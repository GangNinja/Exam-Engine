"""Ingestion result and error reporting models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from exam_engine.models.course import utcnow
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import IngestionStatus, Severity


class IngestionError(BaseModel):
    """A structured problem found while ingesting one document.

    Warnings use the same shape as errors so callers can render one report.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    severity: Severity = Severity.ERROR
    source_path: str | None = None
    document_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


class IngestionResult(BaseModel):
    """Outcome of ingesting a single source document."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    course_id: str
    status: IngestionStatus
    document: DocumentRecord | None = None
    page_count: int = 0
    question_count: int = 0
    syllabus_unit_count: int = 0
    duplicate_of: str | None = Field(
        default=None, description="document_id of the pre-existing identical document."
    )
    errors: list[IngestionError] = Field(default_factory=list)
    warnings: list[IngestionError] = Field(default_factory=list)
    duration_ms: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return self.status not in (IngestionStatus.FAILED, IngestionStatus.EMPTY)

    def error_codes(self) -> list[str]:
        return [e.code for e in self.errors]


class BatchIngestionResult(BaseModel):
    """Outcome of a manifest/batch import."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    manifest_path: str | None = None
    source_id: str | None = None
    results: list[IngestionResult] = Field(default_factory=list)
    warnings: list[IngestionError] = Field(
        default_factory=list, description="Dataset-level provenance warnings."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def totals(self) -> dict[str, int]:
        return {
            "documents": len(self.results),
            "ingested": sum(
                1
                for r in self.results
                if r.status in (IngestionStatus.INGESTED, IngestionStatus.REPLACED)
            ),
            "skipped_duplicate": sum(
                1 for r in self.results if r.status is IngestionStatus.SKIPPED_DUPLICATE
            ),
            "needs_ocr": sum(1 for r in self.results if r.status is IngestionStatus.NEEDS_OCR),
            "empty": sum(1 for r in self.results if r.status is IngestionStatus.EMPTY),
            "failed": sum(1 for r in self.results if r.status is IngestionStatus.FAILED),
            "questions": sum(r.question_count for r in self.results),
            "syllabus_units": sum(r.syllabus_unit_count for r in self.results),
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return not any(r.status is IngestionStatus.FAILED for r in self.results)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_errors(self) -> list[IngestionError]:
        return [e for r in self.results for e in r.errors]

    def by_course(self) -> dict[str, list[IngestionResult]]:
        grouped: dict[str, list[IngestionResult]] = {}
        for result in self.results:
            grouped.setdefault(result.course_id, []).append(result)
        return grouped

    def duration_ms(self) -> int:
        if self.finished_at is None:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)
