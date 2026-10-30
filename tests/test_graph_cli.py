"""CLI coverage for the Phase 5 graph commands."""

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
    db = tmp_path / "graph-cli.db"
    imported = invoke(["import", str(ANALYTICS_MANIFEST)], db)
    assert imported.exit_code == 0, imported.output
    analysed = invoke(["understanding", "analyze"], db)
    assert analysed.exit_code == 0, analysed.output
    built = invoke(["graph", "build", "--course", COURSE, "--json"], db)
    assert built.exit_code == 0, built.output
    return db


def test_graph_cli_build_query_evidence_and_review(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    report = json.loads(invoke(["graph", "build", "--course", COURSE, "--json"], db).output)
    assert report[0]["status"] == "ok"
    assert report[0]["node_count"] > 0

    status = invoke(["graph", "status", "--json"], db)
    assert status.exit_code == 0, status.output
    row = json.loads(status.output)[0]
    assert row["course_id"] == COURSE
    assert row["dangling_edges"] is False
    assert row["concepts"] > 0

    validated = invoke(["graph", "validate", "--course", COURSE, "--json"], db)
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output)["ok"] is True

    concepts = invoke(["graph", "concepts", "--course", COURSE, "--json"], db)
    assert concepts.exit_code == 0, concepts.output
    concept_nodes = json.loads(concepts.output)
    assert concept_nodes
    concept = concept_nodes[0]
    assert concept["course_id"] == COURSE

    hood = invoke(
        [
            "graph",
            "neighborhood",
            concept["node_id"],
            "--course",
            COURSE,
            "--hops",
            "2",
            "--json",
        ],
        db,
    )
    assert hood.exit_code == 0, hood.output
    neighbourhood = json.loads(hood.output)
    assert neighbourhood["edges"]

    edge = neighbourhood["edges"][0]
    evidence = invoke(["graph", "evidence", edge["edge_id"], "--json"], db)
    assert evidence.exit_code == 0, evidence.output
    assert json.loads(evidence.output)["edge"]["edge_id"] == edge["edge_id"]

    reviewed = invoke(
        [
            "graph",
            "review",
            edge["edge_id"],
            "--status",
            "reviewed",
            "--evidence",
            "cli reviewer: checked source",
            "--json",
        ],
        db,
    )
    assert reviewed.exit_code == 0, reviewed.output

    rebuilt = invoke(["graph", "build", "--course", COURSE], db)
    assert rebuilt.exit_code == 0, rebuilt.output
    after = invoke(["graph", "evidence", edge["edge_id"], "--json"], db)
    payload = json.loads(after.output)
    assert payload["edge"]["status"] == "reviewed"
    assert "cli reviewer: checked source" in payload["edge"]["evidence"]

    duplicate = invoke(["graph", "duplicates", "--course", COURSE, "--json"], db)
    assert duplicate.exit_code == 0, duplicate.output
    assert isinstance(json.loads(duplicate.output), list)

    plain = invoke(["graph", "status"], db)
    assert plain.exit_code == 0
    assert COURSE in plain.output

    plain_concepts = invoke(["graph", "concepts", "--course", COURSE], db)
    assert plain_concepts.exit_code == 0
    assert "concept_id" in plain_concepts.output

    reset = invoke(["graph", "reset", "--course", COURSE, "--yes"], db)
    assert reset.exit_code == 0, reset.output
    status_after = json.loads(invoke(["graph", "status", "--json"], db).output)[0]
    assert status_after["nodes"] == 0
    assert status_after["edges"] == 0


def test_graph_cli_rejects_missing_selectors(tmp_path: Path) -> None:
    db = tmp_path / "graph-cli.db"
    assert invoke(["init-db"], db).exit_code == 0

    missing_course = invoke(["graph", "build", "--course", "does-not-exist"], db)
    assert missing_course.exit_code == 1
    assert "unknown course" in missing_course.output
    assert "Traceback" not in missing_course.output

    questions = invoke(["graph", "questions", "--course", COURSE], db)
    assert questions.exit_code == 2
    assert "exactly one" in questions.output

    review = invoke(["graph", "review", "rel_missing", "--status", "reviewed"], db)
    assert review.exit_code == 1
    assert "unknown graph edge" in review.output

    reset = invoke(["graph", "reset", "--course", COURSE], db)
    assert reset.exit_code == 2
    assert "--yes" in reset.output
