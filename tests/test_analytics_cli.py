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


def prepare(tmp_path: Path) -> Path:
    db = tmp_path / "analytics.db"
    imported = invoke(["import", str(ANALYTICS_MANIFEST)], db)
    assert imported.exit_code == 0, imported.output
    analysed = invoke(["understanding", "analyze"], db)
    assert analysed.exit_code == 0, analysed.output
    return db


def test_analytics_cli_report_and_inspectors(tmp_path: Path) -> None:
    db = prepare(tmp_path)

    report = invoke(["analytics", "report", "--course", COURSE, "--json"], db)
    assert report.exit_code == 0, report.output
    payload = json.loads(report.output)
    assert len(payload) == 1
    assert payload[0]["course"]["course_id"] == COURSE
    assert payload[0]["data_quality"]["questions"] == 18
    assert payload[0]["repetition"]["exact_group_count"] == 4
    assert payload[0]["trends"]["status"] == "ok"
    assert payload[0]["topic_priority"]["topics"]

    plain = invoke(["analytics", "report", "--course", COURSE], db)
    assert plain.exit_code == 0
    assert "topic priority" in plain.output
    assert "not_analysed=" in plain.output
    assert "exact_in_groups=8" in plain.output
    assert "analytics describe the available dataset" in plain.output

    exact = invoke(
        ["analytics", "repetitions", "--course", COURSE, "--kind", "exact", "--json"],
        db,
    )
    assert exact.exit_code == 0, exact.output
    assert len(json.loads(exact.output)) == 4

    near = invoke(
        ["analytics", "repetitions", "--course", COURSE, "--kind", "near", "--json"],
        db,
    )
    assert near.exit_code == 0, near.output
    assert all(item["kind"] == "near" for item in json.loads(near.output))

    topics = invoke(["analytics", "topics", "--course", COURSE, "--limit", "3", "--json"], db)
    assert topics.exit_code == 0, topics.output
    assert 0 < len(json.loads(topics.output)) <= 3

    trends = invoke(["analytics", "trends", "--course", COURSE, "--json"], db)
    assert trends.exit_code == 0, trends.output
    assert json.loads(trends.output)["status"] == "ok"

    status = invoke(["analytics", "status", "--json"], db)
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)[0]["analysed"] == 18


def test_analytics_cli_clean_errors_and_multiple_courses(
    tmp_path: Path, sample_manifest: Path
) -> None:
    db = tmp_path / "analytics.db"
    imported = invoke(["import", str(sample_manifest)], db)
    assert imported.exit_code == 0, imported.output
    assert invoke(["understanding", "analyze"], db).exit_code == 0

    missing = invoke(["analytics", "report", "--course", "not-a-course"], db)
    assert missing.exit_code == 1
    assert "unknown course" in missing.output
    assert "Traceback" not in missing.output

    multiple = invoke(["analytics", "report", "--json"], db)
    assert multiple.exit_code == 0, multiple.output
    payload = json.loads(multiple.output)
    assert len(payload) == 2
    assert {item["course"]["course_id"] for item in payload} == {
        "demo-cs201-2024-v1",
        "demo-phy101-2024-v1",
    }

    plain = invoke(["analytics", "report"], db)
    assert plain.exit_code == 0
    assert "no cross-syllabus totals are pooled" in plain.output

    missing_syllabus = invoke(
        ["analytics", "report", "--course", "demo-cs201-2024-v1", "--syllabus-version", "v9"],
        db,
    )
    assert missing_syllabus.exit_code == 1
    assert "does not match" in missing_syllabus.output

    invalid_range = invoke(
        [
            "analytics",
            "report",
            "--course",
            "demo-cs201-2024-v1",
            "--marks-min",
            "10",
            "--marks-max",
            "5",
        ],
        db,
    )
    assert invalid_range.exit_code == 1
    assert "marks_min" in invalid_range.output
    assert "Traceback" not in invalid_range.output
