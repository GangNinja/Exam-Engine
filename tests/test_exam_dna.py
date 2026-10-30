"""Phase 8 tests: distributions, recurrence denominators, grouping and isolation."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from exam_engine.config import ExamDnaSettings
from exam_engine.exam_dna import ExamDnaError, ExamDnaService
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    DocumentKind,
    ExamDnaStatus,
    ExtractionStatus,
    MappingStatus,
    PaperExclusionReason,
    PatternCategory,
    QuestionGroupKind,
    QuestionType,
    SimilarityKind,
    SourceFormat,
)
from exam_engine.models.exam_dna import ExamDnaFilters, ExamDnaRequest
from exam_engine.models.question import QuestionRecord, Uncertainty
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
    SimilarityMatch,
    TypeLabel,
    UnitMatch,
    make_analysis_id,
    make_bloom_id,
    make_difficulty_id,
    make_similarity_id,
    make_type_id,
    make_unit_match_id,
)
from exam_engine.nlp.keywords import normalize_term
from exam_engine.storage.db import Database, memory_database
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from tests.support import make_course, make_source

COURSE = "phase8-course-v1"
COURSE_B = "phase8-course-b-v1"


def _add_course(
    database: Database,
    course_id: str = COURSE,
    *,
    institution_code: str = "phase8-inst",
    academic_year: str = "2024-25",
) -> None:
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id="phase8-fixture"))
        CourseRepository(session).upsert(
            make_course(
                course_id=course_id,
                course_code="CS801",
                title="Phase 8 synthetic Exam DNA fixture",
                institution_code=institution_code,
                academic_year=academic_year,
            )
        )


def _syllabus_units(
    syllabus_id: str, units: list[tuple[str, str, list[str]]]
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
        source_id="phase8-fixture",
        title=f"Synthetic syllabus {suffix}",
        units=_syllabus_units(syllabus_id, units),
        confidence=0.9,
    )
    document = DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id="phase8-fixture",
        kind=DocumentKind.SYLLABUS,
        format=SourceFormat.TXT,
        title=f"SYNTHETIC syllabus {suffix}",
        original_filename=f"syllabus_{suffix}.txt",
        content_sha256=hashlib.sha256(document_id.encode()).hexdigest(),
        byte_size=100,
        page_count=2,
        char_count=200,
        extraction_status=ExtractionStatus.OK,
        pipeline_version="phase8-test",
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
    exam_date: str | None = None,
    kind: DocumentKind = DocumentKind.QUESTION_PAPER,
    extraction_status: ExtractionStatus = ExtractionStatus.OK,
    text_fingerprint: str = "",
    sections: dict[str, str] | None = None,
) -> dict[str, QuestionRecord]:
    metadata: dict[str, object] = {}
    if session is not None:
        metadata["exam_session"] = session
        metadata["exam_type"] = exam_type
    elif exam_date is not None:
        metadata["exam_type"] = exam_type
    if exam_date is not None:
        metadata["exam_date"] = exam_date
    sections = sections or {}
    document = DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id="phase8-fixture",
        kind=kind,
        format=SourceFormat.TXT,
        title=title,
        original_filename=f"{document_id}.txt",
        content_sha256=hashlib.sha256(f"{course_id}:{document_id}".encode()).hexdigest(),
        text_fingerprint=text_fingerprint,
        byte_size=200,
        page_count=1,
        char_count=500,
        extraction_status=extraction_status,
        pipeline_version="phase8-test",
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
            section=sections.get(question_id),
            marks=marks,
            page_number=1,
            confidence=0.9,
            metadata=dict(metadata),
        )
        for index, (question_id, text, marks) in enumerate(questions, start=1)
    }
    with database.transaction() as session_:
        DocumentRepository(session_).upsert(document)
        QuestionRepository(session_).replace_for_document(document_id, list(records.values()))
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
    status: MappingStatus = MappingStatus.MAPPED,
    topic: str | None = "Arrays",
    method: str = "tfidf",
    score: float = 0.8,
    primary_type: QuestionType | None = QuestionType.EXPLANATION,
    type_confidence: float = 0.8,
    bloom: BloomLevel | None = BloomLevel.UNDERSTAND,
    bloom_confidence: float = 0.8,
    difficulty: DifficultyLabel = DifficultyLabel.MODERATE,
) -> None:
    matches: list[UnitMatch] = []
    if topic is not None and status is not MappingStatus.UNMAPPED:
        unit = _unit_for_topic(syllabus, topic)
        matches.append(
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
                rank=1,
                method=method,
                matched_terms=[normalize_term(topic)],
                evidence=[f"fixture mapping to {topic!r}"],
            )
        )
    uncertainties: list[Uncertainty] = []
    if status is MappingStatus.UNMAPPED:
        uncertainties = [
            Uncertainty(
                code="unmapped_question",
                message="no syllabus unit met the configured mapping threshold (0.18)",
            )
        ]
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
        primary_type_confidence=type_confidence if primary_type else 0.0,
        difficulty_label=difficulty,
        difficulty_score=0.5,
        difficulty_confidence=0.6,
        bloom_primary=bloom,
        bloom_confidence=bloom_confidence if bloom else 0.0,
        structure_confidence=0.8,
        uncertainties=uncertainties,
    )
    types = (
        [
            TypeLabel(
                type_id=make_type_id(course_id, question.question_id, primary_type, "fixture"),
                question_id=question.question_id,
                course_id=course_id,
                label=primary_type,
                score=type_confidence,
                rank=1,
                confidence=type_confidence,
                method="fixture",
            )
        ]
        if primary_type
        else []
    )
    bloom_labels = (
        [
            BloomLabel(
                bloom_id=make_bloom_id(course_id, question.question_id, bloom, "fixture"),
                question_id=question.question_id,
                course_id=course_id,
                level=bloom,
                score=bloom_confidence,
                rank=1,
                confidence=bloom_confidence,
                method="fixture",
            )
        ]
        if bloom
        else []
    )
    difficulty_record = DifficultyAssessment(
        difficulty_id=make_difficulty_id(course_id, question.question_id, "fixture"),
        question_id=question.question_id,
        course_id=course_id,
        label=difficulty,
        score=0.5,
        confidence=0.6,
        method="fixture",
    )
    with database.transaction() as session_:
        QuestionUnderstandingRepository(session_).replace_for_question(
            analysis=analysis,
            matches=matches,
            types=types,
            bloom=bloom_labels,
            difficulty=difficulty_record,
        )


def _set_similarities(
    database: Database,
    course_id: str,
    pairs: list[tuple[str, str, SimilarityKind, float]],
) -> None:
    matches = [
        SimilarityMatch(
            pair_id=make_similarity_id(course_id, left, right, kind),
            course_id=course_id,
            question_id=left,
            matched_question_id=right,
            kind=kind,
            score=score,
            lexical_score=score,
            semantic_score=score,
            method="fixture_pair",
            evidence=[f"fixture {kind.value} pair"],
        )
        for left, right, kind, score in pairs
    ]
    with database.transaction() as session_:
        QuestionUnderstandingRepository(session_).replace_similarities(course_id, matches)


def _profile(
    database: Database,
    course_id: str = COURSE,
    *,
    syllabus: SyllabusRecord | None = None,
    filters: ExamDnaFilters | None = None,
    settings: ExamDnaSettings | None = None,
):
    return ExamDnaService(database, settings or ExamDnaSettings()).analyze(
        ExamDnaRequest(
            course_id=course_id,
            syllabus_id=syllabus.syllabus_id if syllabus else None,
            filters=filters or ExamDnaFilters(),
        )
    )


def _three_papers(
    database: Database, syllabus: SyllabusRecord
) -> dict[str, dict[str, QuestionRecord]]:
    first = _add_paper(
        database,
        COURSE,
        "doc_paper_2024",
        title="SYNTHETIC 2024 paper",
        session="2024-05",
        exam_type="final",
        sections={"q1": "A", "q2": "A"},
        questions=[
            ("q1", "Explain array indexing and memory layout.", 5.0),
            ("q2", "Compare arrays and linked lists.", 5.0),
        ],
    )
    second = _add_paper(
        database,
        COURSE,
        "doc_paper_2025",
        title="SYNTHETIC 2025 paper",
        session="2025-05",
        exam_type="final",
        sections={"q3": "A", "q4": "B"},
        questions=[
            ("q3", "Explain tree traversal orders.", 10.0),
            ("q4", "Calculate the height of a binary tree.", 10.0),
        ],
    )
    third = _add_paper(
        database,
        COURSE,
        "doc_paper_2026",
        title="SYNTHETIC 2026 paper",
        session="2026-05",
        exam_type="final",
        sections={"q5": "A", "q6": "B"},
        questions=[
            ("q5", "Explain graph representations.", 5.0),
            ("q6", "Describe hashing collisions.", 5.0),
        ],
    )
    for question in (first["q1"], first["q2"]):
        _add_analysis(
            database,
            COURSE,
            syllabus,
            question,
            topic="Arrays",
            primary_type=QuestionType.EXPLANATION,
            bloom=BloomLevel.UNDERSTAND,
        )
    _add_analysis(
        database,
        COURSE,
        syllabus,
        second["q3"],
        topic="Trees",
        primary_type=QuestionType.EXPLANATION,
        bloom=BloomLevel.ANALYZE,
    )
    _add_analysis(
        database,
        COURSE,
        syllabus,
        second["q4"],
        topic="Trees",
        primary_type=QuestionType.NUMERICAL,
        bloom=BloomLevel.APPLY,
    )
    _add_analysis(
        database,
        COURSE,
        syllabus,
        third["q5"],
        topic="Graphs",
        primary_type=QuestionType.EXPLANATION,
        bloom=BloomLevel.UNDERSTAND,
    )
    _add_analysis(
        database,
        COURSE,
        syllabus,
        third["q6"],
        topic="Linked Lists",
        primary_type=QuestionType.DEFINITION,
        bloom=BloomLevel.REMEMBER,
    )
    return {"first": first, "second": second, "third": third}


def test_distributions_cover_types_marks_units_bloom_and_sections() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        _three_papers(database, syllabus)

        report = _profile(database, syllabus=syllabus)

        assert report.status is ExamDnaStatus.OK
        assert report.summary.eligible_papers == 3
        assert report.summary.questions == 6
        assert report.summary.total_known_marks == 40.0
        assert report.data_quality.missing_marks == 0

        types = {row.label: row for row in report.distributions.question_types}
        assert types["explanation"].question_count == 4
        assert types["explanation"].papers_observed == 3
        assert types["numerical"].question_count == 1
        assert types["numerical"].share_of_questions == pytest.approx(1 / 6, abs=1e-3)

        bloom = {row.label: row for row in report.distributions.bloom_levels}
        assert bloom["understand"].question_count == 3
        assert bloom["apply"].question_count == 1
        assert bloom["remember"].question_count == 1

        units = {row.label: row for row in report.distributions.units}
        assert units["Unit 1"].question_count == 3
        assert units["Unit 2"].question_count == 3
        assert units["Unit 1"].marks == pytest.approx(15.0)
        assert units["Unit 2"].marks == pytest.approx(25.0)
        assert units["Unit 2"].share_of_marks == pytest.approx(25 / 40)

        sections = {row.label: row for row in report.distributions.sections}
        assert sections["A"].question_count == 4
        assert sections["B"].question_count == 2

        marks = {row.label: row for row in report.distributions.marks_values}
        assert marks["5"].question_count == 4
        assert marks["10"].question_count == 2
        assert {row.label for row in report.distributions.marks_bands} == {
            "3-5 marks",
            "6-10 marks",
        }
    finally:
        database.close()


def test_missing_marks_and_sessions_are_counted_without_invention() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_paper_nodata",
            title="SYNTHETIC paper without metadata",
            session=None,
            exam_type="final",
            questions=[
                ("q1", "Explain array indexing.", 5.0),
                ("q2", "Explain linked list insertion.", None),
            ],
        )
        _add_analysis(database, COURSE, syllabus, questions["q1"])
        _add_analysis(database, COURSE, syllabus, questions["q2"])

        report = _profile(database, syllabus=syllabus)

        assert report.summary.questions_with_marks == 1
        assert report.distributions.missing_marks == 1
        assert report.summary.total_known_marks == 5.0
        assert report.data_quality.questions_without_session == 2
        assert report.data_quality.known_sessions == 0
        assert report.data_quality.papers_with_dates == 0
        codes = {warning.code for warning in report.warnings}
        assert {"exam_dna_missing_marks", "exam_dna_missing_dates"} <= codes
        assert report.status is ExamDnaStatus.PARTIAL
    finally:
        database.close()


def test_partial_analysis_is_reported_without_inventing_labels() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        analysed = _add_paper(
            database,
            COURSE,
            "doc_analysed",
            title="SYNTHETIC analysed paper",
            session="2024-05",
            questions=[("m1", "Explain arrays.", 5.0)],
        )
        _add_paper(
            database,
            COURSE,
            "doc_unanalysed",
            title="SYNTHETIC unanalysed paper",
            session="2025-05",
            questions=[("m2", "Explain trees.", 5.0)],
        )
        _add_analysis(database, COURSE, syllabus, analysed["m1"])

        report = _profile(database, syllabus=syllabus)
        types = {row.label: row for row in report.distributions.question_types}

        assert report.summary.eligible_papers == 2
        assert report.data_quality.analysed_questions == 1
        assert report.data_quality.analysis_coverage_rate == 0.5
        assert types["not_analysed"].question_count == 1
        assert types["explanation"].question_count == 1
        assert any(warning.code == "exam_dna_missing_analysis" for warning in report.warnings)
        assert report.status is ExamDnaStatus.PARTIAL
    finally:
        database.close()


def test_empty_dataset_is_insufficient_and_reports_no_rate() -> None:
    database = memory_database()
    try:
        _add_course(database)
        _add_syllabus(database)

        report = _profile(database)

        assert report.status is ExamDnaStatus.INSUFFICIENT_DATA
        assert report.summary.eligible_papers == 0
        assert report.summary.questions == 0
        assert report.patterns == []
        assert report.distributions.question_types == []
        assert {warning.code for warning in report.warnings} >= {"exam_dna_no_questions"}
    finally:
        database.close()


def test_pattern_in_every_paper_has_full_recurrence() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        for index, session in enumerate(("2024-05", "2025-05", "2026-05"), start=1):
            questions = _add_paper(
                database,
                COURSE,
                f"doc_numeric_{index}",
                title=f"SYNTHETIC paper {index}",
                session=session,
                questions=[(f"q{index}", "Calculate the array sum.", 5.0)],
            )
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[f"q{index}"],
                topic="Arrays",
                primary_type=QuestionType.NUMERICAL,
                bloom=BloomLevel.APPLY,
            )

        report = _profile(database, syllabus=syllabus)
        pattern = next(
            item
            for item in report.patterns
            if item.category is PatternCategory.QUESTION_TYPE and item.key == "numerical"
        )

        assert pattern.papers_observed == 3
        assert pattern.eligible_papers == 3
        assert pattern.recurrence_rate == 1.0
        assert pattern.denominator_valid is True
        assert pattern.is_recurring is True
        assert pattern.sessions_observed == 3
        assert pattern.eligible_sessions == 3
        assert pattern.session_recurrence_rate == 1.0
        assert report.summary.recurring_pattern_count >= 1
    finally:
        database.close()


def test_pattern_in_some_papers_uses_an_explicit_denominator() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        for index, session in enumerate(("2022-05", "2023-05", "2024-05", "2025-05"), start=1):
            question_id = f"q{index}"
            question_type = QuestionType.NUMERICAL if index <= 2 else QuestionType.EXPLANATION
            questions = _add_paper(
                database,
                COURSE,
                f"doc_partial_{index}",
                title=f"SYNTHETIC paper {index}",
                session=session,
                questions=[
                    (
                        question_id,
                        "Calculate the array sum." if index <= 2 else "Explain arrays.",
                        5.0,
                    )
                ],
            )
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                topic="Arrays",
                primary_type=question_type,
            )

        report = _profile(database, syllabus=syllabus)
        pattern = next(
            item
            for item in report.patterns
            if item.category is PatternCategory.QUESTION_TYPE and item.key == "numerical"
        )

        assert pattern.papers_observed == 2
        assert pattern.eligible_papers == 4
        assert pattern.recurrence_rate == 0.5
        assert pattern.is_recurring is True
        assert pattern.evidence[0].startswith("Observed in 2 of 4 eligible papers")
    finally:
        database.close()


def test_within_paper_repetition_is_not_cross_paper_recurrence() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_repeat_one",
            title="SYNTHETIC repeated paper",
            session="2024-05",
            questions=[
                ("a1", "Calculate the array sum.", 5.0),
                ("a2", "Calculate the tree height.", 5.0),
                ("a3", "Calculate the graph degree.", 5.0),
            ],
        )
        for question_id in ("a1", "a2", "a3"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                first[question_id],
                topic="Arrays",
                primary_type=QuestionType.NUMERICAL,
            )
        for index, session in enumerate(("2025-05", "2026-05"), start=2):
            question_id = f"b{index}"
            questions = _add_paper(
                database,
                COURSE,
                f"doc_repeat_{index}",
                title=f"SYNTHETIC paper {index}",
                session=session,
                questions=[(question_id, "Explain array indexing.", 5.0)],
            )
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                topic="Arrays",
                primary_type=QuestionType.EXPLANATION,
            )

        report = _profile(database, syllabus=syllabus)
        pattern = next(
            item
            for item in report.patterns
            if item.category is PatternCategory.QUESTION_TYPE and item.key == "numerical"
        )

        assert pattern.papers_observed == 1
        assert pattern.eligible_papers == 3
        assert pattern.within_paper_occurrences == 3
        assert pattern.recurrence_rate == pytest.approx(1 / 3, abs=1e-3)
        assert pattern.is_recurring is False
        assert any("single paper" in limitation for limitation in pattern.limitations)
    finally:
        database.close()


def test_duplicate_paper_copies_are_excluded_from_independent_evidence() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_copy_a",
            title="SYNTHETIC paper 2024",
            session="2024-05",
            text_fingerprint="duplicate-copy-fingerprint",
            questions=[
                ("a1", "Explain array indexing.", 5.0),
                ("a2", "Explain linked list insertion.", 5.0),
            ],
        )
        _add_paper(
            database,
            COURSE,
            "doc_copy_b",
            title="SYNTHETIC paper 2024 (copy)",
            session="2025-05",
            text_fingerprint="duplicate-copy-fingerprint",
            questions=[
                ("b1", "Explain array indexing.", 5.0),
                ("b2", "Explain linked list insertion.", 5.0),
            ],
        )
        third = _add_paper(
            database,
            COURSE,
            "doc_copy_c",
            title="SYNTHETIC paper 2026",
            session="2026-05",
            questions=[("q3", "Explain graph traversal.", 5.0)],
        )
        for question in first.values():
            _add_analysis(database, COURSE, syllabus, question)
        _add_analysis(database, COURSE, syllabus, third["q3"], topic="Graphs")

        report = _profile(database, syllabus=syllabus)
        copy = next(item for item in report.papers if item.document_id == "doc_copy_b")

        assert report.summary.eligible_papers == 2
        assert report.summary.excluded_papers == 1
        assert report.data_quality.duplicate_copies_excluded == 1
        assert report.data_quality.duplicate_paper_groups == 1
        assert copy.eligible is False
        assert copy.exclusion_reason is PaperExclusionReason.DUPLICATE_COPY
        assert copy.duplicate_of == "doc_copy_a"
        assert any(warning.code == "exam_dna_duplicate_paper" for warning in report.warnings)
        explanation = next(
            item
            for item in report.patterns
            if item.category is PatternCategory.QUESTION_TYPE and item.key == "explanation"
        )
        assert explanation.eligible_papers == 2
    finally:
        database.close()


def test_papers_from_one_session_share_a_session_denominator() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        for index in (1, 2):
            questions = _add_paper(
                database,
                COURSE,
                f"doc_same_session_{index}",
                title=f"SYNTHETIC set {index}",
                session="2024-05",
                questions=[(f"q{index}", "Explain array indexing.", 5.0)],
            )
            _add_analysis(database, COURSE, syllabus, questions[f"q{index}"])
        third = _add_paper(
            database,
            COURSE,
            "doc_other_session",
            title="SYNTHETIC other session",
            session="2025-05",
            questions=[("q3", "Explain array indexing.", 5.0)],
        )
        _add_analysis(database, COURSE, syllabus, third["q3"])

        report = _profile(database, syllabus=syllabus)
        pattern = next(
            item
            for item in report.patterns
            if item.category is PatternCategory.QUESTION_TYPE and item.key == "explanation"
        )

        assert report.data_quality.known_sessions == 2
        assert pattern.papers_observed == 3
        assert pattern.eligible_papers == 3
        assert pattern.sessions_observed == 2
        assert pattern.eligible_sessions == 2
        assert pattern.session_recurrence_rate == 1.0
    finally:
        database.close()


def test_exact_duplicate_questions_are_grouped_across_papers() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_exact_a",
            title="SYNTHETIC paper A",
            session="2024-05",
            questions=[("q1", "Explain array indexing and memory layout.", 5.0)],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_exact_b",
            title="SYNTHETIC paper B",
            session="2025-05",
            questions=[("q2", "Explain array indexing and memory layout. [5 marks]", 5.0)],
        )
        _add_analysis(database, COURSE, syllabus, first["q1"])
        _add_analysis(database, COURSE, syllabus, second["q2"])

        report = _profile(database, syllabus=syllabus)
        groups = [
            group for group in report.question_groups if group.kind is QuestionGroupKind.EXACT
        ]

        assert len(groups) == 1
        group = groups[0]
        assert group.question_count == 2
        assert group.document_count == 2
        assert group.score == 1.0
        assert group.review_required is False
        assert group.shared_concepts == ["Arrays"]
        assert group.template_variant is False
        assert {member.question_id for member in group.members} == {"q1", "q2"}
        assert group.min_score == 1.0
    finally:
        database.close()


def test_similarity_kinds_stay_distinct_and_reuse_stored_pairs() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_similarity",
            title="SYNTHETIC similarity paper",
            session="2024-05",
            questions=[
                ("q1", "Explain array indexing.", 5.0),
                ("q2", "Describe how arrays are indexed.", 5.0),
                ("q3", "Compare arrays and linked lists.", 5.0),
                ("q4", "Explain how arrays store elements.", 5.0),
            ],
        )
        for question in questions.values():
            _add_analysis(database, COURSE, syllabus, question)
        _set_similarities(
            database,
            COURSE,
            [
                ("q1", "q2", SimilarityKind.NEAR, 0.91),
                ("q1", "q3", SimilarityKind.SEMANTIC, 0.74),
                ("q1", "q4", SimilarityKind.CONCEPT_RELATED, 0.51),
            ],
        )

        report = _profile(database, syllabus=syllabus)
        kinds = {group.kind for group in report.question_groups}

        assert {
            QuestionGroupKind.NEAR_DUPLICATE,
            QuestionGroupKind.SEMANTIC,
            QuestionGroupKind.CONCEPT_RELATED,
        } <= kinds
        near = next(
            group
            for group in report.question_groups
            if group.kind is QuestionGroupKind.NEAR_DUPLICATE
        )
        assert near.score == pytest.approx(0.91)
        assert near.review_required is True
        assert "stored Phase 3" in near.evidence[0]
        concept_group = next(
            group
            for group in report.question_groups
            if group.kind is QuestionGroupKind.CONCEPT_RELATED
        )
        assert concept_group.evidence_quality.value == "low"
        assert report.data_quality.similarity_pairs_considered == 3
        assert report.data_quality.similarity_pairs_reused == 3
    finally:
        database.close()


def test_similarity_below_threshold_is_excluded_and_reported() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_low_similarity",
            title="SYNTHETIC low similarity paper",
            session="2024-05",
            questions=[
                ("q1", "Explain array indexing.", 5.0),
                ("q2", "Explain tree traversal.", 5.0),
            ],
        )
        for question in questions.values():
            _add_analysis(database, COURSE, syllabus, question)
        _set_similarities(database, COURSE, [("q1", "q2", SimilarityKind.SEMANTIC, 0.50)])

        report = _profile(
            database,
            syllabus=syllabus,
            settings=ExamDnaSettings(similarity_min_score=0.8),
        )

        assert report.question_groups == []
        assert report.data_quality.similarity_pairs_below_threshold == 1
        assert report.data_quality.similarity_pairs_reused == 0
        assert any(warning.code == "exam_dna_no_similarity" for warning in report.warnings)
    finally:
        database.close()


def test_template_variant_requires_shared_concept_type_and_verb() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_template",
            title="SYNTHETIC template paper",
            session="2024-05",
            questions=[
                ("q1", "Calculate the sum of the array elements.", 5.0),
                ("q2", "Calculate the array storage requirement.", 5.0),
                ("q3", "Explain the array memory layout.", 5.0),
                ("q4", "Explain the array indexing rule.", 5.0),
            ],
        )
        for question_id in ("q1", "q2"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                primary_type=QuestionType.NUMERICAL,
            )
        for question_id in ("q3", "q4"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                primary_type=QuestionType.EXPLANATION,
            )
        _set_similarities(
            database,
            COURSE,
            [
                ("q1", "q2", SimilarityKind.CONCEPT_RELATED, 0.72),
                ("q3", "q4", SimilarityKind.CONCEPT_RELATED, 0.72),
            ],
        )

        report = _profile(database, syllabus=syllabus)
        groups = {
            frozenset(member.question_id for member in group.members): group
            for group in report.question_groups
        }

        assert groups[frozenset({"q1", "q2"})].template_variant is True
        assert groups[frozenset({"q1", "q2"})].shared_command_verb == "calculate"
        assert groups[frozenset({"q3", "q4"})].template_variant is True
        assert groups[frozenset({"q3", "q4"})].shared_command_verb == "explain"
    finally:
        database.close()


def test_course_and_institution_isolation() -> None:
    database = memory_database()
    try:
        _add_course(database, COURSE, institution_code="inst-a")
        _add_course(database, COURSE_B, institution_code="inst-b")
        syllabus_a = _add_syllabus(database, COURSE)
        syllabus_b = _add_syllabus(database, COURSE_B)
        first = _add_paper(
            database,
            COURSE,
            "doc_course_a",
            title="SYNTHETIC course A paper",
            session="2024-05",
            questions=[
                ("ca1", "Explain array indexing.", 5.0),
                ("ca2", "Describe how arrays are indexed.", 5.0),
            ],
        )
        second = _add_paper(
            database,
            COURSE_B,
            "doc_course_b",
            title="SYNTHETIC course B paper",
            session="2024-05",
            questions=[("cb1", "Explain graph traversal.", 5.0)],
        )
        _add_analysis(database, COURSE, syllabus_a, first["ca1"], topic="Arrays")
        _add_analysis(database, COURSE, syllabus_a, first["ca2"], topic="Arrays")
        _add_analysis(database, COURSE_B, syllabus_b, second["cb1"], topic="Graphs")
        _set_similarities(database, COURSE, [("ca1", "ca2", SimilarityKind.SEMANTIC, 0.80)])

        report_a = _profile(database, COURSE, syllabus=syllabus_a)
        report_b = _profile(database, COURSE_B, syllabus=syllabus_b)

        assert report_a.course.institution_code == "inst-a"
        assert report_b.course.institution_code == "inst-b"
        assert {paper.document_id for paper in report_a.papers} == {"doc_course_a"}
        assert {paper.document_id for paper in report_b.papers} == {"doc_course_b"}
        assert report_a.summary.questions == 2
        assert report_b.summary.questions == 1
        assert {row.label for row in report_a.distributions.units} == {"Unit 1"}
        assert report_b.question_groups == []
        assert {
            member.question_id for group in report_a.question_groups for member in group.members
        } == {
            "ca1",
            "ca2",
        }
        assert {row.label for row in report_b.distributions.units} == {"Unit 2"}
        assert any(
            concept.label == "Graphs" and concept.distinct_question_count == 1
            for concept in report_b.coverage.concepts
        )
    finally:
        database.close()


def test_syllabus_version_isolation_requires_explicit_selection() -> None:
    database = memory_database()
    try:
        _add_course(database)
        first = _add_syllabus(database, suffix="a")
        second = _add_syllabus(
            database,
            suffix="b",
            units=[("Unit 1", "Modern", ["Neural Networks"])],
        )
        questions = _add_paper(
            database,
            COURSE,
            "doc_versioned",
            title="SYNTHETIC versioned paper",
            session="2024-05",
            questions=[("q1", "Explain neural network training.", 5.0)],
        )
        _add_analysis(
            database,
            COURSE,
            second,
            questions["q1"],
            topic="Neural Networks",
        )

        with pytest.raises(ExamDnaError) as captured:
            _profile(database)
        assert captured.value.code == "ambiguous_syllabus"

        report = _profile(database, syllabus=second)
        assert report.syllabus.syllabus_id == second.syllabus_id
        assert report.syllabus.selected_automatically is False
        assert {row.label for row in report.distributions.units} == {"Unit 1"}
        assert report.coverage.concepts[0].label == "Neural Networks"
        assert first.syllabus_id != report.syllabus.syllabus_id
    finally:
        database.close()


def test_mixed_exam_types_and_marks_totals_are_reported() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_mixed_final",
            title="SYNTHETIC final",
            session="2024-05",
            exam_type="final",
            questions=[("q1", "Explain arrays.", 10.0), ("q2", "Explain trees.", 10.0)],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_mixed_midterm",
            title="SYNTHETIC midterm",
            session="2024-09",
            exam_type="midterm",
            questions=[("q3", "Explain graphs.", 15.0), ("q4", "Explain hashing.", 15.0)],
        )
        third = _add_paper(
            database,
            COURSE,
            "doc_mixed_final_two",
            title="SYNTHETIC final 2",
            session="2025-05",
            exam_type="final",
            questions=[("q5", "Explain arrays.", 10.0), ("q6", "Explain trees.", 10.0)],
        )
        for question in list(first.values()) + list(second.values()) + list(third.values()):
            _add_analysis(database, COURSE, syllabus, question)

        report = _profile(database, syllabus=syllabus)

        assert set(report.data_quality.exam_types) == {"final", "midterm"}
        codes = {warning.code for warning in report.warnings}
        assert {"exam_dna_mixed_exam_types", "exam_dna_different_marks_totals"} <= codes
        assert report.status is ExamDnaStatus.PARTIAL
    finally:
        database.close()


def test_partial_extraction_and_low_confidence_are_reported() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_partial_extraction",
            title="SYNTHETIC partial paper",
            session="2024-05",
            extraction_status=ExtractionStatus.PARTIAL,
            questions=[("q1", "Explain arrays.", 5.0)],
        )
        _add_analysis(
            database,
            COURSE,
            syllabus,
            questions["q1"],
            type_confidence=0.2,
            bloom_confidence=0.2,
        )

        report = _profile(database, syllabus=syllabus)

        assert report.data_quality.low_confidence_questions == 1
        codes = {warning.code for warning in report.warnings}
        assert {"exam_dna_low_confidence", "exam_dna_partial_extraction"} <= codes
        assert report.status is ExamDnaStatus.PARTIAL
    finally:
        database.close()


def test_section_and_choice_patterns_use_explicit_cues_only() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        first = _add_paper(
            database,
            COURSE,
            "doc_choice_one",
            title="SYNTHETIC choice paper",
            session="2024-05",
            sections={"q1": "A", "q2": "B"},
            questions=[
                ("q1", "Answer any two of the following questions. Explain arrays.", 5.0),
                ("q2", "Explain trees.", 5.0),
            ],
        )
        second = _add_paper(
            database,
            COURSE,
            "doc_choice_two",
            title="SYNTHETIC compulsory paper",
            session="2025-05",
            sections={"q3": "A", "q4": "B"},
            questions=[
                ("q3", "All questions are compulsory. Explain arrays.", 5.0),
                ("q4", "Explain trees.", 5.0),
            ],
        )
        third = _add_paper(
            database,
            COURSE,
            "doc_choice_three",
            title="SYNTHETIC ambiguous paper",
            session="2026-05",
            questions=[("q5", "Answer any of the following questions.", 5.0)],
        )
        for question in list(first.values()) + list(second.values()) + list(third.values()):
            _add_analysis(database, COURSE, syllabus, question)

        report = _profile(database, syllabus=syllabus)
        choices = {row.label: row for row in report.distributions.choice_rules}

        assert choices["choose_any (choose 2)"].papers_observed == 1
        assert choices["all_compulsory"].papers_observed == 1
        kinds = {
            pattern.key
            for pattern in report.patterns
            if pattern.category is PatternCategory.CHOICE_STRUCTURE
        }
        assert {"choose_any:2", "all_compulsory"} <= kinds
        assert not any(
            "sorting" in pattern.key
            for pattern in report.patterns
            if pattern.category is PatternCategory.CHOICE_STRUCTURE
        )
        section_pattern = next(
            pattern
            for pattern in report.patterns
            if pattern.category is PatternCategory.SECTION_STRUCTURE
        )
        assert section_pattern.papers_observed == 2
        assert section_pattern.recurrence_rate == pytest.approx(2 / 3, abs=1e-3)
        assert report.data_quality.papers_with_sections == 2
        assert any(warning.code == "exam_dna_choice_uncertain" for warning in report.warnings)
    finally:
        database.close()


def test_question_banks_are_not_counted_as_papers() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        paper = _add_paper(
            database,
            COURSE,
            "doc_real_paper",
            title="SYNTHETIC paper",
            session="2024-05",
            questions=[("p1", "Explain arrays.", 5.0)],
        )
        bank = _add_paper(
            database,
            COURSE,
            "doc_bank",
            title="SYNTHETIC bank",
            session=None,
            kind=DocumentKind.QUESTION_BANK,
            questions=[("b1", "Explain trees.", 5.0)],
        )
        _add_analysis(database, COURSE, syllabus, paper["p1"])
        _add_analysis(database, COURSE, syllabus, bank["b1"], topic="Trees")

        report = _profile(database, syllabus=syllabus)

        assert report.data_quality.question_banks_excluded == 1
        assert report.summary.eligible_papers == 1
        assert {item.document_id for item in report.papers} == {"doc_real_paper"}
        assert report.summary.questions == 1
    finally:
        database.close()


def test_max_questions_truncation_is_reported() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        questions = _add_paper(
            database,
            COURSE,
            "doc_truncation",
            title="SYNTHETIC truncation paper",
            session="2024-05",
            questions=[
                ("q1", "Explain arrays.", 5.0),
                ("q2", "Explain trees.", 5.0),
                ("q3", "Explain graphs.", 5.0),
            ],
        )
        for question in questions.values():
            _add_analysis(database, COURSE, syllabus, question)

        report = _profile(
            database,
            syllabus=syllabus,
            settings=ExamDnaSettings(max_questions=2),
        )

        assert report.summary.questions == 2
        assert report.data_quality.truncated is True
        assert any(warning.code == "exam_dna_truncated" for warning in report.warnings)
        assert report.status is ExamDnaStatus.PARTIAL
    finally:
        database.close()


def test_filters_are_validated_and_echoed() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        _three_papers(database, syllabus)

        scoped = _profile(
            database,
            syllabus=syllabus,
            filters=ExamDnaFilters(document_ids=["doc_paper_2025"]),
        )
        assert scoped.summary.eligible_papers == 1
        assert scoped.filters.document_ids == ["doc_paper_2025"]
        assert scoped.data_quality.excluded_papers == 2

        with pytest.raises(ExamDnaError) as captured:
            _profile(
                database,
                syllabus=syllabus,
                filters=ExamDnaFilters(
                    document_ids=["doc_missing"],
                    source_ids=["source_missing"],
                    exam_types=["oral"],
                    exam_periods=["1999-01"],
                ),
            )
        assert captured.value.code == "invalid_exam_dna_filter"
        assert captured.value.detail["unknown_document_ids"] == ["doc_missing"]
        assert captured.value.detail["unknown_source_ids"] == ["source_missing"]
        assert captured.value.detail["unknown_exam_types"] == ["oral"]
        assert captured.value.detail["unknown_exam_periods"] == ["1999-01"]
    finally:
        database.close()


def test_date_range_filter_records_unverifiable_papers() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        dated = _add_paper(
            database,
            COURSE,
            "doc_dated",
            title="SYNTHETIC dated paper",
            session=None,
            exam_date="2024-05-10",
            questions=[("q1", "Explain arrays.", 5.0)],
        )
        undated = _add_paper(
            database,
            COURSE,
            "doc_undated",
            title="SYNTHETIC undated paper",
            session=None,
            questions=[("q2", "Explain trees.", 5.0)],
        )
        _add_analysis(database, COURSE, syllabus, dated["q1"])
        _add_analysis(database, COURSE, syllabus, undated["q2"], topic="Trees")

        report = _profile(
            database,
            syllabus=syllabus,
            filters=ExamDnaFilters(date_from="2024-01-01", date_to="2024-12-31"),
        )

        assert report.summary.eligible_papers == 1
        assert report.data_quality.papers_without_dates == 0
        filtered = [
            exclusion
            for exclusion in report.data_quality.exclusions
            if exclusion.reason is PaperExclusionReason.FILTERED
        ]
        assert {item.document_id for item in filtered} == {"doc_undated"}
        assert any(warning.code == "exam_dna_missing_dates" for warning in report.warnings)
    finally:
        database.close()


def test_profiles_are_deterministic_and_read_only() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        _three_papers(database, syllabus)

        with database.session() as session:
            tables = [
                row[0]
                for row in session.execute(
                    text(
                        "select name from sqlite_master where type='table' "
                        "and name not like 'sqlite_%'"
                    )
                )
            ]
            before = {
                table: session.execute(text(f"select count(*) from {table}")).scalar()
                for table in tables
            }

        first = _profile(database, syllabus=syllabus)
        second = _profile(database, syllabus=syllabus)

        assert first.profile_id == second.profile_id
        assert first.model_dump(exclude={"generated_at"}) == second.model_dump(
            exclude={"generated_at"}
        )

        with database.session() as session:
            after = {
                table: session.execute(text(f"select count(*) from {table}")).scalar()
                for table in tables
            }
        assert before == after
    finally:
        database.close()


def test_unit_marks_share_command_verb_and_marks_value_patterns() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        for index, session in enumerate(("2024-05", "2025-05", "2026-05"), start=1):
            heavy_id = f"u{index}a"
            light_id = f"u{index}b"
            questions = _add_paper(
                database,
                COURSE,
                f"doc_marks_{index}",
                title=f"SYNTHETIC marks paper {index}",
                session=session,
                questions=[
                    (heavy_id, "Explain the array structure.", 10.0),
                    (light_id, "Describe the tree structure.", 5.0),
                ],
            )
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[heavy_id],
                topic="Arrays",
                primary_type=QuestionType.EXPLANATION,
                bloom=BloomLevel.UNDERSTAND,
            )
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[light_id],
                topic="Trees",
                primary_type=QuestionType.DEFINITION,
                bloom=BloomLevel.REMEMBER,
            )

        report = _profile(database, syllabus=syllabus)
        by_key = {(pattern.category, pattern.key): pattern for pattern in report.patterns}

        unit_share = by_key[(PatternCategory.UNIT_MARKS_SHARE, "Unit 1")]
        assert unit_share.papers_observed == 3
        assert unit_share.recurrence_rate == 1.0
        assert unit_share.is_recurring is True

        verb = by_key[(PatternCategory.COMMAND_VERB, "explain")]
        assert verb.papers_observed == 3
        assert verb.is_recurring is True

        marks = by_key[(PatternCategory.MARKS_VALUE, "10")]
        assert marks.papers_observed == 3
        assert marks.within_paper_occurrences == 3

        bloom = by_key[(PatternCategory.BLOOM_LEVEL, "understand")]
        assert bloom.papers_observed == 3
    finally:
        database.close()


def test_settings_are_echoed_and_thresholds_are_configurable() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        _three_papers(database, syllabus)

        default = _profile(database, syllabus=syllabus)
        strict = _profile(
            database,
            syllabus=syllabus,
            settings=ExamDnaSettings(recurrence_threshold=0.99),
        )

        assert default.parameters["recurrence_threshold"] == 0.5
        assert strict.parameters["recurrence_threshold"] == 0.99
        assert strict.summary.recurring_pattern_count <= default.summary.recurring_pattern_count
        assert strict.patterns[0].recurrence_rate is not None
    finally:
        database.close()


def test_unit_analytics_stay_inside_the_selected_syllabus_version() -> None:
    database = memory_database()
    try:
        _add_course(database)
        version_a = _add_syllabus(
            database,
            suffix="a",
            units=[
                ("Unit 1", "Version A foundations", ["Arrays"]),
                ("Unit 2", "Version A structures", ["Graphs"]),
            ],
        )
        version_b = _add_syllabus(
            database,
            suffix="b",
            units=[("Unit 1", "Version B foundations", ["Stacks"])],
        )
        questions = _add_paper(
            database,
            COURSE,
            "doc_version_a_paper",
            title="SYNTHETIC paper written against syllabus version A",
            session="2024-05",
            questions=[
                ("va1", "Explain array indexing and memory layout.", 5.0),
                ("va2", "Explain graph representations.", 5.0),
            ],
        )
        _add_analysis(database, COURSE, version_a, questions["va1"], topic="Arrays")
        _add_analysis(database, COURSE, version_a, questions["va2"], topic="Graphs")

        report = _profile(database, syllabus=version_b)

        assert [row.label for row in report.distributions.units] == []
        assert report.papers[0].unit_counts == {}
        assert not [
            pattern
            for pattern in report.patterns
            if pattern.category in {PatternCategory.UNIT_PRESENCE, PatternCategory.UNIT_MARKS_SHARE}
        ]
        assert report.data_quality.questions_mapped_to_other_syllabus_versions == 2
        assert report.data_quality.papers_mapped_to_other_syllabus_versions == 1
        assert "exam_dna_mixed_syllabus_versions" in {item.code for item in report.warnings}
        assert report.status is ExamDnaStatus.PARTIAL
        assert report.limitations[-1].startswith(
            "Some questions are mapped to a different syllabus version"
        )
    finally:
        database.close()


def test_dated_sessions_form_an_ordered_series_with_descriptive_changes() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        _three_papers(database, syllabus)

        report = _profile(database, syllabus=syllabus)

        assert [session.session for session in report.sessions] == [
            "exam_session:2024-05|type:final",
            "exam_session:2025-05|type:final",
            "exam_session:2026-05|type:final",
        ]
        first, second, third = report.sessions
        assert first.question_type_shares["explanation"] == 1.0
        assert second.question_type_shares["explanation"] == 0.5
        assert second.question_type_shares["numerical"] == 0.5
        assert third.question_type_shares["definition"] == 0.5
        assert third.question_type_shares["explanation"] == 0.5
        assert first.known_marks == 10.0
        assert first.unit_marks_shares["Unit 1"] == 1.0
        assert first.unit_marks_shares["Unit 2"] == 0.0
        assert second.unit_marks_shares["Unit 2"] == 1.0

        trends = {(trend.dimension, trend.key): trend for trend in report.trends}
        explanation = trends[("question_type", "explanation")]
        assert explanation.values == [1.0, 0.5, 0.5]
        assert explanation.first_value == 1.0
        assert explanation.last_value == 0.5
        assert explanation.change == -0.5
        assert explanation.direction == "decreased"
        assert "not project" in " ".join(explanation.limitations)

        definition = trends[("question_type", "definition")]
        assert definition.values == [0.0, 0.0, 0.5]
        assert definition.direction == "increased"

        unit_trend = trends[("unit_marks_share", "Unit 2")]
        assert unit_trend.values == [0.0, 1.0, 0.5]
        assert unit_trend.direction == "increased"

        assert report.summary.dated_sessions == 3
        assert report.summary.trend_count == len(report.trends)
        assert report.summary.trend_count > 0
    finally:
        database.close()


def test_undated_papers_do_not_enter_the_session_series() -> None:
    database = memory_database()
    try:
        _add_course(database)
        syllabus = _add_syllabus(database)
        dated = _add_paper(
            database,
            COURSE,
            "doc_dated_paper",
            title="SYNTHETIC dated paper",
            session="2024-05",
            questions=[("dp1", "Explain array indexing and memory layout.", 5.0)],
        )
        undated = _add_paper(
            database,
            COURSE,
            "doc_undated_paper",
            title="SYNTHETIC undated paper",
            session=None,
            questions=[("up1", "Explain tree traversal orders.", 5.0)],
        )
        _add_analysis(database, COURSE, syllabus, dated["dp1"], topic="Arrays")
        _add_analysis(database, COURSE, syllabus, undated["up1"], topic="Trees")

        report = _profile(database, syllabus=syllabus)

        assert len(report.sessions) == 1
        assert report.sessions[0].document_ids == ["doc_dated_paper"]
        assert report.trends == []
        assert report.data_quality.questions_without_session == 1
        assert report.summary.eligible_papers == 2
    finally:
        database.close()
