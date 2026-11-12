"""Phase 6 tests: prerequisite candidates, cycles, paths and isolation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exam_engine.config import (
    NlpSettings,
    PrerequisiteSettings,
    Settings,
    UnderstandingSettings,
)
from exam_engine.graph import GraphService
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    DocumentKind,
    ExtractionStatus,
    GraphEdgeStatus,
    GraphNodeType,
    GraphRelationship,
    SourceFormat,
)
from exam_engine.models.graph import GraphEdge, GraphNode, make_edge_id, make_node_id
from exam_engine.models.prerequisite import (
    LearningPathRequest,
    LearningPathStatus,
    PathStepRole,
    PrerequisiteDiscoveryRequest,
)
from exam_engine.models.question import QuestionRecord
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)
from exam_engine.nlp.service import NlpService
from exam_engine.prerequisite import LearningPathService, PrerequisiteDiscoveryService
from exam_engine.storage import graph_mappers
from exam_engine.storage.db import Database, memory_database
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from exam_engine.understanding.service import UnderstandingService
from tests.support import make_course, make_source

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_MANIFEST = PROJECT_ROOT / "datasets" / "analytics_sample" / "manifest.json"
PIPELINE_COURSE = "demo-analytics-cs201-2024-v1"


def _document(document_id: str, course_id: str) -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id="phase6-fixture",
        kind=DocumentKind.SYLLABUS,
        format=SourceFormat.TXT,
        title=document_id,
        original_filename=f"{document_id}.txt",
        content_sha256=hashlib.sha256(document_id.encode()).hexdigest(),
        byte_size=100,
        page_count=1,
        char_count=100,
        extraction_status=ExtractionStatus.OK,
        pipeline_version="phase6-test",
    )


def _syllabus(
    course_id: str,
    document_id: str,
    *,
    units: list[list[str]],
    prerequisites: dict[str, list[str]] | None = None,
) -> SyllabusRecord:
    syllabus_id = make_syllabus_id(document_id)
    records = []
    for index, topics in enumerate(units, start=1):
        unit_id = make_unit_id(syllabus_id, f"Unit {index}", " ".join(topics))
        metadata = {"prerequisites": prerequisites} if prerequisites else {}
        records.append(
            SyllabusUnitRecord(
                unit_id=unit_id,
                unit_label=f"Unit {index}",
                unit_number=index,
                title=f"Unit {index}",
                topics=topics,
                page_number=index,
                confidence=0.9,
                metadata=metadata,
            )
        )
    return SyllabusRecord(
        syllabus_id=syllabus_id,
        course_id=course_id,
        document_id=document_id,
        source_id="phase6-fixture",
        title="Synthetic prerequisite syllabus",
        units=records,
        confidence=0.9,
    )


def _concept(course_id: str, label: str, syllabus_id: str | None = None) -> GraphNode:
    normalized = label.casefold()
    scope = f"{course_id}|{syllabus_id or 'course'}|concept:{normalized}"
    return GraphNode(
        node_id=make_node_id(GraphNodeType.CONCEPT, scope),
        node_type=GraphNodeType.CONCEPT,
        node_key=scope,
        label=label,
        normalized_label=normalized,
        course_id=course_id,
        syllabus_id=syllabus_id,
        entity_id=f"c_{hashlib.sha256(normalized.encode()).hexdigest()[:8]}",
        metadata={"origins": ["syllabus"], "verified": True},
    )


def _question(course_id: str, question_id: str, label: str, syllabus_id: str | None) -> GraphNode:
    return GraphNode(
        node_id=make_node_id(GraphNodeType.QUESTION, question_id),
        node_type=GraphNodeType.QUESTION,
        node_key=question_id,
        label=label,
        normalized_label=label.casefold(),
        course_id=course_id,
        syllabus_id=syllabus_id,
        question_id=question_id,
        metadata={"number": question_id},
    )


def _edge(
    course_id: str,
    source: GraphNode,
    target: GraphNode,
    *,
    status: GraphEdgeStatus = GraphEdgeStatus.EXPLICIT,
    relationship: GraphRelationship = GraphRelationship.PREREQUISITE_OF,
    syllabus_id: str | None = None,
    method: str = "synthetic_fixture",
    confidence: float | None = None,
    evidence: list[str] | None = None,
    metadata: dict | None = None,
) -> GraphEdge:
    return GraphEdge(
        edge_id=make_edge_id(source.node_id, relationship, target.node_id),
        course_id=course_id,
        syllabus_id=syllabus_id,
        source_node_id=source.node_id,
        target_node_id=target.node_id,
        relationship=relationship,
        status=status,
        confidence=confidence,
        method=method,
        evidence=evidence or [f"fixture:{source.label}->{target.label}"],
        metadata=metadata
        or (
            {"validated": status is GraphEdgeStatus.EXPLICIT}
            if relationship is GraphRelationship.PREREQUISITE_OF
            else {}
        ),
    )


def _save(
    database: Database,
    course_id: str,
    *,
    syllabus: SyllabusRecord | None = None,
    questions: list[QuestionRecord] | None = None,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> None:
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id="phase6-fixture"))
        CourseRepository(session).upsert(
            make_course(course_id=course_id, course_code="CS600", title="Phase 6 fixture")
        )
        if syllabus is not None:
            DocumentRepository(session).upsert(_document(syllabus.document_id, course_id))
            SyllabusRepository(session).replace(syllabus)
        session.flush()
        if questions:
            by_document: dict[str, list[QuestionRecord]] = {}
            for question in questions:
                by_document.setdefault(question.document_id, []).append(question)
            for document_id, items in by_document.items():
                QuestionRepository(session).replace_for_document(document_id, items)
        session.flush()
        for node in nodes:
            session.add(graph_mappers.node_to_row(node))
        session.flush()
        for edge in edges:
            session.add(graph_mappers.edge_to_row(edge))


def _ordered_fixture(
    database: Database,
    *,
    course_id: str = "phase6-order-v1",
    units: list[list[str]] | None = None,
    prerequisites: dict[str, list[str]] | None = None,
    links: list[tuple[str, str, GraphEdgeStatus]] | None = None,
    with_questions: bool = False,
) -> tuple[str, SyllabusRecord, dict[str, GraphNode]]:
    units = units or [["Arrays", "Binary Search", "Trees"]]
    topic_labels = [topic for unit in units for topic in unit]
    course_id = course_id
    document_id = f"doc_syllabus_{course_id}"
    syllabus = _syllabus(course_id, document_id, units=units, prerequisites=prerequisites)
    nodes = [_concept(course_id, label, syllabus.syllabus_id) for label in topic_labels]
    by_label = {node.label: node for node in nodes}
    syllabus_node = GraphNode(
        node_id=make_node_id(GraphNodeType.SYLLABUS, f"{course_id}|{syllabus.syllabus_id}"),
        node_type=GraphNodeType.SYLLABUS,
        node_key=f"{course_id}|{syllabus.syllabus_id}",
        label=syllabus.title or syllabus.syllabus_id,
        normalized_label=(syllabus.title or syllabus.syllabus_id).casefold(),
        course_id=course_id,
        syllabus_id=syllabus.syllabus_id,
        entity_id=syllabus.syllabus_id,
    )
    edges = [
        _edge(
            course_id,
            syllabus_node,
            node,
            status=GraphEdgeStatus.EXPLICIT,
            relationship=GraphRelationship.CONTAINS,
            syllabus_id=syllabus.syllabus_id,
            method="phase1_syllabus_structure",
        )
        for node in nodes
    ]
    edges.extend(
        _edge(
            course_id,
            by_label[left],
            by_label[right],
            status=status,
            syllabus_id=syllabus.syllabus_id,
            metadata={"validated": status is GraphEdgeStatus.EXPLICIT},
        )
        for left, right, status in (links or [])
    )
    question_nodes = []
    question_records = []
    if with_questions:
        for index, question_id in enumerate(("q1", "q2"), start=1):
            question = _question(course_id, question_id, f"Question {index}", syllabus.syllabus_id)
            question_nodes.append(question)
            question_records.append(
                QuestionRecord(
                    question_id=question_id,
                    document_id=syllabus.document_id,
                    course_id=course_id,
                    text=f"Question {index} text",
                    question_number=str(index),
                    number_sort=index,
                    marks=5.0,
                    page_number=1,
                    confidence=0.9,
                )
            )
        for question in question_nodes:
            for label in topic_labels:
                edges.append(
                    _edge(
                        course_id,
                        question,
                        by_label[label],
                        status=GraphEdgeStatus.INFERRED,
                        relationship=GraphRelationship.ASSESSES,
                        syllabus_id=syllabus.syllabus_id,
                        confidence=0.8,
                        method="fixture_assessment",
                    )
                )
    _save(
        database,
        course_id,
        syllabus=syllabus,
        questions=question_records,
        nodes=[syllabus_node, *nodes, *question_nodes],
        edges=edges,
    )
    return course_id, syllabus, by_label


def _direct_fixture(
    database: Database,
    *,
    course_id: str = "phase6-direct-v1",
    labels: list[str],
    links: list[tuple[str, str, GraphEdgeStatus]] | None = None,
    syllabus_id: str | None = None,
) -> dict[str, GraphNode]:
    nodes = [_concept(course_id, label, syllabus_id) for label in labels]
    by_label = {node.label: node for node in nodes}
    edges = [
        _edge(
            course_id,
            by_label[left],
            by_label[right],
            status=status,
            syllabus_id=syllabus_id,
            metadata={"validated": status is GraphEdgeStatus.EXPLICIT},
        )
        for left, right, status in (links or [])
    ]
    _save(database, course_id, nodes=nodes, edges=edges)
    return by_label


def _discover(
    database: Database,
    course_id: str,
    *,
    syllabus_id: str | None = None,
    persist: bool = True,
    settings: PrerequisiteSettings | None = None,
):
    return PrerequisiteDiscoveryService(
        GraphService(database), settings or PrerequisiteSettings()
    ).discover(
        PrerequisiteDiscoveryRequest(course_id=course_id, syllabus_id=syllabus_id, persist=persist)
    )


def _plan(
    database: Database,
    course_id: str,
    target: GraphNode,
    *,
    known: list[str] | None = None,
    include_candidates: bool = False,
    include_inferred: bool = False,
    review_known: bool = False,
    max_depth: int = 8,
    max_path_length: int | None = None,
) -> object:
    return LearningPathService(GraphService(database)).generate(
        LearningPathRequest(
            course_id=course_id,
            target_node_id=target.node_id,
            known_concept_ids=known or [],
            include_candidates=include_candidates,
            include_inferred=include_inferred,
            review_known=review_known,
            max_depth=max_depth,
            max_path_length=max_path_length,
        )
    )


def test_discovery_creates_weak_candidates_and_is_idempotent() -> None:
    database = memory_database()
    try:
        course_id, syllabus, concepts = _ordered_fixture(database)
        report = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        assert report.edges_created == 3
        assert report.candidate_count == 3
        edges = GraphService(database).prerequisite_edges(
            course_id, syllabus_id=syllabus.syllabus_id, include_candidates=True
        )
        assert len(edges) == 3
        assert all(edge.status is GraphEdgeStatus.CANDIDATE for edge in edges)
        assert all(edge.confidence is None for edge in edges)
        assert all(edge.metadata["origin"] == "phase6_discovery" for edge in edges)
        assert all(edge.metadata["validated"] is False for edge in edges)
        assert all(edge.source_document_id == syllabus.document_id for edge in edges)
        assert all(edge.evidence for edge in edges)
        assert any(
            evidence.source == "syllabus_order"
            for candidate in report.candidates
            for evidence in candidate.evidence
        )
        assert all(candidate.review_required for candidate in report.candidates)

        again = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        assert again.edges_created == 0
        assert again.edges_updated == 3
        assert (
            len(
                GraphService(database).prerequisite_edges(
                    course_id, syllabus_id=syllabus.syllabus_id, include_candidates=True
                )
            )
            == 3
        )
        assert concepts["Arrays"].node_id
    finally:
        database.close()


def test_explicit_syllabus_prerequisite_is_not_promoted_or_scored() -> None:
    database = memory_database()
    try:
        course_id, syllabus, concepts = _ordered_fixture(
            database,
            units=[["Arrays", "Binary Search"]],
            prerequisites={"Binary Search": ["Arrays"]},
            links=[("Arrays", "Binary Search", GraphEdgeStatus.EXPLICIT)],
        )
        report = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        edge = GraphService(database).prerequisite_edges(
            course_id, syllabus_id=syllabus.syllabus_id, include_candidates=True
        )[0]
        assert edge.status is GraphEdgeStatus.EXPLICIT
        assert edge.confidence is None
        assert edge.metadata["validated"] is True
        assert report.candidates[0].status is GraphEdgeStatus.EXPLICIT
        assert report.candidates[0].review_required is False
        assert any(item.source == "syllabus_explicit" for item in report.candidates[0].evidence)
        plan = _plan(database, course_id, concepts["Binary Search"])
        assert plan.status == LearningPathStatus.OK.value
        assert [step.label for step in plan.steps] == ["Arrays", "Binary Search"]
    finally:
        database.close()


def test_reverse_supported_edge_disables_candidate_but_preserves_both() -> None:
    database = memory_database()
    try:
        course_id, syllabus, concepts = _ordered_fixture(
            database,
            units=[["Arrays", "Binary Search"]],
            links=[("Binary Search", "Arrays", GraphEdgeStatus.REVIEWED)],
        )
        report = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        edges = GraphService(database).prerequisite_edges(
            course_id,
            syllabus_id=syllabus.syllabus_id,
            include_candidates=True,
            include_disabled=True,
        )
        assert len(edges) == 2
        candidate = next(edge for edge in edges if edge.status is GraphEdgeStatus.CANDIDATE)
        assert candidate.metadata["disabled"] is True
        assert candidate.metadata["conflict_with"]
        assert report.conflicts
        assert report.cycles
        validation = PrerequisiteDiscoveryService(GraphService(database)).validate(
            course_id, syllabus_id=syllabus.syllabus_id
        )
        assert not validation.valid
        enabled = GraphService(database).prerequisite_edges(
            course_id, syllabus_id=syllabus.syllabus_id, include_candidates=True
        )
        assert len(enabled) == 1
        assert enabled[0].status is GraphEdgeStatus.REVIEWED
        assert concepts["Arrays"].node_id != concepts["Binary Search"].node_id
    finally:
        database.close()


def test_question_cooccurrence_enriches_order_but_never_invents_direction() -> None:
    database = memory_database()
    try:
        course_id, syllabus, _ = _ordered_fixture(
            database,
            units=[["Arrays", "Binary Search"]],
            with_questions=True,
        )
        report = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        assert len(report.candidates) == 1
        candidate = report.candidates[0]
        sources = {evidence.source for evidence in candidate.evidence}
        assert {"syllabus_order", "question_concept_mapping", "question_cooccurrence"} <= sources
        assert candidate.confidence is None
        assert candidate.source.label == "Arrays"
        assert candidate.target.label == "Binary Search"
        assert all(
            evidence.confidence is None
            for evidence in candidate.evidence
            if evidence.source != "related_concept_similarity"
        )
    finally:
        database.close()


def test_related_concept_evidence_is_supporting_but_not_directional_confidence() -> None:
    database = memory_database()
    try:
        course_id, syllabus, concepts = _ordered_fixture(
            database, units=[["Arrays", "Binary Search"]]
        )
        with database.transaction() as session:
            session.add(
                graph_mappers.edge_to_row(
                    _edge(
                        course_id,
                        concepts["Arrays"],
                        concepts["Binary Search"],
                        status=GraphEdgeStatus.INFERRED,
                        relationship=GraphRelationship.RELATED_TO,
                        syllabus_id=syllabus.syllabus_id,
                        method="phase3_concept_related",
                        confidence=0.7,
                    )
                )
            )
        report = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        candidate = report.candidates[0]
        assert any(
            evidence.source == "related_concept_similarity" and evidence.confidence == 0.7
            for evidence in candidate.evidence
        )
        assert candidate.confidence is None
    finally:
        database.close()


def test_manual_disable_survives_rediscovery() -> None:
    database = memory_database()
    try:
        course_id, syllabus, _ = _ordered_fixture(database, units=[["Arrays", "Binary Search"]])
        first = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        edge_id = first.candidates[0].edge_id
        service = GraphService(database)
        assert service.set_edge_disabled(edge_id, disabled=True, note="reviewer hold")
        _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        after = service.edge(edge_id)
        assert after.metadata["disabled"] is True
        assert after.metadata["action_history"][-1]["note"] == "reviewer hold"
        assert service.set_edge_disabled(edge_id, disabled=False, note="cleared")
        _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        reenabled = service.edge(edge_id)
        assert reenabled.metadata["disabled"] is False
        assert reenabled.metadata["manual_override"] == "enabled"
        assert reenabled.metadata["action_history"][-1]["note"] == "cleared"
    finally:
        database.close()


def test_unit_target_composes_contained_concepts_and_orders_them() -> None:
    database = memory_database()
    try:
        course_id = "phase6-unit-v1"
        concepts = _direct_fixture(
            database,
            course_id=course_id,
            labels=["Arrays", "Binary Search"],
            links=[("Arrays", "Binary Search", GraphEdgeStatus.EXPLICIT)],
        )
        unit = GraphNode(
            node_id=make_node_id(GraphNodeType.UNIT, f"{course_id}|unit:1"),
            node_type=GraphNodeType.UNIT,
            node_key=f"{course_id}|unit:1",
            label="Unit 1",
            normalized_label="unit 1",
            course_id=course_id,
            entity_id="unit_1",
        )
        _save(
            database,
            course_id,
            nodes=[unit],
            edges=[
                _edge(
                    course_id,
                    unit,
                    concepts["Arrays"],
                    relationship=GraphRelationship.CONTAINS,
                ),
                _edge(
                    course_id,
                    unit,
                    concepts["Binary Search"],
                    relationship=GraphRelationship.CONTAINS,
                ),
            ],
        )
        plan = LearningPathService(GraphService(database)).generate(
            LearningPathRequest(
                course_id=course_id,
                target_node_id=unit.node_id,
                target_node_type=GraphNodeType.UNIT,
            )
        )
        assert [step.label for step in plan.steps] == ["Arrays", "Binary Search"]
        assert {item.label for item in plan.target_concepts} == {
            "Arrays",
            "Binary Search",
        }
    finally:
        database.close()


def test_topic_target_composes_concepts_through_question_associations() -> None:
    database = memory_database()
    try:
        course_id = "phase6-topic-v1"
        concepts = _direct_fixture(
            database,
            course_id=course_id,
            labels=["Arrays", "Binary Search"],
            links=[("Arrays", "Binary Search", GraphEdgeStatus.EXPLICIT)],
        )
        syllabus = _syllabus(course_id, "doc_topic_questions", units=[["Arrays", "Binary Search"]])
        topic = GraphNode(
            node_id=make_node_id(GraphNodeType.TOPIC, f"{course_id}|topic:search"),
            node_type=GraphNodeType.TOPIC,
            node_key=f"{course_id}|topic:search",
            label="Searching",
            normalized_label="searching",
            course_id=course_id,
        )
        questions = [
            _question(course_id, "q_topic_1", "Question 1", None),
            _question(course_id, "q_topic_2", "Question 2", None),
        ]
        records = [
            QuestionRecord(
                question_id=question.question_id or "",
                document_id=syllabus.document_id,
                course_id=course_id,
                text="Topic fixture question",
                question_number=str(index),
                number_sort=index,
                marks=5.0,
                page_number=1,
                confidence=0.9,
            )
            for index, question in enumerate(questions, start=1)
        ]
        edges = [
            _edge(
                course_id,
                question,
                topic,
                status=GraphEdgeStatus.INFERRED,
                relationship=GraphRelationship.COVERS,
                method="fixture_topic_coverage",
            )
            for question in questions
        ]
        edges.extend(
            [
                _edge(
                    course_id,
                    questions[0],
                    concepts["Arrays"],
                    status=GraphEdgeStatus.INFERRED,
                    relationship=GraphRelationship.ASSESSES,
                    method="fixture_assessment",
                ),
                _edge(
                    course_id,
                    questions[0],
                    concepts["Binary Search"],
                    status=GraphEdgeStatus.INFERRED,
                    relationship=GraphRelationship.ASSESSES,
                    method="fixture_assessment",
                ),
                _edge(
                    course_id,
                    questions[1],
                    concepts["Arrays"],
                    status=GraphEdgeStatus.INFERRED,
                    relationship=GraphRelationship.ASSESSES,
                    method="fixture_assessment",
                ),
            ]
        )
        _save(
            database,
            course_id,
            syllabus=syllabus,
            questions=records,
            nodes=[topic, *questions],
            edges=edges,
        )
        plan = LearningPathService(GraphService(database)).generate(
            LearningPathRequest(
                course_id=course_id,
                target_node_id=topic.node_id,
                target_node_type=GraphNodeType.TOPIC,
            )
        )
        assert {item.label for item in plan.target_concepts} == {
            "Arrays",
            "Binary Search",
        }
        assert [step.label for step in plan.steps] == ["Arrays", "Binary Search"]
        assert any("indirect" in item for item in plan.limitations)
        wrong_type = LearningPathService(GraphService(database)).generate(
            LearningPathRequest(
                course_id=course_id,
                target_node_id=topic.node_id,
                target_node_type=GraphNodeType.CONCEPT,
            )
        )
        assert wrong_type.status == LearningPathStatus.INVALID_TARGET.value
    finally:
        database.close()


def test_syllabus_versions_are_isolated_for_discovery_and_paths() -> None:
    database = memory_database()
    try:
        course_id = "phase6-versions-v1"
        course = make_course(course_id=course_id, course_code="CS700")
        first_syllabus = _syllabus(
            course_id, "doc_version_one", units=[["Arrays", "Binary Search"]]
        )
        second_syllabus = _syllabus(course_id, "doc_version_two", units=[["Arrays", "Graphs"]])
        first_nodes = [
            _concept(course_id, "Arrays", first_syllabus.syllabus_id),
            _concept(course_id, "Binary Search", first_syllabus.syllabus_id),
        ]
        second_nodes = [
            _concept(course_id, "Arrays", second_syllabus.syllabus_id),
            _concept(course_id, "Graphs", second_syllabus.syllabus_id),
        ]

        def syllabus_node(syllabus: SyllabusRecord) -> GraphNode:
            return GraphNode(
                node_id=make_node_id(GraphNodeType.SYLLABUS, f"{course_id}|{syllabus.syllabus_id}"),
                node_type=GraphNodeType.SYLLABUS,
                node_key=f"{course_id}|{syllabus.syllabus_id}",
                label=syllabus.syllabus_id,
                course_id=course_id,
                syllabus_id=syllabus.syllabus_id,
                entity_id=syllabus.syllabus_id,
            )

        first_syllabus_node = syllabus_node(first_syllabus)
        second_syllabus_node = syllabus_node(second_syllabus)
        _save(
            database,
            course_id,
            syllabus=first_syllabus,
            nodes=[first_syllabus_node, *first_nodes],
            edges=[
                _edge(
                    course_id,
                    first_syllabus_node,
                    node,
                    relationship=GraphRelationship.CONTAINS,
                    syllabus_id=first_syllabus.syllabus_id,
                )
                for node in first_nodes
            ]
            + [
                _edge(
                    course_id,
                    first_nodes[0],
                    first_nodes[1],
                    status=GraphEdgeStatus.EXPLICIT,
                    syllabus_id=first_syllabus.syllabus_id,
                )
            ],
        )
        _save(
            database,
            course_id,
            syllabus=second_syllabus,
            nodes=[second_syllabus_node, *second_nodes],
            edges=[
                _edge(
                    course_id,
                    second_syllabus_node,
                    node,
                    relationship=GraphRelationship.CONTAINS,
                    syllabus_id=second_syllabus.syllabus_id,
                )
                for node in second_nodes
            ],
        )
        report = _discover(database, course_id, syllabus_id=first_syllabus.syllabus_id)
        assert report.candidates
        assert all(
            candidate.source.syllabus_id == first_syllabus.syllabus_id
            and candidate.target.syllabus_id == first_syllabus.syllabus_id
            for candidate in report.candidates
        )
        first_target = next(node for node in first_nodes if node.label == "Binary Search")
        plan = _plan(database, course_id, first_target)
        assert [item.label for item in plan.target_concepts] == ["Binary Search"]
        assert all(
            item in {node.node_id for node in first_nodes} for item in plan.ordered_concept_ids
        )
        wrong_version = LearningPathService(GraphService(database)).generate(
            LearningPathRequest(
                course_id=course_id,
                target_node_id=first_target.node_id,
                syllabus_id=second_syllabus.syllabus_id,
            )
        )
        assert wrong_version.status == LearningPathStatus.INVALID_TARGET.value
        assert course.course_id == course_id
    finally:
        database.close()


def test_discovery_dry_run_does_not_write_candidates() -> None:
    database = memory_database()
    try:
        course_id, syllabus, _ = _ordered_fixture(database)
        report = _discover(database, course_id, syllabus_id=syllabus.syllabus_id, persist=False)
        assert report.candidates
        assert report.edges_created == 0
        assert (
            GraphService(database).prerequisite_edges(
                course_id, syllabus_id=syllabus.syllabus_id, include_candidates=True
            )
            == []
        )
    finally:
        database.close()


def test_learning_path_orders_transitive_prerequisites_without_claiming_optimality() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Arrays", "Binary Search", "Trees", "Graphs"],
            links=[
                ("Arrays", "Binary Search", GraphEdgeStatus.EXPLICIT),
                ("Binary Search", "Trees", GraphEdgeStatus.REVIEWED),
                ("Trees", "Graphs", GraphEdgeStatus.VERIFIED),
            ],
        )
        plan = _plan(database, "phase6-direct-v1", concepts["Graphs"])
        assert plan.status == LearningPathStatus.OK.value
        assert [step.label for step in plan.steps] == [
            "Arrays",
            "Binary Search",
            "Trees",
            "Graphs",
        ]
        assert [step.depth for step in plan.steps] == [3, 2, 1, 0]
        assert plan.steps[-1].role == PathStepRole.TARGET.value
        assert plan.steps[0].role == PathStepRole.PREREQUISITE.value
        assert plan.steps[0].direct is False
        assert plan.steps[-2].direct is True
        assert plan.steps[0].evidence
        assert plan.target_concepts[0].provenance.get("entity_id")
        assert "not evidence of pedagogical optimality" in plan.limitations[0]
    finally:
        database.close()


def test_candidates_are_excluded_by_default_and_opted_in_explicitly() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Arrays", "Binary Search"],
            links=[("Arrays", "Binary Search", GraphEdgeStatus.CANDIDATE)],
        )
        default = _plan(database, "phase6-direct-v1", concepts["Binary Search"])
        assert default.status == LearningPathStatus.INSUFFICIENT_EVIDENCE.value
        assert [step.label for step in default.steps] == ["Binary Search"]
        assert any("excluded" in item for item in default.limitations)

        opted_in = _plan(
            database,
            "phase6-direct-v1",
            concepts["Binary Search"],
            include_candidates=True,
        )
        assert opted_in.status == LearningPathStatus.PARTIAL.value
        assert [step.label for step in opted_in.steps] == ["Arrays", "Binary Search"]
        assert opted_in.steps[0].review_recommended is True
        assert opted_in.steps[0].status is GraphEdgeStatus.CANDIDATE
        assert "unverified" in opted_in.steps[0].rationale
    finally:
        database.close()


def test_known_concepts_are_skipped_or_reviewed_on_request() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Arrays", "Binary Search"],
            links=[("Arrays", "Binary Search", GraphEdgeStatus.EXPLICIT)],
        )
        skipped = _plan(
            database,
            "phase6-direct-v1",
            concepts["Binary Search"],
            known=[concepts["Arrays"].node_id],
        )
        assert skipped.status == LearningPathStatus.OK.value
        assert [step.label for step in skipped.steps] == ["Binary Search"]
        assert [item.label for item in skipped.known_concepts_skipped] == ["Arrays"]
        assert skipped.steps[0].already_known is False
        assert any("mastered" in item for item in skipped.limitations)

        reviewed = _plan(
            database,
            "phase6-direct-v1",
            concepts["Binary Search"],
            known=[concepts["Arrays"].node_id],
            review_known=True,
        )
        assert [step.label for step in reviewed.steps] == ["Arrays", "Binary Search"]
        assert reviewed.steps[0].role == PathStepRole.REVIEW.value
        assert reviewed.steps[0].already_known is True
    finally:
        database.close()


def test_cycle_is_reported_and_broken_without_infinite_traversal() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Arrays", "Binary Search"],
            links=[
                ("Arrays", "Binary Search", GraphEdgeStatus.EXPLICIT),
                ("Binary Search", "Arrays", GraphEdgeStatus.REVIEWED),
            ],
        )
        plan = _plan(database, "phase6-direct-v1", concepts["Binary Search"])
        assert plan.status == LearningPathStatus.PARTIAL.value
        assert plan.cycles
        assert plan.conflicts
        assert len(plan.steps) == 2
        assert {step.label for step in plan.steps} == {"Arrays", "Binary Search"}
        assert any("cycle" in item for item in plan.limitations)

        shallow = _plan(
            database,
            "phase6-direct-v1",
            concepts["Binary Search"],
            max_depth=1,
        )
        assert shallow.cycles
        assert all(
            item.concept_id != concepts["Binary Search"].node_id
            for item in shallow.unresolved_concepts
        )
    finally:
        database.close()


def test_max_depth_and_max_length_report_unresolved_prerequisites() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Alpha", "Beta", "Gamma", "Delta"],
            links=[
                ("Alpha", "Beta", GraphEdgeStatus.EXPLICIT),
                ("Beta", "Gamma", GraphEdgeStatus.EXPLICIT),
                ("Gamma", "Delta", GraphEdgeStatus.EXPLICIT),
            ],
        )
        shallow = _plan(database, "phase6-direct-v1", concepts["Delta"], max_depth=1)
        assert shallow.status == LearningPathStatus.PARTIAL.value
        assert [step.label for step in shallow.steps] == ["Gamma", "Delta"]
        assert [item.label for item in shallow.unresolved_concepts] == ["Beta"]

        truncated = _plan(database, "phase6-direct-v1", concepts["Delta"], max_path_length=1)
        assert truncated.status == LearningPathStatus.PARTIAL.value
        assert len(truncated.steps) == 1
        assert any("truncated" in item for item in truncated.limitations)
    finally:
        database.close()


def test_paths_stay_inside_one_course_and_handle_isolated_targets() -> None:
    database = memory_database()
    try:
        first = _direct_fixture(
            database,
            course_id="phase6-course-a-v1",
            labels=["Arrays", "Binary Search"],
            links=[("Arrays", "Binary Search", GraphEdgeStatus.EXPLICIT)],
        )
        second = _direct_fixture(
            database,
            course_id="phase6-course-b-v1",
            labels=["Arrays", "Hash Table"],
            links=[("Arrays", "Hash Table", GraphEdgeStatus.REVIEWED)],
        )
        plan = _plan(database, "phase6-course-a-v1", first["Binary Search"])
        assert [step.label for step in plan.steps] == ["Arrays", "Binary Search"]
        assert all(
            step.concept_id in {item.node_id for item in first.values()} for step in plan.steps
        )
        assert second["Hash Table"].node_id not in plan.ordered_concept_ids

        isolated = _plan(database, "phase6-course-b-v1", second["Hash Table"])
        assert isolated.status == LearningPathStatus.OK.value
        assert [step.label for step in isolated.steps] == ["Arrays", "Hash Table"]

        wrong_course = _plan(database, "phase6-course-a-v1", second["Hash Table"])
        assert wrong_course.status == LearningPathStatus.INVALID_TARGET.value
        assert wrong_course.steps == []
    finally:
        database.close()


def test_isolated_target_returns_insufficient_evidence_fallback() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(database, course_id="phase6-isolated-v1", labels=["Loner"])
        plan = _plan(database, "phase6-isolated-v1", concepts["Loner"])
        assert plan.status == LearningPathStatus.INSUFFICIENT_EVIDENCE.value
        assert [step.label for step in plan.steps] == ["Loner"]
        assert plan.target_concepts[0].label == "Loner"
        assert any("No supported" in item or "no supported" in item for item in plan.limitations)
    finally:
        database.close()


def test_validation_reports_isolated_concepts_and_missing_metadata() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Alpha", "Beta", "Isolated"],
            links=[("Alpha", "Beta", GraphEdgeStatus.CANDIDATE)],
        )
        report = PrerequisiteDiscoveryService(GraphService(database)).validate("phase6-direct-v1")
        assert report.candidate_count == 1
        assert report.supported_count == 0
        assert [item.label for item in report.isolated_concepts] == ["Isolated"]
        assert report.valid
        plan = _plan(
            database,
            "phase6-direct-v1",
            concepts["Beta"],
            known=[concepts["Alpha"].node_id, "does-not-exist"],
        )
        assert plan.missing_metadata
        assert plan.status == LearningPathStatus.INSUFFICIENT_EVIDENCE.value
    finally:
        database.close()


def test_discovered_candidate_and_review_survive_graph_rebuild() -> None:
    database = memory_database()
    try:
        course_id, syllabus, concepts = _ordered_fixture(
            database, units=[["Arrays", "Binary Search"]]
        )
        report = _discover(database, course_id, syllabus_id=syllabus.syllabus_id)
        assert report.candidates
        candidate = report.candidates[0]
        service = GraphService(database)
        assert service.set_edge_status(
            candidate.edge_id, GraphEdgeStatus.VERIFIED, evidence=["reviewer: fixture"]
        )
        nodes = [concepts["Arrays"], concepts["Binary Search"]]
        with database.transaction() as session:
            preserved = GraphRepository(session).replace_course(course_id, nodes, [])
        assert preserved == 1
        rebuilt = service.edge(candidate.edge_id)
        assert rebuilt.status is GraphEdgeStatus.VERIFIED
        assert rebuilt.metadata["preserved_on_rebuild"] is True
        assert "reviewer: fixture" in rebuilt.evidence
        assert rebuilt.metadata["validated"] is True

        # Removing a source endpoint must also prune the derived candidate
        # rather than leave a dangling relationship.
        with database.transaction() as session:
            removed = GraphRepository(session).replace_course(course_id, [concepts["Arrays"]], [])
        assert removed == 0
        with pytest.raises(KeyError):
            service.edge(candidate.edge_id)
    finally:
        database.close()


def _prepare_pipeline(database: Database) -> None:
    batch = IngestionPipeline(database, settings=Settings()).ingest_manifest(ANALYTICS_MANIFEST)
    assert batch.ok, [error.code for error in batch.all_errors]
    nlp = NlpService(database, NlpSettings()).analyze(PIPELINE_COURSE)
    assert all(report.ok for report in nlp)
    understanding = UnderstandingService(database, UnderstandingSettings()).analyze(PIPELINE_COURSE)
    assert all(report.ok for report in understanding)


def test_phase6_discovery_runs_on_phase1_to_5_pipeline_output() -> None:
    database = memory_database()
    try:
        _prepare_pipeline(database)
        service = GraphService(database)
        build = service.build_course(PIPELINE_COURSE)
        assert build.status.value == "ok"
        report = _discover(database, PIPELINE_COURSE)
        assert report.nodes_scanned > 0
        assert report.candidates
        assert all(item.status is GraphEdgeStatus.CANDIDATE for item in report.candidates)
        assert all(item.evidence for item in report.candidates)
        graph_edges = service.prerequisite_edges(PIPELINE_COURSE, include_candidates=True)
        assert graph_edges
        assert all(edge.confidence is None for edge in graph_edges)
    finally:
        database.close()


def test_max_path_length_retains_the_target_and_marks_omitted_prerequisites() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Alpha", "Beta", "Gamma", "Delta"],
            links=[
                ("Alpha", "Beta", GraphEdgeStatus.EXPLICIT),
                ("Beta", "Gamma", GraphEdgeStatus.EXPLICIT),
                ("Gamma", "Delta", GraphEdgeStatus.EXPLICIT),
            ],
        )
        plan = _plan(
            database,
            "phase6-direct-v1",
            concepts["Delta"],
            max_path_length=1,
        )
        assert plan.status == LearningPathStatus.PARTIAL.value
        assert [step.label for step in plan.steps] == ["Delta"]
        assert plan.steps[0].role == PathStepRole.TARGET.value
        assert {item.label for item in plan.unresolved_concepts} == {"Alpha", "Beta", "Gamma"}
        assert all(item.provenance.get("entity_id") for item in plan.unresolved_concepts)
        assert any("truncated" in item for item in plan.limitations)
    finally:
        database.close()


def test_valid_but_empty_unit_and_topic_targets_are_insufficient_evidence() -> None:
    database = memory_database()
    try:
        course_id = "phase6-empty-targets-v1"
        unit = GraphNode(
            node_id=make_node_id(GraphNodeType.UNIT, f"{course_id}|unit:empty"),
            node_type=GraphNodeType.UNIT,
            node_key=f"{course_id}|unit:empty",
            label="Empty unit",
            normalized_label="empty unit",
            course_id=course_id,
            entity_id="unit_empty",
        )
        topic = GraphNode(
            node_id=make_node_id(GraphNodeType.TOPIC, f"{course_id}|topic:empty"),
            node_type=GraphNodeType.TOPIC,
            node_key=f"{course_id}|topic:empty",
            label="Empty topic",
            normalized_label="empty topic",
            course_id=course_id,
        )
        _save(database, course_id, nodes=[unit, topic], edges=[])
        service = LearningPathService(GraphService(database))
        for target, target_type in (
            (unit, GraphNodeType.UNIT),
            (topic, GraphNodeType.TOPIC),
        ):
            plan = service.generate(
                LearningPathRequest(
                    course_id=course_id,
                    target_node_id=target.node_id,
                    target_node_type=target_type,
                )
            )
            assert plan.status == LearningPathStatus.INSUFFICIENT_EVIDENCE.value
            assert plan.steps == []
            assert plan.target_concepts == []
            assert any("no concepts could be composed" in item for item in plan.limitations)
    finally:
        database.close()


def test_course_wide_target_does_not_use_only_one_of_several_syllabus_versions() -> None:
    database = memory_database()
    try:
        course_id = "phase6-multiversion-target-v1"
        first = _syllabus(course_id, "doc_multiversion_one", units=[["Arrays"]])
        second = _syllabus(course_id, "doc_multiversion_two", units=[["Graphs"]])
        first_concept = _concept(course_id, "Arrays", first.syllabus_id)
        second_concept = _concept(course_id, "Graphs", second.syllabus_id)
        target = _concept(course_id, "Target")
        _save(
            database,
            course_id,
            syllabus=first,
            nodes=[first_concept, target],
            edges=[
                _edge(
                    course_id,
                    first_concept,
                    target,
                    status=GraphEdgeStatus.EXPLICIT,
                    syllabus_id=first.syllabus_id,
                )
            ],
        )
        _save(
            database,
            course_id,
            syllabus=second,
            nodes=[second_concept],
            edges=[],
        )
        plan = _plan(database, course_id, target)
        assert plan.status == LearningPathStatus.INSUFFICIENT_EVIDENCE.value
        assert [step.label for step in plan.steps] == ["Target"]
        assert "Arrays" not in plan.ordered_concept_ids
        assert any("multiple syllabus versions" in item for item in plan.missing_metadata)
    finally:
        database.close()


def test_topic_target_does_not_mix_concepts_from_two_syllabus_versions() -> None:
    database = memory_database()
    try:
        course_id = "phase6-multiversion-topic-v1"
        first = _syllabus(course_id, "doc_topic_version_one", units=[["Arrays"]])
        second = _syllabus(course_id, "doc_topic_version_two", units=[["Graphs"]])
        first_concept = _concept(course_id, "Arrays", first.syllabus_id)
        second_concept = _concept(course_id, "Graphs", second.syllabus_id)
        topic = GraphNode(
            node_id=make_node_id(GraphNodeType.TOPIC, f"{course_id}|topic:search"),
            node_type=GraphNodeType.TOPIC,
            node_key=f"{course_id}|topic:search",
            label="Searching",
            normalized_label="searching",
            course_id=course_id,
        )
        first_question = _question(course_id, "q_multi_topic_1", "Question 1", None)
        second_question = _question(course_id, "q_multi_topic_2", "Question 2", None)
        question_records = [
            QuestionRecord(
                question_id=question.question_id or "",
                document_id=first.document_id,
                course_id=course_id,
                text="Multi-version topic question",
                question_number=str(index),
                number_sort=index,
                marks=5.0,
                page_number=1,
                confidence=0.9,
            )
            for index, question in enumerate((first_question, second_question), start=1)
        ]
        _save(
            database,
            course_id,
            syllabus=first,
            questions=question_records,
            nodes=[first_concept, topic, first_question, second_question],
            edges=[
                _edge(
                    course_id,
                    first_question,
                    topic,
                    status=GraphEdgeStatus.INFERRED,
                    relationship=GraphRelationship.COVERS,
                    syllabus_id=first.syllabus_id,
                ),
                _edge(
                    course_id,
                    first_question,
                    first_concept,
                    status=GraphEdgeStatus.INFERRED,
                    relationship=GraphRelationship.ASSESSES,
                    syllabus_id=first.syllabus_id,
                ),
            ],
        )
        _save(
            database,
            course_id,
            syllabus=second,
            nodes=[second_concept],
            edges=[
                _edge(
                    course_id,
                    second_question,
                    topic,
                    status=GraphEdgeStatus.INFERRED,
                    relationship=GraphRelationship.COVERS,
                    syllabus_id=second.syllabus_id,
                ),
                _edge(
                    course_id,
                    second_question,
                    second_concept,
                    status=GraphEdgeStatus.INFERRED,
                    relationship=GraphRelationship.ASSESSES,
                    syllabus_id=second.syllabus_id,
                ),
            ],
        )
        plan = LearningPathService(GraphService(database)).generate(
            LearningPathRequest(
                course_id=course_id,
                target_node_id=topic.node_id,
                target_node_type=GraphNodeType.TOPIC,
            )
        )
        assert plan.status == LearningPathStatus.INSUFFICIENT_EVIDENCE.value
        assert plan.target_concepts == []
        assert any("version-specific concept mappings" in item for item in plan.limitations)
    finally:
        database.close()


def test_unknown_course_and_syllabus_are_reported_explicitly() -> None:
    database = memory_database()
    try:
        course_id, syllabus, concepts = _ordered_fixture(database, units=[["Arrays", "Trees"]])
        target = concepts["Trees"]
        with pytest.raises(KeyError, match="unknown syllabus"):
            _discover(database, course_id, syllabus_id="syl_missing")
        with pytest.raises(KeyError, match="unknown course"):
            _plan(database, "missing-course", target)
        with pytest.raises(KeyError, match="unknown syllabus"):
            PrerequisiteDiscoveryService(GraphService(database)).validate(
                course_id, syllabus_id="syl_missing"
            )
        plan = LearningPathService(GraphService(database)).generate(
            LearningPathRequest(
                course_id=course_id,
                target_node_id=target.node_id,
                syllabus_id="syl_missing",
            )
        )
        assert plan.status == LearningPathStatus.INVALID_TARGET.value
        assert any("unknown syllabus" in item for item in plan.missing_metadata)
        assert syllabus.syllabus_id
    finally:
        database.close()


def test_target_step_flags_unverified_basis_even_when_a_verified_edge_exists() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Arrays", "Hash Table", "Graphs"],
            links=[
                ("Arrays", "Graphs", GraphEdgeStatus.EXPLICIT),
                ("Hash Table", "Graphs", GraphEdgeStatus.CANDIDATE),
            ],
        )
        plan = _plan(
            database,
            "phase6-direct-v1",
            concepts["Graphs"],
            include_candidates=True,
        )
        target = next(step for step in plan.steps if step.role == PathStepRole.TARGET.value)
        assert target.status is GraphEdgeStatus.EXPLICIT
        assert target.review_recommended is True
        assert "unverified" in target.rationale
        assert plan.status == LearningPathStatus.PARTIAL.value
    finally:
        database.close()


def test_explicit_review_updates_validation_flags_without_claiming_human_review() -> None:
    database = memory_database()
    try:
        _direct_fixture(
            database,
            labels=["Arrays", "Graphs"],
            links=[("Arrays", "Graphs", GraphEdgeStatus.CANDIDATE)],
        )
        service = GraphService(database)
        edge = service.prerequisite_edges("phase6-direct-v1", include_candidates=True)[0]
        assert service.set_edge_status(edge.edge_id, GraphEdgeStatus.EXPLICIT)
        explicit = service.edge(edge.edge_id)
        assert explicit.metadata["validated"] is True
        assert explicit.metadata["review_required"] is False
        assert "validated_by" not in explicit.metadata
        assert service.set_edge_status(edge.edge_id, GraphEdgeStatus.CANDIDATE)
        downgraded = service.edge(edge.edge_id)
        assert downgraded.metadata["validated"] is False
        assert downgraded.metadata["review_required"] is True
    finally:
        database.close()


def test_path_uses_the_settings_default_depth_when_the_request_omits_it() -> None:
    database = memory_database()
    try:
        concepts = _direct_fixture(
            database,
            labels=["Alpha", "Beta", "Gamma"],
            links=[
                ("Alpha", "Beta", GraphEdgeStatus.EXPLICIT),
                ("Beta", "Gamma", GraphEdgeStatus.EXPLICIT),
            ],
        )
        plan = LearningPathService(
            GraphService(database), PrerequisiteSettings(path_max_depth=1)
        ).generate(
            LearningPathRequest(
                course_id="phase6-direct-v1",
                target_node_id=concepts["Gamma"].node_id,
            )
        )
        assert [step.label for step in plan.steps] == ["Beta", "Gamma"]
        assert [item.label for item in plan.unresolved_concepts] == ["Alpha"]
        assert plan.status == LearningPathStatus.PARTIAL.value
        assert plan.config["path_max_depth_effective"] == 1
    finally:
        database.close()


def test_cooccurrence_cap_keeps_question_association_without_the_extra_signal() -> None:
    database = memory_database()
    try:
        course_id, syllabus, _ = _ordered_fixture(
            database,
            units=[["Arrays", "Binary Search"]],
            with_questions=True,
        )
        report = PrerequisiteDiscoveryService(GraphService(database)).discover(
            PrerequisiteDiscoveryRequest(
                course_id=course_id,
                syllabus_id=syllabus.syllabus_id,
                max_cooccurrence_candidates=0,
                persist=False,
            )
        )
        candidate = report.candidates[0]
        sources = {item.source for item in candidate.evidence}
        assert "question_concept_mapping" in sources
        assert "question_cooccurrence" not in sources
        assert any("max_cooccurrence_candidates" in item for item in report.limitations)
    finally:
        database.close()


def test_cross_unit_candidates_retain_both_unit_sources() -> None:
    database = memory_database()
    try:
        course_id, syllabus, _ = _ordered_fixture(
            database,
            course_id="phase6-cross-unit-v1",
            units=[["Arrays"], ["Graphs"]],
        )
        report = _discover(
            database,
            course_id,
            settings=PrerequisiteSettings(include_cross_unit_order=True),
        )
        candidate = next(
            item
            for item in report.candidates
            if item.source.label == "Arrays" and item.target.label == "Graphs"
        )
        evidence = next(
            item for item in candidate.evidence if item.source == "syllabus_containment"
        )
        assert evidence.provenance["source_unit_id"] == syllabus.units[0].unit_id
        assert evidence.provenance["target_unit_id"] == syllabus.units[1].unit_id
        edge = GraphService(database).edge(candidate.edge_id)
        assert set(edge.metadata["unit_ids"]) == {
            syllabus.units[0].unit_id,
            syllabus.units[1].unit_id,
        }
    finally:
        database.close()
