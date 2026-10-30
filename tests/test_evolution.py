"""Phase 9 tests: temporal model, change detection, assessment reuse, isolation."""

from __future__ import annotations

import hashlib
from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from exam_engine.config import EvolutionSettings
from exam_engine.evolution import ConceptEvolutionService, EvolutionError
from exam_engine.evolution.dates import resolve_document_date
from exam_engine.models.document import DocumentRecord, PageRecord
from exam_engine.models.enums import (
    BloomLevel,
    ConceptChangeCategory,
    DifficultyLabel,
    DocumentKind,
    EvolutionDateType,
    EvolutionStatus,
    ExtractionStatus,
    GraphEdgeStatus,
    GraphNodeType,
    GraphRelationship,
    MappingStatus,
    QuestionType,
    SourceFormat,
)
from exam_engine.models.evolution import EvolutionFilters, EvolutionRequest
from exam_engine.models.graph import GraphEdge, GraphNode, make_edge_id, make_node_id
from exam_engine.models.question import QuestionRecord
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)
from exam_engine.models.understanding import (
    BloomLabel,
    DifficultyAssessment,
    QuestionAnalysis,
    TypeLabel,
    UnitMatch,
    make_analysis_id,
    make_bloom_id,
    make_difficulty_id,
    make_type_id,
    make_unit_match_id,
)
from exam_engine.nlp.keywords import normalize_term
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
from exam_engine.storage.schema import DocumentRow, GraphEdgeRow, GraphNodeRow, QuestionRow
from tests.support import make_course, make_source

COURSE = "phase9-course-v1"
COURSE_B = "phase9-course-b-v1"


# --- fixtures ---------------------------------------------------------------


def _add_course(
    database: Database,
    course_id: str = COURSE,
    *,
    institution_code: str = "phase9-inst",
) -> None:
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id="phase9-fixture"))
        CourseRepository(session).upsert(
            make_course(
                course_id=course_id,
                course_code="CS901",
                title="Phase 9 synthetic concept evolution fixture",
                institution_code=institution_code,
                academic_year="2024-25",
            )
        )


def _add_document(
    database: Database,
    course_id: str,
    document_id: str,
    *,
    kind: DocumentKind,
    title: str,
    publication_date: date | None = None,
    metadata: dict[str, object] | None = None,
    extraction: ExtractionStatus = ExtractionStatus.OK,
    ingested_at: datetime | None = None,
    pages: list[str] | None = None,
) -> None:
    page_texts = pages or []
    document = DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id="phase9-fixture",
        kind=kind,
        format=SourceFormat.TXT,
        title=title,
        original_filename=f"{document_id}.txt",
        content_sha256=hashlib.sha256(f"{course_id}:{document_id}".encode()).hexdigest(),
        byte_size=200,
        page_count=len(page_texts),
        char_count=sum(len(text) for text in page_texts),
        extraction_status=extraction,
        pipeline_version="phase9-test",
        publication_date=publication_date,
        metadata=dict(metadata or {}),
        **({"ingested_at": ingested_at} if ingested_at else {}),
    )
    with database.transaction() as session:
        DocumentRepository(session).upsert(document)
        if page_texts:
            DocumentRepository(session).replace_pages(
                document_id,
                [
                    PageRecord(page_number=index, text=text)
                    for index, text in enumerate(page_texts, start=1)
                ],
            )


def _units(syllabus_id: str, units: list[tuple[str, str, list[str]]]) -> list[SyllabusUnitRecord]:
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
    course_id: str,
    document_id: str,
    *,
    title: str,
    units: list[tuple[str, str, list[str]]],
    effective: date | None = None,
    extraction: ExtractionStatus = ExtractionStatus.OK,
    confidence: float = 0.9,
) -> SyllabusRecord:
    metadata: dict[str, object] = {}
    if effective is not None:
        metadata["effective_date"] = effective.isoformat()
    _add_document(
        database,
        course_id,
        document_id,
        kind=DocumentKind.SYLLABUS,
        title=title,
        metadata=metadata,
        extraction=extraction,
        pages=["SYNTHETIC syllabus page"],
    )
    syllabus_id = make_syllabus_id(document_id)
    syllabus = SyllabusRecord(
        syllabus_id=syllabus_id,
        course_id=course_id,
        document_id=document_id,
        source_id="phase9-fixture",
        title=title,
        units=_units(syllabus_id, units),
        confidence=confidence,
    )
    with database.transaction() as session:
        SyllabusRepository(session).replace(syllabus)
    return syllabus


def _add_paper(
    database: Database,
    course_id: str,
    document_id: str,
    *,
    title: str,
    questions: list[tuple[str, str, float | None]],
    session: str | None = None,
    exam_date: date | None = None,
    exam_type: str = "final",
    kind: DocumentKind = DocumentKind.QUESTION_PAPER,
    extraction: ExtractionStatus = ExtractionStatus.OK,
) -> dict[str, QuestionRecord]:
    metadata: dict[str, object] = {}
    if session is not None:
        metadata["exam_session"] = session
        metadata["exam_type"] = exam_type
    if exam_date is not None:
        metadata["exam_date"] = exam_date.isoformat()
    _add_document(
        database,
        course_id,
        document_id,
        kind=kind,
        title=title,
        metadata=metadata,
        extraction=extraction,
    )
    questions = {
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
        QuestionRepository(session).replace_for_document(document_id, list(questions.values()))
    return questions


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
    topic: str = "Arrays",
    primary_type: QuestionType = QuestionType.EXPLANATION,
    bloom: BloomLevel = BloomLevel.UNDERSTAND,
    status: MappingStatus = MappingStatus.MAPPED,
    score: float = 0.8,
) -> None:
    matches: list[UnitMatch] = []
    if topic and status is not MappingStatus.UNMAPPED:
        unit = _unit_for_topic(syllabus, topic)
        matches.append(
            UnitMatch(
                match_id=make_unit_match_id(
                    course_id, question.question_id, unit.unit_id, topic, "tfidf"
                ),
                question_id=question.question_id,
                course_id=course_id,
                unit_id=unit.unit_id,
                syllabus_id=syllabus.syllabus_id,
                unit_label=unit.unit_label,
                unit_title=unit.title,
                topic=topic,
                score=score,
                rank=1,
                method="tfidf",
                matched_terms=[normalize_term(topic)],
                evidence=[f"fixture mapping to {topic!r}"],
            )
        )
    analysis = QuestionAnalysis(
        analysis_id=make_analysis_id(course_id, question.question_id),
        question_id=question.question_id,
        course_id=course_id,
        document_id=question.document_id,
        text_sha256=hashlib.sha256(question.text.encode()).hexdigest(),
        word_count=len(question.text.split()),
        sentence_count=1,
        mapping_status=status,
        mapping_confidence=score if matches else 0.0,
        top_unit_id=matches[0].unit_id if matches else None,
        primary_type=primary_type,
        primary_type_confidence=0.8,
        difficulty_label=DifficultyLabel.MODERATE,
        difficulty_score=0.5,
        difficulty_confidence=0.6,
        bloom_primary=bloom,
        bloom_confidence=0.8,
        structure_confidence=0.8,
    )
    types = [
        TypeLabel(
            type_id=make_type_id(course_id, question.question_id, primary_type, "fixture"),
            question_id=question.question_id,
            course_id=course_id,
            label=primary_type,
            score=0.8,
            rank=1,
            confidence=0.8,
            method="fixture",
        )
    ]
    bloom_labels = [
        BloomLabel(
            bloom_id=make_bloom_id(course_id, question.question_id, bloom, "fixture"),
            question_id=question.question_id,
            course_id=course_id,
            level=bloom,
            score=0.8,
            rank=1,
            confidence=0.8,
            method="fixture",
        )
    ]
    difficulty = DifficultyAssessment(
        difficulty_id=make_difficulty_id(course_id, question.question_id, "fixture"),
        question_id=question.question_id,
        course_id=course_id,
        label=DifficultyLabel.MODERATE,
        score=0.5,
        confidence=0.6,
        method="fixture",
    )
    with database.transaction() as session:
        QuestionUnderstandingRepository(session).replace_for_question(
            analysis=analysis,
            matches=matches,
            types=types,
            bloom=bloom_labels,
            difficulty=difficulty,
        )


def _add_graph(
    database: Database,
    course_id: str,
    concepts: list[tuple[str, list[str], bool]],
    *,
    edges: list[tuple[str, str, str, GraphRelationship]] | None = None,
) -> None:
    nodes: list[GraphNode] = []
    ids: dict[str, str] = {}
    for label, aliases, verified in concepts:
        normalized = normalize_term(label)
        node_id = make_node_id(GraphNodeType.CONCEPT, f"{course_id}|concept:{normalized}")
        ids[label] = node_id
        nodes.append(
            GraphNode(
                node_id=node_id,
                node_type=GraphNodeType.CONCEPT,
                node_key=f"{course_id}|concept:{normalized}",
                label=label,
                normalized_label=normalized,
                course_id=course_id,
                entity_id=normalized,
                metadata={
                    "aliases": sorted({label, *aliases}),
                    "verified": verified,
                    "confidence": 0.9 if verified else 0.5,
                    "origins": ["syllabus" if verified else "inferred"],
                },
            )
        )
    graph_edges: list[GraphEdge] = []
    for left, right, document_id, relationship in edges or []:
        graph_edges.append(
            GraphEdge(
                edge_id=make_edge_id(ids[left], relationship, ids[right]),
                course_id=course_id,
                source_node_id=ids[left],
                target_node_id=ids[right],
                relationship=relationship,
                status=GraphEdgeStatus.INFERRED,
                method="fixture",
                confidence=0.7,
                source_document_id=document_id,
            )
        )
    with database.transaction() as session:
        GraphRepository(session).replace_course(course_id, nodes, graph_edges)


def _profile(
    database: Database,
    course_id: str = COURSE,
    *,
    filters: EvolutionFilters | None = None,
    settings: EvolutionSettings | None = None,
    include_assessment: bool = True,
    include_exam_dna: bool = True,
    include_relationships: bool = True,
):
    return ConceptEvolutionService(database, settings or EvolutionSettings()).analyze(
        EvolutionRequest(
            course_id=course_id,
            filters=filters or EvolutionFilters(),
            include_assessment=include_assessment,
            include_exam_dna=include_exam_dna,
            include_relationships=include_relationships,
        )
    )


def _concept_report(database: Database, concept: str):
    return ConceptEvolutionService(database).timeline(EvolutionRequest(course_id=COURSE), concept)


# --- temporal model ---------------------------------------------------------


def test_date_types_order_and_equal_date_ties_are_deterministic() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2019_b",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes 2019 B",
            publication_date=date(2019, 1, 1),
            pages=["Arrays support constant-time indexing in a contiguous block."],
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2019_a",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes 2019 A",
            publication_date=date(2019, 1, 1),
            pages=["Arrays store elements in contiguous memory."],
        )
        _add_paper(
            database,
            COURSE,
            "doc_paper_2021",
            title="SYNTHETIC paper 2021",
            session="2021-05",
            exam_date=date(2021, 5, 20),
            questions=[("p1", "Explain array indexing.", 5.0)],
        )
        _add_graph(database, COURSE, [("Arrays", ["Array"], True)])

        report = _profile(database)
        timeline = next(item for item in report.timelines if item.canonical_label == "Arrays")

        assert [item.date for item in timeline.observations] == [
            date(2019, 1, 1),
            date(2019, 1, 1),
            date(2020, 1, 1),
            date(2021, 5, 20),
        ]
        assert [item.date_type for item in timeline.observations] == [
            EvolutionDateType.PUBLICATION,
            EvolutionDateType.PUBLICATION,
            EvolutionDateType.SYLLABUS_EFFECTIVE,
            EvolutionDateType.EXAMINATION,
        ]
        assert [item.document_id for item in timeline.observations[:2]] == [
            "doc_notes_2019_a",
            "doc_notes_2019_b",
        ]
        assert timeline.first_observed == date(2019, 1, 1)
        assert timeline.last_observed == date(2021, 5, 20)
    finally:
        database.close()


def test_unknown_conflicting_and_invalid_dates_are_explicit() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_conflict",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes with conflicting dates",
            publication_date=date(2019, 1, 1),
            metadata={"publication_date": "2021-06-01"},
            pages=["Arrays are covered here."],
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_undated",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC undated notes",
            pages=["Arrays again, without a date."],
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_invalid",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes with an invalid date",
            metadata={"effective_date": "Spring term"},
            pages=["Arrays in an undated note."],
        )
        _add_graph(database, COURSE, [("Arrays", [], True)])

        report = _profile(database)
        by_id = {item.document_id: item for item in report.documents}

        conflict = by_id["doc_notes_conflict"]
        assert conflict.date == date(2019, 1, 1)
        assert conflict.date_confident is False
        assert "evolution_conflicting_dates" in conflict.warnings
        assert {(item.source, item.date) for item in conflict.date_candidates} == {
            ("document.publication_date", date(2019, 1, 1)),
            ("metadata.publication_date", date(2021, 6, 1)),
        }
        assert by_id["doc_notes_undated"].date is None
        assert by_id["doc_notes_undated"].date_type is EvolutionDateType.UNKNOWN
        assert by_id["doc_notes_invalid"].date is None
        assert "evolution_invalid_date" in by_id["doc_notes_invalid"].warnings
        codes = {warning.code for warning in report.warnings}
        assert {"evolution_conflicting_dates", "evolution_invalid_date"} <= codes
        assert report.status is EvolutionStatus.PARTIAL
        assert report.data_quality.conflicting_dates == 1
        assert report.data_quality.invalid_dates == 1
        # The invalid-date document has no usable date either, so it counts in both
        # buckets; the two explicit test documents are the undated pair.
        assert report.data_quality.undated_documents == 2
    finally:
        database.close()


def test_ingestion_time_is_never_used_as_a_source_date() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_document(
            database,
            COURSE,
            "doc_notes_old",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC old notes ingested late",
            publication_date=date(2015, 3, 1),
            ingested_at=datetime(2035, 1, 1, 12, 0, 0),
            pages=["Arrays are old but ingested recently."],
        )
        _add_graph(database, COURSE, [("Arrays", [], True)])

        report = _profile(database)
        document = report.documents[0]
        assert document.date == date(2015, 3, 1)
        assert document.date_source == "document.publication_date"
        assert document.date_type is EvolutionDateType.PUBLICATION
        timeline = report.timelines[0]
        assert timeline.observations[0].date == date(2015, 3, 1)
    finally:
        database.close()


def test_edition_date_precedes_publication_for_textbook_material() -> None:
    document = DocumentRecord(
        document_id="doc_notes_edition",
        course_id=COURSE,
        source_id="phase9-fixture",
        kind=DocumentKind.NOTES,
        format=SourceFormat.TXT,
        title="SYNTHETIC textbook notes with an edition date",
        original_filename="doc_notes_edition.txt",
        content_sha256=hashlib.sha256(b"edition").hexdigest(),
        byte_size=10,
        publication_date=date(2020, 1, 1),
        metadata={"edition_date": "2018-03-01", "publication_date": "2020-01-01"},
    )
    resolved = resolve_document_date(document)

    assert resolved.value == date(2018, 3, 1)
    assert resolved.date_type is EvolutionDateType.EDITION
    assert resolved.source == "metadata.edition_date"
    assert resolved.confident is True
    # The publication date was not selected but is preserved with its field.
    assert [(item.source, item.value) for item in resolved.candidates] == [
        ("metadata.edition_date", date(2018, 3, 1)),
        ("document.publication_date", date(2020, 1, 1)),
        ("metadata.publication_date", date(2020, 1, 1)),
    ]


def test_conflicting_effective_dates_are_reported_without_choosing_arbitrarily() -> None:
    document = DocumentRecord(
        document_id="doc_syllabus_conflict",
        course_id=COURSE,
        source_id="phase9-fixture",
        kind=DocumentKind.SYLLABUS,
        format=SourceFormat.TXT,
        title="SYNTHETIC syllabus with conflicting effective dates",
        original_filename="doc_syllabus_conflict.txt",
        content_sha256=hashlib.sha256(b"conflict").hexdigest(),
        byte_size=10,
        metadata={"effective_date": "2020-01-01", "effective_from": "2021-01-01"},
    )
    resolved = resolve_document_date(document)

    assert resolved.value == date(2020, 1, 1)
    assert resolved.date_type is EvolutionDateType.SYLLABUS_EFFECTIVE
    assert resolved.confident is False
    assert resolved.conflicts == (
        "metadata.effective_date=2020-01-01",
        "metadata.effective_from=2021-01-01",
    )
    assert [(item.source, item.value) for item in resolved.candidates] == [
        ("metadata.effective_date", date(2020, 1, 1)),
        ("metadata.effective_from", date(2021, 1, 1)),
    ]


def test_invalid_date_range_is_rejected() -> None:
    database = memory_database()
    try:
        _add_course(database)
        with pytest.raises(ValueError):
            EvolutionFilters(date_from=date(2022, 1, 1), date_to=date(2021, 1, 1))
    finally:
        database.close()


# --- terminology and context ------------------------------------------------


def test_stable_terminology_produces_no_terminology_change() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2021",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes 2021",
            publication_date=date(2021, 1, 1),
            pages=["Arrays store elements in contiguous memory."],
        )
        _add_graph(database, COURSE, [("Arrays", [], True)])

        report = _profile(database)
        categories = {change.category for change in report.changes}
        assert ConceptChangeCategory.TERMINOLOGY_CHANGED not in categories
        assert ConceptChangeCategory.ALIAS_INTRODUCED not in categories
    finally:
        database.close()


def test_verified_alias_introduction_is_low_review() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )
        first = _add_paper(
            database,
            COURSE,
            "doc_paper_2021",
            title="SYNTHETIC paper 2021",
            session="2021-05",
            exam_date=date(2021, 5, 20),
            questions=[("q1", "Explain arrays and their indexing.", 5.0)],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_paper_2022",
            title="SYNTHETIC paper 2022",
            session="2022-05",
            exam_date=date(2022, 5, 20),
            questions=[
                ("q2", "Explain arrays and their indexing.", 5.0),
                ("q3", "Describe an array in memory.", 5.0),
            ],
        )
        _add_analysis(database, COURSE, syllabus, first["q1"], topic="Arrays")
        _add_analysis(database, COURSE, syllabus, second["q2"], topic="Arrays")
        _add_analysis(database, COURSE, syllabus, second["q3"], topic="Arrays")
        _add_graph(database, COURSE, [("Arrays", ["Array"], True)])

        report = _profile(database)
        aliases = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.ALIAS_INTRODUCED
        ]
        assert aliases, report.changes
        change = aliases[0]
        assert change.labels == ["Arrays"]
        assert change.review_required is False
        assert change.confidence >= 0.75
        assert change.earlier is not None and change.later is not None
        assert change.temporal_order_known is True
    finally:
        database.close()


def test_likely_alias_requires_review_and_similar_concepts_stay_distinct() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays", "Array lists"])],
            effective=date(2020, 1, 1),
        )
        first = _add_paper(
            database,
            COURSE,
            "doc_paper_2021",
            title="SYNTHETIC paper 2021",
            session="2021-05",
            exam_date=date(2021, 5, 20),
            questions=[("q1", "Explain arrays and their indexing.", 5.0)],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_paper_2022",
            title="SYNTHETIC paper 2022",
            session="2022-05",
            exam_date=date(2022, 5, 20),
            questions=[
                ("q2", "Explain arrays and their indexing.", 5.0),
                ("q3", "Describe an array in memory.", 5.0),
            ],
        )
        _add_analysis(database, COURSE, syllabus, first["q1"], topic="Arrays")
        _add_analysis(database, COURSE, syllabus, second["q2"], topic="Arrays")
        _add_analysis(database, COURSE, syllabus, second["q3"], topic="Arrays")
        _add_graph(
            database,
            COURSE,
            [
                ("Arrays", ["Array"], False),
                ("Array lists", ["Array list"], True),
            ],
        )

        report = _profile(database)
        labels = {timeline.canonical_label for timeline in report.timelines}
        assert {"Arrays", "Array lists"} <= labels
        aliases = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.ALIAS_INTRODUCED
        ]
        assert aliases
        assert all(change.review_required for change in aliases)
        assert all(change.confidence <= 0.6 for change in aliases)
        assert not [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.POSSIBLE_CONCEPT_MERGE
        ]
    finally:
        database.close()


def test_context_change_uses_excerpts_and_flags_review() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_document(
            database,
            COURSE,
            "doc_notes_2019",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes 2019",
            publication_date=date(2019, 1, 1),
            pages=["Arrays provide fast index access to contiguous memory locations."],
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2021",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes 2021",
            publication_date=date(2021, 1, 1),
            pages=["Arrays also model the schedule of passenger trains at a station."],
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2022",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes without extracted text",
            publication_date=date(2022, 1, 1),
        )
        _add_graph(database, COURSE, [("Arrays", [], True)])

        report = _profile(database)
        changes = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.CONTEXT_CHANGED
        ]
        assert changes
        change = changes[0]
        assert change.review_required is True
        assert change.evidence_quality.value == "low"
        assert "evolution_context_similarity" in {item.code for item in change.warnings}
        assert change.earlier is not None and change.earlier.excerpt
        assert change.later is not None and change.later.excerpt
        assert any("lexical" in limitation.lower() for limitation in change.limitations)
        # A document with no extracted text yields no observation and cannot be
        # presented as either side of a context change.
        by_id = {item.document_id: item for item in report.documents}
        assert by_id["doc_notes_2022"].observation_count == 0
        assert all(
            item.later is None or item.later.document_id != "doc_notes_2022"
            for item in report.changes
        )
    finally:
        database.close()


# --- syllabus evolution ------------------------------------------------------


def test_syllabus_introduced_removed_moved_and_description_changed() -> None:
    database = memory_database()
    try:
        _add_course(database)
        first = _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[
                ("Unit 1", "Foundations", ["Arrays", "Graphs"]),
                ("Unit 2", "Structures", ["Trees"]),
            ],
            effective=date(2020, 6, 1),
        )
        second = _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2022",
            title="SYNTHETIC syllabus 2022",
            units=[
                ("Unit 1", "Foundations and trees", ["Arrays", "Trees"]),
                ("Unit 3", "Advanced", ["Heaps"]),
            ],
            effective=date(2022, 6, 1),
        )
        _add_graph(
            database,
            COURSE,
            [
                ("Arrays", [], True),
                ("Graphs", [], True),
                ("Trees", [], True),
                ("Heaps", [], True),
            ],
        )

        report = _profile(database)
        assert len(report.syllabus_comparisons) == 1
        comparison = report.syllabus_comparisons[0]
        assert comparison.order_known is True
        assert comparison.earlier.syllabus_id == first.syllabus_id
        assert comparison.later.syllabus_id == second.syllabus_id
        assert len(comparison.introduced) == 1
        assert len(comparison.no_longer_observed) == 1
        assert [item.concept_id for item in comparison.moved_units]
        assert comparison.description_changes

        categories = {change.category for change in report.changes}
        assert ConceptChangeCategory.CONCEPT_INTRODUCED in categories
        assert ConceptChangeCategory.CONCEPT_NO_LONGER_OBSERVED in categories
        assert ConceptChangeCategory.SYLLABUS_UNIT_CHANGED in categories
        assert ConceptChangeCategory.DESCRIPTION_CHANGED in categories

        introduced = next(
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.CONCEPT_INTRODUCED
        )
        assert introduced.labels == ["Heaps"]
        assert introduced.temporal_order_known is True
        assert introduced.earlier is not None and introduced.later is not None
        assert introduced.review_required is True
    finally:
        database.close()


def test_unknown_version_order_and_incomplete_extraction_never_claim_removal() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_a",
            title="SYNTHETIC undated syllabus A",
            units=[("Unit 1", "Foundations", ["Arrays", "Graphs"])],
        )
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_b",
            title="SYNTHETIC undated syllabus B",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            extraction=ExtractionStatus.PARTIAL,
        )
        _add_graph(
            database,
            COURSE,
            [("Arrays", [], True), ("Graphs", [], True)],
        )

        report = _profile(database)
        comparison = report.syllabus_comparisons[0]
        assert comparison.order_known is False
        assert comparison.no_longer_observed
        categories = {change.category for change in report.changes}
        assert ConceptChangeCategory.CONCEPT_INTRODUCED not in categories
        assert ConceptChangeCategory.CONCEPT_NO_LONGER_OBSERVED not in categories
        insufficient = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.INSUFFICIENT_EVIDENCE
        ]
        assert insufficient
        assert insufficient[0].temporal_order_known is False
        assert "evolution_unknown_version_order" in {warning.code for warning in report.warnings}
        assert "evolution_incomplete_extraction" in {warning.code for warning in report.warnings}
    finally:
        database.close()


# --- merge and split candidates --------------------------------------------


def test_merge_and_split_candidates_are_review_only() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Neural Network"])],
            effective=date(2020, 1, 1),
        )
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2022",
            title="SYNTHETIC syllabus 2022",
            units=[("Unit 1", "Foundations", ["Neural Network", "Neural Networks"])],
            effective=date(2022, 1, 1),
        )
        _add_graph(
            database,
            COURSE,
            [
                ("Neural Network", [], True),
                ("Neural Networks", [], True),
            ],
        )

        report = _profile(database)
        splits = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.POSSIBLE_CONCEPT_SPLIT
        ]
        assert splits
        assert splits[0].review_required is True
        assert len(splits[0].concept_ids) >= 2
        assert "review candidate" in splits[0].explanation
    finally:
        database.close()


def test_possible_concept_merge_stays_review_only() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Neural Network", "Neural Networks"])],
            effective=date(2020, 1, 1),
        )
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2022",
            title="SYNTHETIC syllabus 2022",
            units=[("Unit 1", "Foundations", ["Neural Networks"])],
            effective=date(2022, 1, 1),
        )
        _add_graph(
            database,
            COURSE,
            [
                ("Neural Network", [], True),
                ("Neural Networks", [], True),
            ],
        )

        report = _profile(database)
        merges = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.POSSIBLE_CONCEPT_MERGE
        ]
        assert merges
        merge = merges[0]
        assert merge.review_required is True
        assert len(merge.concept_ids) == 2
        assert merge.confidence <= 0.6
        assert merge.evidence_quality.value == "low"
        assert "nothing is merged automatically" in merge.explanation
        # The comparison itself still records the absence rather than editing concepts.
        comparison = report.syllabus_comparisons[0]
        assert comparison.order_known is True
        assert comparison.no_longer_observed
    finally:
        database.close()


# --- assessment evolution ---------------------------------------------------


def _assessment_fixture(database: Database) -> None:
    syllabus = _add_syllabus(
        database,
        COURSE,
        "doc_syllabus_2020",
        title="SYNTHETIC syllabus 2020",
        units=[("Unit 1", "Foundations", ["Arrays"])],
        effective=date(2020, 1, 1),
    )
    sessions = [
        ("doc_paper_2020a", "2020-05", QuestionType.EXPLANATION, BloomLevel.UNDERSTAND, 5.0),
        ("doc_paper_2020b", "2020-11", QuestionType.EXPLANATION, BloomLevel.UNDERSTAND, 5.0),
        ("doc_paper_2021a", "2021-05", QuestionType.NUMERICAL, BloomLevel.APPLY, 10.0),
        ("doc_paper_2021b", "2021-11", QuestionType.NUMERICAL, BloomLevel.APPLY, 10.0),
    ]
    for index, (document_id, session, kind, bloom, marks) in enumerate(sessions):
        question_id = f"q{index}"
        questions = _add_paper(
            database,
            COURSE,
            document_id,
            title=f"SYNTHETIC paper {session}",
            session=session,
            questions=[(question_id, "Explain array indexing and memory layout.", marks)],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions[question_id],
            topic="Arrays",
            primary_type=kind,
            bloom=bloom,
        )
    _add_graph(database, COURSE, [("Arrays", [], True)])


def test_assessment_change_reuses_phase7_and_phase8_outputs() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _assessment_fixture(database)

        report = _profile(database)
        assessment = next(item for item in report.assessment if item.label == "Arrays")
        assert assessment.changed is True
        assert assessment.earlier.questions == 2
        assert assessment.later.questions == 2
        assert assessment.max_type_share_delta == 1.0
        assert assessment.max_bloom_share_delta == 1.0
        assert assessment.max_marks_share_delta == 1.0
        assert assessment.earlier.type_shares == {"explanation": 1.0}
        assert assessment.later.type_shares == {"numerical": 1.0}
        # Marks distribution and unit representation are window metrics, not triggers
        # on their own; the marks-value shift also adds a reason here.
        # Labels use the same compact ``:g`` form as the Phase 8 marks distribution.
        assert assessment.earlier.marks_counts == {"5": 2}
        assert assessment.later.marks_counts == {"10": 2}
        assert assessment.earlier.marks_shares == {"5": 1.0}
        assert assessment.later.marks_shares == {"10": 1.0}
        assert assessment.earlier.unit_counts == {"Unit 1": 2}
        assert any("marks-value" in reason for reason in assessment.reasons)
        # Phase 8 repetition groups are reused rather than recomputed: the identical
        # wording in each pair is an exact group, so each window holds two members.
        assert assessment.earlier.repetition_counts == {"exact": 2}
        assert assessment.later.repetition_counts == {"exact": 2}
        assert report.data_quality.coverage_report_ids
        assert assessment.coverage_report_ids == report.data_quality.coverage_report_ids
        assert report.data_quality.exam_dna_profile_ids
        assert assessment.exam_dna_profile_id in report.data_quality.exam_dna_profile_ids

        changes = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.ASSESSMENT_PATTERN_CHANGED
        ]
        assert changes
        assert changes[0].concept_ids == [assessment.concept_id]
        assert changes[0].review_required is True
        assert changes[0].earlier is not None and changes[0].later is not None
        assert "share" in " ".join(assessment.reasons)
    finally:
        database.close()


def test_question_count_and_marks_changes_are_reported_per_period_window() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )
        early = _add_paper(
            database,
            COURSE,
            "doc_paper_2020",
            title="SYNTHETIC three-question paper",
            session="2020-05",
            exam_date=date(2020, 5, 10),
            questions=[
                ("e1", "Explain array indexing.", 5.0),
                ("e2", "Explain array bounds.", 5.0),
                ("e3", "Explain array layout.", 5.0),
            ],
        )
        late = _add_paper(
            database,
            COURSE,
            "doc_paper_2022",
            title="SYNTHETIC one-question paper",
            session="2022-05",
            exam_date=date(2022, 5, 10),
            questions=[("l1", "Explain array indexing again.", 5.0)],
        )
        for question_id in ("e1", "e2", "e3"):
            _add_analysis(database, COURSE, syllabus, early[question_id], topic="Arrays")
        _add_analysis(database, COURSE, syllabus, late["l1"], topic="Arrays")
        _add_graph(database, COURSE, [("Arrays", [], True)])

        report = _profile(database)
        assessment = next(item for item in report.assessment if item.label == "Arrays")

        # Windows follow the examination dates, so the three 2020 questions stay in the
        # earlier window and the single 2022 question in the later one.
        assert assessment.earlier.questions == 3
        assert assessment.later.questions == 1
        assert assessment.question_delta == -2
        assert assessment.earlier.known_marks == 15.0
        assert assessment.later.known_marks == 5.0
        assert assessment.marks_delta == -10.0
        # Counts changed, but type and Bloom shares did not: no pattern claim is made.
        assert assessment.max_type_share_delta == 0.0
        assert assessment.max_bloom_share_delta == 0.0
        assert assessment.changed is False
        assert assessment.limitations
        assert not [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.ASSESSMENT_PATTERN_CHANGED
        ]
    finally:
        database.close()


def test_small_assessment_missing_marks_and_missing_dates_are_reported() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )
        first = _add_paper(
            database,
            COURSE,
            "doc_paper_2020",
            title="SYNTHETIC paper 2020",
            session="2020-05",
            questions=[("qa", "Explain array indexing.", None)],
        )
        _add_paper(
            database,
            COURSE,
            "doc_paper_undated",
            title="SYNTHETIC undated paper",
            questions=[("qb", "Explain array indexing again.", None)],
        )
        _add_analysis(database, COURSE, syllabus, first["qa"], topic="Arrays")
        undated_question = next(
            item for item in _questions_for_document(database, "doc_paper_undated")
        )
        _add_analysis(database, COURSE, syllabus, undated_question, topic="Arrays")
        _add_graph(database, COURSE, [("Arrays", [], True)])

        report = _profile(database)
        assessment = next(item for item in report.assessment if item.label == "Arrays")
        assert assessment.changed is False
        assert assessment.limitations
        assert assessment.earlier.known_marks == 0.0
        assert assessment.earlier.questions_with_marks == 0
        # A session such as 2020-05 names a period, not a day: the window is ordered
        # by that period key and never invents an examination date.
        assert assessment.earlier.date_from is None
        assert assessment.earlier.period_from == "2020-05"
        assert assessment.later.questions == 0
        assert "evolution_small_assessment" in {warning.code for warning in report.warnings}
        assert "evolution_missing_date" in {warning.code for warning in report.warnings}
    finally:
        database.close()


def _questions_for_document(database: Database, document_id: str):
    with database.session() as session:
        return QuestionRepository(session).list_by_document(document_id)


def test_incompatible_exam_formats_are_flagged() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_paper(
            database,
            COURSE,
            "doc_final",
            title="SYNTHETIC final",
            session="2020-05",
            exam_type="final",
            questions=[("qf", "Explain arrays.", 5.0)],
        )
        _add_paper(
            database,
            COURSE,
            "doc_midterm",
            title="SYNTHETIC midterm",
            session="2020-06",
            exam_type="midterm",
            questions=[("qm", "Explain arrays again.", 5.0)],
        )
        _add_graph(database, COURSE, [("Arrays", [], True)])

        report = _profile(database)
        assert "evolution_mixed_formats" in {warning.code for warning in report.warnings}
        assert any(
            "final" in warning.message and "midterm" in warning.message
            for warning in report.warnings
        )
    finally:
        database.close()


# --- isolation and reliability ---------------------------------------------


def test_course_and_institution_isolation() -> None:
    database = memory_database()
    try:
        _add_course(database, COURSE, institution_code="inst-a")
        _add_course(database, COURSE_B, institution_code="inst-b")
        for course_id, document_id in ((COURSE, "doc_a"), (COURSE_B, "doc_b")):
            _add_document(
                database,
                course_id,
                document_id,
                kind=DocumentKind.NOTES,
                title=f"SYNTHETIC notes {course_id}",
                publication_date=date(2020, 1, 1),
                pages=["Arrays belong to this course only."],
            )
            _add_graph(database, course_id, [("Arrays", [], True)])

        first = _profile(database, COURSE)
        second = _profile(database, COURSE_B)
        assert first.course.institution_code == "inst-a"
        assert second.course.institution_code == "inst-b"
        assert {item.document_id for item in first.documents} == {"doc_a"}
        assert {item.document_id for item in second.documents} == {"doc_b"}
        assert all(
            observation.document_id != "doc_b"
            for timeline in first.timelines
            for observation in timeline.observations
        )
    finally:
        database.close()


def test_reports_are_reproducible_and_read_only() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _assessment_fixture(database)

        with database.session() as session:
            counts_before = {
                "documents": session.scalar(select(func.count()).select_from(DocumentRow)),
                "questions": session.scalar(select(func.count()).select_from(QuestionRow)),
                "nodes": session.scalar(select(func.count()).select_from(GraphNodeRow)),
                "edges": session.scalar(select(func.count()).select_from(GraphEdgeRow)),
            }
        first = _profile(database)
        second = _profile(database)
        with database.session() as session:
            counts_after = {
                "documents": session.scalar(select(func.count()).select_from(DocumentRow)),
                "questions": session.scalar(select(func.count()).select_from(QuestionRow)),
                "nodes": session.scalar(select(func.count()).select_from(GraphNodeRow)),
                "edges": session.scalar(select(func.count()).select_from(GraphEdgeRow)),
            }

        assert first.report_id == second.report_id
        assert first.model_dump(exclude={"generated_at"}) == second.model_dump(
            exclude={"generated_at"}
        )
        assert counts_before == counts_after
    finally:
        database.close()


def test_partial_failure_without_graph_or_analysis_is_explicit() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )

        report = _profile(database)
        assert report.status is EvolutionStatus.PARTIAL
        assert "evolution_graph_unavailable" in {warning.code for warning in report.warnings}
        assert report.timelines
        assert report.timelines[0].canonical_label == "Arrays"
        assert report.data_quality.graph_available is False
    finally:
        database.close()


def test_filters_validate_and_narrow_scope() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays", "Trees"])],
            effective=date(2020, 1, 1),
        )
        _add_graph(
            database,
            COURSE,
            [("Arrays", [], True), ("Trees", [], True)],
        )

        narrowed = _profile(
            database,
            filters=EvolutionFilters(concept_ids=["Trees"]),
        )
        assert [item.canonical_label for item in narrowed.timelines] == ["Trees"]
        assert narrowed.scope.concept_ids == [narrowed.timelines[0].concept_id]

        with pytest.raises(EvolutionError) as unknown:
            _profile(database, filters=EvolutionFilters(concept_ids=["Nope"]))
        assert unknown.value.code == "invalid_evolution_filter"

        with pytest.raises(EvolutionError):
            _profile(database, filters=EvolutionFilters(document_ids=["doc_nope"]))

        with pytest.raises(EvolutionError):
            _profile(database, filters=EvolutionFilters(syllabus_ids=["syl_nope"]))

        with pytest.raises(EvolutionError):
            ConceptEvolutionService(database).analyze(EvolutionRequest(course_id="missing-course"))
    finally:
        database.close()


def test_relationship_changes_use_graph_edge_documents() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_document(
            database,
            COURSE,
            "doc_notes_2020",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes 2020",
            publication_date=date(2020, 1, 1),
            pages=["Arrays and linked lists are introduced separately."],
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2021",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC notes 2021",
            publication_date=date(2021, 1, 1),
            pages=["Arrays relate to linked lists through sequential storage."],
        )
        _add_graph(
            database,
            COURSE,
            [("Arrays", [], True), ("Linked lists", [], True)],
            edges=[
                (
                    "Arrays",
                    "Linked lists",
                    "doc_notes_2021",
                    GraphRelationship.RELATED_TO,
                )
            ],
        )

        report = _profile(database)
        relationships = [
            change
            for change in report.changes
            if change.category is ConceptChangeCategory.RELATIONSHIP_CHANGED
        ]
        assert relationships
        change = relationships[0]
        assert change.review_required is True
        assert change.later is not None
        assert change.later.document_id == "doc_notes_2021"
        assert "not proof" in " ".join(change.limitations).lower()
    finally:
        database.close()


def test_similarity_and_difficulty_tables_are_not_duplicated() -> None:
    """Phase 9 reuses Phase 3/7/8 records and writes no similarity rows."""
    database = memory_database()
    try:
        _add_course(database)
        _assessment_fixture(database)
        with database.session() as session:
            before = session.scalar(select(func.count()).select_from(QuestionRow))
        _profile(database)
        with database.session() as session:
            after = session.scalar(select(func.count()).select_from(QuestionRow))
        assert before == after
    finally:
        database.close()
