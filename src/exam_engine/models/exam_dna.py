"""Validated reports for the Phase 8 Exam DNA pattern detector.

The profile describes what the *available* question papers show. It never
forecasts a future examination, never converts a historical frequency into a
probability, and keeps the denominator behind every recurrence rate explicit.

Like Phases 4 and 7 the report is derived state: Phase 8 adds no table and does
not rewrite sources, questions, Phase 3 analysis, Phase 5 graph rows or Phase 7
coverage results.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from exam_engine.models.analytics import CourseScope
from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    BloomLevel,
    ChoiceRuleKind,
    CoverageReportStatus,
    CoverageStatus,
    DifficultyLabel,
    DocumentKind,
    EvidenceQuality,
    ExamDnaStatus,
    ExtractionStatus,
    PaperExclusionReason,
    PatternCategory,
    QuestionGroupKind,
    QuestionType,
)
from exam_engine.models.question import Uncertainty

EXAM_DNA_SCHEMA_VERSION = "phase8-v1"
EXAM_DNA_ALGORITHM = "phase8_exam_dna_pattern_detector"
EXAM_DNA_ALGORITHM_VERSION = "phase8.v1"


def _digest(payload: str, prefix: str) -> str:
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def make_profile_id(payload: str) -> str:
    return _digest(payload, "dna_")


def make_pattern_id(category: PatternCategory, key: str) -> str:
    return _digest(f"{category.value}\x1f{key}", "pat_")


def make_question_group_id(kind: QuestionGroupKind, question_ids: list[str]) -> str:
    joined = "\x1f".join(sorted(question_ids))
    return _digest(f"{kind.value}\x1f{joined}", "qgrp_")


class ExamDnaFilters(BaseModel):
    """Optional narrowing applied before papers and patterns are selected.

    Filters never widen scope to another course or syllabus version. An unknown
    document, source, exam type or period is rejected instead of silently
    matching nothing.
    """

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    exam_types: list[str] = Field(default_factory=list)
    exam_periods: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> ExamDnaFilters:
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
            self.document_ids
            or self.source_ids
            or self.exam_types
            or self.exam_periods
            or self.date_from is not None
            or self.date_to is not None
        )


class ExamDnaRequest(BaseModel):
    """One course-scoped request; a syllabus is required when several exist."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    filters: ExamDnaFilters = Field(default_factory=ExamDnaFilters)


class ExamDnaSyllabus(BaseModel):
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


class ExamDnaChoiceRule(BaseModel):
    """An explicit all-compulsory or choose-any instruction in one paper."""

    model_config = ConfigDict(extra="forbid")

    kind: ChoiceRuleKind
    required_count: int | None = Field(default=None, ge=1)
    evidence: str = Field(..., min_length=1)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    question_id: str | None = None


class ExamDnaPaper(BaseModel):
    """One question paper's summary, including why it was or was not eligible."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    source_id: str
    kind: DocumentKind = DocumentKind.QUESTION_PAPER
    exam_period: str
    exam_period_source: str
    exam_session: str | None = None
    exam_type: str | None = None
    exam_date: date | None = None
    extraction_status: ExtractionStatus = ExtractionStatus.OK
    eligible: bool = True
    exclusion_reason: PaperExclusionReason | None = None
    duplicate_of: str | None = None
    question_count: int = Field(default=0, ge=0)
    analysed_questions: int = Field(default=0, ge=0)
    subquestion_count: int = Field(default=0, ge=0)
    questions_with_marks: int = Field(default=0, ge=0)
    missing_marks: int = Field(default=0, ge=0)
    total_known_marks: float = Field(default=0.0, ge=0.0)
    question_type_counts: dict[str, int] = Field(default_factory=dict)
    bloom_counts: dict[str, int] = Field(default_factory=dict)
    difficulty_counts: dict[str, int] = Field(default_factory=dict)
    unit_counts: dict[str, int] = Field(default_factory=dict)
    unit_marks: dict[str, float] = Field(default_factory=dict)
    section_labels: list[str] = Field(default_factory=list)
    command_verb_counts: dict[str, int] = Field(default_factory=dict)
    choice_rules: list[ExamDnaChoiceRule] = Field(default_factory=list)
    low_confidence_questions: int = Field(default=0, ge=0)


class ExamDnaScope(BaseModel):
    """The exact evidence boundary copied into the profile."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(default_factory=list)
    exam_types: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    scope_statement: str = Field(..., min_length=1)


class ExamDnaDistributionRow(BaseModel):
    """Counts and shares for one label within the selected papers."""

    model_config = ConfigDict(extra="forbid")

    label: str
    question_count: int = Field(default=0, ge=0)
    papers_observed: int = Field(default=0, ge=0)
    share_of_questions: float | None = Field(default=None, ge=0.0, le=1.0)
    share_of_papers: float | None = Field(default=None, ge=0.0, le=1.0)
    marks: float = Field(default=0.0, ge=0.0)
    questions_with_marks: int = Field(default=0, ge=0)
    share_of_marks: float | None = Field(default=None, ge=0.0, le=1.0)


class ExamDnaDistributions(BaseModel):
    """Observed question-setting characteristics over the selected corpus."""

    model_config = ConfigDict(extra="forbid")

    total_questions: int = Field(default=0, ge=0)
    questions_with_marks: int = Field(default=0, ge=0)
    missing_marks: int = Field(default=0, ge=0)
    total_known_marks: float = Field(default=0.0, ge=0.0)
    question_types: list[ExamDnaDistributionRow] = Field(default_factory=list)
    bloom_levels: list[ExamDnaDistributionRow] = Field(default_factory=list)
    difficulty: list[ExamDnaDistributionRow] = Field(default_factory=list)
    units: list[ExamDnaDistributionRow] = Field(default_factory=list)
    sections: list[ExamDnaDistributionRow] = Field(default_factory=list)
    command_verbs: list[ExamDnaDistributionRow] = Field(default_factory=list)
    marks_values: list[ExamDnaDistributionRow] = Field(default_factory=list)
    marks_bands: list[ExamDnaDistributionRow] = Field(default_factory=list)
    choice_rules: list[ExamDnaDistributionRow] = Field(default_factory=list)


class ExamDnaPattern(BaseModel):
    """One observable pattern and the paper/session denominators behind it.

    ``recurrence_rate`` is null when no valid paper denominator exists. The rate
    is a historical description of the analyzed collection, never a probability
    for a future paper.
    """

    model_config = ConfigDict(extra="forbid")

    pattern_id: str
    category: PatternCategory
    key: str
    label: str
    description: str
    scope: str = "eligible_question_papers"
    papers_observed: int = Field(default=0, ge=0)
    eligible_papers: int = Field(default=0, ge=0)
    recurrence_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    denominator_valid: bool = False
    within_paper_occurrences: int = Field(default=0, ge=0)
    sessions_observed: int = Field(default=0, ge=0)
    eligible_sessions: int = Field(default=0, ge=0)
    session_recurrence_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    is_recurring: bool = False
    evidence_quality: EvidenceQuality = EvidenceQuality.LOW
    supporting_document_ids: list[str] = Field(default_factory=list)
    supporting_question_ids: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    exam_types: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExamDnaSession(BaseModel):
    """Observed characteristics of the papers in one dated examination session.

    Shares are descriptive; a missing denominator yields no share rather than a
    fabricated zero. Session rows exist only for papers that carry an explicit
    session key or examination date.
    """

    model_config = ConfigDict(extra="forbid")

    session: str
    label: str
    exam_period: str
    exam_date: date | None = None
    exam_types: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    question_count: int = Field(default=0, ge=0)
    analysed_questions: int = Field(default=0, ge=0)
    questions_with_marks: int = Field(default=0, ge=0)
    known_marks: float = Field(default=0.0, ge=0.0)
    question_type_shares: dict[str, float] = Field(default_factory=dict)
    bloom_shares: dict[str, float] = Field(default_factory=dict)
    unit_marks_shares: dict[str, float] = Field(default_factory=dict)


class ExamDnaTrend(BaseModel):
    """Descriptive change in one share between the observed dated sessions.

    ``change`` is ``last_value - first_value`` and stays null unless both boundary
    sessions define the share. A trend row never implies a future examination.
    """

    model_config = ConfigDict(extra="forbid")

    dimension: str
    key: str
    label: str
    sessions: list[str] = Field(default_factory=list)
    values: list[float | None] = Field(default_factory=list)
    first_value: float | None = Field(default=None, ge=0.0, le=1.0)
    last_value: float | None = Field(default=None, ge=0.0, le=1.0)
    change: float | None = Field(default=None, ge=-1.0, le=1.0)
    direction: Literal["increased", "decreased", "stable", "unknown"] = "unknown"
    evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class QuestionGroupMember(BaseModel):
    """One question appearance inside a repeated/similar group."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    document_id: str
    document_title: str
    question_number: str | None = None
    section: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    marks: float | None = Field(default=None, ge=0.0)
    text_excerpt: str
    exam_period: str
    exam_session: str | None = None
    exam_type: str | None = None
    primary_type: QuestionType | None = None
    bloom_level: BloomLevel | None = None
    difficulty: DifficultyLabel | None = None
    command_verb: str | None = None
    concepts: list[str] = Field(default_factory=list)


class ExamDnaQuestionGroup(BaseModel):
    """A group of repeated or similar questions, with its evidence kept distinct."""

    model_config = ConfigDict(extra="forbid")

    group_id: str
    kind: QuestionGroupKind
    method: str
    question_count: int = Field(default=0, ge=0)
    document_count: int = Field(default=0, ge=0)
    session_count: int = Field(default=0, ge=0)
    document_ids: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    shared_concepts: list[str] = Field(default_factory=list)
    shared_question_types: list[str] = Field(default_factory=list)
    shared_command_verb: str | None = None
    template_variant: bool = False
    wording_difference: str | None = None
    review_required: bool = True
    evidence_quality: EvidenceQuality = EvidenceQuality.LOW
    evidence: list[str] = Field(default_factory=list)
    members: list[QuestionGroupMember] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExamDnaConceptRef(BaseModel):
    """One Phase 7 concept classification carried into the DNA profile."""

    model_config = ConfigDict(extra="forbid")

    concept_id: str
    label: str
    status: CoverageStatus
    unit_labels: list[str] = Field(default_factory=list)
    distinct_question_count: int = Field(default=0, ge=0)


class ExamDnaCoverage(BaseModel):
    """Compact cross-reference to the reused Phase 7 coverage report."""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    status: CoverageReportStatus
    total_concepts: int = Field(default=0, ge=0)
    covered_concepts: int = Field(default=0, ge=0)
    low_coverage_concepts: int = Field(default=0, ge=0)
    unmapped_concepts: int = Field(default=0, ge=0)
    uncertain_concepts: int = Field(default=0, ge=0)
    insufficient_data_concepts: int = Field(default=0, ge=0)
    concept_coverage_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    mapped_question_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    total_questions: int = Field(default=0, ge=0)
    mapped_marks: float = Field(default=0.0, ge=0.0)
    total_known_marks: float = Field(default=0.0, ge=0.0)
    graph_available: bool = False
    concepts: list[ExamDnaConceptRef] = Field(default_factory=list)


class PaperExclusion(BaseModel):
    """A course document that was not independent paper evidence."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    reason: PaperExclusionReason
    duplicate_of: str | None = None
    question_count: int = Field(default=0, ge=0)


class ExamDnaDataQuality(BaseModel):
    """Completeness, comparability and exclusion accounting for one profile."""

    model_config = ConfigDict(extra="forbid")

    question_papers: int = Field(default=0, ge=0)
    eligible_papers: int = Field(default=0, ge=0)
    analysed_papers: int = Field(default=0, ge=0)
    excluded_papers: int = Field(default=0, ge=0)
    duplicate_paper_groups: int = Field(default=0, ge=0)
    duplicate_copies_excluded: int = Field(default=0, ge=0)
    question_banks_excluded: int = Field(default=0, ge=0)
    other_documents: int = Field(default=0, ge=0)
    questions: int = Field(default=0, ge=0)
    analysed_questions: int = Field(default=0, ge=0)
    analysis_coverage_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    questions_with_marks: int = Field(default=0, ge=0)
    missing_marks: int = Field(default=0, ge=0)
    marks_coverage_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    classified_questions: int = Field(default=0, ge=0)
    unclassified_questions: int = Field(default=0, ge=0)
    bloom_classified_questions: int = Field(default=0, ge=0)
    low_confidence_questions: int = Field(default=0, ge=0)
    low_structure_confidence_questions: int = Field(default=0, ge=0)
    questions_with_subparts: int = Field(default=0, ge=0)
    uncertain_concept_mappings: int = Field(default=0, ge=0)
    questions_with_session: int = Field(default=0, ge=0)
    questions_without_session: int = Field(default=0, ge=0)
    known_sessions: int = Field(default=0, ge=0)
    papers_with_dates: int = Field(default=0, ge=0)
    papers_without_dates: int = Field(default=0, ge=0)
    exam_types: list[str] = Field(default_factory=list)
    papers_with_sections: int = Field(default=0, ge=0)
    papers_without_sections: int = Field(default=0, ge=0)
    choice_rules_detected: int = Field(default=0, ge=0)
    similarity_pairs_considered: int = Field(default=0, ge=0)
    similarity_pairs_reused: int = Field(default=0, ge=0)
    similarity_pairs_below_threshold: int = Field(default=0, ge=0)
    truncated: bool = False
    patterns_truncated: bool = False
    question_groups_truncated: bool = False
    small_sample: bool = False
    questions_mapped_to_other_syllabus_versions: int = Field(default=0, ge=0)
    papers_mapped_to_other_syllabus_versions: int = Field(default=0, ge=0)
    exclusions: list[PaperExclusion] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)


class ExamDnaSummary(BaseModel):
    """Headline counts; every rate keeps its own denominator in the sections."""

    model_config = ConfigDict(extra="forbid")

    total_papers: int = Field(default=0, ge=0)
    eligible_papers: int = Field(default=0, ge=0)
    excluded_papers: int = Field(default=0, ge=0)
    questions: int = Field(default=0, ge=0)
    analysed_questions: int = Field(default=0, ge=0)
    questions_with_marks: int = Field(default=0, ge=0)
    total_known_marks: float = Field(default=0.0, ge=0.0)
    known_sessions: int = Field(default=0, ge=0)
    pattern_count: int = Field(default=0, ge=0)
    recurring_pattern_count: int = Field(default=0, ge=0)
    exact_question_groups: int = Field(default=0, ge=0)
    similar_question_groups: int = Field(default=0, ge=0)
    units_observed: int = Field(default=0, ge=0)
    concepts_observed: int = Field(default=0, ge=0)
    dated_sessions: int = Field(default=0, ge=0)
    trend_count: int = Field(default=0, ge=0)


class ExamDnaProfile(BaseModel):
    """One reproducible Phase 8 profile for one course and syllabus version."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXAM_DNA_SCHEMA_VERSION
    profile_id: str
    algorithm: str = EXAM_DNA_ALGORITHM
    algorithm_version: str = EXAM_DNA_ALGORITHM_VERSION
    course: CourseScope
    syllabus: ExamDnaSyllabus
    generated_at: datetime = Field(default_factory=utcnow)
    status: ExamDnaStatus
    filters: ExamDnaFilters = Field(default_factory=ExamDnaFilters)
    scope: ExamDnaScope
    parameters: dict[str, Any] = Field(default_factory=dict)
    summary: ExamDnaSummary
    papers: list[ExamDnaPaper] = Field(default_factory=list)
    distributions: ExamDnaDistributions
    patterns: list[ExamDnaPattern] = Field(default_factory=list)
    question_groups: list[ExamDnaQuestionGroup] = Field(default_factory=list)
    sessions: list[ExamDnaSession] = Field(default_factory=list)
    trends: list[ExamDnaTrend] = Field(default_factory=list)
    coverage: ExamDnaCoverage
    data_quality: ExamDnaDataQuality
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


__all__ = [
    "EXAM_DNA_ALGORITHM",
    "EXAM_DNA_ALGORITHM_VERSION",
    "EXAM_DNA_SCHEMA_VERSION",
    "ExamDnaChoiceRule",
    "ExamDnaConceptRef",
    "ExamDnaCoverage",
    "ExamDnaDataQuality",
    "ExamDnaDistributionRow",
    "ExamDnaDistributions",
    "ExamDnaFilters",
    "ExamDnaPaper",
    "ExamDnaPattern",
    "ExamDnaProfile",
    "ExamDnaQuestionGroup",
    "ExamDnaRequest",
    "ExamDnaScope",
    "ExamDnaSession",
    "ExamDnaSummary",
    "ExamDnaTrend",
    "ExamDnaSyllabus",
    "PaperExclusion",
    "QuestionGroupMember",
    "make_pattern_id",
    "make_profile_id",
    "make_question_group_id",
]
