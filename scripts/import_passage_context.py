"""Validate and backfill canonical exam passage context.

Examples:
    python scripts/import_passage_context.py --dataset dataset_final_v5 --validate-only
    python scripts/import_passage_context.py --dataset dataset_final_v5 --db data/exam_engine.db
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from exam_engine.config import Settings
from exam_engine.datasets.passages import validate_passage_dataset
from exam_engine.datasets.service import ExamDatasetService
from exam_engine.storage.db import open_database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="dataset_final_v5")
    parser.add_argument("--db", default="data/exam_engine.db")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report", default="reports/passage_validation/validation.json")
    args = parser.parse_args()

    root = Path(args.dataset)
    report = validate_passage_dataset(root)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.as_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(report.as_dict()["summary"], indent=2))
    if args.validate_only:
        return 0

    database = open_database(args.db)
    try:
        result = ExamDatasetService(database, Settings.from_env().with_db(args.db)).import_passages(
            root
        )
    finally:
        database.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
