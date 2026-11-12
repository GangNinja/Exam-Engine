from __future__ import annotations

import csv
from pathlib import Path

from exam_engine.config import Settings
from exam_engine.dashboard.api import DashboardAPI
from exam_engine.models.recovery import AnswerCandidate, make_record_id
from exam_engine.recovery.service import DatasetRecoveryService
from exam_engine.storage.db import memory_database, open_database
from exam_engine.storage.recovery_repository import RecoveryRepository

FIELDS = [
    "question_id",
    "original_question_number",
    "question_text",
    "question_type",
    "options",
    "marks",
    "answer_id",
    "answer_text",
    "answer_type",
    "solution_steps",
]


def _write(path: Path, rows: list[dict[str, str]], fields: list[str] | None = None) -> Path:
    fields = fields or FIELDS
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(**changes: str) -> dict[str, str]:
    value = {field: "" for field in FIELDS}
    value.update(
        {
            "question_id": "Q1",
            "original_question_number": "1",
            "question_text": "What is the capital of India? A. Mumbai B. New Delhi C. Chennai D. Kolkata",
            "question_type": "MCQ",
            "options": "A. Mumbai B. New Delhi C. Chennai D. Kolkata",
            "marks": "1",
            "answer_text": "B. New Delhi",
            "answer_type": "MCQ option",
        }
    )
    value.update(changes)
    return value


def _analysis(tmp_path: Path, rows: list[dict[str, str]], **kwargs):
    path = _write(tmp_path / "course.csv", rows)
    database = memory_database()
    try:
        service = DatasetRecoveryService(database, Settings.from_env(), **kwargs)
        run = service.analyze(
            tmp_path,
            dry_run=True,
            csv_path=path,
            course_id="custom-course",
            course_name="Custom Course",
            course_code="CUSTOM",
        )
        return run
    finally:
        database.close()


def test_custom_schema_and_embedded_option_repair(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "custom.csv",
        [
            {
                "id": "X1",
                "question": "What is the capital of India? A. Mumbai B. New Delhi C. Chennai D. Kolkata",
                "choices": "A. Mumbai B. New Delhi C. Chennai D. Kolkata",
                "key": "B",
                "score": "1",
            }
        ],
        fields=["id", "question", "choices", "key", "score"],
    )
    database = memory_database()
    try:
        run = DatasetRecoveryService(database, Settings.from_env()).analyze(
            tmp_path,
            dry_run=True,
            csv_path=path,
            course_id="custom-schema",
            course_name="Alias Schema",
        )
    finally:
        database.close()

    course = run.courses[0]
    record = course.records[0]
    assert course.schema["standard"] is False
    assert record.repaired["question_text"] == "What is the capital of India?"
    assert [item["key"] for item in record.repaired["options"]] == ["A", "B", "C", "D"]
    assert record.answer_status == "structured_extraction"
    assert any(item.repair_type == "remove_duplicated_embedded_options" for item in record.repairs)
    assert record.source_fidelity == "unverifiable"


def test_passage_and_child_questions_are_reconstructed_not_merged(tmp_path: Path) -> None:
    run = _analysis(
        tmp_path,
        [
            _row(
                question_id="P1",
                question_text=(
                    "Read the following passage and answer the questions. "
                    "The sun is a star. It provides energy to the earth and supports life. "
                    "Plants use sunlight to make food.\n"
                    "1. What is the sun?\n"
                    "2. What does the sun provide?"
                ),
                options="A. Star B. Planet C. Moon D. Comet",
                answer_text="",
            )
        ],
    )
    course = run.courses[0]
    parent = next(item for item in course.records if item.source_question_id == "P1")
    children = [item for item in course.records if item.structure.get("child_question")]
    assert parent.repaired["question_type"] == "passage_based"
    assert len(parent.structure["child_questions"]) == 2
    assert len(children) == 2
    assert all(item.release_status == "quarantined" for item in children)
    assert parent.quality["components"]["passage_subquestion_integrity"] < 100


def test_local_duplicate_recovers_answer_and_conflicts_are_quarantined(tmp_path: Path) -> None:
    run = _analysis(
        tmp_path,
        [
            _row(question_id="Q1", answer_text="B. New Delhi"),
            _row(question_id="Q2", answer_text=""),
        ],
    )
    course = run.courses[0]
    recovered = next(item for item in course.records if item.source_question_id == "Q2")
    assert recovered.answer_status == "local_duplicate"
    assert recovered.repaired["answer_text"] == "B. New Delhi"
    assert recovered.release_status == "review_required"
    assert course.counts["recovered_answers"] == 1
    assert course.counts["verified_recoveries"] == 0
    assert course.counts["unverified_recoveries"] == 1
    assert run.totals["recovered_answers"] == 1
    assert run.totals["verified_recoveries"] == 0

    conflicted_run = _analysis(
        tmp_path,
        [
            _row(question_id="Q1", answer_text="B. New Delhi"),
            _row(question_id="Q2", answer_text="A. Mumbai"),
            _row(question_id="Q3", answer_text=""),
        ],
    )
    conflicted = next(
        item for item in conflicted_run.courses[0].records if item.source_question_id == "Q3"
    )
    assert conflicted.answer_status == "conflicting"
    assert conflicted.release_status == "quarantined"
    assert any(issue.code == "answer.conflicting_local_evidence" for issue in conflicted.issues)


class _StubSolver:
    available = True

    def solve(self, *, question_text, options, course_id, source_reference):
        return AnswerCandidate(
            answer_text="AI candidate",
            source_type="ai_model",
            source_name="stub",
            verification_status="ai_generated_unverified",
            confidence=0.5,
            method="stub_solution",
            source_reference=source_reference,
            evidence={"note": "not official"},
            retrieved_at="2026-01-01T00:00:00+00:00",
            official=False,
        )


def test_ai_candidate_is_never_official_or_released(tmp_path: Path) -> None:
    path = _write(tmp_path / "ai.csv", [_row(question_id="Q1", answer_text="")])
    database = memory_database()
    try:
        run = DatasetRecoveryService(
            database, Settings.from_env(), model_solver=_StubSolver()
        ).analyze(
            tmp_path,
            dry_run=True,
            csv_path=path,
            course_id="custom-ai",
            use_model=True,
            model_limit=1,
        )
    finally:
        database.close()
    record = run.courses[0].records[0]
    assert record.answer_status == "ai_generated_unverified"
    assert record.release_status != "released"
    assert all(not item.official for item in record.answer_candidates)
    assert "official_verified" not in run.totals["by_answer_status"]


def test_persistence_review_and_rollback(tmp_path: Path) -> None:
    path = _write(tmp_path / "persist.csv", [_row(question_id="Q1")])
    database = memory_database()
    try:
        run = DatasetRecoveryService(database, Settings.from_env()).analyze(
            tmp_path,
            dry_run=False,
            csv_path=path,
            course_id="custom-persist",
        )
        with database.session() as session:
            repo = RecoveryRepository(session)
            overview = repo.overview(run.run_id)
            detail = repo.course_detail("custom-persist", run_id=run.run_id)
            assert overview["records"] == 1
            assert detail is not None
            record_id = detail["records"][0]["record_id"]
            record = repo.record_detail(record_id, run_id=run.run_id)
            assert record is not None
            repair_id = record["repairs"][0]["repair_id"]
        with database.transaction() as session:
            reviewed = RecoveryRepository(session).review_repair(
                repair_id, status="approved", reviewer="test", note="checked"
            )
        assert reviewed is not None
        assert reviewed["repairs"][0]["status"] == "approved"
        with database.transaction() as session:
            assert RecoveryRepository(session).rollback(run.run_id) is True
        with database.session() as session:
            assert RecoveryRepository(session).get_run(run.run_id)["status"] == "rolled_back"
    finally:
        database.close()


def test_recovery_api_reads_persisted_evidence(tmp_path: Path) -> None:
    path = _write(tmp_path / "api.csv", [_row(question_id="Q1")])
    db_path = tmp_path / "api.db"
    database = open_database(db_path)
    try:
        run = DatasetRecoveryService(database, Settings.from_env()).analyze(
            tmp_path,
            dry_run=False,
            csv_path=path,
            course_id="custom-api",
        )
    finally:
        database.close()
    api = DashboardAPI(db_path, Settings.from_env())
    try:
        overview = api.dispatch("GET", "/api/recovery")
        assert overview.status == 200
        assert overview.payload["records"] == 1
        totals = overview.payload["totals"]
        assert totals["recovered_answers"] == 0
        assert totals["verified_recoveries"] == 0
        assert totals["unverified_recoveries"] == 0
        courses = api.dispatch("GET", "/api/recovery/courses")
        assert courses.status == 200
        assert courses.payload["total"] == 1
        detail = api.dispatch("GET", "/api/recovery/courses/custom-api")
        assert detail.status == 200
        assert detail.payload["records_total"] == 1
        record_id = detail.payload["records"][0]["record_id"]
        record = api.dispatch("GET", f"/api/recovery/records/{record_id}")
        assert record.status == 200
        assert record.payload["record_id"] == record_id
    finally:
        api.close()


def test_resume_index_keeps_completed_course_answers(tmp_path: Path) -> None:
    path_a = _write(tmp_path / "a.csv", [_row(question_id="A1")])
    path_b = _write(
        tmp_path / "b.csv",
        [_row(question_id="B1", answer_text="", answer_type="")],
    )
    database = memory_database()
    try:
        service = DatasetRecoveryService(database, Settings.from_env())
        service.analyze(
            tmp_path,
            dry_run=False,
            csv_path=path_a,
            course_id="custom-resume-a",
            course_name="Resume A",
            course_code="RA",
            run_id="resume-index-test",
        )
        resumed = service.analyze(
            tmp_path,
            dry_run=False,
            csv_path=path_b,
            course_id="custom-resume-b",
            course_name="Resume B",
            course_code="RB",
            resume_run_id="resume-index-test",
        )
    finally:
        database.close()

    record = resumed.courses[0].records[0]
    assert record.repaired["answer_text"]
    assert record.answer_status in {"corroborated_local", "local_duplicate"}
    assert any(
        candidate.source_type == "local_duplicate"
        for candidate in record.answer_candidates
    )
