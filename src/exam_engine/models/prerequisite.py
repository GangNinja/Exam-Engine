"""Models for Phase 6 prerequisite discovery and learning-path generation.

The vocabulary deliberately separates an *algorithmic candidate* from a
*verified* prerequisite. Candidate edges live in the Phase 5 graph and carry the
same provenance contract; nothing in this module promotes a candidate to a fact.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from exam_engine.models.course import utcnow
from exam_engine.models.enums import GraphEdgeStatus, GraphNodeType


class EvidenceSource(StrEnum):
    """Evidence sources kept distinguishable in candidate records."""

    PREEXISTING = "preexisting_relationship"
    SYLLABUS_EXPLICIT = "syllabus_explicit"
    SYLLABUS_ORDER = "syllabus_order"
    SYLLABUS_CONTAINMENT = "syllabus_containment"
    QUESTION_COVERAGE = "question_topic_coverage"
    QUESTION_CONCEPT = "question_concept_mapping"
    COOCCURRENCE = "question_cooccurrence"
    RELATED_CONCEPT = "related_concept_similarity"
    MANUAL_OVERRIDE = "manual_override"


class PrerequisiteEvidence(BaseModel):
    """One distinguishable piece of evidence behind a candidate prerequisite."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1)
    method: str = Field(..., min_length=1)
    detail: str = Field(..., min_length=1)
    source_document_id: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    method_version: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ConceptRef(BaseModel):
    """Small, serialisable concept reference with retained source scope."""

    model_config = ConfigDict(extra="forbid")

    concept_id: str
    label: str
    course_id: str | None = None
    syllabus_id: str | None = None
    provenance: dict[str, Any] = Field(
        default_factory=dict,
        description="Unit/document ids and aliases copied from the graph node when known.",
    )


class PrerequisiteRelationship(BaseModel):
    """A discovered candidate, or an existing graph edge, with its full evidence."""

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    source: ConceptRef
    target: ConceptRef
    status: GraphEdgeStatus
    method: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: list[PrerequisiteEvidence] = Field(default_factory=list)
    source_document_id: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    model_version: str | None = None
    limitations: list[str] = Field(default_factory=list)
    in_cycle: bool = False
    review_required: bool = True

    @property
    def is_supported(self) -> bool:
        """Whether the relationship may be used by a default learning path."""
        return self.status in (
            GraphEdgeStatus.EXPLICIT,
            GraphEdgeStatus.REVIEWED,
            GraphEdgeStatus.VERIFIED,
        )

    @property
    def is_candidate(self) -> bool:
        return self.status in (GraphEdgeStatus.CANDIDATE, GraphEdgeStatus.INFERRED)


class ConflictKind(StrEnum):
    """Conflict kinds reported by validation; none are silently repaired."""

    CYCLE = "cycle"
    ORDER_CONFLICT = "order_conflict"
    DUPLICATE = "duplicate_relationship"
    DISABLED_ENDPOINT = "disabled_endpoint"


class PrerequisiteConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1)
    concept_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    message: str = Field(..., min_length=1)
    blocking: bool = False
    resolution: str | None = None


class PrerequisiteCycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept_ids: list[str] = Field(..., min_length=2)
    labels: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    message: str = Field(..., min_length=1)
    resolved: bool = False


class PrerequisiteDiscoveryRequest(BaseModel):
    """Inputs for one deterministic discovery run."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    syllabus_id: str | None = None
    include_order_evidence: bool = True
    include_cooccurrence: bool = True
    include_question_evidence: bool = True
    max_cooccurrence_candidates: int = Field(default=100, ge=0, le=10000)
    persist: bool = True

    @field_validator("syllabus_id")
    @classmethod
    def _blank_syllabus_is_none(cls, value: str | None) -> str | None:
        return value or None


class PrerequisiteDiscoveryReport(BaseModel):
    """Result of a discovery run; candidates are never silently promoted."""

    model_config = ConfigDict(extra="forbid")

    course_id: str
    syllabus_id: str | None = None
    algorithm: str
    algorithm_version: str
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    nodes_scanned: int = 0
    edges_existing: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    existing_relationships: list[PrerequisiteRelationship] = Field(default_factory=list)
    candidates: list[PrerequisiteRelationship] = Field(default_factory=list)
    isolated_concepts: list[ConceptRef] = Field(default_factory=list)
    conflicts: list[PrerequisiteConflict] = Field(default_factory=list)
    cycles: list[PrerequisiteCycle] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        return sum(1 for candidate in self.candidates if candidate.is_candidate)


class PrerequisiteValidationReport(BaseModel):
    """Read-only integrity report over the prerequisite subgraph."""

    model_config = ConfigDict(extra="forbid")

    course_id: str
    syllabus_id: str | None = None
    algorithm: str = "phase6_prerequisite_validation"
    algorithm_version: str = "phase6.v1"
    config: dict[str, Any] = Field(default_factory=dict)
    relationship_count: int = 0
    candidate_count: int = 0
    supported_count: int = 0
    cycles: list[PrerequisiteCycle] = Field(default_factory=list)
    conflicts: list[PrerequisiteConflict] = Field(default_factory=list)
    isolated_concepts: list[ConceptRef] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.cycles and not any(item.blocking for item in self.conflicts)


class LearningPathStatus(StrEnum):
    """Learning-path outcomes, including honest fallbacks."""

    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_TARGET = "invalid_target"


class LearningPathRequest(BaseModel):
    """Target, learner starting point and explicit inclusion policy."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1)
    target_node_id: str = Field(..., min_length=1)
    target_node_type: GraphNodeType = GraphNodeType.CONCEPT
    syllabus_id: str | None = None
    known_concept_ids: list[str] = Field(default_factory=list)
    include_candidates: bool = False
    include_inferred: bool = False
    review_known: bool = False
    max_depth: int = Field(default=8, ge=1, le=32)
    max_path_length: int | None = Field(default=None, ge=1, le=200)

    @field_validator("target_node_type")
    @classmethod
    def _supported_target_type(cls, value: GraphNodeType) -> GraphNodeType:
        allowed = {GraphNodeType.CONCEPT, GraphNodeType.TOPIC, GraphNodeType.UNIT}
        if value not in allowed:
            raise ValueError("target_node_type must be concept, topic or unit")
        return value

    @field_validator("known_concept_ids")
    @classmethod
    def _dedupe_known(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in value if item.strip()))

    @field_validator("syllabus_id")
    @classmethod
    def _blank_syllabus_is_none(cls, value: str | None) -> str | None:
        return value or None


class PathStepRole(StrEnum):
    """Why a concept appears in the plan."""

    PREREQUISITE = "prerequisite"
    REVIEW = "review"
    TARGET = "target"


class LearningPathStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(..., ge=1)
    concept_id: str
    label: str
    role: str
    depth: int = Field(..., ge=0)
    direct: bool = Field(
        default=False, description="Prerequisite directly required by one of the targets."
    )
    status: GraphEdgeStatus | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str | None = None
    edge_ids: list[str] = Field(default_factory=list)
    evidence: list[PrerequisiteEvidence] = Field(default_factory=list)
    already_known: bool = False
    review_recommended: bool = False
    rationale: str = Field(..., min_length=1)


class LearningPathPlan(BaseModel):
    """Explainable ordered plan; ordering is dependency-based, not proven optimal."""

    model_config = ConfigDict(extra="forbid")

    course_id: str
    syllabus_id: str | None = None
    target_node_id: str
    target_node_type: GraphNodeType
    target_concepts: list[ConceptRef] = Field(default_factory=list)
    status: str
    steps: list[LearningPathStep] = Field(default_factory=list)
    known_concepts_skipped: list[ConceptRef] = Field(default_factory=list)
    unresolved_concepts: list[ConceptRef] = Field(default_factory=list)
    cycles: list[PrerequisiteCycle] = Field(default_factory=list)
    conflicts: list[PrerequisiteConflict] = Field(default_factory=list)
    missing_metadata: list[str] = Field(default_factory=list)
    algorithm: str
    algorithm_version: str
    config: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utcnow)
    limitations: list[str] = Field(default_factory=list)

    @property
    def ordered_concept_ids(self) -> list[str]:
        return [step.concept_id for step in self.steps]
