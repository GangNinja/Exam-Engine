"""Validated records for the Phase 5 persistable knowledge graph.

The graph deliberately sits above the existing records: ingestion provenance,
NLP concepts, syllabus mappings, question classifications and Phase 3
similarities keep their own identities. A graph node is an addressable view with
a deterministic id; an edge records why it exists, how it was extracted and
whether it has been reviewed.

No inferred edge is presented as a verified fact. Structural syllabus edges are
``explicit``; Phase 3 mappings and similarity edges are ``inferred``; prerequisite
ordering candidates are ``candidate`` until reviewed.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    GraphBuildStatus,
    GraphEdgeStatus,
    GraphNodeType,
    GraphRelationship,
)
from exam_engine.models.question import Uncertainty

GRAPH_SCHEMA_VERSION = "phase5-v1"


def _digest(payload: str, prefix: str) -> str:
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def make_node_id(node_type: GraphNodeType, node_key: str) -> str:
    """Stable id: rebuilding the same entity updates rather than duplicates it."""
    return _digest(f"{node_type.value}\x1f{node_key}", "nod_")


def make_edge_id(source_node_id: str, relationship: GraphRelationship, target_node_id: str) -> str:
    """One logical edge per source/type/target; evidence is merged on rebuild."""
    return _digest(f"{source_node_id}\x1f{relationship.value}\x1f{target_node_id}", "rel_")


class GraphNode(BaseModel):
    """A typed graph entity with its source identity and scope."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    node_type: GraphNodeType
    node_key: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    normalized_label: str | None = None
    course_id: str | None = None
    syllabus_id: str | None = None
    document_id: str | None = None
    question_id: str | None = None
    entity_id: str | None = Field(
        default=None,
        description="Original unit/concept/source identifier, kept for traceability.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class GraphEdge(BaseModel):
    """A directed relationship with its evidence and review status."""

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    course_id: str | None = None
    syllabus_id: str | None = None
    source_node_id: str
    target_node_id: str
    relationship: GraphRelationship
    status: GraphEdgeStatus = GraphEdgeStatus.INFERRED
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str = Field(..., min_length=1)
    evidence: list[str] = Field(default_factory=list)
    source_document_id: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    model_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("target_node_id")
    @classmethod
    def _reject_self_edge(cls, value: str, info) -> str:
        source = info.data.get("source_node_id")
        if source is not None and value == source:
            raise ValueError("graph edges must not be self-referential")
        return value


class GraphBuildReport(BaseModel):
    """Auditable result of building one course graph."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = GRAPH_SCHEMA_VERSION
    course_id: str
    status: GraphBuildStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int = Field(default=0, ge=0)
    node_count: int = Field(
        default=0, ge=0, description="Course-scoped nodes written for this course."
    )
    shared_node_count: int = Field(
        default=0,
        ge=0,
        description="Institution/subject nodes reused across courses.",
    )
    edge_count: int = Field(
        default=0, ge=0, description="Course-scoped edges written for this course."
    )
    shared_edge_count: int = Field(
        default=0,
        ge=0,
        description="Edges between shared institution/subject nodes, not owned by one course.",
    )
    preserved_edge_count: int = Field(
        default=0,
        ge=0,
        description="Phase 6 candidate edges retained because their endpoints still exist.",
    )
    nodes_by_type: dict[str, int] = Field(default_factory=dict)
    edges_by_relationship: dict[str, int] = Field(default_factory=dict)
    warnings: list[Uncertainty] = Field(default_factory=list)
    errors: list[Uncertainty] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is not GraphBuildStatus.FAILED


class GraphNeighborhood(BaseModel):
    """A bounded traversal result; includes the centre and every crossed edge."""

    model_config = ConfigDict(extra="forbid")

    center: GraphNode
    hops: int = Field(default=1, ge=0, le=10)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class GraphEvidence(BaseModel):
    """The edge plus both endpoint nodes and its source document, when known."""

    model_config = ConfigDict(extra="forbid")

    edge: GraphEdge
    source: GraphNode
    target: GraphNode
    source_document: GraphNode | None = None


class ConceptDuplicateCandidate(BaseModel):
    """A review candidate; it is never merged automatically."""

    model_config = ConfigDict(extra="forbid")

    course_id: str
    left_concept_id: str
    right_concept_id: str
    left_label: str
    right_label: str
    score: float = Field(..., ge=0.0, le=1.0)
    method: str
    reason: str


__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "ConceptDuplicateCandidate",
    "GraphBuildReport",
    "GraphEdge",
    "GraphEdgeStatus",
    "GraphEvidence",
    "GraphNeighborhood",
    "GraphNode",
    "GraphNodeType",
    "GraphRelationship",
    "make_edge_id",
    "make_node_id",
]
