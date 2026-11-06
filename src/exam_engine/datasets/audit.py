"""Dataset quality and compatibility audit for the personal examination corpus."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.datasets.catalog import DatasetCatalog, ExamSubject, discover
from exam_engine.datasets.parsing import ParsedRow, looks_corrupted, parse_row
from exam_engine.datasets.syllabus import segment_exam_syllabus
from exam_engine.ingest.adapters.text_adapter import decode_text
from exam_engine.ingest.base import ExtractedPage
from exam_engine.models.enums import ExtractionMethod

EXPECTED_COLUMNS = (
    "question_id",
    "original_question_number",
    "question_text",
    "question_type",
    "options",
    "marks",
    "answer_id",
    "answer_text",
    "answer_type",
    "solution_steps",
)


@dataclass
class AuditIssue:
    severity: str
    code: str
    category_id: str
    subject: str
    relative_path: str
    row_number: int | None
    question_id: str | None
    message: str
    action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "category_id": self.category_id,
            "subject": self.subject,
            "relative_path": self.relative_path,
            "row_number": self.row_number or "",
            "question_id": self.question_id or "",
            "message": self.message,
            "action": self.action,
        }


@dataclass
class AuditResult:
    root: Path
    catalog: DatasetCatalog
    inventory: list[dict[str, Any]] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def write(self, output_dir: Path | str = Path("reports/phase13")) -> dict[str, Path]:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        inventory_path = target / "dataset_inventory.csv"
        report_path = target / "dataset_validation_report.csv"
        summary_path = target / "dataset_summary.json"
        _write_csv(inventory_path, self.inventory)
        _write_csv(report_path, [issue.as_dict() for issue in self.issues])
        summary_path.write_text(
            json.dumps(self.summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return {
            "inventory": inventory_path,
            "validation_report": report_path,
            "summary": summary_path,
        }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)


def _add(
    issues: list[AuditIssue],
    severity: str,
    code: str,
    subject: ExamSubject,
    path: Path,
    *,
    row: int | None = None,
    question_id: str | None = None,
    message: str,
    action: str,
) -> None:
    issues.append(
        AuditIssue(
            severity=severity,
            code=code,
            category_id=subject.category_id,
            subject=subject.subject_name,
            relative_path=path.as_posix(),
            row_number=row,
            question_id=question_id,
            message=message,
            action=action,
        )
    )


def _audit_csv(subject: ExamSubject, root: Path) -> tuple[dict[str, Any], list[AuditIssue]]:
    path = subject.csv_path
    issues: list[AuditIssue] = []
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text, encoding, _confident = decode_text(raw)
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    header = list(reader.fieldnames or [])
    missing_columns = [column for column in EXPECTED_COLUMNS if column not in header]
    extra_columns = [column for column in header if column not in EXPECTED_COLUMNS]
    if missing_columns:
        _add(
            issues,
            "error",
            "missing_columns",
            subject,
            path,
            message=f"missing expected column(s): {', '.join(missing_columns)}",
            action="review schema; missing values are not invented",
        )
    if extra_columns:
        _add(
            issues,
            "info",
            "extra_columns",
            subject,
            path,
            message=f"additional column(s) preserved: {', '.join(extra_columns)}",
            action="none",
        )

    seen_qids: set[str] = set()
    seen_rows: set[tuple[str, ...]] = set()
    years: set[int] = set()
    question_types: dict[str, int] = {}
    answer_types: dict[str, int] = {}
    questions_with_text = answers_present = options_present = solutions_present = 0
    duplicate_qids = 0
    corrupted = 0

    for index, row in enumerate(rows):
        row_number = index + 2
        if None in row:
            _add(
                issues,
                "error",
                "malformed_row",
                subject,
                path,
                row=row_number,
                message="row has more cells than the header",
                action="row parsed from available cells; extra cells ignored",
            )
        parsed = parse_row(row, index=row_number)
        if parsed.source_question_id in seen_qids:
            duplicate_qids += 1
            _add(
                issues,
                "error",
                "duplicate_question_id",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="duplicate question_id within one source file",
                action="row skipped; two occurrences must keep distinct source ids",
            )
        else:
            seen_qids.add(parsed.source_question_id)

        row_key = tuple(str(value or "") for value in row.values())
        if row_key in seen_rows:
            _add(
                issues,
                "warning",
                "duplicate_row",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="exact duplicate of an earlier row",
                action="kept as a separate source row; not merged",
            )
        seen_rows.add(row_key)

        if not parsed.question_text.strip():
            _add(
                issues,
                "error",
                "missing_question_text",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message=(
                    "question_text is empty"
                    + (" but answer text is present" if parsed.answer_text else "")
                ),
                action="row skipped; no question was invented",
            )
            continue

        questions_with_text += 1
        if parsed.exam_year is not None:
            years.add(parsed.exam_year)
        else:
            _add(
                issues,
                "info",
                "missing_exam_year",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="no examination year is encoded in question_id",
                action="year left unknown; not guessed",
            )

        if parsed.answer_text:
            answers_present += 1
        else:
            _add(
                issues,
                "warning",
                "missing_answer_text",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="answer_text is empty",
                action="question imported without an answer; no answer was invented",
            )
            if parsed.solution_steps:
                _add(
                    issues,
                    "warning",
                    "solution_without_answer",
                    subject,
                    path,
                    row=row_number,
                    question_id=parsed.source_question_id,
                    message="solution_steps are present but answer_text is empty",
                    action="solution preserved; answer left unmatched",
                )

        if parsed.options:
            options_present += 1
        elif parsed.question_type in {"mcq", "msq", "assertion_reason"}:
            _add(
                issues,
                "warning",
                "missing_options",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message=f"question_type is {parsed.question_type!r} but no explicit options parse",
                action="question imported; option completeness flagged",
            )

        if parsed.solution_steps:
            solutions_present += 1
        if parsed.marks is None:
            _add(
                issues,
                "info",
                "missing_marks",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="marks are empty",
                action="marks left unknown (None), never defaulted to zero",
            )
        if parsed.question_type == "unknown":
            _add(
                issues,
                "info",
                "missing_question_type",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="question_type is empty or unrecognised",
                action="stored as 'unknown'",
            )
        if parsed.answer_text and parsed.answer_type == "unknown":
            _add(
                issues,
                "warning",
                "unclassified_answer_type",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message=f"answer_type {parsed.answer_type_raw!r} is not recognised",
                action="answer preserved; classified as unknown",
            )
        if (
            parsed.answer_type == "mcq_option"
            and parsed.options
            and parsed.correct_option_key is None
        ):
            _add(
                issues,
                "warning",
                "correct_option_not_identified",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="MCQ answer text does not state one unambiguous option key",
                action="answer text preserved; correct option left unknown",
            )
        if looks_corrupted(parsed.question_text) or looks_corrupted(parsed.answer_text or ""):
            corrupted += 1
            _add(
                issues,
                "warning",
                "replacement_characters",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="text contains replacement characters from the source encoding",
                action="text preserved as-is; not silently repaired",
            )
        if len(parsed.question_text.strip()) < 15:
            _add(
                issues,
                "warning",
                "short_question_text",
                subject,
                path,
                row=row_number,
                question_id=parsed.source_question_id,
                message="question text is shorter than 15 characters",
                action="row imported; extraction flagged for review",
            )
        question_types[parsed.question_type] = question_types.get(parsed.question_type, 0) + 1
        answer_types[parsed.answer_type] = answer_types.get(parsed.answer_type, 0) + 1

    inventory = {
        "category_id": subject.category_id,
        "category_name": subject.category.name,
        "subject": subject.subject_name,
        "course_id": subject.course_id,
        "course_code": subject.course_code,
        "file_type": "csv",
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": digest,
        "bytes": len(raw),
        "encoding": encoding,
        "records": len(rows),
        "columns": len(header),
        "examination_years": ",".join(str(year) for year in sorted(years)),
        "questions_with_text": questions_with_text,
        "answers_present": answers_present,
        "options_present": options_present,
        "solutions_present": solutions_present,
        "duplicate_question_ids": duplicate_qids,
        "corrupted_text_rows": corrupted,
        "syllabus_documents": len(subject.syllabus_paths),
    }
    return inventory, issues


def _audit_pdf(
    subject: ExamSubject, path: Path, root: Path
) -> tuple[dict[str, Any], list[AuditIssue]]:
    issues: list[AuditIssue] = []
    import pdfplumber

    pages: list[ExtractedPage] = []
    table_pages = 0
    error: str | None = None
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if page.extract_tables():
                    table_pages += 1
                pages.append(
                    ExtractedPage(
                        page_number=len(pages) + 1,
                        text=text,
                        method=ExtractionMethod.EMBEDDED_TEXT,
                    )
                )
    except Exception as exc:  # noqa: BLE001 - audit reports parser failures
        error = f"{type(exc).__name__}: {exc}"
        _add(
            issues,
            "error",
            "pdf_unreadable",
            subject,
            path,
            message=error,
            action="syllabus not imported",
        )

    text_extractable = any(page.text.strip() for page in pages)
    char_count = sum(len(page.text) for page in pages)
    segmentation = segment_exam_syllabus(pages) if pages else None
    if not error and not text_extractable:
        _add(
            issues,
            "error",
            "needs_ocr",
            subject,
            path,
            message="PDF has no extractable text layer",
            action="not imported; an OCR provider is required",
        )
    elif segmentation is not None and not segmentation.units:
        _add(
            issues,
            "warning",
            "syllabus_unsegmented",
            subject,
            path,
            message="no unit/section headings could be extracted",
            action="syllabus imported; coverage mapping is unavailable for this document",
        )
    elif segmentation is not None and segmentation.status.value != "segmented":
        _add(
            issues,
            "warning",
            "syllabus_inferred_structure",
            subject,
            path,
            message=(f"{len(segmentation.units)} unit(s) extracted with an inferred structure"),
            action="units marked for review; verify before treating coverage as authoritative",
        )

    inventory = {
        "category_id": subject.category_id,
        "category_name": subject.category.name,
        "subject": subject.subject_name,
        "course_id": subject.course_id,
        "course_code": subject.course_code,
        "file_type": "pdf",
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "encoding": "binary",
        "records": len(pages),
        "columns": "",
        "examination_years": subject.syllabus_version or "",
        "questions_with_text": 0,
        "answers_present": 0,
        "options_present": 0,
        "solutions_present": 0,
        "duplicate_question_ids": 0,
        "corrupted_text_rows": 0,
        "syllabus_documents": 1,
        "syllabus_pages": len(pages),
        "syllabus_chars": char_count,
        "syllabus_text_extractable": text_extractable,
        "syllabus_table_pages": table_pages,
        "syllabus_units": len(segmentation.units) if segmentation else 0,
        "syllabus_topics": sum(len(unit.topics) for unit in segmentation.units)
        if segmentation
        else 0,
        "syllabus_error": error or "",
    }
    return inventory, issues


def audit_dataset(root: Path | str = Path("datasets")) -> AuditResult:
    root_path = Path(root)
    catalog = discover(root_path)
    inventory: list[dict[str, Any]] = []
    issues: list[AuditIssue] = []

    for subject in catalog.subjects:
        try:
            row_inventory, row_issues = _audit_csv(subject, root_path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the audit
            row_inventory = {
                "category_id": subject.category_id,
                "category_name": subject.category.name,
                "subject": subject.subject_name,
                "course_id": subject.course_id,
                "course_code": subject.course_code,
                "file_type": "csv",
                "relative_path": subject.csv_path.relative_to(root_path).as_posix(),
                "records": 0,
                "syllabus_documents": len(subject.syllabus_paths),
            }
            row_issues = [
                AuditIssue(
                    severity="error",
                    code="csv_unreadable",
                    category_id=subject.category_id,
                    subject=subject.subject_name,
                    relative_path=subject.csv_path.relative_to(root_path).as_posix(),
                    row_number=None,
                    question_id=None,
                    message=f"{type(exc).__name__}: {exc}",
                    action="file skipped",
                )
            ]
        inventory.append(row_inventory)
        issues.extend(row_issues)
        for pdf in subject.syllabus_paths:
            pdf_inventory, pdf_issues = _audit_pdf(subject, pdf, root_path)
            inventory.append(pdf_inventory)
            issues.extend(pdf_issues)

    csv_rows = [row for row in inventory if row["file_type"] == "csv"]
    pdf_rows = [row for row in inventory if row["file_type"] == "pdf"]
    question_types: dict[str, int] = {}
    answer_types: dict[str, int] = {}
    # Re-derive distributions from actual files for the summary; the inventory
    # keeps per-file counts and the report keeps per-row findings.
    for subject in catalog.subjects:
        text, _encoding, _confident = decode_text(subject.csv_path.read_bytes())
        for index, row in enumerate(csv.DictReader(io.StringIO(text))):
            parsed: ParsedRow = parse_row(row, index=index + 2)
            if not parsed.question_text.strip():
                continue
            question_types[parsed.question_type] = question_types.get(parsed.question_type, 0) + 1
            answer_types[parsed.answer_type] = answer_types.get(parsed.answer_type, 0) + 1

    severity_counts = {"error": 0, "warning": 0, "info": 0}
    code_counts: dict[str, int] = {}
    for issue in issues:
        severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
        code_counts[issue.code] = code_counts.get(issue.code, 0) + 1

    years: set[int] = set()
    for row in csv_rows:
        for value in str(row.get("examination_years", "")).split(","):
            if value.strip().isdigit():
                years.add(int(value.strip()))

    summary = {
        "phase": 13,
        "dataset_root": root_path.resolve().as_posix(),
        "generated_at": datetime.now(UTC).isoformat(),
        "files": {
            "discovered": len(inventory),
            "csv": len(csv_rows),
            "pdf": len(pdf_rows),
            "other": 0,
        },
        "categories": [
            {"category_id": category.category_id, "name": category.name}
            for category in catalog.categories
        ],
        "subjects": [
            {
                "course_id": subject.course_id,
                "category_id": subject.category_id,
                "subject": subject.subject_name,
                "csv": subject.csv_path.relative_to(root_path).as_posix(),
                "syllabus": [
                    path.relative_to(root_path).as_posix() for path in subject.syllabus_paths
                ],
                "syllabus_version": subject.syllabus_version,
            }
            for subject in catalog.subjects
        ],
        "records": {
            "csv_rows": sum(int(row.get("records", 0)) for row in csv_rows),
            "questions_with_text": sum(int(row.get("questions_with_text", 0)) for row in csv_rows),
            "answers_present": sum(int(row.get("answers_present", 0)) for row in csv_rows),
            "options_present": sum(int(row.get("options_present", 0)) for row in csv_rows),
            "solutions_present": sum(int(row.get("solutions_present", 0)) for row in csv_rows),
            "matched_question_answer_pairs": sum(
                int(row.get("answers_present", 0)) for row in csv_rows
            ),
            "duplicate_question_ids": sum(
                int(row.get("duplicate_question_ids", 0)) for row in csv_rows
            ),
            "corrupted_text_rows": sum(int(row.get("corrupted_text_rows", 0)) for row in csv_rows),
        },
        "examination_years": {
            "available": sorted(years),
            "count": len(years),
        },
        "question_types": dict(sorted(question_types.items())),
        "answer_types": dict(sorted(answer_types.items())),
        "syllabus": {
            "documents": len(pdf_rows),
            "text_extractable": sum(1 for row in pdf_rows if row.get("syllabus_text_extractable")),
            "needs_ocr": sum(1 for row in pdf_rows if not row.get("syllabus_text_extractable")),
            "table_documents": sum(
                1 for row in pdf_rows if int(row.get("syllabus_table_pages", 0)) > 0
            ),
            "units_detected": sum(int(row.get("syllabus_units", 0)) for row in pdf_rows),
            "topics_detected": sum(int(row.get("syllabus_topics", 0)) for row in pdf_rows),
            "documents_without_units": sum(
                1 for row in pdf_rows if int(row.get("syllabus_units", 0)) == 0
            ),
        },
        "validation": {
            "issues": len(issues),
            **severity_counts,
            "by_code": dict(sorted(code_counts.items())),
        },
        "compatibility": {
            "schema": "uniform 10-column exam CSV schema"
            if all(int(row.get("columns", 0)) == len(EXPECTED_COLUMNS) for row in csv_rows)
            else "mixed or unexpected CSV schemas",
            "answer_formats": sorted(answer_types),
            "question_formats": sorted(question_types),
        },
    }
    return AuditResult(
        root=root_path,
        catalog=catalog,
        inventory=inventory,
        issues=issues,
        summary=summary,
    )


__all__ = ["AuditIssue", "AuditResult", "EXPECTED_COLUMNS", "audit_dataset"]
