"""Row-level parsing for the personal examination CSV schema.

The schema carries more information than the generic question-bank adapter:
stable source question ids, raw question types, an options column, answer ids,
answer types and worked solutions. This module preserves those values and
classifies only what the row itself states.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_OPTION_CANDIDATE_RE = re.compile(
    r"(?:(?<=\s)|(?<=\|)|^)(?:\(([A-Ha-h]|[1-9])\)|([A-Ha-h]|[1-9])[).:\-](?![0-9]))"
)
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
_PASSAGE_RE = re.compile(
    r"(?is)\b(?:read\s+the\s+following\s+(?:passage|text|extract)|"
    r"read\s+the\s+passage|passage\s+(?:below|given)|"
    r"following\s+passage|comprehension\s+passage|case\s*study|case-based\s+study)\b"
)
_SUBPART_START_RE = re.compile(r"(?m)^\s*(?:\(([a-h])\)|([a-h])[.)])\s+")
_ROMAN_SUBPART_START_RE = re.compile(
    r"(?m)^\s*\((i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)\)\s+", re.IGNORECASE
)
_NUMBERED_SUBPART_START_RE = re.compile(r"(?m)^\s*(\d+)\.(\d+)[.)]?\s+")
_SIMPLE_NUMBERED_SUBPART_START_RE = re.compile(r"(?m)^\s*\((\d+)\)\s+")


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
    passage_id: str | None = None
    subparts_json: str | None = None

    @property
    def has_options(self) -> bool:
        return len(self.options) >= 2


def _marker_groups(text: str) -> list[tuple[str, list[ParsedOption]]]:
    """Return consecutive option-marker runs, separating letter/number groups.

    A source row can contain a statement list (``(a)`` … ``(d)``) followed by
    the actual answer choices (``(1)`` … ``(4)``). They must not be merged into
    one eight-option MCQ. Runs are kept in source order and callers select the
    group that matches the stored answer, or the final group when the source
    does not carry a usable answer marker.
    """

    def at_boundary(start: int) -> bool:
        index = start - 1
        while index >= 0 and text[index] in " \t\r":
            index -= 1
        return index < 0 or text[index] in "\n|"

    candidates: list[tuple[str, str, int, int, bool]] = []
    for match in _OPTION_CANDIDATE_RE.finditer(text):
        raw_key = match.group(1) or match.group(2)
        key = raw_key.upper()
        kind = "letter" if key.isalpha() else "number"
        candidates.append((kind, key, match.start(), match.end(), at_boundary(match.start())))

    # A marker is accepted only when it continues the current run. This keeps
    # parenthesised references inside an option's text (for example “3) (d),
    # (a), (c), (b)”) out of the choice list, while still finding mixed formats
    # such as “1) first (2) second | 3) third (4) fourth”.
    def starts_run(index: int) -> bool:
        kind, first_key, _, _, _ = candidates[index]
        expected_index = 0
        count = 0
        for other_kind, key, _, _, _ in candidates[index:]:
            if other_kind != kind:
                continue
            expected = (
                chr(ord("A") + expected_index) if kind == "letter" else str(expected_index + 1)
            )
            if key == expected:
                count += 1
                expected_index += 1
            elif key == first_key and count >= 2:
                return True
            if count >= 2:
                return True
        return False

    groups: list[tuple[str, list[tuple[str, int, int]]]] = []
    current_kind: str | None = None
    current: list[tuple[str, int, int]] = []
    expected_next = 0
    for index, (kind, key, start, end, boundary) in enumerate(candidates):
        first_key = "A" if kind == "letter" else "1"
        expected = (
            chr(ord("A") + expected_next) if kind == "letter" else str(expected_next + 1)
        )
        if current and kind == current_kind and key == expected:
            current.append((key, start, end))
            expected_next += 1
            continue
        if key == first_key and (not current or boundary) and starts_run(index):
            if len(current) >= 2:
                groups.append((current_kind or kind, current))
            current_kind = kind
            current = [(key, start, end)]
            expected_next = 1
            continue
        # A non-continuing marker is treated as prose, not as a new option.

    if len(current) >= 2:
        groups.append((current_kind or "letter", current))

    accepted = [marker for _, markers in groups for marker in markers]
    accepted.sort(key=lambda marker: marker[1])
    positions = {id(marker): marker[1] for marker in accepted}
    result: list[tuple[str, list[ParsedOption]]] = []
    for kind, markers in groups:
        options: list[ParsedOption] = []
        for key, start, end in markers:
            later = [positions[id(other)] for other in accepted if positions[id(other)] > start]
            next_start = min(later) if later else len(text)
            value = re.sub(r"\s+", " ", text[end:next_start]).strip(" ;,|")
            if value:
                options.append(ParsedOption(key=key, text=value))
        if len(options) >= 2:
            result.append((kind, options))
    return result


def _answer_key(answer_text: str | None) -> str | None:
    """Extract a marker key from an answer without inventing one."""

    if not answer_text:
        return None
    stripped = answer_text.strip()
    single = re.fullmatch(r"\(?([A-Ha-h]|[1-9])\)?[).:]?", stripped)
    if single:
        return single.group(1).upper()
    prefixed = re.fullmatch(
        r"(?:option|answer)\s*(?:is\s*)?[:#-]?\s*\(?([A-Ha-h]|[1-9])\)?",
        stripped,
        re.IGNORECASE,
    )
    if prefixed:
        return prefixed.group(1).upper()
    return None


def _select_group(
    groups: list[tuple[str, list[ParsedOption]]],
    *,
    answer_text: str | None = None,
) -> list[ParsedOption]:
    if not groups:
        return []
    wanted = _answer_key(answer_text)
    if wanted:
        for _, options in groups:
            if any(option.key == wanted for option in options):
                return options
    # Options normally trail the statement list. Choosing the last complete
    # run avoids displaying both the statement labels and the answer choices.
    return groups[-1][1]


def parse_options(raw: str | None, *, answer_text: str | None = None) -> list[ParsedOption]:
    """Parse explicit option markers without inventing missing choices."""
    if not raw or not raw.strip():
        return []
    text = _normalise_option_markers(raw.replace("\u2022", " ").strip())
    return _select_group(_marker_groups(text), answer_text=answer_text)


def parse_options_from_text(
    text: str, *, answer_text: str | None = None
) -> list[ParsedOption]:
    """Recover options that were copied into the question text itself."""
    return _select_group(
        _marker_groups(_normalise_option_markers(text)), answer_text=answer_text
    )


def _normalise_option_markers(text: str) -> str:
    """Repair common OCR markers such as ``(Cc)`` and ``(©)`` for ``(C)``."""

    text = re.sub(
        r"\(\s*([A-Ha-h])\s*\1\s*\)",
        lambda match: f" | ({match.group(1).upper()}) ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\(\s*©\s*\)", " | (C) ", text)
    # An unparenthesised © is used as a C marker in some GATE rows, but the
    # symbol also appears in the copyright footer.  Only repair it next to an
    # option separator or equals sign.
    text = re.sub(r"(?<=[=|])\s*©\s*", " | (C) ", text)
    return text


def _option_marker_pattern(key: str) -> re.Pattern[str]:
    escaped = re.escape(key)
    return re.compile(
        rf"(?<![\w])(?:\(\s*{escaped}\s*\)|{escaped}\s*[).:])\s*",
        re.IGNORECASE,
    )


def _plain_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def strip_embedded_options(text: str, options: list[ParsedOption]) -> str:
    """Remove an option block repeated inside the question stem.

    The source often stores choices both in ``options`` and at the end of
    ``question_text``.  Only strip a complete, ordered match of the structured
    choices so numbered references in a stem are not mistaken for options.
    """

    if not text or len(options) < 2:
        return text
    patterns = [_option_marker_pattern(option.key) for option in options]
    expected = [_plain_text(option.text)[:24] for option in options]
    if not all(expected):
        return text

    def matches(match: re.Match[str], index: int) -> bool:
        following = _plain_text(text[match.end() : match.end() + 80])
        return following.startswith(expected[index])

    best_start: int | None = None
    for start_match in patterns[0].finditer(text):
        if start_match.start() < len(text) * 0.05:
            continue
        if not matches(start_match, 0):
            continue
        position = start_match.end()
        matched = 1
        for index in range(1, len(options)):
            next_match = patterns[index].search(text, position)
            if next_match is None or not matches(next_match, index):
                break
            position = next_match.end()
            matched += 1
        if matched == len(options):
            best_start = start_match.start()
            break
    if best_start is None:
        return text
    stripped = text[:best_start].rstrip()
    # Do not reduce a stem to a bare instruction such as "State:".  Those
    # rows have lettered subparts mislabeled as options, not an option block.
    return stripped if len(stripped) >= 20 else text


def is_passage_question(text: str | None) -> bool:
    """Return whether the source text explicitly introduces a shared passage."""

    return bool(_PASSAGE_RE.search(str(text or "")))


def _extract_marked_parts(value: str, pattern: re.Pattern[str]) -> list[str]:
    matches = list(pattern.finditer(value))
    if len(matches) < 2:
        return []
    parts: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        part = " ".join(value[match.end() : end].split()).strip(" \n\t:;-")
        if part:
            parts.append(part)
    return parts


def _unique_marker_count(value: str, pattern: re.Pattern[str]) -> int:
    keys: set[str] = set()
    for match in pattern.finditer(value):
        key = "".join(group.lower() for group in match.groups() if group)
        if key:
            keys.add(key)
    return len(keys)


def plausible_passage_options(options: list[ParsedOption]) -> bool:
    """Reject concatenated passage text whose numbering is not an MCQ list."""

    if not 2 <= len(options) <= 5:
        return False
    keys = {option.key.upper() for option in options}
    letters = keys <= {"A", "B", "C", "D", "E"}
    numbers = keys <= {"1", "2", "3", "4"}
    return letters or numbers


def extract_passage_subparts(text: str | None) -> list[str]:
    """Extract embedded passage questions without treating them as options.

    The source CSV has no subparts column for many reading passages; the
    questions are embedded after the passage as ``(a)`` … ``(h)``, roman
    numerals, or numbered case-study prompts.  These are question prompts,
    not answer choices, when the row has no options column and the stored
    answer is descriptive.
    """

    value = str(text or "")
    if not is_passage_question(value):
        return []
    lettered_count = _unique_marker_count(value, _SUBPART_START_RE)
    roman_count = _unique_marker_count(value, _ROMAN_SUBPART_START_RE)
    # Prefer the marker family that forms the longer sequence.  A passage can
    # contain a few lettered headlines inside a roman-numeral question set, or
    # roman-numeral choices inside a lettered question set.
    if lettered_count >= 2 and lettered_count >= roman_count:
        return _extract_marked_parts(value, _SUBPART_START_RE)
    # A lone lettered prompt followed by (i)-(iv) is an MCQ stem with its
    # choices, not a multi-part passage.  Its section labels are paper
    # headings, so do not fall through to the numbered/roman patterns.
    if lettered_count == 1:
        return []
    if roman_count >= 2:
        return _extract_marked_parts(value, _ROMAN_SUBPART_START_RE)
    numbered_sections = _extract_marked_parts(value, _NUMBERED_SUBPART_START_RE)
    if len(numbered_sections) >= 2:
        return numbered_sections
    simple_numbered = _extract_marked_parts(value, _SIMPLE_NUMBERED_SUBPART_START_RE)
    if len(simple_numbered) >= 2:
        return simple_numbered
    return []


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
    stripped = answer_text.strip()
    numeric_single = re.fullmatch(r"\(?([1-9])\)?[).:]?", stripped)
    if numeric_single:
        key = numeric_single.group(1)
        if not keys or key in keys:
            return key
    numeric_prefixed = re.fullmatch(
        r"(?:option|answer)\s*(?:is\s*)?[:#-]?\s*\(?([1-9])\)?",
        stripped,
        re.IGNORECASE,
    )
    if numeric_prefixed:
        key = numeric_prefixed.group(1)
        if not keys or key in keys:
            return key
    # Multiple-select answers such as "A;C" or "A, C" must be preserved as a
    # set; the generic single-letter regex would otherwise discard all but A.
    if answer_type == "multiple_options" or re.search(r"[;,]", answer_text):
        letters = sorted(set(re.findall(r"[A-Ha-h]", answer_text.upper())))
        if len(letters) >= 2 and (not keys or set(letters) <= keys):
            return ",".join(letters)
        numbers = sorted(set(re.findall(r"(?<!\d)([1-9])(?!\d)", answer_text)))
        if len(numbers) >= 2 and (not keys or set(numbers) <= keys):
            return ",".join(numbers)
    single = _OPTION_SINGLE_RE.search(answer_text)
    if single:
        key = (single.group(1) or single.group(2) or "").upper()
        if key and (not keys or key in keys):
            return key
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
    answer_text = str(row.get("answer_text") or "").strip() or None
    answer_type_raw = str(row.get("answer_type") or "").strip() or None
    answer_type = normalise_answer_type(answer_type_raw, answer_text)
    passage_question = is_passage_question(question_text)
    passage_subparts = (
        extract_passage_subparts(question_text)
        if passage_question and answer_type != "mcq_option"
        else []
    )
    if not options:
        options = parse_options_from_text(question_text, answer_text=answer_text)
    if passage_question and not options_raw:
        # More than five choices is a concatenated set of subquestions.  A
        # descriptive passage with detected subparts has supplied its questions
        # as the option list; keep genuine A-D options on a single MCQ passage
        # with no detected subparts instead.
        if (answer_type != "mcq_option" and passage_subparts) or len(options) > 5:
            options = []
        elif not plausible_passage_options(options):
            options = []
    year, paper_label = derive_occurrence(source_question_id)
    return ParsedRow(
        source_question_id=source_question_id or f"row-{index}",
        original_question_number=str(row.get("original_question_number") or "").strip() or None,
        question_text=question_text,
        question_type_raw=str(row.get("question_type") or "").strip() or None,
        question_type=(
            "passage_based"
            if passage_question
            else normalise_question_type(
                str(row.get("question_type") or ""), has_options=bool(options)
            )
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
        passage_id=str(row.get("passage_id") or "").strip() or None,
        subparts_json=(
            str(row.get("subparts_json") or "").strip()
            or (
                json.dumps(passage_subparts, ensure_ascii=False)
                if passage_subparts
                else None
            )
        ),
    )


__all__ = [
    "ANSWER_TYPES",
    "QUESTION_TYPES",
    "ParsedOption",
    "ParsedRow",
    "derive_occurrence",
    "extract_correct_option",
    "extract_passage_subparts",
    "is_passage_question",
    "looks_corrupted",
    "normalise_answer_type",
    "normalise_question_type",
    "parse_marks",
    "parse_options",
    "plausible_passage_options",
    "parse_row",
    "strip_embedded_options",
]
