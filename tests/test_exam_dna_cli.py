"""CLI coverage for the Phase 8 Exam DNA profile."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app
from exam_engine.models.enums import QuestionType
from exam_engine.storage.db import open_database
from tests.test_exam_dna import (
    COURSE,
    _add_analysis,
    _add_course,
    _add_paper,
    _add_syllabus,
)

runner = CliRunner()


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def _prepare(tmp_path: Path) -> tuple[Path, str]:
    db_path = tmp_path / "exam-dna-cli.db"
    database = open_database(db_path)
    try:
        _add_course(database)
        syllabus = _add_syllabus(
            database,
            units=[
                ("Unit 1", "Foundations", ["Arrays"]),
                ("Unit 2", "Structures", ["Trees"]),
            ],
        )
        for index, session in enumerate(("2024-05", "2025-05", "2026-05"), start=1):
            question_id = f"c{index}"
            questions = _add_paper(
                database,
                COURSE,
                f"doc_cli_{index}",
                title=f"SYNTHETIC CLI paper {index}",
                session=session,
                exam_date="2024-05-10" if index == 1 else None,
                questions=[
                    (
                        question_id,
                        "Explain array indexing and memory layout."
                        if index <= 2
                        else "Explain tree traversal.",
                        5.0,
                    )
                ],
            )
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                topic="Arrays" if index <= 2 else "Trees",
                primary_type=QuestionType.EXPLANATION,
            )
    finally:
        database.close()
    return db_path, syllabus.syllabus_id


def test_exam_dna_cli_profile_json_export_and_views(tmp_path: Path) -> None:
    db_path, syllabus_id = _prepare(tmp_path)

    result = invoke(
        ["exam-dna", "profile", "--course", COURSE, "--syllabus", syllabus_id, "--json"],
        db_path,
    )
    assert result.exit_code == 0, result.stderr
    profile = json.loads(result.stdout)
    assert profile["schema_version"] == "phase8-v1"
    assert profile["algorithm"] == "phase8_exam_dna_pattern_detector"
    assert profile["syllabus"]["syllabus_id"] == syllabus_id
    assert profile["summary"]["eligible_papers"] == 3
    assert profile["summary"]["questions"] == 3
    assert profile["profile_id"].startswith("dna_")

    export = tmp_path / "reports" / "exam-dna.json"
    exported = invoke(
        [
            "exam-dna",
            "profile",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--json",
            "--output",
            str(export),
        ],
        db_path,
    )
    assert exported.exit_code == 0, exported.stderr
    assert export.exists()
    assert json.loads(export.read_text(encoding="utf-8"))["profile_id"] == profile["profile_id"]

    patterns = invoke(
        [
            "exam-dna",
            "patterns",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--category",
            "question_type",
            "--json",
        ],
        db_path,
    )
    assert patterns.exit_code == 0, patterns.stderr
    rows = json.loads(patterns.stdout)
    assert rows
    assert {row["category"] for row in rows} == {"question_type"}
    assert rows[0]["eligible_papers"] == 3
    assert rows[0]["recurrence_rate"] is not None

    groups = invoke(
        [
            "exam-dna",
            "questions",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--kind",
            "exact",
            "--json",
        ],
        db_path,
    )
    assert groups.exit_code == 0, groups.stderr
    exact = json.loads(groups.stdout)
    assert len(exact) == 1
    assert exact[0]["kind"] == "exact"
    assert exact[0]["question_count"] == 2

    papers = invoke(
        ["exam-dna", "papers", "--course", COURSE, "--syllabus", syllabus_id],
        db_path,
    )
    assert papers.exit_code == 0, papers.stderr
    assert "SYNTHETIC CLI paper 1" in papers.stdout
    assert "eligible" in papers.stdout

    human = invoke(
        ["exam-dna", "profile", "--course", COURSE, "--syllabus", syllabus_id],
        db_path,
    )
    assert human.exit_code == 0, human.stderr
    assert "not a forecast" in human.stdout
    assert "coverage:" in human.stdout


def test_exam_dna_cli_errors_and_syllabus_selection(tmp_path: Path) -> None:
    db_path, syllabus_id = _prepare(tmp_path)

    unknown = invoke(["exam-dna", "profile", "--course", "nope", "--json"], db_path)
    assert unknown.exit_code == 1
    assert json.loads(unknown.stdout)["error"]["code"] == "unknown_course"

    bad_date = invoke(
        [
            "exam-dna",
            "profile",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--date-from",
            "2024-13-01",
            "--json",
        ],
        db_path,
    )
    assert bad_date.exit_code == 1
    assert json.loads(bad_date.stdout)["error"]["code"] == "invalid_exam_dna_filter"

    missing_document = invoke(
        [
            "exam-dna",
            "profile",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--document",
            "doc_missing",
            "--json",
        ],
        db_path,
    )
    assert missing_document.exit_code == 1
    payload = json.loads(missing_document.stdout)["error"]
    assert payload["code"] == "invalid_exam_dna_filter"
    assert payload["detail"]["unknown_document_ids"] == ["doc_missing"]

    database = open_database(db_path)
    try:
        _add_syllabus(database, suffix="b")
    finally:
        database.close()

    ambiguous = invoke(["exam-dna", "profile", "--course", COURSE, "--json"], db_path)
    assert ambiguous.exit_code == 1
    assert json.loads(ambiguous.stdout)["error"]["code"] == "ambiguous_syllabus"

    ranged = invoke(
        [
            "exam-dna",
            "profile",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--date-from",
            "2024-01-01",
            "--date-to",
            "2024-12-31",
            "--json",
        ],
        db_path,
    )
    assert ranged.exit_code == 0, ranged.stderr
    assert json.loads(ranged.stdout)["summary"]["eligible_papers"] == 1
