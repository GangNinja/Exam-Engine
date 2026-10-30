"""Validated reports produced by the Phase 4 analytics engine.

The analytics layer is read-only: it consumes Phase 1 question records, Phase 2
signals already used by Phase 3, and the Phase 3 derived analysis rows. Reports
are deliberately explicit about denominators, sample sizes, missing metadata and
uncertain estimates. A report never turns an unavailable measurement into zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    DifficultyLabel,
    DocumentKind,
    QuestionType,
    SimilarityKind,
    TrendStatus,
)
from exam_engine.models.question import Uncertainty

ANALYTICS_SCHEMA_VERSION = "phase4-v1"


class AnalyticsFilters(BaseModel):
    """Optional narrowing applied before any metric is calculated.

    Course selection is kept separate because it defines the syllabus and
    grading boundary. Institution and syllabus-version filters select whole
    courses; they are never used to combine rows across courses.
    """

    model_config = ConfigDict(extra="forbid")

    institution_code: str | None = None
    syllabus_version: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    exam_types: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    question_types: list[QuestionType] = Field(default_factory=list)
    difficulties: list[DifficultyLabel] = Field(default_factory=list)
    marks_min: float | None = Field(default=None, ge=0.0)
    marks_max: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _validate_marks_range(self) -> AnalyticsFilters:
        if (
            self.marks_min is not None
            and self.marks_max is not None
            and self.marks_min > self.marks_max
        ):
            raise ValueError("marks_min must not exceed marks_max")
        return self

    @property
    def restricts_questions(self) -> bool:
        return bool(
            self.source_ids
            or self.document_ids
            or self.unit_ids
            or self.topics
            or self.exam_types
            or self.periods
            or self.question_types
            or self.difficulties
            or self.marks_min is not None
            or self.marks_max is not None
        )


class CourseScope(BaseModel):
    """Course boundary copied into every report."""

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


class QuestionReference(BaseModel):
    """A question plus the source provenance needed to review a finding."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    document_title: str
    document_kind: DocumentKind
    source_id: str
    question_number: str | None = None
    section: str | None = None
    page_number: int | None = None
    source_locator: str | None = None
    marks: float | None = None
    text: str
    exam_period: str
    exam_period_source: str
    exam_type: str | None = None
    session: str | None = None


class DocumentFrequencyRow(BaseModel):
    """Question count and marks coverage for one source document."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    kind: DocumentKind
    source_id: str
    exam_period: str
    exam_period_source: str
    exam_type: str | None = None
    question_count: int = 0
    distinct_question_texts: int = 0
    total_marks: float = 0.0
    questions_with_marks: int = 0
    missing_marks: int = 0


class ExactDuplicateGroup(BaseModel):
    """Questions whose normalised stored text is identical.

    This is the only repetition kind that establishes identical wording. It does
    not claim that two appearances played the same role in their papers.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    question_count: int
    document_count: int
    known_session_count: int
    within_document: bool
    across_documents: bool
    across_sessions: bool
    appearances: list[QuestionReference]


class SimilarityPair(BaseModel):
    """A stored near/semantic/concept relationship, with source evidence."""

    model_config = ConfigDict(extra="forbid")

    kind: SimilarityKind
    score: float = Field(..., ge=0.0, le=1.0)
    lexical_score: float = Field(default=0.0, ge=0.0, le=1.0)
    semantic_score: float = Field(default=0.0, ge=0.0, le=1.0)
    method: str
    evidence: list[str] = Field(default_factory=list)
    same_document: bool
    same_session: bool | None = None
    review_required: bool = True
    left: QuestionReference
    right: QuestionReference


class SimilarityCoverageRow(BaseModel):
    """Aggregate source coverage for one non-exact similarity kind.

    These are distinct questions/documents involved in stored candidate pairs,
    not a claim that every pair is a duplicate. Pair-level evidence remains in
    ``RepetitionReport.similarity_pairs``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SimilarityKind
    pair_count: int = 0
    question_count: int = 0
    document_count: int = 0
    known_session_count: int = 0
    within_document_pairs: int = 0
    across_document_pairs: int = 0
    same_session_pairs: int = 0
    different_session_pairs: int = 0
    unknown_session_pairs: int = 0


class RepetitionReport(BaseModel):
    """Exact and similar question occurrences across documents and sessions."""

    model_config = ConfigDict(extra="forbid")

    total_questions: int = 0
    distinct_question_texts: int = 0
    documents: int = 0
    question_papers: int = 0
    known_sessions: int = 0
    exact_groups: list[ExactDuplicateGroup] = Field(default_factory=list)
    exact_group_count: int = 0
    exact_questions: int = 0
    exact_repeated_appearances: int = 0
    within_document_groups: int = 0
    across_document_groups: int = 0
    across_session_groups: int = 0
    near_pairs: int = 0
    semantic_pairs: int = 0
    concept_related_pairs: int = 0
    similarity_coverage: list[SimilarityCoverageRow] = Field(default_factory=list)
    similarity_pairs: list[SimilarityPair] = Field(default_factory=list)
    document_frequency: list[DocumentFrequencyRow] = Field(default_factory=list)
    similarity_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[Uncertainty] = Field(default_factory=list)


class UnitFrequencyRow(BaseModel):
    """One syllabus unit and the questions assigned to it."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_label: str
    unit_title: str | None = None
    syllabus_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    question_count: int = 0
    share_of_questions: float = Field(default=0.0, ge=0.0, le=1.0)
    distinct_documents: int = 0
    distinct_sessions: int = 0
    average_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TopicFrequencyRow(BaseModel):
    """One unit/topic bucket; a question may appear in more than one bucket."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_label: str
    topic: str | None = None
    label: str
    document_ids: list[str] = Field(default_factory=list)
    question_count: int = 0
    share_of_questions: float = Field(default=0.0, ge=0.0, le=1.0)
    distinct_documents: int = 0
    distinct_sessions: int = 0
    average_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ConceptFrequencyRow(BaseModel):
    """A syllabus topic used as a question-level concept label.

    Phase 3 has no separate question-to-concept classifier. This report therefore
    groups *syllabus topic labels* across units; it is not a Phase 2 inferred
    concept and is not a validated semantic concept.
    """

    model_config = ConfigDict(extra="forbid")

    concept: str
    unit_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    question_count: int = 0
    share_of_questions: float = Field(default=0.0, ge=0.0, le=1.0)
    distinct_documents: int = 0
    distinct_sessions: int = 0
    average_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class MappingFrequencyReport(BaseModel):
    """Coverage and distributions for course-scoped syllabus mapping."""

    model_config = ConfigDict(extra="forbid")

    total_questions: int = 0
    analysed_questions: int = 0
    mapped_questions: int = 0
    ambiguous_questions: int = 0
    unmapped_questions: int = 0
    not_analysed_questions: int = 0
    questions_with_any_unit: int = 0
    unit_assignments: int = 0
    multi_unit_questions: int = 0
    questions_with_any_topic: int = 0
    topic_assignments: int = 0
    multi_topic_questions: int = 0
    unassigned_topic_questions: int = 0
    mapping_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    units: list[UnitFrequencyRow] = Field(default_factory=list)
    topics: list[TopicFrequencyRow] = Field(default_factory=list)
    concepts: list[ConceptFrequencyRow] = Field(default_factory=list)
    unmapped_references: list[QuestionReference] = Field(default_factory=list)
    ambiguous_references: list[QuestionReference] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)


class QuestionMarksRow(BaseModel):
    """Marks and the primary labels for one question."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    marks: float
    section: str | None = None
    question_number: str | None = None
    unit_label: str | None = None
    topic: str | None = None
    question_type: str | None = None
    difficulty: str | None = None


class MarksAllocationRow(BaseModel):
    """Frequency of one exact stated marks value."""

    model_config = ConfigDict(extra="forbid")

    marks: float
    question_count: int
    total_marks: float


class MarksBreakdownRow(BaseModel):
    """Marks coverage for a unit, topic, section or label."""

    model_config = ConfigDict(extra="forbid")

    label: str
    question_count: int = 0
    questions_with_marks: int = 0
    missing_marks: int = 0
    total_marks: float = 0.0
    mean_marks: float | None = None
    share_of_questions: float = Field(default=0.0, ge=0.0, le=1.0)
    share_of_marks: float | None = Field(default=None, ge=0.0, le=1.0)


class MarksAnomaly(BaseModel):
    """A question whose marks metadata deserves review; no value is inferred."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    question_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class MarksReport(BaseModel):
    """Descriptive marks statistics, always separable from missing values."""

    model_config = ConfigDict(extra="forbid")

    total_questions: int = 0
    questions_with_marks: int = 0
    missing_marks: int = 0
    marks_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    total_marks: float = 0.0
    mean_marks: float | None = None
    median_marks: float | None = None
    min_marks: float | None = None
    max_marks: float | None = None
    zero_marks_questions: int = 0
    marks_sources: dict[str, int] = Field(default_factory=dict)
    question_marks: list[QuestionMarksRow] = Field(default_factory=list)
    common_allocations: list[MarksAllocationRow] = Field(default_factory=list)
    by_section: list[MarksBreakdownRow] = Field(default_factory=list)
    by_unit: list[MarksBreakdownRow] = Field(default_factory=list)
    by_topic: list[MarksBreakdownRow] = Field(default_factory=list)
    by_question_type: list[MarksBreakdownRow] = Field(default_factory=list)
    by_difficulty: list[MarksBreakdownRow] = Field(default_factory=list)
    subpart_mismatches: list[MarksAnomaly] = Field(default_factory=list)
    anomalies: list[MarksAnomaly] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)


class LabelCountRow(BaseModel):
    """Count and share for one classification label."""

    model_config = ConfigDict(extra="forbid")

    label: str
    question_count: int = 0
    share_of_questions: float = Field(default=0.0, ge=0.0, le=1.0)


class QuestionTypeReport(BaseModel):
    """Primary and multi-label question-type distributions."""

    model_config = ConfigDict(extra="forbid")

    total_questions: int = 0
    analysed_questions: int = 0
    unclassified_questions: int = 0
    not_analysed_questions: int = 0
    low_confidence_questions: int = 0
    multi_label_questions: int = 0
    primary_assignments: int = 0
    label_assignments: int = 0
    primary_labels: list[LabelCountRow] = Field(default_factory=list)
    all_labels: list[LabelCountRow] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)


class DifficultyReport(BaseModel):
    """Estimated difficulty distribution; never measured student difficulty."""

    model_config = ConfigDict(extra="forbid")

    total_questions: int = 0
    analysed_questions: int = 0
    estimated_questions: int = 0
    not_analysed_questions: int = 0
    unknown_questions: int = 0
    low_confidence_questions: int = 0
    labels: list[LabelCountRow] = Field(default_factory=list)
    mean_score: float | None = None
    estimated: bool = True
    empirical_measurement_available: bool = False
    warnings: list[Uncertainty] = Field(default_factory=list)


class DataQualityReport(BaseModel):
    """Coverage, missing metadata and sample-size accounting."""

    model_config = ConfigDict(extra="forbid")

    questions: int = 0
    distinct_question_texts: int = 0
    documents: int = 0
    documents_with_questions: int = 0
    question_papers: int = 0
    question_banks: int = 0
    sources: int = 0
    known_sessions: int = 0
    known_periods: int = 0
    questions_with_period: int = 0
    questions_without_period: int = 0
    analysed_questions: int = 0
    analysis_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    mapped_questions: int = 0
    ambiguous_questions: int = 0
    unmapped_questions: int = 0
    not_analysed_questions: int = 0
    mapping_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    classified_questions: int = 0
    unclassified_questions: int = 0
    low_confidence_questions: int = 0
    questions_with_marks: int = 0
    missing_marks: int = 0
    marks_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review_questions: int = 0
    exact_duplicate_groups: int = 0
    exact_duplicate_appearances: int = Field(
        default=0,
        description="All question appearances that belong to an exact repeated-text group.",
    )
    duplicate_document_text_groups: int = 0
    duplicate_document_text_documents: int = 0
    small_sample: bool = False
    truncated: bool = False
    warnings: list[Uncertainty] = Field(default_factory=list)


class TrendPeriodSummary(BaseModel):
    """One observed examination period, without interpolation."""

    model_config = ConfigDict(extra="forbid")

    period: str
    source: str
    sort_key: str
    question_count: int = 0
    document_count: int = 0
    document_ids: list[str] = Field(default_factory=list)
    session_count: int = 0
    exam_types: list[str] = Field(default_factory=list)
    total_marks: float = 0.0
    questions_with_marks: int = 0
    missing_marks: int = 0
    low_sample: bool = False
    unit_counts: dict[str, int] = Field(default_factory=dict)
    topic_counts: dict[str, int] = Field(default_factory=dict)
    question_type_counts: dict[str, int] = Field(default_factory=dict)
    difficulty_counts: dict[str, int] = Field(default_factory=dict)


class TrendChange(BaseModel):
    """Descriptive change between two adjacent observed periods."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    label: str
    previous_period: str
    current_period: str
    previous_value: float
    current_value: float
    delta: float
    previous_sample: int
    current_sample: int
    reliable: bool


class NewTopic(BaseModel):
    """A topic observed for the first time in this dataset."""

    model_config = ConfigDict(extra="forbid")

    topic: str
    normalized_topic: str
    period: str
    question_count: int
    reliable: bool


class TrendReport(BaseModel):
    """Observed-period comparison; not a causal or predictive model."""

    model_config = ConfigDict(extra="forbid")

    status: TrendStatus
    available: bool
    reason: str | None = None
    periods: list[TrendPeriodSummary] = Field(default_factory=list)
    changes: list[TrendChange] = Field(default_factory=list)
    new_topics: list[NewTopic] = Field(default_factory=list)
    questions_with_period: int = 0
    questions_without_period: int = 0
    known_sessions: int = 0
    first_period: str | None = None
    last_period: str | None = None
    period_sources: list[str] = Field(default_factory=list)
    minimum_period_questions: int = 0
    warnings: list[Uncertainty] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TopicPriorityRow(BaseModel):
    """A transparent descriptive ranking of one topic."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    topic: str
    normalized_topic: str
    unit_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    question_count: int = 0
    document_count: int = 0
    paper_count: int = 0
    session_count: int = 0
    frequency_basis: str | None = None
    total_marks: float = 0.0
    questions_with_marks: int = 0
    average_marks: float | None = None
    average_mapping_confidence: float = 0.0
    latest_period: str | None = None
    factors: dict[str, float | None] = Field(default_factory=dict)
    weights_used: dict[str, float] = Field(default_factory=dict)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    explanation: list[str] = Field(default_factory=list)


class TopicPriorityReport(BaseModel):
    """Historical prominence ranking, explicitly not a forecast."""

    model_config = ConfigDict(extra="forbid")

    topics: list[TopicPriorityRow] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    recency_available: bool = False
    coverage_available: bool = False
    marks_available: bool = False
    notes: list[str] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)


class AnalyticsReport(BaseModel):
    """One course's complete Phase 4 report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = ANALYTICS_SCHEMA_VERSION
    course: CourseScope
    generated_at: datetime = Field(default_factory=utcnow)
    settings: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    data_quality: DataQualityReport
    mapping: MappingFrequencyReport
    marks: MarksReport
    question_types: QuestionTypeReport
    difficulty: DifficultyReport
    repetition: RepetitionReport
    trends: TrendReport
    topic_priority: TopicPriorityReport
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


__all__ = [
    "ANALYTICS_SCHEMA_VERSION",
    "AnalyticsFilters",
    "AnalyticsReport",
    "ConceptFrequencyRow",
    "CourseScope",
    "DataQualityReport",
    "DifficultyReport",
    "DocumentFrequencyRow",
    "ExactDuplicateGroup",
    "LabelCountRow",
    "MappingFrequencyReport",
    "MarksAllocationRow",
    "MarksAnomaly",
    "MarksBreakdownRow",
    "MarksReport",
    "NewTopic",
    "QuestionMarksRow",
    "QuestionReference",
    "QuestionTypeReport",
    "RepetitionReport",
    "SimilarityCoverageRow",
    "SimilarityPair",
    "TopicFrequencyRow",
    "TopicPriorityReport",
    "TopicPriorityRow",
    "TrendChange",
    "TrendPeriodSummary",
    "TrendReport",
    "UnitFrequencyRow",
]
