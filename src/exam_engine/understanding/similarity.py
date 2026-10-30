"""Batched question comparison with candidate retrieval.

The service distinguishes four relationships and never treats similarity as
proof of duplication:

``exact``
    The normalised text is identical.
``near``
    Lexical cosine is at least the configured near-duplicate threshold.
``semantic``
    Phase 2 embeddings are highly similar and the two questions carry the same
    reasoning profile (type and Bloom level).
``concept_related``
    Concepts overlap, but the questions ask for materially different reasoning
    (different type or Bloom level), or only the looser concept threshold is met.

Candidate generation is two-stage: an inverted term index proposes lexical
neighbours and a vector index proposes semantic neighbours; full pair scoring
runs only for that union. This avoids scoring every question pair on larger
courses while still finding paraphrases that share no words.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from exam_engine.models.enums import Severity, SimilarityKind
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.understanding import SimilarityMatch, make_similarity_id, ordered_pair
from exam_engine.nlp.keywords import analysis_tokens
from exam_engine.understanding.mapping import SemanticIndex

METHOD = "tfidf+embeddings"


@dataclass(frozen=True)
class ReasoningProfile:
    """Primary labels used to decide whether two similar questions differ."""

    question_type: str | None = None
    bloom_level: str | None = None


@dataclass(frozen=True)
class SimilaritySettings:
    near_threshold: float = 0.78
    semantic_threshold: float = 0.68
    concept_threshold: float = 0.35
    top_k: int = 5
    max_candidates: int = 30


@dataclass(frozen=True)
class SimilarityResult:
    matches: list[SimilarityMatch] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)
    semantic_used: bool = False


_LEADING_MARKER_RE = re.compile(
    r"^\s*(?:(?:q(?:uestion)?)\s*[.:\-]?\s*)?"
    r"(?:\(?\d{1,3}\)?\s*[.)\]:\-])\s+",
    re.IGNORECASE,
)


def normalise_question_text(text: str) -> str:
    """Case/whitespace-insensitive form used for exact-duplicate detection.

    A leading question marker (``1.``, ``Q2)``) is ignored because Phase 1 keeps
    the marker in paper text but structured banks do not. The marker is not part
    of the question's meaning; the original stored text is never changed.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    stripped = _LEADING_MARKER_RE.sub("", folded, count=1)
    return " ".join(stripped.split())


def _tfidf_vectors(documents: list[list[str]]) -> list[dict[str, float]]:
    if not documents:
        return []
    document_frequency: Counter[str] = Counter()
    for tokens in documents:
        document_frequency.update(set(tokens))
    total = len(documents)
    vectors: list[dict[str, float]] = []
    for tokens in documents:
        counts = Counter(tokens)
        if not counts:
            vectors.append({})
            continue
        vector = {
            term: (1.0 + math.log(count))
            * (math.log((1 + total) / (1 + document_frequency[term])) + 1.0)
            for term, count in counts.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values()))
        vectors.append({term: value / norm for term, value in vector.items()} if norm else {})
    return vectors


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return max(0.0, min(1.0, sum(value * right.get(term, 0.0) for term, value in left.items())))


def _token_dice(left: list[str], right: list[str]) -> float:
    left_set, right_set = set(left), set(right)
    if not left_set or not right_set:
        return 0.0
    return 2.0 * len(left_set & right_set) / (len(left_set) + len(right_set))


def _shared_terms(left: dict[str, float], right: dict[str, float]) -> list[str]:
    shared = [(term, left[term] * right[term]) for term in left if term in right]
    shared.sort(key=lambda item: (-item[1], item[0]))
    return [term for term, _score in shared[:8]]


def _semantic_candidates(
    questions: list[QuestionRecord],
    index: SemanticIndex,
    *,
    max_candidates: int,
    threshold: float,
) -> dict[int, list[tuple[int, float]]]:
    """Approximate nearest-neighbour retrieval; only candidates are scored later."""
    available = [
        position
        for position, question in enumerate(questions)
        if question.question_id in index.question_vectors
    ]
    if len(available) < 2:
        return {}
    try:
        import numpy as np
        from sklearn.neighbors import NearestNeighbors
    except ImportError:  # pragma: no cover - both are declared dependencies
        return {}

    matrix = np.asarray(
        [index.question_vectors[questions[position].question_id] for position in available],
        dtype=np.float64,
    )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    matrix = matrix / norms
    neighbours = min(len(available), max(2, max_candidates + 1))
    model = NearestNeighbors(n_neighbors=neighbours, metric="cosine", algorithm="brute").fit(matrix)
    distances, indices = model.kneighbors(matrix)

    candidates: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for row, position in enumerate(available):
        for distance, neighbour in zip(distances[row], indices[row], strict=True):
            other = available[int(neighbour)]
            if other == position:
                continue
            score = max(0.0, min(1.0, 1.0 - float(distance)))
            if score >= threshold:
                candidates[position].append((other, score))
    return candidates


def _lexical_candidates(
    documents: list[list[str]], *, max_candidates: int
) -> dict[int, list[tuple[int, float]]]:
    postings: dict[str, list[int]] = defaultdict(list)
    for position, tokens in enumerate(documents):
        for term in set(tokens):
            postings[term].append(position)

    candidates: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for position, tokens in enumerate(documents):
        overlap: Counter[int] = Counter()
        for term in set(tokens):
            for other in postings[term]:
                if other > position:
                    overlap[other] += 1
        ranked = sorted(overlap.items(), key=lambda item: (-item[1], item[0]))[:max_candidates]
        for other, count in ranked:
            candidates[position].append((other, float(count)))
    return candidates


def find_similar_questions(
    course_id: str,
    questions: list[QuestionRecord],
    *,
    semantic: SemanticIndex | None = None,
    profiles: dict[str, ReasoningProfile] | None = None,
    settings: SimilaritySettings | None = None,
) -> SimilarityResult:
    """Return ranked, deduplicated similarity matches for one course."""
    settings = settings or SimilaritySettings()
    if len(questions) < 2:
        return SimilarityResult(
            uncertainties=[
                Uncertainty(
                    code="insufficient_data",
                    message="fewer than two questions are available for comparison",
                    severity=Severity.INFO,
                    detail={"course_id": course_id},
                )
            ]
        )

    profiles = profiles or {}
    texts = [normalise_question_text(question.text) for question in questions]
    tokens = [
        [token.casefold() for token in analysis_tokens(question.text, lemmatize_terms=True)]
        for question in questions
    ]
    vectors = _tfidf_vectors(tokens)

    semantic_used = bool(semantic and semantic.question_similarity_available)
    lexical_candidates = _lexical_candidates(tokens, max_candidates=settings.max_candidates)
    semantic_candidates = (
        _semantic_candidates(
            questions,
            semantic,
            max_candidates=settings.max_candidates,
            threshold=settings.concept_threshold,
        )
        if semantic_used and semantic is not None
        else {}
    )

    exact_groups: dict[str, list[int]] = defaultdict(list)
    for position, text in enumerate(texts):
        exact_groups[text].append(position)

    pairs: set[tuple[int, int]] = set()
    for group in exact_groups.values():
        for offset, left in enumerate(group):
            for right in group[offset + 1 :]:
                pairs.add((min(left, right), max(left, right)))
    for position, candidates in lexical_candidates.items():
        for other, _count in candidates:
            pairs.add((min(position, other), max(position, other)))
    for position, candidates in semantic_candidates.items():
        for other, _score in candidates:
            pairs.add((min(position, other), max(position, other)))

    scored: dict[tuple[int, int], SimilarityMatch] = {}
    for left, right in pairs:
        first, second = questions[left], questions[right]
        tfidf_score = _cosine(vectors[left], vectors[right])
        token_dice = _token_dice(tokens[left], tokens[right])
        # TF-IDF alone punishes a single changed word heavily in tiny courses.
        # Token overlap is the second lexical baseline; the stronger one wins.
        lexical = max(tfidf_score, token_dice)
        semantic_score = None
        if semantic_used and semantic is not None:
            vector_left = semantic.question_vectors.get(first.question_id)
            vector_right = semantic.question_vectors.get(second.question_id)
            if (
                vector_left is not None
                and vector_right is not None
                and len(vector_left) == len(vector_right)
            ):
                semantic_score = _cosine(
                    {str(index): value for index, value in enumerate(vector_left)},
                    {str(index): value for index, value in enumerate(vector_right)},
                )
        exact = texts[left] == texts[right]
        combined = max(lexical, semantic_score or 0.0, 1.0 if exact else 0.0)
        profile_left = profiles.get(first.question_id, ReasoningProfile())
        profile_right = profiles.get(second.question_id, ReasoningProfile())
        different_reasoning = _materially_different(profile_left, profile_right)

        if exact:
            kind = SimilarityKind.EXACT
        elif lexical >= settings.near_threshold:
            kind = SimilarityKind.NEAR
        elif semantic_score is not None and semantic_score >= settings.semantic_threshold:
            # Equivalent demand requires both profiles to be known and equal. If
            # either label is unknown, semantic similarity alone is not enough to
            # claim the questions ask for the same reasoning.
            kind = (
                SimilarityKind.SEMANTIC
                if _same_reasoning(profile_left, profile_right)
                else SimilarityKind.CONCEPT_RELATED
            )
        elif combined >= settings.concept_threshold:
            kind = SimilarityKind.CONCEPT_RELATED
        else:
            continue

        evidence: list[str] = []
        if exact:
            evidence.append("normalised text is identical (case/whitespace insensitive)")
        if lexical > 0.0:
            evidence.append(
                f"lexical similarity {lexical:.3f} "
                f"(tf-idf {tfidf_score:.3f}, token overlap {token_dice:.3f})"
            )
        if semantic_score is not None and semantic is not None:
            evidence.append(f"semantic cosine {semantic_score:.3f} ({semantic.model_id})")
        terms = _shared_terms(vectors[left], vectors[right])
        if terms:
            evidence.append("shared terms: " + ", ".join(terms[:6]))
        if different_reasoning:
            evidence.append(
                "reasoning demands differ: "
                f"type {profile_left.question_type or 'unknown'} vs "
                f"{profile_right.question_type or 'unknown'}, "
                f"Bloom {profile_left.bloom_level or 'unknown'} vs "
                f"{profile_right.bloom_level or 'unknown'}"
            )
        elif (
            kind is SimilarityKind.CONCEPT_RELATED
            and semantic_score is not None
            and semantic_score >= settings.semantic_threshold
        ):
            evidence.append(
                "reasoning profile is incomplete for at least one question; "
                "the pair is not treated as the same demand"
            )
        if kind is not SimilarityKind.EXACT:
            evidence.append("similarity is evidence, not proof of duplication")

        ordered_first, ordered_second = ordered_pair(first.question_id, second.question_id)
        method = METHOD if semantic_score is not None else "tfidf"
        scored[(left, right)] = SimilarityMatch(
            pair_id=make_similarity_id(course_id, ordered_first, ordered_second, kind),
            course_id=course_id,
            question_id=ordered_first,
            matched_question_id=ordered_second,
            kind=kind,
            score=round(combined, 4),
            lexical_score=round(lexical, 4),
            semantic_score=round(semantic_score, 4) if semantic_score is not None else 0.0,
            method=method,
            evidence=evidence,
        )

    # Keep the strongest ``top_k`` relationships for each question, then store the
    # union once per unordered pair.
    by_question: dict[str, list[SimilarityMatch]] = defaultdict(list)
    for match in scored.values():
        by_question[match.question_id].append(match)
        by_question[match.matched_question_id].append(match)
    keep: set[str] = set()
    for matches in by_question.values():
        matches.sort(key=lambda item: (-item.score, item.pair_id))
        keep.update(match.pair_id for match in matches[: settings.top_k])
    # Exact duplicates are the only definitive relationship; never hide one
    # behind the per-question top-k cap used for weaker similarities.
    keep.update(match.pair_id for match in scored.values() if match.kind is SimilarityKind.EXACT)

    result = sorted(
        (match for match in scored.values() if match.pair_id in keep),
        key=lambda item: (-item.score, item.pair_id),
    )
    uncertainties: list[Uncertainty] = []
    if not semantic_used:
        uncertainties.append(
            Uncertainty(
                code="similarity_fallback",
                message=(
                    "Phase 2 question embeddings are unavailable; similarity used "
                    "lexical candidate retrieval and pair scoring only"
                ),
                severity=Severity.INFO,
                detail={"course_id": course_id},
            )
        )
    return SimilarityResult(
        matches=result, uncertainties=uncertainties, semantic_used=semantic_used
    )


def _materially_different(left: ReasoningProfile, right: ReasoningProfile) -> bool:
    if left.question_type and right.question_type and left.question_type != right.question_type:
        return True
    return bool(left.bloom_level and right.bloom_level and left.bloom_level != right.bloom_level)


def _same_reasoning(left: ReasoningProfile, right: ReasoningProfile) -> bool:
    """True only when both profiles are known and identical."""
    return bool(
        left.question_type
        and right.question_type
        and left.question_type == right.question_type
        and left.bloom_level
        and right.bloom_level
        and left.bloom_level == right.bloom_level
    )


__all__ = [
    "METHOD",
    "ReasoningProfile",
    "SimilarityResult",
    "SimilaritySettings",
    "find_similar_questions",
    "normalise_question_text",
]
