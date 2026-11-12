"""Educational structure reconstruction without altering the source row.

The functions here operate on text only. A proposed split or cleaned stem is
stored as a repair; the raw CSV is never rewritten in place.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from exam_engine.datasets.parsing import (
    ParsedOption,
    parse_options_from_text,
    parse_options,
)

_OPTION_MARKER_RE = re.compile(r"(?:(?<=\s)|^)\(?([A-H])[).:\-]\s*")
_SUBPART_RE = re.compile(
    r"(?m)^\s*(?:\(([a-h])\)|([a-h])[.)])\s+(\S.{8,}?)(?=\n\s*(?:\([a-h]\)|[a-h][.)])|\Z)",
    re.IGNORECASE,
)
_NUMBERED_RE = re.compile(
    r"(?m)^\s*(\d{1,3})[.)]\s+(\S.{8,}?)(?=\n\s*\d{1,3}[.)]|\Z)"
)
_PASSAGE_RE = re.compile(
    r"(?is)\b(?:read\s+the\s+following\s+(?:passage|text|extract)|"
    r"read\s+the\s+passage|passage\s+(?:below|given)|"
    r"following\s+passage|comprehension\s+passage|case\s*study|case-based\s+study)\b"
)
_STATEMENT_RE = re.compile(
    r"(?is)\b(?:given\s+below\s+are\s+(?:two|three|four|\d+)\s+statements|"
    r"statements?\s+(?:i|ii|iii|1|2|3)\s*[.:)]|\bconsider\s+the\s+following\s+statements)\b"
)
_QUESTION_START_RE = re.compile(r"(?im)^\s*(?:Q\s*)?\d{1,3}[.)]\s+")
_MOJIBAKE_RE = re.compile(r"(?:\ufffd|Ã.|Â.|â€|â€™|â€œ|â€\x9d)")
_GENERIC_OPTION_RE = re.compile(r"^\s*(?:all|none)\s+of\s+the\s+above\s*$", re.IGNORECASE)

QUESTION_TYPES = {
    "mcq": "single_correct_mcq",
    "multiple_choice": "single_correct_mcq",
    "multiple choice": "single_correct_mcq",
    "msq": "multiple_correct_mcq",
    "multiple correct": "multiple_correct_mcq",
    "numerical": "numerical",
    "nat": "numerical",
    "short_answer": "short_answer",
    "long_answer": "long_answer",
    "descriptive": "descriptive",
    "assertion_reason": "assertion_reason",
    "case_based": "case_study",
    "reading_comprehension": "passage_based",
}


@dataclass
class StructureResult:
    question_text: str
    options: list[dict[str, str]]
    embedded_options: list[dict[str, str]]
    passage: str | None = None
    child_questions: list[str] = field(default_factory=list)
    subparts: list[str] = field(default_factory=list)
    question_type: str = "unknown"
    structural_flags: list[str] = field(default_factory=list)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_text": self.question_text,
            "options": list(self.options),
            "embedded_options": list(self.embedded_options),
            "passage": self.passage,
            "child_questions": list(self.child_questions),
            "subparts": list(self.subparts),
            "question_type": self.question_type,
            "structural_flags": list(self.structural_flags),
            "repairs": list(self.repairs),
            "issues": list(self.issues),
        }


def _option_dicts(options: list[ParsedOption]) -> list[dict[str, str]]:
    return [option.as_dict() for option in options]


def _same_options(left: list[ParsedOption], right: list[ParsedOption]) -> bool:
    if len(left) != len(right):
        return False
    return all(
        a.key == b.key and " ".join(a.text.casefold().split()) == " ".join(b.text.casefold().split())
        for a, b in zip(left, right, strict=False)
    )


def _strip_embedded(
    text: str, embedded: list[ParsedOption]
) -> str:
    if not embedded:
        return text
    first = None
    for option in embedded:
        marker = re.search(
            rf"(?:(?<=\s)|^)\(?{re.escape(option.key)}[).:\-]\s*{re.escape(option.text[:24])}",
            text,
            re.IGNORECASE,
        )
        if marker:
            first = marker if first is None else min(first, marker, key=lambda item: item.start())
    if first is None:
        markers = list(_OPTION_MARKER_RE.finditer(text))
        if markers and markers[0].group(1).upper() == embedded[0].key:
            first = markers[0]
    if first is None:
        return text
    return text[: first.start()].strip(" \n\t:;-")


def _looks_like_question(value: str) -> bool:
    """Reject answer commentary and option labels masquerading as child questions."""
    text = " ".join(value.split()).strip()
    if len(text.split()) < 4:
        return False
    lowered = text.casefold()
    if re.match(r"^(?:option|answer|solution|correct|incorrect|marks?)\b", lowered):
        return False
    if text.endswith("?"):
        return True
    return bool(
        re.match(
            r"^(?:what|why|how|which|who|when|where|state|explain|describe|discuss|"
            r"complete|choose|identify|find|calculate|determine|name|write|read|"
            r"mention|give|list|define|compare|justify|comment|suggest|infer|derive|"
            r"evaluate|predict|show|prove|draw|label)\b",
            lowered,
        )
    )


def _passage_parts(text: str) -> tuple[str | None, list[str], list[str]]:
    # Recognize a shared passage and numbered child questions only with evidence.

    if not _PASSAGE_RE.search(text):
        return None, [], []
    numbered = list(_NUMBERED_RE.finditer(text))
    if len(numbered) < 2:
        # Some PDF conversions flatten line breaks. Only use an inline fallback
        # after a clear passage marker, and keep the same two-question evidence
        # requirement so a numbered list in a normal question is not split.
        passage_match = _PASSAGE_RE.search(text)
        if passage_match is None:
            return None, [], []
        inline = list(
            re.finditer(r"(?<!\d)(\d{1,3})[.)]\s+(\S.{8,}?)(?=(?:\s+\d{1,3}[.)]\s+)|$)", text[passage_match.end() :])
        )
        if len(inline) < 2:
            return None, [], []
        offset = passage_match.end()
        first = inline[0]
        passage = text[: offset + first.start()].strip()
        if len(passage.split()) < 12:
            return None, [], []
        children = [" ".join(match.group(2).split()) for match in inline]
        if sum(1 for child in children if _looks_like_question(child)) < 2:
            return None, [], []
        return passage, children, [text[offset + match.start() : offset + match.end()].strip() for match in inline]
    first = numbered[0]
    passage = text[: first.start()].strip()
    if len(passage.split()) < 12:
        return None, [], []
    children = [" ".join(match.group(2).split()) for match in numbered]
    # A child question must look like a question, not a list of marks or topics.
    if sum(1 for child in children if _looks_like_question(child)) < 2:
        return None, [], []
    return passage, children, [match.group(0).strip() for match in numbered]


def _subparts(text: str) -> list[str]:
    matches = list(_SUBPART_RE.finditer(text))
    if len(matches) < 2:
        return []
    return [" ".join(match.group(3).split()) for match in matches]


def _classify(
    raw_type: str | None,
    question_text: str,
    options: list[dict[str, str]],
    answer_type: str | None,
    passage: str | None,
) -> str:
    lowered = question_text.casefold()
    raw = (raw_type or "").strip().casefold().replace("-", "_")
    if re.search(r"\bassertion\b", lowered) and re.search(r"\breason\b", lowered):
        return "assertion_reason"
    if raw in {"case_based", "case study", "case_study"}:
        return "case_study"
    if passage:
        return "passage_based"
    if raw in {"reading_comprehension", "reading comprehension", "comprehension"}:
        return "passage_based"
    if _STATEMENT_RE.search(question_text):
        return "statement_based"
    if re.search(r"\b(true|false)\b", lowered) and len(options) == 2:
        return "true_false"
    if re.search(r"\bfill\s+in\s+the\s+blank", lowered):
        return "fill_blank"
    if re.search(r"\bmatch\s+the\s+(?:following|column|list)", lowered):
        return "matching"
    if raw in {"msq", "multiple_select", "multiple_correct"}:
        return "multiple_correct_mcq"
    if raw in {"mcq", "multiple_choice", "multiple choice"}:
        return "single_correct_mcq"
    if raw in {"nat", "numerical", "numeric", "numerical_answer"} or answer_type == "numerical":
        return "numerical"
    if options:
        return "single_correct_mcq"
    if raw in QUESTION_TYPES:
        return QUESTION_TYPES[raw]
    if re.search(r"\b(diagram|graph|figure|table)\b", lowered):
        return "visual_reference"
    if re.search(r"\b(short|brief)\b", lowered):
        return "short_answer"
    if re.search(r"\b(long|essay|discuss|describe)\b", lowered):
        return "long_answer"
    return "unknown"


def _math_issues(text: str, answer_text: str | None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    combined = f"{text}\n{answer_text or ''}"
    if _MOJIBAKE_RE.search(combined):
        issues.append(
            {
                "code": "math.mojibake_or_encoding_corruption",
                "category": "mathematical_integrity",
                "severity": "warning",
                "message": "Text contains mojibake or replacement characters that can corrupt formulas.",
                "evidence": {"marker": _MOJIBAKE_RE.search(combined).group(0)},
                "confidence": 0.9,
                "remediation": "Compare with the source document; do not guess the original symbol.",
                "blocking": False,
                "field": "question_text",
            }
        )
    opens = sum(combined.count(char) for char in "([{")
    closes = sum(combined.count(char) for char in ")]}")
    if abs(opens - closes) >= 2 and re.search(r"[=+\-*/^]|\\[a-zA-Z]+", combined):
        issues.append(
            {
                "code": "math.unbalanced_delimiters",
                "category": "mathematical_integrity",
                "severity": "warning",
                "message": "Mathematical delimiters are unbalanced; this may indicate extraction loss.",
                "evidence": {"opens": opens, "closes": closes},
                "confidence": 0.65,
                "remediation": "Review the source expression before using it.",
                "blocking": False,
                "field": "question_text",
            }
        )
    if re.search(r"\b(?:infty|sqrt|frac|int|sum)\b", combined) and "\\" not in combined:
        issues.append(
            {
                "code": "math.unstructured_notation",
                "category": "mathematical_integrity",
                "severity": "info",
                "message": "Mathematical notation appears in a lossy text form.",
                "evidence": {},
                "confidence": 0.55,
                "remediation": "Preserve the original and retain a structured LaTeX/source reference.",
                "blocking": False,
                "field": "question_text",
            }
        )
    return issues


def analyse_structure(
    *,
    question_text: str,
    options_raw: str | None,
    question_type_raw: str | None,
    answer_type: str | None,
    passage_text: str | None = None,
) -> StructureResult:
    text = question_text.strip()
    explicit_passage = " ".join(str(passage_text or "").split()) or None
    column_options = parse_options(options_raw)
    embedded = parse_options_from_text(text)
    repairs: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    canonical = column_options or embedded
    cleaned = text
    if embedded and column_options and _same_options(embedded, column_options):
        cleaned = _strip_embedded(text, embedded) or text
        if cleaned != text:
            repairs.append(
                {
                    "repair_type": "remove_duplicated_embedded_options",
                    "field": "question_text",
                    "before": text,
                    "after": cleaned,
                    "method": "exact_option_match",
                    "confidence": 0.99,
                    "risk": "low",
                    "reason": "Options are duplicated verbatim inside the question text and in the options column.",
                }
            )
            issues.append(
                {
                    "code": "options.duplicated_in_question_and_column",
                    "category": "option_integrity",
                    "severity": "warning",
                    "message": "MCQ options appear both inside the question text and in the options column.",
                    "evidence": {"embedded": _option_dicts(embedded), "column": _option_dicts(column_options)},
                    "confidence": 0.99,
                    "remediation": "Use one canonical option list; retain the source row for audit.",
                    "blocking": False,
                    "field": "options",
                }
            )
    elif embedded and not column_options:
        cleaned = _strip_embedded(text, embedded) or text
        if cleaned != text:
            repairs.append(
                {
                    "repair_type": "extract_embedded_options",
                    "field": "options",
                    "before": options_raw,
                    "after": _option_dicts(embedded),
                    "method": "option_marker_parse",
                    "confidence": 0.9,
                    "risk": "medium",
                    "reason": "Options were found in the question text and moved to a canonical option list.",
                }
            )
    elif embedded and column_options:
        issues.append(
            {
                "code": "options.embedded_mismatch",
                "category": "option_integrity",
                "severity": "warning",
                "message": "Embedded options differ from the separate options column.",
                "evidence": {"embedded": _option_dicts(embedded), "column": _option_dicts(column_options)},
                "confidence": 0.8,
                "remediation": "Compare with the source; do not choose one list automatically.",
                "blocking": False,
                "field": "options",
            }
        )
    canonical_dicts = _option_dicts(canonical)
    if len(canonical_dicts) < 2 and (question_type_raw or "").casefold() in {"mcq", "msq", "multiple choice"}:
        issues.append(
            {
                "code": "options.missing_for_mcq",
                "category": "option_integrity",
                "severity": "warning",
                "message": "The row is labelled as a choice question but fewer than two options parse.",
                "evidence": {"parsed_options": canonical_dicts},
                "confidence": 0.95,
                "remediation": "Recover options from the source; do not invent them.",
                "blocking": False,
                "field": "options",
            }
        )
    seen_text: dict[str, int] = {}
    seen_key: set[str] = set()
    duplicate_indices: list[int] = []
    for index, option in enumerate(canonical_dicts):
        key = option["key"]
        normalized = " ".join(option["text"].casefold().split())
        if key in seen_key:
            duplicate_indices.append(index)
        if normalized in seen_text:
            duplicate_indices.append(index)
        seen_key.add(key)
        seen_text[normalized] = index
    if duplicate_indices:
        issues.append(
            {
                "code": "options.duplicate_options",
                "category": "option_integrity",
                "severity": "warning",
                "message": "Duplicate option keys or option texts were found.",
                "evidence": {"indices": sorted(set(duplicate_indices)), "options": canonical_dicts},
                "confidence": 0.99,
                "remediation": "Review whether the duplication is intentional; do not silently delete content.",
                "blocking": False,
                "field": "options",
            }
        )
    if any(_GENERIC_OPTION_RE.match(option["text"]) for option in canonical_dicts):
        issues.append(
            {
                "code": "options.generic_choice",
                "category": "option_integrity",
                "severity": "info",
                "message": "A generic 'all/none of the above' option is present.",
                "evidence": {},
                "confidence": 0.8,
                "remediation": "Keep it if it is part of the original question.",
                "blocking": False,
                "field": "options",
            }
        )
    passage, children, child_markers = _passage_parts(cleaned)
    passage_context = False
    if explicit_passage is not None:
        passage = explicit_passage
        passage_context = True
    elif not passage and _PASSAGE_RE.search(cleaned) and len(cleaned.split()) >= 20:
        # A single reading-comprehension question still has a passage context,
        # but there is not enough evidence for separate child questions.
        passage = cleaned
        passage_context = True
    if children and not explicit_passage:
        # Children are attached to the detected shared passage.
        passage = passage
    subparts = _subparts(cleaned)
    if children:
        repairs.append(
            {
                "repair_type": "split_passage_and_children",
                "field": "structure",
                "before": text,
                "after": {"passage": passage, "child_questions": children},
                "method": "explicit_passage_and_numbered_markers",
                "confidence": 0.86,
                "risk": "medium",
                "reason": "A shared passage and at least two numbered child questions were detected.",
            }
        )
    if subparts:
        repairs.append(
            {
                "repair_type": "record_subparts",
                "field": "structure",
                "before": text,
                "after": {"subparts": subparts},
                "method": "explicit_lettered_markers",
                "confidence": 0.84,
                "risk": "medium",
                "reason": "Multiple lettered subparts were detected; the parent row remains intact.",
            }
        )
    question_type = _classify(question_type_raw, cleaned, canonical_dicts, answer_type, passage)
    flags = []
    if passage:
        flags.append("shared_passage" if children or explicit_passage else "passage_context")
    if explicit_passage:
        flags.append("explicit_passage_column")
    if children:
        flags.append("child_questions_present")
    if subparts:
        flags.append("subparts_present")
    math_issues = _math_issues(cleaned, None)
    issues.extend(math_issues)
    return StructureResult(
        question_text=cleaned,
        options=canonical_dicts,
        embedded_options=_option_dicts(embedded),
        passage=passage,
        child_questions=children,
        subparts=subparts,
        question_type=question_type,
        structural_flags=flags,
        repairs=repairs,
        issues=issues,
    )


def extract_solution_answer(solution: str | None) -> str | None:
    """Extract only an explicitly stated final answer from a worked solution."""
    if not solution:
        return None
    text = " ".join(solution.split())
    patterns = (
        r"(?i)\b(?:final\s+)?answer\s*(?:is|:|-|=)\s*([A-H])\b",
        r"(?i)\b(?:correct\s+)?(?:option|choice)\s*(?:is|:|-)?\s*\(?([A-H])\)?\b",
        r"(?i)\b(?:final\s+)?answer\s*(?:is|:|-|=)\s*([^.;]{1,80})",
        r"(?i)\\boxed\{\s*([^}]{1,80})\s*\}",
        r"(?i)(?:^|\s)=\s*([+-]?\d+(?:\.\d+)?)\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = " ".join(match.group(1).strip(" .,;:").split())
            if value:
                return value
    return None


def normalise_math_text(value: str) -> str:
    """Normalize whitespace and full-width punctuation for comparison only."""

    return " ".join(unicodedata.normalize("NFKC", value).split())


__all__ = [
    "QUESTION_TYPES",
    "StructureResult",
    "analyse_structure",
    "extract_solution_answer",
    "normalise_math_text",
]
