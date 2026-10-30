"""Phase 11 advanced assessment models.

Three capabilities share these models:

``MutationRequest`` / ``AdversarialRequest``
    Validated inputs. Both produce ordinary :class:`GeneratedQuestion` rows with an
    explicit ``origin`` and ``strategy``, so validation, review, dedupe and export
    stay the Phase 10 surface.
``ResponseSubmission`` / ``ResponseRecord``
    One anonymised student answer. Response identifiers are hashes, never names.
``MisconceptionRecord`` / ``MisconceptionReport``
    Evidence-bearing candidate patterns. A record is a *candidate* until a person
    reviews it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    AssessmentOrigin,
    BloomLevel,
    ChallengeStrategy,
    DifficultyLabel,
    GenerationFormat,
    MisconceptionKind,
    MisconceptionStatus,
    MutationStrategy,
    ResponseCorrectness,
    SupportLevel,
)
from exam_engine.models.generation import ReviewEvent, SourcePassage
from exam_engine.models.question import Uncertainty

MUTATION_SCHEMA_VERSION = "phase11.mutation.v1"
ADVERSARIAL_SCHEMA_VERSION = "phase11.adversarial.v1"
MISCONCEPTION_SCHEMA_VERSION = "phase11.misconception.v1"

#: Longest answer excerpt kept in a misconception record or report.
EXCERPT_CHARS = 240

#: Shortest answer, in words, that can be read as an attempt at all.
MIN_ANSWER_WORDS = 3


def response_ref(*parts: str) -> str:
    """An anonymised, stable response identifier.

    Only a hash of the caller's reference is kept. Two imports of the same answer
    with the same reference collapse to one id, which is what independent-response
    counting needs.
    """

    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return f"resp_{digest[:16]}"


def make_misconception_id(
    course_id: str, syllabus_id: str, kind: MisconceptionKind, concept_ids: list[str]
) -> str:
    """Deterministic id: the same pattern over the same scope keeps its identity."""

    scope = ",".join(sorted(concept_ids))
    digest = hashlib.sha256(
        f"{course_id}\x1f{syllabus_id}\x1f{kind.value}\x1f{scope}".encode()
    ).hexdigest()
    return f"mc_{digest[:20]}"


class MutationRequest(BaseModel):
    """One validated request to derive variants of an existing question."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    question_id: str = Field(..., min_length=1)
    strategies: list[MutationStrategy] = Field(default_factory=list)
    count: int = Field(default=3, ge=1)
    difficulty: DifficultyLabel | None = None
    bloom: BloomLevel | None = None
    format: GenerationFormat | None = Field(
        default=None,
        description=(
            "Optional constraint on the response format. When given it must match the source "
            "question, because a mutation never converts between formats."
        ),
    )
    preserve_objective: bool = True
    preserve_concept: bool = True
    preserve_answer: bool = True
    allow_format_change: bool = False
    seed: int = Field(default=0, ge=0)
    persist: bool = True

    @model_validator(mode="after")
    def _no_duplicate_strategies(self) -> MutationRequest:
        if len(set(self.strategies)) != len(self.strategies):
            raise ValueError("each mutation strategy may be requested once")
        return self


class AdversarialRequest(BaseModel):
    """One validated request for challenging but fair questions."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    concept_ids: list[str] = Field(default_factory=list)
    unit_id: str | None = None
    strategies: list[ChallengeStrategy] = Field(default_factory=list)
    count: int = Field(default=3, ge=1)
    difficulty: DifficultyLabel | None = None
    bloom: BloomLevel | None = None
    allow_out_of_scope: bool = False
    include_rubric: bool = True
    seed: int = Field(default=0, ge=0)
    language: str = Field(default="en", min_length=2, max_length=10)
    persist: bool = True

    @model_validator(mode="after")
    def _require_target(self) -> AdversarialRequest:
        if not self.concept_ids and not self.unit_id:
            raise ValueError("provide at least one concept or a syllabus unit")
        if len(set(self.strategies)) != len(self.strategies):
            raise ValueError("each challenge strategy may be requested once")
        return self


class MutationCandidate(BaseModel):
    """One derived variant before it is accepted into a batch."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=0)
    strategy: MutationStrategy
    text: str = Field(..., min_length=1)
    concept_ids: list[str] = Field(default_factory=list)
    concept_labels: list[str] = Field(default_factory=list)
    answer_key: str | None = None
    explanation: str | None = None
    solution_steps: list[str] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)
    correct_option_key: str | None = None
    bloom: BloomLevel
    difficulty: DifficultyLabel
    objective: str
    invariants: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_references: list[SourcePassage] = Field(default_factory=list)


class AdversarialCandidate(BaseModel):
    """One challenging question plus its teacher-facing material."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=0)
    strategy: ChallengeStrategy
    text: str = Field(..., min_length=1)
    answer_key: str | None = None
    explanation: str | None = None
    rubric: list[str] = Field(default_factory=list)
    bloom: BloomLevel
    difficulty: DifficultyLabel
    format: GenerationFormat
    objective: str
    rationale: str
    concept_ids: list[str] = Field(default_factory=list)
    concept_labels: list[str] = Field(default_factory=list)
    source_references: list[SourcePassage] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResponseSubmission(BaseModel):
    """One student answer as supplied for import.

    ``student_ref`` is optional and is hashed on import; the plain value is never
    stored, logged or reported.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(..., min_length=1)
    answer: str
    correctness: ResponseCorrectness | None = None
    marks_awarded: float | None = Field(default=None, ge=0.0)
    marks_available: float | None = Field(default=None, gt=0.0)
    student_ref: str | None = Field(default=None, max_length=200)
    group_ref: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _marks_sane(self) -> ResponseSubmission:
        if (
            self.marks_awarded is not None
            and self.marks_available is not None
            and self.marks_awarded > self.marks_available
        ):
            raise ValueError("marks_awarded cannot exceed marks_available")
        return self


class ResponseImport(BaseModel):
    """A batch of responses for one course (and optionally one syllabus)."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    source: str = Field(default="manual_import", min_length=1, max_length=80)
    responses: list[ResponseSubmission] = Field(default_factory=list)


class ResponseRecord(BaseModel):
    """One stored, anonymised response."""

    model_config = ConfigDict(extra="forbid")

    response_id: str
    course_id: str
    syllabus_id: str
    question_id: str
    answer_text: str
    correctness: ResponseCorrectness
    marks_awarded: float | None = None
    marks_available: float | None = None
    group_ref: str | None = None
    identified: bool = Field(
        default=False,
        description=(
            "Whether the import carried a student or group reference. Only the fact is kept; "
            "the reference itself is hashed."
        ),
    )
    source: str = "manual_import"
    fingerprint: str
    imported_at: datetime = Field(default_factory=utcnow)

    def excerpt(self, limit: int = EXCERPT_CHARS) -> str:
        compact = " ".join(self.answer_text.split())
        return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"

    @property
    def word_count(self) -> int:
        return len(self.answer_text.split())


class MisconceptionEvidence(BaseModel):
    """Why one response supports a pattern: an excerpt, not the whole answer."""

    model_config = ConfigDict(extra="forbid")

    response_id: str
    question_id: str
    excerpt: str = Field(..., min_length=1, max_length=EXCERPT_CHARS + 1)
    marker: str
    method: str
    weight: float = Field(default=0.5, ge=0.0, le=1.0)


class MisconceptionRecord(BaseModel):
    """One evidence-bearing suspected misunderstanding."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISCONCEPTION_SCHEMA_VERSION
    misconception_id: str
    course_id: str
    syllabus_id: str
    kind: MisconceptionKind
    label: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    concept_ids: list[str] = Field(default_factory=list)
    concept_labels: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    evidence: list[MisconceptionEvidence] = Field(default_factory=list)
    support_count: int = Field(default=0, ge=0)
    distinct_questions: int = Field(default=0, ge=0)
    distinct_groups: int = Field(default=0, ge=0)
    detection_method: str = "rule_based_response_analysis"
    support_level: SupportLevel = SupportLevel.SINGLE_OBSERVATION
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    suggested_correction: str | None = None
    follow_up_questions: list[str] = Field(default_factory=list)
    review_status: MisconceptionStatus = MisconceptionStatus.CANDIDATE
    review_history: list[ReviewEvent] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("evidence")
    @classmethod
    def _evidence_matches_support(
        cls, value: list[MisconceptionEvidence]
    ) -> list[MisconceptionEvidence]:
        seen: set[str] = set()
        unique: list[MisconceptionEvidence] = []
        for item in value:
            if item.response_id in seen:
                continue
            seen.add(item.response_id)
            unique.append(item)
        return unique


class MisconceptionDiscard(BaseModel):
    """A response that was read but cannot support a misconception claim."""

    model_config = ConfigDict(extra="forbid")

    response_id: str
    question_id: str
    reason: str
    detail: str


class MisconceptionReport(BaseModel):
    """One analysis run, including what could not be concluded."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISCONCEPTION_SCHEMA_VERSION
    course_id: str
    syllabus_id: str
    responses_supplied: int = Field(default=0, ge=0)
    responses_stored: int = Field(default=0, ge=0)
    distinct_responses: int = Field(default=0, ge=0)
    duplicate_responses: int = Field(default=0, ge=0)
    questions_analysed: int = Field(default=0, ge=0)
    records: list[MisconceptionRecord] = Field(default_factory=list)
    discarded: list[MisconceptionDiscard] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Uncertainty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


class MisconceptionQuery(BaseModel):
    """Filters for reading stored misconception records."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    kind: MisconceptionKind | None = None
    status: MisconceptionStatus | None = None
    concept_id: str | None = None
    min_support: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class MutationScope(BaseModel):
    """The exact source scope copied into a mutation or adversarial report."""

    model_config = ConfigDict(extra="forbid")

    course_id: str
    syllabus_id: str
    source_question_id: str | None = None
    concept_ids: list[str] = Field(default_factory=list)
    concept_labels: list[str] = Field(default_factory=list)
    unit_id: str | None = None
    unit_label: str | None = None
    origin: AssessmentOrigin = AssessmentOrigin.MUTATION


__all__ = [
    "ADVERSARIAL_SCHEMA_VERSION",
    "EXCERPT_CHARS",
    "MIN_ANSWER_WORDS",
    "MISCONCEPTION_SCHEMA_VERSION",
    "MUTATION_SCHEMA_VERSION",
    "AdversarialCandidate",
    "AdversarialRequest",
    "MisconceptionDiscard",
    "MisconceptionEvidence",
    "MisconceptionQuery",
    "MisconceptionRecord",
    "MisconceptionReport",
    "MutationCandidate",
    "MutationRequest",
    "MutationScope",
    "ResponseImport",
    "ResponseRecord",
    "ResponseSubmission",
    "make_misconception_id",
    "response_ref",
]
