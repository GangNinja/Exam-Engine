"""Ingestion layer: adapters, segmentation, quality checks and the pipeline."""

from exam_engine.ingest.base import (
    ExtractedDocument,
    ExtractedPage,
    ExtractedQuestion,
    ExtractionContext,
    UnsupportedFormatError,
    adapter_for_path,
    get_adapter_for_suffix,
    register_adapter,
    registered_suffixes,
)
from exam_engine.ingest.manifest import (
    IngestionManifest,
    ManifestCourse,
    ManifestDocument,
    ManifestError,
    load_manifest,
)
from exam_engine.ingest.ocr import (
    NullOCRProvider,
    OCRProvider,
    OCRUnavailableError,
    get_ocr_provider,
    register_ocr_provider,
)
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.ingest.quality import QualityReport, assess_pages, mark_question_duplicates
from exam_engine.ingest.segmentation import (
    QuestionSegmentation,
    extract_marks,
    segment_questions_pages,
    segment_questions_text,
)
from exam_engine.ingest.syllabus_segmenter import (
    ExtractedUnit,
    SyllabusSegmentation,
    segment_syllabus,
)
from exam_engine.ingest.validation import (
    FileValidationError,
    resolve_source_path,
    validate_source_file,
)

__all__ = [
    "ExtractedDocument",
    "ExtractedPage",
    "ExtractedQuestion",
    "ExtractedUnit",
    "ExtractionContext",
    "FileValidationError",
    "IngestionManifest",
    "IngestionPipeline",
    "ManifestCourse",
    "ManifestDocument",
    "ManifestError",
    "NullOCRProvider",
    "OCRProvider",
    "OCRUnavailableError",
    "QualityReport",
    "QuestionSegmentation",
    "SyllabusSegmentation",
    "UnsupportedFormatError",
    "adapter_for_path",
    "assess_pages",
    "extract_marks",
    "get_adapter_for_suffix",
    "get_ocr_provider",
    "load_manifest",
    "mark_question_duplicates",
    "register_adapter",
    "register_ocr_provider",
    "registered_suffixes",
    "resolve_source_path",
    "segment_questions_pages",
    "segment_questions_text",
    "segment_syllabus",
    "validate_source_file",
]
