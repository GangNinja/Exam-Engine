"""Syllabus records and unit/topic segmentation."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from exam_engine.models.course import utcnow
from exam_engine.models.question import Uncertainty


class SyllabusUnitRecord(BaseModel):
    """One unit/module/chapter of a syllabus."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_label: str = Field(..., min_length=1, description="e.g. 'Unit 1'.")
    unit_number: int | None = Field(default=None, ge=0)
    title: str | None = None
    hours: float | None = Field(
        default=None, ge=0.0, le=500.0, description="Teaching hours, only when stated."
    )
    topics: list[str] = Field(default_factory=list)
    raw_text: str = ""
    page_number: int | None = Field(default=None, ge=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SyllabusRecord(BaseModel):
    """A parsed syllabus attached to exactly one course document."""

    model_config = ConfigDict(extra="forbid")

    syllabus_id: str
    course_id: str
    document_id: str
    source_id: str
    title: str | None = None
    units: list[SyllabusUnitRecord] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    ingested_at: datetime = Field(default_factory=utcnow)


def make_syllabus_id(document_id: str) -> str:
    return "syl_" + hashlib.sha256(document_id.encode()).hexdigest()[:24]


def make_unit_id(syllabus_id: str, unit_label: str, raw_text: str) -> str:
    payload = f"{syllabus_id}\x1f{unit_label}\x1f{raw_text[:200]}"
    return "unit_" + hashlib.sha256(payload.encode()).hexdigest()[:24]
