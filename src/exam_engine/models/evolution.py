"""Validated reports for the Phase 9 concept evolution tracker.

The report describes how concepts appear, are worded, are placed and are assessed
inside the *available* dated documents. It never claims to reconstruct the
complete history of a concept, never treats a missing mention as proof of
removal, and never rewrites a canonical concept from an inferred change.

Like Phases 4, 7 and 8 the report is derived state: Phase 9 adds no table and
writes nothing back to sources, syllabus records, the graph, coverage results or
Exam DNA profiles.
"""

from __future__ import annotations

import hashlib
from datetime import date as Date
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from exam_engine.models.analytics import CourseScope
from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    ConceptChangeCategory,
    DocumentKind,
    EvidenceQuality,
    EvolutionDateType,
    EvolutionReviewStatus,
    EvolutionStatus,
    ExtractionStatus,
)
from exam_engine.models.question import Uncertainty

EVOLUTION_SCHEMA_VERSION = "phase9-v1"
EVOLUTION_ALGORITHM = "phase9_concept_evolution_tracker"
EVOLUTION_ALGORITHM_VERSION = "phase9.v1"


def _digest(payload: str, prefix: str) -> str:
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def make_report_id(payload: str) -> str:
    return _digest(payload, "evo_")


def make_observation_id(payload: str) -> str:
    return _digest(payload, "obs_")


def make_change_id(payload: str) -> str:
    return _digest(payload, "chg_")


class EvolutionFilters(BaseModel):
    """Optional narrowing; it never widens scope to another course."""

    model_config = ConfigDict(extra="forbid")

    concept_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    document_kinds: list[DocumentKind] = Field(default_factory=list)
    syllabus_ids: list[str] = Field(default_factory=list)
    categories: list[ConceptChangeCategory] = Field(default_factory=list)
    date_from: Date | None = None
    date_to: Date | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> EvolutionFilters:
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from must not be after date_to")
        return self

    @property
    def restricts(self) -> bool:
        return bool(
            self.concept_ids
            or self.document_ids
            or self.document_kinds
            or self.syllabus_ids
            or self.categories
            or self.date_from is not None
            or self.date_to is not None
        )


class EvolutionRequest(BaseModel):
    """One course-scoped evolution analysis request."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    filters: EvolutionFilters = Field(default_factory=EvolutionFilters)
    include_assessment: bool = True
    include_exam_dna: bool = True
    include_relationships: bool = True


class EvolutionObservation(BaseModel):
    """One dated appearance of a concept term in one source document."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    concept_id: str
    canonical_label: str
    observed_term: str
    verified_term: bool = False
    document_id: str
    document_title: str
    document_kind: DocumentKind
    date: Date | None = None
    date_type: EvolutionDateType = EvolutionDateType.UNKNOWN
    date_source: str = "unknown"
    date_confident: bool = True
    syllabus_id: str | None = None
    unit_label: str | None = None
    unit_number: int | None = Field(default=None, ge=0)
    excerpt: str = ""
    page_number: int | None = Field(default=None, ge=1)
    question_ids: list[str] = Field(default_factory=list)
    related_concepts: list[str] = Field(default_factory=list)
    mapping_method: str = "unknown"
    mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ConceptTimeline(BaseModel):
    """Chronological observations for one concept inside the analyzed corpus."""

    model_config = ConfigDict(extra="forbid")

    concept_id: str
    canonical_label: str
    aliases: list[str] = Field(default_factory=list)
    first_observed: Date | None = None
    last_observed: Date | None = None
    observation_count: int = Field(default=0, ge=0)
    dated_observation_count: int = Field(default=0, ge=0)
    document_count: int = Field(default=0, ge=0)
    syllabus_ids: list[str] = Field(default_factory=list)
    unit_labels: list[str] = Field(default_factory=list)
    observations: list[EvolutionObservation] = Field(default_factory=list)
    evidence_quality: EvidenceQuality = EvidenceQuality.LOW
    limitations: list[str] = Field(default_factory=list)


class ChangeSide(BaseModel):
    """One side of a change event, with its source and date provenance."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_title: str
    date: Date | None = None
    date_to: Date | None = None
    date_type: EvolutionDateType = EvolutionDateType.UNKNOWN
    period: str | None = None
    term: str | None = None
    unit_label: str | None = None
    excerpt: str = ""
    question_ids: list[str] = Field(default_factory=list)
    syllabus_id: str | None = None


class ConceptChange(BaseModel):
    """A reviewable, evidence-backed change inside the analyzed corpus."""

    model_config = ConfigDict(extra="forbid")

    change_id: str
    category: ConceptChangeCategory
    concept_ids: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    scope: str = "course_and_selected_documents"
    earlier: ChangeSide | None = None
    later: ChangeSide | None = None
    temporal_order_known: bool = False
    method: str = Field(..., min_length=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_quality: EvidenceQuality = EvidenceQuality.LOW
    review_required: bool = True
    review_status: EvolutionReviewStatus = EvolutionReviewStatus.PENDING_REVIEW
    explanation: str = Field(..., min_length=1)
    change_summary: str | None = Field(
        default=None,
        description=(
            "Concise, visible statement of the observed change; review aid, not a "
            "conclusion."
        ),
    )
    evidence: list[str] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SyllabusVersionRef(BaseModel):
    """A syllabus version with the date evidence used to order it."""

    model_config = ConfigDict(extra="forbid")

    syllabus_id: str
    document_id: str
    title: str | None = None
    date: Date | None = None
    date_type: EvolutionDateType = EvolutionDateType.UNKNOWN
    date_source: str = "unknown"
    date_confident: bool = True
    extraction_status: ExtractionStatus = ExtractionStatus.OK
    unit_count: int = Field(default=0, ge=0)
    concept_count: int = Field(default=0, ge=0)


class UnitMove(BaseModel):
    """One concept whose syllabus unit placement differs between versions."""

    model_config = ConfigDict(extra="forbid")

    concept_id: str
    label: str
    earlier_unit: str
    later_unit: str


class DescriptionChange(BaseModel):
    """One unit label whose title or topic list differs between versions."""

    model_config = ConfigDict(extra="forbid")

    unit_label: str
    added_topics: list[str] = Field(default_factory=list)
    removed_topics: list[str] = Field(default_factory=list)
    earlier_title: str | None = None
    later_title: str | None = None


class SyllabusComparison(BaseModel):
    """A review-only comparison of two syllabus versions."""

    model_config = ConfigDict(extra="forbid")

    earlier: SyllabusVersionRef
    later: SyllabusVersionRef
    order_known: bool = False
    shared_concepts: list[str] = Field(default_factory=list)
    introduced: list[str] = Field(default_factory=list)
    no_longer_observed: list[str] = Field(default_factory=list)
    moved_units: list[UnitMove] = Field(default_factory=list)
    description_changes: list[DescriptionChange] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AssessmentWindow(BaseModel):
    """One comparison window over dated, concept-mapped questions."""

    model_config = ConfigDict(extra="forbid")

    label: str
    date_from: Date | None = None
    date_to: Date | None = None
    period_from: str | None = Field(
        default=None, description="Earliest resolved examination-period key, e.g. 2020-05."
    )
    period_to: str | None = None
    questions: int = Field(default=0, ge=0)
    analysed_questions: int = Field(default=0, ge=0)
    type_labelled_questions: int = Field(default=0, ge=0)
    bloom_labelled_questions: int = Field(default=0, ge=0)
    papers: int = Field(default=0, ge=0)
    sessions: int = Field(default=0, ge=0)
    document_ids: list[str] = Field(default_factory=list)
    questions_with_marks: int = Field(default=0, ge=0)
    known_marks: float = Field(default=0.0, ge=0.0)
    type_counts: dict[str, int] = Field(default_factory=dict)
    bloom_counts: dict[str, int] = Field(default_factory=dict)
    marks_counts: dict[str, int] = Field(
        default_factory=dict, description="Stated marks value -> questions with that value."
    )
    unit_counts: dict[str, int] = Field(
        default_factory=dict, description="Top Phase 3 unit label -> questions in the window."
    )
    repetition_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Phase 8 repeated/similar group kind -> distinct window questions in one.",
    )
    type_shares: dict[str, float] = Field(default_factory=dict)
    bloom_shares: dict[str, float] = Field(default_factory=dict)
    marks_shares: dict[str, float] = Field(
        default_factory=dict, description="Share of questions with known marks per value."
    )


class ConceptAssessment(BaseModel):
    """Observed assessment emphasis for one concept in two dated windows.

    Raw counts and normalized shares are kept apart; no single evolution score is
    computed.
    """

    model_config = ConfigDict(extra="forbid")

    concept_id: str
    label: str
    earlier: AssessmentWindow
    later: AssessmentWindow
    question_delta: int = 0
    marks_delta: float = 0.0
    max_type_share_delta: float | None = Field(default=None, ge=0.0, le=1.0)
    max_bloom_share_delta: float | None = Field(default=None, ge=0.0, le=1.0)
    max_marks_share_delta: float | None = Field(default=None, ge=0.0, le=1.0)
    changed: bool = False
    reasons: list[str] = Field(default_factory=list)
    coverage_report_ids: list[str] = Field(default_factory=list)
    exam_dna_profile_id: str | None = None
    limitations: list[str] = Field(default_factory=list)


class DateCandidate(BaseModel):
    """One parsed source date, kept even when it was not selected for ordering."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1)
    date: Date
    date_type: EvolutionDateType


class EvolutionDocument(BaseModel):
    """Per-document date evidence and inclusion summary."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    kind: DocumentKind
    source_id: str
    syllabus_id: str | None = None
    date: Date | None = None
    date_type: EvolutionDateType = EvolutionDateType.UNKNOWN
    date_source: str = "unknown"
    date_confident: bool = True
    date_candidates: list[DateCandidate] = Field(default_factory=list)
    extraction_status: ExtractionStatus = ExtractionStatus.OK
    question_count: int = Field(default=0, ge=0)
    observation_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class EvolutionExclusion(BaseModel):
    """A document excluded from the report with an explicit reason."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    reason: str


class EvolutionScope(BaseModel):
    """The exact evidence boundary copied into the report."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(default_factory=list)
    syllabus_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)
    date_from: Date | None = None
    date_to: Date | None = None
    scope_statement: str = Field(..., min_length=1)


class EvolutionDataQuality(BaseModel):
    """Completeness, comparability and exclusion accounting for one report."""

    model_config = ConfigDict(extra="forbid")

    documents: int = Field(default=0, ge=0)
    dated_documents: int = Field(default=0, ge=0)
    undated_documents: int = Field(default=0, ge=0)
    syllabus_versions: int = Field(default=0, ge=0)
    concepts: int = Field(default=0, ge=0)
    observations: int = Field(default=0, ge=0)
    dated_observations: int = Field(default=0, ge=0)
    conflicting_dates: int = Field(default=0, ge=0)
    invalid_dates: int = Field(default=0, ge=0)
    incomplete_extractions: int = Field(default=0, ge=0)
    low_confidence_observations: int = Field(default=0, ge=0)
    graph_available: bool = False
    coverage_report_ids: list[str] = Field(default_factory=list)
    exam_dna_profile_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    excluded_documents: list[EvolutionExclusion] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)


class EvolutionSummary(BaseModel):
    """Headline counts; every rate keeps its own denominator elsewhere."""

    model_config = ConfigDict(extra="forbid")

    documents: int = Field(default=0, ge=0)
    dated_documents: int = Field(default=0, ge=0)
    concepts: int = Field(default=0, ge=0)
    observations: int = Field(default=0, ge=0)
    dated_observations: int = Field(default=0, ge=0)
    timelines: int = Field(default=0, ge=0)
    changes: int = Field(default=0, ge=0)
    review_required_changes: int = Field(default=0, ge=0)
    insufficient_evidence_changes: int = Field(default=0, ge=0)
    changes_by_category: dict[str, int] = Field(default_factory=dict)
    syllabus_comparisons: int = Field(default=0, ge=0)
    assessment_concepts: int = Field(default=0, ge=0)


class EvolutionReport(BaseModel):
    """One reproducible Phase 9 evolution report for one course."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EVOLUTION_SCHEMA_VERSION
    report_id: str
    algorithm: str = EVOLUTION_ALGORITHM
    algorithm_version: str = EVOLUTION_ALGORITHM_VERSION
    course: CourseScope
    generated_at: datetime = Field(default_factory=utcnow)
    status: EvolutionStatus
    filters: EvolutionFilters = Field(default_factory=EvolutionFilters)
    scope: EvolutionScope
    parameters: dict[str, Any] = Field(default_factory=dict)
    summary: EvolutionSummary
    documents: list[EvolutionDocument] = Field(default_factory=list)
    timelines: list[ConceptTimeline] = Field(default_factory=list)
    changes: list[ConceptChange] = Field(default_factory=list)
    syllabus_comparisons: list[SyllabusComparison] = Field(default_factory=list)
    assessment: list[ConceptAssessment] = Field(default_factory=list)
    data_quality: EvolutionDataQuality
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


__all__ = [
    "EVOLUTION_ALGORITHM",
    "EVOLUTION_ALGORITHM_VERSION",
    "EVOLUTION_SCHEMA_VERSION",
    "AssessmentWindow",
    "ChangeSide",
    "DateCandidate",
    "ConceptAssessment",
    "ConceptChange",
    "ConceptTimeline",
    "DescriptionChange",
    "EvolutionDataQuality",
    "EvolutionDocument",
    "EvolutionExclusion",
    "EvolutionFilters",
    "EvolutionObservation",
    "EvolutionReport",
    "EvolutionRequest",
    "EvolutionScope",
    "EvolutionSummary",
    "SyllabusComparison",
    "SyllabusVersionRef",
    "UnitMove",
    "make_change_id",
    "make_observation_id",
    "make_report_id",
]
