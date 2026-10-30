"""Dataset manifest: the repeatable entry point for every import.

A manifest is plain JSON so public datasets, department exports and the reserved
Phase 13 personal uploads can all use one workflow.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from exam_engine.models.course import CourseRecord
from exam_engine.models.enums import DocumentKind, SourceFormat
from exam_engine.models.provenance import DatasetSource


class ManifestError(Exception):
    """Raised when a manifest cannot be read or validated."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1, description="Path relative to the manifest file.")
    kind: DocumentKind
    format: SourceFormat | None = Field(
        default=None, description="Optional declared format; the real bytes win."
    )
    title: str | None = None
    academic_year: str | None = None
    publication_date: date | None = None
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _clean_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("document path must not be empty")
        return cleaned


class ManifestCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str
    course_code: str
    title: str
    institution_code: str
    institution_name: str
    department: str | None = None
    programme: str | None = None
    academic_year: str | None = None
    syllabus_version: str | None = None
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    documents: list[ManifestDocument] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique_paths(self) -> ManifestCourse:
        seen: set[str] = set()
        for document in self.documents:
            key = PurePosixPath(document.path.replace("\\", "/")).as_posix().lower()
            if key in seen:
                raise ValueError(
                    f"course '{self.course_id}' lists document '{document.path}' twice"
                )
            seen.add(key)
        return self

    def to_record(self) -> CourseRecord:
        return CourseRecord(
            course_id=self.course_id,
            course_code=self.course_code,
            title=self.title,
            institution_code=self.institution_code,
            institution_name=self.institution_name,
            department=self.department,
            programme=self.programme,
            academic_year=self.academic_year,
            syllabus_version=self.syllabus_version,
            description=self.description,
            metadata=self.metadata,
        )


class IngestionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    dataset: DatasetSource
    courses: list[ManifestCourse] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique_courses(self) -> IngestionManifest:
        seen: set[str] = set()
        for course in self.courses:
            if course.course_id in seen:
                raise ValueError(f"duplicate course_id '{course.course_id}' in manifest")
            seen.add(course.course_id)
        return self

    @property
    def document_count(self) -> int:
        return sum(len(course.documents) for course in self.courses)


def load_manifest(path: Path | str) -> tuple[IngestionManifest, Path]:
    """Load and validate a manifest; returns it plus its base directory."""
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ManifestError("manifest_document_missing", f"manifest not found: {manifest_path}")
    if not manifest_path.is_file():
        raise ManifestError("invalid_manifest", f"manifest is not a file: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(
            "invalid_manifest",
            f"manifest is not valid JSON: {exc}",
            {"line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise ManifestError("unreadable_file", f"could not read manifest: {exc}") from exc

    try:
        manifest = IngestionManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(
            "invalid_manifest",
            "manifest failed validation",
            {"errors": exc.errors(include_url=False)[:20]},
        ) from exc

    return manifest, manifest_path.parent
