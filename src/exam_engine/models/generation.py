"""Validated Phase 10 generation requests, drafts, questions and batches.

Generated questions are proposed content. They carry their grounding passages,
validation outcome and review state, and they are stored separately from historical
examination questions. No field here is a prediction that an examination will ask
the question.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    DocumentKind,
    GenerationFormat,
    GenerationReviewStatus,
    GenerationStatus,
    GenerationValidationStatus,
    Severity,
)
from exam_engine.models.question import Uncertainty

GENERATION_SCHEMA_VERSION = "phase10-v1"
GENERATION_ALGORITHM = "phase10_grounded_question_generator"
GENERATION_ALGORITHM_VERSION = "phase10.v1"


class GenerationRequest(BaseModel):
    """One validated generation request.

    Either explicit concepts or a syllabus unit is required. Defaults are resolved by
    the service and echoed into the batch report; no unrelated course material is
    substituted silently.
    """

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    concept_ids: list[str] = Field(default_factory=list)
    unit_id: str | None = None
    count: int = Field(default=5, ge=1)
    format: GenerationFormat = GenerationFormat.EXPLANATION
    difficulty: DifficultyLabel | None = None
    bloom: BloomLevel | None = None
    marks: float | None = Field(default=None, gt=0.0, le=100.0)
    document_ids: list[str] = Field(default_factory=list)
    reference_question_ids: list[str] = Field(default_factory=list)
    language: str = Field(default="en", min_length=2, max_length=10)
    provider: str = Field(default="auto", min_length=1, max_length=40)
    seed: int = Field(default=0, ge=0)
    include_answer_key: bool = True
    include_explanation: bool = True
    allow_overwrite_approved: bool = False

    @model_validator(mode="after")
    def _require_target(self) -> GenerationRequest:
        if not self.concept_ids and not self.unit_id:
            raise ValueError("provide at least one concept or a syllabus unit")
        return self


class GenerationQuery(BaseModel):
    """Filters for reading persisted generated questions."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    batch_id: str | None = None
    concept_id: str | None = None
    format: GenerationFormat | None = None
    review_status: GenerationReviewStatus | None = None
    validation_status: GenerationValidationStatus | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class SourcePassage(BaseModel):
    """One grounded excerpt attached to a generated question."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_title: str
    document_kind: DocumentKind
    page_number: int | None = Field(default=None, ge=1)
    excerpt: str = Field(..., min_length=1)
    concept_id: str | None = None
    method: str = Field(default="document_text", min_length=1)


class GeneratedOption(BaseModel):
    """One multiple-choice option; exactly one option is correct."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=4)
    text: str = Field(..., min_length=1)
    is_correct: bool = False


class ValidationIssue(BaseModel):
    """One explainable validation finding."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    severity: Severity = Severity.WARNING
    rule: str = Field(..., min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    """Automated validation outcome, not an educational correctness guarantee."""

    model_config = ConfigDict(extra="forbid")

    status: GenerationValidationStatus
    issues: list[ValidationIssue] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ReviewEvent(BaseModel):
    """One review transition, kept as an append-only audit trail."""

    model_config = ConfigDict(extra="forbid")

    status: GenerationReviewStatus
    note: str | None = None
    reviewer: str = "local-user"
    at: datetime = Field(default_factory=utcnow)


class GenerationPrompt(BaseModel):
    """Everything a provider may use for one question; no hidden course access."""

    model_config = ConfigDict(extra="forbid")

    course_id: str
    syllabus_id: str
    concept_id: str
    concept_label: str
    concept_terms: list[str] = Field(
        default_factory=list,
        description="Label plus known aliases, used to hide the answer in cloze items.",
    )
    unit_id: str
    unit_label: str
    unit_title: str | None = None
    learning_objective: str
    related_labels: list[str] = Field(default_factory=list)
    passages: list[SourcePassage] = Field(default_factory=list)
    related_passages: list[SourcePassage] = Field(
        default_factory=list,
        description="Grounding excerpts for related concepts, used by comparison tasks.",
    )
    format: GenerationFormat
    bloom: BloomLevel
    difficulty: DifficultyLabel
    marks: float
    language: str
    index: int = Field(default=0, ge=0)
    count: int = Field(default=1, ge=1)
    seed: int = Field(default=0, ge=0)
    include_answer_key: bool = True
    include_explanation: bool = True
    reference_style: dict[str, Any] = Field(default_factory=dict)


class QuestionDraft(BaseModel):
    """Provider output before validation, grounding and provenance are attached."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1)
    options: list[str] = Field(default_factory=list)
    correct_index: int | None = Field(default=None, ge=0)
    answer_key: str | None = None
    explanation: str | None = None
    solution_steps: list[str] = Field(default_factory=list)
    distractor_explanations: dict[str, str] = Field(default_factory=dict)
    numeric_value: float | None = None
    numeric_unit: str | None = None
    template_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GeneratedQuestion(BaseModel):
    """One persisted, validated, reviewable generated question."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    batch_id: str
    revision: int = Field(default=1, ge=1)
    course_id: str
    syllabus_id: str
    origin: str = "generation"
    source_question_id: str | None = None
    strategy: str | None = None
    unit_id: str
    unit_label: str
    unit_title: str | None = None
    concept_ids: list[str] = Field(default_factory=list)
    concept_labels: list[str] = Field(default_factory=list)
    learning_objective: str
    format: GenerationFormat
    question_type: str | None = None
    requested_difficulty: DifficultyLabel = DifficultyLabel.MODERATE
    estimated_difficulty: DifficultyLabel = DifficultyLabel.UNKNOWN
    difficulty_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    difficulty_method: str = "phase3_difficulty"
    requested_bloom: BloomLevel = BloomLevel.UNDERSTAND
    estimated_bloom: BloomLevel | None = None
    bloom_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    marks: float = Field(..., gt=0.0, le=100.0)
    text: str = Field(..., min_length=1)
    options: list[GeneratedOption] = Field(default_factory=list)
    correct_option_key: str | None = None
    answer_key: str | None = None
    explanation: str | None = None
    solution_steps: list[str] = Field(default_factory=list)
    distractor_explanations: dict[str, str] = Field(default_factory=dict)
    source_references: list[SourcePassage] = Field(default_factory=list)
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    validation: ValidationResult
    review_status: GenerationReviewStatus = GenerationReviewStatus.PENDING_REVIEW
    review_history: list[ReviewEvent] = Field(default_factory=list)
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class GenerationRejection(BaseModel):
    """A candidate that could not be generated, validated or kept as distinct."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=0)
    concept_id: str
    concept_label: str
    format: GenerationFormat
    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    text: str | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)


class DuplicateResolution(BaseModel):
    """A candidate rejected because an existing question is too similar."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=0)
    concept_id: str
    kind: str
    matched_question_id: str
    score: float = Field(..., ge=0.0, le=1.0)
    reason: str


class GenerationCounts(BaseModel):
    """Batch accounting; every requested item has exactly one outcome."""

    model_config = ConfigDict(extra="forbid")

    requested: int = Field(default=0, ge=0)
    generated: int = Field(default=0, ge=0)
    validated: int = Field(default=0, ge=0)
    needs_review: int = Field(default=0, ge=0)
    validation_incomplete: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    persisted: int = Field(default=0, ge=0)
    reused_approved: int = Field(default=0, ge=0)


class GenerationScope(BaseModel):
    """Exact evidence boundary copied into a batch report."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(default_factory=list)
    syllabus_id: str
    unit_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)
    statement: str = Field(..., min_length=1)


class GenerationBatch(BaseModel):
    """One reproducible batch, including rejections and duplicate decisions."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = GENERATION_SCHEMA_VERSION
    batch_id: str
    algorithm: str = GENERATION_ALGORITHM
    algorithm_version: str = GENERATION_ALGORITHM_VERSION
    course_id: str
    status: GenerationStatus
    request: GenerationRequest
    provider: str
    model: str | None = None
    capability: str = "generation"
    resolved_bloom: BloomLevel
    resolved_difficulty: DifficultyLabel
    reference_style: dict[str, Any] = Field(default_factory=dict)
    counts: GenerationCounts
    questions: list[GeneratedQuestion] = Field(default_factory=list)
    rejections: list[GenerationRejection] = Field(default_factory=list)
    duplicates: list[DuplicateResolution] = Field(default_factory=list)
    scope: GenerationScope
    parameters: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


__all__ = [
    "GENERATION_ALGORITHM",
    "GENERATION_ALGORITHM_VERSION",
    "GENERATION_SCHEMA_VERSION",
    "DuplicateResolution",
    "GeneratedOption",
    "GeneratedQuestion",
    "GenerationBatch",
    "GenerationCounts",
    "GenerationFormat",
    "GenerationPrompt",
    "GenerationQuery",
    "GenerationRejection",
    "GenerationRequest",
    "GenerationScope",
    "QuestionDraft",
    "ReviewEvent",
    "SourcePassage",
    "ValidationIssue",
    "ValidationResult",
]
