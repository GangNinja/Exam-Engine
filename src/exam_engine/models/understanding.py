"""Validated records produced by the Phase 3 question-understanding layer.

The same separation as Phase 2 applies: services produce these records, storage
persists them, and the CLI reads them. A classifier can be replaced without
changing the table shapes, and every inferred label keeps its evidence and
version so later phases can audit it.

Nothing here is an empirically validated educational measurement: mapping,
type, Bloom level and difficulty are estimates with explicit confidence and
uncertainty. ``UNKNOWN``/``UNMAPPED`` are valid outcomes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    MappingStatus,
    QuestionType,
    ReferenceKind,
    SimilarityKind,
    UnderstandingStatus,
)
from exam_engine.models.question import Uncertainty


def _digest(payload: str, prefix: str) -> str:
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# --- structure ---------------------------------------------------------------


class Subpart(BaseModel):
    """One detected sub-question inside a stored question's text."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    marks: float | None = Field(default=None, ge=0.0, le=100.0)


class AssetReference(BaseModel):
    """A diagram/code/table/equation/case study the question points to."""

    model_config = ConfigDict(extra="forbid")

    kind: ReferenceKind
    text: str = Field(..., min_length=1)
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# --- mapping and classifications --------------------------------------------


class UnitMatch(BaseModel):
    """One ranked syllabus-unit/topic candidate for a question."""

    model_config = ConfigDict(extra="forbid")

    match_id: str
    question_id: str
    course_id: str
    unit_id: str
    syllabus_id: str
    unit_label: str
    unit_title: str | None = None
    topic: str | None = None
    score: float = Field(..., ge=0.0, le=1.0)
    rank: int = Field(..., ge=1)
    method: str
    matched_terms: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class TypeLabel(BaseModel):
    """A question-type label; questions may carry several."""

    model_config = ConfigDict(extra="forbid")

    type_id: str
    question_id: str
    course_id: str
    label: QuestionType
    score: float = Field(..., ge=0.0, le=1.0)
    rank: int = Field(..., ge=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    method: str
    evidence: list[str] = Field(default_factory=list)


class BloomLabel(BaseModel):
    """A candidate Bloom level; the top-ranked one is the primary label."""

    model_config = ConfigDict(extra="forbid")

    bloom_id: str
    question_id: str
    course_id: str
    level: BloomLevel
    score: float = Field(..., ge=0.0, le=1.0)
    rank: int = Field(..., ge=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    method: str
    evidence: list[str] = Field(default_factory=list)


class DifficultyAssessment(BaseModel):
    """Provisional difficulty estimate with the observable features behind it."""

    model_config = ConfigDict(extra="forbid")

    difficulty_id: str
    question_id: str
    course_id: str
    label: DifficultyLabel
    score: float = Field(..., ge=0.0, le=1.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    method: str
    estimated: bool = Field(
        default=True, description="Always true in Phase 3; no empirical calibration exists."
    )
    features: dict[str, float] = Field(default_factory=dict)
    explanation: list[str] = Field(default_factory=list)


# --- per-question summary ----------------------------------------------------


class QuestionAnalysis(BaseModel):
    """One row per analysed question, joining structure and primary labels."""

    model_config = ConfigDict(extra="forbid")

    analysis_id: str
    question_id: str
    course_id: str
    document_id: str
    text_sha256: str = Field(..., min_length=64, max_length=64)
    word_count: int = Field(default=0, ge=0)
    sentence_count: int = Field(default=0, ge=0)
    subquestion_count: int = Field(default=0, ge=0)
    subparts: list[Subpart] = Field(default_factory=list)
    references: list[AssetReference] = Field(default_factory=list)
    mapping_status: MappingStatus = MappingStatus.UNMAPPED
    mapping_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    top_unit_id: str | None = None
    primary_type: QuestionType | None = None
    primary_type_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    difficulty_label: DifficultyLabel = DifficultyLabel.UNKNOWN
    difficulty_score: float = Field(default=0.0, ge=0.0, le=1.0)
    difficulty_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    bloom_primary: BloomLevel | None = None
    bloom_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    structure_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# --- similarity --------------------------------------------------------------


class SimilarityMatch(BaseModel):
    """A ranked relationship between two questions in the same course."""

    model_config = ConfigDict(extra="forbid")

    pair_id: str
    course_id: str
    question_id: str
    matched_question_id: str
    kind: SimilarityKind
    score: float = Field(..., ge=0.0, le=1.0)
    lexical_score: float = Field(default=0.0, ge=0.0, le=1.0)
    semantic_score: float = Field(default=0.0, ge=0.0, le=1.0)
    method: str
    evidence: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# --- runs --------------------------------------------------------------------


class UnderstandingRunReport(BaseModel):
    """Accounting for one Phase 3 run, including failures."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    course_id: str | None = None
    status: UnderstandingStatus = UnderstandingStatus.OK
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    methods: list[str] = Field(default_factory=list)
    model_ids: list[str] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Uncertainty] = Field(default_factory=list)
    errors: list[Uncertainty] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)

    @property
    def ok(self) -> bool:
        return self.status is not UnderstandingStatus.FAILED


# --- deterministic ids -------------------------------------------------------


def make_analysis_id(course_id: str, question_id: str) -> str:
    return _digest(f"{course_id}\x1f{question_id}", "qa_")


def make_unit_match_id(
    course_id: str, question_id: str, unit_id: str, topic: str | None, method: str
) -> str:
    payload = f"{course_id}\x1f{question_id}\x1f{unit_id}\x1f{topic or ''}\x1f{method}"
    return _digest(payload, "qum_")


def make_type_id(course_id: str, question_id: str, label: QuestionType, method: str) -> str:
    return _digest(f"{course_id}\x1f{question_id}\x1f{label.value}\x1f{method}", "qty_")


def make_bloom_id(course_id: str, question_id: str, level: BloomLevel, method: str) -> str:
    return _digest(f"{course_id}\x1f{question_id}\x1f{level.value}\x1f{method}", "qbl_")


def make_difficulty_id(course_id: str, question_id: str, method: str) -> str:
    return _digest(f"{course_id}\x1f{question_id}\x1f{method}", "qdf_")


def ordered_pair(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first <= second else (second, first)


def make_similarity_id(course_id: str, first: str, second: str, kind: SimilarityKind) -> str:
    left, right = ordered_pair(first, second)
    return _digest(f"{course_id}\x1f{left}\x1f{right}\x1f{kind.value}", "qsm_")


__all__ = [
    "AssetReference",
    "BloomLabel",
    "DifficultyAssessment",
    "QuestionAnalysis",
    "SimilarityMatch",
    "Subpart",
    "TypeLabel",
    "UnderstandingRunReport",
    "UnitMatch",
    "make_analysis_id",
    "make_bloom_id",
    "make_difficulty_id",
    "make_similarity_id",
    "make_type_id",
    "make_unit_match_id",
    "ordered_pair",
]
