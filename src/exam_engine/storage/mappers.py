"""Explicit conversion between validated models and ORM rows."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from exam_engine.models.course import CourseRecord
from exam_engine.models.document import DocumentRecord, PageRecord
from exam_engine.models.enums import (
    DatasetType,
    DocumentKind,
    ExtractionMethod,
    ExtractionStatus,
    SourceFormat,
)
from exam_engine.models.provenance import DatasetSource
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import SyllabusRecord, SyllabusUnitRecord
from exam_engine.storage.schema import (
    CourseRow,
    DatasetSourceRow,
    DocumentRow,
    PageRow,
    QuestionRow,
    SyllabusRow,
    SyllabusUnitRow,
)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# --- courses -----------------------------------------------------------------


def course_to_row(course: CourseRecord, row: CourseRow | None = None) -> CourseRow:
    row = row or CourseRow(course_id=course.course_id)
    row.course_code = course.course_code
    row.title = course.title
    row.institution_code = course.institution_code
    row.institution_name = course.institution_name
    row.department = course.department
    row.programme = course.programme
    row.academic_year = course.academic_year
    row.syllabus_version = course.syllabus_version
    row.description = course.description
    row.metadata_json = dumps(course.metadata)
    row.created_at = iso(course.created_at) or ""
    row.updated_at = iso(course.updated_at) or ""
    return row


def row_to_course(row: CourseRow) -> CourseRecord:
    return CourseRecord(
        course_id=row.course_id,
        course_code=row.course_code,
        title=row.title,
        institution_code=row.institution_code,
        institution_name=row.institution_name,
        department=row.department,
        programme=row.programme,
        academic_year=row.academic_year,
        syllabus_version=row.syllabus_version,
        description=row.description,
        metadata=loads(row.metadata_json, {}),
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
        updated_at=parse_dt(row.updated_at) or datetime.now().astimezone(),
    )


# --- dataset sources ---------------------------------------------------------


def source_to_row(source: DatasetSource, row: DatasetSourceRow | None = None) -> DatasetSourceRow:
    row = row or DatasetSourceRow(source_id=source.source_id)
    row.name = source.name
    row.dataset_type = source.dataset_type.value
    row.retrieved_at = source.retrieved_at.isoformat()
    row.license = source.license
    row.license_url = source.license_url
    row.source_url = source.source_url
    row.publisher = source.publisher
    row.usage_terms = source.usage_terms
    row.notes = source.notes
    row.is_synthetic = 1 if source.is_synthetic else 0
    row.external_dataset = 1 if source.external_dataset else 0
    return row


def row_to_source(row: DatasetSourceRow) -> DatasetSource:
    return DatasetSource(
        source_id=row.source_id,
        name=row.name,
        dataset_type=DatasetType(row.dataset_type),
        retrieved_at=parse_date(row.retrieved_at) or date.today(),
        license=row.license,
        license_url=row.license_url,
        source_url=row.source_url,
        publisher=row.publisher,
        usage_terms=row.usage_terms,
        notes=row.notes,
        external_dataset=bool(row.external_dataset),
    )


# --- documents ---------------------------------------------------------------


def document_to_row(document: DocumentRecord, row: DocumentRow | None = None) -> DocumentRow:
    row = row or DocumentRow(document_id=document.document_id)
    row.course_id = document.course_id
    row.source_id = document.source_id
    row.kind = document.kind.value
    row.format = document.format.value
    row.title = document.title
    row.original_filename = document.original_filename
    row.relative_path = document.relative_path
    row.content_sha256 = document.content_sha256
    row.text_fingerprint = document.text_fingerprint
    row.byte_size = document.byte_size
    row.page_count = document.page_count
    row.char_count = document.char_count
    row.extraction_status = document.extraction_status.value
    row.quality_score = document.quality_score
    row.quality_flags_json = dumps(document.quality_flags)
    row.language = document.language
    row.academic_year = document.academic_year
    row.publication_date = iso(document.publication_date)
    row.ingested_at = iso(document.ingested_at) or ""
    row.pipeline_version = document.pipeline_version
    row.metadata_json = dumps(document.metadata)
    row.warnings_json = dumps(document.warnings)
    return row


def row_to_document(row: DocumentRow) -> DocumentRecord:
    return DocumentRecord(
        document_id=row.document_id,
        course_id=row.course_id,
        source_id=row.source_id,
        kind=DocumentKind(row.kind),
        format=SourceFormat(row.format),
        title=row.title,
        original_filename=row.original_filename,
        relative_path=row.relative_path,
        content_sha256=row.content_sha256,
        text_fingerprint=row.text_fingerprint,
        byte_size=row.byte_size,
        page_count=row.page_count,
        char_count=row.char_count,
        extraction_status=ExtractionStatus(row.extraction_status),
        quality_score=row.quality_score,
        quality_flags=loads(row.quality_flags_json, []),
        language=row.language,
        academic_year=row.academic_year,
        publication_date=parse_date(row.publication_date),
        ingested_at=parse_dt(row.ingested_at) or datetime.now().astimezone(),
        pipeline_version=row.pipeline_version,
        metadata=loads(row.metadata_json, {}),
        warnings=loads(row.warnings_json, []),
    )


def page_to_row(document_id: str, page: PageRecord) -> PageRow:
    return PageRow(
        document_id=document_id,
        page_number=page.page_number,
        text=page.text,
        char_count=page.char_count,
        method=page.method.value,
        has_images=1 if page.has_images else 0,
        ocr_attempted=1 if page.ocr_attempted else 0,
    )


def row_to_page(row: PageRow) -> PageRecord:
    return PageRecord(
        page_number=row.page_number,
        text=row.text,
        method=ExtractionMethod(row.method),
        has_images=bool(row.has_images),
        ocr_attempted=bool(row.ocr_attempted),
    )


# --- questions ---------------------------------------------------------------


def question_to_row(question: QuestionRecord) -> QuestionRow:
    return QuestionRow(
        question_id=question.question_id,
        document_id=question.document_id,
        course_id=question.course_id,
        question_number=question.question_number,
        number_sort=question.number_sort,
        section=question.section,
        unit=question.unit,
        text=question.text,
        marks=question.marks,
        marks_source=question.marks_source,
        page_number=question.page_number,
        source_locator=question.source_locator,
        confidence=question.confidence,
        needs_review=1 if question.needs_review else 0,
        fingerprint=question.fingerprint,
        duplicate_of=question.duplicate_of,
        uncertainties_json=dumps([u.model_dump(mode="json") for u in question.uncertainties]),
        metadata_json=dumps(question.metadata),
    )


def row_to_question(row: QuestionRow) -> QuestionRecord:
    uncertainties = [Uncertainty.model_validate(item) for item in loads(row.uncertainties_json, [])]
    return QuestionRecord(
        question_id=row.question_id,
        document_id=row.document_id,
        course_id=row.course_id,
        question_number=row.question_number,
        number_sort=row.number_sort,
        section=row.section,
        unit=row.unit,
        text=row.text,
        marks=row.marks,
        marks_source=row.marks_source,
        page_number=row.page_number,
        source_locator=row.source_locator,
        confidence=row.confidence,
        needs_review=bool(row.needs_review),
        uncertainties=uncertainties,
        fingerprint=row.fingerprint,
        duplicate_of=row.duplicate_of,
        metadata=loads(row.metadata_json, {}),
    )


# --- syllabi -----------------------------------------------------------------


def syllabus_to_row(syllabus: SyllabusRecord) -> SyllabusRow:
    return SyllabusRow(
        syllabus_id=syllabus.syllabus_id,
        course_id=syllabus.course_id,
        document_id=syllabus.document_id,
        source_id=syllabus.source_id,
        title=syllabus.title,
        confidence=syllabus.confidence,
        needs_review=1 if syllabus.needs_review else 0,
        uncertainties_json=dumps([u.model_dump(mode="json") for u in syllabus.uncertainties]),
        ingested_at=iso(syllabus.ingested_at) or "",
    )


def unit_to_row(unit: SyllabusUnitRecord, syllabus_id: str) -> SyllabusUnitRow:
    return SyllabusUnitRow(
        unit_id=unit.unit_id,
        syllabus_id=syllabus_id,
        unit_label=unit.unit_label,
        unit_number=unit.unit_number,
        title=unit.title,
        hours=unit.hours,
        topics_json=dumps(unit.topics),
        raw_text=unit.raw_text,
        page_number=unit.page_number,
        confidence=unit.confidence,
        uncertainties_json=dumps([u.model_dump(mode="json") for u in unit.uncertainties]),
        metadata_json=dumps(unit.metadata),
    )


def row_to_unit(row: SyllabusUnitRow) -> SyllabusUnitRecord:
    return SyllabusUnitRecord(
        unit_id=row.unit_id,
        unit_label=row.unit_label,
        unit_number=row.unit_number,
        title=row.title,
        hours=row.hours,
        topics=loads(row.topics_json, []),
        raw_text=row.raw_text,
        page_number=row.page_number,
        confidence=row.confidence,
        uncertainties=[
            Uncertainty.model_validate(item) for item in loads(row.uncertainties_json, [])
        ],
        metadata=loads(row.metadata_json, {}),
    )


def row_to_syllabus(
    row: SyllabusRow, units: list[SyllabusUnitRecord] | None = None
) -> SyllabusRecord:
    return SyllabusRecord(
        syllabus_id=row.syllabus_id,
        course_id=row.course_id,
        document_id=row.document_id,
        source_id=row.source_id,
        title=row.title,
        units=units or [],
        confidence=row.confidence,
        needs_review=bool(row.needs_review),
        uncertainties=[
            Uncertainty.model_validate(item) for item in loads(row.uncertainties_json, [])
        ],
        ingested_at=parse_dt(row.ingested_at) or datetime.now().astimezone(),
    )
