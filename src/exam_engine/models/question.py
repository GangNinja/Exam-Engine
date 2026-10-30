"""Question records and extraction uncertainty."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from exam_engine.models.enums import Severity


class Uncertainty(BaseModel):
    """A named, inspectable doubt about an extracted value.

    Uncertainties are data, not log noise: downstream phases can filter on them
    instead of silently trusting a low-confidence extraction.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    severity: Severity = Severity.WARNING
    detail: dict[str, Any] = Field(default_factory=dict)


class QuestionRecord(BaseModel):
    """One question extracted from a paper or question bank.

    ``marks`` is ``None`` when the source does not state marks; it is never
    defaulted to zero, and no answer text is invented.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    course_id: str
    text: str = Field(..., min_length=1)
    question_number: str | None = None
    number_sort: int | None = Field(default=None, ge=0)
    section: str | None = None
    unit: str | None = Field(
        default=None, description="Syllabus unit/chapter, when the source states it."
    )
    marks: float | None = Field(default=None, ge=0.0, le=100.0)
    marks_source: str | None = Field(
        default=None, description="How marks were found: explicit, labelled or bare_bracket."
    )
    page_number: int | None = Field(default=None, ge=1)
    source_locator: str | None = Field(
        default=None, description="Human readable pointer such as 'page 2' or 'row 14'."
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    needs_review: bool = False
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    fingerprint: str = ""
    duplicate_of: str | None = Field(
        default=None, description="question_id of an earlier identical question in the course."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_fingerprint(self) -> QuestionRecord:
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", question_fingerprint(self.text))
        return self

    @property
    def has_uncertainties(self) -> bool:
        return bool(self.uncertainties)


def question_fingerprint(text: str) -> str:
    """Fingerprint used for duplicate detection (never for display)."""
    from exam_engine.ingest.normalize import fingerprint_text

    return fingerprint_text(text)[:32]


def make_question_id(document_id: str, question_number: str | None, text: str) -> str:
    """Deterministic id so repeat imports map to the same row."""
    payload = f"{document_id}\x1f{question_number or ''}\x1f{question_fingerprint(text)}"
    return "q_" + hashlib.sha256(payload.encode()).hexdigest()[:24]
