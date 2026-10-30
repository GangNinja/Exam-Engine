"""Intelligent Exam Knowledge & Assessment Engine - Phase 1 ingestion foundation."""

from exam_engine.config import IngestionLimits, Settings
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models import (
    BatchIngestionResult,
    CourseRecord,
    DatasetSource,
    DatasetType,
    DocumentKind,
    DocumentRecord,
    ExtractionStatus,
    IngestionError,
    IngestionResult,
    IngestionStatus,
    PageRecord,
    QuestionRecord,
    SourceFormat,
    SyllabusRecord,
    SyllabusUnitRecord,
    Uncertainty,
)
from exam_engine.storage.db import open_database
from exam_engine.version import PIPELINE_VERSION, __version__

__all__ = [
    "PIPELINE_VERSION",
    "BatchIngestionResult",
    "CourseRecord",
    "DatasetSource",
    "DatasetType",
    "DocumentKind",
    "DocumentRecord",
    "ExtractionStatus",
    "IngestionError",
    "IngestionLimits",
    "IngestionPipeline",
    "IngestionResult",
    "IngestionStatus",
    "PageRecord",
    "QuestionRecord",
    "Settings",
    "SourceFormat",
    "SyllabusRecord",
    "SyllabusUnitRecord",
    "Uncertainty",
    "__version__",
    "open_database",
]
