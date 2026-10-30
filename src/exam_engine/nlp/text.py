"""Text primitives: normalisation, sentence segmentation, tokenisation, lemmas.

Design rules
------------
* The original extracted text is never modified; this module produces a
  parallel representation plus character spans that point back into it.
* Notation that carries meaning in exam material (mathematical expressions,
  code identifiers, question numbering, marks, abbreviations) is protected from
  sentence splitting and from destructive cleanup.
* Nothing is dropped silently: callers get tokens/spans and can inspect them.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal, cast

from exam_engine.models.nlp import Sentence, Token

UnicodeForm = Literal["NFC", "NFD", "NFKC", "NFKD"]

# --- stop words --------------------------------------------------------------

# Deliberately small and general-purpose: domain words such as "analysis",
# "process", "state" or "class" are meaningful in computing papers and must not
# be treated as noise.
STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all am an and any are as at be because been before
    being below between both but by can cannot could did do does doing down during each few
    for from further had has have having he her here hers herself him himself his how i if in
    into is it its itself just me more most must my myself no nor not of off on once only or
    other ought our ours ourselves out over own same she should so some such than that the
    their theirs them themselves then there these they this those through to too under until
    up very was we were what when where which while who whom why will with would you your
    yours yourself yourselves also may might shall upon among within without whether whose
    """.split()  # noqa: SIM905 - readable word list
)

# Words that look like stop words but change technical meaning.
TECHNICAL_KEEP: frozenset[str] = frozenset(
    {"no", "not", "all", "any", "each", "both", "same", "other", "up", "down", "over", "under"}
)


def is_stopword(token: str) -> bool:
    lowered = token.casefold()
    return lowered in STOPWORDS and lowered not in TECHNICAL_KEEP


# --- normalisation -----------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
_INLINE_SPACE_RE = re.compile(r"[ \t\u00a0\u2000-\u200a]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_UNICODE_FORMS = {"NFC": "NFC", "NFD": "NFD", "NFKC": "NFKC", "NFKD": "NFKD", "none": None}


def normalize_for_analysis(text: str, *, unicode_form: str = "NFKC") -> str:
    """Normalise presentation while preserving wording and notation.

    Paragraph breaks survive as ``\\n\\n`` because they carry section structure;
    intra-line whitespace (including PDF alignment gaps) collapses to one space.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00ad", "")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    form = _UNICODE_FORMS.get(unicode_form.upper(), "NFKC")
    if form:
        text = unicodedata.normalize(cast(UnicodeForm, form), text)
    text = text.replace("\t", " ")
    lines = [_INLINE_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip("\n")


# --- protected spans ---------------------------------------------------------

ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "e.g.",
        "i.e.",
        "etc.",
        "eg.",
        "ie.",
        "fig.",
        "figs.",
        "vs.",
        "cf.",
        "al.",
        "dr.",
        "mr.",
        "mrs.",
        "ms.",
        "prof.",
        "eq.",
        "eqs.",
        "no.",
        "nos.",
        "ref.",
        "refs.",
        "sec.",
        "ch.",
        "vol.",
        "pp.",
        "p.",
        "approx.",
        "resp.",
        "viz.",
        "ph.d.",
        "b.sc.",
        "m.sc.",
        "u.s.",
        "u.k.",
    }
)

# Character used to mask a dot that must not end a sentence. It is restored
# before anything is returned, and keeps offsets stable because the mask is
# exactly one character wide.
_MASK = "\x01"

_MATH_CALL_RE = re.compile(r"\b[A-Za-z]\s*\(\s*[^()\n]{1,60}\)")
_DECIMAL_RE = re.compile(r"\d\.\d")
_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){2,}\b")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*(?:::\w+|_\w+|\w*_\w*)+\b")
_URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+")
_MARKS_RE = re.compile(r"[\[(]\s*\d{1,3}(?:\.\d+)?\s*(?:marks?|mks?|m)\s*[\])]", re.IGNORECASE)
_BRACKET_NUMBER_RE = re.compile(r"[\[(]\s*\d{1,3}(?:\.\d+)?\s*[\])]")
_SINGLE_INITIAL_RE = re.compile(r"\b[A-Z]\.(?=\s+[A-Z])")
# "1. Define a stack" is one sentence: the marker dot is not a terminator.
_LIST_MARKER_RE = re.compile(r"(?<![\w.])\d{1,3}\.(?=\s)")


def protected_spans(text: str) -> list[tuple[int, int]]:
    """Spans whose punctuation must not be treated as a sentence boundary."""
    spans: list[tuple[int, int]] = []
    for pattern in (
        _MATH_CALL_RE,
        _URL_RE,
        _IDENTIFIER_RE,
        _VERSION_RE,
        _MARKS_RE,
        _BRACKET_NUMBER_RE,
    ):
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text))
    # Abbreviations and single initials: mask only the trailing dot.
    for match in re.finditer(r"\b[A-Za-z][A-Za-z.]{0,7}\.", text):
        candidate = match.group(0).casefold()
        if candidate in ABBREVIATIONS or _SINGLE_INITIAL_RE.fullmatch(match.group(0)):
            spans.append((match.end() - 1, match.end()))
    for match in _LIST_MARKER_RE.finditer(text):
        spans.append((match.end() - 1, match.end()))
    for match in _DECIMAL_RE.finditer(text):
        spans.append((match.start() + 1, match.start() + 2))
    return spans


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            if chars[index] in ".!?":
                chars[index] = _MASK
    return "".join(chars)


def _unmask(text: str, original: str, offset: int = 0) -> str:
    """Restore masked characters, mapping slice positions back to the source."""
    return "".join(
        original[offset + index] if char == _MASK and offset + index < len(original) else char
        for index, char in enumerate(text)
    )


_TERMINAL_RE = re.compile(r"[.!?][\"')\]]?\s+")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_LIST_START_RE = re.compile(r"^\s*(?:\d{1,3}[.)]|\(?[a-zA-Z0-9]{1,4}\)|[-–—•*·])\s+")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?$")


def segment_sentences(text: str, *, min_chars: int = 2) -> list[Sentence]:
    """Split text into sentences, protecting notation and abbreviations.

    Splits happen at terminal punctuation followed by whitespace, at paragraph
    breaks, and before list/question markers on a new line. A sentence that is
    only whitespace is dropped; its absence is visible in the spans.
    """
    if not text.strip():
        return []

    masked = _mask(text, protected_spans(text))
    boundaries: set[int] = {0}

    for match in _TERMINAL_RE.finditer(masked):
        end = match.end()
        boundaries.add(end)

    for match in _PARAGRAPH_RE.finditer(masked):
        boundaries.add(match.end())

    for match in re.finditer(r"\n", masked):
        before = masked[: match.start()].rstrip()
        after = masked[match.end() :]
        if not before or not after:
            continue
        # A line break is a boundary when the previous line ends a sentence or
        # when the next line starts a numbered/bulleted item.
        if _SENTENCE_END_RE.search(before) or _LIST_START_RE.match(after):
            boundaries.add(match.start() + 1)

    boundaries.add(len(masked))
    ordered = sorted(boundaries)

    sentences: list[Sentence] = []
    index = 0
    for start, end in zip(ordered, ordered[1:], strict=False):
        raw = masked[start:end]
        restored = _unmask(raw, text, start)
        stripped = restored.strip()
        if len(stripped) < min_chars:
            continue
        # Recompute the span after trimming so offsets stay exact.
        leading = len(restored) - len(restored.lstrip())
        trailing = len(restored) - len(restored.rstrip())
        sentences.append(
            Sentence(
                index=index,
                text=stripped,
                start=start + leading,
                end=end - trailing,
                token_count=len(tokenize(stripped)),
            )
        )
        index += 1
    return sentences


# --- tokenisation ------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<math_call>\b[A-Za-z]\s*\(\s*[^()\n]{1,60}\))          # O(n log n), f(x)
    |(?P<url>https?://\S+|www\.\S+|\S+@\S+\.\S+)
    |(?P<identifier>[A-Za-z_]\w*(?:::\w+)+|[A-Za-z_]\w*_\w+|\w*_\w+)
    |(?P<lang>[A-Za-z]+[+#]{1,2})                              # C++, C#
    |(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    |(?P<word>[^\W\d_]+(?:['’\-][^\W\d_]+)*)
    |(?P<symbol>[+\-*/^=<>≤≥≠≈±×÷%∑∏∫√∞∈∉⊂⊆∪∩→←↔°])
    """,
    re.VERBOSE | re.UNICODE,
)

_WORD_GROUPS = frozenset({"math_call", "url", "identifier", "lang", "number", "word"})


def tokenize(text: str) -> list[Token]:
    """Tokenise text, keeping notation and technical identifiers intact."""
    tokens: list[Token] = []
    for match in _TOKEN_RE.finditer(text):
        group = match.lastgroup or ""
        value = match.group(0)
        is_word = group in _WORD_GROUPS
        technical = bool(
            group in {"math_call", "url", "identifier", "lang"}
            or (group == "number" and any(char in value for char in ".eE"))
        )
        tokens.append(
            Token(
                text=value,
                start=match.start(),
                end=match.end(),
                is_word=is_word,
                is_technical=technical,
            )
        )
    return tokens


def token_texts(text: str, *, drop_stopwords: bool = False) -> list[str]:
    values = [t.text for t in tokenize(text) if t.is_word]
    if drop_stopwords:
        values = [v for v in values if not is_stopword(v)]
    return values


# --- lemmatisation -----------------------------------------------------------

_IRREGULAR: dict[str, str] = {
    "am": "be",
    "is": "be",
    "are": "be",
    "was": "be",
    "were": "be",
    "been": "be",
    "being": "be",
    "has": "have",
    "have": "have",
    "had": "have",
    "having": "have",
    "does": "do",
    "did": "do",
    "done": "do",
    "doing": "do",
    "children": "child",
    "indices": "index",
    "indexes": "index",
    "matrices": "matrix",
    "vertices": "vertex",
    "analyses": "analysis",
    "bases": "basis",
    "theses": "thesis",
    "hypotheses": "hypothesis",
    "criteria": "criterion",
    "phenomena": "phenomenon",
    "data": "datum",
    "alumni": "alumnus",
    "formulae": "formula",
    "formulas": "formula",
    "leaves": "leaf",
    "halves": "half",
    "lives": "life",
    "wives": "wife",
    "better": "good",
    "worse": "bad",
}

_VOWELS = set("aeiou")


def lemmatize(word: str) -> str:
    """Rule-based English lemma.

    This is an approximation, not full morphology: it handles plural nouns and
    regular verb endings, and deliberately leaves technical identifiers,
    acronyms and short words untouched. Anything it cannot reduce is returned
    unchanged, so no information is invented.
    """
    if not word or not word.isalpha():
        return word
    lowered = word.casefold()
    if len(lowered) <= 3 or lowered.isupper():
        return lowered
    if lowered in _IRREGULAR:
        return _IRREGULAR[lowered]
    if lowered.endswith("ies") and len(lowered) > 4:
        return lowered[:-3] + "y"
    if lowered.endswith(("sses", "shes", "ches", "xes", "zes")):
        return lowered[:-2]
    if lowered.endswith("ing") and len(lowered) > 5:
        stem = lowered[:-3]
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
            stem = stem[:-1]
        return stem
    if lowered.endswith("ed") and len(lowered) > 4:
        stem = lowered[:-2]
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
            stem = stem[:-1]
        return stem
    if lowered.endswith("s") and not lowered.endswith(("ss", "us", "is", "as", "os")):
        return lowered[:-1]
    return lowered


def lemmatize_tokens(text: str) -> list[str]:
    return [lemmatize(t.text) for t in tokenize(text) if t.is_word]


# --- language ----------------------------------------------------------------

_SCRIPT_RANGES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("zh", re.compile(r"[\u4e00-\u9fff]")),
    ("ja", re.compile(r"[\u3040-\u30ff]")),
    ("ko", re.compile(r"[\uac00-\ud7af]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
)


def detect_language(text: str) -> str | None:
    """Coarse language hint: ``en`` when English stop words dominate, else a script code.

    Returns ``None`` when there is not enough evidence; callers must treat that
    as "unknown", never guess.
    """
    words = [t.text for t in tokenize(text) if t.is_word]
    if not words:
        for code, pattern in _SCRIPT_RANGES:
            if pattern.search(text):
                return code
        return None
    stop_hits = sum(1 for word in words if word.casefold() in STOPWORDS)
    if stop_hits / len(words) >= 0.15:
        return "en"
    for code, pattern in _SCRIPT_RANGES:
        if pattern.search(text):
            return code
    # Latin script with no stop words: single words, headings, lists.
    if all(re.fullmatch(r"[A-Za-z0-9\W_]+", word) for word in words[:50]):
        return "en" if len(words) >= 3 else None
    return None


__all__ = [
    "ABBREVIATIONS",
    "STOPWORDS",
    "TECHNICAL_KEEP",
    "detect_language",
    "is_stopword",
    "lemmatize",
    "lemmatize_tokens",
    "normalize_for_analysis",
    "protected_spans",
    "segment_sentences",
    "token_texts",
    "tokenize",
]
