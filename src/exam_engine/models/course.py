"""Course, institution and syllabus-version metadata."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from exam_engine.models.provenance import SLUG_PATTERN

_slug_re = re.compile(SLUG_PATTERN)


def utcnow() -> datetime:
    return datetime.now(UTC)


class CourseRecord(BaseModel):
    """A single taught course offering.

    A course is the isolation boundary for documents, questions and syllabi.
    Re-ingesting the same institution/code/year/version tuple must resolve to the
    same ``course_id`` rather than silently creating a second course.
    """

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(
        ...,
        description="Stable slug, e.g. 'demo-cs201-2024-v1'. Never derived from a filename.",
    )
    course_code: str = Field(..., min_length=1, max_length=32)
    title: str = Field(..., min_length=1)
    institution_code: str = Field(..., min_length=1, max_length=40)
    institution_name: str = Field(..., min_length=1)
    department: str | None = None
    programme: str | None = Field(default=None, description="e.g. B.Tech, B.Sc.")
    academic_year: str | None = Field(default=None, description="e.g. '2024-25'.")
    syllabus_version: str | None = Field(default=None, description="e.g. 'v1', '2023-revised'.")
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("course_id")
    @classmethod
    def _validate_course_id(cls, value: str) -> str:
        if not _slug_re.match(value):
            raise ValueError("course_id must be a lowercase slug matching " + SLUG_PATTERN)
        return value

    @field_validator("course_code")
    @classmethod
    def _normalise_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("institution_code")
    @classmethod
    def _normalise_institution(cls, value: str) -> str:
        # Identifiers are compared case-insensitively when detecting conflicting
        # offerings, so store them in one canonical form.
        return value.strip().lower()

    def offering_key(self) -> tuple[str, str, str | None, str | None]:
        """Identity of the offering, used to detect conflicting duplicates."""
        return (
            self.institution_code.strip().lower(),
            self.course_code.strip().upper(),
            self.academic_year,
            self.syllabus_version,
        )
