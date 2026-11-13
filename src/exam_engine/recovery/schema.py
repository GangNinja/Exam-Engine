"""Schema discovery for arbitrary educational CSV datasets.

Known column names are convenient, not mandatory. The detector maps aliases to
canonical fields, preserves every unknown column and reports uncertainty rather
than forcing a file into a schema it does not match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_NORMALISE_RE = re.compile(r"[^a-z0-9]+")

#: Canonical field -> accepted header aliases. Keep these descriptive rather
#: than exam-specific so a future board or university file can be understood.
ALIASES: dict[str, tuple[str, ...]] = {
    "question_id": (
        "question_id",
        "question id",
        "questionid",
        "qid",
        "q_id",
        "id",
        "item_id",
        "source_question_id",
        "question_no",
        "question_number",
        "number",
        "qno",
    ),
    "original_question_number": (
        "original_question_number",
        "original number",
        "question_no",
        "question_number",
        "number",
        "qno",
        "item_number",
    ),
    "question_text": (
        "question_text",
        "question",
        "question text",
        "question_statement",
        "statement",
        "stem",
        "text",
        "item",
        "prompt",
    ),
    "question_type": (
        "question_type",
        "question type",
        "qtype",
        "type",
        "format",
        "item_type",
    ),
    "options": (
        "options",
        "option",
        "choices",
        "choice",
        "options_json",
        "option_list",
        "alternatives",
    ),
    "marks": (
        "marks",
        "mark",
        "score",
        "points",
        "maximum_marks",
        "max_marks",
        "weight",
    ),
    "answer_id": ("answer_id", "answer id", "ans_id", "solution_id"),
    "answer_text": (
        "answer_text",
        "answer",
        "answer text",
        "correct_answer",
        "correct answer",
        "answer_key",
        "key",
        "solution",
        "solution_text",
        "response",
    ),
    "answer_type": (
        "answer_type",
        "answer type",
        "answer_format",
        "key_type",
    ),
    "correct_option": (
        "correct_option",
        "correct_option_key",
        "correct option",
        "option_key",
        "key",
    ),
    "solution_steps": (
        "solution_steps",
        "solution steps",
        "worked_solution",
        "explanation",
        "explanations",
        "rationale",
        "reasoning",
    ),
    "exam_year": ("exam_year", "year", "examination_year", "session_year"),
    "paper_label": (
        "paper_label",
        "paper",
        "paper_name",
        "set",
        "shift",
        "session",
        "exam",
    ),
    "section": ("section", "part", "section_name"),
    "unit": ("unit", "chapter", "topic", "unit_name"),
    "subject": ("subject", "subject_name", "course", "course_name"),
    "category": ("category", "category_id", "exam", "examination"),
    "passage": ("passage", "passage_text", "case_study", "case study", "context"),
    "source_url": ("source_url", "url", "source", "reference"),
}

REQUIRED_FIELDS = ("question_text",)
CANONICAL_FIELDS = tuple(ALIASES)


def normalise_header(value: str) -> str:
    return _NORMALISE_RE.sub("_", value.strip().casefold()).strip("_")


_LOOKUP: dict[str, str] = {}
for _field, _aliases in ALIASES.items():
    for _alias in _aliases:
        _LOOKUP.setdefault(normalise_header(_alias), _field)


@dataclass
class SchemaProfile:
    headers: tuple[str, ...]
    mapping: dict[str, str]
    extra_headers: tuple[str, ...]
    missing_fields: tuple[str, ...]
    confidence: float
    standard: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    def adapt(self, row: dict[str, Any]) -> dict[str, Any]:
        """Map one raw row to canonical keys, preserving the raw row separately."""
        mapped: dict[str, Any] = {}
        for header, value in row.items():
            if header is None:
                continue
            target = self.mapping.get(header)
            if target is not None and target not in mapped:
                mapped[target] = value
        mapped["_raw"] = dict(row)
        return mapped

    def as_dict(self) -> dict[str, Any]:
        return {
            "headers": list(self.headers),
            "mapping": dict(self.mapping),
            "extra_headers": list(self.extra_headers),
            "missing_fields": list(self.missing_fields),
            "confidence": self.confidence,
            "standard": self.standard,
            "evidence": dict(self.evidence),
        }


def detect_schema(
    headers: Iterable[str],
    rows: Iterable[dict[str, Any]] | None = None,
) -> SchemaProfile:
    """Detect a canonical schema from headers and, optionally, sample values."""

    header_list = tuple(str(header) for header in headers if header is not None)
    mapping: dict[str, str] = {}
    used_fields: set[str] = set()
    for header in header_list:
        target = _LOOKUP.get(normalise_header(header))
        if target and target not in used_fields:
            mapping[header] = target
            used_fields.add(target)
    missing = tuple(field for field in REQUIRED_FIELDS if field not in used_fields)
    extra = tuple(header for header in header_list if header not in mapping)
    # A standard file has all core fields; a generic file can still be usable
    # when it has a question text column, but its confidence is deliberately low.
    standard = not missing and len(used_fields) >= 7
    confidence = 0.0
    if not missing:
        confidence = min(0.99, 0.45 + 0.06 * len(used_fields))
    elif "question_text" in used_fields:
        confidence = 0.3
    sample_evidence: dict[str, Any] = {}
    if rows:
        sample = [row for row in list(rows)[:3] if isinstance(row, dict)]
        sample_evidence = {
            "sampled_rows": len(sample),
            "nonempty_question_text": sum(
                1
                for row in sample
                if str(row.get(next((h for h, f in mapping.items() if f == "question_text"), ""), "")).strip()
            ),
        }
    return SchemaProfile(
        headers=header_list,
        mapping=mapping,
        extra_headers=extra,
        missing_fields=missing,
        confidence=round(confidence, 3),
        standard=standard,
        evidence=sample_evidence,
    )


__all__ = [
    "ALIASES",
    "CANONICAL_FIELDS",
    "REQUIRED_FIELDS",
    "SchemaProfile",
    "detect_schema",
    "normalise_header",
]
