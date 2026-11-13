"""Validated reports for the Phase 7 exam coverage gap detector.

Coverage is a descriptive statement about the *available* question corpus, not a
prediction about future examinations. Every finding keeps its mapping evidence,
source document and uncertainty so a reviewer can see why a concept was classified
as covered, low-coverage, unmapped, uncertain or insufficiently supported.

The report is derived state: Phase 7 adds no coverage tables and never rewrites
sources, questions, Phase 3 mappings or the Phase 5 graph.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from exam_engine.models.analytics import CourseScope
from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    CoverageReportStatus,
    CoverageStatus,
    DocumentKind,
    GraphEdgeStatus,
    MappingStatus,
    QuestionType,
)
from exam_engine.models.question import Uncertainty

COVERAGE_SCHEMA_VERSION = "phase7-v1"
COVERAGE_ALGORITHM = "phase7_coverage_gap_detector"
COVERAGE_ALGORITHM_VERSION = "phase7.v1"


def _digest(payload: str, prefix: str) -> str:
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def make_coverage_concept_id(course_id: str, syllabus_id: str, normalized_label: str) -> str:
    """Stable identity for a normalized syllabus topic within one syllabus."""
    return _digest(f"{course_id}\x1f{syllabus_id}\x1f{normalized_label}", "syc_")


def make_coverage_report_id(payload: str) -> str:
    return _digest(payload, "cov_")


class CoverageFilters(BaseModel):
    """Optional narrowing applied before metrics are calculated.

    Unit filters restrict the syllabus concepts under review. Question filters
    restrict the available question corpus. Filters are echoed into the report;
    nothing excluded by a filter is silently treated as evidence of absence.
    """

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    question_types: list[QuestionType] = Field(default_factory=list)
    exam_periods: list[str] = Field(default_factory=list)

    @field_validator("document_ids", "source_ids", "unit_ids", "exam_periods")
    @classmethod
    def _dedupe_strings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("question_types")
    @classmethod
    def _dedupe_types(cls, value: list[QuestionType]) -> list[QuestionType]:
        return list(dict.fromkeys(value))

    @property
    def restricts(self) -> bool:
        return bool(
            self.document_ids
            or self.source_ids
            or self.unit_ids
            or self.question_types
            or self.exam_periods
        )


class CoverageRequest(BaseModel):
    """Course-scoped request; a syllabus is mandatory when several exist."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    filters: CoverageFilters = Field(default_factory=CoverageFilters)

    @field_validator("syllabus_id")
    @classmethod
    def _blank_syllabus_is_none(cls, value: str | None) -> str | None:
        return value or None


class CoverageSyllabus(BaseModel):
    """The selected syllabus version and its scope."""

    model_config = ConfigDict(extra="forbid")

    syllabus_id: str
    title: str | None = None
    document_id: str
    course_syllabus_version: str | None = None
    unit_count: int = Field(default=0, ge=0)
    concept_count: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    selected_automatically: bool = False


class CoverageQuestionEvidence(BaseModel):
    """An accepted question-to-concept mapping with its source provenance."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    document_title: str
    document_kind: DocumentKind
    source_id: str
    question_number: str | None = None
    section: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    source_locator: str | None = None
    marks: float | None = Field(default=None, ge=0.0)
    text_excerpt: str
    mapping_sources: list[str] = Field(default_factory=list)
    mapping_method: str
    mapping_score: float | None = Field(default=None, ge=0.0, le=1.0)
    graph_edge_ids: list[str] = Field(default_factory=list)
    graph_edge_status: GraphEdgeStatus | None = None
    analysis_status: MappingStatus | None = None
    primary_type: QuestionType | None = None
    matched_terms: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    exam_period: str
    exam_period_source: str
    exam_session: str | None = None
    exam_type: str | None = None


class CoverageMappingCandidate(BaseModel):
    """A rejected/alternate mapping retained to explain uncertainty."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    question_number: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    marks: float | None = Field(default=None, ge=0.0)
    text_excerpt: str
    unit_id: str | None = None
    unit_label: str | None = None
    topic: str | None = None
    syllabus_id: str | None = None
    mapping_source: str
    mapping_method: str
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    rank: int | None = Field(default=None, ge=1)
    analysis_status: MappingStatus | None = None
    matched_terms: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reason: str = Field(..., min_length=1)


class ConceptCoverageFinding(BaseModel):
    """Coverage of one syllabus concept, with its mapping and source evidence."""

    model_config = ConfigDict(extra="forbid")

    concept_id: str
    graph_concept_id: str | None = None
    label: str
    normalized_label: str
    aliases: list[str] = Field(default_factory=list)
    syllabus_id: str
    unit_ids: list[str] = Field(default_factory=list)
    unit_labels: list[str] = Field(default_factory=list)
    source_document_id: str
    source_page: int | None = Field(default=None, ge=1)
    extraction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: CoverageStatus
    distinct_question_count: int = Field(default=0, ge=0)
    mapped_marks: float = Field(default=0.0, ge=0.0)
    questions_with_marks: int = Field(default=0, ge=0)
    document_count: int = Field(default=0, ge=0)
    session_count: int = Field(default=0, ge=0)
    question_type_counts: dict[str, int] = Field(default_factory=dict)
    unknown_question_type_count: int = Field(default=0, ge=0)
    scored_mapping_count: int = Field(default=0, ge=0)
    average_mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    minimum_mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    maximum_mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supporting_questions: list[CoverageQuestionEvidence] = Field(default_factory=list)
    candidate_mappings: list[CoverageMappingCandidate] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class UnitCoverageFinding(BaseModel):
    """Unit-level summary; concept membership may overlap between units."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_label: str
    unit_title: str | None = None
    unit_number: int | None = Field(default=None, ge=0)
    syllabus_id: str
    source_document_id: str
    source_page: int | None = Field(default=None, ge=1)
    concept_count: int = Field(default=0, ge=0)
    concept_ids: list[str] = Field(default_factory=list)
    status_counts: dict[str, int] = Field(default_factory=dict)
    distinct_question_count: int = Field(default=0, ge=0)
    mapped_marks: float = Field(default=0.0, ge=0.0)
    questions_with_marks: int = Field(default=0, ge=0)
    concept_coverage_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    assessed_concept_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    question_share: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Share of the analysed corpus mapped to this unit; units may overlap.",
    )
    mapped_marks_share: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Share of all known marks mapped to this unit; units may overlap.",
    )
    needs_attention: list[str] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)


class UnmappedQuestionFinding(BaseModel):
    """A question without an accepted mapping to any selected-syllabus concept."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    document_title: str
    document_kind: DocumentKind
    source_id: str
    question_number: str | None = None
    section: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    source_locator: str | None = None
    marks: float | None = Field(default=None, ge=0.0)
    text_excerpt: str
    analysis_status: MappingStatus | None = None
    reason: str = Field(..., min_length=1)
    explanation: str = Field(..., min_length=1)
    exam_period: str
    exam_period_source: str
    exam_session: str | None = None
    exam_type: str | None = None
    existing_mappings: list[CoverageMappingCandidate] = Field(default_factory=list)


class CoverageSummary(BaseModel):
    """Corpus totals and concept classifications.

    All question counts are distinct question identifiers. A concept's supporting
    questions can overlap with another concept's; report-level mapped-question
    counts are deduplicated across concepts.
    """

    model_config = ConfigDict(extra="forbid")

    total_concepts: int = Field(default=0, ge=0)
    covered_concepts: int = Field(default=0, ge=0)
    low_coverage_concepts: int = Field(default=0, ge=0)
    unmapped_concepts: int = Field(default=0, ge=0)
    uncertain_concepts: int = Field(default=0, ge=0)
    insufficient_data_concepts: int = Field(default=0, ge=0)
    total_questions: int = Field(default=0, ge=0)
    analysed_questions: int = Field(default=0, ge=0)
    mapped_questions: int = Field(default=0, ge=0)
    unmapped_questions: int = Field(default=0, ge=0)
    ambiguous_questions: int = Field(default=0, ge=0)
    not_analysed_questions: int = Field(default=0, ge=0)
    analysis_coverage_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    mapped_question_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    concept_coverage_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    known_sessions: int = Field(default=0, ge=0)
    total_known_marks: float = Field(default=0.0, ge=0.0)
    mapped_marks: float = Field(default=0.0, ge=0.0)
    questions_with_marks: int = Field(default=0, ge=0)
    missing_marks: int = Field(default=0, ge=0)
    mapped_marks_share: float | None = Field(default=None, ge=0.0, le=1.0)
    shared_concept_units: int = Field(default=0, ge=0)


class CoverageDataQuality(BaseModel):
    """Completeness of the evidence used by this report."""

    model_config = ConfigDict(extra="forbid")

    documents: int = Field(default=0, ge=0)
    document_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    question_papers: int = Field(default=0, ge=0)
    question_banks: int = Field(default=0, ge=0)
    sources: int = Field(default=0, ge=0)
    questions: int = Field(default=0, ge=0)
    analysed_questions: int = Field(default=0, ge=0)
    not_analysed_questions: int = Field(default=0, ge=0)
    mapped_questions: int = Field(default=0, ge=0)
    ambiguous_questions: int = Field(default=0, ge=0)
    unmapped_questions: int = Field(default=0, ge=0)
    questions_with_marks: int = Field(default=0, ge=0)
    missing_marks: int = Field(default=0, ge=0)
    marks_coverage_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    known_sessions: int = Field(default=0, ge=0)
    questions_with_session: int = Field(default=0, ge=0)
    questions_without_session: int = Field(default=0, ge=0)
    graph_concepts_matched: int = Field(default=0, ge=0)
    graph_assessment_edges: int = Field(default=0, ge=0)
    unscoped_questions_excluded: int = Field(default=0, ge=0)
    truncated: bool = False
    small_corpus: bool = False
    warnings: list[Uncertainty] = Field(default_factory=list)


class CoverageQuestionAssignment(BaseModel):
    """One question's best-fit concept for the complete mapping table.

    This is a review aid: accepted Phase 3/5/topic mappings are used first, and
    questions without a topic match receive a conservative best-fit assignment
    so the question is not hidden from the corpus table.  ``confidence`` is an
    evidence-strength display score on the 75--100% band, not a calibrated
    probability that the question will be examined.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    document_title: str
    question_number: str | None = None
    section: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    source_locator: str | None = None
    text_excerpt: str
    concept_id: str
    concept_label: str
    unit_ids: list[str] = Field(default_factory=list)
    unit_labels: list[str] = Field(default_factory=list)
    assignment_source: str
    assignment_method: str
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    matched_terms: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    analysis_status: MappingStatus | None = None
    exam_period: str
    exam_period_source: str
    exam_session: str | None = None
    exam_type: str | None = None


class CoverageReport(BaseModel):
    """One reproducible Phase 7 report for one course and syllabus version."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = COVERAGE_SCHEMA_VERSION
    report_id: str
    algorithm: str = COVERAGE_ALGORITHM
    algorithm_version: str = COVERAGE_ALGORITHM_VERSION
    course: CourseScope
    syllabus: CoverageSyllabus
    generated_at: datetime = Field(default_factory=utcnow)
    status: CoverageReportStatus
    filters: CoverageFilters = Field(default_factory=CoverageFilters)
    parameters: dict[str, Any] = Field(default_factory=dict)
    summary: CoverageSummary
    concepts: list[ConceptCoverageFinding] = Field(default_factory=list)
    units: list[UnitCoverageFinding] = Field(default_factory=list)
    question_assignments: list[CoverageQuestionAssignment] = Field(default_factory=list)
    unmapped_questions: list[UnmappedQuestionFinding] = Field(default_factory=list)
    data_quality: CoverageDataQuality
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


__all__ = [
    "COVERAGE_ALGORITHM",
    "COVERAGE_ALGORITHM_VERSION",
    "COVERAGE_SCHEMA_VERSION",
    "ConceptCoverageFinding",
    "CoverageDataQuality",
    "CoverageFilters",
    "CoverageMappingCandidate",
    "CoverageQuestionAssignment",
    "CoverageQuestionEvidence",
    "CoverageReport",
    "CoverageRequest",
    "CoverageStatus",
    "CoverageSummary",
    "CoverageSyllabus",
    "UnmappedQuestionFinding",
    "UnitCoverageFinding",
    "make_coverage_concept_id",
    "make_coverage_report_id",
]
