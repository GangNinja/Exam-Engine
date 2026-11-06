"""Row-level parsing for the personal examination CSV schema.

The schema carries more information than the generic question-bank adapter:
stable source question ids, raw question types, an options column, answer ids,
answer types and worked solutions. This module preserves those values and
classifies only what the row itself states.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_OPTION_MARKER_RE = re.compile(r"(?:(?<=\s)|^)\(?([A-H])[).:\-]\s*")
_OPTION_SINGLE_RE = re.compile(
    r"\boption\s*\(?([a-h])\)?|\bcorrect\s+(?:option|answer)\s*(?:is)?\s*[:\-]?\s*\(?([a-h])\)?",
    re.IGNORECASE,
)
_OPTION_MULTI_RE = re.compile(
    r"^\(?([a-h])\)?(?:\s*[,;/&]\s*(?:and\s+)?\(?([a-h])\)?)+$", re.IGNORECASE
)
_OPTION_PREFIX_RE = re.compile(r"^\s*(?:option\s*)?\(?([A-Ha-h])\)?\s*[).:\-]\s*(.+)$", re.DOTALL)
_OPTION_LETTER_RE = re.compile(r"^\s*\(?([A-Ha-h])\)?\s*$")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_QUESTION_SUFFIX_RE = re.compile(r"_Q\d+[A-Z]?$", re.IGNORECASE)
_SECTION_SUFFIX_RE = re.compile(r"_(?:SEC(?:TION)?[A-Z0-9]+|GA)$", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*(?:to|-|–)\s*[+-]?\d+(?:\.\d+)?)?$")
_REPLACEMENT_CHARS = ("\ufffd", "\u00c2", "\u00e2\u20ac")


QUESTION_TYPES = {
    "mcq": "mcq",
    "multiple choice": "mcq",
    "multiple choice question": "mcq",
    "msq": "msq",
    "multiple select": "msq",
    "multiple correct": "msq",
    "multiple correct answer": "msq",
    "nat": "numerical",
    "nat_or_figure_based": "numerical",
    "numerical": "numerical",
    "numerical answer": "numerical",
    "numeric": "numerical",
    "short answer": "short_answer",
    "short answer with internal choice": "short_answer",
    "short/long answer": "short_or_long_answer",
    "long answer": "long_answer",
    "long answer with internal choice": "long_answer",
    "long/short answer": "short_or_long_answer",
    "descriptive": "descriptive",
    "assertion-reason": "assertion_reason",
    "assertion reason": "assertion_reason",
    "case based": "case_based",
    "case-based": "case_based",
    "value-based": "value_based",
    "reading/comprehension": "reading_comprehension",
}

ANSWER_TYPES = {
    "mcq option": "mcq_option",
    "mcq": "mcq_option",
    "option": "mcq_option",
    "options": "mcq_option",
    "msq options": "multiple_options",
    "multiple options": "multiple_options",
    "numeric": "numerical",
    "nat": "numerical",
    "range": "numerical",
    "numeric/range": "numerical",
    "numeric range": "numerical",
    "numeric_range": "numerical",
    "numeric accepted range/value": "numerical",
    "numeric answer": "numerical",
    "numerical answer": "numerical",
    "answer": "verbatim_answer",
    "answer key": "answer_key",
    "short answer": "short_answer",
    "solution": "worked_solution",
    "worked solution": "worked_solution",
    "diagram": "diagram",
    "diagram / structured representation": "diagram",
}


@dataclass
class ParsedOption:
    key: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "text": self.text}


@dataclass
class ParsedRow:
    source_question_id: str
    original_question_number: str | None
    question_text: str
    question_type_raw: str | None
    question_type: str
    options_raw: str | None
    options: list[ParsedOption] = field(default_factory=list)
    marks: float | None = None
    answer_id: str | None = None
    answer_text: str | None = None
    answer_type_raw: str | None = None
    answer_type: str = "unknown"
    correct_option_key: str | None = None
    solution_steps: str | None = None
    exam_year: int | None = None
    paper_label: str = ""
    source_row: int = 0

    @property
    def has_options(self) -> bool:
        return len(self.options) >= 2


def _parse_markers(text: str) -> list[ParsedOption]:
    matches = list(_OPTION_MARKER_RE.finditer(text))
    if len(matches) < 2:
        return []
    keys = [match.group(1).upper() for match in matches]
    expected = [chr(ord("A") + index) for index in range(len(keys))]
    if keys != expected:
        return []
    options: list[ParsedOption] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = re.sub(r"\s+", " ", text[start:end]).strip(" ;,|")
        if not value:
            return []
        options.append(ParsedOption(key=keys[index], text=value))
    return options


def parse_options(raw: str | None) -> list[ParsedOption]:
    """Parse explicit option markers without inventing missing choices."""
    if not raw or not raw.strip():
        return []
    text = raw.replace("|", " ").replace("\u2022", " ").strip()
    return _parse_markers(text)


def parse_options_from_text(text: str) -> list[ParsedOption]:
    """Recover options that were copied into the question text itself."""
    matches = list(_OPTION_MARKER_RE.finditer(text))
    if len(matches) < 2:
        return []
    return _parse_markers(text[matches[0].start() :])


def normalise_question_type(raw: str | None, *, has_options: bool = False) -> str:
    if not raw or not raw.strip():
        return "mcq" if has_options else "unknown"
    key = raw.strip().lower()
    return QUESTION_TYPES.get(key, "unknown")


def normalise_answer_type(raw: str | None, answer_text: str | None) -> str:
    if raw and raw.strip():
        mapped = ANSWER_TYPES.get(raw.strip().lower())
        if mapped:
            return mapped
    if not answer_text:
        return "unknown"
    lowered = answer_text.lower()
    if _OPTION_SINGLE_RE.search(lowered):
        return "mcq_option"
    if _NUMERIC_RE.match(answer_text.strip()):
        return "numerical"
    return "descriptive"


def _choice_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .;,:–—-").lower()


def extract_correct_option(
    answer_text: str | None,
    answer_type: str,
    options: list[ParsedOption],
) -> str | None:
    """Return a single correct key only when the answer states one unambiguously."""
    if not answer_text:
        return None
    keys = {option.key for option in options}
    single = _OPTION_SINGLE_RE.search(answer_text)
    if single:
        key = (single.group(1) or single.group(2) or "").upper()
        if key and (not keys or key in keys):
            return key
    stripped = answer_text.strip()
    letter = _OPTION_LETTER_RE.match(stripped)
    if letter:
        key = letter.group(1).upper()
        if not keys or key in keys:
            return key
    if options:
        wanted = _choice_text(stripped)
        exact = [option.key for option in options if _choice_text(option.text) == wanted]
        if len(exact) == 1:
            return exact[0]
    prefix = _OPTION_PREFIX_RE.match(stripped)
    if prefix:
        key = prefix.group(1).upper()
        rest = prefix.group(2).strip()
        if key and (not keys or key in keys):
            same = [
                option.key for option in options if _choice_text(option.text) == _choice_text(rest)
            ]
            if same == [key]:
                return key
            if rest.startswith(("(", "[")):
                return None
            if keys and key in keys and len(rest) <= 80:
                return key
    if answer_type == "multiple_options":
        match = _OPTION_MULTI_RE.match(stripped)
        if match:
            letters = re.findall(r"[A-Ha-h]", stripped.upper())
            unique = sorted(set(letters))
            if unique and (not keys or set(unique) <= keys):
                return ",".join(unique)
    return None


def derive_occurrence(source_question_id: str) -> tuple[int | None, str]:
    """Derive the examination year and source paper label from the source id.

    The source CSV fully qualifies every question id (for example
    ``CS2015_Shift1_GA_Q01``). Those ids — not a guessed year — define the
    occurrence group.
    """
    year_match = _YEAR_RE.search(source_question_id)
    year = int(year_match.group(1)) if year_match else None
    label = _QUESTION_SUFFIX_RE.sub("", source_question_id)
    label = _SECTION_SUFFIX_RE.sub("", label)
    return year, label or source_question_id or "unassigned"


def parse_marks(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    return number if 0 <= number <= 100 else None


def looks_corrupted(text: str) -> bool:
    return any(marker in text for marker in _REPLACEMENT_CHARS)


def parse_row(row: dict[str, Any], *, index: int) -> ParsedRow:
    """Convert one CSV record into the exam occurrence model."""
    source_question_id = str(row.get("question_id") or "").strip()
    question_text = str(row.get("question_text") or "")
    options_raw = str(row.get("options") or "").strip() or None
    options = parse_options(options_raw)
    if not options:
        options = parse_options_from_text(question_text)
    answer_text = str(row.get("answer_text") or "").strip() or None
    answer_type_raw = str(row.get("answer_type") or "").strip() or None
    answer_type = normalise_answer_type(answer_type_raw, answer_text)
    year, paper_label = derive_occurrence(source_question_id)
    return ParsedRow(
        source_question_id=source_question_id or f"row-{index}",
        original_question_number=str(row.get("original_question_number") or "").strip() or None,
        question_text=question_text,
        question_type_raw=str(row.get("question_type") or "").strip() or None,
        question_type=normalise_question_type(
            str(row.get("question_type") or ""), has_options=bool(options)
        ),
        options_raw=options_raw,
        options=options,
        marks=parse_marks(row.get("marks")),
        answer_id=str(row.get("answer_id") or "").strip() or None,
        answer_text=answer_text,
        answer_type_raw=answer_type_raw,
        answer_type=answer_type,
        correct_option_key=extract_correct_option(answer_text, answer_type, options),
        solution_steps=str(row.get("solution_steps") or "").strip() or None,
        exam_year=year,
        paper_label=paper_label,
        source_row=index,
    )


__all__ = [
    "ANSWER_TYPES",
    "QUESTION_TYPES",
    "ParsedOption",
    "ParsedRow",
    "derive_occurrence",
    "extract_correct_option",
    "looks_corrupted",
    "normalise_answer_type",
    "normalise_question_type",
    "parse_marks",
    "parse_options",
    "parse_row",
]
