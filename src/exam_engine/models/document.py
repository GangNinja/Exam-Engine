"""Documents and extracted pages."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    DocumentKind,
    ExtractionMethod,
    ExtractionStatus,
    SourceFormat,
)

_SHA256_RE = r"^[a-f0-9]{64}$"


class PageRecord(BaseModel):
    """Extracted text for one page (or one logical block) of a document."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(..., ge=1)
    text: str = ""
    method: ExtractionMethod = ExtractionMethod.EMBEDDED_TEXT
    has_images: bool = False
    ocr_attempted: bool = False

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class DocumentRecord(BaseModel):
    """A source document with provenance and extraction state."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    course_id: str
    source_id: str
    kind: DocumentKind
    format: SourceFormat
    title: str
    original_filename: str
    content_sha256: str = Field(..., pattern=_SHA256_RE)
    text_fingerprint: str = Field(
        default="",
        description="sha256 of normalised text; catches cosmetic duplicates.",
    )
    byte_size: int = Field(..., ge=0)
    page_count: int = Field(default=0, ge=0)
    char_count: int = Field(default=0, ge=0)
    extraction_status: ExtractionStatus = ExtractionStatus.OK
    quality_score: float = Field(default=1.0, ge=0.0, le=1.0)
    quality_flags: list[str] = Field(default_factory=list)
    language: str | None = None
    academic_year: str | None = None
    publication_date: date | None = None
    relative_path: str | None = Field(
        default=None, description="Path relative to the dataset root, never absolute."
    )
    ingested_at: datetime = Field(default_factory=utcnow)
    pipeline_version: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("content_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("content_sha256 must be a lowercase hex sha256 digest")
        return value


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_document_id(course_id: str, content_sha256: str) -> str:
    """Deterministic id: the same bytes in the same course yield the same id."""
    digest = hashlib.sha256(f"{course_id}\x1f{content_sha256}".encode()).hexdigest()
    return f"doc_{digest[:24]}"
