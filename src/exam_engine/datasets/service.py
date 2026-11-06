"""Phase 13 ingestion service for the personal examination datasets."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from exam_engine.config import Settings
from exam_engine.datasets.audit import AuditResult, audit_dataset
from exam_engine.datasets.catalog import DatasetCatalog, ExamSubject, discover
from exam_engine.datasets.parsing import (
    derive_occurrence,
    extract_correct_option,
    normalise_answer_type,
    normalise_question_type,
    parse_options,
    parse_options_from_text,
)
from exam_engine.datasets.syllabus import segment_exam_syllabus
from exam_engine.ingest.adapters.pdf_adapter import PdfAdapter
from exam_engine.ingest.base import ExtractionContext
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.course import CourseRecord
from exam_engine.models.enums import (
    DatasetType,
    DocumentKind,
    DuplicatePolicy,
    IngestionStatus,
)
from exam_engine.models.provenance import DatasetSource
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)
from exam_engine.storage.db import Database
from exam_engine.storage.exam_repository import ExamDatasetRepository
from exam_engine.storage.repositories import (
    QuestionRepository,
    SyllabusRepository,
)

SOURCE_ID = "personal-exam-datasets"
DATASET_SOURCE = DatasetSource(
    source_id=SOURCE_ID,
    name="Personal GATE/CBSE/JEE/NEET examination dataset",
    dataset_type=DatasetType.UNKNOWN,
    retrieved_at=date(2026, 11, 4),
    license=None,
    usage_terms=(
        "Personal dataset supplied by the project owner for local analysis. "
        "Not redistributed by this repository."
    ),
    publisher=None,
    notes=(
        "retrieved_at records the Phase 13 import date; the source files carry no "
        "acquisition date. CSV question banks and syllabus PDFs are used in place."
    ),
    external_dataset=True,
)


@dataclass
class ImportFileReport:
    category_id: str
    subject_code: str
    course_id: str
    relative_path: str
    kind: str
    status: str
    row_count: int = 0
    imported_questions: int = 0
    imported_answers: int = 0
    imported_syllabus_units: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    duration_ms: int = 0
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImportReport:
    run_id: str
    started_at: datetime
    finished_at: datetime
    status: str
    files: list[ImportFileReport]
    totals: dict[str, Any]
    warnings: list[str]
    errors: list[str]
    duration_ms: int
    output_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["started_at"] = self.started_at.isoformat()
        payload["finished_at"] = self.finished_at.isoformat()
        payload["files"] = [item.as_dict() for item in self.files]
        return payload


def _now() -> datetime:
    return datetime.now(UTC)


def _new_run_id() -> str:
    return f"exrun_{_now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _paper_id(course_id: str, label: str) -> str:
    digest = hashlib.sha256(f"{course_id}\x1f{label}".encode()).hexdigest()
    return f"pap_{digest[:24]}"


def _answer_id(question_id: str) -> str:
    return "ans_" + hashlib.sha256(question_id.encode()).hexdigest()[:24]


def course_for_subject(subject: ExamSubject) -> CourseRecord:
    return CourseRecord(
        course_id=subject.course_id,
        course_code=subject.course_code,
        title=f"{subject.subject_name} — {subject.category.name}",
        institution_code=subject.category.institution_code,
        institution_name=subject.category.institution_name,
        programme=subject.category.programme,
        academic_year=None,
        syllabus_version=subject.syllabus_version,
        description=(
            f"Examination course imported from the personal {subject.category.name} "
            f"{subject.subject_name} dataset. Question occurrences keep their source year "
            "and paper ids."
        ),
        metadata={
            "category_id": subject.category_id,
            "subject": subject.subject_name,
            "dataset_source": SOURCE_ID,
        },
    )


class ExamDatasetService:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.db = database
        self.settings = settings or Settings()
        self.pipeline = IngestionPipeline(database, settings=self.settings)

    # -- discovery / audit -------------------------------------------------
    def discover(self, root: Path | str = Path("datasets")) -> DatasetCatalog:
        return discover(root)

    def audit(
        self,
        root: Path | str = Path("datasets"),
        output_dir: Path | str = Path("reports/phase13"),
    ) -> AuditResult:
        result = audit_dataset(root)
        result.write(output_dir)
        return result

    # -- import ------------------------------------------------------------
    def import_dataset(
        self,
        root: Path | str = Path("datasets"),
        *,
        output_dir: Path | str = Path("reports/phase13"),
        categories: list[str] | None = None,
        subjects: list[str] | None = None,
        with_syllabus: bool = True,
        on_duplicate: DuplicatePolicy = DuplicatePolicy.SKIP,
        dry_run: bool = False,
    ) -> ImportReport:
        catalog = discover(root)
        root_path = Path(root)
        selected = [
            subject
            for subject in catalog.subjects
            if (not categories or subject.category_id in categories)
            and (not subjects or subject.course_id in subjects or subject.subject_name in subjects)
        ]
        if not selected:
            raise ValueError("no dataset subjects matched the requested filters")

        run_id = _new_run_id()
        started = _now()
        started_perf = time.perf_counter()
        files: list[ImportFileReport] = []
        warnings: list[str] = []
        errors: list[str] = []

        if not dry_run:
            with self.db.transaction() as session:
                ExamDatasetRepository(session).create_run(run_id, started)

        for subject in selected:
            course = course_for_subject(subject)
            files.append(self._import_csv(subject, course, root_path, on_duplicate, dry_run))
            if with_syllabus:
                for pdf in subject.syllabus_paths:
                    files.append(
                        self._import_syllabus(
                            subject, course, pdf, root_path, on_duplicate, dry_run
                        )
                    )
            elif not subject.syllabus_paths:
                warnings.append(f"{subject.course_id}: no syllabus PDF matched this subject")

        finished = _now()
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        totals = {
            "files": len(files),
            "questions": sum(item.imported_questions for item in files),
            "answers": sum(item.imported_answers for item in files),
            "syllabus_units": sum(item.imported_syllabus_units for item in files),
            "skipped": sum(item.skipped_count for item in files),
            "failed_files": sum(1 for item in files if item.status == "failed"),
            "subjects": len(selected),
            "categories": len({item.category_id for item in files}),
            "papers": 0,
            "duration_ms": duration_ms,
            "dry_run": dry_run,
        }
        for item in files:
            errors.extend(f"{item.relative_path}: {message}" for message in item.errors)
            warnings.extend(f"{item.relative_path}: {message}" for message in item.warnings)
        if not dry_run:
            with self.db.transaction() as session:
                repo = ExamDatasetRepository(session)
                repo.save_run(
                    run_id,
                    finished_at=finished,
                    status="ok" if not totals["failed_files"] else "partial",
                    totals=totals,
                    warnings=warnings,
                    errors=errors,
                    duration_ms=duration_ms,
                )
                for item in files:
                    repo.add_file(run_id, item.as_dict())
                totals["papers"] = repo.counts()["papers"]

        report = ImportReport(
            run_id=run_id,
            started_at=started,
            finished_at=finished,
            status="ok" if not totals["failed_files"] else "partial",
            files=files,
            totals=totals,
            warnings=warnings,
            errors=errors,
            duration_ms=duration_ms,
        )
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        output_path = target / "dataset_import_report.json"
        output_path.write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        report.output_path = output_path.as_posix()
        return report

    def _import_csv(
        self,
        subject: ExamSubject,
        course: CourseRecord,
        root: Path,
        on_duplicate: DuplicatePolicy,
        dry_run: bool,
    ) -> ImportFileReport:
        started = time.perf_counter()
        relative = subject.csv_path.relative_to(root).as_posix()
        row_count = _count_rows(subject.csv_path)
        result = self.pipeline.ingest_file(
            subject.csv_path,
            course=course,
            source=DATASET_SOURCE,
            kind=DocumentKind.QUESTION_BANK,
            title=f"{subject.subject_name} question bank ({subject.category.name})",
            metadata={
                "category_id": subject.category_id,
                "subject": subject.subject_name,
                "dataset_family": "personal_exams",
                "source_csv": relative,
            },
            duplicate_policy=on_duplicate,
            dry_run=dry_run,
        )
        imported_questions = 0
        imported_answers = 0
        skipped = max(0, row_count - result.question_count)
        errors = [f"{item.code}: {item.message}" for item in result.errors]
        warnings = [
            f"{item.code}: {item.message}"
            for item in result.warnings
            if item.severity.value != "info"
        ]
        if (
            not dry_run
            and result.document is not None
            and result.status
            not in (
                IngestionStatus.FAILED,
                IngestionStatus.EMPTY,
            )
        ):
            try:
                projected_questions, projected_answers = self._project_document(
                    result.document.document_id, course.course_id, subject, relative
                )
                if result.status is not IngestionStatus.SKIPPED_DUPLICATE:
                    imported_questions = projected_questions
                    imported_answers = projected_answers
            except Exception as exc:  # noqa: BLE001 - one file must not stop the batch
                errors.append(f"projection_error: {type(exc).__name__}: {exc}")
                result = result.model_copy(update={"status": IngestionStatus.FAILED})
        return ImportFileReport(
            category_id=subject.category_id,
            subject_code=course.course_code,
            course_id=course.course_id,
            relative_path=relative,
            kind="question_bank",
            status=result.status.value,
            row_count=row_count,
            imported_questions=imported_questions,
            imported_answers=imported_answers,
            skipped_count=skipped,
            failed_count=1 if result.status is IngestionStatus.FAILED else 0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            message=(
                "dry run: nothing written"
                if dry_run
                else (
                    "already imported; existing records retained"
                    if result.status is IngestionStatus.SKIPPED_DUPLICATE
                    else f"{result.question_count} question record(s) stored"
                )
            ),
            warnings=warnings,
            errors=errors,
        )

    def _import_syllabus(
        self,
        subject: ExamSubject,
        course: CourseRecord,
        path: Path,
        root: Path,
        on_duplicate: DuplicatePolicy,
        dry_run: bool,
    ) -> ImportFileReport:
        started = time.perf_counter()
        relative = path.relative_to(root).as_posix()
        result = self.pipeline.ingest_file(
            path,
            course=course,
            source=DATASET_SOURCE,
            kind=DocumentKind.SYLLABUS,
            title=path.stem.replace("_", " "),
            metadata={
                "category_id": subject.category_id,
                "subject": subject.subject_name,
                "dataset_family": "personal_exams",
                "source_pdf": relative,
                "syllabus_version": subject.syllabus_version,
            },
            duplicate_policy=on_duplicate,
            dry_run=dry_run,
        )
        units = result.syllabus_unit_count
        warnings = [
            f"{item.code}: {item.message}"
            for item in result.warnings
            if item.severity.value != "info"
        ]
        errors = [f"{item.code}: {item.message}" for item in result.errors]
        if (
            not dry_run
            and result.document is not None
            and units == 0
            and result.status not in (IngestionStatus.FAILED, IngestionStatus.EMPTY)
        ):
            try:
                units, fallback_warnings = self._fallback_syllabus(
                    result.document.document_id, path
                )
                warnings.extend(fallback_warnings)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"syllabus_fallback_failed: {type(exc).__name__}: {exc}")
        return ImportFileReport(
            category_id=subject.category_id,
            subject_code=course.course_code,
            course_id=course.course_id,
            relative_path=relative,
            kind="syllabus",
            status=result.status.value,
            row_count=result.page_count,
            imported_syllabus_units=units,
            skipped_count=0,
            failed_count=1 if result.status is IngestionStatus.FAILED else 0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            message=("dry run: nothing written" if dry_run else f"{units} syllabus unit(s) stored"),
            warnings=warnings,
            errors=errors,
        )

    # -- projection --------------------------------------------------------
    def _project_document(
        self, document_id: str, course_id: str, subject: ExamSubject, source_file: str
    ) -> tuple[int, int]:
        with self.db.session() as session:
            questions = QuestionRepository(session).list_by_document(document_id)
        now = _now().isoformat()
        papers: dict[str, dict[str, Any]] = {}
        details: list[dict[str, Any]] = []
        answers: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for question in questions:
            metadata = question.metadata or {}
            source_question_id = str(
                metadata.get("source_question_id")
                or question.question_number
                or question.question_id
            )
            if source_question_id in seen_sources:
                continue
            seen_sources.add(source_question_id)
            year, paper_label = derive_occurrence(source_question_id)
            paper_key = paper_label or source_file
            paper_id = _paper_id(course_id, paper_key)
            papers.setdefault(
                paper_key,
                {
                    "paper_id": paper_id,
                    "course_id": course_id,
                    "document_id": document_id,
                    "category_id": subject.category_id,
                    "subject_code": subject.course_code,
                    "paper_label": paper_key,
                    "exam_year": year,
                    "source_question_prefix": paper_key,
                    "question_count": 0,
                    "created_at": now,
                },
            )
            papers[paper_key]["question_count"] += 1

            question_type_raw = _optional(metadata.get("question_type"))
            options = parse_options(_optional(metadata.get("options")))
            if not options:
                options = parse_options_from_text(question.text)
            question_type = normalise_question_type(question_type_raw, has_options=bool(options))
            details.append(
                {
                    "question_id": question.question_id,
                    "course_id": course_id,
                    "paper_id": paper_id,
                    "category_id": subject.category_id,
                    "subject_code": subject.course_code,
                    "source_question_id": source_question_id,
                    "source_row": _optional_int(metadata.get("source_row")),
                    "original_question_number": _optional(
                        metadata.get("original_question_number") or question.question_number
                    ),
                    "question_type": question_type,
                    "question_type_raw": question_type_raw,
                    "options_json": json.dumps(
                        [option.as_dict() for option in options], ensure_ascii=False
                    ),
                    "has_options": 1 if options else 0,
                    "exam_year": year,
                    "source_file": source_file,
                    "created_at": now,
                }
            )

            answer_text = _optional(metadata.get("answer"))
            if not answer_text:
                continue
            answer_type_raw = _optional(metadata.get("answer_type"))
            answer_type = normalise_answer_type(answer_type_raw, answer_text)
            answer_warnings: list[str] = []
            correct_option = extract_correct_option(answer_text, answer_type, options)
            if answer_type == "mcq_option" and options and correct_option is None:
                answer_warnings.append("correct_option_not_identified")
            if not options and answer_type == "mcq_option":
                answer_warnings.append("options_missing")
            answers.append(
                {
                    "answer_id": _answer_id(question.question_id),
                    "question_id": question.question_id,
                    "course_id": course_id,
                    "source_answer_id": _optional(metadata.get("answer_id")),
                    "answer_text": answer_text,
                    "answer_type": answer_type,
                    "answer_type_raw": answer_type_raw,
                    "correct_option_key": correct_option,
                    "solution_steps": _optional(metadata.get("solution_steps")),
                    "match_method": "csv_row_pairing",
                    "confidence": 1.0,
                    "warnings_json": json.dumps(answer_warnings, ensure_ascii=False),
                    "created_at": now,
                }
            )
        with self.db.transaction() as session:
            ExamDatasetRepository(session).replace_document_projection(
                document_id,
                papers=list(papers.values()),
                details=details,
                answers=answers,
            )
        return len(details), len(answers)

    # -- syllabus fallback -------------------------------------------------
    def _fallback_syllabus(self, document_id: str, path: Path) -> tuple[int, list[str]]:
        with self.db.session() as session:
            syllabus = SyllabusRepository(session).get_by_document(document_id)
        if syllabus is None or syllabus.units:
            return (len(syllabus.units) if syllabus else 0), []
        extracted = PdfAdapter().extract(
            path,
            ExtractionContext(path=path, limits=self.settings.limits, base_dir=path.parent),
        )
        segmentation = segment_exam_syllabus(extracted.pages)
        if not segmentation.units:
            return 0, ["syllabus_unsegmented: no explicit or inferable unit/section headings"]
        syllabus_id = make_syllabus_id(document_id)
        units = [
            SyllabusUnitRecord(
                unit_id=make_unit_id(
                    syllabus_id,
                    unit.unit_label,
                    unit.raw_text,
                ),
                unit_label=unit.unit_label,
                unit_number=unit.unit_number,
                title=unit.title,
                hours=unit.hours,
                topics=unit.topics,
                raw_text=unit.raw_text,
                page_number=unit.page_number,
                confidence=unit.confidence,
                uncertainties=unit.uncertainties,
                metadata=unit.metadata,
            )
            for unit in segmentation.units
        ]
        record = SyllabusRecord(
            syllabus_id=syllabus_id,
            course_id=syllabus.course_id,
            document_id=document_id,
            source_id=syllabus.source_id,
            title=syllabus.title or segmentation.title_hint,
            units=units,
            confidence=segmentation.confidence,
            needs_review=True,
            uncertainties=list(segmentation.uncertainties),
            ingested_at=syllabus.ingested_at,
        )
        with self.db.transaction() as session:
            SyllabusRepository(session).replace(record)
        return len(units), [
            f"inferred_syllabus_structure: {len(units)} unit(s) recovered with review flags"
        ]

    # -- status ------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.db.session() as session:
            repo = ExamDatasetRepository(session)
            return {
                "counts": repo.counts(),
                "latest_run": repo.latest_run(),
            }


def _count_rows(path: Path) -> int:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return sum(1 for _ in reader)


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DATASET_SOURCE",
    "ExamDatasetService",
    "ImportFileReport",
    "ImportReport",
    "SOURCE_ID",
    "course_for_subject",
]
