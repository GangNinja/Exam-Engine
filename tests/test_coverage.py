"""Phase 7 tests: coverage classification, provenance, scoping and isolation."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from exam_engine.config import CoverageSettings
from exam_engine.coverage import CoverageError, CoverageService
from exam_engine.models.coverage import (
    CoverageFilters,
    CoverageRequest,
    CoverageStatus,
)
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    CoverageReportStatus,
    DifficultyLabel,
    DocumentKind,
    ExtractionStatus,
    GraphEdgeStatus,
    GraphNodeType,
    GraphRelationship,
    MappingStatus,
    QuestionType,
    SourceFormat,
)
from exam_engine.models.graph import GraphEdge, GraphNode, make_edge_id, make_node_id
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)
from exam_engine.models.understanding import (
    DifficultyAssessment,
    QuestionAnalysis,
    UnitMatch,
    make_analysis_id,
    make_difficulty_id,
    make_unit_match_id,
)
from exam_engine.nlp.keywords import normalize_term
from exam_engine.storage import graph_mappers
from exam_engine.storage.db import Database, memory_database
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from tests.support import make_course, make_source

COURSE = "phase7-course-v1"
COURSE_B = "phase7-course-b-v1"


def _add_course(database: Database, course_id: str = COURSE) -> None:
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id="phase7-fixture"))
        CourseRepository(session).upsert(
            make_course(
                course_id=course_id,
                course_code="CS701",
                title="Phase 7 synthetic coverage fixture",
            )
        )


def _syllabus_units(
    syllabus_id: str,
    units: list[tuple[str, str, list[str]]],
) -> list[SyllabusUnitRecord]:
    records: list[SyllabusUnitRecord] = []
    for index, (label, title, topics) in enumerate(units, start=1):
        records.append(
            SyllabusUnitRecord(
                unit_id=make_unit_id(syllabus_id, label, title),
                unit_label=label,
                unit_number=index,
                title=title,
                topics=topics,
                page_number=index,
                confidence=0.9,
            )
        )
    return records


def _add_syllabus(
    database: Database,
    course_id: str = COURSE,
    *,
    suffix: str = "a",
    units: list[tuple[str, str, list[str]]] | None = None,
) -> SyllabusRecord:
    document_id = f"doc_syllabus_{course_id}_{suffix}"
    syllabus_id = make_syllabus_id(document_id)
    units = units or [
        ("Unit 1", "Foundations", ["Arrays", "Linked Lists"]),
        ("Unit 2", "Structures", ["Trees", "Graphs"]),
    ]
    syllabus = SyllabusRecord(
        syllabus_id=syllabus_id,
        course_id=course_id,
        document_id=document_id,
        source_id="phase7-fixture",
        title=f"Synthetic syllabus {suffix}",
        units=_syllabus_units(syllabus_id, units),
        confidence=0.9,
    )
    document = DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id="phase7-fixture",
        kind=DocumentKind.SYLLABUS,
        format=SourceFormat.TXT,
        title=f"SYNTHETIC syllabus {suffix}",
        original_filename=f"syllabus_{suffix}.txt",
        content_sha256=hashlib.sha256(document_id.encode()).hexdigest(),
        byte_size=100,
        page_count=2,
        char_count=200,
        extraction_status=ExtractionStatus.OK,
        pipeline_version="phase7-test",
    )
    with database.transaction() as session:
        DocumentRepository(session).upsert(document)
        SyllabusRepository(session).replace(syllabus)
    return syllabus


def _add_paper(
    database: Database,
    course_id: str,
    document_id: str,
    *,
    title: str,
    session: str | None,
    questions: list[tuple[str, str, float | None]],
    exam_type: str = "final",
) -> dict[str, QuestionRecord]:
    metadata = {}
    if session is not None:
        metadata = {"exam_session": session, "exam_type": exam_type}
    document = DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id="phase7-fixture",
        kind=DocumentKind.QUESTION_PAPER,
        format=SourceFormat.TXT,
        title=title,
        original_filename=f"{document_id}.txt",
        content_sha256=hashlib.sha256(f"{course_id}:{document_id}".encode()).hexdigest(),
        byte_size=200,
        page_count=1,
        char_count=500,
        extraction_status=ExtractionStatus.OK,
        pipeline_version="phase7-test",
        metadata=metadata,
    )
    records = {
        question_id: QuestionRecord(
            question_id=question_id,
            document_id=document_id,
            course_id=course_id,
            text=text,
            question_number=str(index),
            number_sort=index,
            marks=marks,
            page_number=1,
            confidence=0.9,
            metadata=dict(metadata),
        )
        for index, (question_id, text, marks) in enumerate(questions, start=1)
    }
    with database.transaction() as session:
        DocumentRepository(session).upsert(document)
        QuestionRepository(session).replace_for_document(document_id, list(records.values()))
    return records


def _unit_for_topic(syllabus: SyllabusRecord, topic: str) -> SyllabusUnitRecord:
    normalized = normalize_term(topic)
    for unit in syllabus.units:
        if normalized in {normalize_term(item) for item in unit.topics}:
            return unit
    raise AssertionError(f"no unit contains {topic!r}")


def _add_analysis(
    database: Database,
    course_id: str,
    syllabus: SyllabusRecord,
    question: QuestionRecord,
    *,
    status: MappingStatus,
    matches: list[tuple[str, str, float, int, str]] | None = None,
    primary_type: QuestionType | None = QuestionType.EXPLANATION,
) -> None:
    """Persist one deterministic Phase 3 analysis plus its unit matches."""
    matches = matches or []
    uncertainty: list[Uncertainty] = []
    if status is MappingStatus.UNMAPPED:
        uncertainty = [
            Uncertainty(
                code="unmapped_question",
                message="no syllabus unit met the configured mapping threshold (0.18)",
                detail={"question_id": question.question_id},
            )
        ]
    elif status is MappingStatus.AMBIGUOUS:
        uncertainty = [
            Uncertainty(
                code="ambiguous_mapping",
                message="several syllabus units matched within the configured margin",
            )
        ]
    records: list[UnitMatch] = []
    for topic, method, score, rank, _unused in matches:
        unit = _unit_for_topic(syllabus, topic)
        records.append(
            UnitMatch(
                match_id=make_unit_match_id(
                    course_id, question.question_id, unit.unit_id, topic, method
                ),
                question_id=question.question_id,
                course_id=course_id,
                unit_id=unit.unit_id,
                syllabus_id=syllabus.syllabus_id,
                unit_label=unit.unit_label,
                unit_title=unit.title,
                topic=topic,
                score=score,
                rank=rank,
                method=method,
                matched_terms=[normalize_term(topic)],
                evidence=[f"fixture mapping to {topic!r}"],
            )
        )
    top = records[0] if records else None
    analysis = QuestionAnalysis(
        analysis_id=make_analysis_id(course_id, question.question_id),
        question_id=question.question_id,
        course_id=course_id,
        document_id=question.document_id,
        text_sha256=hashlib.sha256(question.text.encode()).hexdigest(),
        word_count=len(question.text.split()),
        sentence_count=1,
        mapping_status=status,
        mapping_confidence=max((record.score for record in records), default=0.0),
        top_unit_id=top.unit_id if top else None,
        primary_type=primary_type,
        primary_type_confidence=0.8 if primary_type else 0.0,
        difficulty_label=DifficultyLabel.UNKNOWN,
        structure_confidence=0.8,
        uncertainties=uncertainty,
    )
    difficulty = DifficultyAssessment(
        difficulty_id=make_difficulty_id(course_id, question.question_id, "fixture"),
        question_id=question.question_id,
        course_id=course_id,
        label=DifficultyLabel.UNKNOWN,
        score=0.4,
        confidence=0.0,
        method="fixture",
    )
    with database.transaction() as session:
        QuestionUnderstandingRepository(session).replace_for_question(
            analysis=analysis,
            matches=records,
            types=[],
            bloom=[],
            difficulty=difficulty,
        )


def _add_graph_assessment(
    database: Database,
    course_id: str,
    syllabus: SyllabusRecord,
    question: QuestionRecord,
    concept_label: str,
    *,
    status: GraphEdgeStatus = GraphEdgeStatus.EXPLICIT,
    syllabus_scoped: bool = True,
    aliases: list[str] | None = None,
) -> GraphEdge:
    normalized = normalize_term(concept_label)
    concept = GraphNode(
        node_id=make_node_id(GraphNodeType.CONCEPT, f"{course_id}|concept:{normalized}"),
        node_type=GraphNodeType.CONCEPT,
        node_key=f"{course_id}|concept:{normalized}",
        label=concept_label,
        normalized_label=normalized,
        course_id=course_id,
        entity_id=normalized,
        metadata={"aliases": aliases or [concept_label], "origins": ["syllabus"]},
    )
    question_node = GraphNode(
        node_id=make_node_id(GraphNodeType.QUESTION, question.question_id),
        node_type=GraphNodeType.QUESTION,
        node_key=question.question_id,
        label=question.text,
        course_id=course_id,
        question_id=question.question_id,
        document_id=question.document_id,
        entity_id=question.question_id,
    )
    edge = GraphEdge(
        edge_id=make_edge_id(question_node.node_id, GraphRelationship.ASSESSES, concept.node_id),
        course_id=course_id,
        syllabus_id=syllabus.syllabus_id if syllabus_scoped else None,
        source_node_id=question_node.node_id,
        target_node_id=concept.node_id,
        relationship=GraphRelationship.ASSESSES,
        status=status,
        method="fixture_explicit_assessment",
        evidence=[f"reviewer mapped {question.question_id} to {concept.label}"],
        metadata={"validated": True},
    )
    with database.transaction() as session:
        session.merge(graph_mappers.node_to_row(concept))
        session.merge(graph_mappers.node_to_row(question_node))
        GraphRepository(session).upsert_edge(edge)
    return edge


def _service(
    database: Database,
    *,
    settings: CoverageSettings | None = None,
) -> CoverageService:
    return CoverageService(database, settings or CoverageSettings())


def _report(
    database: Database,
    course_id: str = COURSE,
    *,
    syllabus: SyllabusRecord | None = None,
    filters: CoverageFilters | None = None,
    settings: CoverageSettings | None = None,
):
    return _service(database, settings=settings).analyze(
        CoverageRequest(
            course_id=course_id,
            syllabus_id=syllabus.syllabus_id if syllabus else None,
            filters=filters or CoverageFilters(),
        )
    )


def _five_questions(
    database: Database,
    course_id: str = COURSE,
    *,
    document_id: str = "doc_paper_2024",
    session: str | None = "2024-05",
    prefix: str = "q",
) -> dict[str, QuestionRecord]:
    return _add_paper(
        database,
        course_id,
        document_id,
        title="SYNTHETIC question paper",
        session=session,
        questions=[
            (f"{prefix}1", "Explain array indexing and memory layout.", 5.0),
            (f"{prefix}2", "Compare linked lists and arrays.", 5.0),
            (f"{prefix}3", "Describe tree traversal.", 4.0),
            (f"{prefix}4", "Discuss graph representations.", 6.0),
            (f"{prefix}5", "Explain hashing collisions.", None),
        ],
    )


def test_covered_concept_uses_distinct_questions_documents_sessions_and_marks() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_paper_one",
            title="Paper one",
            session="2024-05",
            questions=[
                ("q1", "Explain array indexing.", 5.0),
                ("q2", "Compare arrays with linked lists.", 4.0),
            ],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_paper_two",
            title="Paper two",
            session="2024-11",
            questions=[
                ("q3", "Describe array traversal.", 6.0),
                ("q4", "Explain a tree.", 5.0),
                ("q5", "Explain hashing collisions.", None),
            ],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            first["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf+semantic", 0.82, 1, "")],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            first["q2"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.66, 1, "")],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            second["q3"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "semantic", 0.71, 1, "")],
            primary_type=QuestionType.APPLICATION,
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            second["q4"],
            status=MappingStatus.UNMAPPED,
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            second["q5"],
            status=MappingStatus.UNMAPPED,
        )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")

        assert arrays.status is CoverageStatus.COVERED
        assert arrays.distinct_question_count == 3
        assert arrays.document_count == 2
        assert arrays.session_count == 2
        assert arrays.mapped_marks == 15.0
        assert arrays.questions_with_marks == 3
        assert arrays.question_type_counts == {"application": 1, "explanation": 2}
        assert arrays.average_mapping_confidence == pytest.approx((0.82 + 0.66 + 0.71) / 3)
        assert arrays.scored_mapping_count == 3
        assert arrays.supporting_questions[0].document_title in {"Paper one", "Paper two"}
        assert report.summary.mapped_questions == 3
        assert report.summary.concept_coverage_rate == pytest.approx(0.25)
        assert report.status is CoverageReportStatus.OK
    finally:
        database.close()


def test_duplicate_source_records_do_not_inflate_documents_sources_or_sessions() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        # Both papers come from the same dataset source and the same exam session.
        first = _add_paper(
            database,
            COURSE,
            "doc_duplicate_source_one",
            title="Paper one",
            session="2024-05",
            questions=[
                ("q1", "Explain array indexing.", 5.0),
                ("q2", "Compare arrays with linked lists.", 5.0),
            ],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_duplicate_source_two",
            title="Paper two",
            session="2024-05",
            questions=[("q3", "Describe array traversal.", 5.0)],
        )
        for question in (first["q1"], second["q3"]):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf", 0.8, 1, "")],
            )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            first["q2"],
            status=MappingStatus.UNMAPPED,
        )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")

        # Two distinct documents from one source and one shared session.
        assert arrays.document_count == 2
        assert arrays.session_count == 1
        assert arrays.distinct_question_count == 2
        assert arrays.mapped_marks == 10.0
        assert report.data_quality.documents == 2
        assert report.data_quality.sources == 1
        assert report.data_quality.source_ids == ["phase7-fixture"]
        assert report.data_quality.known_sessions == 1
        assert report.summary.total_known_marks == 15.0
        assert report.summary.mapped_marks == 10.0
    finally:
        database.close()


def test_single_mapped_question_is_low_coverage_not_covered() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.9, 1, "")],
        )
        for question_id in ("q2", "q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        assert arrays.status is CoverageStatus.LOW_COVERAGE
        assert report.summary.covered_concepts == 0
        assert report.summary.low_coverage_concepts == 1
        assert "below the configured covered minimum 2" in arrays.reasons[0]
    finally:
        database.close()


def test_zero_mapped_questions_is_unmapped_and_not_a_future_exam_claim() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.UNMAPPED,
            )

        report = _report(database, syllabus=syllabus)
        assert report.status is CoverageReportStatus.OK
        for finding in report.concepts:
            assert finding.status is CoverageStatus.UNMAPPED
            assert "does not mean the concept" in finding.reasons[1]
        assert len(report.unmapped_questions) == 5
        assert report.unmapped_questions[0].reason == "no_mapping_above_threshold"
        assert "not proven to be outside" not in report.unmapped_questions[0].explanation
    finally:
        database.close()


def test_topic_fallback_is_opt_in_and_keeps_its_provenance() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(
            database,
            units=[
                (
                    "Unit 1",
                    "Biochemistry",
                    [
                        "Enzymes, types and action",
                        "Photosynthesis",
                        "Respiration",
                        "Cell division",
                        "Genetics",
                        "Ecology",
                    ],
                )
            ],
        )
        questions = _add_paper(
            database,
            COURSE,
            "doc_topic_fallback",
            title="Fallback paper",
            session="2024-05",
            questions=[("q1", "Explain enzyme action and classification.", 4.0)],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            status=MappingStatus.UNMAPPED,
        )

        default = _report(database, syllabus=syllabus)
        assert default.concepts[0].distinct_question_count == 0

        fallback = _service(database).analyze(
            CoverageRequest(course_id=COURSE, syllabus_id=syllabus.syllabus_id),
            include_topic_fallback=True,
        )
        enzyme = next(item for item in fallback.concepts if item.label.startswith("Enzymes"))
        assert enzyme.distinct_question_count == 1
        assert enzyme.supporting_questions[0].mapping_sources == ["coverage_topic_fallback"]
        assert "enzyme" in enzyme.supporting_questions[0].matched_terms
        assert fallback.summary.mapped_questions == 1

        complete = _service(database).analyze(
            CoverageRequest(course_id=COURSE, syllabus_id=syllabus.syllabus_id),
            include_topic_fallback=True,
            include_question_assignments=True,
        )
        assert len(complete.question_assignments) == complete.summary.total_questions
        assert all(
            0.75 <= assignment.confidence <= 1.0
            for assignment in complete.question_assignments
        )
        assert complete.question_assignments[0].concept_id == enzyme.concept_id
    finally:
        database.close()


def test_unmapped_question_is_not_labelled_out_of_syllabus() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.UNMAPPED,
            )
        report = _report(database, syllabus=syllabus)
        item = report.unmapped_questions[0]
        assert item.reason == "no_mapping_above_threshold"
        assert "not proven to be outside" not in item.explanation
        assert "out-of-syllabus" not in item.explanation.casefold()
        assert report.limitations[0].startswith("Coverage describes the available")
    finally:
        database.close()


def test_multiple_units_are_scoped_and_rates_use_the_right_denominators() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        mappings = {
            "q1": "Arrays",
            "q2": "Linked Lists",
            "q3": "Trees",
            "q4": "Trees",
            "q5": "Graphs",
        }
        for question_id, topic in mappings.items():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.MAPPED,
                matches=[(topic, "tfidf", 0.8, 1, "")],
            )

        report = _report(database, syllabus=syllabus)
        unit_one, unit_two = report.units
        assert unit_one.unit_label == "Unit 1"
        assert unit_one.concept_count == 2
        assert unit_one.distinct_question_count == 2
        assert unit_one.concept_coverage_rate == 0.0
        assert unit_two.unit_label == "Unit 2"
        assert unit_two.concept_count == 2
        assert unit_two.distinct_question_count == 3
        assert unit_two.concept_coverage_rate == 0.5
        assert report.summary.concept_coverage_rate == 0.25
        assert report.summary.mapped_questions == 5
        assert report.summary.mapped_question_rate == 1.0
    finally:
        database.close()


def test_one_question_can_map_to_two_concepts_via_explicit_graph_assessment() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_graph_assessment(database, COURSE, syllabus, questions["q1"], "Arrays")
        _add_graph_assessment(database, COURSE, syllabus, questions["q1"], "Trees")
        for question_id in ("q2", "q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        trees = next(item for item in report.concepts if item.label == "Trees")
        assert arrays.status is CoverageStatus.LOW_COVERAGE
        assert trees.status is CoverageStatus.LOW_COVERAGE
        assert arrays.distinct_question_count == 1
        assert trees.distinct_question_count == 1
        assert report.summary.mapped_questions == 1
        assert report.data_quality.graph_assessment_edges == 2
        assert "phase5_explicit_assessment" in arrays.supporting_questions[0].mapping_sources
    finally:
        database.close()


def test_graph_alias_enriches_concept_identity_without_inventing_a_mapping() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_graph_assessment(database, COURSE, syllabus, questions["q1"], "Arrays")
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.UNMAPPED,
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        assert arrays.graph_concept_id is not None
        assert "Arrays" in arrays.aliases
        assert arrays.distinct_question_count == 1
        assert arrays.minimum_mapping_confidence is None
        assert arrays.average_mapping_confidence is None
    finally:
        database.close()


def test_graph_alias_is_used_for_identity_and_explicit_edge_matching() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database, units=[("Unit 1", "Foundations", ["BST"])])
        questions = _five_questions(database)
        _add_graph_assessment(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            "Binary search tree",
            status=GraphEdgeStatus.REVIEWED,
            aliases=["BST"],
        )

        report = _report(database, syllabus=syllabus)
        bst = next(item for item in report.concepts if item.label == "BST")
        assert bst.graph_concept_id is not None
        assert "BST" in bst.aliases
        assert "Binary search tree" in bst.aliases
        assert bst.distinct_question_count == 1
        assert bst.supporting_questions[0].graph_edge_status is GraphEdgeStatus.REVIEWED
        assert report.data_quality.graph_assessment_edges == 1
        assert report.summary.mapped_questions == 1
    finally:
        database.close()


def test_unscoped_graph_edge_does_not_cross_syllabus_versions() -> None:
    database = memory_database()
    try:
        _add_course(database)
        first = _add_syllabus(database, suffix="v1", units=[("Unit 1", "A", ["Arrays", "Trees"])])
        second = _add_syllabus(database, suffix="v2", units=[("Unit 1", "B", ["Graphs"])])
        first_paper = _add_paper(
            database,
            COURSE,
            "doc_unscoped_first",
            title="First version paper",
            session="2024-05",
            questions=[("q1", "Explain arrays.", 5.0)],
        )
        second_paper = _add_paper(
            database,
            COURSE,
            "doc_unscoped_second",
            title="Second version paper",
            session="2024-11",
            questions=[("q3", "Explain graphs.", 5.0)],
        )
        _add_analysis(
            database,
            COURSE,
            first,
            first_paper["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.9, 1, "")],
        )
        _add_analysis(
            database,
            COURSE,
            second,
            second_paper["q3"],
            status=MappingStatus.MAPPED,
            matches=[("Graphs", "tfidf", 0.9, 1, "")],
        )
        _add_graph_assessment(
            database,
            COURSE,
            first,
            first_paper["q1"],
            "Trees",
            syllabus_scoped=False,
        )

        report = _report(database, syllabus=first)
        trees = next(item for item in report.concepts if item.label == "Trees")
        assert trees.distinct_question_count == 0
        assert report.summary.mapped_questions == 1
        assert report.data_quality.graph_assessment_edges == 0
        assert report.data_quality.unscoped_questions_excluded == 1
    finally:
        database.close()


def test_unit_marks_count_each_question_once_when_it_supports_two_concepts() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_graph_assessment(database, COURSE, syllabus, questions["q1"], "Arrays")
        _add_graph_assessment(database, COURSE, syllabus, questions["q1"], "Linked Lists")

        report = _report(database, syllabus=syllabus)
        unit = report.units[0]
        assert unit.distinct_question_count == 1
        assert unit.mapped_marks == 5.0
        assert unit.questions_with_marks == 1
        assert unit.mapped_marks_share == 0.25
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        linked = next(item for item in report.concepts if item.label == "Linked Lists")
        # A concept keeps the whole question's marks; the unit does not double count it.
        assert arrays.mapped_marks == 5.0
        assert linked.mapped_marks == 5.0
    finally:
        database.close()


def test_distinct_documents_with_one_shared_session_are_not_double_counted() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_shared_session_one",
            title="Paper one",
            session="2024-05",
            questions=[("q1", "Explain arrays.", 5.0), ("q2", "Describe arrays.", 5.0)],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_shared_session_two",
            title="Paper two",
            session="2024-05",
            questions=[("q3", "Explain arrays again.", 5.0), ("q4", "Describe arrays.", 5.0)],
        )
        for question in (*first.values(), *second.values()):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf", 0.9, 1, "")],
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        assert arrays.distinct_question_count == 4
        assert arrays.document_count == 2
        assert arrays.session_count == 1
        assert report.summary.known_sessions == 1
    finally:
        database.close()


def test_ambiguous_mapping_is_uncertain_and_never_counted_as_coverage() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            status=MappingStatus.AMBIGUOUS,
            matches=[
                ("Arrays", "tfidf", 0.45, 1, ""),
                ("Linked Lists", "tfidf", 0.44, 2, ""),
            ],
        )
        for question_id in ("q2", "q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        linked = next(item for item in report.concepts if item.label == "Linked Lists")
        assert arrays.status is CoverageStatus.UNCERTAIN
        assert linked.status is CoverageStatus.UNCERTAIN
        assert arrays.distinct_question_count == 0
        assert arrays.candidate_mappings[0].reason == "ambiguous_mapping"
        assert report.summary.mapped_questions == 0
        assert report.unmapped_questions[0].reason == "ambiguous_mapping"
        assert any(warning.code == "coverage_ambiguous_evidence" for warning in report.warnings)
    finally:
        database.close()


def test_mapping_below_confidence_floor_is_candidate_not_coverage() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.10, 1, "")],
        )
        for question_id in ("q2", "q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        assert arrays.status is CoverageStatus.UNCERTAIN
        assert arrays.distinct_question_count == 0
        assert arrays.candidate_mappings[0].reason == "below_confidence_floor"
        assert report.summary.mapped_questions == 0
    finally:
        database.close()


def test_low_confidence_accepted_mappings_are_uncertain_even_when_plural() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "semantic", 0.30, 1, "")],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q2"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "semantic", 0.34, 1, "")],
        )
        for question_id in ("q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        assert arrays.status is CoverageStatus.UNCERTAIN
        assert arrays.distinct_question_count == 2
        assert "below the configured coverage threshold" in arrays.reasons[0]
        assert any(warning.code == "coverage_low_confidence" for warning in arrays.warnings)
    finally:
        database.close()


def test_threshold_is_configurable() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        for question_id in ("q1", "q2"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf", 0.8, 1, "")],
            )
        for question_id in ("q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )
        default = _report(database, syllabus=syllabus)
        strict = _report(
            database,
            syllabus=syllabus,
            settings=replace(CoverageSettings(), covered_min_questions=3),
        )
        assert (
            next(item for item in default.concepts if item.label == "Arrays").status
            is CoverageStatus.COVERED
        )
        strict_arrays = next(item for item in strict.concepts if item.label == "Arrays")
        assert strict_arrays.status is CoverageStatus.LOW_COVERAGE
        assert strict.parameters["covered_min_questions"] == 3
    finally:
        database.close()


def test_missing_marks_and_missing_sessions_are_explicit() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_unknown_session",
            title="Paper without session",
            session=None,
            questions=[(f"q{i}", f"Question {i} about arrays.", None) for i in range(1, 6)],
        )
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf", 0.8, 1, "")],
            )

        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        assert arrays.distinct_question_count == 5
        assert arrays.session_count == 0
        assert arrays.mapped_marks == 0.0
        assert arrays.questions_with_marks == 0
        assert arrays.average_mapping_confidence == 0.8
        assert report.summary.total_known_marks == 0.0
        assert report.summary.mapped_marks_share is None
        assert report.summary.missing_marks == 5
        assert report.data_quality.questions_without_session == 5
        assert any(warning.code == "missing_period_metadata" for warning in report.warnings)
        assert any(warning.code == "coverage_missing_marks" for warning in report.warnings)
        assert any("have no stated marks" in item for item in report.limitations)
    finally:
        database.close()


def test_empty_question_corpus_is_insufficient_data_with_no_fake_zero_rates() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        report = _report(database, syllabus=syllabus)
        assert report.status is CoverageReportStatus.INSUFFICIENT_DATA
        assert report.summary.total_questions == 0
        assert report.summary.analysis_coverage_rate is None
        assert report.summary.mapped_question_rate is None
        assert report.summary.concept_coverage_rate == 0.0
        assert all(
            finding.status is CoverageStatus.INSUFFICIENT_DATA for finding in report.concepts
        )
        assert report.units[0].concept_coverage_rate == 0.0
        assert report.units[0].question_share is None
    finally:
        database.close()


def test_no_analysis_is_insufficient_but_missing_some_analysis_is_uncertain() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        raw = _report(database, syllabus=syllabus)
        assert raw.status is CoverageReportStatus.INSUFFICIENT_DATA
        assert raw.summary.analysed_questions == 0

        for question_id in ("q1", "q2"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )
        partial = _report(database, syllabus=syllabus)
        arrays = next(item for item in partial.concepts if item.label == "Arrays")
        assert partial.status is CoverageReportStatus.PARTIAL
        assert arrays.status is CoverageStatus.UNCERTAIN
        assert partial.summary.analysed_questions == 2
        assert partial.summary.analysis_coverage_rate == 0.4
        assert any(warning.code == "coverage_partial_analysis" for warning in partial.warnings)
    finally:
        database.close()


def test_small_corpus_prevents_absence_claim_but_keeps_positive_evidence() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_small",
            title="Small paper",
            session="2024-05",
            questions=[
                ("q1", "Explain arrays.", 5.0),
                ("q2", "Describe trees.", 5.0),
            ],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.8, 1, "")],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q2"],
            status=MappingStatus.UNMAPPED,
        )
        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        trees = next(item for item in report.concepts if item.label == "Trees")
        assert arrays.status is CoverageStatus.LOW_COVERAGE
        assert trees.status is CoverageStatus.UNCERTAIN
        assert report.status is CoverageReportStatus.PARTIAL
        assert report.data_quality.small_corpus is True
        assert any(warning.code == "coverage_small_corpus" for warning in trees.warnings)
    finally:
        database.close()


def test_reports_are_deterministic_and_read_only() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf", 0.8, 1, "")],
            )
        before = _report(database, syllabus=syllabus).model_dump(mode="json")
        after_report = _report(database, syllabus=syllabus)
        after = after_report.model_dump(mode="json")
        before.pop("generated_at")
        after.pop("generated_at")
        assert before == after
        assert before["report_id"] == after["report_id"]
        with database.session() as session:
            assert len(SyllabusRepository(session).get_by_course(COURSE)) == 1
            assert QuestionRepository(session).count_by_course(COURSE) == 5
    finally:
        database.close()


def test_unknown_course_and_syllabus_are_structured_errors() -> None:
    database = memory_database()
    try:
        with pytest.raises(CoverageError) as missing_course:
            _report(database, "missing-course")
        assert missing_course.value.code == "unknown_course"

        _add_course(database)
        syllabus = _add_syllabus(database)
        with pytest.raises(CoverageError) as missing_syllabus:
            _report(
                database,
                syllabus=syllabus,
                filters=CoverageFilters(document_ids=["missing-document"]),
            )
        assert missing_syllabus.value.code == "invalid_coverage_filter"
    finally:
        database.close()


def test_course_without_syllabus_fails_safely() -> None:
    database = memory_database()
    try:
        _add_course(database)
        with pytest.raises(CoverageError) as error:
            _report(database)
        assert error.value.code == "no_syllabus"
    finally:
        database.close()


def test_multiple_syllabus_versions_require_selection_and_do_not_mix_question_corpora() -> None:
    database = memory_database()
    try:
        _add_course(database)
        first = _add_syllabus(database, suffix="v1", units=[("Unit 1", "A", ["Arrays"])])
        second = _add_syllabus(database, suffix="v2", units=[("Unit 1", "B", ["Trees"])])
        first_paper = _add_paper(
            database,
            COURSE,
            "doc_first_version",
            title="Version one paper",
            session="2024-05",
            questions=[
                ("q1", "Explain arrays.", 5.0),
                ("q2", "Describe arrays again.", 5.0),
            ],
        )
        second_paper = _add_paper(
            database,
            COURSE,
            "doc_second_version",
            title="Version two paper",
            session="2024-11",
            questions=[("q3", "Explain arrays again.", 5.0)],
        )
        _add_analysis(
            database,
            COURSE,
            first,
            first_paper["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.9, 1, "")],
        )
        _add_analysis(
            database,
            COURSE,
            first,
            first_paper["q2"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.7, 1, "")],
        )
        _add_analysis(
            database,
            COURSE,
            second,
            second_paper["q3"],
            status=MappingStatus.MAPPED,
            matches=[("Trees", "tfidf", 0.9, 1, "")],
        )

        with pytest.raises(CoverageError) as ambiguous:
            _report(database)
        assert ambiguous.value.code == "ambiguous_syllabus"

        report = _report(database, syllabus=first)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        assert arrays.status is CoverageStatus.COVERED
        assert all(item.label != "Trees" for item in report.concepts)
        assert report.summary.total_questions == 2
        assert report.data_quality.unscoped_questions_excluded == 1
    finally:
        database.close()


def test_identical_concept_names_in_two_courses_stay_isolated() -> None:
    database = memory_database()
    try:
        _add_course(database, COURSE)
        _add_course(database, COURSE_B)
        first = _add_syllabus(database, COURSE, units=[("Unit 1", "A", ["Arrays"])])
        second = _add_syllabus(database, COURSE_B, units=[("Unit 1", "A", ["Arrays"])])
        questions = _add_paper(
            database,
            COURSE,
            "doc_first_course",
            title="First course paper",
            session="2024-05",
            questions=[
                ("q1", "Explain arrays.", 5.0),
                ("q2", "Describe arrays again.", 5.0),
            ],
        )
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                first,
                question,
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf", 0.8, 1, "")],
            )
        first_report = _report(database, COURSE, syllabus=first)
        second_report = _report(database, COURSE_B, syllabus=second)
        first_arrays = first_report.concepts[0]
        second_arrays = second_report.concepts[0]
        assert first_arrays.status is CoverageStatus.COVERED
        assert second_arrays.status is CoverageStatus.INSUFFICIENT_DATA
        assert first_arrays.concept_id != second_arrays.concept_id
        assert second_report.summary.total_questions == 0
    finally:
        database.close()


def test_invalid_unit_and_filter_arguments_fail_clearly() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        with pytest.raises(CoverageError) as unit_error:
            _report(
                database,
                syllabus=syllabus,
                filters=CoverageFilters(unit_ids=["not-a-unit"]),
            )
        assert unit_error.value.code == "invalid_coverage_filter"
        with pytest.raises(CoverageError) as source_error:
            _report(
                database,
                syllabus=syllabus,
                filters=CoverageFilters(source_ids=["missing-source"]),
            )
        assert source_error.value.code == "invalid_coverage_filter"
        with pytest.raises(CoverageError) as period_error:
            _report(
                database,
                syllabus=syllabus,
                filters=CoverageFilters(exam_periods=["1999-01"]),
            )
        assert period_error.value.code == "invalid_coverage_filter"
        assert period_error.value.detail["unknown_exam_periods"] == ["1999-01"]
    finally:
        database.close()


def test_unit_filter_restricts_concepts_but_keeps_the_question_denominator() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        for question_id in ("q1", "q2", "q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )
        unit = syllabus.units[1]
        report = _report(
            database,
            syllabus=syllabus,
            filters=CoverageFilters(unit_ids=[unit.unit_id]),
        )
        assert {item.unit_id for item in report.units} == {unit.unit_id}
        assert {item.label for item in report.concepts} == {"Trees", "Graphs"}
        assert report.summary.total_questions == 5
        assert report.syllabus.unit_count == 1
        assert report.syllabus.concept_count == 2
        assert any(
            "keeps the whole selected question corpus" in item for item in report.limitations
        )
    finally:
        database.close()


def test_document_source_exam_period_and_question_type_filters_are_reported() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_filter_one",
            title="First paper",
            session="2024-05",
            questions=[("q1", "Explain arrays.", 5.0)],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_filter_two",
            title="Second paper",
            session="2024-11",
            questions=[("q2", "Explain arrays again.", 5.0)],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            first["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.8, 1, "")],
            primary_type=QuestionType.EXPLANATION,
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            second["q2"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.8, 1, "")],
            primary_type=QuestionType.DESIGN,
        )
        report = _report(
            database,
            syllabus=syllabus,
            filters=CoverageFilters(
                document_ids=["doc_filter_one"],
                source_ids=["phase7-fixture"],
                exam_periods=["2024-05"],
                question_types=[QuestionType.EXPLANATION],
            ),
        )
        assert report.summary.total_questions == 1
        assert report.summary.mapped_questions == 1
        assert report.filters.document_ids == ["doc_filter_one"]
        assert report.filters.exam_periods == ["2024-05"]
        assert report.parameters["filter_active"] is True
    finally:
        database.close()


def test_max_questions_truncates_deterministically_and_marks_report_partial() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf", 0.8, 1, "")],
            )
        report = _report(
            database,
            syllabus=syllabus,
            settings=replace(CoverageSettings(), max_questions=2),
        )
        assert report.data_quality.truncated is True
        assert report.summary.total_questions == 2
        assert report.status is CoverageReportStatus.PARTIAL
        assert any(warning.code == "coverage_truncated" for warning in report.warnings)
    finally:
        database.close()


def test_concept_and_unit_lookup_and_unknown_ids() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            status=MappingStatus.MAPPED,
            matches=[("Arrays", "tfidf", 0.8, 1, "")],
        )
        for question_id in ("q2", "q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )
        request = CoverageRequest(course_id=COURSE, syllabus_id=syllabus.syllabus_id)
        service = _service(database)
        arrays = next(item for item in service.analyze(request).concepts if item.label == "Arrays")
        by_label = service.concept(request, "Arrays")
        by_id = service.concept(request, arrays.concept_id)
        assert by_label.concept_id == arrays.concept_id
        assert by_id.label == "Arrays"
        unit = service.unit(request, syllabus.units[0].unit_id)
        assert unit.unit_label == "Unit 1"
        with pytest.raises(KeyError):
            service.concept(request, "missing-concept")
        with pytest.raises(KeyError):
            service.unit(request, "missing-unit")
    finally:
        database.close()


def test_graph_candidate_edges_are_not_accepted_and_unscoped_graph_edges_respect_versioning() -> (
    None
):
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        for question in questions.values():
            _add_analysis(
                database,
                COURSE,
                syllabus,
                question,
                status=MappingStatus.UNMAPPED,
            )
        _add_graph_assessment(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            "Arrays",
            status=GraphEdgeStatus.CANDIDATE,
        )
        _add_graph_assessment(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            "Trees",
            status=GraphEdgeStatus.INFERRED,
        )
        report = _report(database, syllabus=syllabus)
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        trees = next(item for item in report.concepts if item.label == "Trees")
        assert arrays.status is CoverageStatus.UNMAPPED
        assert trees.status is CoverageStatus.UNMAPPED
        assert report.summary.mapped_questions == 0
        assert report.data_quality.graph_assessment_edges == 0
    finally:
        database.close()


def test_graph_mappings_can_be_disabled_without_a_false_unavailable_warning() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _five_questions(database)
        _add_graph_assessment(database, COURSE, syllabus, questions["q1"], "Arrays")

        report = _report(
            database,
            syllabus=syllabus,
            settings=replace(CoverageSettings(), include_graph_explicit_mappings=False),
        )
        arrays = next(item for item in report.concepts if item.label == "Arrays")
        codes = {warning.code for warning in report.warnings}
        assert arrays.distinct_question_count == 0
        assert report.summary.mapped_questions == 0
        assert report.data_quality.graph_assessment_edges == 0
        assert "coverage_graph_disabled" in codes
        assert "coverage_graph_unavailable" not in codes
        assert any("graph mappings were disabled" in item for item in report.limitations)
    finally:
        database.close()
