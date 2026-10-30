"""CLI coverage for the Phase 7 exam coverage gap detector."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app
from exam_engine.models.enums import MappingStatus
from exam_engine.storage.db import open_database
from tests.test_coverage import (
    COURSE,
    _add_analysis,
    _add_course,
    _add_syllabus,
    _five_questions,
)

runner = CliRunner()


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def _prepare(tmp_path: Path) -> tuple[Path, str]:
    db_path = tmp_path / "coverage-cli.db"
    database = open_database(db_path)
    try:
        _add_course(database)
        syllabus = _add_syllabus(
            database,
            units=[("Unit 1", "Foundations", ["Réseaux", "Arrays"])],
        )
        questions = _five_questions(database)
        for question_id in ("q1", "q2"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.MAPPED,
                matches=[("Arrays", "tfidf+semantic", 0.8, 1, "")],
            )
        for question_id in ("q3", "q4", "q5"):
            _add_analysis(
                database,
                COURSE,
                syllabus,
                questions[question_id],
                status=MappingStatus.UNMAPPED,
            )
    finally:
        database.close()
    return db_path, syllabus.syllabus_id


def test_coverage_cli_report_json_export_and_inspectors(tmp_path: Path) -> None:
    db, syllabus_id = _prepare(tmp_path)
    base = ["coverage", "report", "--course", COURSE, "--syllabus", syllabus_id]

    report = invoke([*base, "--json"], db)
    assert report.exit_code == 0, report.output
    payload = json.loads(report.output)
    assert payload["schema_version"] == "phase7-v1"
    assert payload["course"]["course_id"] == COURSE
    assert payload["syllabus"]["syllabus_id"] == syllabus_id
    assert payload["summary"]["total_concepts"] == 2
    assert payload["summary"]["covered_concepts"] == 1
    assert payload["summary"]["mapped_questions"] == 2
    assert payload["data_quality"]["document_ids"]
    assert "coverage_missing_marks" in {warning["code"] for warning in payload["warnings"]}
    assert any(concept["label"] == "Réseaux" for concept in payload["concepts"])
    # The JSON stream is UTF-8 even on Windows code pages that cannot encode it.
    assert "Réseaux".encode() in report.stdout_bytes

    export = tmp_path / "exports" / "coverage.json"
    exported = invoke([*base, "--output", str(export), "--json"], db)
    assert exported.exit_code == 0, exported.output
    saved = json.loads(export.read_text(encoding="utf-8"))
    assert saved["report_id"] == json.loads(exported.output)["report_id"]
    assert saved["summary"] == payload["summary"]

    repeated = invoke([*base, "--json"], db)
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.output)["report_id"] == payload["report_id"]

    arrays = next(item for item in payload["concepts"] if item["label"] == "Arrays")
    concepts = invoke(
        [
            "coverage",
            "concepts",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--status",
            "covered",
            "--json",
        ],
        db,
    )
    assert concepts.exit_code == 0, concepts.output
    rows = json.loads(concepts.output)
    assert [row["concept_id"] for row in rows] == [arrays["concept_id"]]

    concept = invoke(
        [
            "coverage",
            "concept",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--concept",
            arrays["concept_id"],
            "--json",
        ],
        db,
    )
    assert concept.exit_code == 0, concept.output
    detail = json.loads(concept.output)
    assert detail["status"] == "covered"
    assert len(detail["supporting_questions"]) == 2
    assert all(
        item["mapping_sources"] == ["phase3_primary_mapping"]
        for item in detail["supporting_questions"]
    )

    units = invoke(
        ["coverage", "units", "--course", COURSE, "--syllabus", syllabus_id, "--json"],
        db,
    )
    assert units.exit_code == 0, units.output
    unit_rows = json.loads(units.output)
    assert len(unit_rows) == 1
    assert unit_rows[0]["unit_label"] == "Unit 1"
    assert unit_rows[0]["distinct_question_count"] == 2
    assert unit_rows[0]["mapped_marks"] == 10.0

    unmapped = invoke(
        ["coverage", "unmapped", "--course", COURSE, "--syllabus", syllabus_id, "--json"],
        db,
    )
    assert unmapped.exit_code == 0, unmapped.output
    unmapped_rows = json.loads(unmapped.output)
    assert len(unmapped_rows) == 3
    assert all("out-of-syllabus" not in item["explanation"].casefold() for item in unmapped_rows)

    plain = invoke(base, db)
    assert plain.exit_code == 0
    assert "covered=1" in plain.output
    assert "coverage describes" in plain.output or "coverage" in plain.output

    plain_concepts = invoke(
        ["coverage", "concepts", "--course", COURSE, "--syllabus", syllabus_id],
        db,
    )
    assert plain_concepts.exit_code == 0
    assert "Arrays" in plain_concepts.output

    plain_concept = invoke(
        [
            "coverage",
            "concept",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--concept",
            arrays["concept_id"],
        ],
        db,
    )
    assert plain_concept.exit_code == 0
    assert "why:" in plain_concept.output

    plain_units = invoke(
        ["coverage", "units", "--course", COURSE, "--syllabus", syllabus_id],
        db,
    )
    assert plain_units.exit_code == 0
    assert "Unit 1" in plain_units.output

    plain_unmapped = invoke(
        ["coverage", "unmapped", "--course", COURSE, "--syllabus", syllabus_id],
        db,
    )
    assert plain_unmapped.exit_code == 0
    assert "not automatically out-of-syllabus" in plain_unmapped.output


def test_coverage_cli_clean_errors_and_syllabus_selection(tmp_path: Path) -> None:
    db, syllabus_id = _prepare(tmp_path)
    missing_course = invoke(["coverage", "report", "--course", "missing-course", "--json"], db)
    assert missing_course.exit_code == 1
    error = json.loads(missing_course.output)
    assert error["error"]["code"] == "unknown_course"
    assert "Traceback" not in missing_course.output

    bad_unit = invoke(
        [
            "coverage",
            "report",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--unit",
            "missing-unit",
        ],
        db,
    )
    assert bad_unit.exit_code == 1
    assert "invalid_coverage_filter" in bad_unit.output
    assert "Traceback" not in bad_unit.output

    bad_period = invoke(
        [
            "coverage",
            "unmapped",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--exam-period",
            "1999-01",
        ],
        db,
    )
    assert bad_period.exit_code == 1
    assert "unknown_exam_periods" in bad_period.output
    assert "Traceback" not in bad_period.output

    missing_concept = invoke(
        [
            "coverage",
            "concept",
            "--course",
            COURSE,
            "--syllabus",
            syllabus_id,
            "--concept",
            "missing-concept",
        ],
        db,
    )
    assert missing_concept.exit_code == 1
    assert "unknown syllabus concept" in missing_concept.output
    assert "Traceback" not in missing_concept.output

    database = open_database(db)
    try:
        _add_syllabus(
            database,
            suffix="v2",
            units=[("Unit 1", "Alternative", ["Graphs"])],
        )
    finally:
        database.close()
    ambiguous = invoke(["coverage", "report", "--course", COURSE], db)
    assert ambiguous.exit_code == 1
    assert "ambiguous_syllabus" in ambiguous.output
    assert "pass an explicit syllabus" in ambiguous.output
    assert "Traceback" not in ambiguous.output
