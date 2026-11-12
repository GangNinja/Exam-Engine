"""Phase 14.2 record-level provenance: backfill and reporting.

The script only rebuilds the additive ``exam_provenance`` table from the stored
projection and writes report artifacts. It never rewrites source CSVs/PDFs,
questions, answers or recovery evidence.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from exam_engine.storage.db import Database, open_database
from exam_engine.storage.exam_repository import ExamDatasetRepository
from exam_engine.storage.schema import DocumentRow

FIELDS = (
    "source_url",
    "source_publisher",
    "retrieved_at",
    "source_document_sha256",
    "examining_authority",
    "examination_name",
    "exam_year",
    "exam_session",
    "subject_code",
    "paper_code",
    "set_code",
    "region",
    "language",
    "original_question_id",
    "original_question_number",
    "extraction_method",
    "extraction_confidence",
)


def backfill(database: Database) -> int:
    """Rebuild provenance for every stored document; returns rows written."""
    with database.session() as session:
        document_ids = list(
            session.scalars(select(DocumentRow.document_id).order_by(DocumentRow.document_id)).all()
        )
    total = 0
    for document_id in document_ids:
        with database.transaction() as session:
            total += ExamDatasetRepository(session).replace_document_provenance(document_id)
    return total


def summarise(database: Database) -> dict[str, Any]:
    with database.session() as session:
        counts = ExamDatasetRepository(session).provenance_counts()
        courses = session.execute(
            text(
                """
                SELECT course_id,
                       count(*) AS rows,
                       sum(CASE WHEN scope = 'document' THEN 1 ELSE 0 END) AS documents,
                       sum(CASE WHEN scope = 'paper' THEN 1 ELSE 0 END) AS papers,
                       sum(CASE WHEN scope = 'question' THEN 1 ELSE 0 END) AS questions,
                       sum(CASE WHEN scope = 'answer' THEN 1 ELSE 0 END) AS answers,
                       sum(CASE WHEN source_classification = 'unknown' THEN 1 ELSE 0 END)
                           AS unknown_source,
                       sum(CASE WHEN source_classification = 'official' THEN 1 ELSE 0 END)
                           AS official_source,
                       sum(CASE WHEN scope = 'answer'
                                 AND source_classification = 'official'
                                 AND identity_status = 'confirmed'
                                 AND paper_identity_status = 'confirmed'
                                 AND key_identity_status = 'confirmed'
                                THEN 1 ELSE 0 END) AS promotable_answers
                FROM exam_provenance
                GROUP BY course_id
                ORDER BY course_id
                """
            )
        ).mappings().all()
        coverage_sql = ", ".join(
            f"sum(CASE WHEN {field} IS NULL THEN 0 ELSE 1 END) AS {field}" for field in FIELDS
        )
        coverage = (
            session.execute(text(f"SELECT {coverage_sql} FROM exam_provenance"))
            .mappings()
            .one()
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "db_path": database.path.as_posix() if database.path else None,
        "provenance": counts,
        "field_coverage": {field: int(coverage[field] or 0) for field in FIELDS},
        "courses": [dict(row) for row in courses],
    }


def render_markdown(report: dict[str, Any]) -> str:
    provenance = report["provenance"]
    lines = [
        "# Phase 14.2 record-level provenance report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"Database: `{report['db_path']}`",
        "",
        "A course-level status never implies that every question in that course has the same",
        "provenance. These counts are per provenance row (document, paper, question, answer).",
        "",
        "## Status vocabulary",
        "",
        "| Status | Meaning at this snapshot |",
        "|---|---|",
        "| `official_source_identified` | An authoritative document exists, but local "
        "row identity is not established. |",
        "| `official_paper_identity_confirmed` | The official paper and booklet/set are "
        "established for the row. |",
        "| `partial_identity` | Some content matches an official document; the complete "
        "paper/set identity is not established. |",
        "| `third_party_source_identified` | Evidence points to coaching or commercial "
        "compiled material. |",
        "| `mixed_source_corpus` | Several years, shifts, sets or source types are "
        "combined; no single identity. |",
        "| `identity_unresolved` | Evidence is insufficient to pair the row with a "
        "specific official paper or key. |",
        "| `source_retrieval_failed` | The official source was attempted but produced "
        "no usable extraction. |",
        "| `official_final_key_available_unmapped` | An official final key exists, but "
        "no official paper/option identity maps it to the local rows (NEET, JEE Main). |",
        "| `official_paper_available_no_final_key` | Official papers exist, but no "
        "retained official final key covers the relevant years (JEE Advanced). |",
        "",
        "## Rows by scope",
        "",
        "| Scope | Rows |",
        "|---|---:|",
    ]
    for scope, count in sorted(provenance["by_scope"].items()):
        lines.append(f"| {scope} | {count} |")
    lines.extend(
        [
            f"| **Total** | **{provenance['total']}** |",
            "",
            "## Status distribution",
            "",
            "| Dimension | Value | Rows |",
            "|---|---|---:|",
        ]
    )
    for dimension, label in (
        ("by_source_classification", "source classification"),
        ("by_identity_status", "identity"),
        ("by_paper_identity_status", "paper identity"),
        ("by_key_identity_status", "final-key identity"),
    ):
        for value, count in sorted(provenance[dimension].items()):
            lines.append(f"| {label} | {value} | {count} |")
    lines.extend(
        [
            "",
            "## Recorded values per field",
            "",
            "| Field | Rows with a value | Rows unknown (NULL) |",
            "|---|---:|---:|",
        ]
    )
    total = provenance["total"]
    for field in FIELDS:
        recorded = report["field_coverage"][field]
        lines.append(f"| `{field}` | {recorded} | {total - recorded} |")
    lines.extend(
        [
            "",
            "## Courses",
            "",
            "| Course | Rows | Documents | Papers | Questions | Answers | Unknown source |"
            " Official source | Officially promotable answers |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for course in report["courses"]:
        lines.append(
            f"| `{course['course_id']}` | {course['rows']} | {course['documents']} | "
            f"{course['papers']} | {course['questions']} | {course['answers']} | "
            f"{course['unknown_source']} | {course['official_source']} | "
            f"{course['promotable_answers']} |"
        )
    lines.extend(
        [
            "",
            "An officially promotable answer requires all of: official source classification,",
            "confirmed answer identity, confirmed paper identity and confirmed final-key identity.",
            "Zero such rows means the corpus carries no evidence strong enough to promote "
            "an answer.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/exam_engine.db")
    parser.add_argument("--output-dir", default="reports/phase142-provenance")
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args()

    database = open_database(args.db)
    try:
        if args.backfill:
            written = backfill(database)
            print(f"provenance rows rebuilt: {written}")
        report = summarise(database)
    finally:
        database.close()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "provenance_summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output / "provenance_levels.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report["provenance"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
