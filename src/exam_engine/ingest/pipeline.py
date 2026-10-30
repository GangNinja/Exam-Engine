"""Ingestion pipeline: validate -> extract -> OCR -> segment -> persist.

Each document is processed in its own transaction so one bad file cannot abort a
whole batch; every failure becomes an :class:`IngestionError` in the report.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.config import IngestionLimits, Settings
from exam_engine.ingest.adapters import register_builtin_adapters  # noqa: F401
from exam_engine.ingest.base import (
    ExtractedDocument,
    ExtractedQuestion,
    ExtractionContext,
    adapter_for_path,
    apply_char_limit,
    ingestion_warning,
)
from exam_engine.ingest.manifest import ManifestDocument, load_manifest
from exam_engine.ingest.normalize import fingerprint_text, normalize_text
from exam_engine.ingest.ocr import (
    OCRFailedError,
    OCRUnavailableError,
    get_ocr_provider,
    render_pdf_page_png,
)
from exam_engine.ingest.quality import (
    QualityReport,
    assess_pages,
    mark_question_duplicates,
)
from exam_engine.ingest.segmentation import segment_questions_pages
from exam_engine.ingest.syllabus_segmenter import (
    SyllabusSegmentation,
    segment_syllabus,
)
from exam_engine.ingest.validation import (
    FileValidationError,
    resolve_source_path,
    validate_source_file,
)
from exam_engine.models.course import CourseRecord
from exam_engine.models.document import (
    DocumentRecord,
    PageRecord,
    content_digest,
    make_document_id,
)
from exam_engine.models.enums import (
    DocumentKind,
    DuplicatePolicy,
    ExtractionMethod,
    ExtractionStatus,
    IngestionStatus,
    Severity,
    SourceFormat,
)
from exam_engine.models.ingestion import (
    BatchIngestionResult,
    IngestionError,
    IngestionResult,
)
from exam_engine.models.provenance import DatasetSource
from exam_engine.models.question import (
    QuestionRecord,
    Uncertainty,
    make_question_id,
)
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)
from exam_engine.storage.db import Database
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    RunRepository,
    SyllabusRepository,
)
from exam_engine.version import PIPELINE_VERSION

logger = logging.getLogger(__name__)

# Uncertainty codes that always require human review, even when the numeric
# confidence happens to stay above the threshold.
REVIEW_REQUIRED_CODES = frozenset(
    {"possible_instruction", "short_question_text", "multiple_marks_values"}
)


class IngestionPipeline:
    """Coordinates adapters, segmenters, OCR and storage for Phase 1."""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        ocr_provider: str | None = None,
    ) -> None:
        self.db = database
        self.settings = settings or Settings()
        self.limits: IngestionLimits = self.settings.limits
        provider_name = ocr_provider or self.settings.ocr_provider
        self.ocr = get_ocr_provider(provider_name)

    # -- public API --------------------------------------------------------
    def ingest_manifest(
        self,
        manifest_path: Path | str,
        *,
        duplicate_policy: DuplicatePolicy = DuplicatePolicy.SKIP,
        dry_run: bool = False,
        allow_external: bool = False,
    ) -> BatchIngestionResult:
        manifest, base_dir = load_manifest(manifest_path)
        run_id = _new_run_id()
        started = _utcnow()
        results: list[IngestionResult] = []
        batch_warnings = _dataset_warnings(manifest.dataset)

        if not dry_run:
            with self.db.transaction() as session:
                RunRepository(session).create(
                    run_id,
                    started_at=started,
                    manifest_path=str(Path(manifest_path).resolve()),
                    source_id=manifest.dataset.source_id,
                )

        for course_spec in manifest.courses:
            course = course_spec.to_record()
            course_warnings = list(batch_warnings)
            if not dry_run:
                course_warnings.extend(self._persist_course(course, manifest.dataset))
            for document_spec in course_spec.documents:
                results.append(
                    self._run_document(
                        document_spec,
                        course,
                        manifest.dataset,
                        base_dir,
                        duplicate_policy=duplicate_policy,
                        dry_run=dry_run,
                        allow_external=allow_external,
                        inherited_warnings=course_warnings,
                    )
                )

        finished = _utcnow()
        batch = BatchIngestionResult(
            run_id=run_id,
            started_at=started,
            finished_at=finished,
            manifest_path=str(Path(manifest_path).resolve()),
            source_id=manifest.dataset.source_id,
            results=results,
            warnings=batch_warnings,
        )
        if not dry_run:
            try:
                with self.db.transaction() as session:
                    RunRepository(session).save(run_id, finished_at=finished, results=results)
            except Exception:  # noqa: BLE001 - reporting must not break ingestion
                logger.exception("failed to persist ingestion run %s", run_id)
        return batch

    def ingest_file(
        self,
        path: Path | str,
        *,
        course: CourseRecord,
        source: DatasetSource,
        kind: DocumentKind = DocumentKind.OTHER,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        duplicate_policy: DuplicatePolicy = DuplicatePolicy.SKIP,
        dry_run: bool = False,
    ) -> IngestionResult:
        file_path = Path(path)
        # The spec path is resolved against its own parent, so it must be the
        # bare file name: keeping a relative directory component would make
        # "sub/doc.txt" resolve to "sub/sub/doc.txt".
        spec = ManifestDocument(
            path=file_path.name,
            kind=kind,
            title=title,
            metadata=metadata or {},
        )
        inherited = _dataset_warnings(source)
        if not dry_run:
            inherited.extend(self._persist_course(course, source))
        return self._run_document(
            spec,
            course,
            source,
            file_path.parent,
            duplicate_policy=duplicate_policy,
            dry_run=dry_run,
            allow_external=True,
            inherited_warnings=inherited,
        )

    # -- course/source persistence ----------------------------------------
    def _persist_course(self, course: CourseRecord, source: DatasetSource) -> list[IngestionError]:
        warnings: list[IngestionError] = []
        with self.db.transaction() as session:
            courses = CourseRepository(session)
            conflicts = courses.conflicting_offerings(course)
            if conflicts:
                warnings.append(
                    ingestion_warning(
                        "course_conflict",
                        f"course '{course.course_id}' shares its institution/code/year/"
                        f"version with {', '.join(conflicts)}; documents stay isolated "
                        "per course_id",
                    )
                )
            existing_source = DatasetSourceRepository(session).get(source.source_id)
            if existing_source and (
                existing_source.name != source.name
                or existing_source.dataset_type != source.dataset_type
            ):
                warnings.append(
                    ingestion_warning(
                        "source_conflict",
                        f"dataset source '{source.source_id}' already exists with "
                        f"type '{existing_source.dataset_type.value}'; its provenance "
                        "record is being updated",
                    )
                )
            courses.upsert(course)
            DatasetSourceRepository(session).upsert(source)
        return warnings

    # -- single document ---------------------------------------------------
    def _run_document(
        self,
        spec: ManifestDocument,
        course: CourseRecord,
        source: DatasetSource,
        base_dir: Path,
        **options: Any,
    ) -> IngestionResult:
        """Fault boundary: any unexpected exception becomes a failed result.

        Phase 1 requires that one bad document never aborts a batch, so even a
        bug in a segmenter or record builder is contained here and reported
        instead of propagating out of the batch.
        """
        started = time.perf_counter()
        try:
            return self._ingest_document(spec, course, source, base_dir, **options)
        except Exception as exc:  # noqa: BLE001 - deliberate batch fault boundary
            logger.exception("unexpected failure while ingesting %s", spec.path)
            return _failure(
                spec.path,
                course.course_id,
                "unexpected_error",
                f"unexpected failure: {type(exc).__name__}: {exc}",
                list(options.get("inherited_warnings") or []),
                started,
                {"error_type": type(exc).__name__},
            )

    def _ingest_document(
        self,
        spec: ManifestDocument,
        course: CourseRecord,
        source: DatasetSource,
        base_dir: Path,
        *,
        duplicate_policy: DuplicatePolicy,
        dry_run: bool,
        allow_external: bool,
        inherited_warnings: list[IngestionError],
    ) -> IngestionResult:
        started = time.perf_counter()
        warnings: list[IngestionError] = list(inherited_warnings)
        source_path = spec.path

        try:
            path = resolve_source_path(spec.path, base_dir, allow_external=allow_external)
            facts = validate_source_file(path, self.limits)
        except FileValidationError as exc:
            return _failure(
                source_path, course.course_id, exc.code, exc.message, warnings, started, exc.detail
            )
        except OSError as exc:
            return _failure(
                source_path,
                course.course_id,
                "unreadable_file",
                f"could not read '{spec.path}': {exc}",
                warnings,
                started,
            )

        if spec.format is not None and spec.format is not facts.format:
            warnings.append(
                ingestion_warning(
                    "format_mismatch",
                    f"manifest declares '{spec.format.value}' but the file is "
                    f"'{facts.format.value}'; the file content is authoritative",
                    severity=Severity.INFO,
                )
            )

        try:
            data = path.read_bytes()
        except OSError as exc:
            return _failure(
                source_path,
                course.course_id,
                "unreadable_file",
                f"could not read '{spec.path}': {exc}",
                warnings,
                started,
            )

        sha = content_digest(data)
        document_id = make_document_id(course.course_id, sha)
        existing_document = None
        try:
            with self.db.session() as session:
                existing_document = DocumentRepository(session).find_by_content(
                    course.course_id, sha
                )
        except Exception as exc:  # noqa: BLE001 - DB failures must be reported
            return _failure(
                source_path,
                course.course_id,
                "database_error",
                f"could not check for duplicates: {exc}",
                warnings,
                started,
            )

        if existing_document is not None:
            if duplicate_policy is DuplicatePolicy.SKIP:
                warnings.append(
                    ingestion_warning(
                        "duplicate_document",
                        f"identical content already ingested as "
                        f"'{existing_document.document_id}'; skipped",
                        severity=Severity.INFO,
                        detail={"duplicate_of": existing_document.document_id},
                    )
                )
                return IngestionResult(
                    source_path=source_path,
                    course_id=course.course_id,
                    status=IngestionStatus.SKIPPED_DUPLICATE,
                    document=existing_document,
                    page_count=existing_document.page_count,
                    duplicate_of=existing_document.document_id,
                    warnings=warnings,
                    duration_ms=_elapsed_ms(started),
                )
            if duplicate_policy is DuplicatePolicy.FAIL:
                return _failure(
                    source_path,
                    course.course_id,
                    "duplicate_document",
                    f"identical content already ingested as "
                    f"'{existing_document.document_id}' (policy: fail)",
                    warnings,
                    started,
                    {"duplicate_of": existing_document.document_id},
                )
            warnings.append(
                ingestion_warning(
                    "duplicate_document",
                    f"replacing existing document '{existing_document.document_id}' "
                    "and its derived records (policy: replace)",
                    severity=Severity.INFO,
                )
            )

        # --- extract
        try:
            adapter = adapter_for_path(path)
            extracted = adapter.extract(
                path,
                ExtractionContext(path=path, limits=self.limits, base_dir=base_dir),
            )
        except FileValidationError as exc:
            return _failure(
                source_path,
                course.course_id,
                exc.code,
                exc.message,
                warnings,
                started,
                exc.detail,
            )
        except Exception as exc:  # noqa: BLE001 - third-party parsers raise broadly
            logger.exception("adapter failed for %s", path)
            return _failure(
                source_path,
                course.course_id,
                "parse_error",
                f"could not parse '{spec.path}': {exc}",
                warnings,
                started,
                {"error": type(exc).__name__},
            )

        warnings.extend(extracted.warnings)

        # --- OCR for scanned pages
        ocr_errors = self._apply_ocr(path, facts.format, extracted)
        warnings.extend(ocr_errors)

        pages, truncation_warning = apply_char_limit(
            extracted.pages, self.limits.max_document_chars
        )
        extracted.pages = pages
        if truncation_warning:
            warnings.append(truncation_warning)

        if not pages and extracted.questions:
            # Structured banks have no pages; score them from the questions that
            # were parsed rather than reporting a false "empty document".
            mean_confidence = sum(q.confidence for q in extracted.questions) / len(
                extracted.questions
            )
            quality = QualityReport(
                score=round(mean_confidence, 3),
                flags=[],
                char_count=sum(len(q.text) for q in extracted.questions),
            )
        else:
            quality = assess_pages(pages, min_page_chars=self.limits.min_page_chars)
        if quality.flags and quality.char_count:
            warnings.append(
                ingestion_warning(
                    "low_quality_text",
                    f"extraction quality flags: {', '.join(quality.flags)}",
                    severity=Severity.WARNING,
                    detail={"flags": quality.flags, "score": quality.score},
                )
            )

        text_fingerprint = fingerprint_text("\n".join(page.text for page in pages))
        if existing_document is None and text_fingerprint:
            try:
                with self.db.session() as session:
                    near = DocumentRepository(session).find_by_text_fingerprint(
                        course.course_id, text_fingerprint
                    )
                if near is not None:
                    warnings.append(
                        ingestion_warning(
                            "duplicate_document",
                            f"text is identical to document '{near.document_id}' after "
                            "normalisation (different bytes); both are kept",
                            severity=Severity.INFO,
                            detail={"similar_document": near.document_id},
                        )
                    )
            except Exception:  # noqa: BLE001
                logger.exception("near-duplicate check failed for %s", path)

        extraction_status = _extraction_status(extracted)
        page_records = [
            PageRecord(
                page_number=page.page_number,
                text=page.text,
                method=page.method,
                has_images=page.has_images,
                ocr_attempted=page.ocr_attempted,
            )
            for page in pages
        ]

        # --- segmentation
        ext_questions: list[ExtractedQuestion] = list(extracted.questions)
        syllabus_segmentation: SyllabusSegmentation | None = None
        segmentation_meta: dict[str, Any] = {}

        if not ext_questions and spec.kind in (
            DocumentKind.QUESTION_PAPER,
            DocumentKind.QUESTION_BANK,
        ):
            segmentation = segment_questions_pages(
                pages,
                min_chars=self.limits.min_question_chars,
                max_questions=self.limits.max_questions_per_document,
                min_confidence=self.limits.min_question_confidence,
            )
            ext_questions = segmentation.questions
            warnings.extend(_uncertainty_warnings(segmentation.uncertainties))
            segmentation_meta = {
                "status": segmentation.status.value,
                "confidence": segmentation.confidence,
                "sections": segmentation.sections,
                "numbering_gaps": [list(gap) for gap in segmentation.numbering_gaps],
                "rejected_markers": segmentation.rejected_markers,
            }
        elif spec.kind is DocumentKind.SYLLABUS:
            syllabus_segmentation = segment_syllabus(pages)
            warnings.extend(_uncertainty_warnings(syllabus_segmentation.uncertainties))
            segmentation_meta = {
                "status": syllabus_segmentation.status.value,
                "confidence": syllabus_segmentation.confidence,
                "unit_count": len(syllabus_segmentation.units),
            }

        # Applied to structured banks and to heuristic segmentation alike.
        ext_questions = self._cap_questions(ext_questions, warnings)

        if not any(page.text.strip() for page in pages) and not ext_questions:
            warnings.append(
                ingestion_warning(
                    "empty_document",
                    "no extractable text was produced for this document",
                    severity=Severity.WARNING,
                )
            )

        # --- build records
        question_records = _build_question_records(
            ext_questions,
            document_id=document_id,
            course_id=course.course_id,
            min_confidence=self.limits.min_question_confidence,
        )
        nonempty_questions = sum(1 for question in ext_questions if question.text.strip())
        if len(question_records) < nonempty_questions:
            warnings.append(
                ingestion_warning(
                    "duplicate_question_record",
                    f"{nonempty_questions - len(question_records)} identical question "
                    "record(s) were collapsed into one",
                    severity=Severity.INFO,
                )
            )
        syllabus_record = None
        if syllabus_segmentation is not None:
            syllabus_record = _build_syllabus_record(
                syllabus_segmentation,
                document_id=document_id,
                course_id=course.course_id,
                source_id=source.source_id,
            )

        if question_records and extraction_status is ExtractionStatus.EMPTY:
            extraction_status = ExtractionStatus.OK
        status = _status_for(extraction_status, replaced=existing_document is not None)

        document = DocumentRecord(
            document_id=document_id,
            course_id=course.course_id,
            source_id=source.source_id,
            kind=spec.kind,
            format=facts.format,
            title=spec.title
            or extracted.title_hint
            or Path(spec.path).stem.replace("_", " ").strip()
            or Path(spec.path).name,
            original_filename=path.name,
            relative_path=_relative_path(path, base_dir),
            content_sha256=sha,
            text_fingerprint=text_fingerprint,
            byte_size=facts.byte_size,
            page_count=len(page_records),
            char_count=sum(page.char_count for page in page_records)
            or sum(len(question.text) for question in question_records),
            extraction_status=extraction_status,
            quality_score=quality.score,
            quality_flags=quality.flags,
            language=spec.language,
            academic_year=spec.academic_year or course.academic_year,
            publication_date=spec.publication_date,
            pipeline_version=PIPELINE_VERSION,
            metadata={
                **extracted.metadata,
                **spec.metadata,
                "segmentation": segmentation_meta,
                "table_count": extracted.table_count,
            },
            warnings=[w.message for w in warnings if w.severity is Severity.WARNING],
        )

        # --- duplicate questions (course-scoped)
        if question_records:
            try:
                with self.db.session() as session:
                    known = QuestionRepository(session).fingerprint_index(
                        course.course_id, exclude_document_id=document_id
                    )
                mark_question_duplicates(question_records, known)
            except Exception:  # noqa: BLE001
                logger.exception("question duplicate detection failed for %s", document_id)

        # --- persist
        if not dry_run:
            try:
                with self.db.transaction() as session:
                    documents = DocumentRepository(session)
                    if existing_document is not None:
                        documents.delete(existing_document.document_id)
                    documents.upsert(document)
                    documents.replace_pages(document_id, page_records)
                    QuestionRepository(session).replace_for_document(document_id, question_records)
                    if syllabus_record is not None:
                        SyllabusRepository(session).replace(syllabus_record)
            except Exception as exc:  # noqa: BLE001
                logger.exception("failed to persist document %s", document_id)
                return _failure(
                    source_path,
                    course.course_id,
                    "database_error",
                    f"could not store extracted records: {exc}",
                    warnings,
                    started,
                    {"document_id": document_id},
                )

        return IngestionResult(
            source_path=source_path,
            course_id=course.course_id,
            status=status,
            document=document,
            page_count=len(page_records),
            question_count=len(question_records),
            syllabus_unit_count=len(syllabus_record.units) if syllabus_record else 0,
            duplicate_of=existing_document.document_id if existing_document else None,
            errors=[],
            warnings=warnings,
            duration_ms=_elapsed_ms(started),
        )

    def _cap_questions(
        self,
        questions: list[ExtractedQuestion],
        warnings: list[IngestionError],
    ) -> list[ExtractedQuestion]:
        """Enforce the per-document question cap for every question source."""
        limit = self.limits.max_questions_per_document
        if len(questions) <= limit:
            return questions
        dropped = len(questions) - limit
        warnings.append(
            ingestion_warning(
                "question_limit_exceeded",
                f"{dropped} question(s) dropped: the per-document limit is {limit}",
                severity=Severity.WARNING,
                detail={"dropped": dropped, "limit": limit},
            )
        )
        return questions[:limit]

    # -- OCR ---------------------------------------------------------------
    def _apply_ocr(
        self, path: Path, source_format: SourceFormat, extracted: ExtractedDocument
    ) -> list[IngestionError]:
        if not extracted.needs_ocr_pages:
            return []
        warnings: list[IngestionError] = []
        if not self.ocr.available():
            warnings.append(
                ingestion_warning(
                    "ocr_unavailable",
                    f"{len(extracted.needs_ocr_pages)} page(s) need OCR but provider "
                    f"'{self.ocr.name}' is unavailable; pages were stored without text",
                    severity=Severity.WARNING,
                    detail={"pages": list(extracted.needs_ocr_pages)},
                )
            )
            return warnings

        page_lookup = {page.page_number: page for page in extracted.pages}
        for page_number in list(extracted.needs_ocr_pages):
            page = page_lookup.get(page_number)
            if page is None:
                continue
            try:
                if source_format is SourceFormat.PDF:
                    image = render_pdf_page_png(path, page_number, self.limits.ocr_dpi)
                elif source_format is SourceFormat.IMAGE:
                    image = path.read_bytes()
                else:
                    warnings.append(
                        ingestion_warning(
                            "ocr_failed",
                            f"no OCR rendering path for format "
                            f"'{source_format.value}' on page {page_number}",
                            severity=Severity.WARNING,
                        )
                    )
                    break
                text = normalize_text(self.ocr.extract_text(image))
            except OCRUnavailableError as exc:
                warnings.append(
                    ingestion_warning(
                        "ocr_unavailable",
                        f"OCR became unavailable: {exc}",
                        severity=Severity.WARNING,
                    )
                )
                break
            except (OCRFailedError, OSError, ValueError) as exc:
                warnings.append(
                    ingestion_warning(
                        "ocr_failed",
                        f"OCR failed on page {page_number}: {exc}",
                        severity=Severity.WARNING,
                        detail={"page": page_number},
                    )
                )
                continue

            page.text = text
            page.method = ExtractionMethod.OCR
            page.ocr_attempted = True
            if text.strip():
                extracted.needs_ocr_pages.remove(page_number)
            if not text.strip():
                warnings.append(
                    ingestion_warning(
                        "ocr_failed",
                        f"OCR returned no text for page {page_number}",
                        severity=Severity.WARNING,
                        detail={"page": page_number},
                    )
                )
        return warnings


# --- helpers ----------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _new_run_id() -> str:
    stamp = _utcnow().strftime("%Y%m%dT%H%M%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:6]}"


def _dataset_warnings(source: DatasetSource) -> list[IngestionError]:
    return [
        ingestion_warning("dataset_provenance", message, severity=Severity.INFO)
        for message in source.provenance_warnings()
    ]


def _relative_path(path: Path, base_dir: Path) -> str | None:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return None


def _failure(
    source_path: str,
    course_id: str,
    code: str,
    message: str,
    warnings: list[IngestionError],
    started: float,
    detail: dict[str, Any] | None = None,
) -> IngestionResult:
    return IngestionResult(
        source_path=source_path,
        course_id=course_id,
        status=IngestionStatus.FAILED,
        errors=[
            IngestionError(
                code=code,
                message=message,
                severity=Severity.ERROR,
                source_path=source_path,
                detail=detail or {},
            )
        ],
        warnings=warnings,
        duration_ms=_elapsed_ms(started),
    )


def _extraction_status(extracted: ExtractedDocument) -> ExtractionStatus:
    has_text = any(page.text.strip() for page in extracted.pages)
    pending_ocr = bool(extracted.needs_ocr_pages)
    if not has_text:
        if pending_ocr or extracted.format is SourceFormat.IMAGE:
            return ExtractionStatus.NEEDS_OCR
        return ExtractionStatus.EMPTY
    if pending_ocr:
        return ExtractionStatus.PARTIAL
    return ExtractionStatus.OK


def _status_for(extraction_status: ExtractionStatus, *, replaced: bool) -> IngestionStatus:
    if extraction_status is ExtractionStatus.NEEDS_OCR:
        return IngestionStatus.NEEDS_OCR
    if extraction_status is ExtractionStatus.EMPTY:
        return IngestionStatus.EMPTY
    return IngestionStatus.REPLACED if replaced else IngestionStatus.INGESTED


def _uncertainty_warnings(uncertainties: list[Uncertainty]) -> list[IngestionError]:
    warnings: list[IngestionError] = []
    for uncertainty in uncertainties:
        if uncertainty.severity is Severity.INFO:
            continue
        warnings.append(
            ingestion_warning(
                uncertainty.code,
                uncertainty.message,
                severity=uncertainty.severity,
                detail=uncertainty.detail,
            )
        )
    return warnings


def _build_question_records(
    extracted: list[ExtractedQuestion],
    *,
    document_id: str,
    course_id: str,
    min_confidence: float,
) -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    seen_ids: set[str] = set()
    for question in extracted:
        text = question.text.strip()
        if not text:
            continue
        question_id = make_question_id(document_id, question.question_number, text)
        if question_id in seen_ids:
            # Same document, same number and same text: one stored record only.
            continue
        seen_ids.add(question_id)
        codes = {uncertainty.code for uncertainty in question.uncertainties}
        needs_review = bool(question.confidence < min_confidence or codes & REVIEW_REQUIRED_CODES)
        records.append(
            QuestionRecord(
                question_id=question_id,
                document_id=document_id,
                course_id=course_id,
                text=text,
                question_number=question.question_number,
                number_sort=question.number_sort,
                section=question.section,
                unit=question.unit,
                marks=question.marks,
                marks_source=question.marks_source,
                page_number=question.page_number,
                source_locator=question.source_locator,
                confidence=question.confidence,
                needs_review=needs_review,
                uncertainties=question.uncertainties,
                metadata=question.metadata,
            )
        )
    return records


def _build_syllabus_record(
    segmentation: SyllabusSegmentation,
    *,
    document_id: str,
    course_id: str,
    source_id: str,
) -> SyllabusRecord:
    syllabus_id = make_syllabus_id(document_id)
    units: list[SyllabusUnitRecord] = []
    seen_unit_ids: set[str] = set()
    for unit in segmentation.units:
        unit_id = make_unit_id(syllabus_id, unit.unit_label, unit.raw_text)
        if unit_id in seen_unit_ids:
            # Identical heading and body: keep one unit instead of colliding on
            # the primary key (which would fail the whole document).
            continue
        seen_unit_ids.add(unit_id)
        units.append(
            SyllabusUnitRecord(
                unit_id=unit_id,
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
        )
    collapsed = len(segmentation.units) - len(units)
    uncertainties = list(segmentation.uncertainties)
    if collapsed:
        uncertainties.append(
            Uncertainty(
                code="duplicate_syllabus_unit",
                message=f"{collapsed} identical syllabus unit(s) were collapsed into one",
                severity=Severity.INFO,
            )
        )
    return SyllabusRecord(
        syllabus_id=syllabus_id,
        course_id=course_id,
        document_id=document_id,
        source_id=source_id,
        title=segmentation.title_hint,
        units=units,
        confidence=segmentation.confidence,
        needs_review=segmentation.needs_review,
        uncertainties=uncertainties,
    )


__all__ = ["IngestionPipeline"]
