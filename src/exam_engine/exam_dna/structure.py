"""Rule-based question-structure cues for Phase 8.

Only explicit wording is used. A missing or ambiguous cue produces ``None`` or an
uncertainty-bearing rule instead of an invented structure. The detectors do not
replace Phase 3 classification; they read the stored question text.
"""

from __future__ import annotations

import re

from exam_engine.models.enums import ChoiceRuleKind
from exam_engine.models.exam_dna import ExamDnaChoiceRule
from exam_engine.understanding.similarity import normalise_question_text

_TRAILING_MARKS_RE = re.compile(
    r"\s*(?:\[|\(|\{)?\s*\d+(?:\.\d+)?\s*marks?\s*(?:\]|\)|\})?\s*$",
    re.IGNORECASE,
)
_LEADING_JUNK_RE = re.compile(
    r"^\s*(?:q(?:uestion)?\s*)?(?:\d+|[a-z])?\s*[).:\-]\s*", re.IGNORECASE
)

COMMAND_VERBS = frozenset(
    {
        "analyse",
        "analyze",
        "apply",
        "argue",
        "assess",
        "calculate",
        "classify",
        "compare",
        "compute",
        "construct",
        "contrast",
        "create",
        "criticise",
        "criticize",
        "deduce",
        "define",
        "demonstrate",
        "describe",
        "design",
        "determine",
        "develop",
        "differentiate",
        "discuss",
        "distinguish",
        "draw",
        "evaluate",
        "examine",
        "explain",
        "find",
        "formulate",
        "give",
        "identify",
        "illustrate",
        "implement",
        "interpret",
        "justify",
        "list",
        "name",
        "outline",
        "prove",
        "show",
        "sketch",
        "solve",
        "state",
        "summarise",
        "summarize",
        "write",
    }
)
_SKIPPED_LEADING_WORDS = frozenset(
    {"a", "an", "and", "briefly", "clearly", "consider", "for", "in", "the", "with", "your"}
)

_ALL_COMPULSORY_RE = re.compile(
    r"\b(?:answer|attempt|do)\s+all\s+(?:the\s+)?(?:questions?|parts?)\b"
    r"|\ball\s+questions\s+are\s+compulsory\b"
    r"|\bcompulsory\s+(?:section|questions?)\b",
    re.IGNORECASE,
)
_CHOOSE_ANY_RE = re.compile(
    r"\b(?:answer|attempt|choose|select|solve)\s+(?:any\s+)?"
    r"(?P<count>\d+|one|two|three|four|five|six)\s+"
    r"(?:of\s+the\s+following\s+)?(?:questions?|parts?|sub-?questions?|items?)\b",
    re.IGNORECASE,
)
_WORD_COUNTS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def normalise_for_exact(text: str) -> str:
    """Wording used for exact repetition; a trailing marks annotation is metadata."""
    return _TRAILING_MARKS_RE.sub("", normalise_question_text(text)).strip()


def extract_command_verb(text: str) -> str | None:
    """Return the first recognised command verb in the question's opening words."""
    stripped = _LEADING_JUNK_RE.sub("", text.strip(), count=1)
    tokens = re.findall(r"[a-z]+", stripped.casefold())
    checked = 0
    for token in tokens:
        if token in _SKIPPED_LEADING_WORDS:
            continue
        if token in COMMAND_VERBS:
            return token
        checked += 1
        if checked >= 4:
            break
    return None


def _sentence_containing(text: str, start: int, end: int) -> str:
    left = max(text.rfind("\n", 0, start), text.rfind(". ", 0, start))
    right_candidates = [
        index for index in (text.find("\n", end), text.find(". ", end)) if index != -1
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return " ".join(text[left + 1 : right].split())


def detect_choice_rules(text: str, question_id: str | None = None) -> list[ExamDnaChoiceRule]:
    """Find explicit optional/compulsory instructions; never infer them."""
    rules: list[ExamDnaChoiceRule] = []
    compulsory = _ALL_COMPULSORY_RE.search(text)
    if compulsory is not None:
        rules.append(
            ExamDnaChoiceRule(
                kind=ChoiceRuleKind.ALL_COMPULSORY,
                evidence=_sentence_containing(text, compulsory.start(), compulsory.end()),
                confidence=0.7,
                question_id=question_id,
            )
        )
    choose = _CHOOSE_ANY_RE.search(text)
    if choose is not None:
        raw = choose.group("count").casefold()
        count = int(raw) if raw.isdigit() else _WORD_COUNTS.get(raw)
        if count is not None and count >= 1:
            rules.append(
                ExamDnaChoiceRule(
                    kind=ChoiceRuleKind.CHOOSE_ANY,
                    required_count=count,
                    evidence=_sentence_containing(text, choose.start(), choose.end()),
                    confidence=0.8,
                    question_id=question_id,
                )
            )
    return rules


def wording_difference(left: str, right: str) -> str:
    """Human-readable wording delta between two question appearances."""
    left_tokens = normalise_for_exact(left).split()
    right_tokens = normalise_for_exact(right).split()
    if left_tokens == right_tokens:
        return "identical normalised wording"
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    left_only = sorted(left_set - right_set)[:6]
    right_only = sorted(right_set - left_set)[:6]
    parts: list[str] = []
    if left_only:
        parts.append("left-only: " + ", ".join(left_only))
    if right_only:
        parts.append("right-only: " + ", ".join(right_only))
    if not parts:
        return "same words in a different order"
    return "; ".join(parts)


def excerpt(text: str, limit: int = 220) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


__all__ = [
    "COMMAND_VERBS",
    "detect_choice_rules",
    "excerpt",
    "extract_command_verb",
    "normalise_for_exact",
    "wording_difference",
]
