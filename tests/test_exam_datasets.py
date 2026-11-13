"""Phase 13 personal dataset integration tests.

The fixture is deliberately small and synthetic. It exercises the real folder
catalog, the ten-column exam CSV schema, PDF syllabus ingestion, projection,
idempotent re-import and the database-backed dashboard API.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from exam_engine.config import Settings
from exam_engine.dashboard.api import DashboardAPI
from exam_engine.datasets.audit import audit_dataset
from exam_engine.datasets.catalog import discover
from exam_engine.datasets.parsing import (
    ParsedOption,
    extract_correct_option,
    parse_options,
    parse_options_from_text,
    parse_row,
    strip_embedded_options,
)
from exam_engine.datasets.queries import ExamDatasetQueries
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


def test_parser_recovers_numbered_and_mixed_option_formats() -> None:
    neet = (
        "a) Secondary cortex | b) Wood | c) Secondary phloem\n"
        "(D) Phellem | 1) (c), (d), (b), (a) | 2) (a), (b), (d), (c) | "
        "3) (d), (a), (c), (b) | 4) (d), (c), (a), (b)"
    )
    options = parse_options(neet, answer_text="3")
    assert [(option.key, option.text) for option in options] == [
        ("1", "(c), (d), (b), (a)"),
        ("2", "(a), (b), (d), (c)"),
        ("3", "(d), (a), (c), (b)"),
        ("4", "(d), (c), (a), (b)"),
    ]
    assert extract_correct_option("3", "mcq_option", options) == "3"

    mixed = parse_options(
        "1) Sex ratio (2) N atality | 3) Mortality (4) Species interaction",
        answer_text="2",
    )
    assert [(option.key, option.text) for option in mixed] == [
        ("1", "Sex ratio"),
        ("2", "N atality"),
        ("3", "Mortality"),
        ("4", "Species interaction"),
    ]
    assert extract_correct_option("2", "mcq_option", mixed) == "2"

    from_text = parse_options_from_text(
        "Chromatophores take part in :\n(1) Photosynthesis\n(2) Growth\n"
        "(3) Movement\n(4) Respiration"
    )
    assert [option.key for option in from_text] == ["1", "2", "3", "4"]


def test_question_payload_recovers_options_when_detail_column_is_empty() -> None:
    detail = SimpleNamespace(
        options_json="[]",
        question_type="mcq",
        subparts_json="[]",
        question_id="q_test",
        source_question_id="source_q_test",
        source_row=2,
        category_id="neet",
        subject_code="NEET-BIOLOGY",
        course_id="neet-biology",
        paper_id="paper_test",
        exam_year=2015,
        question_type_raw="MCQ",
        original_question_number="1",
        source_file="Biology2015_Paper.csv",
        passage_id=None,
        passage_order=None,
    )
    question = SimpleNamespace(
        metadata_json=json.dumps(
            {
                "options": "1) Photosynthesis | 2) Growth | 3) Movement | "
                "4) Respiration"
            }
        ),
        text="Chromatophores take part in :",
        marks=None,
    )
    answer = SimpleNamespace(
        answer_text="1",
        answer_type="mcq_option",
        correct_option_key=None,
        answer_type_raw="MCQ option",
        solution_steps=None,
        source_answer_id="source_q_test",
        match_method="csv_row_pairing",
    )
    queries = ExamDatasetQueries.__new__(ExamDatasetQueries)
    paper = SimpleNamespace(paper_label="P")
    payload = queries._question_payload(detail, question, answer, paper, None)

    assert payload["has_options"] is True
    assert [(option["key"], option["text"]) for option in payload["options"]] == [
        ("1", "Photosynthesis"),
        ("2", "Growth"),
        ("3", "Movement"),
        ("4", "Respiration"),
    ]
    assert payload["answer"]["correct_option_key"] == "1"


def test_parse_row_keeps_passage_subquestions_out_of_options() -> None:
    row = {
        "question_id": "English2016_DelhiSet1_SecA_Q01",
        "original_question_number": "1",
        "question_text": (
            "Read the passage given below and answer the questions that follow:\n"
            "Coffee can be good for you.\n"
            "(a) What do doctors advise about coffee?\n"
            "(b) What are the two versions of coffee?\n"
            "(c) State two benefits of antioxidants.\n"
            "(d) What do researchers warn about?"
        ),
        "question_type": "MCQ",
        "options": "",
        "marks": "8",
        "answer_id": "English2016_DelhiSet1_SecA_Q01",
        "answer_text": "Value points: (a) ... (b) ...",
        "answer_type": "Descriptive",
        "solution_steps": "",
    }

    parsed = parse_row(row, index=0)

    assert parsed.question_type == "passage_based"
    assert parsed.question_type_raw == "MCQ"
    assert parsed.options == []
    assert json.loads(parsed.subparts_json or "[]") == [
        "What do doctors advise about coffee?",
        "What are the two versions of coffee?",
        "State two benefits of antioxidants.",
        "What do researchers warn about?",
    ]


def test_question_payload_does_not_render_passage_subparts_as_options() -> None:
    detail = SimpleNamespace(
        options_json=json.dumps(
            [{"key": "A", "text": "What do doctors advise?"}]
        ),
        question_type="mcq",
        subparts_json="[]",
        question_id="q_passage",
        source_question_id="English2016_DelhiSet1_SecA_Q01",
        source_row=61,
        category_id="cbse-class-10",
        subject_code="CBSE-ENGLISH",
        course_id="cbse-class-10-english",
        paper_id="paper_test",
        exam_year=2016,
        question_type_raw="MCQ",
        original_question_number="1",
        source_file="English.csv",
        passage_id=None,
        passage_order=None,
    )
    question = SimpleNamespace(
        metadata_json="{}",
        text=(
            "Read the following passage and answer the questions:\n"
            "Coffee can be good for you.\n"
            "(a) What do doctors advise?\n"
            "(b) What are the two versions?\n"
            "(c) State two benefits."
        ),
        marks=None,
    )
    answer = SimpleNamespace(
        answer_text="Value points: (a) ... (b) ...",
        answer_type="descriptive",
        correct_option_key=None,
        answer_type_raw="Descriptive",
        solution_steps=None,
        source_answer_id="sa_passage",
        match_method="csv_row_pairing",
    )
    queries = ExamDatasetQueries.__new__(ExamDatasetQueries)
    payload = queries._question_payload(
        detail, question, answer, SimpleNamespace(paper_label="P"), None
    )

    assert payload["question_type"] == "passage_based"
    assert payload["options"] == []
    assert payload["has_options"] is False
    assert payload["subparts"] == [
        "What do doctors advise?",
        "What are the two versions?",
        "State two benefits.",
    ]
    assert payload["answer"]["correct_option_key"] is None


def test_parse_row_keeps_real_options_on_a_single_passage_mcq() -> None:
    row = {
        "question_id": "GATE2023_GA_Q8",
        "original_question_number": "8",
        "question_text": (
            "Which one of the options does NOT describe the passage below?\n"
            "We tend to think of cancer as a modern illness.\n"
            "(A) It is a reflection of modern medicine.\n"
            "(B) It uses metaphors of machinery.\n"
            "(C) Cancer is imagined as a machine.\n"
            "(D) It never uses figurative language."
        ),
        "question_type": "MCQ",
        "options": "",
        "marks": "1",
        "answer_id": "GATE2023_GA_Q8",
        "answer_text": "B",
        "answer_type": "MCQ option",
        "solution_steps": "",
    }

    parsed = parse_row(row, index=0)

    assert parsed.question_type == "passage_based"
    assert [option.key for option in parsed.options] == ["A", "B", "C", "D"]
    assert json.loads(parsed.subparts_json or "[]") == []
    assert parsed.correct_option_key == "B"


def test_parser_repairs_duplicated_ocr_option_letters() -> None:
    options = parse_options(
        "A) first statement | B) second statement "
        "(Cc) third statement | D) fourth statement",
        answer_text="A; B; C",
    )

    assert [option.key for option in options] == ["A", "B", "C", "D"]
    assert options[2].text == "third statement"
    assert options[3].text == "fourth statement"
    assert extract_correct_option("A; B; C", "multiple_options", options) == "A,B,C"


def test_strip_embedded_options_removes_a_complete_repeated_block() -> None:
    text = (
        "Which of the following structure is not found in a prokaryotic cell?\n"
        "(1) Nuclear envelope\n(2) Ribosome\n(3) Mesosome\n(4) Plasma membrane"
    )
    options = [
        ParsedOption("1", "Nuclear envelope"),
        ParsedOption("2", "Ribosome"),
        ParsedOption("3", "Mesosome"),
        ParsedOption("4", "Plasma membrane"),
    ]

    assert strip_embedded_options(text, options) == (
        "Which of the following structure is not found in a prokaryotic cell?"
    )
    assert strip_embedded_options("State:\n(a) one\n(b) two", options[:2]) == (
        "State:\n(a) one\n(b) two"
    )


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
        by_source_id = call(
            "/api/exams/questions?course=gate-computer-science-and-information-technology"
            "&search=CS2024_Paper_Q01"
        )
        assert by_source_id.status == 200
        assert by_source_id.payload["total"] == 1
        assert by_source_id.payload["questions"][0]["source_question_id"] == "CS2024_Paper_Q01"
        by_question_id = call(
            "/api/exams/questions?course=gate-computer-science-and-information-technology"
            "&question_id=" + by_source_id.payload["questions"][0]["question_id"]
        )
        assert by_question_id.status == 200
        assert by_question_id.payload["total"] == 1
        syllabus = call(
            "/api/exams/syllabus?course=gate-computer-science-and-information-technology"
        )
        assert syllabus.status == 200
        assert syllabus.payload["unit_count"] >= 1
        assert json.loads(json.dumps(overview.payload))["summary"]["categories"] == 2
    finally:
        api.close()
