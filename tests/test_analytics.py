from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exam_engine.analytics.service import AnalyticsService
from exam_engine.config import AnalyticsSettings, Settings, UnderstandingSettings
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.analytics import AnalyticsFilters
from exam_engine.models.course import CourseRecord
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    DifficultyLabel,
    DocumentKind,
    ExtractionStatus,
    QuestionType,
    SimilarityKind,
    SourceFormat,
)
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.understanding import Subpart
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
)
from exam_engine.understanding.service import UnderstandingService
from tests.support import make_course, make_source

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_MANIFEST = PROJECT_ROOT / "datasets" / "analytics_sample" / "manifest.json"
SAMPLE_MANIFEST = PROJECT_ROOT / "datasets" / "sample" / "manifest.json"
COURSE = "demo-analytics-cs201-2024-v1"


def _ingest_and_analyse(database, manifest: Path = ANALYTICS_MANIFEST) -> None:
    batch = IngestionPipeline(database, settings=Settings()).ingest_manifest(manifest)
    assert batch.ok, [error.code for error in batch.all_errors]
    reports = UnderstandingService(database, UnderstandingSettings()).analyze()
    assert all(report.ok for report in reports)


def _seed_unanalysed_question(
    database,
    *,
    course_id: str = "test-metadata-2024-v1",
    academic_year: str | None = None,
    document_academic_year: str | None = None,
    marks: float | None = None,
    metadata: dict | None = None,
    uncertainties: list[Uncertainty] | None = None,
    document_id: str = "doc_metadata_fixture",
    question_id: str = "q_metadata_fixture",
    text: str = "Explain the missing metadata case.",
    text_fingerprint: str = "",
) -> CourseRecord:
    course = make_course(course_id=course_id, academic_year=academic_year)
    source = make_source()
    document = DocumentRecord(
        document_id=document_id,
        course_id=course.course_id,
        source_id=source.source_id,
        kind=DocumentKind.QUESTION_PAPER,
        format=SourceFormat.TXT,
        title=f"Metadata fixture paper {document_id}",
        original_filename="metadata.txt",
        content_sha256=hashlib.sha256(document_id.encode()).hexdigest(),
        text_fingerprint=text_fingerprint,
        byte_size=120,
        page_count=1,
        char_count=80,
        extraction_status=ExtractionStatus.OK,
        academic_year=document_academic_year,
        pipeline_version="test",
        metadata=metadata or {},
    )
    question = QuestionRecord(
        question_id=question_id,
        document_id=document.document_id,
        course_id=course.course_id,
        text=text,
        marks=marks,
        confidence=0.9,
        uncertainties=uncertainties or [],
    )
    with database.transaction() as session:
        CourseRepository(session).upsert(course)
        DatasetSourceRepository(session).upsert(source)
        DocumentRepository(session).upsert(document)
        QuestionRepository(session).replace_for_document(document.document_id, [question])
    return course


def test_report_preserves_denominators_sessions_and_evidence(database) -> None:
    _ingest_and_analyse(database)
    report = AnalyticsService(database).analyze_course(COURSE)

    assert report.course.course_id == COURSE
    assert report.data_quality.questions == 18
    assert report.data_quality.distinct_question_texts == 14
    assert report.data_quality.documents == 4
    assert report.data_quality.question_papers == 3
    assert report.data_quality.known_sessions == 3
    assert report.data_quality.known_periods == 3
    assert report.data_quality.questions_with_period == 18
    assert report.data_quality.exact_duplicate_appearances == 8

    repetition = report.repetition
    assert repetition.exact_group_count == 4
    assert repetition.exact_questions == 8
    assert repetition.exact_repeated_appearances == 4
    assert repetition.within_document_groups == 0
    assert repetition.across_document_groups == 4
    assert repetition.across_session_groups == 4
    assert repetition.distinct_question_texts == 14
    assert repetition.similarity_coverage
    assert sum(row.pair_count for row in repetition.similarity_coverage) == (
        repetition.near_pairs + repetition.semantic_pairs + repetition.concept_related_pairs
    )
    for row in repetition.similarity_coverage:
        assert row.kind is not SimilarityKind.EXACT
        assert row.question_count >= 2
        assert row.document_count >= 1
        assert row.within_document_pairs + row.across_document_pairs == row.pair_count
        assert (
            row.same_session_pairs + row.different_session_pairs + row.unknown_session_pairs
            == row.pair_count
        )
    assert len(repetition.document_frequency) == 3
    assert sum(row.question_count for row in repetition.document_frequency) == 18
    periods_seen: set[str] = set()
    for group in repetition.exact_groups:
        assert group.question_count == 2
        assert group.document_count == 2
        assert group.known_session_count == 2
        assert group.across_documents is True
        assert group.across_sessions is True
        assert len({appearance.exam_period for appearance in group.appearances}) == 2
        assert all(appearance.text for appearance in group.appearances)
        periods_seen.update(appearance.exam_period for appearance in group.appearances)
    assert periods_seen == {"2024-05", "2024-11", "2025-04"}

    mapping = report.mapping
    assert mapping.total_questions == 18
    assert (
        mapping.mapped_questions
        + mapping.ambiguous_questions
        + mapping.unmapped_questions
        + mapping.not_analysed_questions
        == 18
    )
    assert mapping.mapped_questions == 16
    assert mapping.ambiguous_questions == 2
    assert mapping.unit_assignments == 20
    assert mapping.multi_unit_questions == 2
    assert mapping.mapping_coverage == round(mapping.questions_with_any_unit / 18, 4)
    assert len(mapping.units) == 5
    assert len(mapping.ambiguous_references) == 2
    assert sum(row.question_count for row in mapping.units) >= 18
    assert all(row.share_of_questions == round(row.question_count / 18, 4) for row in mapping.units)
    assert all(
        row.share_of_questions == round(row.question_count / 18, 4) for row in mapping.topics
    )
    frequency_document_ids = {row.document_id for row in repetition.document_frequency}
    for row in (*mapping.units, *mapping.topics, *mapping.concepts):
        if row.question_count:
            assert row.document_ids
            assert set(row.document_ids) <= frequency_document_ids

    marks = report.marks
    assert marks.total_questions == 18
    assert marks.questions_with_marks == 18
    assert marks.missing_marks == 0
    assert marks.total_marks == 124
    assert sum(row.marks for row in marks.question_marks) == 124
    assert sum(row.question_count for row in marks.common_allocations) == 18
    assert sum(marks.marks_sources.values()) == marks.questions_with_marks
    for breakdown in (
        marks.by_section,
        marks.by_unit,
        marks.by_topic,
        marks.by_question_type,
        marks.by_difficulty,
    ):
        assert breakdown
        assert sum(row.question_count for row in breakdown) == 18
        assert sum(row.total_marks for row in breakdown) == 124

    types = report.question_types
    assert types.primary_assignments == 18
    assert types.unclassified_questions == 0
    assert types.label_assignments >= types.primary_assignments
    assert types.multi_label_questions > 0
    assert sum(row.question_count for row in types.primary_labels) == 18
    assert types.primary_assignments == sum(row.question_count for row in types.primary_labels)
    assert types.label_assignments == sum(row.question_count for row in types.all_labels)

    difficulty = report.difficulty
    assert difficulty.analysed_questions == 18
    assert difficulty.estimated is True
    assert difficulty.empirical_measurement_available is False
    assert sum(row.question_count for row in difficulty.labels) == 18

    trends = report.trends
    assert trends.status.value == "ok"
    assert trends.available is True
    assert [period.period for period in trends.periods] == [
        "2024-05",
        "2024-11",
        "2025-04",
    ]
    assert trends.first_period == "2024-05"
    assert trends.last_period == "2025-04"
    assert trends.known_sessions == 3
    assert all(period.question_count == 6 for period in trends.periods)
    assert all(period.document_count == 1 for period in trends.periods)
    assert all(len(period.document_ids) == 1 for period in trends.periods)
    assert trends.new_topics
    assert all(item.period != "2024-05" for item in trends.new_topics)
    assert any(change.metric == "question_count" for change in trends.changes)

    priority = report.topic_priority
    assert priority.topics
    assert priority.recency_available is True
    assert priority.coverage_available is True
    assert priority.marks_available is True
    assert priority.topics[0].rank == 1
    assert 0.0 <= priority.topics[0].score <= 1.0
    assert priority.topics[0].factors["coverage"] is not None
    assert all(row.document_ids for row in priority.topics)
    assert any("historical prominence" in note.casefold() for note in priority.notes)

    assert report.filters == {}
    assert report.limitations
    assert not any(warning.code == "missing_analysis" for warning in report.warnings)


def test_topic_priority_formula_and_factors_are_reproducible(database) -> None:
    _ingest_and_analyse(database)
    report = AnalyticsService(database).analyze_course(COURSE)
    priority = report.topic_priority
    rows = priority.topics
    periods = [period.period for period in report.trends.periods]
    known_sessions = report.data_quality.known_sessions
    max_papers = max(row.paper_count for row in rows)
    max_average_marks = max(row.average_marks for row in rows if row.average_marks is not None)

    assert known_sessions > 0
    assert max_papers > 0
    assert max_average_marks > 0
    for row in rows:
        assert row.frequency_basis == "question_papers"
        assert row.factors["frequency"] == round(row.paper_count / max_papers, 4)
        assert row.factors["coverage"] == round(row.session_count / known_sessions, 4)
        if row.average_marks is not None:
            assert abs(row.factors["marks"] - row.average_marks / max_average_marks) <= 1e-3
        if row.latest_period is not None:
            index = periods.index(row.latest_period)
            assert row.factors["recency"] == round(index / (len(periods) - 1), 4)

        available = {name for name, factor in row.factors.items() if factor is not None}
        assert set(row.weights_used) == available
        weight_sum = sum(priority.weights[name] for name in available)
        expected_score = (
            sum(
                row.factors[name] * priority.weights[name]
                for name in available
                if row.factors[name] is not None
            )
            / weight_sum
        )
        assert row.score == round(expected_score, 4)


def test_trends_and_missing_marks_are_not_inferred(database) -> None:
    _ingest_and_analyse(database, SAMPLE_MANIFEST)
    report = AnalyticsService(database).analyze_course("demo-cs201-2024-v1")

    assert report.data_quality.questions == 15
    assert report.data_quality.question_banks == 1
    assert report.marks.missing_marks == 1
    assert report.marks.questions_with_marks == 14
    assert sum(report.marks.marks_sources.values()) == 14
    assert report.marks.total_marks == 100
    assert report.trends.status.value == "insufficient_data"
    assert report.trends.available is False
    assert len(report.trends.periods) == 1
    assert any(warning.code == "insufficient_periods" for warning in report.trends.warnings)
    assert any(warning.code == "insufficient_data" for warning in report.marks.warnings)


def test_question_bank_only_view_uses_document_frequency(database) -> None:
    _ingest_and_analyse(database, SAMPLE_MANIFEST)
    with database.session() as session:
        bank = next(
            document
            for document in DocumentRepository(session).list_by_course("demo-cs201-2024-v1")
            if document.kind is DocumentKind.QUESTION_BANK
        )
        bank_id = bank.document_id

    report = AnalyticsService(database).analyze_course(
        "demo-cs201-2024-v1", AnalyticsFilters(document_ids=[bank_id])
    )
    assert report.data_quality.questions > 0
    assert report.data_quality.question_banks == 1
    assert report.data_quality.question_papers == 0
    assert report.topic_priority.topics
    assert all(row.frequency_basis == "documents" for row in report.topic_priority.topics)
    assert all(row.paper_count == 0 for row in report.topic_priority.topics)


def test_filters_apply_to_question_metrics_and_reject_course_mismatch(database) -> None:
    _ingest_and_analyse(database)
    service = AnalyticsService(database)

    final = service.analyze_course(COURSE, AnalyticsFilters(exam_types=["final"]))
    assert final.data_quality.questions == 6
    assert [period.period for period in final.trends.periods] == ["2024-11"]
    assert final.trends.status.value == "insufficient_data"
    assert all(row.exam_type == "final" for row in final.repetition.document_frequency)

    period = service.analyze_course(COURSE, AnalyticsFilters(periods=["2025-04"]))
    assert period.data_quality.questions == 6
    assert period.trends.periods[0].exam_types == ["midterm"]

    marked = service.analyze_course(COURSE, AnalyticsFilters(marks_min=8))
    assert marked.data_quality.questions > 0
    assert all(row.marks >= 8 for row in marked.marks.question_marks)

    typed = service.analyze_course(COURSE, AnalyticsFilters(question_types=[QuestionType.PROOF]))
    assert typed.data_quality.questions > 0
    assert all(row.label == "proof" for row in typed.question_types.primary_labels)

    topic = next(
        row.topic
        for row in service.analyze_course(COURSE).mapping.topics
        if row.topic is not None and row.question_count > 0
    )
    topic_filtered = service.analyze_course(COURSE, AnalyticsFilters(topics=[topic]))
    assert 0 < topic_filtered.data_quality.questions < 18

    easy = service.analyze_course(COURSE, AnalyticsFilters(difficulties=[DifficultyLabel.EASY]))
    assert easy.data_quality.questions > 0
    assert all(row.label == "easy" for row in easy.difficulty.labels)

    sourced = service.analyze_course(
        COURSE, AnalyticsFilters(source_ids=["demo-analytics-synthetic"])
    )
    assert sourced.data_quality.questions == 18
    absent_source = service.analyze_course(COURSE, AnalyticsFilters(source_ids=["not-present"]))
    assert absent_source.data_quality.questions == 0

    unit_id = next(
        row.unit_id
        for row in service.analyze_course(COURSE).mapping.units
        if row.question_count > 0
    )
    unit_filtered = service.analyze_course(COURSE, AnalyticsFilters(unit_ids=[unit_id]))
    assert 0 < unit_filtered.data_quality.questions < 18

    with pytest.raises(ValueError, match="does not match"):
        service.analyze(
            COURSE,
            AnalyticsFilters(institution_code="another-institution"),
        )
    with pytest.raises(ValueError, match="does not match"):
        service.analyze_course(
            COURSE,
            AnalyticsFilters(syllabus_version="not-this-version"),
        )


def test_multiple_courses_are_reported_separately(database) -> None:
    _ingest_and_analyse(database, SAMPLE_MANIFEST)
    batch = IngestionPipeline(database, settings=Settings()).ingest_manifest(ANALYTICS_MANIFEST)
    assert batch.ok
    UnderstandingService(database, UnderstandingSettings()).analyze()

    reports = AnalyticsService(database).analyze()
    assert {report.course.course_id for report in reports} == {
        "demo-cs201-2024-v1",
        "demo-phy101-2024-v1",
        COURSE,
    }
    assert all(report.data_quality.questions > 0 for report in reports)
    assert len({report.course.course_id for report in reports}) == len(reports)

    filtered = AnalyticsService(database).analyze(
        None, AnalyticsFilters(institution_code="not-present")
    )
    assert filtered == []


def test_invalid_exam_date_falls_back_without_faking_a_period(database) -> None:
    _seed_unanalysed_question(
        database,
        course_id="invalid-date-course",
        academic_year="2024-25",
        metadata={"exam_date": "not-a-date"},
    )
    report = AnalyticsService(database).analyze_course("invalid-date-course")
    assert report.data_quality.known_periods == 1
    assert report.trends.periods[0].source == "academic_year"
    assert report.trends.periods[0].period == "2024-25"
    assert any(warning.code == "missing_period_metadata" for warning in report.warnings)


def test_marks_trend_reliability_uses_the_marks_sample(database) -> None:
    _ingest_and_analyse(database)
    with database.transaction() as session:
        for document in DocumentRepository(session).list_by_course(COURSE):
            if document.kind is not DocumentKind.QUESTION_PAPER:
                continue
            questions = QuestionRepository(session).list_by_document(document.document_id)
            updated = [
                question if index == 0 else question.model_copy(update={"marks": None})
                for index, question in enumerate(questions)
            ]
            QuestionRepository(session).replace_for_document(document.document_id, updated)

    report = AnalyticsService(database, AnalyticsSettings(min_period_questions=2)).analyze_course(
        COURSE
    )
    question_changes = [
        change for change in report.trends.changes if change.metric == "question_count"
    ]
    marks_changes = [change for change in report.trends.changes if change.metric == "total_marks"]
    assert question_changes and all(change.reliable for change in question_changes)
    assert marks_changes
    assert all(
        change.previous_sample == 1 and change.current_sample == 1 for change in marks_changes
    )
    assert all(not change.reliable for change in marks_changes)


def test_trends_respect_minimum_period_and_sample_settings(database) -> None:
    _ingest_and_analyse(database)
    service = AnalyticsService(database, AnalyticsSettings(min_trend_periods=4))
    report = service.analyze_course(COURSE)
    assert report.trends.status.value == "insufficient_data"
    assert report.trends.available is False
    assert report.trends.changes == []
    assert report.trends.new_topics == []

    low_sample = AnalyticsService(
        database, AnalyticsSettings(min_period_questions=20)
    ).analyze_course(COURSE)
    assert low_sample.trends.status.value == "low_sample"
    assert low_sample.trends.available is True
    assert low_sample.trends.changes
    assert all(not change.reliable for change in low_sample.trends.changes)


def test_missing_analysis_and_metadata_do_not_crash(database) -> None:
    _ingest_and_analyse(database)
    # A second course deliberately has no Phase 3 rows.
    _seed_unanalysed_question(database)
    report = AnalyticsService(database).analyze_course("test-metadata-2024-v1")

    assert report.data_quality.questions == 1
    assert report.data_quality.analysed_questions == 0
    assert report.data_quality.analysis_coverage == 0
    assert report.mapping.not_analysed_questions == 1
    assert report.question_types.unclassified_questions == 1
    assert report.difficulty.not_analysed_questions == 1
    assert report.trends.status.value == "no_period_metadata"
    assert report.trends.available is False
    assert report.marks.total_marks == 0
    assert report.marks.mean_marks is None
    assert report.marks.missing_marks == 1
    assert any(warning.code == "missing_analysis" for warning in report.warnings)


def test_empty_database_and_course_are_explicit(database) -> None:
    service = AnalyticsService(database)
    assert service.analyze() == []
    with pytest.raises(KeyError, match="unknown course"):
        service.analyze_course("not-a-course")

    with database.transaction() as session:
        CourseRepository(session).upsert(make_course(course_id="empty-course", academic_year=None))
    empty = service.analyze_course("empty-course")
    assert empty.data_quality.questions == 0
    assert empty.marks.total_marks == 0
    assert empty.trends.status.value == "no_period_metadata"
    assert any(warning.code == "insufficient_data" for warning in empty.warnings)


def test_marks_mismatch_is_reported_from_analysis_evidence(database) -> None:
    _ingest_and_analyse(database)
    with database.transaction() as session:
        questions = QuestionRepository(session).list_by_course(COURSE)
        target = next(
            question for question in questions if question.marks is not None and question.marks == 5
        )
        repo = QuestionUnderstandingRepository(session)
        analysis = repo.get_analysis(target.question_id)
        difficulty = repo.get_difficulty(target.question_id)
        assert analysis is not None and difficulty is not None
        updated = analysis.model_copy(
            update={
                "subparts": [
                    Subpart(
                        label="a",
                        text=target.text,
                        start=0,
                        end=len(target.text),
                        marks=2.0,
                    )
                ]
            }
        )
        repo.replace_for_question(
            analysis=updated,
            matches=repo.list_unit_matches(target.question_id),
            types=repo.list_types(target.question_id),
            bloom=repo.list_bloom(target.question_id),
            difficulty=difficulty,
        )
    report = AnalyticsService(database).analyze_course(COURSE)
    assert len(report.marks.subpart_mismatches) == 1
    assert report.marks.subpart_mismatches[0].question_id == target.question_id
    assert report.marks.subpart_mismatches[0].detail["parent_marks"] == 5
    assert report.marks.subpart_mismatches[0].detail["subpart_marks"] == 2


def test_marks_anomalies_do_not_require_phase3_analysis(database) -> None:
    _seed_unanalysed_question(
        database,
        course_id="marks-anomaly-course",
        marks=5,
        uncertainties=[
            Uncertainty(
                code="multiple_marks_values",
                message="more than one marks value was found",
                detail={"values": [5, 8]},
            )
        ],
    )
    report = AnalyticsService(database).analyze_course("marks-anomaly-course")

    assert report.data_quality.analysed_questions == 0
    assert len(report.marks.anomalies) == 1
    assert report.marks.anomalies[0].code == "multiple_marks_values"
    assert report.marks.anomalies[0].question_id == "q_metadata_fixture"
    assert any(warning.code == "inconsistent_marks_metadata" for warning in report.marks.warnings)


def test_trends_warn_when_one_period_mixes_exam_types(database) -> None:
    _seed_unanalysed_question(
        database,
        course_id="mixed-type-course",
        document_id="doc_mixed_midterm",
        question_id="q_mixed_midterm",
        text="Define a stack.",
        metadata={"exam_session": "2024-05", "exam_type": "midterm"},
    )
    _seed_unanalysed_question(
        database,
        course_id="mixed-type-course",
        document_id="doc_mixed_final",
        question_id="q_mixed_final",
        text="Define a queue.",
        metadata={"exam_session": "2024-05", "exam_type": "final"},
    )
    report = AnalyticsService(database).analyze_course("mixed-type-course")

    assert len(report.trends.periods) == 1
    assert report.trends.periods[0].period == "2024-05"
    assert report.trends.periods[0].exam_types == ["final", "midterm"]
    assert report.trends.known_sessions == 2
    assert any(warning.code == "mixed_exam_types" for warning in report.trends.warnings)


def test_priority_recency_works_for_academic_year_periods(database) -> None:
    _ingest_and_analyse(database)
    with database.transaction() as session:
        papers = [
            document
            for document in DocumentRepository(session).list_by_course(COURSE)
            if document.kind is DocumentKind.QUESTION_PAPER
        ]
        papers.sort(key=lambda document: document.document_id)
        assert len(papers) == 3
        for index, document in enumerate(papers):
            # Strip the explicit session/date metadata and use two coarse
            # academic-year periods. A known period without a session must still
            # contribute to recency.
            updated = document.model_copy(
                update={
                    "academic_year": "2023-24" if index < 2 else "2024-25",
                    "metadata": {},
                }
            )
            DocumentRepository(session).upsert(updated)

    report = AnalyticsService(database).analyze_course(COURSE)
    assert report.trends.period_sources == ["academic_year"]
    assert report.trends.known_sessions == 0
    assert report.data_quality.known_sessions == 0
    assert report.topic_priority.recency_available is True
    assert report.topic_priority.coverage_available is False
    assert all(row.factors["coverage"] is None for row in report.topic_priority.topics)
    assert any(row.factors["recency"] is not None for row in report.topic_priority.topics)
    assert all(
        row.latest_period is not None
        for row in report.topic_priority.topics
        if row.factors["recency"] is not None
    )


def test_exact_repetition_within_one_document_is_not_cross_paper(database) -> None:
    _seed_unanalysed_question(
        database,
        course_id="within-document-course",
        document_id="doc_within_document",
        question_id="q_within_a",
        text="State the time complexity of binary search.",
    )
    with database.transaction() as session:
        first = QuestionRepository(session).get("q_within_a")
        assert first is not None
        second = first.model_copy(
            update={
                "question_id": "q_within_b",
                "question_number": "2",
                "number_sort": 2,
            }
        )
        QuestionRepository(session).replace_for_document("doc_within_document", [first, second])

    report = AnalyticsService(database).analyze_course("within-document-course")
    assert report.data_quality.questions == 2
    assert report.data_quality.distinct_question_texts == 1
    assert report.data_quality.exact_duplicate_appearances == 2
    group = report.repetition.exact_groups[0]
    assert group.question_count == 2
    assert group.document_count == 1
    assert group.within_document is True
    assert group.across_documents is False
    assert group.across_sessions is False
    assert report.repetition.within_document_groups == 1
    assert report.repetition.across_document_groups == 0


def test_repeated_documents_and_questions_are_reported_not_deleted(database) -> None:
    with database.transaction() as session:
        CourseRepository(session).upsert(
            make_course(course_id="duplicate-docs", academic_year=None)
        )
    _seed_unanalysed_question(
        database,
        course_id="duplicate-docs",
        document_id="doc_duplicate_a",
        question_id="q_duplicate_a",
        text="Explain the repeated text.",
        text_fingerprint="same-text",
    )
    _seed_unanalysed_question(
        database,
        course_id="duplicate-docs",
        document_id="doc_duplicate_b",
        question_id="q_duplicate_b",
        text="Explain the repeated text.",
        text_fingerprint="same-text",
    )
    report = AnalyticsService(database).analyze_course("duplicate-docs")
    assert report.data_quality.questions == 2
    assert report.data_quality.distinct_question_texts == 1
    assert report.data_quality.duplicate_document_text_groups == 1
    assert report.data_quality.duplicate_document_text_documents == 2
    assert report.repetition.exact_group_count == 1
    assert report.repetition.exact_groups[0].across_documents is True
    duplicate_warning = next(
        warning for warning in report.warnings if warning.code == "duplicate_document_text"
    )
    assert duplicate_warning.detail["document_ids"] == ["doc_duplicate_a", "doc_duplicate_b"]


def test_report_is_deterministic_except_for_generation_time(database) -> None:
    _ingest_and_analyse(database)
    service = AnalyticsService(database)
    first = service.analyze_course(COURSE).model_dump(mode="json")
    second = service.analyze_course(COURSE).model_dump(mode="json")
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_zero_priority_weights_are_explicitly_not_meaningful(database) -> None:
    _ingest_and_analyse(database)
    report = AnalyticsService(
        database,
        AnalyticsSettings(
            priority_frequency_weight=0.0,
            priority_recency_weight=0.0,
            priority_marks_weight=0.0,
            priority_coverage_weight=0.0,
            priority_confidence_weight=0.0,
        ),
    ).analyze_course(COURSE)

    assert report.topic_priority.topics
    assert all(row.score == 0.0 for row in report.topic_priority.topics)
    assert all(not row.weights_used for row in report.topic_priority.topics)
    assert any(
        warning.code == "priority_weights_unavailable" for warning in report.topic_priority.warnings
    )


def test_similarity_threshold_retains_exact_groups(database) -> None:
    _ingest_and_analyse(database)
    strict = AnalyticsService(
        database, AnalyticsSettings(similarity_threshold=0.99)
    ).analyze_course(COURSE)
    assert strict.repetition.exact_group_count == 4
    assert all(pair.score >= 0.99 for pair in strict.repetition.similarity_pairs)
    assert strict.repetition.similarity_threshold == 0.99


def test_analytics_recalculate_after_question_change(database) -> None:
    _ingest_and_analyse(database)
    service = AnalyticsService(database)
    before = service.analyze_course(COURSE)
    with database.transaction() as session:
        question = next(
            item for item in QuestionRepository(session).list_by_course(COURSE) if item.marks == 7
        )
        questions = QuestionRepository(session).list_by_document(question.document_id)
        updated = [
            item.model_copy(update={"marks": 9})
            if item.question_id == question.question_id
            else item
            for item in questions
        ]
        QuestionRepository(session).replace_for_document(question.document_id, updated)
    after = service.analyze_course(COURSE)
    assert after.marks.total_marks == before.marks.total_marks + 2
    assert after.data_quality.questions == before.data_quality.questions
    assert after.data_quality.analysed_questions < before.data_quality.analysed_questions
