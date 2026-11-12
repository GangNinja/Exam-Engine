#!/usr/bin/env python
"""Run the Step 2 question-bank quality audit.

This command is read-only. It writes evidence and reports; it never applies the
safe correction plan, modifies v5/v6, or edits answers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from exam_engine.datasets.quality import audit_question_dataset, write_reports


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="dataset_final_v5", help="Source dataset root")
    parser.add_argument(
        "--output",
        default="reports/step2_quality",
        help="Directory for JSON, CSV and Markdown evidence",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows between progress/checkpoint boundaries",
    )
    return parser.parse_args()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    args = _parse_args()
    root = Path(args.root)
    output = Path(args.output)

    def progress(done: int, total: int) -> None:
        if done % 2000 == 0 or done == total:
            print(f"audited {done} / {total} records", flush=True)

    audit = audit_question_dataset(root, batch_size=args.batch_size, progress=progress)
    artifacts = write_reports(audit, output)
    summary = dict(audit.summary)
    v6 = Path("dataset_final_v6")
    summary["v6_tree_sha256"] = _tree_hash(v6) if v6.is_dir() else None
    summary["v6_present"] = v6.is_dir()
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("artifacts:")
    for name, path in sorted(artifacts.items()):
        print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
