from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app

runner = CliRunner()


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def test_init_db_creates_schema(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    result = invoke(["init-db"], db)
    assert result.exit_code == 0, result.output
    assert "database ready" in result.output
    assert db.exists()

    again = invoke(["init-db"], db)
    assert again.exit_code == 0
    assert "already up to date" in again.output


def test_import_list_and_report(tmp_path: Path, sample_manifest: Path) -> None:
    db = tmp_path / "cli.db"
    result = invoke(["import", str(sample_manifest)], db)
    assert result.exit_code == 0, result.output
    assert "totals: documents=7" in result.output
    assert "questions=26" in result.output
    assert "synthetic" in result.output

    courses = invoke(["courses"], db)
    assert courses.exit_code == 0
    assert "demo-cs201-2024-v1" in courses.output
    assert "demo-phy101-2024-v1" in courses.output

    documents = invoke(["documents", "--course", "demo-cs201-2024-v1"], db)
    assert documents.exit_code == 0
    assert "question_paper_midterm.pdf" not in documents.output  # title is shown, not filename
    assert "CS201 Midterm" in documents.output

    questions = invoke(["questions", "--course", "demo-cs201-2024-v1", "--limit", "5"], db)
    assert questions.exit_code == 0
    assert "5 question(s) shown" in questions.output

    syllabus = invoke(["syllabus", "--course", "demo-cs201-2024-v1"], db)
    assert syllabus.exit_code == 0
    assert "Unit 1" in syllabus.output
    assert "Asymptotic notation" in syllabus.output

    stats = invoke(["stats"], db)
    assert stats.exit_code == 0
    assert "demo-cs201-2024-v1" in stats.output


def test_json_report_and_run_inspection(tmp_path: Path, sample_manifest: Path) -> None:
    db = tmp_path / "cli.db"
    result = invoke(["import", str(sample_manifest), "--json"], db)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["totals"]["documents"] == 7
    assert payload["ok"] is True
    run_id = payload["run_id"]

    runs = invoke(["runs"], db)
    assert runs.exit_code == 0
    assert run_id in runs.output

    detail = invoke(["run", run_id], db)
    assert detail.exit_code == 0
    assert "cs201/syllabus.docx" in detail.output

    missing = invoke(["run", "run_does_not_exist"], db)
    assert missing.exit_code == 1


def test_nlp_analyze_status_and_json_views(tmp_path: Path, sample_manifest: Path) -> None:
    db = tmp_path / "cli.db"
    assert invoke(["import", str(sample_manifest)], db).exit_code == 0

    analyzed = invoke(["nlp", "analyze", "--course", "demo-cs201-2024-v1", "--json"], db)
    assert analyzed.exit_code == 0, analyzed.output
    payload = json.loads(analyzed.output)
    assert len(payload) == 1
    assert payload[0]["course_id"] == "demo-cs201-2024-v1"
    assert payload[0]["status"] in {"ok", "partial"}
    assert payload[0]["totals"]["texts"] > 0
    assert payload[0]["model_ids"]

    status = invoke(["nlp", "status", "--json"], db)
    assert status.exit_code == 0, status.output
    rows = json.loads(status.output)
    row = next(item for item in rows if item["course_id"] == "demo-cs201-2024-v1")
    assert row["last_run"] == payload[0]["run_id"]
    assert row["keyphrases"] == payload[0]["totals"]["keyphrases"]

    plain_status = invoke(["nlp", "status"], db)
    assert plain_status.exit_code == 0
    assert payload[0]["run_id"] in plain_status.output

    keywords = invoke(
        ["nlp", "keywords", "--course", "demo-cs201-2024-v1", "--scope", "course", "--json"],
        db,
    )
    assert keywords.exit_code == 0, keywords.output
    assert 0 < len(json.loads(keywords.output)) <= 20

    concepts = invoke(
        [
            "nlp",
            "concepts",
            "--course",
            "demo-cs201-2024-v1",
            "--origin",
            "syllabus",
            "--json",
        ],
        db,
    )
    assert concepts.exit_code == 0, concepts.output
    assert all(item["verified"] for item in json.loads(concepts.output))

    entities = invoke(["nlp", "entities", "--course", "demo-cs201-2024-v1", "--json"], db)
    assert entities.exit_code == 0, entities.output
    assert json.loads(entities.output)

    providers = invoke(["nlp", "providers"], db)
    assert providers.exit_code == 0
    assert "hashing" in providers.output


def test_nlp_analyze_unknown_course_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    assert invoke(["init-db"], db).exit_code == 0
    result = invoke(["nlp", "analyze", "--course", "missing-course"], db)
    assert result.exit_code == 1
    assert "unknown course" in result.output
    assert "Traceback" not in result.output


def test_nlp_reset_requires_a_selection(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    result = invoke(["nlp", "reset"], db)
    assert result.exit_code == 2
    assert "--course" in result.output


def test_bad_manifest_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    manifest = tmp_path / "bad.json"
    manifest.write_text("{not json", encoding="utf-8")
    result = invoke(["import", str(manifest)], db)
    assert result.exit_code == 1
    assert "manifest" in result.output.lower()


def test_single_file_ingest(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    paper = tmp_path / "paper.txt"
    paper.write_text(
        "1. Define a stack. [5 marks]\n\n2. Define a queue. [5 marks]\n", encoding="utf-8"
    )
    result = invoke(
        [
            "ingest",
            str(paper),
            "--course-id",
            "cli-cs101-2024-v1",
            "--course-code",
            "CS101",
            "--course-title",
            "Intro",
            "--institution-code",
            "cli-inst",
            "--institution-name",
            "CLI Institute",
            "--kind",
            "question_paper",
            "--source-id",
            "cli-synthetic",
        ],
        db,
    )
    assert result.exit_code == 0, result.output
    assert "INGESTED" in result.output
    assert "questions=2" in result.output

    listed = invoke(["questions", "--course", "cli-cs101-2024-v1"], db)
    assert listed.exit_code == 0
    assert "Define a stack" in listed.output


def test_failed_single_file_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 nope")
    result = invoke(
        [
            "ingest",
            str(broken),
            "--course-id",
            "cli-cs101-2024-v1",
            "--course-code",
            "CS101",
            "--course-title",
            "Intro",
            "--institution-code",
            "cli-inst",
            "--institution-name",
            "CLI Institute",
            "--source-id",
            "cli-synthetic",
        ],
        db,
    )
    assert result.exit_code == 1
    assert "error" in result.output.lower()


def test_run_id_format(tmp_path: Path, sample_manifest: Path) -> None:
    db = tmp_path / "cli.db"
    result = invoke(["import", str(sample_manifest), "--json"], db)
    run_id = json.loads(result.output)["run_id"]
    assert re.fullmatch(r"run_\d{8}T\d{6}_[0-9a-f]{6}", run_id)
