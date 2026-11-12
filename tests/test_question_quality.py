from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from exam_engine.datasets.quality import (
    audit_question_dataset,
    safe_correct_text,
    write_reports,
)

COLUMNS = [
    "question_id",
    "original_question_number",
    "question_text",
    "question_type",
    "options",
    "marks",
    "answer_id",
    "answer_text",
    "answer_type",
    "solution_steps",
    "correct_option_key",
]


def _write_fixture(root: Path) -> Path:
    path = root / "GATE" / "Physics.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        [
            "Q_VALID",
            "1",
            "What is 2 + 2?",
            "MCQ",
            "A) 3 B) 4 C) 5 D) 6",
            "2",
            "A1",
            "B) 4",
            "MCQ option",
            "",
            "B",
        ],
        [
            "Q_MISSING_OPTS",
            "2",
            "Choose the correct relation.",
            "MCQ",
            "",
            "1",
            "A2",
            "B) nope",
            "MCQ option",
            "",
            "",
        ],
        [
            "Q_UNKNOWN",
            "3",
            "What is this item?",
            "Weird",
            "A) one B) two",
            "1",
            "A3",
            "A",
            "MCQ option",
            "",
            "A",
        ],
        [
            "Q_INVALID",
            "4",
            "Pick the valid option.",
            "MCQ",
            "A) one B) two",
            "1",
            "A4",
            "E) invalid",
            "MCQ option",
            "",
            "E",
        ],
        [
            "Q_CONTROL",
            "5",
            "Bad\x08 text\nPage 2 of 9",
            "MCQ",
            "A) x B) [figure/image option in source PDF]",
            "1",
            "A5",
            "A",
            "MCQ option",
            "",
            "A",
        ],
        [
            "Q_IMAGE",
            "6",
            "Look ![diagram](missing.png)",
            "Short answer",
            "",
            "2",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Q_EMPTY",
            "7",
            "",
            "MCQ",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "",
            "8",
            "Identify the item.",
            "Short answer",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Q_DUP",
            "9",
            "First duplicate wording.",
            "Short answer",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Q_DUP",
            "10",
            "Second duplicate wording.",
            "Short answer",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Q_EXACT",
            "11",
            "Exact repeated row.",
            "Short answer",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Q_EXACT",
            "11",
            "Exact repeated row.",
            "Short answer",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Q_UNANSWERED",
            "12",
            "Explain the result in one sentence.",
            "Short answer",
            "",
            "5",
            "",
            "",
            "",
            "",
            "",
        ],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return path


def _codes(audit) -> set[str]:
    return {issue.code for issue in audit.issues}


def test_quality_audit_classifies_real_structural_defects_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "v5"
    source = _write_fixture(root)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    before_rows = source.read_text(encoding="utf-8")

    audit = audit_question_dataset(root, batch_size=2)

    assert audit.summary["record_count"] == 13
    assert audit.summary["duplicate_question_id_groups"] == 2
    assert audit.summary["duplicate_source_records"] == 1
    assert audit.summary["missing_options"] == 2
    assert audit.summary["unknown_question_type"] == 1
    assert audit.summary["page_artifact_records"] == 1
    assert audit.summary["control_character_records"] == 1
    assert audit.summary["private_use_math_records"] == 0
    assert audit.summary["visual_placeholder_rows"] == 1
    assert audit.summary["visual_placeholder_options"] == 1
    assert audit.summary["missing_image_assets"] == 1
    assert audit.summary["image_asset_files"] == 0
    assert audit.summary["auto_correctable_records"] == 1
    assert audit.summary["unanswered_records"] == 8

    codes = _codes(audit)
    assert {
        "missing_options",
        "unknown_question_type",
        "answer_points_to_unknown_option",
        "missing_question_text",
        "missing_question_id",
        "duplicate_question_id",
        "duplicate_source_record",
        "control_character",
        "page_header_footer",
        "visual_asset_placeholder",
        "missing_image_asset",
    } <= codes

    valid_ids = {issue.question_id for issue in audit.issues}
    assert "Q_VALID" not in valid_ids
    assert "Q_UNANSWERED" not in valid_ids

    # The audit is read-only: no source row, answer, option or question text changed.
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert source.read_text(encoding="utf-8") == before_rows
    assert audit.source_hashes["root"]

    # The candidate correction retains both values and is recoverable from the report.
    assert len(audit.safe_corrections) == 1
    correction = audit.safe_corrections[0]
    assert correction.source_question_id == "Q_CONTROL"
    assert "\x08" in correction.before
    assert "\x08" not in correction.after
    assert "\nPage 2 of 9" in correction.after


def test_valid_image_references_are_resolved_and_ordered(tmp_path: Path) -> None:
    root = tmp_path / "images"
    gate = root / "GATE"
    gate.mkdir(parents=True)
    (gate / "a.png").write_bytes(b"not-an-image-payload-but-an-existing-asset")
    (gate / "b.png").write_bytes(b"not-an-image-payload-but-an-existing-asset")
    with (gate / "Physics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerow(
            [
                "Q_IMAGES",
                "1",
                "Use ![first](a.png) and ![second](b.png).",
                "Short answer",
                "",
                "2",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    audit = audit_question_dataset(root)
    assert audit.asset_inventory == ["GATE/a.png", "GATE/b.png"]
    assert audit.summary["image_asset_files"] == 2
    assert audit.summary["missing_image_assets"] == 0
    assert "missing_image_asset" not in _codes(audit)


def test_private_use_math_and_truncation_are_flagged_for_review(tmp_path: Path) -> None:
    root = tmp_path / "formatting"
    gate = root / "GATE"
    gate.mkdir(parents=True)
    with (gate / "Physics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerow(
            [
                "Q_MATH",
                "1",
                "Evaluate the expression \uf8eb x + 1 \uf8f7 for (",
                "Short answer",
                "",
                "2",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    audit = audit_question_dataset(root)
    assert audit.summary["private_use_math_records"] == 1
    assert "private_use_math_glyph" in _codes(audit)
    assert "unclosed_bracket_tail" in _codes(audit)
    assert audit.safe_corrections == []


def test_safe_correction_is_limited_to_controls_and_preserves_whitespace() -> None:
    value = "line one\x08\n\tline two"
    assert safe_correct_text(value) == "line one\n\tline two"
    assert safe_correct_text("No controls here.") == "No controls here."


def test_quality_reports_include_all_requested_surfaces(tmp_path: Path) -> None:
    root = tmp_path / "v5"
    _write_fixture(root)
    audit = audit_question_dataset(root)
    artifacts = write_reports(audit, tmp_path / "reports")

    assert set(artifacts) == {"summary", "issues", "safe_corrections"}
    for name in (
        "QUALITY_AUDIT_REPORT.md",
        "DATA_INTEGRITY_REPORT.md",
        "QUESTION_FORMATTING_REPORT.md",
        "IMAGE_DIAGRAM_INTEGRITY_REPORT.md",
        "CORRECTION_REPORT.md",
    ):
        assert (tmp_path / "reports" / name).is_file()
    assert (tmp_path / "reports" / "issues.csv").is_file()
    assert (tmp_path / "reports" / "summary.json").is_file()
    assert (tmp_path / "reports" / "safe_corrections.json").is_file()
    assert "Q_VALID" not in (tmp_path / "reports" / "issues.csv").read_text(encoding="utf-8")
    assert "Q_CONTROL" in (tmp_path / "reports" / "issues.csv").read_text(encoding="utf-8")
