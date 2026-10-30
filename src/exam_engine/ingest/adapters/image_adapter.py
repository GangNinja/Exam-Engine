"""Image inputs: never parsed directly, always handed to the OCR interface."""

from __future__ import annotations

from pathlib import Path

from exam_engine.ingest.base import (
    ExtractedDocument,
    ExtractedPage,
    ExtractionContext,
    ingestion_warning,
)
from exam_engine.ingest.validation import FORMAT_BY_SUFFIX, FileValidationError
from exam_engine.models.enums import ExtractionMethod, Severity, SourceFormat


class ImageAdapter:
    name = "image"
    suffixes: tuple[str, ...] = tuple(
        suffix for suffix, fmt in FORMAT_BY_SUFFIX.items() if fmt is SourceFormat.IMAGE
    )

    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument:
        if not path.exists():  # defensive: pipeline validates first
            raise FileValidationError("file_not_found", f"image not found: {path}")
        return ExtractedDocument(
            format=SourceFormat.IMAGE,
            pages=[
                ExtractedPage(
                    page_number=1,
                    text="",
                    method=ExtractionMethod.NONE,
                    has_images=True,
                )
            ],
            metadata={"extractor": "image", "requires_ocr": True},
            warnings=[
                ingestion_warning(
                    "ocr_unavailable",
                    "image input requires OCR to produce text",
                    severity=Severity.INFO,
                )
            ],
            needs_ocr_pages=[1],
        )
