"""CLI coverage for Phase 6 discovery, review, validation and path commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app

runner = CliRunner()
COURSE = "demo-analytics-cs201-2024-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_MANIFEST = PROJECT_ROOT / "datasets" / "analytics_sample" / "manifest.json"


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def _prepare(tmp_path: Path) -> Path:
    db = tmp_path / "prerequisite-cli.db"
    imported = invoke(["import", str(ANALYTICS_MANIFEST)], db)
    assert imported.exit_code == 0, imported.output
    analysed = invoke(["understanding", "analyze"], db)
    assert analysed.exit_code == 0, analysed.output
    built = invoke(["graph", "build", "--course", COURSE, "--json"], db)
    assert built.exit_code == 0, built.output
    return db


def test_prerequisite_cli_discover_review_validate_path_disable(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    discover = invoke(["prerequisites", "discover", "--course", COURSE, "--json"], db)
    assert discover.exit_code == 0, discover.output
    report = json.loads(discover.output)
    assert report["candidates"]
    assert all(item["status"] == "candidate" for item in report["candidates"])
    edge_id = report["candidates"][0]["edge_id"]
    target_id = report["candidates"][0]["target"]["concept_id"]

    candidates = invoke(["prerequisites", "candidates", "--course", COURSE, "--json"], db)
    assert candidates.exit_code == 0, candidates.output
    assert any(item["edge_id"] == edge_id for item in json.loads(candidates.output))

    reviewed = invoke(
        [
            "prerequisites",
            "review",
            edge_id,
            "--status",
            "reviewed",
            "--evidence",
            "cli reviewer: checked fixture syllabus",
            "--json",
        ],
        db,
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert json.loads(reviewed.output)["updated"] is True

    validation = invoke(["prerequisites", "validate", "--course", COURSE, "--json"], db)
    assert validation.exit_code == 0, validation.output
    payload = json.loads(validation.output)
    assert payload["candidate_count"] >= 1

    path = invoke(
        [
            "prerequisites",
            "path",
            "--course",
            COURSE,
            "--target",
            target_id,
            "--json",
        ],
        db,
    )
    assert path.exit_code == 0, path.output
    plan = json.loads(path.output)
    assert plan["steps"]
    assert plan["target_node_id"] == target_id
    assert all(step["role"] in {"prerequisite", "target", "review"} for step in plan["steps"])

    disabled = invoke(["prerequisites", "disable", edge_id, "--note", "cli hold", "--json"], db)
    assert disabled.exit_code == 0, disabled.output
    after_disabled = json.loads(
        invoke(
            [
                "prerequisites",
                "candidates",
                "--course",
                COURSE,
                "--include-disabled",
                "--json",
            ],
            db,
        ).output
    )
    assert (
        next(item for item in after_disabled if item["edge_id"] == edge_id)["metadata"]["disabled"]
        is True
    )

    enabled = invoke(["prerequisites", "enable", edge_id, "--json"], db)
    assert enabled.exit_code == 0, enabled.output

    dry_run = invoke(
        [
            "prerequisites",
            "discover",
            "--course",
            COURSE,
            "--dry-run",
            "--json",
        ],
        db,
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert json.loads(dry_run.output)["edges_created"] == 0


def test_prerequisite_cli_rejects_unknown_target(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    missing = invoke(
        ["prerequisites", "path", "--course", COURSE, "--target", "no-such-concept"],
        db,
    )
    assert missing.exit_code == 1
    assert "unknown concept target" in missing.output
    assert "Traceback" not in missing.output

    unknown_course = invoke(["prerequisites", "discover", "--course", "missing-course"], db)
    assert unknown_course.exit_code == 1
    assert "unknown course" in unknown_course.output

    missing_path = invoke(
        ["prerequisites", "path", "--course", "missing-course", "--target", "anything"],
        db,
    )
    assert missing_path.exit_code == 1
    assert "unknown course" in missing_path.output
    assert "Traceback" not in missing_path.output

    missing_candidates = invoke(["prerequisites", "candidates", "--course", "missing-course"], db)
    assert missing_candidates.exit_code == 1
    assert "unknown course" in missing_candidates.output
    assert "Traceback" not in missing_candidates.output
