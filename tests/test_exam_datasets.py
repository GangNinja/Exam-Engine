"""Phase 13 personal dataset integration tests.

The fixture is deliberately small and synthetic. It exercises the real folder
catalog, the ten-column exam CSV schema, PDF syllabus ingestion, projection,
idempotent re-import and the database-backed dashboard API.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from exam_engine.config import Settings
from exam_engine.dashboard.api import DashboardAPI
from exam_engine.datasets.audit import audit_dataset
from exam_engine.datasets.catalog import discover
from exam_engine.datasets.parsing import (
    extract_correct_option,
    parse_options,
    parse_row,
)
from exam_engine.datasets.service import ExamDatasetService
from exam_engine.storage.db import open_database
from tests.support import write_pdf

EXAM_COLUMNS = [
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


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXAM_COLUMNS)
        writer.writerows(rows)


def _fixture(root: Path) -> Path:
    _write_csv(
        root / "GATE" / "Computer_Science_and_Information_Technology.csv",
        [
            [
                "CS2024_Paper_Q01",
                "1",
                "Which structure stores values in last-in-first-out order?",
                "MCQ",
                "A) Queue B) Stack C) Array D) Tree",
                "2",
                "CS2024_Paper_Q01",
                "B) Stack",
                "MCQ option",
                "",
            ],
            [
                "CS2024_Paper_Q02",
                "2",
                "Explain why a stack is useful for function calls.",
                "Short answer",
                "",
                "5",
                "CS2024_Paper_Q02",
                "A stack stores return addresses in LIFO order.",
                "Answer",
                "",
            ],
        ],
    )
    _write_csv(
        root / "NEET" / "Physics.csv",
        [
            [
                "2019_S1_Q01",
                "1",
                "A body starts from rest with acceleration 2 m/s2 for 3 s. Find its speed.",
                "Numerical answer",
                "",
                "4",
                "2019_S1_Q01",
                "6 m/s",
                "Numeric",
                "v = u + at = 0 + 2*3 = 6 m/s",
            ]
        ],
    )
    gate_syllabus = root / "GATE" / "Syllabus"
    neet_syllabus = root / "NEET" / "Syllabus"
    gate_syllabus.mkdir(parents=True, exist_ok=True)
    neet_syllabus.mkdir(parents=True, exist_ok=True)
    write_pdf(
        gate_syllabus / "CS_GATE2027_Syllabus.pdf",
        [["UNIT I: Data Structures", "1. Stacks", "2. Queues"]],
    )
    write_pdf(
        neet_syllabus / "NEET_2026_Physics_Syllabus.pdf",
        [["UNIT I: Mechanics", "1. Motion in a straight line"]],
    )
    return root


def test_discovery_preserves_categories_and_subject_isolation(tmp_path: Path) -> None:
    catalog = discover(_fixture(tmp_path))

    assert {category.category_id for category in catalog.categories} == {"gate", "neet"}
    assert {subject.course_id for subject in catalog.subjects} == {
        "gate-computer-science-and-information-technology",
        "neet-physics",
    }
    gate = catalog.subject("gate-computer-science-and-information-technology")
    neet = catalog.subject("neet-physics")
    assert gate is not None and neet is not None
    assert gate.course_code == "CS"
    assert gate.syllabus_version == "2027"
    assert neet.syllabus_version == "2026"
    assert gate.syllabus_paths[0].name.startswith("CS_")
    assert neet.syllabus_paths[0].name.startswith("NEET_")


def test_audit_and_parser_preserve_source_answers(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    result = audit_dataset(root)
    result.write(tmp_path / "reports")

    assert result.summary["files"]["csv"] == 2
    assert result.summary["files"]["pdf"] == 2
    assert result.summary["records"]["csv_rows"] == 3
    assert result.summary["records"]["answers_present"] == 3
    assert result.summary["validation"]["error"] == 0
    assert (tmp_path / "reports" / "dataset_inventory.csv").exists()
    assert (tmp_path / "reports" / "dataset_validation_report.csv").exists()
    assert (tmp_path / "reports" / "dataset_summary.json").exists()

    options = parse_options("A) Queue B) Stack C) Array D) Tree")
    assert extract_correct_option("B) Stack", "mcq_option", options) == "B"
    row = parse_row(
        {
            "question_id": "CS2024_Paper_Q01",
            "original_question_number": "1",
            "question_text": "Which structure is LIFO? A) Queue B) Stack",
            "question_type": "MCQ",
            "options": "",
            "marks": "2",
            "answer_id": "CS2024_Paper_Q01",
            "answer_text": "B",
            "answer_type": "MCQ option",
            "solution_steps": "",
        },
        index=2,
    )
    assert row.exam_year == 2024
    assert row.paper_label == "CS2024_Paper"
    assert [option.key for option in row.options] == ["A", "B"]
    assert row.correct_option_key == "B"


def test_import_projection_reimport_and_dashboard(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    db_path = tmp_path / "exam.db"
    settings = Settings.from_env().with_db(db_path)
    database = open_database(db_path)
    try:
        service = ExamDatasetService(database, settings)
        report = service.import_dataset(root, output_dir=tmp_path / "out")
        assert report.status == "ok"
        counts = service.status()["counts"]
        assert counts["questions"] == 3
        assert counts["answers"] == 3
        assert counts["papers"] == 2
        assert counts["courses"] == 2
        assert counts["categories"] == 2
        assert counts["syllabus_documents"] == 2
        assert counts["syllabus_units"] >= 2

        from exam_engine.datasets.queries import ExamDatasetQueries, ExamFilters

        with database.session() as session:
            queries = ExamDatasetQueries(session)
            gate = queries.questions(
                ExamFilters(course_id="gate-computer-science-and-information-technology")
            )
            neet = queries.questions(ExamFilters(course_id="neet-physics"))
            assert gate["total"] == 2
            assert neet["total"] == 1
            assert gate["questions"][0]["source_file"].endswith(
                "Computer_Science_and_Information_Technology.csv"
            )

        repeat = service.import_dataset(root, output_dir=tmp_path / "repeat")
        assert repeat.totals["questions"] == 0
        assert repeat.totals["skipped"] == 3
        assert service.status()["counts"] == counts
    finally:
        database.close()

    api = DashboardAPI(db_path, settings)
    try:

        def call(path: str):
            parsed = urlsplit(path)
            return api.dispatch(
                "GET", parsed.path, parse_qs(parsed.query, keep_blank_values=True), b""
            )

        overview = call("/api/exams")
        assert overview.status == 200
        assert overview.payload["summary"]["questions"] == 3
        assert overview.payload["summary"]["answers"] == 3
        assert overview.payload["summary"]["papers"] == 2
        assert overview.payload["summary"]["syllabus_documents"] == 2
        filtered = call("/api/exams?course=gate-computer-science-and-information-technology")
        assert filtered.status == 200
        assert filtered.payload["summary"]["questions"] == 2
        assert filtered.payload["summary"]["syllabus_documents"] == 1
        assert filtered.payload["summary"]["syllabus_units"] >= 1
        questions = call(
            "/api/exams/questions?course=gate-computer-science-and-information-technology"
        )
        assert questions.status == 200
        assert questions.payload["total"] == 2
        assert all(
            item["course_id"] == "gate-computer-science-and-information-technology"
            for item in questions.payload["questions"]
        )
        syllabus = call(
            "/api/exams/syllabus?course=gate-computer-science-and-information-technology"
        )
        assert syllabus.status == 200
        assert syllabus.payload["unit_count"] >= 1
        assert json.loads(json.dumps(overview.payload))["summary"]["categories"] == 2
    finally:
        api.close()
