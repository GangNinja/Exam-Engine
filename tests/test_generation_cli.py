"""CLI coverage for Phase 10 generation, review and export commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app

runner = CliRunner()
COURSE = "demo-cs201-2024-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MANIFEST = PROJECT_ROOT / "datasets" / "sample" / "manifest.json"


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def _prepare(tmp_path: Path) -> Path:
    db = tmp_path / "generation-cli.db"
    imported = invoke(["import", str(SAMPLE_MANIFEST)], db)
    assert imported.exit_code == 0, imported.output
    return db


def test_generation_cli_formats_and_generate_list_show(tmp_path: Path) -> None:
    db = _prepare(tmp_path)

    formats = invoke(["generation", "formats", "--json"], db)
    assert formats.exit_code == 0, formats.output
    payload = json.loads(formats.output)
    assert payload["provider"]["name"] in {"template", "openai"}
    rows = {row["format"]: row for row in payload["formats"]}
    assert rows["multiple_choice"]["requires_model"] is False
    assert rows["numerical"]["requires_model"] is True

    generated = invoke(
        [
            "generation",
            "generate",
            "--course",
            COURSE,
            "--concept",
            "Stacks and queues",
            "--count",
            "1",
            "--format",
            "explanation",
            "--json",
        ],
        db,
    )
    assert generated.exit_code == 0, generated.output
    batch = json.loads(generated.output)
    assert batch["counts"]["requested"] == 1
    assert batch["counts"]["persisted"] == 1
    question = batch["questions"][0]
    question_id = question["question_id"]
    assert question["concept_labels"] == ["Stacks and queues"]
    assert question["source_references"]
    assert question["validation"]["checks"]

    listed = invoke(["generation", "list", "--course", COURSE, "--json"], db)
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert [row["question_id"] for row in rows] == [question_id]
    assert rows[0]["review_status"] == "pending_review"

    shown = invoke(["generation", "show", "--question", question_id, "--json"], db)
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["question_id"] == question_id

    full = invoke(["generation", "show", "--batch", batch["batch_id"], "--json"], db)
    assert full.exit_code == 0, full.output
    assert json.loads(full.output)["batch_id"] == batch["batch_id"]

    batches = invoke(["generation", "batches", "--course", COURSE, "--json"], db)
    assert batches.exit_code == 0, batches.output
    rows = json.loads(batches.output)
    assert rows[0]["batch_id"] == batch["batch_id"]
    assert rows[0]["counts"]["requested"] == 1

    summary = invoke(
        [
            "generation",
            "review-summary",
            "--course",
            COURSE,
            "--syllabus",
            batch["scope"]["syllabus_id"],
            "--json",
        ],
        db,
    )
    assert summary.exit_code == 0, summary.output
    assert json.loads(summary.output)["pending_review"] == 1


def test_generation_cli_review_export_and_failures(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    generated = invoke(
        [
            "generation",
            "generate",
            "--course",
            COURSE,
            "--concept",
            "Stacks and queues",
            "--count",
            "1",
            "--format",
            "explanation",
            "--json",
        ],
        db,
    )
    assert generated.exit_code == 0, generated.output
    batch = json.loads(generated.output)
    question_id = batch["questions"][0]["question_id"]

    reviewed = invoke(
        [
            "generation",
            "review",
            "--question",
            question_id,
            "--status",
            "approved",
            "--note",
            "cli reviewer approved",
            "--json",
        ],
        db,
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert json.loads(reviewed.output)["review_status"] == "approved"

    applied = invoke(
        [
            "generation",
            "review-batch",
            "--batch",
            batch["batch_id"],
            "--status",
            "approved",
            "--all",
            "--json",
        ],
        db,
    )
    assert applied.exit_code == 0, applied.output
    updated = json.loads(applied.output)
    assert len(updated) >= 1
    assert all(item["review_status"] == "approved" for item in updated)

    destination = tmp_path / "phase10-export.json"
    exported = invoke(
        [
            "generation",
            "export",
            "--batch",
            batch["batch_id"],
            "--out",
            str(destination),
            "--json",
        ],
        db,
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output)["path"].endswith("phase10-export.json")
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["batch_id"] == batch["batch_id"]

    failed = invoke(
        [
            "generation",
            "generate",
            "--course",
            COURSE,
            "--concept",
            "Stacks and queues",
            "--count",
            "3",
            "--format",
            "explanation",
            "--json",
        ],
        db,
    )
    assert failed.exit_code == 0, failed.output
    failed_batch = json.loads(failed.output)
    assert failed_batch["questions"] == []
    assert failed_batch["rejections"]

    failures = invoke(["generation", "failures", "--batch", failed_batch["batch_id"], "--json"], db)
    assert failures.exit_code == 0, failures.output
    report = json.loads(failures.output)
    assert report["counts"]["rejected"] >= 1
    assert report["rejections"]


def test_generation_cli_reports_unknown_course_without_a_traceback(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    missing = invoke(
        [
            "generation",
            "generate",
            "--course",
            "no-such-course",
            "--concept",
            "Stacks",
            "--json",
        ],
        db,
    )
    assert missing.exit_code == 1
    payload = json.loads(missing.output)
    assert payload["error"]["code"] == "unknown_course"

    plain = invoke(["generation", "list", "--course", "no-such-course"], db)
    assert plain.exit_code == 1
    assert "Traceback" not in plain.output

    unknown_batch = invoke(["generation", "failures", "--batch", "gb_missing"], db)
    assert unknown_batch.exit_code == 1
    assert "unknown_batch" in unknown_batch.output
    assert "Traceback" not in unknown_batch.output
