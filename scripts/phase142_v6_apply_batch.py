"""Apply a reasoned NEET batch to dataset_final_v3 without overwriting answers.

This is a data-processing harness, not an answer generator. Every answer comes
from work/phase142_v6/batch_01_decisions*.json and is written only once.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path("dataset_final_v3")
WORK = Path("work/phase142_v6")
PROGRESS = WORK / "progress_checkpoint.json"


def load_decisions(batch: str) -> dict[tuple[str, str], dict]:
    decisions: dict[tuple[str, str], dict] = {}
    paths = sorted(WORK.glob(f"{batch}_decisions*.json"))
    if not paths:
        raise SystemExit(f"no decisions for {batch}")
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data:
            key = (item["rel"], item["id"])
            if key in decisions:
                raise SystemExit(f"duplicate decision: {key}")
            decisions[key] = item
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True, help="e.g. batch_01 or batch_02")
    args = parser.parse_args()
    batch = args.batch
    batch_input = WORK / f"{batch}_input.json"
    batch_num = int(batch.split("_")[-1])
    data = json.loads(batch_input.read_text(encoding="utf-8"))
    batch_keys = {(r["rel"], r["id"]) for r in data}
    decisions = load_decisions(batch)
    if set(decisions) != batch_keys:
        missing = batch_keys - set(decisions)
        extra = set(decisions) - batch_keys
        raise SystemExit(f"batch mismatch missing={len(missing)} extra={len(extra)}")

    files = sorted(ROOT.glob("**/*.csv"))
    files = [p for p in files if p.name != "passages.csv"]
    rows_by_key: dict[tuple[str, str], dict] = {}
    all_rows: list[tuple[Path, dict]] = []
    for path in files:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            assert fields, path
            for row in reader:
                rel = path.relative_to(ROOT).as_posix()
                key = (rel, row["question_id"])
                if key in rows_by_key:
                    raise SystemExit(f"duplicate question key: {key}")
                rows_by_key[key] = row
                all_rows.append((path, row))

    answer_id_counts = Counter(
        r["answer_id"] for _, r in all_rows if r.get("answer_id", "").strip()
    )
    used_answer_ids = set(answer_id_counts)
    before_answered = sum(bool(r.get("answer_text", "").strip()) for _, r in all_rows)
    applied = 0
    skipped: list[dict] = []
    per_file = Counter()
    for key, decision in decisions.items():
        row = rows_by_key[key]
        if decision.get("skip"):
            skipped.append(
                {
                    "course_id": key[0].split("/")[0],
                    "relative_path": key[0],
                    "question_id": key[1],
                    "reason": decision.get("reason", "unspecified ambiguity"),
                }
            )
            continue
        answer = decision.get("answer", "").strip().upper()
        options = row.get("options", "")
        option_keys = {m.group(1) for m in re.finditer(r"(?:^|\|)\s*([A-D])\)", options)}
        if not answer or answer not in option_keys:
            raise SystemExit(f"answer {answer!r} does not map to options for {key}: {options!r}")
        if row.get("answer_text", "").strip():
            raise SystemExit(f"refusing to overwrite existing answer: {key}")
        answer_id = row.get("answer_id", "").strip()
        if answer_id and answer_id_counts[answer_id] > 1:
            answer_id = ""
        if not answer_id:
            stem = re.sub(r"[^A-Za-z0-9]+", "_", key[0].replace(".csv", ""))
            answer_id = f"V3_{stem}_{key[1]}"
            if answer_id in used_answer_ids:
                answer_id = f"{answer_id}_B01"
            if answer_id in used_answer_ids:
                raise SystemExit(f"could not allocate unique answer_id: {answer_id}")
            used_answer_ids.add(answer_id)
        row["answer_id"] = answer_id
        row["answer_text"] = answer
        row["answer_type"] = "MCQ option"
        row["solution_steps"] = decision["solution"]
        row["correct_option_key"] = answer
        row["answer_status"] = "ai_generated_unverified"
        row["answer_confidence"] = "0.85" if decision.get("confidence") == "high" else "0.70"
        row["answer_source"] = "coding_agent_reasoning"
        row["review_status"] = "needs_human_review"
        note = "v3 batch 01: coding-agent reasoning; not independently verified"
        if row.get("correction_notes", "").strip():
            note = row["correction_notes"].rstrip() + "; " + note
        row["correction_notes"] = note
        applied += 1
        per_file[key[0]] += 1

    # Rewrite each course file atomically, preserving row and column order.
    by_path: dict[Path, list[dict]] = {}
    for path, row in all_rows:
        by_path.setdefault(path, []).append(row)
    for path, rows in by_path.items():
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(path)

    total_questions = len(all_rows)
    total_answered = sum(bool(r.get("answer_text", "").strip()) for _, r in all_rows)
    total_unanswered = total_questions - total_answered
    if total_answered != before_answered + applied:
        raise SystemExit(f"unexpected answer count: {total_answered}")

    remaining = WORK / f"remaining_after_{batch}.csv"
    with remaining.open("w", encoding="utf-8", newline="") as handle:
        fields = ["course_id", "relative_path", "question_id", "question_type", "has_options", "passage_id", "reason"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for path, row in all_rows:
            if row.get("answer_text", "").strip():
                continue
            writer.writerow(
                {
                    "course_id": path.relative_to(ROOT).parts[0],
                    "relative_path": path.relative_to(ROOT).as_posix(),
                    "question_id": row["question_id"],
                    "question_type": row.get("question_type_normalized") or row.get("question_type", ""),
                    "has_options": "yes" if row.get("options", "").strip() else "no",
                    "passage_id": row.get("passage_id", ""),
                    "reason": f"not_solved_in_v3_{batch}",
                }
            )

    previous = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {}
    prior_skips = previous.get("skips", []) if isinstance(previous.get("skips"), list) else []
    prior_new = int(previous.get("new_answers_this_execution", 0))
    progress = {
        "dataset": "dataset_final_v3",
        "status": "in_progress",
        "total_questions": total_questions,
        "total_answered": total_answered,
        "total_unanswered": total_unanswered,
        "new_answers_this_execution": prior_new + applied,
        "evidence_recovered_answers": previous.get("evidence_recovered_answers", 0),
        "model_reasoned_answers": previous.get("model_reasoned_answers", 0) + applied,
        "unresolved_questions": total_unanswered,
        "existing_answers_audited": 6,
        "existing_answers_confirmed_correct": 6,
        "existing_answers_corrected": 0,
        "existing_answers_ambiguous": 0,
        "existing_answers_not_audited": 9303,
        "last_completed_batch": batch_num,
        f"{batch}_input": len(data),
        f"{batch}_answered": applied,
        f"{batch}_skipped": len(skipped),
        "batch_history": previous.get("batch_history", [])
        + [{"batch": batch, "input": len(data), "answered": applied, "skipped": len(skipped)}],
        "remaining_batch_queue": {
            "next_batch": batch_num + 1,
            "remaining_unanswered": total_unanswered,
            "estimated_100_question_batches": (total_unanswered + 99) // 100,
            "queue_file": remaining.as_posix(),
        },
        "batch_answer_counts": dict(sorted(per_file.items())),
        "skips": prior_skips + skipped,
    }
    PROGRESS.write_text(json.dumps(progress, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("APPLIED", applied)
    print("SKIPPED", len(skipped))
    print("TOTAL", total_questions, total_answered, total_unanswered)
    print("PER_FILE", dict(sorted(per_file.items())))


if __name__ == "__main__":
    main()
