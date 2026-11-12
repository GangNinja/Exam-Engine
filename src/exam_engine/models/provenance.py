"""Provenance metadata for every dataset that enters the platform.

Two levels are modelled:

* :class:`DatasetSource` describes an entire supplied dataset and its licence.
* :class:`ProvenanceRecord` describes one document, paper, question or answer
  and keeps every unproven field explicitly unknown.

A provenance record never guesses official identity. Fields that were not
observed at import time stay ``None``, ``unknown`` or ``unresolved``, and the
record only becomes promotable when paper identity and final-key identity are
both confirmed by retained evidence.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from exam_engine.models.enums import DatasetType, ExtractionMethod

SLUG_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,80}$"
_slug_re = re.compile(SLUG_PATTERN)


class DatasetSource(BaseModel):
    """Where a document came from, and under what terms it may be used.

    ``is_synthetic`` is derived from :attr:`dataset_type` so a synthetic set can
    never be silently relabelled as an official paper.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(..., description="Stable slug identifying this dataset.")
    name: str = Field(..., min_length=1, description="Human readable dataset name.")
    dataset_type: DatasetType
    retrieved_at: date = Field(..., description="Date this dataset was obtained.")
    license: str | None = Field(default=None, description="SPDX id or licence name.")
    license_url: str | None = None
    source_url: str | None = None
    publisher: str | None = None
    usage_terms: str | None = Field(
        default=None, description="Free-text usage terms when no formal licence exists."
    )
    notes: str | None = None
    external_dataset: bool = Field(
        default=False,
        description="True when the data originates outside this repository.",
    )

    @field_validator("source_id")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        if not _slug_re.match(value):
            raise ValueError("source_id must be a lowercase slug matching " + SLUG_PATTERN)
        return value

    @model_validator(mode="after")
    def _validate_terms(self) -> DatasetSource:
        if self.dataset_type is DatasetType.OPENLY_LICENSED and not self.license:
            raise ValueError(
                "openly_licensed datasets must record a licence in 'license' "
                "(public accessibility is not the same as a reuse licence)"
            )
        if self.dataset_type is DatasetType.SYNTHETIC and not (self.license or self.usage_terms):
            raise ValueError(
                "synthetic datasets must record their licence or usage terms "
                "so they cannot be mistaken for official material"
            )
        return self

    @property
    def is_synthetic(self) -> bool:
        return self.dataset_type is DatasetType.SYNTHETIC

    def provenance_warnings(self) -> list[str]:
        """Non-fatal gaps in provenance that callers surface as warnings."""
        warnings: list[str] = []
        if not self.license and not self.usage_terms:
            warnings.append(f"dataset '{self.source_id}' records no licence or usage terms")
        if self.dataset_type is DatasetType.OFFICIAL and self.external_dataset:
            warnings.append(
                f"dataset '{self.source_id}' is marked official but sourced externally; "
                "confirm the paper actually belongs to the stated course and year"
            )
        if self.dataset_type is DatasetType.SYNTHETIC:
            warnings.append(
                f"dataset '{self.source_id}' is synthetic: extracted questions are "
                "development fixtures, not official exam papers"
            )
        return warnings


# --- record-level provenance -------------------------------------------------


class ProvenanceScope(StrEnum):
    """What one provenance row describes."""

    DOCUMENT = "document"
    PAPER = "paper"
    QUESTION = "question"
    ANSWER = "answer"


class SourceClassification(StrEnum):
    """Who published the material a record describes."""

    OFFICIAL = "official"
    THIRD_PARTY = "third_party"
    COMPILED = "compiled"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class IdentityStatus(StrEnum):
    """How far identity has been established from retained evidence."""

    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def make_provenance_id(
    scope: ProvenanceScope,
    *,
    document_id: str | None = None,
    paper_id: str | None = None,
    question_id: str | None = None,
    answer_id: str | None = None,
) -> str:
    """Deterministic id for one scope/target pair, so re-imports are idempotent."""
    parts = [
        scope.value,
        document_id or "",
        paper_id or "",
        question_id or "",
        answer_id or "",
    ]
    return "prv_" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


class ProvenanceRecord(BaseModel):
    """Evidence-bound provenance for one document, paper, question or answer.

    ``paper_identity_status`` and ``key_identity_status`` are separate on
    purpose: an official final key does not establish the paper a local row
    came from, and a paper does not establish that a key entry belongs to it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance_id: str
    scope: ProvenanceScope
    course_id: str = Field(..., min_length=1)
    document_id: str | None = None
    paper_id: str | None = None
    question_id: str | None = None
    answer_id: str | None = None

    source_id: str | None = None
    source_url: str | None = None
    source_publisher: str | None = None
    retrieved_at: date | None = None
    source_document_sha256: str | None = None
    examining_authority: str | None = None
    examination_name: str | None = None
    exam_year: int | None = None
    exam_session: str | None = None
    subject_code: str | None = None
    paper_code: str | None = None
    set_code: str | None = None
    region: str | None = None
    language: str | None = None
    original_question_id: str | None = None
    original_option_ids: tuple[str, ...] = ()
    original_question_number: str | None = None
    extraction_method: ExtractionMethod | None = None
    extraction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_classification: SourceClassification = SourceClassification.UNKNOWN
    identity_status: IdentityStatus = IdentityStatus.UNRESOLVED
    paper_identity_status: IdentityStatus = IdentityStatus.UNRESOLVED
    key_identity_status: IdentityStatus = IdentityStatus.NOT_APPLICABLE
    notes: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("source_url")
    @classmethod
    def _validate_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("https://", "http://")):
            raise ValueError("source_url must be an absolute http(s) URL")
        return value

    @field_validator("source_document_sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SHA256_RE.match(value):
            raise ValueError("source_document_sha256 must be a lowercase 64-character digest")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> ProvenanceRecord:
        expected = make_provenance_id(
            self.scope,
            document_id=self.document_id,
            paper_id=self.paper_id,
            question_id=self.question_id,
            answer_id=self.answer_id,
        )
        if self.provenance_id != expected:
            raise ValueError(
                f"provenance_id does not match its scope/targets (expected {expected})"
            )
        required = {
            ProvenanceScope.DOCUMENT: (self.document_id,),
            ProvenanceScope.PAPER: (self.document_id, self.paper_id),
            ProvenanceScope.QUESTION: (self.document_id, self.paper_id, self.question_id),
            ProvenanceScope.ANSWER: (
                self.document_id,
                self.paper_id,
                self.question_id,
                self.answer_id,
            ),
        }[self.scope]
        if any(value is None for value in required):
            raise ValueError(f"scope '{self.scope.value}' requires {required}")
        if (
            self.scope is not ProvenanceScope.ANSWER
            and self.key_identity_status is not IdentityStatus.NOT_APPLICABLE
        ):
            raise ValueError("key_identity_status applies only to answer provenance")
        if self.source_classification is SourceClassification.OFFICIAL:
            missing = [
                name
                for name, value in (
                    ("source_url", self.source_url),
                    ("examining_authority", self.examining_authority),
                    ("retrieved_at", self.retrieved_at),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "official provenance requires retained evidence: " + ", ".join(missing)
                )
        if self.identity_status is IdentityStatus.CONFIRMED and not self.source_document_sha256:
            raise ValueError("confirmed identity requires the source document sha256")
        if self.paper_identity_status is IdentityStatus.CONFIRMED:
            missing = [
                name
                for name, value in (
                    ("examining_authority", self.examining_authority),
                    ("exam_year", self.exam_year),
                    ("paper_code or set_code", self.paper_code or self.set_code),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "confirmed paper identity requires recorded evidence: " + ", ".join(missing)
                )
        if self.key_identity_status is IdentityStatus.CONFIRMED:
            missing = [
                name
                for name in ("answer_key_source_url", "answer_key_sha256")
                if not self.evidence.get(name)
            ]
            if missing:
                raise ValueError(
                    "confirmed final-key identity requires retained key evidence: "
                    + ", ".join(missing)
                )
        return self

    @property
    def is_officially_promotable(self) -> bool:
        """True only when paper identity *and* final-key identity are confirmed."""
        return (
            self.scope is ProvenanceScope.ANSWER
            and self.source_classification is SourceClassification.OFFICIAL
            and self.identity_status is IdentityStatus.CONFIRMED
            and self.paper_identity_status is IdentityStatus.CONFIRMED
            and self.key_identity_status is IdentityStatus.CONFIRMED
        )

    @classmethod
    def build(
        cls,
        *,
        scope: ProvenanceScope,
        course_id: str,
        created_at: datetime,
        **fields: Any,
    ) -> ProvenanceRecord:
        """Construct a record with the deterministic id for its targets."""
        provenance_id = make_provenance_id(
            scope,
            document_id=fields.get("document_id"),
            paper_id=fields.get("paper_id"),
            question_id=fields.get("question_id"),
            answer_id=fields.get("answer_id"),
        )
        return cls(
            provenance_id=provenance_id,
            scope=scope,
            course_id=course_id,
            created_at=created_at,
            **fields,
        )


__all__ = [
    "DatasetSource",
    "IdentityStatus",
    "ProvenanceRecord",
    "ProvenanceScope",
    "SourceClassification",
    "make_provenance_id",
]
