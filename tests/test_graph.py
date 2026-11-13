"""Phase 5 tests: graph construction, identity, persistence, queries and review."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import delete

from exam_engine.config import GraphSettings, NlpSettings, Settings, UnderstandingSettings
from exam_engine.graph import GraphService
from exam_engine.graph.builder import GraphBuilder, GraphCourseData
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    ConceptOrigin,
    DocumentKind,
    ExtractionStatus,
    GraphBuildStatus,
    GraphEdgeStatus,
    GraphNodeType,
    GraphRelationship,
    MappingStatus,
    NlpMethod,
    SimilarityKind,
    SourceFormat,
)
from exam_engine.models.graph import make_edge_id, make_node_id
from exam_engine.models.nlp import ConceptRecord
from exam_engine.models.question import QuestionRecord
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)
from exam_engine.models.understanding import (
    QuestionAnalysis,
    SimilarityMatch,
    UnitMatch,
    make_analysis_id,
    make_similarity_id,
    make_unit_match_id,
)
from exam_engine.nlp.service import NlpService
from exam_engine.storage.db import Database, memory_database
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    SyllabusRepository,
)
from exam_engine.storage.schema import QuestionRow
from exam_engine.understanding.service import UnderstandingService
from tests.support import make_course, make_source

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_MANIFEST = PROJECT_ROOT / "datasets" / "analytics_sample" / "manifest.json"
COURSE = "demo-analytics-cs201-2024-v1"
SHORT_HASH = "0" * 64


# --- deterministic fixture builders -----------------------------------------


def _document(
    document_id: str,
    course_id: str,
    *,
    kind: DocumentKind = DocumentKind.QUESTION_PAPER,
    title: str | None = None,
    metadata: dict | None = None,
) -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id="graph-fixture",
        kind=kind,
        format=SourceFormat.TXT,
        title=title or document_id,
        original_filename=f"{document_id}.txt",
        content_sha256=hashlib.sha256(document_id.encode()).hexdigest(),
        byte_size=100,
        page_count=1,
        char_count=80,
        extraction_status=ExtractionStatus.OK,
        pipeline_version="test",
        metadata=metadata or {},
    )


def _syllabus(
    course_id: str,
    document_id: str,
    *,
    topics: list[str],
    prerequisites: dict[str, list[str]] | None = None,
) -> SyllabusRecord:
    syllabus_id = make_syllabus_id(document_id)
    unit_id = make_unit_id(syllabus_id, "Unit 1", " ".join(topics))
    unit = SyllabusUnitRecord(
        unit_id=unit_id,
        unit_label="Unit 1",
        unit_number=1,
        title="Foundations",
        topics=topics,
        confidence=0.9,
        metadata={"prerequisites": prerequisites} if prerequisites else {},
    )
    return SyllabusRecord(
        syllabus_id=syllabus_id,
        course_id=course_id,
        document_id=document_id,
        source_id="graph-fixture",
        title="Graph fixture syllabus",
        units=[unit],
        confidence=0.9,
    )


def _question(document_id: str, course_id: str, number: str, text: str) -> QuestionRecord:
    return QuestionRecord(
        question_id=f"q_{document_id}_{number}",
        document_id=document_id,
        course_id=course_id,
        text=text,
        question_number=number,
        number_sort=int(number),
        marks=5.0,
        page_number=1,
        confidence=0.9,
    )


def _analysis(
    question: QuestionRecord,
    *,
    mapping_status: MappingStatus = MappingStatus.MAPPED,
) -> QuestionAnalysis:
    confidence = {
        MappingStatus.MAPPED: 0.9,
        MappingStatus.AMBIGUOUS: 0.4,
        MappingStatus.UNMAPPED: 0.0,
    }[mapping_status]
    return QuestionAnalysis(
        analysis_id=make_analysis_id(question.course_id, question.question_id),
        question_id=question.question_id,
        course_id=question.course_id,
        document_id=question.document_id,
        text_sha256=SHORT_HASH,
        mapping_status=mapping_status,
        mapping_confidence=confidence,
        top_unit_id="unit",
    )


def _match(question: QuestionRecord, syllabus: SyllabusRecord, topic: str) -> UnitMatch:
    unit = syllabus.units[0]
    return UnitMatch(
        match_id=make_unit_match_id(
            question.course_id, question.question_id, unit.unit_id, topic, "test"
        ),
        question_id=question.question_id,
        course_id=question.course_id,
        unit_id=unit.unit_id,
        syllabus_id=syllabus.syllabus_id,
        unit_label=unit.unit_label,
        topic=topic,
        score=0.82,
        rank=1,
        method="test",
        matched_terms=[topic.casefold()],
        evidence=["fixture match"],
    )


def _data(
    *,
    course_id: str = "graph-fixture-v1",
    topics: list[str] | None = None,
    prerequisites: dict[str, list[str]] | None = None,
    concept_terms: list[str] | None = None,
) -> GraphCourseData:
    topics = topics or ["Arrays", "Binary Search", "Binary Search Tree"]
    course = make_course(course_id=course_id, course_code="CS101", title="Graph fixture")
    syllabus_document = _document(
        f"doc_syllabus_{course_id}", course.course_id, kind=DocumentKind.SYLLABUS
    )
    paper = _document(f"doc_paper_{course_id}", course.course_id, title="Fixture paper")
    syllabus = _syllabus(
        course.course_id,
        syllabus_document.document_id,
        topics=topics,
        prerequisites=prerequisites,
    )
    questions = [
        _question(paper.document_id, course.course_id, "1", "Explain binary search."),
        _question(paper.document_id, course.course_id, "2", "Explain binary trees."),
    ]
    analyses = {question.question_id: _analysis(question) for question in questions}
    matches = {
        questions[0].question_id: [_match(questions[0], syllabus, "Binary Search")],
        questions[1].question_id: [_match(questions[1], syllabus, "Binary Search Tree")],
    }
    concepts = [
        ConceptRecord(
            concept_id=f"c_{index}",
            course_id=course.course_id,
            term=term,
            normalized=term.casefold(),
            origin=ConceptOrigin.INFERRED,
            method=NlpMethod.TFIDF,
            confidence=0.7,
            unit_id=syllabus.units[0].unit_id,
            evidence=[f"fixture:{term}"],
        )
        for index, term in enumerate(concept_terms or ["Binary Search", "Hash Table"])
    ]
    return GraphCourseData(
        course=course,
        documents=[syllabus_document, paper],
        questions=questions,
        syllabi=[syllabus],
        concepts=concepts,
        analyses=analyses,
        matches=matches,
        similarities=[],
        periods={},
    )


def _find(nodes, node_type: GraphNodeType, label: str | None = None):
    return [
        node
        for node in nodes
        if node.node_type is node_type
        and (label is None or node.normalized_label == label.casefold())
    ]


# --- builder tests -----------------------------------------------------------


def test_builder_merges_only_normalized_concept_identity_and_keeps_aliases() -> None:
    data = _data(concept_terms=["Binary Search", "binary search", "Hash Table"])
    output = GraphBuilder(GraphSettings()).build(data)
    concepts = _find(output.nodes, GraphNodeType.CONCEPT, "binary search")
    assert len(concepts) == 1
    node = concepts[0]
    assert sorted(node.metadata["source_concept_ids"]) == ["c_0", "c_1"]
    assert node.metadata["verified"] is True
    assert set(node.metadata["origins"]) == {"inferred", "syllabus"}
    assert "Binary Search" in node.metadata["aliases"]
    assert len(_find(output.nodes, GraphNodeType.CONCEPT, "hash table")) == 1
    assert len(_find(output.nodes, GraphNodeType.CONCEPT, "binary search tree")) == 1


def test_builder_marks_inferred_edges_with_evidence_and_confidence() -> None:
    output = GraphBuilder(GraphSettings()).build(_data())
    covers = [edge for edge in output.edges if edge.relationship is GraphRelationship.COVERS]
    assesses = [edge for edge in output.edges if edge.relationship is GraphRelationship.ASSESSES]
    assert covers and assesses
    assert all(edge.status is GraphEdgeStatus.INFERRED for edge in covers + assesses)
    assert all(edge.evidence for edge in covers + assesses)
    # Confidence is only recorded where the producing method supports one; the
    # aggregate document coverage edge must not invent a score.
    direct = [edge for edge in covers + assesses if edge.method == "test"]
    assert direct and all(edge.confidence == 0.82 for edge in direct)
    aggregate = [edge for edge in covers if edge.method == "aggregate_question_mapping"]
    assert aggregate and all(edge.confidence is None for edge in aggregate)
    for edge in output.edges:
        if edge.status is not GraphEdgeStatus.EXPLICIT:
            assert edge.evidence, edge.edge_id
        else:
            # Explicit syllabus structure must not inherit a Phase 3 score.
            assert edge.confidence is None, edge.edge_id
        assert edge.relationship is not None
    # The fixture has no explicit session metadata: this must be reported, not
    # silently treated as an exam-session graph.
    assert any(warning.code == "no_exam_sessions" for warning in output.warnings)


def test_prerequisite_candidates_are_never_verified() -> None:
    output = GraphBuilder(GraphSettings(infer_prerequisite_candidates=True)).build(
        _data(topics=["Arrays", "Binary Search", "Binary Search Tree"])
    )
    prerequisites = [
        edge for edge in output.edges if edge.relationship is GraphRelationship.PREREQUISITE_OF
    ]
    assert prerequisites
    assert all(edge.status is GraphEdgeStatus.CANDIDATE for edge in prerequisites)
    assert all(edge.metadata.get("validated") is False for edge in prerequisites)
    assert all(edge.evidence for edge in prerequisites)


def test_explicit_syllabus_prerequisites_are_distinguished_from_candidates() -> None:
    output = GraphBuilder(GraphSettings()).build(
        _data(
            topics=["Arrays", "Binary Search"],
            prerequisites={"Binary Search": ["Arrays"]},
        )
    )
    prerequisites = [
        edge for edge in output.edges if edge.relationship is GraphRelationship.PREREQUISITE_OF
    ]
    assert len(prerequisites) == 1
    edge = prerequisites[0]
    assert edge.status is GraphEdgeStatus.EXPLICIT
    assert edge.metadata["validated"] is True
    assert edge.evidence


def test_inferred_concept_never_gains_explicit_syllabus_provenance() -> None:
    output = GraphBuilder().build(_data(concept_terms=["Hash Table"]))
    concept = _find(output.nodes, GraphNodeType.CONCEPT, "hash table")[0]
    syllabus_node = _find(output.nodes, GraphNodeType.SYLLABUS)[0]
    syllabus_edge = next(
        edge
        for edge in output.edges
        if edge.relationship is GraphRelationship.CONTAINS
        and edge.source_node_id == syllabus_node.node_id
        and edge.target_node_id == concept.node_id
    )
    assert syllabus_edge.status is GraphEdgeStatus.INFERRED
    assert syllabus_edge.method == "phase2_concept"
    doc_edges = [
        edge
        for edge in output.edges
        if edge.relationship is GraphRelationship.DERIVED_FROM
        and edge.source_node_id == concept.node_id
    ]
    assert doc_edges
    assert all(edge.status is GraphEdgeStatus.INFERRED for edge in doc_edges)

    verified = _find(output.nodes, GraphNodeType.CONCEPT, "binary search")[0]
    verified_edge = next(
        edge
        for edge in output.edges
        if edge.relationship is GraphRelationship.CONTAINS
        and edge.source_node_id == syllabus_node.node_id
        and edge.target_node_id == verified.node_id
    )
    assert verified_edge.status is GraphEdgeStatus.EXPLICIT


def test_ambiguous_mapping_is_a_candidate_not_an_inferred_fact() -> None:
    data = _data()
    question = data.questions[0]
    data.analyses[question.question_id] = _analysis(
        question, mapping_status=MappingStatus.AMBIGUOUS
    )
    output = GraphBuilder().build(data)
    question_node = next(
        node
        for node in output.nodes
        if node.node_type is GraphNodeType.QUESTION and node.question_id == question.question_id
    )
    mapped = [
        edge
        for edge in output.edges
        if edge.source_node_id == question_node.node_id
        and edge.relationship in (GraphRelationship.COVERS, GraphRelationship.ASSESSES)
    ]
    assert mapped
    assert all(edge.status is GraphEdgeStatus.CANDIDATE for edge in mapped)
    assert all(edge.metadata["mapping_status"] == "ambiguous" for edge in mapped)
    document_edge = next(
        edge
        for edge in output.edges
        if edge.relationship is GraphRelationship.COVERS
        and edge.method == "aggregate_question_mapping"
        and question.question_id in edge.metadata.get("question_ids", [])
    )
    assert document_edge.status is GraphEdgeStatus.CANDIDATE


def test_related_concepts_inherit_candidate_mapping_status() -> None:
    data = _data()
    first, second = data.questions
    data.analyses[first.question_id] = _analysis(first, mapping_status=MappingStatus.AMBIGUOUS)
    data.similarities = [
        SimilarityMatch(
            pair_id=make_similarity_id(
                first.course_id,
                first.question_id,
                second.question_id,
                SimilarityKind.CONCEPT_RELATED,
            ),
            course_id=first.course_id,
            question_id=first.question_id,
            matched_question_id=second.question_id,
            kind=SimilarityKind.CONCEPT_RELATED,
            score=0.8,
            lexical_score=0.0,
            semantic_score=0.8,
            method="test_similarity",
            evidence=["fixture semantic relation"],
        )
    ]
    output = GraphBuilder().build(data)
    related = [edge for edge in output.edges if edge.relationship is GraphRelationship.RELATED_TO]
    assert related
    assert all(edge.status is GraphEdgeStatus.CANDIDATE for edge in related)
    assert all("ambiguous" in edge.metadata.get("phase3_mapping_statuses", []) for edge in related)


def test_phase3_concept_has_syllabus_and_source_document_provenance() -> None:
    data = _data()
    question = data.questions[0]
    syllabus = data.syllabi[0]
    data.matches[question.question_id] = [_match(question, syllabus, "Hash Table")]
    output = GraphBuilder().build(data)
    concept = _find(output.nodes, GraphNodeType.CONCEPT, "hash table")[0]
    unit_id = syllabus.units[0].unit_id
    assert unit_id in concept.metadata["unit_ids"]
    assert question.document_id in concept.metadata["document_ids"]
    assert concept.metadata["confidence"] == 0.82

    syllabus_node = _find(output.nodes, GraphNodeType.SYLLABUS)[0]
    syllabus_edge = next(
        edge
        for edge in output.edges
        if edge.relationship is GraphRelationship.CONTAINS
        and edge.source_node_id == syllabus_node.node_id
        and edge.target_node_id == concept.node_id
    )
    assert syllabus_edge.status is GraphEdgeStatus.INFERRED
    assert syllabus_edge.source_document_id == syllabus.document_id

    provenance = next(
        edge
        for edge in output.edges
        if edge.relationship is GraphRelationship.DERIVED_FROM
        and edge.source_node_id == concept.node_id
        and edge.source_document_id == question.document_id
    )
    assert provenance.status is GraphEdgeStatus.INFERRED
    assert provenance.source_page == question.page_number


def test_builder_instance_is_reusable_without_accumulating_state() -> None:
    builder = GraphBuilder()
    first = builder.build(_data())
    second = builder.build(_data())
    assert {node.node_id for node in first.nodes} == {node.node_id for node in second.nodes}
    assert {edge.edge_id for edge in first.edges} == {edge.edge_id for edge in second.edges}
    assert len(first.warnings) == len(second.warnings)


def test_partial_question_analysis_is_reported_without_failing_the_build() -> None:
    data = _data()
    removed = next(iter(data.analyses))
    data.analyses.pop(removed)
    output = GraphBuilder().build(data)
    warning = next(item for item in output.warnings if item.code == "missing_analysis")
    assert warning.detail == {"count": 1, "total": 2}
    assert "1 question(s)" in warning.message


def test_ambiguous_prerequisite_list_is_reported_not_guessed() -> None:
    data = _data(prerequisites={"Binary Search": ["Arrays"]})
    data.syllabi[0].units[0].metadata["prerequisites"] = ["Arrays"]
    output = GraphBuilder().build(data)
    assert not [
        edge for edge in output.edges if edge.relationship is GraphRelationship.PREREQUISITE_OF
    ]
    assert any(warning.code == "ambiguous_prerequisites" for warning in output.warnings)


def test_builder_never_shares_concept_nodes_across_courses() -> None:
    first = GraphBuilder().build(_data(course_id="graph-course-a-v1"))
    second = GraphBuilder().build(_data(course_id="graph-course-b-v1"))
    first_concept = _find(first.nodes, GraphNodeType.CONCEPT, "binary search")[0]
    second_concept = _find(second.nodes, GraphNodeType.CONCEPT, "binary search")[0]
    assert first_concept.label == second_concept.label
    assert first_concept.node_id != second_concept.node_id
    assert first_concept.course_id == "graph-course-a-v1"
    assert second_concept.course_id == "graph-course-b-v1"

    first_institution = _find(first.nodes, GraphNodeType.INSTITUTION)[0]
    second_institution = _find(second.nodes, GraphNodeType.INSTITUTION)[0]
    assert first_institution.node_id == second_institution.node_id


def test_builder_ids_are_deterministic() -> None:
    first = GraphBuilder().build(_data())
    second = GraphBuilder().build(_data())
    assert {node.node_id for node in first.nodes} == {node.node_id for node in second.nodes}
    assert {edge.edge_id for edge in first.edges} == {edge.edge_id for edge in second.edges}
    assert make_node_id(GraphNodeType.CONCEPT, "graph-fixture-v1|concept:arrays").startswith("nod_")
    assert make_edge_id("a", GraphRelationship.COVERS, "b").startswith("rel_")


# --- persistence and integration -------------------------------------------


def _prepare_analytics(database: Database) -> None:
    batch = IngestionPipeline(database, settings=Settings()).ingest_manifest(ANALYTICS_MANIFEST)
    assert batch.ok, [error.code for error in batch.all_errors]
    nlp = NlpService(database, NlpSettings()).analyze(COURSE)
    assert all(report.ok for report in nlp)
    understanding = UnderstandingService(database, UnderstandingSettings()).analyze(COURSE)
    assert all(report.ok for report in understanding)


def test_service_build_is_idempotent_and_reports_evidence() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        first = service.build_course(COURSE)
        assert first.status is GraphBuildStatus.OK
        first_nodes = {node.node_id for node in service.concepts(COURSE)}
        second = service.build_course(COURSE)
        assert second.status is GraphBuildStatus.OK
        assert {node.node_id for node in service.concepts(COURSE)} == first_nodes
        assert service.validate(COURSE) == []
        rows = service.status()
        assert rows[0]["dangling_edges"] is False
        # The course-scoped status path must return the same row as the bulk path.
        assert service.status(COURSE) == rows
        # The fixture has ambiguous Phase 3 mappings; those must remain candidates.
        assert rows[0]["candidate_edges"] > 0
        # The build report counts course-scoped rows; shared institution/subject
        # nodes are reported separately so it agrees with ``graph status``.
        assert second.node_count == rows[0]["nodes"]
        assert second.shared_node_count == 2
        assert sum(second.nodes_by_type.values()) == second.node_count
        assert second.edge_count == rows[0]["edges"]
        assert rows[0]["sessions"] == 3
        assert second.edges_by_relationship.get("APPEARS_IN", 0) > 0

        inferred = [
            edge for edge in _edges(database, COURSE) if edge.status is not GraphEdgeStatus.EXPLICIT
        ]
        assert inferred
        assert all(edge.evidence for edge in inferred)
        assert all(edge.method for edge in inferred)
    finally:
        database.close()


def _edges(database: Database, course_id: str):
    from exam_engine.storage.graph_repository import GraphRepository

    with database.session() as session:
        return GraphRepository(session).list_edges(course_id=course_id)


def test_review_survives_rebuild_and_created_at_is_preserved() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        concept = service.concepts(COURSE)[0]
        created_at = concept.created_at
        edge = next(
            edge for edge in _edges(database, COURSE) if edge.status is GraphEdgeStatus.INFERRED
        )
        assert service.set_edge_status(
            edge.edge_id, GraphEdgeStatus.REVIEWED, evidence=["reviewer: looks correct"]
        )
        service.build_course(COURSE)
        reviewed = service.edge(edge.edge_id)
        assert reviewed.status is GraphEdgeStatus.REVIEWED
        assert "reviewer: looks correct" in reviewed.evidence
        assert reviewed.metadata["reviewed_at"]
        assert service.node(concept.node_id).created_at == created_at
        assert service.validate(COURSE) == []
    finally:
        database.close()


def test_reviewed_prerequisite_candidate_is_marked_validated() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE, infer_prerequisites=True)
        candidate = next(
            edge
            for edge in _edges(database, COURSE)
            if edge.relationship is GraphRelationship.PREREQUISITE_OF
            and edge.status is GraphEdgeStatus.CANDIDATE
        )
        assert candidate.metadata.get("validated") is False
        assert service.set_edge_status(
            candidate.edge_id,
            GraphEdgeStatus.REVIEWED,
            evidence=["reviewer: prerequisite confirmed for this syllabus"],
        )
        service.build_course(COURSE, infer_prerequisites=True)
        reviewed = service.edge(candidate.edge_id)
        assert reviewed.status is GraphEdgeStatus.REVIEWED
        assert reviewed.metadata["validated"] is True
        assert reviewed.metadata["validated_by"] == "human_review"
        assert "reviewer: prerequisite confirmed for this syllabus" in reviewed.evidence
        assert service.validate(COURSE) == []
    finally:
        database.close()


def test_candidate_mappings_are_not_presented_as_supported_results() -> None:
    from exam_engine.storage.graph_repository import GraphRepository

    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        with database.session() as session:
            repo = GraphRepository(session)
            candidate = None
            for edge in repo.list_edges(
                course_id=COURSE,
                relationship=GraphRelationship.COVERS,
                status=GraphEdgeStatus.CANDIDATE,
            ):
                source = repo.get_node(edge.source_node_id)
                if source is not None and source.node_type is GraphNodeType.QUESTION:
                    candidate = edge
                    break
            assert candidate is not None
            question = repo.get_node(candidate.source_node_id)
            assert question is not None
            assert question.node_type is GraphNodeType.QUESTION
            included = {
                node.node_id
                for node in service.questions_for(
                    COURSE, topic_id=candidate.target_node_id, include_candidates=True
                )
            }
            supported = {
                node.node_id
                for node in service.questions_for(COURSE, topic_id=candidate.target_node_id)
            }
        assert question.node_id in included
        assert question.node_id not in supported

        with database.session() as session:
            repo = GraphRepository(session)
            questions = repo.list_nodes(
                course_id=COURSE, node_type=GraphNodeType.QUESTION, limit=10000
            )
            supported_sources = {
                edge.source_node_id
                for edge in repo.list_edges(
                    course_id=COURSE, relationship=GraphRelationship.ASSESSES
                )
                if edge.status is not GraphEdgeStatus.CANDIDATE
            }
        expected = [node.node_id for node in questions if node.node_id not in supported_sources]
        assert {node.node_id for node in service.questions_without_concepts(COURSE)} == set(
            expected
        )
    finally:
        database.close()


def test_concept_duplicate_review_uses_embeddings_when_available() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        before = {node.node_id for node in service.concepts(COURSE)}
        candidates = service.concept_duplicates(COURSE, threshold=0.0)
        assert candidates
        assert any(candidate.method == "embedding_cosine" for candidate in candidates)
        # Duplicate review must never mutate the graph identity.
        assert {node.node_id for node in service.concepts(COURSE)} == before
    finally:
        database.close()


def test_syllabus_document_answers_topic_coverage_and_course_filters() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        documents = _nodes_of_type(service, COURSE, GraphNodeType.SOURCE_DOCUMENT)
        syllabus_document = next(
            node for node in documents if node.metadata.get("kind") == "syllabus"
        )
        document_id = syllabus_document.document_id or ""
        topics = service.topics_for_document(document_id, course_id=COURSE)
        assert topics
        assert all(node.node_type is GraphNodeType.TOPIC for node in topics)
        assert service.topics_for_document(document_id, course_id="other-course") == []

        topic_id = _topic_id(service, COURSE)
        question = service.questions_for(COURSE, topic_id=topic_id)[0]
        assert service.topics_for_question(question.question_id, course_id=COURSE)
        assert service.topics_for_question(question.question_id, course_id="other-course") == []
        assert service.concepts_for_question(question.question_id, course_id=COURSE)
        assert service.concepts_for_question(question.question_id, course_id="other-course") == []
    finally:
        database.close()


def test_review_downgrade_clears_validation_but_keeps_history() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE, infer_prerequisites=True)
        candidate = next(
            edge
            for edge in _edges(database, COURSE)
            if edge.relationship is GraphRelationship.PREREQUISITE_OF
            and edge.status is GraphEdgeStatus.CANDIDATE
        )
        assert service.set_edge_status(candidate.edge_id, GraphEdgeStatus.REVIEWED)
        reviewed = service.edge(candidate.edge_id)
        assert reviewed.metadata["validated"] is True
        assert reviewed.metadata["reviewed_at"]

        assert service.set_edge_status(candidate.edge_id, GraphEdgeStatus.CANDIDATE)
        downgraded = service.edge(candidate.edge_id)
        assert downgraded.status is GraphEdgeStatus.CANDIDATE
        assert downgraded.metadata["validated"] is False
        assert "reviewed_at" not in downgraded.metadata
        assert downgraded.metadata["review_history"]

        service.build_course(COURSE, infer_prerequisites=True)
        rebuilt = service.edge(candidate.edge_id)
        assert rebuilt.status is GraphEdgeStatus.CANDIDATE
        assert rebuilt.metadata["validated"] is False
        assert rebuilt.metadata["review_history"]
    finally:
        database.close()


def test_source_record_removal_cascades_and_rebuild_drops_stale_nodes() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        questions = service.questions_for(COURSE, topic_id=_topic_id(service, COURSE))
        assert questions
        question = questions[0]
        with database.transaction() as session:
            session.execute(
                delete(QuestionRow).where(QuestionRow.question_id == question.question_id)
            )
        assert service.validate(COURSE) == []
        with pytest.raises(KeyError):
            service.node(question.node_id)
        service.build_course(COURSE)
        with pytest.raises(KeyError):
            service.node(question.node_id)
        assert service.validate(COURSE) == []
    finally:
        database.close()


def _topic_id(service: GraphService, course_id: str) -> str:
    """Return a topic that has at least one question COVERS edge."""
    from exam_engine.storage.graph_repository import GraphRepository

    with service.db.session() as session:
        repo = GraphRepository(session)
        for edge in repo.list_edges(course_id=course_id, relationship=GraphRelationship.COVERS):
            source = repo.get_node(edge.source_node_id)
            if source is not None and source.node_type is GraphNodeType.QUESTION:
                return edge.target_node_id
    raise AssertionError("expected at least one question/topic COVERS edge")


def test_empty_course_is_explicitly_empty() -> None:
    database = memory_database()
    try:
        course = make_course(course_id="empty-graph-v1")
        with database.transaction() as session:
            CourseRepository(session).upsert(course)
        service = GraphService(database)
        report = service.build_course(course.course_id)
        assert report.status is GraphBuildStatus.EMPTY
        assert service.concepts(course.course_id) == []
        assert service.questions_without_concepts(course.course_id) == []
        codes = {warning.code for warning in report.warnings}
        assert {"no_syllabus", "no_concepts", "no_questions"} <= codes
    finally:
        database.close()


def test_concept_only_course_is_not_reported_empty() -> None:
    from exam_engine.storage.nlp_repositories import ConceptRepository

    database = memory_database()
    try:
        course = make_course(course_id="concept-only-v1")
        concept = ConceptRecord(
            concept_id="concept_only_fixture",
            course_id=course.course_id,
            term="Standalone concept",
            normalized="standalone concept",
            origin=ConceptOrigin.INFERRED,
            method=NlpMethod.TFIDF,
            confidence=0.6,
        )
        with database.transaction() as session:
            CourseRepository(session).upsert(course)
            ConceptRepository(session).upsert_many([concept])
        report = GraphService(database).build_course(course.course_id)
        assert report.status is GraphBuildStatus.OK
        assert report.node_count == 2  # course + concept
        codes = {warning.code for warning in report.warnings}
        assert {"no_syllabus", "no_questions"} <= codes
    finally:
        database.close()


def test_isolated_node_query_finds_unreferenced_concepts_only() -> None:
    from exam_engine.models.graph import GraphNode
    from exam_engine.storage import graph_mappers

    database = memory_database()
    try:
        course = make_course(course_id="isolated-graph-v1")
        with database.transaction() as session:
            CourseRepository(session).upsert(course)
        service = GraphService(database)
        service.build_course(course.course_id)
        orphan = GraphNode(
            node_id="nod_isolated_fixture",
            node_type=GraphNodeType.CONCEPT,
            node_key="isolated-graph-v1|concept:orphan",
            label="Orphan concept",
            normalized_label="orphan concept",
            course_id=course.course_id,
        )
        with database.transaction() as session:
            session.add(graph_mappers.node_to_row(orphan))
        isolated = service.isolated_nodes(course.course_id, node_type=GraphNodeType.CONCEPT)
        assert [node.node_id for node in isolated] == [orphan.node_id]
        assert service.isolated_nodes(course.course_id) == [orphan]
        assert service.validate(course.course_id) == []
    finally:
        database.close()


def test_queries_are_course_scoped_and_preserve_syllabus_scope() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        units = _nodes_of_type(service, COURSE, GraphNodeType.UNIT)
        assert units
        unit_id = units[0].entity_id or ""
        scoped = service.concepts(COURSE, unit_id=unit_id)
        assert scoped
        assert all(unit_id in node.metadata.get("unit_ids", []) for node in scoped)
        assert all(node.course_id == COURSE for node in scoped)
        syllabi = _nodes_of_type(service, COURSE, GraphNodeType.SYLLABUS)
        syllabus_id = syllabi[0].entity_id or ""
        assert service.concepts(COURSE, syllabus_id=syllabus_id)

        topic = _topic_id(service, COURSE)
        questions = service.questions_for(COURSE, topic_id=topic)
        assert questions
        assert all(question.course_id == COURSE for question in questions)
        with pytest.raises(ValueError, match="not a topic"):
            service.questions_for(COURSE, topic_id=scoped[0].node_id)
        with pytest.raises(ValueError, match="not a concept"):
            service.questions_for(COURSE, concept_id=topic)
        for question in questions:
            assert topic in {
                item.node_id for item in service.topics_for_question(question.question_id)
            }
            concepts = service.concepts_for_question(question.question_id)
            assert concepts
            assert question.node_id in {
                item.node_id
                for item in service.questions_for(COURSE, concept_id=concepts[0].node_id)
            }
            assert service.topics_for_document(question.document_id or "")
            assert service.topics_for_question(
                question.question_id, course_id=COURSE, syllabus_id=syllabus_id
            )
            assert not service.topics_for_question(
                question.question_id, course_id=COURSE, syllabus_id="other-syllabus"
            )
            assert service.concepts_for_question(
                question.question_id, course_id=COURSE, syllabus_id=syllabus_id
            )
            assert not service.concepts_for_question(
                question.question_id, course_id=COURSE, syllabus_id="other-syllabus"
            )
            assert service.topics_for_document(
                question.document_id or "", course_id=COURSE, syllabus_id=syllabus_id
            )
            assert not service.topics_for_document(
                question.document_id or "", course_id=COURSE, syllabus_id="other-syllabus"
            )

        edge = _edges(database, COURSE)[0]
        evidence = service.evidence_for_edge(edge.edge_id)
        assert evidence.edge.edge_id == edge.edge_id
        assert evidence.source.node_id == edge.source_node_id
        assert evidence.target.node_id == edge.target_node_id
        neighbourhood = service.neighborhood(edge.source_node_id, course_id=COURSE, hops=2)
        assert neighbourhood.nodes
        assert neighbourhood.center.node_id == edge.source_node_id
        assert service.validate(COURSE) == []
    finally:
        database.close()


def _nodes_of_type(service: GraphService, course_id: str, node_type: GraphNodeType):
    from exam_engine.storage.graph_repository import GraphRepository

    with service.db.session() as session:
        return GraphRepository(session).list_nodes(course_id=course_id, node_type=node_type)


def test_cross_course_concepts_stay_separate_in_storage() -> None:
    database = memory_database()
    try:
        source = make_source()
        courses = [
            make_course(
                course_id="shared-topic-a-v1",
                course_code="CS101",
                title="Course A",
            ),
            make_course(
                course_id="shared-topic-b-v1",
                course_code="CS102",
                title="Course B",
            ),
        ]
        with database.transaction() as session:
            DatasetSourceRepository(session).upsert(source)
            for course in courses:
                CourseRepository(session).upsert(course)
                document = _document(
                    f"doc_syl_{course.course_id}", course.course_id, kind=DocumentKind.SYLLABUS
                )
                document = document.model_copy(update={"source_id": source.source_id})
                DocumentRepository(session).upsert(document)
                syllabus = _syllabus(
                    course.course_id, document.document_id, topics=["Binary Search"]
                )
                syllabus = syllabus.model_copy(update={"source_id": source.source_id})
                SyllabusRepository(session).replace(syllabus)

        service = GraphService(database)
        for course in courses:
            report = service.build_course(course.course_id)
            assert report.status is GraphBuildStatus.OK
        first = service.find_concepts("Binary Search", course_id=courses[0].course_id)
        second = service.find_concepts("Binary Search", course_id=courses[1].course_id)
        assert len(first) == len(second) == 1
        assert first[0].node_id != second[0].node_id
        across = service.find_concepts("Binary Search")
        assert {node.course_id for node in across} == {
            courses[0].course_id,
            courses[1].course_id,
        }
        with pytest.raises(KeyError):
            service.questions_for(courses[0].course_id, concept_id=second[0].node_id)
        assert service.validate(courses[0].course_id) == []
        assert service.validate(courses[1].course_id) == []
        concepts_a = service.concepts(courses[0].course_id)
        assert {node.course_id for node in concepts_a} == {courses[0].course_id}
    finally:
        database.close()


def test_same_course_code_across_syllabus_versions_stays_isolated() -> None:
    database = memory_database()
    try:
        source = make_source()
        versions = [
            make_course(
                course_id="version-boundary-v1",
                course_code="CS201",
                title="Version one",
                syllabus_version="v1",
            ),
            make_course(
                course_id="version-boundary-v2",
                course_code="CS201",
                title="Version two",
                syllabus_version="v2",
            ),
        ]
        with database.transaction() as session:
            DatasetSourceRepository(session).upsert(source)
            for course in versions:
                CourseRepository(session).upsert(course)
                document = _document(
                    f"doc_syl_{course.course_id}", course.course_id, kind=DocumentKind.SYLLABUS
                ).model_copy(update={"source_id": source.source_id})
                DocumentRepository(session).upsert(document)
                syllabus = _syllabus(
                    course.course_id, document.document_id, topics=["Binary Search"]
                ).model_copy(update={"source_id": source.source_id})
                SyllabusRepository(session).replace(syllabus)
        service = GraphService(database)
        for course in versions:
            assert service.build_course(course.course_id).status is GraphBuildStatus.OK
        first = service.find_concepts("Binary Search", course_id=versions[0].course_id)
        second = service.find_concepts("Binary Search", course_id=versions[1].course_id)
        assert len(first) == len(second) == 1
        assert first[0].node_id != second[0].node_id
        course_nodes = _nodes_of_type(service, versions[0].course_id, GraphNodeType.COURSE)
        assert course_nodes[0].metadata["syllabus_version"] == "v1"
        assert service.concepts(versions[0].course_id) != service.concepts(versions[1].course_id)
        assert service.validate(versions[0].course_id) == []
    finally:
        database.close()


def test_shared_institution_and_subject_nodes_are_not_deleted_by_a_rebuild() -> None:
    database = memory_database()
    try:
        for course_id, code in [("shared-a-v1", "CS101"), ("shared-b-v1", "CS102")]:
            with database.transaction() as session:
                CourseRepository(session).upsert(
                    make_course(course_id=course_id, course_code=code, title=course_id)
                )
        service = GraphService(database)
        service.build("shared-a-v1")
        service.build("shared-b-v1")
        before = service.status()
        assert len(before) == 2
        from exam_engine.storage.graph_repository import GraphRepository

        with database.session() as session:
            subjects = GraphRepository(session).list_nodes(node_type=GraphNodeType.SUBJECT)
        assert len(subjects) == 2
        shared_titles = {
            title for subject in subjects for title in subject.metadata.get("course_titles", [])
        }
        assert {"shared-a-v1", "shared-b-v1"} <= shared_titles
        service.build("shared-a-v1")
        after = service.status()
        assert after == before
        assert all(not row["dangling_edges"] for row in after)
    finally:
        database.close()


def test_shared_subject_edge_survives_sibling_builds_and_prunes_when_unused() -> None:
    from exam_engine.storage.graph_repository import GraphRepository

    database = memory_database()
    try:
        versions = [
            make_course(
                course_id="shared-code-v1",
                course_code="CS201",
                title="Version one",
                syllabus_version="v1",
            ),
            make_course(
                course_id="shared-code-v2",
                course_code="CS201",
                title="Version two",
                syllabus_version="v2",
            ),
        ]
        with database.transaction() as session:
            for course in versions:
                CourseRepository(session).upsert(course)
        service = GraphService(database)
        reports = [service.build_course(course.course_id) for course in versions]
        assert all(report.edge_count == 2 for report in reports)
        assert all(report.shared_edge_count == 1 for report in reports)
        status = {row["course_id"]: row for row in service.status()}
        assert {course.course_id: status[course.course_id]["edges"] for course in versions} == {
            versions[0].course_id: 2,
            versions[1].course_id: 2,
        }
        for course in versions:
            assert service.status(course.course_id) == [status[course.course_id]]
        with database.session() as session:
            repo = GraphRepository(session)
            shared_edges = [
                edge
                for edge in repo.list_edges(relationship=GraphRelationship.BELONGS_TO)
                if edge.course_id is None
            ]
            subjects = repo.list_nodes(node_type=GraphNodeType.SUBJECT)
        assert len(shared_edges) == 1
        assert len(subjects) == 1
        assert subjects[0].metadata["course_titles"] == ["Version one", "Version two"]
        assert service.validate(versions[0].course_id) == []
        assert service.validate(versions[1].course_id) == []

        # Rebuilding one version must not steal or double the shared edge.
        service.build_course(versions[0].course_id)
        status = {row["course_id"]: row for row in service.status()}
        assert status[versions[0].course_id]["edges"] == 2
        assert status[versions[1].course_id]["edges"] == 2

        # The shared pair survives while one version still references it.
        service.reset(versions[0].course_id)
        with database.session() as session:
            repo = GraphRepository(session)
            assert len(repo.list_nodes(node_type=GraphNodeType.SUBJECT)) == 1
            assert len(repo.list_nodes(node_type=GraphNodeType.INSTITUTION)) == 1
            assert len(repo.list_edges(relationship=GraphRelationship.BELONGS_TO)) == 3

        # Removing the last user prunes the shared edge and both shared nodes.
        service.reset(versions[1].course_id)
        with database.session() as session:
            repo = GraphRepository(session)
            assert repo.list_nodes() == []
            assert repo.list_edges() == []
    finally:
        database.close()


def test_validate_rejects_invalid_relationship_endpoints() -> None:
    from exam_engine.models.graph import GraphEdge
    from exam_engine.storage import graph_mappers

    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        concept = service.concepts(COURSE)[0]
        question = _nodes_of_type(service, COURSE, GraphNodeType.QUESTION)[0]
        invalid = GraphEdge(
            edge_id="rel_invalid_type_fixture",
            course_id=COURSE,
            source_node_id=concept.node_id,
            target_node_id=question.node_id,
            relationship=GraphRelationship.SIMILAR_TO,
            status=GraphEdgeStatus.INFERRED,
            method="test",
            evidence=["fixture invalid endpoint types"],
        )
        with database.transaction() as session:
            session.add(graph_mappers.edge_to_row(invalid))
        problems = service.validate(COURSE)
        assert any("invalid SIMILAR_TO endpoints" in problem for problem in problems)
    finally:
        database.close()


def test_invalid_edge_endpoint_is_rejected_atomically() -> None:
    from exam_engine.models.graph import GraphEdge, GraphNode
    from exam_engine.storage.graph_repository import GraphRepository

    database = memory_database()
    try:
        course = make_course(course_id="invalid-edge-v1")
        with database.transaction() as session:
            CourseRepository(session).upsert(course)
        service = GraphService(database)
        service.build_course(course.course_id)
        before = service.status()[0]
        node = GraphNode(
            node_id="nod_fixture",
            node_type=GraphNodeType.CONCEPT,
            node_key="invalid-edge-v1|concept:fixture",
            label="Fixture",
            course_id=course.course_id,
        )
        edge = GraphEdge(
            edge_id="rel_fixture",
            course_id=course.course_id,
            source_node_id=node.node_id,
            target_node_id="nod_missing",
            relationship=GraphRelationship.COVERS,
            status=GraphEdgeStatus.INFERRED,
            method="test",
            evidence=["fixture"],
        )
        with (
            pytest.raises(ValueError, match="outside the built graph"),
            database.transaction() as session,
        ):
            GraphRepository(session).replace_course(course.course_id, [node], [edge])
        assert service.status()[0] == before
        assert service.validate(course.course_id) == []
    finally:
        database.close()


def test_similarity_edge_source_provenance_matches_source_question() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        edges = [
            edge
            for edge in _edges(database, COURSE)
            if edge.relationship is GraphRelationship.SIMILAR_TO
        ]
        assert edges
        for edge in edges:
            source = service.node(edge.source_node_id)
            if source.document_id is None:
                continue
            # The endpoints are sorted for deterministic ids; provenance must
            # follow the node that actually became the source.
            assert edge.source_document_id == source.document_id
            assert edge.source_page == source.metadata.get("page_number")
    finally:
        database.close()


def test_similarity_edges_do_not_merge_question_identity() -> None:
    database = memory_database()
    try:
        _prepare_analytics(database)
        service = GraphService(database)
        service.build_course(COURSE)
        edges = [
            edge
            for edge in _edges(database, COURSE)
            if edge.relationship is GraphRelationship.SIMILAR_TO
        ]
        assert edges
        assert all(edge.status is GraphEdgeStatus.INFERRED for edge in edges)
        assert all("similarity_kind:" in " ".join(edge.evidence) for edge in edges)
        assert all(edge.metadata.get("similarity_kinds") for edge in edges)
        kinds = {kind for edge in edges for kind in edge.metadata.get("similarity_kinds", [])}
        assert "exact" in kinds
        assert kinds & {"near", "semantic"}
        questions = _nodes_of_type(service, COURSE, GraphNodeType.QUESTION)
        assert len({node.node_id for node in questions}) == len(questions)
        assert len({node.question_id for node in questions}) == len(questions)
        assert all(
            edge.relationship is GraphRelationship.SIMILAR_TO
            and edge.source_node_id != edge.target_node_id
            for edge in edges
        )
    finally:
        database.close()
