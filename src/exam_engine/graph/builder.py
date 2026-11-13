"""Build a course-scoped knowledge graph from existing Phase 1-4 records.

This module never re-ingests or re-classifies material. It turns stored courses,
documents, syllabi, concepts, question mappings and similarities into typed
nodes and evidence-bearing edges. The builder is deterministic: every node and
edge id is derived from its stable source identity, and replacing a course graph
cannot accumulate duplicates.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from exam_engine.analytics.metadata import ExamPeriod
from exam_engine.config import GraphSettings
from exam_engine.models.course import CourseRecord, utcnow
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    ConceptOrigin,
    GraphBuildStatus,
    GraphEdgeStatus,
    GraphNodeType,
    GraphRelationship,
    MappingStatus,
    Severity,
    SimilarityKind,
)
from exam_engine.models.graph import GraphEdge, GraphNode, make_edge_id, make_node_id
from exam_engine.models.nlp import ConceptRecord
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import SyllabusRecord, SyllabusUnitRecord
from exam_engine.models.understanding import QuestionAnalysis, SimilarityMatch, UnitMatch
from exam_engine.nlp.concept_quality import concept_noise_reason
from exam_engine.nlp.keywords import normalize_term

_STATUS_PRIORITY = {
    GraphEdgeStatus.CANDIDATE: 0,
    GraphEdgeStatus.INFERRED: 1,
    GraphEdgeStatus.EXPLICIT: 2,
    GraphEdgeStatus.REVIEWED: 3,
    GraphEdgeStatus.VERIFIED: 4,
}


@dataclass
class GraphCourseData:
    course: CourseRecord
    documents: list[DocumentRecord]
    questions: list[QuestionRecord]
    syllabi: list[SyllabusRecord]
    concepts: list[ConceptRecord]
    analyses: dict[str, QuestionAnalysis]
    matches: dict[str, list[UnitMatch]]
    similarities: list[SimilarityMatch]
    periods: dict[str, ExamPeriod]


@dataclass
class GraphBuildOutput:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    warnings: list[Uncertainty]
    status: GraphBuildStatus


@dataclass
class _ConceptSeed:
    labels: Counter[str] = field(default_factory=Counter)
    origins: set[ConceptOrigin] = field(default_factory=set)
    unit_ids: set[str] = field(default_factory=set)
    document_ids: set[str] = field(default_factory=set)
    concept_ids: set[str] = field(default_factory=set)
    confidences: list[float] = field(default_factory=list)
    evidence: set[str] = field(default_factory=set)
    verified: bool = False


class GraphBuilder:
    """Pure builder for one course; persistence is the service's responsibility."""

    def __init__(self, settings: GraphSettings | None = None) -> None:
        self.settings = settings or GraphSettings()
        self._reset_state()

    def _reset_state(self) -> None:
        """Make one builder instance safe to reuse for another course."""
        self.now = utcnow()
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.warnings: list[Uncertainty] = []
        self._data: GraphCourseData | None = None
        self._course_id = ""
        self._institution_id = ""
        self._subject_id = ""
        self._course_node_id = ""
        self._document_nodes: dict[str, str] = {}
        self._syllabus_nodes: dict[str, str] = {}
        self._unit_nodes: dict[str, str] = {}
        self._topic_nodes: dict[tuple[str, str], str] = {}
        self._concept_nodes: dict[str, str] = {}
        self._question_nodes: dict[str, str] = {}
        self._node_questions: dict[str, str] = {}
        self._session_nodes: dict[str, str] = {}
        self._concept_seeds: dict[str, _ConceptSeed] = {}
        self._concept_syllabi: dict[str, set[str]] = defaultdict(set)
        self._concept_documents: dict[str, set[str]] = defaultdict(set)
        self._unit_by_id: dict[str, SyllabusUnitRecord] = {}
        self._syllabus_by_id: dict[str, SyllabusRecord] = {}
        self._unit_syllabus: dict[str, str] = {}
        self._syllabus_documents: dict[str, str] = {}
        self._question_documents: dict[str, str] = {}
        self._question_pages: dict[str, int | None] = {}
        self._question_concepts: dict[str, set[str]] = defaultdict(set)
        self._question_topics: dict[str, set[str]] = defaultdict(set)
        self._question_mapping_status: dict[str, GraphEdgeStatus] = {}
        self._ambiguous_mapping_questions = 0
        self._mapping_unassigned = 0

    def build(self, data: GraphCourseData) -> GraphBuildOutput:
        self._reset_state()
        self._data = data
        self._course_id = data.course.course_id
        self._add_course_nodes()
        self._add_documents()
        self._add_syllabus_structures()
        self._collect_concepts()
        self._add_concept_nodes()
        self._add_questions_and_sessions()
        self._add_question_mapping_edges()
        self._add_document_coverage()
        if self.settings.include_similarities:
            self._add_similarity_edges()
            self._add_related_concept_edges()
        self._add_prerequisite_edges()
        self._check_completeness()
        status = GraphBuildStatus.OK
        if not self.nodes:
            status = GraphBuildStatus.EMPTY
        elif any(item.severity is Severity.ERROR for item in self.warnings):
            status = GraphBuildStatus.PARTIAL
        return GraphBuildOutput(
            nodes=sorted(self.nodes.values(), key=lambda node: node.node_id),
            edges=sorted(self.edges.values(), key=lambda edge: edge.edge_id),
            warnings=self.warnings,
            status=status,
        )

    # -- nodes -------------------------------------------------------------

    def _add_node(
        self,
        node_type: GraphNodeType,
        key: str,
        label: str,
        *,
        normalized_label: str | None = None,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        document_id: str | None = None,
        question_id: str | None = None,
        entity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        node_id = make_node_id(node_type, key)
        existing = self.nodes.get(node_id)
        if existing is not None:
            if metadata:
                existing.metadata.update(metadata)
            existing.updated_at = self.now
            return node_id
        self.nodes[node_id] = GraphNode(
            node_id=node_id,
            node_type=node_type,
            node_key=key,
            label=label.strip() or key,
            normalized_label=normalized_label,
            course_id=course_id,
            syllabus_id=syllabus_id,
            document_id=document_id,
            question_id=question_id,
            entity_id=entity_id,
            metadata=metadata or {},
            created_at=self.now,
            updated_at=self.now,
        )
        return node_id

    def _add_edge(
        self,
        source_node_id: str,
        target_node_id: str,
        relationship: GraphRelationship,
        *,
        status: GraphEdgeStatus,
        method: str,
        confidence: float | None = None,
        evidence: list[str] | None = None,
        source_document_id: str | None = None,
        source_page: int | None = None,
        model_version: str | None = None,
        metadata: dict[str, Any] | None = None,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        shared: bool = False,
    ) -> str | None:
        if source_node_id == target_node_id:
            return None
        edge_id = make_edge_id(source_node_id, relationship, target_node_id)
        new_evidence = [item for item in (evidence or []) if item]
        existing = self.edges.get(edge_id)
        if existing is not None:
            existing.evidence = sorted(set(existing.evidence) | set(new_evidence))
            previous_status = existing.status
            if _STATUS_PRIORITY[status] > _STATUS_PRIORITY[previous_status]:
                existing.status = status
            # Structural/explicit syllabus facts do not inherit a model score
            # merely because a later inferred edge shares the same endpoints.
            if (
                status is GraphEdgeStatus.EXPLICIT
                and previous_status is not GraphEdgeStatus.EXPLICIT
            ):
                existing.confidence = None
            elif (
                status is not GraphEdgeStatus.EXPLICIT
                and previous_status is not GraphEdgeStatus.EXPLICIT
                and confidence is not None
                and (existing.confidence is None or confidence > existing.confidence)
            ):
                existing.confidence = confidence
            if existing.source_document_id is None:
                existing.source_document_id = source_document_id
            if existing.source_page is None:
                existing.source_page = source_page
            if existing.model_version is None:
                existing.model_version = model_version
            if metadata:
                _merge_metadata(existing.metadata, metadata)
            methods = {*existing.method.split("+"), method}
            existing.method = "+".join(sorted(methods))
            existing.updated_at = self.now
            return edge_id
        self.edges[edge_id] = GraphEdge(
            edge_id=edge_id,
            course_id=None if shared else (course_id if course_id is not None else self._course_id),
            syllabus_id=syllabus_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relationship=relationship,
            status=status,
            confidence=confidence,
            method=method,
            evidence=sorted(set(new_evidence)),
            source_document_id=source_document_id,
            source_page=source_page,
            model_version=model_version,
            metadata=metadata or {},
            created_at=self.now,
            updated_at=self.now,
        )
        return edge_id

    def _add_course_nodes(self) -> None:
        assert self._data is not None
        course = self._data.course
        self._institution_id = self._add_node(
            GraphNodeType.INSTITUTION,
            f"institution:{course.institution_code}",
            course.institution_name,
            normalized_label=normalize_term(course.institution_code),
            entity_id=course.institution_code,
            metadata={"institution_code": course.institution_code},
        )
        subject_key = (
            f"institution:{course.institution_code}|subject:{course.course_code.casefold()}"
        )
        self._subject_id = self._add_node(
            GraphNodeType.SUBJECT,
            subject_key,
            course.course_code,
            normalized_label=normalize_term(course.course_code),
            entity_id=course.course_code,
            metadata={
                "institution_code": course.institution_code,
                "course_code": course.course_code,
                "course_titles": [course.title],
            },
        )
        self._course_node_id = self._add_node(
            GraphNodeType.COURSE,
            course.course_id,
            course.title,
            normalized_label=normalize_term(f"{course.course_code} {course.title}"),
            course_id=course.course_id,
            entity_id=course.course_id,
            metadata={
                "course_code": course.course_code,
                "institution_code": course.institution_code,
                "academic_year": course.academic_year,
                "syllabus_version": course.syllabus_version,
            },
        )
        self._add_edge(
            self._subject_id,
            self._institution_id,
            GraphRelationship.BELONGS_TO,
            status=GraphEdgeStatus.EXPLICIT,
            method="phase1_course_metadata",
            # Both endpoints are shared across courses, so the edge cannot be
            # owned by whichever course happened to be built last.
            metadata={"scope": "institution_subject"},
            shared=True,
        )
        self._add_edge(
            self._course_node_id,
            self._institution_id,
            GraphRelationship.BELONGS_TO,
            status=GraphEdgeStatus.EXPLICIT,
            method="phase1_course_metadata",
        )
        self._add_edge(
            self._course_node_id,
            self._subject_id,
            GraphRelationship.BELONGS_TO,
            status=GraphEdgeStatus.EXPLICIT,
            method="phase1_course_metadata",
        )

    def _add_documents(self) -> None:
        assert self._data is not None
        for document in self._data.documents:
            node_id = self._add_node(
                GraphNodeType.SOURCE_DOCUMENT,
                document.document_id,
                document.title,
                normalized_label=normalize_term(document.title),
                course_id=self._course_id,
                document_id=document.document_id,
                entity_id=document.document_id,
                metadata={
                    "kind": document.kind.value,
                    "source_id": document.source_id,
                    "page_count": document.page_count,
                },
            )
            self._document_nodes[document.document_id] = node_id
            self._add_edge(
                node_id,
                self._course_node_id,
                GraphRelationship.BELONGS_TO,
                status=GraphEdgeStatus.EXPLICIT,
                method="phase1_document_provenance",
                source_document_id=document.document_id,
            )

    def _add_syllabus_structures(self) -> None:
        assert self._data is not None
        for syllabus in self._data.syllabi:
            self._syllabus_by_id[syllabus.syllabus_id] = syllabus
            self._syllabus_documents[syllabus.syllabus_id] = syllabus.document_id
            syllabus_label = syllabus.title or syllabus.document_id
            syllabus_node = self._add_node(
                GraphNodeType.SYLLABUS,
                syllabus.syllabus_id,
                syllabus_label,
                normalized_label=normalize_term(syllabus_label),
                course_id=self._course_id,
                syllabus_id=syllabus.syllabus_id,
                document_id=syllabus.document_id,
                entity_id=syllabus.syllabus_id,
                metadata={
                    "title": syllabus.title,
                    "document_id": syllabus.document_id,
                    "version": self._data.course.syllabus_version,
                    "confidence": syllabus.confidence,
                },
            )
            self._syllabus_nodes[syllabus.syllabus_id] = syllabus_node
            self._add_edge(
                self._course_node_id,
                syllabus_node,
                GraphRelationship.CONTAINS,
                status=GraphEdgeStatus.EXPLICIT,
                method="phase1_syllabus_provenance",
                evidence=[f"syllabus:{syllabus.syllabus_id}"],
                source_document_id=syllabus.document_id,
                syllabus_id=syllabus.syllabus_id,
            )
            self._add_edge(
                syllabus_node,
                self._course_node_id,
                GraphRelationship.BELONGS_TO,
                status=GraphEdgeStatus.EXPLICIT,
                method="phase1_syllabus_provenance",
                source_document_id=syllabus.document_id,
                syllabus_id=syllabus.syllabus_id,
            )
            if syllabus.document_id in self._document_nodes:
                self._add_edge(
                    syllabus_node,
                    self._document_nodes[syllabus.document_id],
                    GraphRelationship.DERIVED_FROM,
                    status=GraphEdgeStatus.EXPLICIT,
                    method="phase1_syllabus_provenance",
                    source_document_id=syllabus.document_id,
                    syllabus_id=syllabus.syllabus_id,
                )
            for unit in syllabus.units:
                unit_node = self._add_node(
                    GraphNodeType.UNIT,
                    unit.unit_id,
                    unit.unit_label,
                    normalized_label=normalize_term(f"{unit.unit_label} {unit.title or ''}"),
                    course_id=self._course_id,
                    syllabus_id=syllabus.syllabus_id,
                    entity_id=unit.unit_id,
                    metadata={
                        "unit_label": unit.unit_label,
                        "title": unit.title,
                        "hours": unit.hours,
                        "page_number": unit.page_number,
                        "topics": unit.topics,
                        "confidence": unit.confidence,
                    },
                )
                self._unit_by_id[unit.unit_id] = unit
                self._unit_syllabus[unit.unit_id] = syllabus.syllabus_id
                self._unit_nodes[unit.unit_id] = unit_node
                self._add_edge(
                    syllabus_node,
                    unit_node,
                    GraphRelationship.CONTAINS,
                    status=GraphEdgeStatus.EXPLICIT,
                    method="phase1_syllabus_structure",
                    evidence=[f"syllabus_unit:{unit.unit_id}"],
                    source_document_id=syllabus.document_id,
                    source_page=unit.page_number,
                    syllabus_id=syllabus.syllabus_id,
                )
                self._add_edge(
                    unit_node,
                    syllabus_node,
                    GraphRelationship.BELONGS_TO,
                    status=GraphEdgeStatus.EXPLICIT,
                    method="phase1_syllabus_structure",
                    source_document_id=syllabus.document_id,
                    source_page=unit.page_number,
                    syllabus_id=syllabus.syllabus_id,
                )
                self._add_edge(
                    unit_node,
                    syllabus_node,
                    GraphRelationship.DERIVED_FROM,
                    status=GraphEdgeStatus.EXPLICIT,
                    method="phase1_syllabus_structure",
                    source_document_id=syllabus.document_id,
                    source_page=unit.page_number,
                    syllabus_id=syllabus.syllabus_id,
                )
                seen_topics: set[str] = set()
                for position, topic in enumerate(unit.topics):
                    normalized = normalize_term(topic)
                    if not normalized or normalized in seen_topics:
                        continue
                    seen_topics.add(normalized)
                    topic_node = self._add_node(
                        GraphNodeType.TOPIC,
                        self._topic_key(unit.unit_id, normalized),
                        topic,
                        normalized_label=normalized,
                        course_id=self._course_id,
                        syllabus_id=syllabus.syllabus_id,
                        entity_id=normalized,
                        metadata={
                            "unit_id": unit.unit_id,
                            "unit_label": unit.unit_label,
                            "position": position,
                            "page_number": unit.page_number,
                        },
                    )
                    self._topic_nodes[(unit.unit_id, normalized)] = topic_node
                    self._add_edge(
                        unit_node,
                        topic_node,
                        GraphRelationship.CONTAINS,
                        status=GraphEdgeStatus.EXPLICIT,
                        method="phase1_syllabus_structure",
                        evidence=[f"{unit.unit_id}:{topic}"],
                        source_document_id=syllabus.document_id,
                        source_page=unit.page_number,
                        syllabus_id=syllabus.syllabus_id,
                    )
                    self._add_edge(
                        topic_node,
                        unit_node,
                        GraphRelationship.DERIVED_FROM,
                        status=GraphEdgeStatus.EXPLICIT,
                        method="phase1_syllabus_structure",
                        source_document_id=syllabus.document_id,
                        source_page=unit.page_number,
                        syllabus_id=syllabus.syllabus_id,
                    )

    def _topic_key(self, unit_id: str, normalized: str) -> str:
        return f"{self._course_id}|unit:{unit_id}|topic:{normalized}"

    def _add_concept_seed(
        self,
        label: str,
        *,
        origin: ConceptOrigin,
        verified: bool,
        unit_id: str | None = None,
        concept_id: str | None = None,
        confidence: float | None = None,
        evidence: str | None = None,
    ) -> None:
        normalized = normalize_term(label)
        if not normalized:
            return
        seed = self._concept_seeds.setdefault(normalized, _ConceptSeed())
        seed.labels[label.strip()] += 1
        seed.origins.add(origin)
        seed.verified = seed.verified or verified
        if unit_id:
            seed.unit_ids.add(unit_id)
        if concept_id:
            seed.concept_ids.add(concept_id)
        if confidence is not None:
            seed.confidences.append(confidence)
        if evidence:
            seed.evidence.add(evidence)

    def _collect_concepts(self) -> None:
        assert self._data is not None
        for unit in self._unit_by_id.values():
            for topic in unit.topics:
                if concept_noise_reason(topic) is not None:
                    continue
                self._add_concept_seed(
                    topic,
                    origin=ConceptOrigin.SYLLABUS,
                    verified=True,
                    unit_id=unit.unit_id,
                    evidence=f"syllabus_unit:{unit.unit_id}",
                )
        for concept in self._data.concepts:
            if concept.origin is ConceptOrigin.INFERRED and not (
                self.settings.include_inferred_concepts
            ):
                continue
            if concept_noise_reason(concept.term) is not None:
                continue
            self._add_concept_seed(
                concept.term,
                origin=concept.origin,
                verified=concept.verified,
                unit_id=concept.unit_id,
                concept_id=concept.concept_id,
                confidence=concept.confidence,
                evidence=concept.concept_id,
            )
            for item in concept.evidence:
                seed = self._concept_seeds.get(normalize_term(concept.term))
                if seed is not None:
                    seed.evidence.add(item)

    def _add_concept_nodes(self) -> None:
        for normalized, seed in sorted(self._concept_seeds.items()):
            label = sorted(
                seed.labels,
                key=lambda value: (-seed.labels[value], value.casefold(), value),
            )[0]
            confidence = round(max(seed.confidences), 4) if seed.confidences else None
            for unit_id in seed.unit_ids:
                syllabus_id = self._unit_syllabus.get(unit_id)
                document_id = self._syllabus_documents.get(syllabus_id or "")
                if document_id:
                    seed.document_ids.add(document_id)
            metadata = {
                "normalized": normalized,
                "aliases": sorted(seed.labels),
                "origins": sorted(origin.value for origin in seed.origins),
                "verified": seed.verified,
                "unit_ids": sorted(seed.unit_ids),
                "document_ids": sorted(seed.document_ids),
                "source_concept_ids": sorted(seed.concept_ids),
                "confidence": confidence,
                "evidence": sorted(seed.evidence)[:50],
            }
            node_id = self._add_node(
                GraphNodeType.CONCEPT,
                f"{self._course_id}|concept:{normalized}",
                label,
                normalized_label=normalized,
                course_id=self._course_id,
                entity_id=normalized,
                metadata=metadata,
            )
            self._concept_nodes[normalized] = node_id
            self._add_edge(
                node_id,
                self._course_node_id,
                GraphRelationship.BELONGS_TO,
                status=GraphEdgeStatus.EXPLICIT,
                method="phase2_concept_identity",
                evidence=[f"concept:{normalized}"],
            )
            for unit_id in sorted(seed.unit_ids):
                unit_node = self._unit_nodes.get(unit_id)
                if unit_node is None:
                    continue
                syllabus_id = self._unit_syllabus.get(unit_id)
                document_id = self._syllabus_documents.get(syllabus_id or "")
                explicit_topic = seed.verified and normalized in self._unit_topics(unit_id)
                self._add_edge(
                    unit_node,
                    node_id,
                    GraphRelationship.CONTAINS,
                    status=(
                        GraphEdgeStatus.EXPLICIT if explicit_topic else GraphEdgeStatus.INFERRED
                    ),
                    method=("syllabus_topic_identity" if explicit_topic else "phase2_concept"),
                    confidence=None if explicit_topic else confidence,
                    evidence=[f"concept:{normalized}", f"syllabus_unit:{unit_id}"],
                    syllabus_id=syllabus_id,
                    source_document_id=document_id,
                )
                if syllabus_id:
                    self._concept_syllabi[normalized].add(syllabus_id)
                    if document_id:
                        self._concept_documents[normalized].add(document_id)
            for syllabus_id in sorted(self._concept_syllabi.get(normalized, set())):
                syllabus_node = self._syllabus_nodes.get(syllabus_id)
                if syllabus_node is None:
                    continue
                syllabus_units = [
                    unit_id
                    for unit_id in seed.unit_ids
                    if self._unit_syllabus.get(unit_id) == syllabus_id
                ]
                explicit = seed.verified and any(
                    normalized in self._unit_topics(unit_id) for unit_id in syllabus_units
                )
                self._add_edge(
                    syllabus_node,
                    node_id,
                    GraphRelationship.CONTAINS,
                    status=GraphEdgeStatus.EXPLICIT if explicit else GraphEdgeStatus.INFERRED,
                    method="syllabus_topic_identity" if explicit else "phase2_concept",
                    confidence=None if explicit else confidence,
                    evidence=[f"concept:{normalized}"],
                    syllabus_id=syllabus_id,
                    source_document_id=self._syllabus_documents.get(syllabus_id),
                )
            explicit_documents = {
                self._syllabus_documents[unit_syllabus]
                for unit_id in seed.unit_ids
                if seed.verified and normalized in self._unit_topics(unit_id)
                for unit_syllabus in [self._unit_syllabus.get(unit_id)]
                if unit_syllabus in self._syllabus_documents
            }
            for document_id in sorted(self._concept_documents.get(normalized, set())):
                document_node = self._document_nodes.get(document_id)
                if document_node is None:
                    continue
                explicit = document_id in explicit_documents
                self._add_edge(
                    node_id,
                    document_node,
                    GraphRelationship.DERIVED_FROM,
                    status=(GraphEdgeStatus.EXPLICIT if explicit else GraphEdgeStatus.INFERRED),
                    method=(
                        "phase1_syllabus_provenance" if explicit else "phase2_concept_provenance"
                    ),
                    confidence=None if explicit else confidence,
                    evidence=[f"concept:{normalized}"],
                    source_document_id=document_id,
                )

    def _unit_topics(self, unit_id: str) -> set[str]:
        unit = self._unit_by_id.get(unit_id)
        if unit is None:
            return set()
        return {normalize_term(topic) for topic in unit.topics}

    def _node_syllabus(self, node_id: str) -> str | None:
        """Return a node's syllabus only when it belongs to exactly one version."""
        node = self.nodes.get(node_id)
        if node is None:
            return None
        unit_ids: set[str] = set()
        single_unit = node.metadata.get("unit_id")
        if isinstance(single_unit, str):
            unit_ids.add(single_unit)
        listed_units = node.metadata.get("unit_ids")
        if isinstance(listed_units, list):
            unit_ids.update(str(unit_id) for unit_id in listed_units)
        syllabi = {
            self._unit_syllabus[unit_id] for unit_id in unit_ids if unit_id in self._unit_syllabus
        }
        return next(iter(syllabi)) if len(syllabi) == 1 else None

    def _ensure_topic(
        self,
        unit_id: str,
        label: str,
        *,
        method: str = "phase3_mapping",
        source_document_id: str | None = None,
        source_page: int | None = None,
    ) -> str | None:
        if concept_noise_reason(label) is not None:
            return None
        normalized = normalize_term(label)
        if not normalized:
            return None
        unit_node = self._unit_nodes.get(unit_id)
        if unit_node is None:
            return None
        existing = self._topic_nodes.get((unit_id, normalized))
        if existing is not None:
            return existing
        unit = self._unit_by_id.get(unit_id)
        syllabus_id = self._unit_syllabus.get(unit_id)
        node_id = self._add_node(
            GraphNodeType.TOPIC,
            self._topic_key(unit_id, normalized),
            label,
            normalized_label=normalized,
            course_id=self._course_id,
            syllabus_id=syllabus_id,
            entity_id=normalized,
            metadata={
                "unit_id": unit_id,
                "unit_label": unit.unit_label if unit else None,
                "source": method,
                "source_document_id": source_document_id,
                "page_number": source_page,
            },
        )
        self._topic_nodes[(unit_id, normalized)] = node_id
        self._add_edge(
            unit_node,
            node_id,
            GraphRelationship.CONTAINS,
            status=GraphEdgeStatus.INFERRED,
            method=method,
            evidence=[f"{unit_id}:{label}"],
            syllabus_id=syllabus_id,
            source_document_id=source_document_id,
            source_page=source_page,
        )
        self._add_edge(
            node_id,
            unit_node,
            GraphRelationship.DERIVED_FROM,
            status=GraphEdgeStatus.INFERRED,
            method=method,
            evidence=[f"{unit_id}:{label}"],
            syllabus_id=syllabus_id,
            source_document_id=source_document_id,
            source_page=source_page,
        )
        document_node = self._document_nodes.get(source_document_id or "")
        if document_node is not None:
            self._add_edge(
                node_id,
                document_node,
                GraphRelationship.DERIVED_FROM,
                status=GraphEdgeStatus.INFERRED,
                method=method,
                evidence=[f"topic:{normalized}"],
                source_document_id=source_document_id,
                source_page=source_page,
                syllabus_id=syllabus_id,
            )
        return node_id

    def _ensure_concept(
        self,
        label: str,
        *,
        unit_id: str | None,
        confidence: float | None = None,
        source_document_id: str | None = None,
        source_page: int | None = None,
    ) -> str | None:
        normalized = normalize_term(label)
        if not normalized:
            return None
        existing = self._concept_nodes.get(normalized)
        if existing is not None:
            node = self.nodes.get(existing)
            if node is not None:
                self._extend_concept_metadata(
                    node,
                    label=label,
                    unit_id=unit_id,
                    document_id=source_document_id,
                    confidence=confidence,
                )
            self._attach_concept_scope(
                existing,
                normalized,
                unit_id=unit_id,
                confidence=confidence,
                source_document_id=source_document_id,
                source_page=source_page,
            )
            return existing
        seed = self._concept_seeds.setdefault(normalized, _ConceptSeed())
        seed.labels[label.strip()] += 1
        seed.origins.add(ConceptOrigin.INFERRED)
        seed.evidence.add(f"phase3_topic:{label}")
        if confidence is not None:
            seed.confidences.append(confidence)
        if unit_id:
            seed.unit_ids.add(unit_id)
        if source_document_id:
            seed.document_ids.add(source_document_id)
        node_id = self._add_node(
            GraphNodeType.CONCEPT,
            f"{self._course_id}|concept:{normalized}",
            label.strip(),
            normalized_label=normalized,
            course_id=self._course_id,
            entity_id=normalized,
            metadata={
                "normalized": normalized,
                "aliases": [label.strip()],
                "origins": [ConceptOrigin.INFERRED.value],
                "verified": False,
                "unit_ids": sorted(seed.unit_ids),
                "document_ids": sorted(seed.document_ids),
                "source_concept_ids": [],
                "confidence": confidence,
                "evidence": sorted(seed.evidence)[:50],
            },
        )
        self._concept_nodes[normalized] = node_id
        self._add_edge(
            node_id,
            self._course_node_id,
            GraphRelationship.BELONGS_TO,
            status=GraphEdgeStatus.INFERRED,
            method="phase3_mapping",
            evidence=[f"concept:{normalized}"],
        )
        self._attach_concept_scope(
            node_id,
            normalized,
            unit_id=unit_id,
            confidence=confidence,
            source_document_id=source_document_id,
            source_page=source_page,
        )
        return node_id

    def _extend_concept_metadata(
        self,
        node: GraphNode,
        *,
        label: str,
        unit_id: str | None,
        document_id: str | None,
        confidence: float | None,
    ) -> None:
        """Keep merged concepts traceable when a later mapping adds a scope."""
        aliases = {str(item) for item in node.metadata.get("aliases", [])}
        aliases.add(label.strip())
        node.metadata["aliases"] = sorted(aliases)
        if unit_id:
            units = {str(item) for item in node.metadata.get("unit_ids", [])}
            units.add(unit_id)
            node.metadata["unit_ids"] = sorted(units)
        if document_id:
            documents = {str(item) for item in node.metadata.get("document_ids", [])}
            documents.add(document_id)
            node.metadata["document_ids"] = sorted(documents)
        if confidence is not None:
            current = node.metadata.get("confidence")
            if not isinstance(current, (int, float)) or confidence > current:
                node.metadata["confidence"] = round(confidence, 4)
        evidence = {str(item) for item in node.metadata.get("evidence", [])}
        evidence.add("phase3_topic")
        node.metadata["evidence"] = sorted(evidence)[:50]
        node.updated_at = self.now

    def _attach_concept_scope(
        self,
        node_id: str,
        normalized: str,
        *,
        unit_id: str | None,
        confidence: float | None,
        source_document_id: str | None,
        source_page: int | None,
    ) -> None:
        """Attach a Phase 3 concept to its unit, syllabus and source document."""
        if unit_id:
            unit_node = self._unit_nodes.get(unit_id)
            if unit_node is not None:
                syllabus_id = self._unit_syllabus.get(unit_id)
                if syllabus_id:
                    self._concept_syllabi[normalized].add(syllabus_id)
                self._add_edge(
                    unit_node,
                    node_id,
                    GraphRelationship.CONTAINS,
                    status=GraphEdgeStatus.INFERRED,
                    method="phase3_mapping",
                    confidence=confidence,
                    evidence=[f"concept:{normalized}", f"syllabus_unit:{unit_id}"],
                    syllabus_id=syllabus_id,
                    source_document_id=source_document_id,
                    source_page=source_page,
                )
                syllabus_node = self._syllabus_nodes.get(syllabus_id or "")
                if syllabus_node is not None:
                    self._add_edge(
                        syllabus_node,
                        node_id,
                        GraphRelationship.CONTAINS,
                        status=GraphEdgeStatus.INFERRED,
                        method="phase3_mapping",
                        confidence=confidence,
                        evidence=[f"concept:{normalized}", f"syllabus_unit:{unit_id}"],
                        syllabus_id=syllabus_id,
                        source_document_id=source_document_id,
                        source_page=source_page,
                    )
        document_node = self._document_nodes.get(source_document_id or "")
        if document_node is not None:
            self._add_edge(
                node_id,
                document_node,
                GraphRelationship.DERIVED_FROM,
                status=GraphEdgeStatus.INFERRED,
                method="phase3_mapping",
                confidence=confidence,
                evidence=[f"concept:{normalized}"],
                source_document_id=source_document_id,
                source_page=source_page,
            )

    def _add_questions_and_sessions(self) -> None:
        assert self._data is not None
        for question in self._data.questions:
            analysis = self._data.analyses.get(question.question_id)
            metadata: dict[str, Any] = {
                "question_number": question.question_number,
                "section": question.section,
                "marks": question.marks,
                "page_number": question.page_number,
                "fingerprint": question.fingerprint,
                "question_id": question.question_id,
            }
            if analysis is not None:
                metadata.update(
                    {
                        "mapping_status": analysis.mapping_status.value,
                        "primary_type": (
                            analysis.primary_type.value if analysis.primary_type else None
                        ),
                        "difficulty": analysis.difficulty_label.value,
                        "bloom": (analysis.bloom_primary.value if analysis.bloom_primary else None),
                    }
                )
            label = (
                f"{question.question_number}. {question.text[:120]}"
                if question.question_number
                else question.text[:160]
            )
            node_id = self._add_node(
                GraphNodeType.QUESTION,
                question.question_id,
                label,
                normalized_label=None,
                course_id=self._course_id,
                document_id=question.document_id,
                question_id=question.question_id,
                entity_id=question.question_id,
                metadata={**metadata, "text": question.text[:500]},
            )
            self._question_nodes[question.question_id] = node_id
            self._node_questions[node_id] = question.question_id
            self._question_documents[question.question_id] = question.document_id
            self._question_pages[question.question_id] = question.page_number
            self._add_edge(
                node_id,
                self._course_node_id,
                GraphRelationship.BELONGS_TO,
                status=GraphEdgeStatus.EXPLICIT,
                method="phase1_question_provenance",
                source_document_id=question.document_id,
                source_page=question.page_number,
            )
            document_node = self._document_nodes.get(question.document_id)
            if document_node:
                self._add_edge(
                    node_id,
                    document_node,
                    GraphRelationship.APPEARS_IN,
                    status=GraphEdgeStatus.EXPLICIT,
                    method="phase1_question_provenance",
                    evidence=[f"question:{question.question_id}"],
                    source_document_id=question.document_id,
                    source_page=question.page_number,
                )
            self._add_session_edge(question)
        if not self._data.questions:
            self.warnings.append(
                _warning(
                    "no_questions",
                    "course has no questions; question nodes and assessment edges are empty",
                    Severity.INFO,
                )
            )

    def _add_session_edge(self, question: QuestionRecord) -> None:
        assert self._data is not None
        period = self._data.periods.get(question.document_id)
        if period is None or period.session_key is None or period.session is None:
            return
        session_key = period.session_key
        session_node = self._session_nodes.get(session_key)
        if session_node is None:
            session_node = self._add_node(
                GraphNodeType.EXAM_SESSION,
                f"{self._course_id}|session:{session_key}",
                period.label,
                normalized_label=normalize_term(period.session),
                course_id=self._course_id,
                entity_id=session_key,
                metadata={
                    "session": period.session,
                    "exam_type": period.exam_type,
                    "source": period.source,
                },
            )
            self._session_nodes[session_key] = session_node
            self._add_edge(
                session_node,
                self._course_node_id,
                GraphRelationship.BELONGS_TO,
                status=GraphEdgeStatus.EXPLICIT,
                method="phase4_exam_period",
                evidence=[f"exam_session:{period.session}"],
            )
        self._add_edge(
            self._question_nodes[question.question_id],
            session_node,
            GraphRelationship.APPEARS_IN,
            status=GraphEdgeStatus.EXPLICIT,
            method="phase4_exam_period",
            evidence=[f"session:{session_key}"],
            source_document_id=question.document_id,
            source_page=question.page_number,
        )

    def _add_question_mapping_edges(self) -> None:
        assert self._data is not None
        for question in self._data.questions:
            analysis = self._data.analyses.get(question.question_id)
            if analysis is None:
                continue
            matches = self._data.matches.get(question.question_id, [])
            assigned = (
                matches[:1]
                if analysis.mapping_status is MappingStatus.MAPPED
                else matches
                if analysis.mapping_status is MappingStatus.AMBIGUOUS
                else []
            )
            if analysis.mapping_status is MappingStatus.UNMAPPED:
                continue
            mapping_status = (
                GraphEdgeStatus.CANDIDATE
                if analysis.mapping_status is MappingStatus.AMBIGUOUS
                else GraphEdgeStatus.INFERRED
            )
            linked = 0
            for match in assigned:
                unit_syllabus_id = self._unit_syllabus.get(match.unit_id)
                if (
                    match.syllabus_id not in self._syllabus_nodes
                    or unit_syllabus_id != match.syllabus_id
                ):
                    # A stale mapping that points a unit at another syllabus
                    # version must not create a cross-version edge.
                    self._mapping_unassigned += 1
                    continue
                topic_id = self._ensure_topic(
                    match.unit_id,
                    match.topic or "",
                    source_document_id=question.document_id,
                    source_page=question.page_number,
                )
                if topic_id is None:
                    self._mapping_unassigned += 1
                    continue
                concept_id = self._ensure_concept(
                    match.topic or "",
                    unit_id=match.unit_id,
                    confidence=match.score,
                    source_document_id=question.document_id,
                    source_page=question.page_number,
                )
                if concept_id is None:
                    self._mapping_unassigned += 1
                    continue
                evidence = [
                    f"phase3_unit_match:{match.match_id}",
                    *[f"matched_term:{term}" for term in match.matched_terms],
                    *match.evidence,
                ]
                question_node = self._question_nodes[question.question_id]
                self._add_edge(
                    question_node,
                    topic_id,
                    GraphRelationship.COVERS,
                    status=mapping_status,
                    method=match.method,
                    confidence=match.score,
                    evidence=evidence,
                    source_document_id=question.document_id,
                    source_page=question.page_number,
                    model_version="phase3-mapping",
                    syllabus_id=match.syllabus_id,
                    metadata={
                        "mapping_status": analysis.mapping_status.value,
                        "match_id": match.match_id,
                    },
                )
                self._add_edge(
                    question_node,
                    concept_id,
                    GraphRelationship.ASSESSES,
                    status=mapping_status,
                    method=match.method,
                    confidence=match.score,
                    evidence=evidence,
                    source_document_id=question.document_id,
                    source_page=question.page_number,
                    model_version="phase3-mapping",
                    syllabus_id=match.syllabus_id,
                    metadata={
                        "mapping_status": analysis.mapping_status.value,
                        "match_id": match.match_id,
                    },
                )
                self._question_topics[question.question_id].add(topic_id)
                self._question_concepts[question.question_id].add(concept_id)
                linked += 1
            if linked:
                self._question_mapping_status[question.question_id] = mapping_status
                if mapping_status is GraphEdgeStatus.CANDIDATE:
                    self._ambiguous_mapping_questions += 1
        missing_analysis = [
            question
            for question in self._data.questions
            if question.question_id not in self._data.analyses
        ]
        if missing_analysis:
            if len(missing_analysis) == len(self._data.questions):
                message = (
                    "no Phase 3 question analysis was found; question assessment edges are empty"
                )
            else:
                message = (
                    f"{len(missing_analysis)} question(s) have no Phase 3 analysis; "
                    "their assessment edges are missing"
                )
            self.warnings.append(
                _warning(
                    "missing_analysis",
                    message,
                    Severity.INFO,
                    detail={
                        "count": len(missing_analysis),
                        "total": len(self._data.questions),
                    },
                )
            )

    def _add_document_coverage(self) -> None:
        for question_id, document_id in self._question_documents.items():
            document_node = self._document_nodes.get(document_id)
            if document_node is None:
                continue
            for target_id in sorted(
                self._question_topics.get(question_id, set())
                | self._question_concepts.get(question_id, set())
            ):
                self._add_edge(
                    document_node,
                    target_id,
                    GraphRelationship.COVERS,
                    method="aggregate_question_mapping",
                    evidence=[f"question:{question_id}"],
                    source_document_id=document_id,
                    syllabus_id=self._node_syllabus(target_id),
                    status=(
                        GraphEdgeStatus.CANDIDATE
                        if self._question_mapping_status.get(question_id)
                        is GraphEdgeStatus.CANDIDATE
                        else GraphEdgeStatus.INFERRED
                    ),
                    metadata={"question_ids": [question_id]},
                )

    def _add_similarity_edges(self) -> None:
        assert self._data is not None
        for similarity in self._data.similarities:
            left = self._question_nodes.get(similarity.question_id)
            right = self._question_nodes.get(similarity.matched_question_id)
            if left is None or right is None or left == right:
                continue
            source, target = sorted((left, right))
            # Endpoint sorting makes the edge deterministic, so source evidence
            # must be taken from the question that actually became the source.
            source_question_id = self._node_questions[source]
            evidence = [
                f"similarity_kind:{similarity.kind.value}",
                f"similarity_pair:{similarity.pair_id}",
                *similarity.evidence,
            ]
            self._add_edge(
                source,
                target,
                GraphRelationship.SIMILAR_TO,
                status=GraphEdgeStatus.INFERRED,
                method=similarity.method,
                confidence=similarity.score,
                evidence=evidence,
                source_document_id=self._question_documents.get(source_question_id),
                source_page=self._question_pages.get(source_question_id),
                model_version="phase3-similarity",
                metadata={
                    "similarity_kinds": [similarity.kind.value],
                    "pair_ids": [similarity.pair_id],
                    "question_ids": [
                        similarity.question_id,
                        similarity.matched_question_id,
                    ],
                    "document_ids": [
                        document_id
                        for document_id in (
                            self._question_documents.get(similarity.question_id),
                            self._question_documents.get(similarity.matched_question_id),
                        )
                        if document_id
                    ],
                    "lexical_score": similarity.lexical_score,
                    "semantic_score": similarity.semantic_score,
                    "confidence_source": "phase3_question_similarity",
                },
            )

    def _add_related_concept_edges(self) -> None:
        assert self._data is not None
        for similarity in self._data.similarities:
            if similarity.kind is not SimilarityKind.CONCEPT_RELATED:
                continue
            left_concepts = self._question_concepts.get(similarity.question_id, set())
            right_concepts = self._question_concepts.get(similarity.matched_question_id, set())
            mapping_statuses = [
                self._question_mapping_status.get(question_id, GraphEdgeStatus.INFERRED)
                for question_id in (similarity.question_id, similarity.matched_question_id)
            ]
            phase3_statuses = [
                analysis.mapping_status.value
                for question_id in (similarity.question_id, similarity.matched_question_id)
                if (analysis := self._data.analyses.get(question_id)) is not None
            ]
            status = (
                GraphEdgeStatus.CANDIDATE
                if any(item is GraphEdgeStatus.CANDIDATE for item in mapping_statuses)
                else GraphEdgeStatus.INFERRED
            )
            for left in sorted(left_concepts):
                for right in sorted(right_concepts):
                    if left == right:
                        continue
                    source, target = sorted((left, right))
                    common_syllabi = self._concept_syllabi.get(
                        self.nodes[source].normalized_label or "", set()
                    ) & self._concept_syllabi.get(self.nodes[target].normalized_label or "", set())
                    syllabus_scope = (
                        next(iter(common_syllabi)) if len(common_syllabi) == 1 else None
                    )
                    self._add_edge(
                        source,
                        target,
                        GraphRelationship.RELATED_TO,
                        status=status,
                        method="phase3_concept_related",
                        confidence=similarity.score,
                        evidence=[
                            f"similarity_pair:{similarity.pair_id}",
                            f"question:{similarity.question_id}",
                            f"question:{similarity.matched_question_id}",
                            *[f"mapping_status:{item.value}" for item in mapping_statuses],
                        ],
                        source_document_id=self._question_documents.get(similarity.question_id),
                        model_version="phase3-similarity",
                        syllabus_id=syllabus_scope,
                        metadata={
                            "similarity_kind": similarity.kind.value,
                            "pair_ids": [similarity.pair_id],
                            "question_ids": [
                                similarity.question_id,
                                similarity.matched_question_id,
                            ],
                            "mapping_statuses": [item.value for item in mapping_statuses],
                            "phase3_mapping_statuses": phase3_statuses,
                            "document_ids": [
                                document_id
                                for document_id in (
                                    self._question_documents.get(similarity.question_id),
                                    self._question_documents.get(similarity.matched_question_id),
                                )
                                if document_id
                            ],
                            "confidence_source": "phase3_question_similarity",
                        },
                    )

    def _add_prerequisite_edges(self) -> None:
        assert self._data is not None
        for unit in self._unit_by_id.values():
            self._add_explicit_prerequisites(unit)
        if not self.settings.infer_prerequisite_candidates:
            return
        for unit in sorted(self._unit_by_id.values(), key=lambda item: item.unit_id):
            ordered: list[str] = []
            for topic in unit.topics:
                concept = self._concept_nodes.get(normalize_term(topic))
                if concept is not None and concept not in ordered:
                    ordered.append(concept)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                self._add_edge(
                    previous,
                    current,
                    GraphRelationship.PREREQUISITE_OF,
                    status=GraphEdgeStatus.CANDIDATE,
                    method="syllabus_topic_order",
                    evidence=[f"syllabus_unit:{unit.unit_id}:topic_order"],
                    syllabus_id=self._unit_syllabus.get(unit.unit_id),
                    source_document_id=self._syllabus_documents.get(
                        self._unit_syllabus.get(unit.unit_id, "")
                    ),
                    metadata={"validated": False, "warning": "syllabus order is not proof"},
                )

    def _add_explicit_prerequisites(self, unit: SyllabusUnitRecord) -> None:
        raw = unit.metadata.get("prerequisites")
        if raw is None:
            raw = unit.metadata.get("prerequisite_of")
        if raw is None:
            return
        targets: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for target, prerequisites in raw.items():
                if isinstance(prerequisites, str):
                    prerequisites = [prerequisites]
                if isinstance(prerequisites, (list, tuple)):
                    targets[str(target)] = [str(item) for item in prerequisites]
        elif isinstance(raw, (list, tuple)):
            # A bare prerequisite list does not identify which topic it belongs
            # to. Applying it to every topic would fabricate explicit edges, so
            # it is reported instead of guessed.
            self.warnings.append(
                _warning(
                    "ambiguous_prerequisites",
                    f"unit {unit.unit_id} has a prerequisite list without target topics; "
                    "no explicit prerequisite edges were created",
                    Severity.INFO,
                    detail={"unit_id": unit.unit_id, "raw_count": len(raw)},
                )
            )
            return
        if not targets:
            return
        syllabus_id = self._unit_syllabus.get(unit.unit_id)
        document_id = self._syllabus_documents.get(syllabus_id or "")
        for target_label, prerequisites in targets.items():
            target = self._concept_nodes.get(normalize_term(target_label))
            if target is None:
                continue
            for prerequisite in prerequisites:
                source = self._concept_nodes.get(normalize_term(prerequisite))
                if source is None or source == target:
                    continue
                self._add_edge(
                    source,
                    target,
                    GraphRelationship.PREREQUISITE_OF,
                    status=GraphEdgeStatus.EXPLICIT,
                    method="syllabus_metadata_prerequisites",
                    evidence=[f"syllabus_unit:{unit.unit_id}:prerequisites"],
                    syllabus_id=syllabus_id,
                    source_document_id=document_id,
                    metadata={"validated": True, "source": "syllabus_metadata"},
                )

    def _check_completeness(self) -> None:
        assert self._data is not None
        if not self._data.syllabi:
            self.warnings.append(
                _warning(
                    "no_syllabus",
                    "course has no stored syllabus; syllabus units and topics are unavailable",
                    Severity.INFO,
                )
            )
        if not self._concept_nodes:
            self.warnings.append(
                _warning(
                    "no_concepts",
                    "no syllabus topics or Phase 2 concepts were available; "
                    "concept queries are empty",
                    Severity.INFO,
                )
            )
        if self._mapping_unassigned:
            self.warnings.append(
                _warning(
                    "incomplete_question_mapping",
                    f"{self._mapping_unassigned} mapped candidate(s) could not be linked to a "
                    "syllabus topic; they remain unmapped in the graph",
                    Severity.INFO,
                    detail={"count": self._mapping_unassigned},
                )
            )
        if self._ambiguous_mapping_questions:
            self.warnings.append(
                _warning(
                    "ambiguous_question_mappings",
                    f"{self._ambiguous_mapping_questions} question(s) have ambiguous Phase 3 "
                    "mappings; their COVERS/ASSESSES edges are candidates, not inferred facts",
                    Severity.INFO,
                    detail={"count": self._ambiguous_mapping_questions},
                )
            )
        if self._data.questions and self._data.analyses and not self._question_topics:
            self.warnings.append(
                _warning(
                    "no_question_mappings",
                    "questions were analysed but no mapped topic/concept edges were created; "
                    "assessment queries are empty",
                    Severity.INFO,
                )
            )
        supported_questions = {
            question_id
            for question_id, status in self._question_mapping_status.items()
            if status is not GraphEdgeStatus.CANDIDATE
        }
        analysed_unmapped = [
            question
            for question in self._data.questions
            if question.question_id in self._data.analyses
            and question.question_id not in supported_questions
        ]
        if supported_questions and analysed_unmapped:
            self.warnings.append(
                _warning(
                    "unmapped_questions",
                    f"{len(analysed_unmapped)} analysed question(s) have no supported "
                    "topic/concept mapping; they remain in graph unmapped",
                    Severity.INFO,
                    detail={"count": len(analysed_unmapped)},
                )
            )
        if self._data.questions and not self._session_nodes:
            self.warnings.append(
                _warning(
                    "no_exam_sessions",
                    "no question has explicit examination-session metadata; session queries "
                    "and time comparisons are unavailable",
                    Severity.INFO,
                )
            )
        elif self._data.questions and self._session_nodes:
            missing_documents = sorted(
                {
                    question.document_id
                    for question in self._data.questions
                    if self._data.periods.get(question.document_id) is None
                    or self._data.periods[question.document_id].session_key is None
                }
            )
            if missing_documents:
                self.warnings.append(
                    _warning(
                        "missing_period_metadata",
                        f"{len(missing_documents)} document(s) have no explicit examination "
                        "session; their questions have no session node",
                        Severity.INFO,
                        detail={"document_ids": missing_documents},
                    )
                )
        if self._data.similarities and not self.settings.include_similarities:
            self.warnings.append(
                _warning(
                    "similarity_disabled",
                    "stored Phase 3 similarities were not used because similarity is disabled",
                    Severity.INFO,
                )
            )


def _warning(
    code: str,
    message: str,
    severity: Severity = Severity.WARNING,
    detail: dict[str, Any] | None = None,
) -> Uncertainty:
    return Uncertainty(code=code, message=message, severity=severity, detail=detail or {})


def _merge_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if key not in existing:
            existing[key] = value
            continue
        current = existing[key]
        if isinstance(current, list) and isinstance(value, list):
            combined = [*current]
            for item in value:
                if item not in combined:
                    combined.append(item)
            existing[key] = combined
        elif current != value:
            existing.setdefault("alternate_values", {})
            alternates = existing["alternate_values"]
            if isinstance(alternates, dict):
                bucket = alternates.setdefault(key, [current])
                if value not in bucket:
                    bucket.append(value)


__all__ = ["GraphBuilder", "GraphBuildOutput", "GraphCourseData"]
