from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from exam_engine.config import Settings
from exam_engine.dashboard.api import DashboardAPI
from exam_engine.datasets.passages import validate_passage_dataset
from exam_engine.datasets.queries import ExamDatasetQueries, ExamFilters
from exam_engine.datasets.service import ExamDatasetService
from exam_engine.storage.db import open_database

PASSAGE_ID = "psg_test_physics_passage"
Q1 = "GATE2025_PH_Q44"
Q2 = "GATE2025_PH_Q45"
Q3 = "GATE2025_PH_Q46"
PASSAGE_TEXT = (
    "A charged particle enters a uniform magnetic field.\n\n"
    "The field is perpendicular to the velocity of the particle. "
    "The particle follows a circular path whose radius is measured in the laboratory."
)

COLUMNS = [
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
    "passage_id",
    "subparts_json",
]


def _write_fixture(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "GATE" / "Physics.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        [
            Q1,
            "44",
            "What is the radius of the circular path?",
            "MCQ",
            "A) r B) 2r C) r/2 D) 4r",
            "2",
            Q1,
            "B) 2r",
            "MCQ option",
            "",
            PASSAGE_ID,
            json.dumps(["The radius is proportional to the measured value."]),
        ],
        [
            Q2,
            "45",
            "What is the direction of the magnetic force?",
            "MCQ",
            "A) Along velocity B) Opposite velocity C) Perpendicular to velocity D) Zero",
            "2",
            Q2,
            "",
            "",
            "",
            PASSAGE_ID,
            "",
        ],
        [
            Q3,
            "46",
            "Which independent question has no passage context?",
            "MCQ",
            "A) Yes B) No C) Maybe D) Not stated",
            "2",
            Q3,
            "A) Yes",
            "MCQ option",
            "",
            "",
            "",
        ],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    with (root / "passages.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "passage_id",
                "course_id",
                "passage_text",
                "source_reference",
                "child_question_ids",
                "confidence",
            ]
        )
        writer.writerow(
            [
                PASSAGE_ID,
                "gate-physics",
                PASSAGE_TEXT,
                "GATE 2025 Physics · Q44–Q45",
                f"{Q1};{Q2}",
                "0.98",
            ]
        )


def _call(api: DashboardAPI, path: str):
    parsed = urlsplit(path)
    return api.dispatch("GET", parsed.path, parse_qs(parsed.query, keep_blank_values=True), b"")


def test_passage_import_retrieval_api_and_frontend_model(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    _write_fixture(root)
    db_path = tmp_path / "exam.db"
    settings = Settings.from_env().with_db(db_path)
    database = open_database(db_path)
    try:
        service = ExamDatasetService(database, settings)
        service.import_dataset(root, output_dir=tmp_path / "out", with_syllabus=False)
        result = service.import_passages(root)
        assert result["passages"] == 1
        assert result["linked"] == 2
        assert result["subparts_updated"] == 0  # already stored by the CSV import
        assert result["missing"] == []

        with database.session() as session:
            queries = ExamDatasetQueries(session)
            listing = queries.questions(ExamFilters(course_id="gate-physics"), limit=10)
            by_source = {item["source_question_id"]: item for item in listing["questions"]}
            assert by_source[Q1]["passage"]["passage_id"] == PASSAGE_ID
            assert by_source[Q1]["passage"]["text"] == PASSAGE_TEXT
            assert by_source[Q1]["passage_order"] == 1
            assert by_source[Q1]["subparts"] == ["The radius is proportional to the measured value."]
            assert by_source[Q2]["passage_order"] == 2
            assert by_source[Q2]["answer"] is None
            assert by_source[Q3]["passage"] is None
            assert by_source[Q3]["passage_id"] is None

            passage = queries.passage(PASSAGE_ID)
            assert passage is not None
            assert passage["question_count"] == 2
            assert [item["source_question_id"] for item in passage["questions"]] == [Q1, Q2]
            assert passage["questions"][0]["options"] == [
                {"key": "A", "text": "r"},
                {"key": "B", "text": "2r"},
                {"key": "C", "text": "r/2"},
                {"key": "D", "text": "4r"},
            ]
            assert passage["questions"][0]["answer"]["answer_text"] == "B) 2r"
            assert passage["questions"][1]["answer"] is None
            assert passage["questions"][0]["source_file"].endswith("Physics.csv")

            individual = queries.question(by_source[Q1]["question_id"])
            assert individual is not None
            assert individual["passage"]["passage_id"] == PASSAGE_ID
            assert individual["question"]["source_question_id"] == Q1
    finally:
        database.close()

    validation = validate_passage_dataset(root)
    summary = validation.as_dict()["summary"]
    assert summary["passage_count"] == 1
    assert summary["passage_question_count"] == 2
    assert summary["valid_relationships"] == 2
    assert summary["orphan_questions"] == 0
    assert summary["unused_passages"] == 0
    assert summary["unanswered_passage_questions"] == 1
    assert summary["corrupted_or_incomplete_passages"] == 0

    api = DashboardAPI(db_path, settings)
    try:
        response = _call(api, f"/api/exams/passages/{PASSAGE_ID}")
        assert response.status == 200
        assert response.payload["passage_id"] == PASSAGE_ID
        assert [item["source_question_id"] for item in response.payload["questions"]] == [Q1, Q2]

        response = _call(api, f"/api/exams/questions/{by_source[Q1]['question_id']}")
        assert response.status == 200
        assert response.payload["passage"]["passage_id"] == PASSAGE_ID

        missing = _call(api, "/api/exams/passages/does-not-exist")
        assert missing.status == 404
        assert missing.payload["error"]["code"] == "unknown_passage"

        missing_question = _call(api, "/api/exams/questions/does-not-exist")
        assert missing_question.status == 404
        assert missing_question.payload["error"]["code"] == "unknown_question"
    finally:
        api.close()


def test_passage_validation_flags_missing_orphan_unused_and_malformed(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    (root / "GATE").mkdir(parents=True)
    with (root / "passages.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["passage_id", "course_id", "passage_text", "source_reference", "child_question_ids"]
        )
        writer.writerow(
            [
                "psg_missing",
                "gate-physics",
                "This passage has enough words to be structurally complete but its child row "
                "is absent from the source document file.",
                "GATE/Physics.csv",
                "UNKNOWN_Q",
            ]
        )
        writer.writerow(
            [
                "psg_short",
                "gate-physics",
                "too short",
                "GATE/Physics.csv",
                "NO_OPTIONS",
            ]
        )
        writer.writerow(
            [
                "psg_unused",
                "gate-physics",
                "This passage is deliberately complete but has no associated child question in the "
                "source document, so it must be reported rather than deleted.",
                "GATE/Physics.csv",
                "",
            ]
        )
    with (root / "GATE" / "Physics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "question_id",
                "original_question_number",
                "question_text",
                "question_type",
                "options",
                "answer_text",
                "passage_id",
                "subparts_json",
            ]
        )
        writer.writerow(
            [
                "ORPHAN_Q",
                "1",
                "Orphan question.",
                "MCQ",
                "",
                "",
                "psg_not_declared",
                "{not-json",
            ]
        )
        writer.writerow(
            [
                "NO_OPTIONS",
                "2",
                "Passage child with no options.",
                "MCQ",
                "",
                "",
                "psg_short",
                "{not-json",
            ]
        )

    summary = validate_passage_dataset(root).as_dict()["summary"]
    assert summary["passage_count"] == 3
    assert summary["passage_question_count"] == 2
    assert summary["orphan_questions"] == 1
    assert summary["unused_passages"] == 1
    assert summary["corrupted_or_incomplete_passages"] == 1
    assert summary["missing_or_invalid_options"] == 1
    assert summary["malformed_subquestions"] == 1
    assert summary["valid_relationships"] == 1
    assert summary["manual_review_records"] >= 3


def test_passage_validation_flags_page_artifacts_without_repair(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    (root / "GATE").mkdir(parents=True)
    with (root / "passages.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["passage_id", "course_id", "passage_text", "source_reference", "child_question_ids"]
        )
        writer.writerow(
            [
                "psg_artifact",
                "gate-physics",
                "This passage has enough words to be structurally complete but it also "
                "contains a page footer. Physics (PH) Page 24 of 39 Organizing Institute.",
                "GATE/Physics.csv",
                "Q_ARTIFACT",
            ]
        )
    with (root / "GATE" / "Physics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "question_id",
                "original_question_number",
                "question_text",
                "question_type",
                "options",
                "answer_text",
                "passage_id",
                "subparts_json",
            ]
        )
        writer.writerow(
            [
                "Q_ARTIFACT",
                "1",
                "A page question? Physics (PH) Page 24 of 39",
                "MCQ",
                "A) one B) two Page 24 of 39 Organizing Institute",
                "",
                "psg_artifact",
                "[]",
            ]
        )

    report = validate_passage_dataset(root).as_dict()
    summary = report["summary"]
    assert summary["corrupted_or_incomplete_passages"] == 1
    assert summary["missing_or_invalid_options"] == 1
    assert summary["valid_relationships"] == 1
    codes = {item["code"] for item in report["issues"]}
    assert "passage.source_page_artifact" in codes
    assert "question.option_page_artifact" in codes
    assert "question.page_artifact" in codes
    # Detection only: the source passage text remains exactly as supplied.
    assert "Page 24 of 39" in (root / "passages.csv").read_text(encoding="utf-8")


def test_passage_validation_flags_missing_and_invalid_child_references(tmp_path: Path) -> None:
    root = tmp_path / "references"
    (root / "GATE").mkdir(parents=True)
    with (root / "passages.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["passage_id", "course_id", "passage_text", "source_reference", "child_question_ids"]
        )
        writer.writerow(
            [
                "psg_references",
                "gate-physics",
                "This passage has enough words to pass the structural completeness threshold "
                "while its two child references are deliberately invalid for testing.",
                "GATE/Physics.csv",
                "MISSING_REF;WRONG_REF",
            ]
        )
    with (root / "GATE" / "Physics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "question_id",
                "original_question_number",
                "question_text",
                "question_type",
                "options",
                "answer_text",
                "passage_id",
                "subparts_json",
            ]
        )
        writer.writerow(["MISSING_REF", "1", "Missing ref.", "NAT", "", "", "", "[]"])
        writer.writerow(
            ["WRONG_REF", "2", "Wrong ref.", "NAT", "", "", "psg_other", "[]"]
        )

    report = validate_passage_dataset(root).as_dict()
    summary = report["summary"]
    assert summary["passage_question_count"] == 2
    assert summary["valid_relationships"] == 0
    assert summary["orphan_questions"] == 1
    codes = {item["code"] for item in report["issues"]}
    assert "question.missing_passage_reference" in codes
    assert "question.invalid_passage_reference" in codes


def test_unrelated_json_payload_remains_serialisable() -> None:
    # Guard the report/dashboard boundary: no non-serialisable row objects leak.
    payload = validate_passage_dataset("dataset_final_v5").as_dict()
    assert json.loads(json.dumps(payload))["summary"]["passage_count"] == 5
