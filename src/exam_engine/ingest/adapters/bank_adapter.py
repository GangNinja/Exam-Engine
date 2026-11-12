"""CSV and JSON question banks.

Structured banks are the highest-confidence question source, so parsing is
strict: unknown rows are reported and skipped individually instead of failing
the file, and missing numbers/marks are flagged rather than invented.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from exam_engine.ingest.adapters.text_adapter import decode_text
from exam_engine.ingest.base import (
    ExtractedDocument,
    ExtractedQuestion,
    ExtractionContext,
    ingestion_warning,
)
from exam_engine.ingest.normalize import normalize_text, truncate
from exam_engine.ingest.validation import FileValidationError, read_limited_bytes
from exam_engine.models.enums import Severity, SourceFormat
from exam_engine.models.question import Uncertainty

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "text": (
        "text",
        "question",
        "question_text",
        "body",
        "questiontext",
        "qtext",
        "content",
        "prompt",
        "stem",
    ),
    "number": (
        "number",
        "no",
        "qno",
        "q_no",
        "question_number",
        "question_no",
        "questionno",
        "num",
        "q_num",
        "id",
    ),
    "marks": ("marks", "mark", "score", "points", "weight", "max_marks", "marks_"),
    "section": ("section", "part", "section_name", "group"),
    "unit": ("unit", "chapter", "module", "unit_name", "topic_unit"),
    "answer": ("answer", "answer_text", "solution", "key", "correct_answer"),
    "source_question_id": ("question_id", "source_question_id", "qid", "question_uid"),
    "original_number": ("original_question_number", "original_question_no", "original_qno"),
    "question_type": ("question_type", "qtype", "question_kind"),
    "options": ("options", "choices", "answer_options"),
    "answer_id": ("answer_id", "answer_uid"),
    "answer_type": ("answer_type", "answer_format"),
    "solution_steps": ("solution_steps", "worked_solution"),
    "difficulty": ("difficulty", "level", "complexity"),
    "tags": ("tags", "keywords", "labels"),
    "passage_id": ("passage_id", "passage", "passage_uid"),
    "subparts_json": ("subparts_json", "subparts"),
}

_MAX_TEXT_CHARS = 20_000


class QuestionBankAdapter:
    name = "question_bank"
    suffixes: tuple[str, ...] = (".csv", ".json")

    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument:
        suffix = path.suffix.lower()
        raw = read_limited_bytes(path, context.limits.max_file_bytes + 1)
        text, encoding, _confident = decode_text(raw)

        warnings = []
        if suffix == ".csv":
            rows, parse_warnings = _read_csv_rows(text)
            locator_prefix = "row"
            row_offset = 2  # header line is row 1
        else:
            rows, parse_warnings = _read_json_rows(text)
            locator_prefix = "item"
            row_offset = 1

        warnings.extend(parse_warnings)
        if not rows:
            raise FileValidationError(
                "empty_document",
                f"question bank '{path.name}' contains no question rows",
            )

        questions: list[ExtractedQuestion] = []
        seen_numbers: dict[str, int] = {}
        for index, row in enumerate(rows):
            canonical = _canonicalise_row(row)
            if not any(str(v).strip() for v in canonical.values() if v is not None):
                continue
            if not canonical.get("text"):
                warnings.append(
                    ingestion_warning(
                        "missing_manifest_field",
                        f"skipped {locator_prefix} {index + row_offset}: no question text",
                        detail={"locator": f"{locator_prefix} {index + row_offset}"},
                    )
                )
                continue
            question = _build_question(
                canonical,
                index=index,
                row_offset=row_offset,
                locator_prefix=locator_prefix,
                seen_numbers=seen_numbers,
                min_chars=context.limits.min_question_chars,
            )
            questions.append(question)

        if not questions:
            raise FileValidationError(
                "parse_error",
                f"question bank '{path.name}' has no usable questions "
                "(no recognisable question text column)",
            )

        metadata = {
            "extractor": f"{suffix.lstrip('.')}-question-bank",
            "encoding": encoding,
            "row_count": len(rows),
            "columns": sorted({k for row in rows for k in row}),
            "has_answer_data": any(
                str(_canonicalise_row(row).get("answer") or "").strip() for row in rows
            ),
        }
        return ExtractedDocument(
            format=SourceFormat.CSV if suffix == ".csv" else SourceFormat.JSON,
            pages=[],
            questions=questions,
            metadata=metadata,
            warnings=warnings,
            title_hint=path.stem,
        )


def _norm_key(key: str) -> str:
    key = key.strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    return key.strip("_")


def _canonicalise_row(row: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    for key, value in row.items():
        if key is None:
            continue
        norm = _norm_key(str(key))
        for field, aliases in FIELD_ALIASES.items():
            if field in canonical:
                continue
            if norm in aliases:
                canonical[field] = value
                break
    return canonical


def _read_csv_rows(text: str) -> tuple[list[dict[str, Any]], list]:
    if not text.strip():
        return [], [
            ingestion_warning(
                "empty_document", "CSV question bank is empty", severity=Severity.ERROR
            )
        ]
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise FileValidationError("csv_invalid", "CSV has no header row")
        rows = [dict(row) for row in reader]
    except csv.Error as exc:
        raise FileValidationError("csv_invalid", f"malformed CSV: {exc}") from exc
    return rows, []


def _read_json_rows(text: str) -> tuple[list[dict[str, Any]], list]:
    if not text.strip():
        return [], [
            ingestion_warning(
                "empty_document", "JSON question bank is empty", severity=Severity.ERROR
            )
        ]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FileValidationError("json_invalid", f"malformed JSON: {exc}") from exc

    rows: list[dict[str, Any]] = []
    warnings: list = []

    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("sections"), list):
            for section in payload["sections"]:
                if not isinstance(section, dict):
                    continue
                section_name = str(section.get("name") or section.get("section") or "")
                for item in section.get("questions") or section.get("items") or []:
                    if isinstance(item, dict):
                        row = dict(item)
                        if section_name and "section" not in _canonicalise_row(row):
                            row["__section__"] = section_name
                        rows.append(row)
            candidates = []
        else:
            for key in ("questions", "items", "data", "rows"):
                if isinstance(payload.get(key), list):
                    candidates = payload[key]
                    break
            else:
                candidates = [payload] if _canonicalise_row(payload).get("text") else []
    else:
        raise FileValidationError(
            "json_invalid", "JSON must be a list, an object or an object with 'questions'"
        )

    for item in candidates:
        if isinstance(item, dict):
            rows.append(dict(item))
        else:
            warnings.append(
                ingestion_warning(
                    "parse_error",
                    f"skipped non-object JSON item: {truncate(str(item), 80)}",
                    severity=Severity.WARNING,
                )
            )
    return rows, warnings


def _build_question(
    canonical: dict[str, Any],
    *,
    index: int,
    row_offset: int,
    locator_prefix: str,
    seen_numbers: dict[str, int],
    min_chars: int,
) -> ExtractedQuestion:
    uncertainties: list[Uncertainty] = []
    text = normalize_text(str(canonical.get("text") or ""))
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
        uncertainties.append(
            Uncertainty(
                code="question_text_truncated",
                message=f"question text truncated to {_MAX_TEXT_CHARS} characters",
            )
        )

    source_question_id = _clean(canonical.get("source_question_id"))
    raw_number = canonical.get("original_number") or canonical.get("number")
    parsed_number, number_sort = _parse_number(raw_number)
    number = source_question_id or parsed_number
    if number is None:
        uncertainties.append(
            Uncertainty(
                code="missing_question_number",
                message="bank row does not state a question number",
            )
        )
    elif source_question_id is None and number in seen_numbers:
        uncertainties.append(
            Uncertainty(
                code="duplicate_question_number",
                message=f"question number '{number}' already used earlier in this bank",
                detail={"first_index": seen_numbers[number]},
            )
        )
    elif source_question_id is None:
        seen_numbers[number] = index

    marks, marks_invalid = _parse_marks(canonical.get("marks"))
    marks_source = "structured" if marks is not None else None
    if marks_invalid:
        uncertainties.append(
            Uncertainty(
                code="invalid_marks_value",
                message=f"marks value {canonical.get('marks')!r} is not a number in 0..100",
            )
        )
    elif marks is None:
        uncertainties.append(
            Uncertainty(
                code="no_marks_detected",
                message="bank row does not state marks for this question",
                severity=Severity.INFO,
            )
        )

    confidence = 0.95
    if number is None:
        confidence -= 0.15
    if len(text.strip()) < min_chars:
        confidence -= 0.3
        uncertainties.append(
            Uncertainty(
                code="short_question_text",
                message=f"question text is shorter than {min_chars} characters",
            )
        )
    if marks_invalid:
        confidence -= 0.1
    if any(u.code == "duplicate_question_number" for u in uncertainties):
        confidence -= 0.1

    section = _clean(canonical.get("section") or canonical.get("__section__"))
    unit = _clean(canonical.get("unit"))
    metadata: dict[str, Any] = {}
    for field in (
        "answer",
        "answer_id",
        "answer_type",
        "solution_steps",
        "question_type",
        "options",
        "difficulty",
        "passage_id",
        "subparts_json",
    ):
        value = _clean(canonical.get(field))
        if value:
            metadata[field] = value
    if source_question_id:
        metadata["source_question_id"] = source_question_id
    original_number = _clean(canonical.get("original_number"))
    if original_number:
        metadata["original_question_number"] = original_number
    metadata["source_row"] = index + row_offset
    tags = _parse_tags(canonical.get("tags"))
    if tags:
        metadata["tags"] = tags

    return ExtractedQuestion(
        text=text,
        question_number=number,
        number_sort=number_sort,
        section=section,
        unit=unit,
        marks=marks,
        marks_source=marks_source,
        page_number=None,
        source_locator=f"{locator_prefix} {index + row_offset}",
        confidence=max(0.05, min(0.99, round(confidence, 3))),
        uncertainties=uncertainties,
        metadata=metadata,
    )


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = normalize_text(str(value)).strip()
    return text or None


def _parse_number(value: Any) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int | float):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None, None
        if numeric.is_integer():
            number = int(numeric)
            return str(number), number
    text = str(value).strip()
    if not text:
        return None, None
    exact = re.fullmatch(r"(?:q(?:uestion)?\s*[.:\-]?\s*)?(\d{1,3})", text, re.IGNORECASE)
    if exact:
        number = int(exact.group(1))
        return str(number), number
    leading = re.match(r"(\d{1,3})", text)
    return text, int(leading.group(1)) if leading else None


def _parse_marks(value: Any) -> tuple[float | None, bool]:
    """Return (marks, invalid). ``None`` means "not stated", never zero."""
    if value is None:
        return None, False
    if isinstance(value, bool):
        return None, True
    if isinstance(value, int | float):
        number = float(value)
        if not math.isfinite(number):
            return None, True
    else:
        text = str(value).strip()
        if not text:
            return None, False
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None, True
        number = float(match.group())
    if number < 0 or number > 100:
        return None, True
    return number, False


def _parse_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    parts = re.split(r"[;,]", str(value))
    return [part.strip() for part in parts if part.strip()]


__all__ = ["QuestionBankAdapter"]
