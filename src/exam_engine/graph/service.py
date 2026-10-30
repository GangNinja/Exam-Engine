"""Persistent knowledge-graph construction and query service.

The graph is derived state: rebuilding a course replaces its nodes and edges
from the current Phase 1-4 rows. Shared institution/subject nodes survive course
rebuilds, while every edge is course-scoped and therefore cannot leak across
course boundaries.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Any, Literal

from sqlalchemy.orm import Session

from exam_engine.analytics.metadata import resolve_exam_period
from exam_engine.config import GraphSettings
from exam_engine.graph.builder import GraphBuilder, GraphCourseData
from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    GraphBuildStatus,
    GraphEdgeStatus,
    GraphNodeType,
    GraphRelationship,
    Severity,
    TextOwnerType,
)
from exam_engine.models.graph import (
    ConceptDuplicateCandidate,
    GraphBuildReport,
    GraphEdge,
    GraphEvidence,
    GraphNeighborhood,
    GraphNode,
)
from exam_engine.models.question import Uncertainty
from exam_engine.models.understanding import UnitMatch
from exam_engine.nlp.keywords import normalize_term
from exam_engine.storage.db import Database
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.nlp_repositories import (
    ConceptRepository,
    EmbeddingRepository,
    VectorStoreRepository,
)
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)

logger = logging.getLogger(__name__)

Direction = Literal["in", "out", "both"]

# Endpoint-type contracts for the built taxonomy. New relationship types can be
# added without touching this map; an unmapped relationship is left to the
# service that produced it.
_ALLOWED_ENDPOINTS: dict[GraphRelationship, set[tuple[GraphNodeType, GraphNodeType]]] = {
    GraphRelationship.CONTAINS: {
        (GraphNodeType.COURSE, GraphNodeType.SYLLABUS),
        (GraphNodeType.SYLLABUS, GraphNodeType.UNIT),
        (GraphNodeType.SYLLABUS, GraphNodeType.CONCEPT),
        (GraphNodeType.UNIT, GraphNodeType.TOPIC),
        (GraphNodeType.UNIT, GraphNodeType.CONCEPT),
    },
    GraphRelationship.COVERS: {
        (GraphNodeType.QUESTION, GraphNodeType.TOPIC),
        (GraphNodeType.QUESTION, GraphNodeType.CONCEPT),
        (GraphNodeType.SOURCE_DOCUMENT, GraphNodeType.TOPIC),
        (GraphNodeType.SOURCE_DOCUMENT, GraphNodeType.CONCEPT),
    },
    GraphRelationship.ASSESSES: {(GraphNodeType.QUESTION, GraphNodeType.CONCEPT)},
    GraphRelationship.APPEARS_IN: {
        (GraphNodeType.QUESTION, GraphNodeType.SOURCE_DOCUMENT),
        (GraphNodeType.QUESTION, GraphNodeType.EXAM_SESSION),
    },
    GraphRelationship.DERIVED_FROM: {
        (GraphNodeType.SYLLABUS, GraphNodeType.SOURCE_DOCUMENT),
        (GraphNodeType.UNIT, GraphNodeType.SYLLABUS),
        (GraphNodeType.TOPIC, GraphNodeType.UNIT),
        (GraphNodeType.TOPIC, GraphNodeType.SOURCE_DOCUMENT),
        (GraphNodeType.CONCEPT, GraphNodeType.SOURCE_DOCUMENT),
    },
    GraphRelationship.BELONGS_TO: {
        (GraphNodeType.SUBJECT, GraphNodeType.INSTITUTION),
        (GraphNodeType.COURSE, GraphNodeType.INSTITUTION),
        (GraphNodeType.COURSE, GraphNodeType.SUBJECT),
        (GraphNodeType.SOURCE_DOCUMENT, GraphNodeType.COURSE),
        (GraphNodeType.SYLLABUS, GraphNodeType.COURSE),
        (GraphNodeType.UNIT, GraphNodeType.SYLLABUS),
        (GraphNodeType.CONCEPT, GraphNodeType.COURSE),
        (GraphNodeType.QUESTION, GraphNodeType.COURSE),
        (GraphNodeType.EXAM_SESSION, GraphNodeType.COURSE),
    },
    GraphRelationship.SIMILAR_TO: {(GraphNodeType.QUESTION, GraphNodeType.QUESTION)},
    GraphRelationship.RELATED_TO: {(GraphNodeType.CONCEPT, GraphNodeType.CONCEPT)},
    GraphRelationship.PREREQUISITE_OF: {(GraphNodeType.CONCEPT, GraphNodeType.CONCEPT)},
}

# Promotion order for merged edges. Explicit means "stated by the syllabus" and
# ranks above inferred scores but below a recorded human review.
_EDGE_STATUS_PRIORITY: dict[GraphEdgeStatus, int] = {
    GraphEdgeStatus.CANDIDATE: 0,
    GraphEdgeStatus.INFERRED: 1,
    GraphEdgeStatus.EXPLICIT: 2,
    GraphEdgeStatus.REVIEWED: 3,
    GraphEdgeStatus.VERIFIED: 4,
}


def _merge_metadata(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge edge metadata without dropping prior evidence or review notes."""
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            combined = [*current]
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_metadata(current, value)
        else:
            merged[key] = value
    return merged


class GraphService:
    def __init__(self, database: Database, settings: GraphSettings | None = None) -> None:
        self.db = database
        self.settings = settings or GraphSettings()

    # -- build -------------------------------------------------------------

    def build(
        self, course_id: str | None = None, *, infer_prerequisites: bool | None = None
    ) -> list[GraphBuildReport]:
        """Build one course or every course; each course is a separate graph."""
        settings = (
            replace(
                self.settings,
                infer_prerequisite_candidates=infer_prerequisites,
            )
            if infer_prerequisites is not None
            else self.settings
        )
        if course_id is not None:
            course_ids = [course_id]
        else:
            with self.db.session() as session:
                course_ids = sorted(course.course_id for course in CourseRepository(session).list())
        return [self._build_course(cid, settings) for cid in course_ids]

    def build_course(
        self, course_id: str, *, infer_prerequisites: bool | None = None
    ) -> GraphBuildReport:
        settings = (
            replace(
                self.settings,
                infer_prerequisite_candidates=infer_prerequisites,
            )
            if infer_prerequisites is not None
            else self.settings
        )
        return self._build_course(course_id, settings)

    def _build_course(self, course_id: str, settings: GraphSettings) -> GraphBuildReport:
        started_at = utcnow()
        started = time.perf_counter()
        report = GraphBuildReport(
            course_id=course_id,
            status=GraphBuildStatus.FAILED,
            started_at=started_at,
        )
        try:
            with self.db.transaction() as session:
                data, load_warnings = self._load(session, course_id)
                output = GraphBuilder(settings).build(data)
                preserved_edges = GraphRepository(session).replace_course(
                    course_id, output.nodes, output.edges
                )
            report.status = output.status
            if not data.questions and not data.syllabi and not data.concepts:
                report.status = GraphBuildStatus.EMPTY
            report.warnings = _dedupe([*load_warnings, *output.warnings])
            course_nodes = [node for node in output.nodes if node.course_id == course_id]
            shared_nodes = [node for node in output.nodes if node.course_id is None]
            report.node_count = len(course_nodes)
            report.shared_node_count = len(shared_nodes)
            course_edges = [edge for edge in output.edges if edge.course_id == course_id]
            shared_edges = [edge for edge in output.edges if edge.course_id is None]
            report.edge_count = len(course_edges) + preserved_edges
            report.shared_edge_count = len(shared_edges)
            report.preserved_edge_count = preserved_edges
            report.nodes_by_type = dict(
                sorted(Counter(node.node_type.value for node in course_nodes).items())
            )
            relationship_counts = Counter(edge.relationship.value for edge in course_edges)
            if preserved_edges:
                relationship_counts[GraphRelationship.PREREQUISITE_OF.value] += preserved_edges
            report.edges_by_relationship = dict(sorted(relationship_counts.items()))
        except KeyError:
            raise
        except Exception as exc:  # noqa: BLE001 - report boundary; keep prior graph intact
            logger.error(
                "graph build failed for course %s: %s: %s",
                course_id,
                type(exc).__name__,
                exc,
            )
            report.errors.append(
                Uncertainty(
                    code="graph_build_failed",
                    message=f"graph build failed: {type(exc).__name__}: {exc}",
                    severity=Severity.ERROR,
                )
            )
        report.finished_at = utcnow()
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        return report

    def _load(self, session: Session, course_id: str) -> tuple[GraphCourseData, list[Uncertainty]]:
        warnings: list[Uncertainty] = []
        course = CourseRepository(session).get(course_id)
        if course is None:
            raise KeyError(f"unknown course: {course_id}")
        documents = DocumentRepository(session).list_by_course(course_id)
        questions = QuestionRepository(session).list_by_course(
            course_id, limit=self.settings.max_questions + 1
        )
        if len(questions) > self.settings.max_questions:
            warnings.append(
                _warning(
                    "graph_truncated",
                    f"only {self.settings.max_questions} of {len(questions)} questions were "
                    "included (EXAM_ENGINE_Q5_MAX_QUESTIONS)",
                    detail={"included": self.settings.max_questions, "total": len(questions)},
                )
            )
            questions = questions[: self.settings.max_questions]
        syllabi = SyllabusRepository(session).get_by_course(course_id)
        concepts = ConceptRepository(session).list_by_course(course_id)
        understanding = QuestionUnderstandingRepository(session)
        analyses = {
            analysis.question_id: analysis for analysis in understanding.list_analyses(course_id)
        }
        matches: dict[str, list[UnitMatch]] = defaultdict(list)
        for match in understanding.list_unit_matches_for_course(course_id):
            matches[match.question_id].append(match)
        similarities = understanding.list_similarities(course_id)
        if len(similarities) > self.settings.max_similarities:
            warnings.append(
                _warning(
                    "graph_truncated",
                    f"only {self.settings.max_similarities} of {len(similarities)} similarity "
                    "pairs were included (EXAM_ENGINE_Q5_MAX_SIMILARITIES)",
                    detail={
                        "included": self.settings.max_similarities,
                        "total": len(similarities),
                    },
                )
            )
            similarities = similarities[: self.settings.max_similarities]
        periods = {
            document.document_id: resolve_exam_period(document, course) for document in documents
        }
        return (
            GraphCourseData(
                course=course,
                documents=documents,
                questions=questions,
                syllabi=syllabi,
                concepts=concepts,
                analyses=analyses,
                matches=dict(matches),
                similarities=similarities,
                periods=periods,
            ),
            warnings,
        )

    # -- lifecycle ---------------------------------------------------------

    def reset(self, course_id: str | None = None) -> None:
        with self.db.transaction() as session:
            if course_id is not None and CourseRepository(session).get(course_id) is None:
                raise KeyError(f"unknown course: {course_id}")
            GraphRepository(session).clear(course_id)

    def status(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        with self.db.session() as session:
            courses = CourseRepository(session).list()
            repo = GraphRepository(session)
            for course in courses:
                rows.append(
                    {
                        "course_id": course.course_id,
                        "nodes": repo.count_nodes(course_id=course.course_id),
                        "edges": repo.count_edges(course_id=course.course_id),
                        "concepts": repo.count_nodes(
                            course_id=course.course_id, node_type=GraphNodeType.CONCEPT
                        ),
                        "questions": repo.count_nodes(
                            course_id=course.course_id, node_type=GraphNodeType.QUESTION
                        ),
                        "units": repo.count_nodes(
                            course_id=course.course_id, node_type=GraphNodeType.UNIT
                        ),
                        "topics": repo.count_nodes(
                            course_id=course.course_id, node_type=GraphNodeType.TOPIC
                        ),
                        "sessions": repo.count_nodes(
                            course_id=course.course_id, node_type=GraphNodeType.EXAM_SESSION
                        ),
                        "candidate_edges": repo.count_edges(
                            course_id=course.course_id,
                            status=GraphEdgeStatus.CANDIDATE,
                        ),
                        "dangling_edges": repo.has_dangling_edges(course_id=course.course_id),
                    }
                )
        return rows

    def validate(self, course_id: str | None = None) -> list[str]:
        """Check referential and scope integrity; no inferred value is upgraded."""
        with self.db.session() as session:
            repo = GraphRepository(session)
            problems: list[str] = []
            if repo.has_dangling_edges(course_id=course_id):
                problems.append("graph contains edge(s) whose node endpoint is missing")
            nodes: dict[str, GraphNode] = {}
            for edge in repo.list_edges(course_id=course_id):
                source = nodes.get(edge.source_node_id)
                if source is None:
                    source = repo.get_node(edge.source_node_id)
                    if source is not None:
                        nodes[source.node_id] = source
                target = nodes.get(edge.target_node_id)
                if target is None:
                    target = repo.get_node(edge.target_node_id)
                    if target is not None:
                        nodes[target.node_id] = target
                if source is None or target is None:
                    continue
                allowed = _ALLOWED_ENDPOINTS.get(edge.relationship)
                if allowed is not None and (source.node_type, target.node_type) not in allowed:
                    problems.append(
                        f"edge {edge.edge_id} has invalid {edge.relationship.value} endpoints "
                        f"({source.node_type.value} -> {target.node_type.value})"
                    )
                scopes = [source.course_id, target.course_id]
                if edge.course_id is None and any(scope is not None for scope in scopes):
                    problems.append(
                        f"edge {edge.edge_id} has no course scope but connects a scoped node"
                    )
                if any(
                    scope is not None and edge.course_id is not None and scope != edge.course_id
                    for scope in scopes
                ):
                    problems.append(
                        f"edge {edge.edge_id} crosses a course boundary "
                        f"({source.course_id or 'shared'} -> {target.course_id or 'shared'})"
                    )
                if (
                    source.syllabus_id is not None
                    and target.syllabus_id is not None
                    and source.syllabus_id != target.syllabus_id
                ):
                    problems.append(
                        f"edge {edge.edge_id} connects two syllabus versions "
                        f"({source.syllabus_id} -> {target.syllabus_id})"
                    )
                if edge.syllabus_id is not None:
                    for node in (source, target):
                        if node.syllabus_id is not None and node.syllabus_id != edge.syllabus_id:
                            problems.append(
                                f"edge {edge.edge_id} has syllabus scope {edge.syllabus_id} "
                                f"but node {node.node_id} is in {node.syllabus_id}"
                            )
                            break
            return problems

    def set_edge_status(
        self,
        edge_id: str,
        status: GraphEdgeStatus,
        *,
        evidence: list[str] | None = None,
    ) -> bool:
        with self.db.transaction() as session:
            return GraphRepository(session).update_edge_status(
                edge_id, status=status, evidence=evidence
            )

    def delete_edge(self, edge_id: str) -> bool:
        with self.db.transaction() as session:
            return GraphRepository(session).delete_edge(edge_id)

    def delete_node(self, node_id: str) -> bool:
        with self.db.transaction() as session:
            return GraphRepository(session).delete_node(node_id)

    def upsert_edge(self, edge: GraphEdge) -> bool:
        """Insert/merge one derived edge, preserving review state and audit trail.

        Returns ``True`` when the edge did not exist. This is the write primitive
        used by Phase 6 discovery; it never deletes an existing relationship.
        """
        with self.db.transaction() as session:
            repo = GraphRepository(session)
            existing = repo.get_edge(edge.edge_id)
            if existing is None:
                repo.upsert_edge(edge)
                return True

            merged = edge.model_copy(deep=True)
            merged.created_at = existing.created_at
            merged.evidence = sorted({*existing.evidence, *merged.evidence})
            methods = {*existing.method.split("+"), *merged.method.split("+")}
            merged.method = "+".join(sorted(methods))
            merged.metadata = _merge_metadata(existing.metadata, merged.metadata)

            if (
                existing.status
                in (
                    GraphEdgeStatus.REVIEWED,
                    GraphEdgeStatus.VERIFIED,
                )
                or _EDGE_STATUS_PRIORITY[merged.status] < _EDGE_STATUS_PRIORITY[existing.status]
            ):
                merged.status = existing.status

            # A later candidate proposal must never downgrade an explicit or
            # human-reviewed record's validation flags.
            if merged.status in (
                GraphEdgeStatus.EXPLICIT,
                GraphEdgeStatus.REVIEWED,
                GraphEdgeStatus.VERIFIED,
            ):
                merged.metadata["validated"] = True
                merged.metadata["review_required"] = False
                for key in ("reviewed_at", "reviewed_by", "review_note", "validated_by"):
                    if key in existing.metadata:
                        merged.metadata[key] = existing.metadata[key]
            else:
                merged.metadata.setdefault("validated", False)
                merged.metadata["review_required"] = True
            # Manual disable/enable decisions are audit decisions; rediscovery
            # must not silently reverse them. Re-enable through the explicit
            # command, which records the change in action_history.
            override = existing.metadata.get("manual_override")
            if override == "disabled" or (override is None and existing.metadata.get("disabled")):
                merged.metadata["disabled"] = True
            elif override == "enabled":
                merged.metadata["disabled"] = False

            # An explicit syllabus fact must not acquire a model score, even if
            # a later inferred edge proposes the same endpoints.
            if (
                existing.status is GraphEdgeStatus.EXPLICIT
                or merged.status is GraphEdgeStatus.EXPLICIT
                or merged.confidence is None
            ):
                merged.confidence = existing.confidence

            for field_name in (
                "source_document_id",
                "source_page",
                "model_version",
                "course_id",
                "syllabus_id",
            ):
                if getattr(merged, field_name) is None:
                    setattr(merged, field_name, getattr(existing, field_name))
            if merged.status in (GraphEdgeStatus.REVIEWED, GraphEdgeStatus.VERIFIED):
                merged.metadata.setdefault("reviewed_at", utcnow().isoformat())
            merged.updated_at = utcnow()
            repo.upsert_edge(merged)
            return False

    def set_edge_disabled(self, edge_id: str, *, disabled: bool, note: str | None = None) -> bool:
        """Disable/enable an edge without deleting it, keeping an audit trail."""
        with self.db.transaction() as session:
            repo = GraphRepository(session)
            edge = repo.get_edge(edge_id)
            if edge is None:
                return False
            metadata = dict(edge.metadata)
            metadata["disabled"] = disabled
            metadata["manual_override"] = "disabled" if disabled else "enabled"
            history = list(metadata.get("action_history") or [])
            history.append(
                {
                    "action": "disabled" if disabled else "enabled",
                    "at": utcnow().isoformat(),
                    "note": note or "",
                }
            )
            metadata["action_history"] = history[-50:]
            updated = edge.model_copy(update={"metadata": metadata, "updated_at": utcnow()})
            repo.upsert_edge(updated)
            return True

    def nodes(
        self,
        course_id: str,
        *,
        node_type: GraphNodeType | None = None,
        limit: int = 10000,
    ) -> list[GraphNode]:
        """Read course-scoped nodes without exposing the repository to callers."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.db.session() as session:
            return GraphRepository(session).list_nodes(
                course_id=course_id, node_type=node_type, limit=limit
            )

    def assessment_edges(
        self,
        course_id: str,
        *,
        syllabus_id: str | None = None,
        include_inferred: bool = False,
        include_candidates: bool = False,
        include_disabled: bool = False,
    ) -> list[GraphEdge]:
        """Return ASSESSES edges with explicit review/status filters.

        A supplied syllabus keeps edges scoped to that version and includes
        course-wide edges (``syllabus_id is None``). Callers that require strict
        version isolation must reject the unscoped rows when several versions
        exist; this query keeps them visible instead of silently dropping data.
        """
        allowed = {
            GraphEdgeStatus.EXPLICIT,
            GraphEdgeStatus.REVIEWED,
            GraphEdgeStatus.VERIFIED,
        }
        if include_inferred:
            allowed.add(GraphEdgeStatus.INFERRED)
        if include_candidates:
            allowed.add(GraphEdgeStatus.CANDIDATE)
        with self.db.session() as session:
            edges = GraphRepository(session).list_edges(
                course_id=course_id, relationship=GraphRelationship.ASSESSES
            )
        return [
            edge
            for edge in edges
            if edge.status in allowed
            and (include_disabled or not bool(edge.metadata.get("disabled")))
            and (syllabus_id is None or edge.syllabus_id is None or edge.syllabus_id == syllabus_id)
        ]

    def prerequisite_edges(
        self,
        course_id: str,
        *,
        syllabus_id: str | None = None,
        include_candidates: bool = True,
        include_inferred: bool = True,
        include_disabled: bool = False,
    ) -> list[GraphEdge]:
        """Return course-scoped PREREQUISITE_OF edges with explicit status filters."""
        allowed = {
            GraphEdgeStatus.EXPLICIT,
            GraphEdgeStatus.REVIEWED,
            GraphEdgeStatus.VERIFIED,
        }
        if include_inferred:
            allowed.add(GraphEdgeStatus.INFERRED)
        if include_candidates:
            allowed.add(GraphEdgeStatus.CANDIDATE)
        with self.db.session() as session:
            edges = GraphRepository(session).list_edges(
                course_id=course_id,
                syllabus_id=syllabus_id,
                relationship=GraphRelationship.PREREQUISITE_OF,
            )
        return [
            edge
            for edge in edges
            if edge.status in allowed
            and (include_disabled or not bool(edge.metadata.get("disabled")))
        ]

    # -- queries -----------------------------------------------------------

    def node(self, node_id: str) -> GraphNode:
        with self.db.session() as session:
            node = GraphRepository(session).get_node(node_id)
        if node is None:
            raise KeyError(f"unknown graph node: {node_id}")
        return node

    def find_nodes(
        self,
        course_id: str,
        query: str,
        *,
        node_type: GraphNodeType | None = None,
        limit: int = 20,
    ) -> list[GraphNode]:
        """Resolve a node id/entity id/label or label substring inside one course."""
        wanted = normalize_term(query)
        with self.db.session() as session:
            nodes = GraphRepository(session).list_nodes(course_id=course_id, node_type=node_type)
        exact_ids = [
            node
            for node in nodes
            if node.node_id == query
            or node.entity_id == query
            or node.question_id == query
            or node.document_id == query
        ]
        if exact_ids:
            return sorted(exact_ids, key=lambda item: (item.label.casefold(), item.node_id))[:limit]
        exact_labels = [node for node in nodes if wanted and normalize_term(node.label) == wanted]
        if exact_labels:
            return sorted(exact_labels, key=lambda item: (item.label.casefold(), item.node_id))[
                :limit
            ]
        if not wanted:
            return []
        return sorted(
            [node for node in nodes if wanted in normalize_term(node.label)],
            key=lambda item: (len(item.label), item.label.casefold(), item.node_id),
        )[:limit]

    def edge(self, edge_id: str) -> GraphEdge:
        with self.db.session() as session:
            edge = GraphRepository(session).get_edge(edge_id)
        if edge is None:
            raise KeyError(f"unknown graph edge: {edge_id}")
        return edge

    def concepts(
        self,
        course_id: str,
        *,
        unit_id: str | None = None,
        syllabus_id: str | None = None,
    ) -> list[GraphNode]:
        with self.db.session() as session:
            repo = GraphRepository(session)
            concepts = repo.list_nodes(
                course_id=course_id, node_type=GraphNodeType.CONCEPT, limit=10000
            )
            if unit_id is None and syllabus_id is None:
                return concepts
            allowed: set[str] | None = None
            if unit_id is not None:
                units = repo.list_nodes(
                    course_id=course_id,
                    node_type=GraphNodeType.UNIT,
                    entity_id=unit_id,
                    limit=1,
                )
                if not units:
                    return []
                edges = repo.neighbors(
                    units[0].node_id,
                    course_id=course_id,
                    relationships={GraphRelationship.CONTAINS},
                    direction="out",
                )
                allowed = {edge.target_node_id for edge in edges}
            if syllabus_id is not None:
                syllabi = repo.list_nodes(
                    course_id=course_id,
                    node_type=GraphNodeType.SYLLABUS,
                    syllabus_id=syllabus_id,
                    limit=10,
                )
                node_ids: set[str] = set()
                for syllabus in syllabi:
                    for edge in repo.neighbors(
                        syllabus.node_id,
                        course_id=course_id,
                        relationships={GraphRelationship.CONTAINS},
                        direction="out",
                    ):
                        node_ids.add(edge.target_node_id)
                allowed = node_ids if allowed is None else allowed & node_ids
            concepts = [node for node in concepts if allowed is None or node.node_id in allowed]
            return sorted(concepts, key=lambda node: (node.label.casefold(), node.node_id))

    def questions_for(
        self,
        course_id: str,
        *,
        topic_id: str | None = None,
        concept_id: str | None = None,
        syllabus_id: str | None = None,
        include_candidates: bool = False,
    ) -> list[GraphNode]:
        if (topic_id is None) == (concept_id is None):
            raise ValueError("provide exactly one of topic_id or concept_id")
        target_id = topic_id or concept_id
        relationship = (
            GraphRelationship.COVERS if topic_id is not None else GraphRelationship.ASSESSES
        )
        with self.db.session() as session:
            repo = GraphRepository(session)
            target = repo.get_node(target_id or "")
            if target is None or target.course_id not in (None, course_id):
                raise KeyError(f"unknown graph node: {target_id}")
            expected = GraphNodeType.TOPIC if topic_id is not None else GraphNodeType.CONCEPT
            if target.node_type is not expected:
                raise ValueError(
                    f"node {target_id} is a {target.node_type.value}, not a {expected.value}"
                )
            edges = repo.neighbors(
                target.node_id,
                course_id=course_id,
                syllabus_id=syllabus_id,
                relationships={relationship},
                direction="in",
            )
            nodes = []
            for edge in edges:
                if not include_candidates and edge.status is GraphEdgeStatus.CANDIDATE:
                    continue
                source = repo.get_node(edge.source_node_id)
                if source is not None and source.node_type is GraphNodeType.QUESTION:
                    nodes.append(source)
        return _unique_nodes(nodes)

    def topics_for_question(
        self,
        question_id: str,
        *,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        include_candidates: bool = False,
    ) -> list[GraphNode]:
        with self.db.session() as session:
            repo = GraphRepository(session)
            question = repo.list_nodes(
                course_id=course_id,
                node_type=GraphNodeType.QUESTION,
                question_id=question_id,
                limit=1,
            )
            if not question:
                return []
            edges = repo.neighbors(
                question[0].node_id,
                course_id=course_id,
                syllabus_id=syllabus_id,
                relationships={GraphRelationship.COVERS},
                direction="out",
            )
            nodes = _target_nodes(
                repo,
                edges,
                node_type=GraphNodeType.TOPIC,
                include_candidates=include_candidates,
            )
        return _unique_nodes(nodes)

    def concepts_for_question(
        self,
        question_id: str,
        *,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        include_candidates: bool = False,
    ) -> list[GraphNode]:
        with self.db.session() as session:
            repo = GraphRepository(session)
            question = repo.list_nodes(
                course_id=course_id,
                node_type=GraphNodeType.QUESTION,
                question_id=question_id,
                limit=1,
            )
            if not question:
                return []
            edges = repo.neighbors(
                question[0].node_id,
                course_id=course_id,
                syllabus_id=syllabus_id,
                relationships={GraphRelationship.ASSESSES},
                direction="out",
            )
            nodes = _target_nodes(
                repo,
                edges,
                node_type=GraphNodeType.CONCEPT,
                include_candidates=include_candidates,
            )
        return _unique_nodes(nodes)

    def topics_for_document(
        self,
        document_id: str,
        *,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        include_candidates: bool = False,
    ) -> list[GraphNode]:
        """Topics covered by a question paper or by a syllabus document.

        Question papers expose direct ``COVERS`` edges from their questions. A
        syllabus has no question mappings, so its topics are reached through the
        syllabus node (``syllabus -> unit -> topic``) linked by ``DERIVED_FROM``.
        """
        with self.db.session() as session:
            repo = GraphRepository(session)
            document = repo.list_nodes(
                course_id=course_id,
                node_type=GraphNodeType.SOURCE_DOCUMENT,
                document_id=document_id,
                limit=1,
            )
            if not document:
                return []
            nodes = _target_nodes(
                repo,
                repo.neighbors(
                    document[0].node_id,
                    course_id=course_id,
                    relationships={GraphRelationship.COVERS},
                    direction="out",
                ),
                node_type=GraphNodeType.TOPIC,
                include_candidates=include_candidates,
            )
            seen_syllabi: set[str] = set()
            for edge in repo.neighbors(
                document[0].node_id,
                course_id=course_id,
                relationships={GraphRelationship.DERIVED_FROM},
                direction="in",
            ):
                if not include_candidates and edge.status is GraphEdgeStatus.CANDIDATE:
                    continue
                syllabus = repo.get_node(edge.source_node_id)
                if (
                    syllabus is None
                    or syllabus.node_type is not GraphNodeType.SYLLABUS
                    or syllabus.node_id in seen_syllabi
                ):
                    continue
                seen_syllabi.add(syllabus.node_id)
                unit_edges = repo.neighbors(
                    syllabus.node_id,
                    course_id=course_id,
                    relationships={GraphRelationship.CONTAINS},
                    direction="out",
                )
                nodes.extend(
                    _target_nodes(
                        repo,
                        unit_edges,
                        node_type=GraphNodeType.TOPIC,
                        include_candidates=include_candidates,
                    )
                )
                for unit_edge in unit_edges:
                    if not include_candidates and unit_edge.status is GraphEdgeStatus.CANDIDATE:
                        continue
                    unit = repo.get_node(unit_edge.target_node_id)
                    if unit is None or unit.node_type is not GraphNodeType.UNIT:
                        continue
                    nodes.extend(
                        _target_nodes(
                            repo,
                            repo.neighbors(
                                unit.node_id,
                                course_id=course_id,
                                relationships={GraphRelationship.CONTAINS},
                                direction="out",
                            ),
                            node_type=GraphNodeType.TOPIC,
                            include_candidates=include_candidates,
                        )
                    )
        if syllabus_id is not None:
            nodes = [node for node in nodes if node.syllabus_id == syllabus_id]
        return _unique_nodes(nodes)

    def related_concepts(
        self,
        concept_id: str,
        *,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        min_confidence: float | None = None,
        include_candidates: bool = False,
    ) -> list[GraphEdge]:
        with self.db.session() as session:
            repo = GraphRepository(session)
            source = repo.get_node(concept_id)
            if source is None:
                raise KeyError(f"unknown graph node: {concept_id}")
            if source.node_type is not GraphNodeType.CONCEPT:
                raise ValueError(f"node {concept_id} is not a concept")
            if course_id is not None and source.course_id not in (None, course_id):
                raise ValueError("concept does not belong to the requested course")
            edges = [
                *repo.neighbors(
                    concept_id,
                    course_id=course_id,
                    syllabus_id=syllabus_id,
                    relationships={GraphRelationship.RELATED_TO},
                    direction="both",
                )
            ]
        result = [
            edge
            for edge in edges
            if (min_confidence is None or (edge.confidence or 0.0) >= min_confidence)
            and (include_candidates or edge.status is not GraphEdgeStatus.CANDIDATE)
        ]
        return sorted(
            result,
            key=lambda edge: (
                -(edge.confidence or 0.0),
                edge.edge_id,
            ),
        )

    def neighborhood(
        self,
        node_id: str,
        *,
        hops: int = 1,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        relationships: set[GraphRelationship] | None = None,
        direction: Direction = "both",
    ) -> GraphNeighborhood:
        if hops < 0 or hops > 10:
            raise ValueError("hops must be between 0 and 10")
        with self.db.session() as session:
            repo = GraphRepository(session)
            center = repo.get_node(node_id)
            if center is None:
                raise KeyError(f"unknown graph node: {node_id}")
            if course_id is not None and center.course_id not in (None, course_id):
                raise ValueError("node does not belong to the requested course")
            visited: dict[str, GraphNode] = {center.node_id: center}
            crossed: dict[str, GraphEdge] = {}
            frontier = [center]
            for _depth in range(hops):
                next_frontier: list[GraphNode] = []
                for node in frontier:
                    for edge in repo.neighbors(
                        node.node_id,
                        course_id=course_id,
                        syllabus_id=syllabus_id,
                        relationships=relationships,
                        direction=direction,
                    ):
                        crossed[edge.edge_id] = edge
                        if direction == "out":
                            other_id = edge.target_node_id
                        elif direction == "in":
                            other_id = edge.source_node_id
                        else:
                            other_id = (
                                edge.target_node_id
                                if edge.source_node_id == node.node_id
                                else edge.source_node_id
                            )
                        if other_id in visited:
                            continue
                        other = repo.get_node(other_id)
                        if other is None:
                            continue
                        visited[other_id] = other
                        next_frontier.append(other)
                frontier = next_frontier
                if not frontier:
                    break
        return GraphNeighborhood(
            center=center,
            hops=hops,
            nodes=sorted(visited.values(), key=lambda node: node.node_id),
            edges=sorted(crossed.values(), key=lambda edge: edge.edge_id),
        )

    def evidence_for_edge(self, edge_id: str) -> GraphEvidence:
        with self.db.session() as session:
            repo = GraphRepository(session)
            edge = repo.get_edge(edge_id)
            if edge is None:
                raise KeyError(f"unknown graph edge: {edge_id}")
            source = repo.get_node(edge.source_node_id)
            target = repo.get_node(edge.target_node_id)
            if source is None or target is None:
                raise ValueError(f"edge {edge_id} has a missing endpoint")
            document = None
            if edge.source_document_id:
                documents = repo.list_nodes(
                    node_type=GraphNodeType.SOURCE_DOCUMENT,
                    document_id=edge.source_document_id,
                    limit=1,
                )
                document = documents[0] if documents else None
        return GraphEvidence(edge=edge, source=source, target=target, source_document=document)

    def isolated_nodes(
        self, course_id: str, *, node_type: GraphNodeType | None = None
    ) -> list[GraphNode]:
        with self.db.session() as session:
            return GraphRepository(session).list_isolated_nodes(course_id, node_type=node_type)

    def questions_without_concepts(self, course_id: str) -> list[GraphNode]:
        with self.db.session() as session:
            repo = GraphRepository(session)
            questions = repo.list_nodes(
                course_id=course_id, node_type=GraphNodeType.QUESTION, limit=10000
            )
            assessed_questions: set[str] = set()
            for edge in repo.list_edges(
                course_id=course_id, relationship=GraphRelationship.ASSESSES
            ):
                # A candidate mapping is not sufficient support: the question
                # remains in the unmapped report until an inferred/explicit or
                # human-reviewed edge exists.
                if edge.status is not GraphEdgeStatus.CANDIDATE:
                    assessed_questions.add(edge.source_node_id)
            return [
                question for question in questions if question.node_id not in assessed_questions
            ]

    def find_concepts(
        self, normalized_label: str, *, course_id: str | None = None
    ) -> list[GraphNode]:
        with self.db.session() as session:
            return GraphRepository(session).list_nodes(
                course_id=course_id,
                node_type=GraphNodeType.CONCEPT,
                normalized_label=normalize_term(normalized_label),
                limit=10000,
            )

    def concept_duplicates(
        self, course_id: str, *, threshold: float | None = None
    ) -> list[ConceptDuplicateCandidate]:
        """Return review-only lexical/embedding candidates; nothing is merged."""
        cutoff = self.settings.concept_duplicate_threshold if threshold is None else threshold
        if not 0.0 <= cutoff <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        with self.db.session() as session:
            repo = GraphRepository(session)
            concepts = repo.list_nodes(
                course_id=course_id, node_type=GraphNodeType.CONCEPT, limit=10000
            )
            candidates = self._lexical_duplicate_candidates(course_id, concepts, cutoff)
            candidates.extend(
                self._embedding_duplicate_candidates(session, repo, course_id, concepts, cutoff)
            )
        return sorted(
            candidates,
            key=lambda item: (
                -item.score,
                item.left_label.casefold(),
                item.right_label.casefold(),
                item.method,
            ),
        )

    @staticmethod
    def _lexical_duplicate_candidates(
        course_id: str, concepts: list[GraphNode], cutoff: float
    ) -> list[ConceptDuplicateCandidate]:
        candidates: list[ConceptDuplicateCandidate] = []
        for index, left in enumerate(concepts):
            for right in concepts[index + 1 :]:
                left_label = left.normalized_label or normalize_term(left.label)
                right_label = right.normalized_label or normalize_term(right.label)
                if left_label == right_label:
                    continue
                score = SequenceMatcher(None, left_label, right_label).ratio()
                if score < cutoff:
                    continue
                candidates.append(
                    ConceptDuplicateCandidate(
                        course_id=course_id,
                        left_concept_id=left.node_id,
                        right_concept_id=right.node_id,
                        left_label=left.label,
                        right_label=right.label,
                        score=round(score, 4),
                        method="lexical_label_similarity",
                        reason="similar labels; review before any merge",
                    )
                )
        return candidates

    def _embedding_duplicate_candidates(
        self,
        session: Session,
        repo: GraphRepository,
        course_id: str,
        concepts: list[GraphNode],
        cutoff: float,
    ) -> list[ConceptDuplicateCandidate]:
        """Compare concepts through the mean embedding of questions assessing them.

        Phase 2 emits question-level embeddings, not concept-level vectors. The
        mean of supported question vectors is therefore a review signal only,
        never an automatic merge.
        """
        question_vectors: dict[str, dict[str, list[float]]] = defaultdict(dict)
        store = VectorStoreRepository(session)
        for record in EmbeddingRepository(session).list_by_course(course_id):
            if record.owner_type is not TextOwnerType.QUESTION:
                continue
            vector = store.get(record.model_id, record.text_sha256)
            if vector:
                question_vectors[record.owner_id][record.model_id] = vector
        if not question_vectors:
            return []

        concept_vectors: dict[str, dict[str, list[list[float]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for edge in repo.list_edges(course_id=course_id, relationship=GraphRelationship.ASSESSES):
            if edge.status is GraphEdgeStatus.CANDIDATE:
                continue
            question = repo.get_node(edge.source_node_id)
            if question is None or question.question_id is None:
                continue
            for model_id, vector in question_vectors.get(question.question_id, {}).items():
                concept_vectors[edge.target_node_id][model_id].append(vector)
        if not concept_vectors:
            return []

        by_id = {concept.node_id: concept for concept in concepts}
        best: dict[tuple[str, str], tuple[float, str]] = {}
        for index, left in enumerate(concepts):
            left_models = concept_vectors.get(left.node_id, {})
            if not left_models:
                continue
            for right in concepts[index + 1 :]:
                right_models = concept_vectors.get(right.node_id, {})
                for model_id in sorted(set(left_models) & set(right_models)):
                    left_vector = _mean_vector(left_models[model_id])
                    right_vector = _mean_vector(right_models[model_id])
                    score = _cosine_similarity(left_vector, right_vector)
                    if score < cutoff:
                        continue
                    key = (left.node_id, right.node_id)
                    current = best.get(key)
                    if current is None or score > current[0]:
                        best[key] = (score, model_id)
        candidates: list[ConceptDuplicateCandidate] = []
        for (left_id, right_id), (score, model_id) in best.items():
            left = by_id[left_id]
            right = by_id[right_id]
            candidates.append(
                ConceptDuplicateCandidate(
                    course_id=course_id,
                    left_concept_id=left.node_id,
                    right_concept_id=right.node_id,
                    left_label=left.label,
                    right_label=right.label,
                    score=round(min(1.0, max(0.0, score)), 4),
                    method="embedding_cosine",
                    reason=f"mean question-embedding cosine using {model_id}; review before merge",
                )
            )
        return candidates


def _warning(
    code: str,
    message: str,
    severity: Severity = Severity.WARNING,
    detail: dict[str, Any] | None = None,
) -> Uncertainty:
    return Uncertainty(code=code, message=message, severity=severity, detail=detail or {})


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    usable = [vector for vector in vectors if len(vector) == dim]
    if not usable:
        return []
    return [sum(vector[index] for vector in usable) / len(usable) for index in range(dim)]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _target_nodes(
    repo: GraphRepository,
    edges: list[GraphEdge],
    *,
    node_type: GraphNodeType,
    include_candidates: bool,
) -> list[GraphNode]:
    """Resolve edge targets of one type, excluding unreviewed candidates by default."""
    nodes: list[GraphNode] = []
    for edge in edges:
        if not include_candidates and edge.status is GraphEdgeStatus.CANDIDATE:
            continue
        node = repo.get_node(edge.target_node_id)
        if node is not None and node.node_type is node_type:
            nodes.append(node)
    return nodes


def _unique_nodes(nodes: list[GraphNode]) -> list[GraphNode]:
    by_id = {node.node_id: node for node in nodes}
    return sorted(by_id.values(), key=lambda node: (node.label.casefold(), node.node_id))


def _dedupe(warnings: list[Uncertainty]) -> list[Uncertainty]:
    seen: set[tuple[str, str]] = set()
    result: list[Uncertainty] = []
    for warning in warnings:
        key = (warning.code, warning.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return result


__all__ = ["GraphService"]
