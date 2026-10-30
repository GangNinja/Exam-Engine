"""CLI coverage for Phase 11 mutation, adversarial and misconception commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from exam_engine.cli import app

runner = CliRunner()
COURSE = "demo-cs201-2024-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MANIFEST = PROJECT_ROOT / "datasets" / "sample" / "manifest.json"
CONFUSED = (
    "A queue is a linear structure in which the item added first is removed first, "
    "because dequeue removes the front item."
)


def invoke(args: list[str], db: Path):
    return runner.invoke(app, ["--db", str(db), *args])


def _prepare(tmp_path: Path) -> Path:
    db = tmp_path / "advanced-cli.db"
    imported = invoke(["import", str(SAMPLE_MANIFEST)], db)
    assert imported.exit_code == 0, imported.output
    return db


def _generate(db: Path) -> dict:
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
    assert batch["questions"], batch["rejections"]
    return batch["questions"][0]


def test_advanced_cli_capabilities_lists_strategies(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    result = invoke(["advanced", "capabilities", "--json"], db)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["phase"] == 11
    assert any(item["strategy"] == "rephrase" for item in payload["mutation_strategies"])
    assert any(item["strategy"] == "counterexample" for item in payload["challenge_strategies"])


def test_advanced_cli_mutates_and_shows_a_variant(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    question = _generate(db)
    mutated = invoke(
        [
            "advanced",
            "mutate",
            "--course",
            COURSE,
            "--question",
            question["question_id"],
            "--strategy",
            "rephrase",
            "--count",
            "1",
            "--json",
        ],
        db,
    )
    assert mutated.exit_code == 0, mutated.output
    batch = json.loads(mutated.output)
    assert batch["capability"] == "mutation"
    assert batch["counts"]["persisted"] == 1
    variant = batch["questions"][0]
    assert variant["origin"] == "mutation"
    assert variant["strategy"] == "rephrase"
    assert variant["source_question_id"] == question["question_id"]

    shown = invoke(["advanced", "show", variant["question_id"]], db)
    assert shown.exit_code == 0, shown.output
    assert "origin=mutation" in shown.output

    mismatch = invoke(
        [
            "advanced",
            "mutate",
            "--course",
            COURSE,
            "--question",
            question["question_id"],
            "--strategy",
            "rephrase",
            "--format",
            "multiple_choice",
            "--json",
        ],
        db,
    )
    assert mismatch.exit_code == 1
    assert json.loads(mismatch.output)["error"]["code"] == "format_constraint_mismatch"

    reviewed = invoke(
        [
            "advanced",
            "review",
            variant["question_id"],
            "--status",
            "approved",
            "--note",
            "checked by hand",
            "--json",
        ],
        db,
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert json.loads(reviewed.output)["review_status"] == "approved"


def test_advanced_cli_generates_adversarial_questions_and_lists_variants(
    tmp_path: Path,
) -> None:
    db = _prepare(tmp_path)
    generated = invoke(
        [
            "advanced",
            "adversarial",
            "--course",
            COURSE,
            "--concept",
            "Stacks and queues",
            "--count",
            "2",
            "--json",
        ],
        db,
    )
    assert generated.exit_code == 0, generated.output
    batch = json.loads(generated.output)
    assert batch["capability"] == "adversarial"
    assert batch["counts"]["persisted"] == 2
    for question in batch["questions"]:
        assert question["origin"] == "adversarial"
        assert question["solution_steps"]
        assert question["review_status"] == "pending_review"

    listed = invoke(
        ["advanced", "variants", "--course", COURSE, "--origin", "adversarial", "--json"],
        db,
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert len(rows) == 2
    assert {row["origin"] for row in rows} == {"adversarial"}


def test_advanced_cli_imports_responses_and_reports_misconceptions(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    question = _generate(db)
    payload = {
        "responses": [
            {
                "question_id": question["question_id"],
                "answer": CONFUSED,
                "marks_awarded": 0,
                "marks_available": 4,
                "student_ref": "student-1",
            },
            {
                "question_id": question["question_id"],
                "answer": CONFUSED + " The queue also waits.",
                "marks_awarded": 0,
                "marks_available": 4,
                "student_ref": "student-2",
            },
        ]
    }
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    imported = invoke(
        ["advanced", "responses", "import", "--course", COURSE, "--file", str(path), "--json"],
        db,
    )
    assert imported.exit_code == 0, imported.output
    summary = json.loads(imported.output)
    assert summary["inserted"] == 2
    assert "student-1" not in imported.output

    analysed = invoke(["advanced", "misconceptions", "analyse", "--course", COURSE, "--json"], db)
    assert analysed.exit_code == 0, analysed.output
    report = json.loads(analysed.output)
    assert report["distinct_responses"] == 2
    assert report["records"]
    record = report["records"][0]
    assert record["review_status"] == "candidate"
    assert record["evidence"][0]["response_id"].startswith("resp_")

    listed = invoke(["advanced", "misconceptions", "list", "--course", COURSE, "--json"], db)
    assert listed.exit_code == 0, listed.output
    assert [row["misconception_id"] for row in json.loads(listed.output)] == [
        record["misconception_id"]
    ]

    shown = invoke(["advanced", "misconceptions", "show", record["misconception_id"]], db)
    assert shown.exit_code == 0, shown.output
    assert "evidence:" in shown.output

    reviewed = invoke(
        [
            "advanced",
            "misconceptions",
            "review",
            record["misconception_id"],
            "--status",
            "approved",
            "--note",
            "supported by two answers",
            "--json",
        ],
        db,
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert json.loads(reviewed.output)["review_status"] == "approved"

    exported = invoke(
        [
            "advanced",
            "misconceptions",
            "export",
            "--course",
            COURSE,
            "--syllabus",
            question["syllabus_id"],
            "--out",
            str(tmp_path / "misconceptions.json"),
            "--json",
        ],
        db,
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output)["count"] == 1


def test_advanced_cli_reports_unknown_course_without_a_traceback(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    result = invoke(
        [
            "advanced",
            "mutate",
            "--course",
            "course-does-not-exist",
            "--question",
            "q_missing",
        ],
        db,
    )
    assert result.exit_code == 1
    assert "unknown_course" in result.output


def test_advanced_cli_rejects_an_invalid_response_file(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = invoke(["advanced", "responses", "import", "--course", COURSE, "--file", str(bad)], db)
    assert result.exit_code == 1
    assert "invalid_file" in result.output


def test_advanced_cli_adversarial_requires_a_target(tmp_path: Path) -> None:
    db = _prepare(tmp_path)
    result = invoke(["advanced", "adversarial", "--course", COURSE], db)
    assert result.exit_code == 1
    assert "missing_target" in result.output
