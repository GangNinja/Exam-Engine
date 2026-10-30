"""Conservative text normalisation.

Cleaning must never destroy mathematical notation, question numbering, marks or
unit labels, so this module only removes characters that carry no meaning
(control codes, zero-width joiners, soft hyphens) and collapses runs of spaces.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# C0 controls except tab (\x09) and newline (\x0a); form feed (\x0c) is page
# separated by adapters before normalisation, so it is removed here.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
_SOFT_HYPHEN = "\u00ad"
_HORIZONTAL_SPACE_RE = re.compile(r"[ \t\u00a0\u2000-\u200a]{2,}")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")

# Page furniture that carries no question content.
_PAGE_NUMBER_RES = (
    re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*(?:of|/)\s*\d{1,4}\s*$", re.IGNORECASE),
    re.compile(r"^\s*[-–—]\s*\d{1,4}\s*[-–—]\s*$"),
    re.compile(r"^\s*(?:page|pg\.?)\s*\d{1,4}\s*$", re.IGNORECASE),
    re.compile(r"^\s*\d{1,4}\s*\|\s*(?:p\s*a\s*g\s*e)?\s*$", re.IGNORECASE),
)

# Symbols that change mathematical meaning, kept in duplicate-detection fingerprints.
# Brackets, punctuation, slashes and carets are intentionally dropped: the
# fingerprint is an advisory duplicate key, and keeping them caused identical
# questions with different punctuation to look distinct.
_MATH_SYMBOLS = set("+-=<>≤≥≠≈±×÷√∫∑∏∂∞∈∉⊂⊆∪∩→←↔πθλαβγμΔΩ°")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """Normalise whitespace and strip meaningless characters, preserving content."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(_SOFT_HYPHEN, "")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\t", " ")
    lines = []
    for line in text.split("\n"):
        line = _HORIZONTAL_SPACE_RE.sub(" ", line).rstrip()
        lines.append(line)
    text = "\n".join(lines)
    text = _EXCESS_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip("\n")


def is_page_artifact(line: str) -> bool:
    """True for lines that are only page furniture (page numbers etc.)."""
    stripped = line.strip()
    if not stripped:
        return False
    return any(pattern.match(stripped) for pattern in _PAGE_NUMBER_RES)


def drop_page_artifacts(text: str) -> str:
    """Remove page-number-only lines from an extracted block."""
    return "\n".join(line for line in text.split("\n") if not is_page_artifact(line))


def fingerprint_text(text: str) -> str:
    """Stable digest for duplicate detection (never shown to users).

    Case, punctuation spacing and unicode presentation differences are ignored,
    but mathematical symbols stay significant so two different formulae do not
    collide.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept: list[str] = []
    for char in folded:
        if char.isalnum() or char in _MATH_SYMBOLS:
            kept.append(char)
        elif char.isspace():
            continue
    return hashlib.sha256("".join(kept).encode("utf-8")).hexdigest()


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def looks_like_math_or_notation(text: str) -> bool:
    """Heuristic used to protect blocks from over-aggressive downstream cleanup."""
    return any(char in _MATH_SYMBOLS for char in text)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
