from __future__ import annotations

import csv
from pathlib import Path

from exam_engine.config import NlpSettings, Settings, UnderstandingSettings
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.enums import (
    DocumentKind,
    MappingStatus,
    SimilarityKind,
    UnderstandingStatus,
)
from exam_engine.nlp.service import NlpService
from exam_engine.storage.question_repositories import (
    QuestionUnderstandingRepository,
    UnderstandingRunRepository,
)
from exam_engine.storage.repositories import DocumentRepository, QuestionRepository
from exam_engine.storage.schema import QuestionRow
from exam_engine.understanding.service import UnderstandingService
from tests.support import make_course, make_source

SYLLABUS = """\
Unit 1: Algorithm Analysis
Asymptotic notation; time complexity; recurrence relations; worst case analysis.

Unit 2: Linear Structures
Arrays; linked lists; stacks; queues; LIFO behaviour.

Unit 3: Trees
Binary trees; binary search trees; tree traversal; heaps.

Unit 4: Graphs
Graph traversal; breadth-first search; adjacency matrix; adjacency list.

Unit 5: Sorting and Hashing
Comparison sorting; merge sort; quicksort; lower bounds; hash tables; collisions.
"""

ROWS = [
    {
        "number": "1",
        "text": "Define a stack and describe its last-in-first-out behaviour.",
        "marks": "5",
        "unit": "Unit 2",
    },
    {
        "number": "2",
        "text": "Compare arrays and linked lists for insertion in the middle.",
        "marks": "5",
    },
    {
        "number": "3",
        "text": "Explain the time complexity of binary search in the worst case.",
        "marks": "4",
    },
    {
        "number": "4",
        "text": "Derive the recurrence relation for merge sort and solve it.",
        "marks": "10",
    },
    {
        "number": "5",
        "text": "Describe breadth-first graph traversal and give one example.",
        "marks": "5",
    },
    {
        "number": "6",
        "text": "Prove that comparison sorting has a lower bound of O(n log n).",
        "marks": "8",
        "unit": "Unit 5",
    },
    {
        "number": "7",
        "text": "Design a hash table that handles collisions and evaluate its trade-offs.",
        "marks": "10",
    },
    {
        "number": "",
        "text": "Discuss the role of asymptotic notation in algorithm analysis.",
        "marks": "",
    },
    {
        "number": "9",
        "text": "Write pseudocode to detect a cycle in a singly linked list.",
        "marks": "7",
        "unit": "Unit 2",
    },
    {
        "number": "10",
        "text": (
            "Consider the following case study: a company stores records in a hash table. "
            "(a) Explain the collision strategy. (b) Evaluate its impact on lookup time."
        ),
        "marks": "10",
    },
    {
        "number": "11",
        "text": "Explain why comparison sorting cannot beat O(n log n) in the worst case.",
        "marks": "6",
    },
    {
        "number": "12",
        "text": "Define a stack and describe its last-in-first-out behaviour.",
        "marks": "5",
        "unit": "Unit 2",
    },
    {
        "number": "13",
        "text": "Define a stack and describe its last-in first-out behaviour.",
        "marks": "5",
        "unit": "Unit 2",
    },
    {
        "number": "14",
        "text": "Calculate the maximum height of a binary tree with 15 nodes.",
        "marks": "4",
    },
    {
        "number": "15",
        "text": "Analyze the trade-offs of an adjacency matrix versus an adjacency list.",
        "marks": "8",
    },
    {
        "number": "16",
        "text": "Zzz qqq.",
        "marks": "",
    },
]

PHYSICS_SYLLABUS = """\
Unit 1: Mechanics
Newton's laws; motion; acceleration; force.

Unit 2: Waves
Oscillation; superposition; transverse waves; longitudinal waves.
"""

PHYSICS_ROWS = [
    {
        "number": "1",
        "text": "State Newton's three laws of motion.",
        "marks": "5",
    },
    {
        "number": "2",
        "text": "Explain superposition of travelling waves.",
        "marks": "6",
        "unit": "Unit 2",
    },
    {
        "number": "3",
        "text": "Explain the material from Unit 9 of another course.",
        "marks": "5",
        "unit": "Unit 9",
    },
]


def _write_questions(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["number", "text", "marks", "unit"], extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def seed(
    database,
    tmp_path: Path,
    course_id: str = "test-course-2024-v1",
    *,
    syllabus: str | None = SYLLABUS,
    rows: list[dict[str, str]] | None = ROWS,
    rows_name: str = "questions.csv",
) -> None:
    course = make_course(course_id=course_id)
    source = make_source()
    pipeline = IngestionPipeline(database, settings=Settings())
    if syllabus is not None:
        path = tmp_path / f"{course_id}-syllabus.txt"
        path.write_text(syllabus, encoding="utf-8")
        result = pipeline.ingest_file(
            path, course=course, source=source, kind=DocumentKind.SYLLABUS
        )
        assert result.document is not None, result.error_codes()
    if rows is not None:
        path = _write_questions(tmp_path / f"{course_id}-{rows_name}", rows)
        result = pipeline.ingest_file(
            path, course=course, source=source, kind=DocumentKind.QUESTION_BANK
        )
        assert result.document is not None, result.error_codes()


def test_analysis_persists_structure_mapping_and_classification(database, tmp_path: Path) -> None:
    seed(database, tmp_path)
    report = UnderstandingService(database).analyze_course("test-course-2024-v1")

    assert report.status in {UnderstandingStatus.OK, UnderstandingStatus.PARTIAL}
    assert report.totals["questions"] == len(ROWS)
    assert report.totals["analysed"] == len(ROWS)
    assert report.totals["mapped"] > 0
    assert report.totals["type_labels"] > report.totals["analysed"]
    assert report.totals["bloom_labels"] > 0
    assert report.totals["similarities"] >= 1
    assert report.config["taxonomy_version"] == "builtin-1"

    with database.session() as session:
        question_repo = QuestionRepository(session)
        repo = QuestionUnderstandingRepository(session)
        questions = question_repo.list_by_course("test-course-2024-v1")
        assert len(questions) == len(ROWS)
        analyses = repo.list_analyses("test-course-2024-v1")
        assert len(analyses) == len(ROWS)
        assert all(analysis.mapping_status for analysis in analyses)
        assert all(analysis.difficulty_label.value for analysis in analyses)
        assert repo.count_similarities("test-course-2024-v1") == report.totals["similarities"]

        # Every stored match belongs to this course and its own syllabus.
        for analysis in analyses:
            for match in repo.list_unit_matches(analysis.question_id):
                assert match.course_id == "test-course-2024-v1"
                assert match.syllabus_id.startswith("syl_")

        exact = repo.list_similarities("test-course-2024-v1", kind=SimilarityKind.EXACT)
        assert exact, "the repeated question must be found as an exact duplicate"
        assert all(match.question_id != match.matched_question_id for match in exact)

        case_question = next(q for q in questions if q.text.startswith("Consider"))
        case_analysis = repo.get_analysis(case_question.question_id)
        assert case_analysis is not None
        assert case_analysis.subquestion_count == 2
        assert len(case_analysis.subparts) == 2

    stored_run = None
    with database.session() as session:
        stored_run = UnderstandingRunRepository(session).get(report.run_id)
    assert stored_run is not None
    assert stored_run.status is report.status


def test_repeat_run_replaces_rows_and_updates_changed_question(database, tmp_path: Path) -> None:
    seed(database, tmp_path)
    service = UnderstandingService(database)
    first = service.analyze_course("test-course-2024-v1")
    with database.session() as session:
        question = QuestionRepository(session).list_by_course("test-course-2024-v1")[0]
        before = QuestionUnderstandingRepository(session).get_analysis(question.question_id)
        assert before is not None
        first_count = QuestionUnderstandingRepository(session).count_analyses("test-course-2024-v1")

    with database.transaction() as session:
        row = session.get(QuestionRow, question.question_id)
        assert row is not None
        row.text = "Prove that the recurrence relation for merge sort is correct."
        row.question_number = "1"
        row.number_sort = 1

    second = service.analyze_course("test-course-2024-v1")
    assert second.totals["analysed"] == first.totals["analysed"]
    with database.session() as session:
        repo = QuestionUnderstandingRepository(session)
        assert repo.count_analyses("test-course-2024-v1") == first_count
        after = repo.get_analysis(question.question_id)
        assert after is not None
        assert after.text_sha256 != before.text_sha256
        assert after.primary_type is not None
        assert after.primary_type.value in {"proof", "explanation"}


def test_course_syllabus_isolation_and_unresolved_reference(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a-2024-v1")
    seed(
        database,
        tmp_path,
        "course-b-2024-v1",
        syllabus=PHYSICS_SYLLABUS,
        rows=PHYSICS_ROWS,
        rows_name="physics.csv",
    )
    service = UnderstandingService(database)
    service.analyze_course("course-a-2024-v1")
    report = service.analyze_course("course-b-2024-v1")

    with database.session() as session:
        repo = QuestionUnderstandingRepository(session)
        analyses = repo.list_analyses("course-b-2024-v1")
        assert analyses
        for analysis in analyses:
            assert analysis.course_id == "course-b-2024-v1"
            for match in repo.list_unit_matches(analysis.question_id):
                assert match.course_id == "course-b-2024-v1"
                assert match.syllabus_id.startswith("syl_")
                assert "course-a" not in match.syllabus_id

        unresolved = [
            analysis
            for analysis in analyses
            if any(
                item.code == "unmapped_question" and "Unit 9" in item.message
                for item in analysis.uncertainties
            )
        ]
        assert unresolved
        assert unresolved[0].mapping_status is MappingStatus.UNMAPPED
        assert repo.list_unit_matches(unresolved[0].question_id) == []

    assert report.totals["unmapped"] >= 1


def test_syllabus_versions_remain_separate(database, tmp_path: Path) -> None:
    seed(
        database,
        tmp_path,
        "same-code-2024-v1",
        syllabus="Unit 1: Calculus\nLimits; derivatives; integrals.\n",
        rows=[{"number": "1", "text": "Explain limits and derivatives.", "marks": "5"}],
        rows_name="v1.csv",
    )
    seed(
        database,
        tmp_path,
        "same-code-2024-v2",
        syllabus="Unit 1: Mechanics\nForce; motion; Newton laws.\n",
        rows=[{"number": "1", "text": "Explain force and motion.", "marks": "5"}],
        rows_name="v2.csv",
    )
    service = UnderstandingService(database)
    service.analyze_course("same-code-2024-v1")
    service.analyze_course("same-code-2024-v2")

    with database.session() as session:
        repo = QuestionUnderstandingRepository(session)
        matches_v1 = [
            match
            for analysis in repo.list_analyses("same-code-2024-v1")
            for match in repo.list_unit_matches(analysis.question_id)
        ]
        matches_v2 = [
            match
            for analysis in repo.list_analyses("same-code-2024-v2")
            for match in repo.list_unit_matches(analysis.question_id)
        ]
    assert matches_v1 and matches_v2
    assert {match.syllabus_id for match in matches_v1}.isdisjoint(
        {match.syllabus_id for match in matches_v2}
    )
    assert {match.course_id for match in matches_v1} == {"same-code-2024-v1"}
    assert {match.course_id for match in matches_v2} == {"same-code-2024-v2"}


def test_course_without_syllabus_still_classifies_but_does_not_map(
    database, tmp_path: Path
) -> None:
    seed(
        database,
        tmp_path,
        "no-syllabus-2024-v1",
        syllabus=None,
        rows=[
            {
                "number": "1",
                "text": "Explain the difference between arrays and linked lists.",
                "marks": "5",
            }
        ],
    )
    report = UnderstandingService(database).analyze_course("no-syllabus-2024-v1")
    assert any(item.code == "no_syllabus" for item in report.warnings)
    with database.session() as session:
        analysis = QuestionUnderstandingRepository(session).list_analyses("no-syllabus-2024-v1")[0]
        assert analysis.mapping_status is MappingStatus.UNMAPPED
        assert analysis.primary_type is not None
        assert (
            QuestionUnderstandingRepository(session).list_unit_matches(analysis.question_id) == []
        )


def test_course_without_questions_reports_insufficient_data(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "questions-empty-2024-v1", rows=None)
    report = UnderstandingService(database).analyze_course("questions-empty-2024-v1")
    assert report.status is UnderstandingStatus.INSUFFICIENT_DATA
    assert any(item.code == "insufficient_data" for item in report.errors)
    with database.session() as session:
        assert (
            QuestionUnderstandingRepository(session).count_analyses("questions-empty-2024-v1") == 0
        )


def test_one_question_failure_is_recorded_without_stopping_batch(
    database, tmp_path: Path, monkeypatch
) -> None:
    seed(database, tmp_path)
    import exam_engine.understanding.service as service_module

    original = service_module.classify_question_types

    def flaky(text, structure, taxonomy, **kwargs):
        if text.startswith("Design a hash table"):
            raise RuntimeError("simulated classifier failure")
        return original(text, structure, taxonomy, **kwargs)

    monkeypatch.setattr(service_module, "classify_question_types", flaky)
    report = UnderstandingService(database).analyze_course("test-course-2024-v1")
    assert report.status is UnderstandingStatus.PARTIAL
    assert any(item.code == "question_analysis_failed" for item in report.errors)
    with database.session() as session:
        repo = QuestionUnderstandingRepository(session)
        assert repo.count_analyses("test-course-2024-v1") == len(ROWS) - 1


def test_similarity_failure_keeps_classifications_and_reports_partial(
    database, tmp_path: Path, monkeypatch
) -> None:
    seed(database, tmp_path)
    import exam_engine.understanding.service as service_module

    def failing_similarity(*args, **kwargs):
        raise RuntimeError("simulated similarity model failure")

    monkeypatch.setattr(service_module, "find_similar_questions", failing_similarity)
    report = UnderstandingService(database).analyze_course("test-course-2024-v1")

    assert report.status is UnderstandingStatus.PARTIAL
    assert any(item.code == "model_failure" for item in report.errors)
    with database.session() as session:
        repo = QuestionUnderstandingRepository(session)
        assert repo.count_analyses("test-course-2024-v1") == len(ROWS)
        assert repo.count_similarities("test-course-2024-v1") == 0


def test_max_questions_limit_is_reported(database, tmp_path: Path) -> None:
    seed(database, tmp_path)
    report = UnderstandingService(database, UnderstandingSettings(max_questions=3)).analyze_course(
        "test-course-2024-v1"
    )
    assert report.totals["questions"] == len(ROWS)
    assert report.totals["analysed"] == 3
    assert any(item.code == "analysis_truncated" for item in report.warnings)


def test_nlp_embeddings_are_used_and_model_ids_are_recorded(database, tmp_path: Path) -> None:
    seed(database, tmp_path)
    nlp = NlpService(
        database,
        NlpSettings(embedding_provider="hashing", hashing_dim=64, top_k=10, topics_k=3),
    )
    nlp_report = nlp.analyze_course("test-course-2024-v1")
    assert nlp_report.totals["embeddings"] > 0

    report = UnderstandingService(database).analyze_course("test-course-2024-v1")
    assert report.model_ids
    assert report.model_ids[0].startswith("hashing-tfidf")
    with database.session() as session:
        analysis = QuestionUnderstandingRepository(session).list_analyses("test-course-2024-v1")[0]
        match = QuestionUnderstandingRepository(session).list_unit_matches(analysis.question_id)
    assert match
    assert any("semantic cosine" in evidence for item in match for evidence in item.evidence)


def test_reset_removes_phase3_rows_but_keeps_phase1_data(database, tmp_path: Path) -> None:
    seed(database, tmp_path)
    service = UnderstandingService(database)
    service.analyze_course("test-course-2024-v1")
    with database.session() as session:
        assert QuestionUnderstandingRepository(session).count_analyses("test-course-2024-v1") > 0
        document_count = len(DocumentRepository(session).list_by_course("test-course-2024-v1"))
        question_count = QuestionRepository(session).count_by_course("test-course-2024-v1")

    service.reset("test-course-2024-v1")
    with database.session() as session:
        assert QuestionUnderstandingRepository(session).count_analyses("test-course-2024-v1") == 0
        assert (
            len(DocumentRepository(session).list_by_course("test-course-2024-v1")) == document_count
        )
        assert QuestionRepository(session).count_by_course("test-course-2024-v1") == question_count


def test_invalid_custom_taxonomy_falls_back_and_is_reported(database, tmp_path: Path) -> None:
    seed(database, tmp_path)
    bad = tmp_path / "taxonomy.json"
    bad.write_text("{not json", encoding="utf-8")
    report = UnderstandingService(
        database, UnderstandingSettings(taxonomy_path=str(bad))
    ).analyze_course("test-course-2024-v1")

    assert report.config["taxonomy_version"] == "builtin-1"
    assert any(item.code == "degraded_fallback" for item in report.warnings)
    assert report.status is UnderstandingStatus.PARTIAL


def test_unknown_course_fails_cleanly(database) -> None:
    report = UnderstandingService(database).analyze_course("does-not-exist")
    assert report.status is UnderstandingStatus.FAILED
    assert any(item.code == "question_analysis_failed" for item in report.errors)
    assert "Traceback" not in " ".join(item.message for item in report.errors)


def test_status_reports_coverage_and_latest_run(database, tmp_path: Path) -> None:
    seed(database, tmp_path)
    report = UnderstandingService(database).analyze_course("test-course-2024-v1")
    rows = UnderstandingService(database).status()
    row = next(item for item in rows if item["course_id"] == "test-course-2024-v1")
    assert row["questions"] == len(ROWS)
    assert row["analyzed"] == report.totals["analysed"]
    assert row["mapped"] == report.totals["mapped"]
    assert row["last_run"] == report.run_id
    assert row["last_status"] == report.status.value
