"""Conservative quality gate for derived concept and topic labels.

The extractors in this project are deliberately lexical.  They are allowed to
propose candidates, but a candidate is not an academic concept merely because it
appeared in a question bank.  This module centralises the small set of rejects
that can be established without semantic interpretation:

* page headers/footers and source-document furniture;
* option labels, part labels and bare schematic letters;
* instruction/boilerplate words (``NOT``, ``PAPER``, ``SOLVED``, ...);
* stripped function/option notation (``f(x)``, ``A(c)``, ``P(B)``);
* fragments beginning with a connective and phrases with no alphabetic content.

It does not attempt to reject every unusual string.  In particular, short
technical terms (``DNA``, ``ATP``, ``AC``, ``C++``, ``O(n log n)``) and chemical
formulae (``H2O``, ``CO2``, ``NH3``) are retained.  A rejected label remains in
the source/NLP record; callers exclude it only from derived concept and topic
views.
"""

from __future__ import annotations

import re

from exam_engine.nlp.keywords import normalize_term

# Source furniture and instruction words.  These are exact normalized labels.
_NOISE_LABELS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "answer",
        "answers",
        "chapter",
        "chapters",
        "code",
        "correct",
        "does",
        "false",
        "gate",
        "iit",
        "cbse",
        "ncert",
        "nta",
        "nmc",
        "no",
        "note",
        "not",
        "of",
        "one",
        "option",
        "options",
        "or",
        "page",
        "paper",
        "pdf",
        "practical",
        "practicals",
        "question",
        "questions",
        "section",
        "sections",
        "solved",
        "the",
        "true",
        "two",
        "unit",
        "units",
        "use",
        "sem",
        "semester",
        "marks",
        "mark",
        "time",
        "minutes",
        "syllabus",
        "download",
        "click",
        "here",
        "given",
        "below",
        "above",
        "following",
        "choose",
        "select",
        "match",
        "statement",
        "statements",
        "assertion",
        "reason",
        "solution",
        "solutions",
    }
)

# Short labels that are legitimate technical terms rather than schematic
# option/part labels.  Keep this list explicit and small.
_TECHNICAL_SHORT: frozenset[str] = frozenset(
    {
        "ac",
        "ai",
        "atp",
        "avl",
        "bfs",
        "bst",
        "dfs",
        "gcd",
        "lcm",
        "c",
        "c#",
        "c++",
        "cpu",
        "css",
        "db",
        "dc",
        "dna",
        "fm",
        "go",
        "gpu",
        "html",
        "http",
        "https",
        "json",
        "ml",
        "nlp",
        "os",
        "php",
        "ph",
        "ram",
        "rna",
        "rom",
        "r",
        "sql",
        "tcp",
        "tv",
        "udp",
        "uri",
        "url",
        "xml",
    }
)

# Chemical formulae with an element count are meaningful; CH2 is explicitly
# excluded because it is a common OCR fragment for a substituted group and is
# not a complete concept by itself.
_CHEMICAL_RE = re.compile(r"^(?:[A-Z][a-z]?\d{0,3}){2,6}$")
_CHEMICAL_KEEP: frozenset[str] = frozenset(
    {"h2o", "co2", "nh3", "so2", "so3", "no2", "n2o", "ch4", "o2", "h2", "n2", "co"}
)
_FUNCTION_RE = re.compile(r"^[a-z]\s*\(\s*[^()]{1,40}\s*\)$", re.IGNORECASE)
_ASYMPTOTIC_RE = re.compile(r"^O\s*\(.+\)$")
_OPTION_LABEL_RE = re.compile(r"^[a-z]\s*(?:[0-9]+|\(\s*[^()]{1,20}\s*\))$", re.IGNORECASE)
_ROMAN_RE = re.compile(r"^(?:i{1,3}|iv|v|vi{1,3}|ix|x{1,3})\.?$")
_BARE_NUMBER_RE = re.compile(r"^[\d\s.,%+\-–—/]+$")
_PAGE_RE = re.compile(r"\bpage\s+\d+(?:\s+of\s+\d+)?\b", re.IGNORECASE)
_SOURCE_RE = re.compile(
    r"(?:official\s+reference|syllabus\s+for|cbse\s+class|university|institute|"
    r"public\s+notice|national\s+medical|nta\s+neet|use\s+the\s+official\s+pdf|"
    r"checklist|download|source\s+document|pdf\s+version)",
    re.IGNORECASE,
)
_LEADING_CONNECTIVE_RE = re.compile(
    r"^(?:of|the|and|or|to|in|for|with|by|on|as|at|from|that|which|their|its|"
    r"this|these|those|a|an)\b",
    re.IGNORECASE,
)
_TRAILING_CONNECTOR_RE = re.compile(
    r"\b(?:of|the|and|or|to|in|for|with|by|on|as|at|from|that|which|because)\s*$",
    re.IGNORECASE,
)
_ACRONYM_RE = re.compile(r"^[A-Z0-9][A-Z0-9+#./-]{1,12}$")
_UPPER_WORD_RE = re.compile(r"^[A-Z]{2,}$")


def _raw(value: str) -> str:
    return " ".join(str(value or "").split())


def concept_noise_reason(label: str) -> str | None:
    """Return a stable reason when *label* is unsuitable as a concept/topic.

    ``None`` means the label passes this conservative gate.  This is a
    provenance-preserving filter: removing a label from a derived view is not a
    correction to the source record.
    """
    raw = _raw(label)
    normalized = normalize_term(raw)
    if not raw or not normalized:
        return "empty_label"
    if len(raw) > 500:
        return "overlong_label"
    if raw.endswith(")") and "(" not in raw:
        return "unbalanced_parenthesis"
    if _PAGE_RE.search(raw):
        return "page_artifact"
    if _SOURCE_RE.search(raw):
        return "source_furniture"
    if normalized in _NOISE_LABELS:
        return "instruction_or_furniture"
    if _ASYMPTOTIC_RE.fullmatch(raw):
        return None
    if _FUNCTION_RE.fullmatch(raw):
        return "bare_notation"
    if _OPTION_LABEL_RE.fullmatch(raw):
        return "option_or_part_label"
    if _ROMAN_RE.fullmatch(normalized):
        return "roman_numeral"
    if _BARE_NUMBER_RE.fullmatch(raw):
        return "numeric_only"
    if _LEADING_CONNECTIVE_RE.search(raw):
        return "fragment_leading_connective"
    if _TRAILING_CONNECTOR_RE.search(raw):
        return "fragment_trailing_connective"
    if re.fullmatch(r"(?:point|them)\s+are\s+equal", normalized):
        return "sentence_fragment"

    # Bare schematic labels such as AB, PQ, XY, A0, S2, R3.  Preserve a small
    # explicit set of real technical abbreviations and complete formulae.
    compact = normalized.replace(" ", "")
    if _CHEMICAL_RE.fullmatch(raw) and normalized in _CHEMICAL_KEEP:
        return None
    if re.fullmatch(r"[a-z]{1,2}\d?", compact):
        if compact in _TECHNICAL_SHORT:
            return None
        return "schematic_label"
    if len(compact) == 2 and compact.isalpha() and compact not in _TECHNICAL_SHORT:
        return "schematic_label"
    if compact == "ch2":
        return "incomplete_chemical_fragment"
    if re.fullmatch(r"[a-z]{1,3}\s*\+", compact):
        return "schematic_label"
    if compact in _TECHNICAL_SHORT:
        return None
    if _UPPER_WORD_RE.fullmatch(raw) and compact not in _TECHNICAL_SHORT:
        return "uppercase_fragment"
    if not any(character.isalpha() for character in normalized):
        return "no_alphabetic_content"

    # One-character alphabetic labels are not concepts unless explicitly kept.
    if len(normalized) == 1 and normalized not in _TECHNICAL_SHORT:
        return "single_character_label"
    return None


def is_meaningful_concept(label: str) -> bool:
    """True when a derived concept/topic label passes the shared quality gate."""
    return concept_noise_reason(label) is None


def filter_meaningful_concepts(labels: list[str]) -> list[str]:
    """Return labels in original order, excluding only explained noise."""
    return [label for label in labels if is_meaningful_concept(label)]


__all__ = [
    "concept_noise_reason",
    "filter_meaningful_concepts",
    "is_meaningful_concept",
]
