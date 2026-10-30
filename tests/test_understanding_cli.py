from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app

runner = CliRunner()
COURSE = "demo-cs201-2024-v1"


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def test_understanding_cli_end_to_end(tmp_path: Path, sample_manifest: Path) -> None:
    db = tmp_path / "cli.db"
    imported = invoke(["import", str(sample_manifest)], db)
    assert imported.exit_code == 0, imported.output

    analyzed = invoke(["understanding", "analyze", "--course", COURSE, "--json"], db)
    assert analyzed.exit_code == 0, analyzed.output
    payload = json.loads(analyzed.output)
    assert len(payload) == 1
    assert payload[0]["course_id"] == COURSE
    assert payload[0]["totals"]["analysed"] > 0
    assert payload[0]["totals"]["questions"] == 15
    assert payload[0]["config"]["taxonomy_version"] == "builtin-1"

    status = invoke(["understanding", "status", "--json"], db)
    assert status.exit_code == 0, status.output
    rows = json.loads(status.output)
    row = next(item for item in rows if item["course_id"] == COURSE)
    assert row["last_run"] == payload[0]["run_id"]
    assert row["analyzed"] == payload[0]["totals"]["analysed"]
    assert row["mapped"] + row["ambiguous"] + row["unmapped"] == row["analyzed"]

    listing = invoke(["understanding", "questions", "--course", COURSE, "--json"], db)
    assert listing.exit_code == 0, listing.output
    items = json.loads(listing.output)
    assert items
    question_id = items[0]["analysis"]["question_id"]
    assert items[0]["question"]["text"]

    plain_listing = invoke(["understanding", "questions", "--course", COURSE], db)
    assert plain_listing.exit_code == 0
    assert "mapping" in plain_listing.output

    ambiguous = invoke(
        ["understanding", "questions", "--course", COURSE, "--status", "ambiguous", "--json"],
        db,
    )
    assert ambiguous.exit_code == 0, ambiguous.output
    ambiguous_items = json.loads(ambiguous.output)
    assert all(item["analysis"]["mapping_status"] == "ambiguous" for item in ambiguous_items)

    detail = invoke(["understanding", "show", question_id, "--json"], db)
    assert detail.exit_code == 0, detail.output
    record = json.loads(detail.output)
    assert record["question"]["question_id"] == question_id
    assert record["analysis"]["question_id"] == question_id
    assert "difficulty" in record
    assert "types" in record and "bloom" in record

    plain_detail = invoke(["understanding", "show", question_id], db)
    assert plain_detail.exit_code == 0
    assert question_id in plain_detail.output
    assert "mapping:" in plain_detail.output

    similar = invoke(["understanding", "similar", "--course", COURSE, "--json"], db)
    assert similar.exit_code == 0, similar.output
    matches = json.loads(similar.output)
    assert matches
    assert all(match["course_id"] == COURSE for match in matches)
    assert {match["kind"] for match in matches} <= {
        "exact",
        "near",
        "semantic",
        "concept_related",
    }

    taxonomy = invoke(["understanding", "taxonomy", "--json"], db)
    assert taxonomy.exit_code == 0, taxonomy.output
    taxonomy_payload = json.loads(taxonomy.output)
    assert taxonomy_payload["version"] == "builtin-1"
    assert "definition" in taxonomy_payload["question_types"]
    assert "remember" in taxonomy_payload["bloom_levels"]

    runs = invoke(["understanding", "runs", "--json"], db)
    assert runs.exit_code == 0, runs.output
    assert json.loads(runs.output)[0]["run_id"] == payload[0]["run_id"]

    reset = invoke(["understanding", "reset", "--course", COURSE, "--yes"], db)
    assert reset.exit_code == 0, reset.output
    after = invoke(["understanding", "status", "--json"], db)
    row_after = next(item for item in json.loads(after.output) if item["course_id"] == COURSE)
    assert row_after["analyzed"] == 0


def test_understanding_cli_requires_reset_selection(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    assert invoke(["init-db"], db).exit_code == 0
    result = invoke(["understanding", "reset"], db)
    assert result.exit_code == 2
    assert "--course" in result.output


def test_understanding_cli_unknown_question_and_course(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    assert invoke(["init-db"], db).exit_code == 0

    missing = invoke(["understanding", "show", "q_missing"], db)
    assert missing.exit_code == 1
    assert "unknown question" in missing.output

    no_analysis = invoke(["understanding", "show", "q_missing"], db)
    assert no_analysis.exit_code == 1

    unknown_course = invoke(["understanding", "analyze", "--course", "does-not-exist"], db)
    assert unknown_course.exit_code == 1
    assert "unknown course" in unknown_course.output
    assert "Traceback" not in unknown_course.output
