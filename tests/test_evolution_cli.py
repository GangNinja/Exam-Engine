"""CLI coverage for the Phase 9 concept evolution tracker."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app
from exam_engine.models.enums import DocumentKind
from exam_engine.models.syllabus import make_syllabus_id
from exam_engine.storage.db import open_database
from tests.test_evolution import (
    COURSE,
    _add_course,
    _add_document,
    _add_graph,
    _add_syllabus,
)

runner = CliRunner()


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def _prepare(tmp_path: Path) -> tuple[Path, str, str]:
    db_path = tmp_path / "evolution-cli.db"
    database = open_database(db_path)
    try:
        _add_course(database)
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2020",
            title="SYNTHETIC CLI syllabus 2020",
            units=[("Unit 1", "Foundations", ["Arrays"])],
            effective=date(2020, 1, 1),
        )
        _add_syllabus(
            database,
            COURSE,
            "doc_syllabus_2022",
            title="SYNTHETIC CLI syllabus 2022",
            units=[("Unit 1", "Foundations", ["Arrays"]), ("Unit 2", "Structures", ["Trees"])],
            effective=date(2022, 1, 1),
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2019",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC CLI notes 2019",
            publication_date=date(2019, 1, 1),
            pages=["Arrays provide fast index access to contiguous memory locations."],
        )
        _add_document(
            database,
            COURSE,
            "doc_notes_2021",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC CLI notes 2021",
            publication_date=date(2021, 1, 1),
            pages=["Arrays also model the schedule of passenger trains at a station."],
        )
        _add_graph(database, COURSE, [("Arrays", [], True)])
    finally:
        database.close()
    return (
        db_path,
        make_syllabus_id("doc_syllabus_2020"),
        make_syllabus_id("doc_syllabus_2022"),
    )


def test_evolution_cli_report_json_export_timeline_and_changes(tmp_path: Path) -> None:
    db_path, earlier, later = _prepare(tmp_path)

    result = invoke(["evolution", "report", "--course", COURSE, "--json"], db_path)
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == "phase9-v1"
    assert report["algorithm"] == "phase9_concept_evolution_tracker"
    assert report["status"] == "partial"
    assert report["summary"]["documents"] == 4
    assert report["summary"]["syllabus_comparisons"] == 1
    assert report["report_id"].startswith("evo_")
    assert report["data_quality"]["graph_available"] is True
    assert "4 document(s)" in report["scope"]["scope_statement"]

    labels = {timeline["canonical_label"] for timeline in report["timelines"]}
    assert {"Arrays", "Trees"} <= labels
    arrays = next(item for item in report["timelines"] if item["canonical_label"] == "Arrays")
    assert arrays["first_observed"] == "2019-01-01"
    assert arrays["last_observed"] == "2022-01-01"
    assert [item["date"] for item in arrays["observations"]] == [
        "2019-01-01",
        "2020-01-01",
        "2021-01-01",
        "2022-01-01",
    ]
    categories = {change["category"] for change in report["changes"]}
    assert {"context_changed", "concept_introduced"} <= categories

    export = tmp_path / "reports" / "evolution.json"
    exported = invoke(
        [
            "evolution",
            "report",
            "--course",
            COURSE,
            "--json",
            "--output",
            str(export),
        ],
        db_path,
    )
    assert exported.exit_code == 0, exported.stderr
    assert export.exists()
    assert json.loads(export.read_text(encoding="utf-8"))["report_id"] == report["report_id"]

    timeline = invoke(
        ["evolution", "timeline", "--course", COURSE, "--concept", "Arrays", "--json"],
        db_path,
    )
    assert timeline.exit_code == 0, timeline.stderr
    value = json.loads(timeline.stdout)
    assert value["canonical_label"] == "Arrays"
    assert value["dated_observation_count"] == len(value["observations"])
    assert {item["date_type"] for item in value["observations"]} == {
        "publication",
        "syllabus_effective",
    }

    context_only = invoke(
        [
            "evolution",
            "changes",
            "--course",
            COURSE,
            "--category",
            "context_changed",
            "--json",
        ],
        db_path,
    )
    assert context_only.exit_code == 0, context_only.stderr
    rows = json.loads(context_only.stdout)
    assert rows
    assert {row["category"] for row in rows} == {"context_changed"}
    assert all(row["review_required"] is True for row in rows)

    review_queue = invoke(["evolution", "changes", "--course", COURSE, "--review-only"], db_path)
    assert review_queue.exit_code == 0, review_queue.stderr
    assert "changes=" in review_queue.stdout
    assert "context_changed" in review_queue.stdout

    comparison = invoke(
        [
            "evolution",
            "compare",
            "--course",
            COURSE,
            "--earlier",
            earlier,
            "--later",
            later,
        ],
        db_path,
    )
    assert comparison.exit_code == 0, comparison.stderr
    assert "order_known=true" in comparison.stdout
    assert "not observed later" in comparison.stdout

    comparison_json = invoke(
        [
            "evolution",
            "compare",
            "--course",
            COURSE,
            "--earlier",
            earlier,
            "--later",
            later,
            "--json",
        ],
        db_path,
    )
    assert comparison_json.exit_code == 0, comparison_json.stderr
    payload = json.loads(comparison_json.stdout)
    assert payload["order_known"] is True
    assert payload["introduced"]
    assert payload["no_longer_observed"] == []

    human = invoke(["evolution", "report", "--course", COURSE], db_path)
    assert human.exit_code == 0, human.stderr
    assert "not the complete history" in human.stdout
    assert "scope:" in human.stdout


def test_evolution_cli_errors_are_structured(tmp_path: Path) -> None:
    db_path, earlier, later = _prepare(tmp_path)

    unknown_course = invoke(["evolution", "report", "--course", "nope", "--json"], db_path)
    assert unknown_course.exit_code == 1
    assert json.loads(unknown_course.stdout)["error"]["code"] == "unknown_course"

    bad_date = invoke(
        [
            "evolution",
            "report",
            "--course",
            COURSE,
            "--date-from",
            "2024-13-01",
            "--json",
        ],
        db_path,
    )
    assert bad_date.exit_code == 1
    assert json.loads(bad_date.stdout)["error"]["code"] == "invalid_evolution_filter"

    missing_document = invoke(
        [
            "evolution",
            "report",
            "--course",
            COURSE,
            "--document",
            "doc_missing",
            "--json",
        ],
        db_path,
    )
    assert missing_document.exit_code == 1
    payload = json.loads(missing_document.stdout)["error"]
    assert payload["code"] == "invalid_evolution_filter"
    assert payload["detail"]["unknown_document_ids"] == ["doc_missing"]

    unknown_concept = invoke(
        ["evolution", "timeline", "--course", COURSE, "--concept", "Nope", "--json"],
        db_path,
    )
    assert unknown_concept.exit_code == 1
    assert json.loads(unknown_concept.stdout)["error"]["code"] == "unknown_concept"

    unknown_syllabus = invoke(
        [
            "evolution",
            "compare",
            "--course",
            COURSE,
            "--earlier",
            "syl_missing",
            "--later",
            later,
        ],
        db_path,
    )
    assert unknown_syllabus.exit_code == 1
    assert "invalid_evolution_filter" in unknown_syllabus.stderr

    ranged = invoke(
        [
            "evolution",
            "report",
            "--course",
            COURSE,
            "--date-from",
            "2021-01-01",
            "--date-to",
            "2022-12-31",
            "--json",
        ],
        db_path,
    )
    assert ranged.exit_code == 0, ranged.stderr
    filtered = json.loads(ranged.stdout)
    assert {document["document_id"] for document in filtered["documents"]} == {
        "doc_notes_2021",
        "doc_syllabus_2022",
    }
    assert filtered["data_quality"]["excluded_documents"]
    assert earlier != later
