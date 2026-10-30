"""Validated data models for Phase 1 ingestion."""

from exam_engine.models.course import CourseRecord, utcnow
from exam_engine.models.document import (
    DocumentRecord,
    PageRecord,
    content_digest,
    make_document_id,
)
from exam_engine.models.enums import (
    ERROR_CODES,
    DatasetType,
    DocumentKind,
    DuplicatePolicy,
    ExtractionMethod,
    ExtractionStatus,
    FileVerdict,
    IngestionStatus,
    SegmentationStatus,
    Severity,
    SourceFormat,
)
from exam_engine.models.ingestion import BatchIngestionResult, IngestionError, IngestionResult
from exam_engine.models.provenance import DatasetSource
from exam_engine.models.question import (
    QuestionRecord,
    Uncertainty,
    make_question_id,
    question_fingerprint,
)
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)

__all__ = [
    "ERROR_CODES",
    "BatchIngestionResult",
    "CourseRecord",
    "DatasetSource",
    "DatasetType",
    "DocumentKind",
    "DocumentRecord",
    "DuplicatePolicy",
    "ExtractionMethod",
    "ExtractionStatus",
    "FileVerdict",
    "IngestionError",
    "IngestionResult",
    "IngestionStatus",
    "PageRecord",
    "QuestionRecord",
    "SegmentationStatus",
    "Severity",
    "SourceFormat",
    "SyllabusRecord",
    "SyllabusUnitRecord",
    "Uncertainty",
    "content_digest",
    "make_document_id",
    "make_question_id",
    "make_syllabus_id",
    "make_unit_id",
    "question_fingerprint",
    "utcnow",
]
