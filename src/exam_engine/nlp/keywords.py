"""Keyword and keyphrase extraction.

Three transparent baselines are provided:

* TF-IDF over sentence-bounded word n-grams (``TfidfKeywordExtractor``) — ranks
  terms that are distinctive for a document relative to its course corpus;
* RAKE (``RakeKeywordExtractor``) — an unsupervised phrase scorer that needs no
  corpus, useful when a course has only one document.

``HybridKeywordExtractor`` averages min-max normalised scores of both.

N-grams never cross sentence boundaries, multiword phrases outrank the single
words they contain (a phrase is the more specific term), and paper furniture such
as "marks" or "12 hours" is dropped unless it is part of a real phrase.

None of these measures educational importance. They measure how distinctive a
term is inside the supplied text; verified concepts come from the syllabus parser.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from exam_engine.models.enums import NlpMethod, TextOwnerType
from exam_engine.models.nlp import KeyphraseRecord, make_keyphrase_id
from exam_engine.nlp.text import is_stopword, lemmatize, segment_sentences, tokenize

# Joins tokens inside a generated n-gram. It must not appear in normal text, so a
# technical token that itself contains spaces ("O(n log n)") stays one token.
_NGRAM_SEP = "\x1f"

_TERM_RE = re.compile(r"[^a-z0-9+#]+")

# Exam paper furniture: formatting, not course content. Dropped only when a term
# carries no other content word, so "unit testing" survives while "12 hours"
# does not.
FURNITURE_TERMS: frozenset[str] = frozenset(
    {
        "marks",
        "mark",
        "mks",
        "hours",
        "hrs",
        "unit",
        "units",
        "module",
        "modules",
        "question",
        "questions",
        "answer",
        "answers",
        "section",
        "paper",
        "syllabus",
        "page",
        "pages",
        "chapter",
        "chapters",
        "semester",
        "exam",
        "examination",
        "candidate",
        "candidates",
        "note",
        "notes",
    }
)


def normalize_term(term: str) -> str:
    return _TERM_RE.sub(" ", term.casefold()).strip()


def _is_noise_term(normalized: str) -> bool:
    """True when a term is only paper furniture and numbers."""
    words = normalized.split()
    if not words:
        return True
    return all(
        word in FURNITURE_TERMS or not any(char.isalpha() for char in word) for word in words
    )


@dataclass(frozen=True)
class KeywordDocument:
    """One text to extract keyphrases from, with its owner for provenance."""

    owner_type: TextOwnerType
    owner_id: str
    text: str
    course_id: str
    document_id: str | None = None


class KeywordExtractor(Protocol):
    name: NlpMethod

    def extract(
        self, documents: Sequence[KeywordDocument], *, top_k: int = 20
    ) -> list[KeyphraseRecord]:
        """Return ranked keyphrases, each carrying its owner and method."""
        ...


# --- shared tokenisation -----------------------------------------------------


def analysis_tokens(
    text: str, *, lemmatize_terms: bool = False, drop_stopwords: bool = True
) -> list[str]:
    """Tokens suitable for term statistics.

    Single characters, pure punctuation and (by default) stop words are dropped,
    but technical tokens (identifiers, notation, numbers with decimals) are kept
    even when short, because ``O(n log n)`` and ``C++`` are content here.
    """
    values: list[str] = []
    for token in tokenize(text):
        if not token.is_word:
            continue
        value = token.text
        if token.is_technical:
            values.append(value)
            continue
        if len(value) < 2 or (drop_stopwords and is_stopword(value)):
            continue
        values.append(lemmatize(value) if lemmatize_terms else value)
    return values


def sentence_ngrams(
    text: str,
    *,
    max_ngram: int = 3,
    lemmatize_terms: bool = False,
    drop_stopwords: bool = True,
) -> list[str]:
    """All 1..max n-grams, generated inside sentence boundaries.

    Tokens are joined with a private separator so a token that contains spaces
    (``O(n log n)``) is not split when the term is displayed or counted later.
    """
    limit = max(1, max_ngram)
    grams: list[str] = []
    for sentence in segment_sentences(text) or []:
        tokens = analysis_tokens(
            sentence.text, lemmatize_terms=lemmatize_terms, drop_stopwords=drop_stopwords
        )
        for size in range(1, limit + 1):
            if size > len(tokens):
                break
            grams.extend(
                _NGRAM_SEP.join(tokens[index : index + size])
                for index in range(len(tokens) - size + 1)
            )
    return grams


def _tokens_of(raw: str) -> list[str]:
    return raw.split(_NGRAM_SEP)


def _display(raw: str) -> tuple[str, int]:
    tokens = _tokens_of(raw)
    return " ".join(tokens), len(tokens)


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if list(haystack[start : start + len(needle)]) == list(needle):
            return True
    return False


def prefer_phrases(
    entries: Sequence[tuple[str, float]], *, window: int = 200, ratio: float = 0.9
) -> list[tuple[str, float]]:
    """Drop a term when a comparable-scoring longer phrase contains it.

    Keyphrases are more specific than the words inside them, so ``binary search
    tree`` replaces ``binary``/``search``/``tree`` when it scores at least
    ``ratio`` (90% by default) as high. The rule is one-directional and bounded to
    the top ``window`` candidates so large vocabularies stay cheap.
    """
    kept: list[tuple[str, float]] = []
    for raw, score in entries[:window]:
        tokens = _tokens_of(raw)
        if any(
            len(_tokens_of(kept_raw)) > len(tokens)
            and _contains(_tokens_of(kept_raw), tokens)
            and kept_score >= ratio * score
            for kept_raw, kept_score in kept
        ):
            continue
        if len(tokens) > 1:
            kept = [
                (kept_raw, kept_score)
                for kept_raw, kept_score in kept
                if not (
                    len(_tokens_of(kept_raw)) < len(tokens)
                    and _contains(tokens, _tokens_of(kept_raw))
                    and score >= ratio * kept_score
                )
            ]
        kept.append((raw, score))
    return kept


def _finalize(
    scores: dict[str, float],
    *,
    documents_by_term: dict[str, KeywordDocument],
    top_k: int,
    method: NlpMethod,
    extra: dict[str, dict[str, float]] | None = None,
    length_weighted: bool = True,
) -> list[KeyphraseRecord]:
    """Rank terms and build records.

    ``length_weighted`` multiplies the raw score by the token count so a
    two-word concept competes fairly with the single words inside it. The raw
    score is kept in ``metadata`` so the weighting stays auditable.
    """
    weighted = {
        raw: (score * len(_tokens_of(raw)) if length_weighted else score)
        for raw, score in scores.items()
    }
    ordered = sorted(weighted.items(), key=lambda item: (-item[1], item[0]))
    records: list[KeyphraseRecord] = []
    seen: set[str] = set()
    for raw, score in prefer_phrases(ordered):
        if len(records) >= top_k:
            break
        term, n_gram = _display(raw)
        normalized = normalize_term(term)
        if not normalized or normalized in seen or _is_noise_term(normalized):
            continue
        seen.add(normalized)
        source = documents_by_term[raw]
        metadata: dict[str, Any] = {
            "n_tokens": n_gram,
            "raw_score": round(float(scores[raw]), 6),
        }
        if extra and raw in extra:
            metadata["component_scores"] = extra[raw]
        records.append(
            KeyphraseRecord(
                keyphrase_id=make_keyphrase_id(
                    source.owner_type, source.owner_id, method, normalized
                ),
                course_id=source.course_id,
                owner_type=source.owner_type,
                owner_id=source.owner_id,
                document_id=source.document_id,
                term=term,
                normalized=normalized,
                score=round(float(score), 6),
                method=method,
                rank=len(records) + 1,
                n_gram=n_gram,
                metadata=metadata,
            )
        )
    return records


# --- TF-IDF ------------------------------------------------------------------


class TfidfKeywordExtractor:
    """Corpus-relative TF-IDF ranking over sentence-bounded word n-grams.

    Score is the maximum TF-IDF of the term across documents, and each term is
    attributed to the document where it scores highest. Values are comparable
    within a run, not across runs with different corpora.
    """

    name = NlpMethod.TFIDF

    def __init__(
        self,
        *,
        max_ngram: int = 3,
        min_df: int = 1,
        sublinear: bool = True,
        lemmatize_terms: bool = False,
        drop_stopwords: bool = True,
    ) -> None:
        self.max_ngram = max(1, max_ngram)
        self.min_df = max(1, min_df)
        self.sublinear = sublinear
        self.lemmatize_terms = lemmatize_terms
        self.drop_stopwords = drop_stopwords

    def extract(
        self, documents: Sequence[KeywordDocument], *, top_k: int = 20
    ) -> list[KeyphraseRecord]:
        if not documents:
            return []
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError:  # pragma: no cover - scikit-learn is a hard dependency
            return []

        gram_lists = [
            sentence_ngrams(
                document.text,
                max_ngram=self.max_ngram,
                lemmatize_terms=self.lemmatize_terms,
                drop_stopwords=self.drop_stopwords,
            )
            for document in documents
        ]
        if not any(gram_lists):
            return []

        vectorizer = TfidfVectorizer(
            analyzer=lambda raw: raw,  # the document is already a list of n-grams
            preprocessor=None,
            tokenizer=None,
            lowercase=False,
            min_df=self.min_df,
            sublinear_tf=self.sublinear,
            norm="l2",
        )
        matrix = vectorizer.fit_transform(gram_lists)
        terms = list(vectorizer.get_feature_names_out())
        dense = matrix.toarray()
        best_doc = dense.argmax(axis=0)
        counts = matrix.sum(axis=0).A1
        doc_freq = (matrix > 0).sum(axis=0).A1
        scores: dict[str, float] = {}
        documents_by_term: dict[str, KeywordDocument] = {}
        for index, term in enumerate(terms):
            # A phrase seen once is usually an arbitrary word window; keep only
            # collocations that recur in the corpus (or twice in one document).
            if len(_tokens_of(term)) > 1 and doc_freq[index] < 2 and counts[index] < 2:
                continue
            scores[term] = float(dense[:, index].max())
            documents_by_term[term] = documents[int(best_doc[index])]
        return _finalize(
            scores,
            documents_by_term=documents_by_term,
            top_k=top_k,
            method=self.name,
            length_weighted=True,
        )


# --- RAKE --------------------------------------------------------------------

_PHRASE_SPLIT_RE = re.compile(r"[.;:!?,\n()\[\]{}\"“”]+")


class RakeKeywordExtractor:
    """RAKE: score candidate phrases by word degree over word frequency.

    Works with a single document, which makes it the fallback when a course has
    too little text for corpus statistics.
    """

    name = NlpMethod.RAKE

    def __init__(
        self,
        *,
        max_phrase_words: int = 3,
        min_word_chars: int = 2,
        lemmatize_terms: bool = False,
        drop_stopwords: bool = True,
    ) -> None:
        self.max_phrase_words = max(2, max_phrase_words)
        self.min_word_chars = min_word_chars
        self.lemmatize_terms = lemmatize_terms
        self.drop_stopwords = drop_stopwords

    def _phrases(self, text: str) -> list[list[str]]:
        phrases: list[list[str]] = []
        for sentence in segment_sentences(text):
            for chunk in _PHRASE_SPLIT_RE.split(sentence.text):
                current: list[str] = []
                for token in tokenize(chunk):
                    if not token.is_word:
                        if current:
                            phrases.append(current)
                            current = []
                        continue
                    word = token.text
                    if self.drop_stopwords and is_stopword(word) and not token.is_technical:
                        if current:
                            phrases.append(current)
                            current = []
                        continue
                    if not token.is_technical and len(word) < self.min_word_chars:
                        continue
                    current.append(lemmatize(word) if self.lemmatize_terms else word)
                if current:
                    phrases.append(current)
        return [phrase[: self.max_phrase_words] for phrase in phrases if phrase]

    def extract(
        self, documents: Sequence[KeywordDocument], *, top_k: int = 20
    ) -> list[KeyphraseRecord]:
        if not documents:
            return []
        frequency: Counter[str] = Counter()
        degree: Counter[str] = Counter()
        phrase_counts: Counter[str] = Counter()
        documents_by_term: dict[str, KeywordDocument] = {}

        for document in documents:
            for phrase in self._phrases(document.text):
                raw = _NGRAM_SEP.join(phrase)
                phrase_counts[raw] += 1
                documents_by_term.setdefault(raw, document)
                lowered = [word.casefold() for word in phrase]
                for word in lowered:
                    frequency[word] += 1
                    degree[word] += len(lowered) - 1

        scores: dict[str, float] = {}
        for raw, count in phrase_counts.items():
            if len(_tokens_of(raw)) > 1 and count < 2:
                continue
            tokens = _tokens_of(raw)
            total = 0.0
            for word in (token.casefold() for token in tokens):
                freq = frequency[word]
                if freq:
                    total += (degree[word] + freq) / freq
            # Length-normalised so a long one-off phrase cannot outrank a
            # repeated short one; repetition is then rewarded explicitly.
            scores[raw] = (total / len(tokens)) * count

        return _finalize(
            scores,
            documents_by_term=documents_by_term,
            top_k=top_k,
            method=self.name,
            # RAKE already accumulates per-word contribution, so length is not
            # applied twice.
            length_weighted=False,
        )


# --- hybrid ------------------------------------------------------------------


def _min_max(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    values = list(scores.values())
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return dict.fromkeys(scores, 1.0)
    return {term: (value - low) / (high - low) for term, value in scores.items()}


class HybridKeywordExtractor:
    """Equal-weight average of normalised TF-IDF and RAKE scores.

    This is a heuristic combination, not a tuned model: a term that is both
    corpus-distinctive and phrase-coherent ranks above one that wins on a single
    signal.
    """

    name = NlpMethod.HYBRID

    def __init__(
        self,
        *,
        max_ngram: int = 3,
        lemmatize_terms: bool = False,
        drop_stopwords: bool = True,
    ) -> None:
        self.tfidf = TfidfKeywordExtractor(
            max_ngram=max_ngram,
            lemmatize_terms=lemmatize_terms,
            drop_stopwords=drop_stopwords,
        )
        self.rake = RakeKeywordExtractor(
            lemmatize_terms=lemmatize_terms, drop_stopwords=drop_stopwords
        )

    def extract(
        self, documents: Sequence[KeywordDocument], *, top_k: int = 20
    ) -> list[KeyphraseRecord]:
        tfidf = self.tfidf.extract(documents, top_k=top_k * 3)
        rake = self.rake.extract(documents, top_k=top_k * 3)
        if not tfidf and not rake:
            return []
        if not tfidf:
            return [self._relabel(record) for record in rake[:top_k]]
        if not rake:
            return [self._relabel(record) for record in tfidf[:top_k]]

        tfidf_scores = _min_max({record.normalized: record.score for record in tfidf})
        rake_scores = _min_max({record.normalized: record.score for record in rake})
        by_term: dict[str, KeyphraseRecord] = {}
        for record in tfidf:
            by_term[record.normalized] = record
        for record in rake:
            by_term.setdefault(record.normalized, record)

        combined: dict[str, float] = {}
        extra: dict[str, dict[str, float]] = {}
        for normalized in by_term:
            left = tfidf_scores.get(normalized, 0.0)
            right = rake_scores.get(normalized, 0.0)
            if left == 0.0 and right == 0.0:
                continue
            combined[normalized] = (left + right) / 2.0
            extra[normalized] = {"tfidf": round(left, 6), "rake": round(right, 6)}

        ranked = sorted(combined.items(), key=lambda item: (-item[1], item[0]))
        records: list[KeyphraseRecord] = []
        for rank, (normalized, score) in enumerate(ranked[:top_k], start=1):
            source = by_term[normalized]
            records.append(
                KeyphraseRecord(
                    keyphrase_id=make_keyphrase_id(
                        source.owner_type, source.owner_id, self.name, normalized
                    ),
                    course_id=source.course_id,
                    owner_type=source.owner_type,
                    owner_id=source.owner_id,
                    document_id=source.document_id,
                    term=source.term,
                    normalized=normalized,
                    score=round(float(score), 6),
                    method=self.name,
                    rank=rank,
                    n_gram=source.n_gram,
                    metadata={"component_scores": extra[normalized]},
                )
            )
        return records

    def _relabel(self, record: KeyphraseRecord) -> KeyphraseRecord:
        """Re-scope a single-signal result to this method without stale ids."""
        return record.model_copy(
            update={
                "method": self.name,
                "keyphrase_id": make_keyphrase_id(
                    record.owner_type, record.owner_id, self.name, record.normalized
                ),
            }
        )


EXTRACTORS: dict[str, KeywordExtractor] = {
    "tfidf": TfidfKeywordExtractor(),
    "rake": RakeKeywordExtractor(),
    "hybrid": HybridKeywordExtractor(),
}


def get_extractor(
    name: str, *, lemmatize_terms: bool = False, drop_stopwords: bool = True
) -> KeywordExtractor:
    """Build an extractor configured for the requested term handling."""
    key = name.casefold()
    if key == "tfidf":
        return TfidfKeywordExtractor(lemmatize_terms=lemmatize_terms, drop_stopwords=drop_stopwords)
    if key == "rake":
        return RakeKeywordExtractor(lemmatize_terms=lemmatize_terms, drop_stopwords=drop_stopwords)
    if key == "hybrid":
        return HybridKeywordExtractor(
            lemmatize_terms=lemmatize_terms, drop_stopwords=drop_stopwords
        )
    raise KeyError(
        f"unknown keyword extractor '{name}'; available: {', '.join(sorted(EXTRACTORS))}"
    )


__all__ = [
    "EXTRACTORS",
    "FURNITURE_TERMS",
    "HybridKeywordExtractor",
    "KeywordDocument",
    "KeywordExtractor",
    "RakeKeywordExtractor",
    "TfidfKeywordExtractor",
    "analysis_tokens",
    "get_extractor",
    "normalize_term",
    "prefer_phrases",
    "sentence_ngrams",
]
