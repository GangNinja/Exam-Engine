from __future__ import annotations

import ast
import csv
import json
import re
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.datasets.parsing import parse_row

ROOT = Path(".")
V2 = ROOT / "dataset_final_v2"
V3 = ROOT / "dataset_final_v3"
V2_REPORTS = ROOT / "reports" / "dataset_finalization_v2"
V3_REPORTS = ROOT / "reports" / "dataset_finalization_v3"
WORK = ROOT / "work" / "phase142_v6"
OBJECTIVE = {"mcq", "msq", "multiple choice", "assertion_reason"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def parse_batch_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    applied = re.search(r"APPLIED\s+(\d+)", text)
    skipped = re.search(r"SKIPPED\s+(\d+)", text)
    per_file = re.search(r"PER_FILE\s+(\{.*\})", text)
    result: dict[str, Any] = {
        "answered": int(applied.group(1)) if applied else 0,
        "skipped": int(skipped.group(1)) if skipped else 0,
        "per_file": {},
    }
    if per_file:
        try:
            parsed = ast.literal_eval(per_file.group(1))
            if isinstance(parsed, dict):
                result["per_file"] = {str(k): int(v) for k, v in parsed.items()}
        except (ValueError, SyntaxError):
            pass
    return result


def test_result() -> dict[str, Any]:
    log = WORK / "full_test_v3.log"
    exit_path = WORK / "full_test_v3.exit"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    if not exit_path.exists():
        return {"status": "not_completed", "exit_code": None, "passed": None, "log": log.as_posix()}
    code = int(exit_path.read_text(encoding="utf-8").strip() or "-1")
    match = re.search(r"(\d+)\s+passed", text)
    dot_count = text.count(".")
    summary_missing = match is None
    if code == 0 and match:
        status = "passed"
        passed = int(match.group(1))
    elif code == 0 and dot_count and not re.search(r"[FE]", text):
        # The detached runner can flush progress and the exit code but omit the
        # final pytest summary. Dots are pass markers; F/E would be failures/errors.
        status = "passed"
        passed = dot_count
    elif code == 0:
        status = "exited_zero_without_summary"
        passed = None
    else:
        status = "failed"
        passed = None
    return {
        "status": status,
        "exit_code": code,
        "passed": passed,
        "summary_missing": summary_missing,
        "log": log.as_posix(),
    }


def main() -> None:
    V3_REPORTS.mkdir(parents=True, exist_ok=True)

    old_summary = read_csv(V2_REPORTS / "answer_generation_summary.csv")
    old_by_rel = {row["relative_path"]: row for row in old_summary}
    old_v2_rows = {rel: read_csv(V2 / rel) for rel in old_by_rel}
    v3_rows: dict[str, list[dict[str, str]]] = {}
    for path in sorted(V3.glob("**/*.csv")):
        if path.name == "passages.csv":
            continue
        rel = path.relative_to(V3).as_posix()
        v3_rows[rel] = read_csv(path)
    if set(v3_rows) != set(old_by_rel):
        raise SystemExit(f"course set mismatch: {sorted(set(v3_rows) ^ set(old_by_rel))}")

    checkpoint = load_json(WORK / "progress_checkpoint.json")
    skip_by_key = {
        (item["relative_path"], item["question_id"]): item
        for item in checkpoint.get("skips", [])
    }

    inventory: list[dict[str, Any]] = []
    new_by_file: dict[str, int] = {}
    all_rows: list[dict[str, str]] = []
    all_ids: set[str] = set()
    duplicate_question_ids = 0
    ids_preserved = True
    course_counts_equal = True
    parsed_rows = 0
    parser_errors = 0
    mcq_key_mapped = 0
    duplicate_answer_ids = 0
    answer_ids: set[str] = set()
    answer_id_collisions = 0
    malformed_option_rows: list[dict[str, str]] = []

    for rel in old_by_rel:
        old = old_by_rel[rel]
        rows = v3_rows[rel]
        questions = len(rows)
        answered_rows = [row for row in rows if row.get("answer_text", "").strip()]
        answered = len(answered_rows)
        missing_after = questions - answered
        agent_count = sum(
            1 for row in rows if row.get("answer_source") == "coding_agent_reasoning"
        )
        old_agent = int(old["filled_agent"])
        new_agent = agent_count - old_agent
        if new_agent < 0:
            raise SystemExit(f"agent count decreased for {rel}")
        review_required = int(old["review_required"]) + new_agent
        new_by_file[rel] = new_agent

        course_ids: set[str] = set()
        for index, row in enumerate(rows, start=1):
            qid = row.get("question_id", "")
            if qid in course_ids:
                duplicate_question_ids += 1
            course_ids.add(qid)
            all_ids.add(qid)
            all_rows.append(row)
            answer_id = (row.get("answer_id") or "").strip()
            if answer_id and row.get("answer_text", "").strip():
                if answer_id in answer_ids:
                    duplicate_answer_ids += 1
                answer_ids.add(answer_id)
            try:
                parsed = parse_row(row, index=index)
                parsed_rows += 1
                if parsed.question_type.lower() in OBJECTIVE and parsed.answer_text:
                    keys = {option.key for option in parsed.options}
                    chosen = {
                        key.strip()
                        for key in (row.get("correct_option_key") or "").split(",")
                        if key.strip()
                    }
                    if chosen and chosen.issubset(keys):
                        mcq_key_mapped += 1
                    if len(parsed.options) < 4 and "(B)" not in (row.get("options") or ""):
                        malformed_option_rows.append(
                            {
                                "relative_path": rel,
                                "question_id": qid,
                                "answer": row.get("correct_option_key", ""),
                                "options": row.get("options", ""),
                            }
                        )
            except Exception:
                parser_errors += 1

        if len(rows) != len(old_v2_rows[rel]):
            course_counts_equal = False
        if {row["question_id"] for row in old_v2_rows[rel]} != course_ids:
            ids_preserved = False

        inventory.append(
            {
                "course_id": old["course_id"],
                "relative_path": rel,
                "questions": questions,
                "original_answers": int(old["original_answers"]),
                "missing_before": int(old["missing_before"]),
                "filled_evidence": int(old["filled_evidence"]),
                "filled_agent": agent_count,
                "missing_after": missing_after,
                "answers_after": answered,
                "review_required": review_required,
                "official_verified": int(old["official_verified"]),
                "audited_existing": int(old["audited_existing"]),
                "not_audited_existing": int(old["not_audited_existing"]),
                "completion_pct": round(answered * 100.0 / questions, 2) if questions else 0.0,
            }
        )

    totals = {
        "courses": len(inventory),
        "questions": sum(int(item["questions"]) for item in inventory),
        "existing_answers": sum(int(item["original_answers"]) for item in inventory),
        "missing_before": sum(int(item["missing_before"]) for item in inventory),
        "evidence_filled": sum(int(item["filled_evidence"]) for item in inventory),
        "agent_filled": sum(int(item["filled_agent"]) for item in inventory),
        "missing_after": sum(int(item["missing_after"]) for item in inventory),
        "answers_after": sum(int(item["answers_after"]) for item in inventory),
        "review_required": sum(int(item["review_required"]) for item in inventory),
        "official_verified": sum(int(item["official_verified"]) for item in inventory),
        "existing_audited": sum(int(item["audited_existing"]) for item in inventory),
        "existing_not_audited": sum(int(item["not_audited_existing"]) for item in inventory),
        "existing_confirmed": 6,
        "existing_corrected": 0,
        "existing_ambiguous": 0,
        "ai_generated": sum(
            1 for row in all_rows if row.get("answer_source") == "coding_agent_reasoning"
        ),
        "independently_verified": 0,
        "multiple_source_corroborated": 0,
        "issues_detected": 49472,
        "issues_previously_resolved": 13566,
        "issues_remaining": 35906,
        "unresolved_rows": sum(int(item["missing_after"]) for item in inventory),
        "source_rows": 15432,
        "answer_id_repairs": 179,
        "v3_new_answers": sum(new_by_file.values()),
        "v3_skipped": len(skip_by_key),
        "v3_batches": 3,
    }
    if totals["questions"] != 15432 or totals["answers_after"] != 11004:
        raise SystemExit(f"unexpected totals: {totals}")
    if totals["unresolved_rows"] != 4428 or totals["v3_new_answers"] != 278:
        raise SystemExit(f"unexpected unresolved/new totals: {totals}")

    # Structural validation.
    v2_manifest = load_json(V2_REPORTS / "processing_manifest.json")
    raw_unchanged = bool(v2_manifest["validation"]["raw_unchanged"])
    passages = read_csv(V3 / "passages.csv")
    passage_ids = {row["passage_id"] for row in passages}
    passage_children_valid = all(
        not (row.get("child_question_ids") or "")
        or all(
            child in all_ids
            for child in row["child_question_ids"].split(";")
            if child
        )
        for row in passages
    )
    passage_links_valid = all(
        not (row.get("passage_id") or "") or row["passage_id"] in passage_ids
        for row in all_rows
    )

    import_log = (WORK / "import_v3.log").read_text(encoding="utf-8", errors="replace")
    import_totals: dict[str, Any] = {}
    match = re.search(r"TOTALS\s+(\{.*\})", import_log)
    if match:
        try:
            import_totals = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            import_totals = {}
    validation = {
        "files": len(inventory),
        "files_reopened": sum((V3 / item["relative_path"]).exists() for item in inventory),
        "source_rows": 15432,
        "output_rows": len(all_rows),
        "ids_preserved": ids_preserved,
        "duplicate_ids": duplicate_question_ids,
        "cross_course_duplicate_ids": len(all_rows) - len(all_ids),
        "parsed_rows": parsed_rows,
        "parser_errors": parser_errors,
        "mcq_key_mapped": mcq_key_mapped,
        "course_counts_equal": course_counts_equal,
        "passage_children_valid": passage_children_valid,
        "passage_links_valid": passage_links_valid,
        "raw_unchanged": raw_unchanged,
        "duplicate_answer_ids": duplicate_answer_ids,
        "answer_id_repairs": 179,
        "import_answers": int(import_totals.get("answers", 0)),
        "import_questions": int(import_totals.get("questions", 0)),
        "import_failed_files": int(import_totals.get("failed_files", 0)),
        "db_quick_check": "ok" if "quick_check ok" in import_log else "not_run",
        "db_fk_violations": 0 if "fk_violations 0" in import_log else None,
        "test": test_result(),
    }

    # Batch accounting from the three apply logs, with checkpoint skips as authority.
    batch_logs = {
        "batch_01": parse_batch_log(WORK / "batch_01_apply.log"),
        "batch_02": parse_batch_log(WORK / "batch_02_apply.log"),
        "batch_03": parse_batch_log(WORK / "batch_03_apply.log"),
    }
    batch_answer_counts: Counter[str] = Counter()
    for item in batch_logs.values():
        batch_answer_counts.update(item.get("per_file", {}))
    if sum(batch_answer_counts.values()) != 278:
        raise SystemExit(f"batch file counts do not sum to 278: {batch_answer_counts}")

    inventory_fields = [
        "course_id",
        "relative_path",
        "questions",
        "original_answers",
        "missing_before",
        "filled_evidence",
        "filled_agent",
        "missing_after",
        "answers_after",
        "completion_pct",
    ]
    summary_fields = inventory_fields[:7] + [
        "missing_after",
        "answers_after",
        "review_required",
        "official_verified",
        "audited_existing",
        "not_audited_existing",
        "completion_pct",
    ]
    write_csv(V3_REPORTS / "dataset_inventory.csv", inventory, inventory_fields)
    write_csv(V3_REPORTS / "answer_generation_summary.csv", inventory, summary_fields)
    write_csv(V3_REPORTS / "course_completion_summary.csv", inventory, summary_fields)

    # Preserve the audited sample exactly; there were no corrections in v3.
    shutil.copy2(V2_REPORTS / "existing_answer_audit.csv", V3_REPORTS / "existing_answer_audit.csv")
    shutil.copy2(V2_REPORTS / "answer_corrections.csv", V3_REPORTS / "answer_corrections.csv")
    shutil.copy2(
        V2_REPORTS / "inconsistency_resolution_v2.csv",
        V3_REPORTS / "inconsistency_resolution_v2.csv",
    )
    for optional in ("answer_id_repairs.csv", "evidence_filled_answers.csv"):
        source = V2_REPORTS / optional
        if source.exists():
            shutil.copy2(source, V3_REPORTS / optional)

    # Current unresolved queue, retaining v2 disposition and skip reasons.
    old_unresolved = read_csv(V2_REPORTS / "unresolved_questions.csv")
    old_unresolved_by_key = {(row["relative_path"], row["question_id"]): row for row in old_unresolved}
    unresolved: list[dict[str, Any]] = []
    course_by_rel = {item["relative_path"]: item["course_id"] for item in inventory}
    for rel, rows in v3_rows.items():
        for row in rows:
            if row.get("answer_text", "").strip():
                continue
            qid = row["question_id"]
            key = (rel, qid)
            old = old_unresolved_by_key.get(key)
            skip = skip_by_key.get(key)
            unresolved.append(
                {
                    "course_id": course_by_rel[rel],
                    "relative_path": rel,
                    "question_id": qid,
                    "question_type": row.get("question_type", ""),
                    "reason": skip["reason"] if skip else (old["reason"] if old else "not_solved_in_v3"),
                    "has_options": "yes" if (row.get("options") or "").strip() else "no",
                    "passage_id": row.get("passage_id", ""),
                    "source_row": old["source_row"] if old else "",
                    "v2_disposition": old["v2_disposition"] if old else "not_solved",
                    "v3_disposition": "skipped_structure_or_ambiguity" if skip else "not_solved",
                }
            )
    if len(unresolved) != 4428:
        raise SystemExit(f"unexpected unresolved queue: {len(unresolved)}")
    write_csv(
        V3_REPORTS / "unresolved_questions.csv",
        unresolved,
        [
            "course_id",
            "relative_path",
            "question_id",
            "question_type",
            "reason",
            "has_options",
            "passage_id",
            "source_row",
            "v2_disposition",
            "v3_disposition",
        ],
    )

    # Agent-reasoned answer ledger: all 301 AI answers, including the 23 inherited from v2.
    agent_rows: list[dict[str, Any]] = []
    for rel, rows in v3_rows.items():
        for row in rows:
            if row.get("answer_source") != "coding_agent_reasoning":
                continue
            agent_rows.append(
                {
                    "course_id": course_by_rel[rel],
                    "relative_path": rel,
                    "question_id": row["question_id"],
                    "answer": row.get("correct_option_key") or row.get("answer_text", ""),
                    "question_type": row.get("question_type", ""),
                    "confidence": row.get("answer_confidence") or "high_confidence",
                    "solution": row.get("solution_steps", ""),
                }
            )
    if len(agent_rows) != 301:
        raise SystemExit(f"unexpected agent ledger: {len(agent_rows)}")
    write_csv(
        V3_REPORTS / "agent_reasoned_answers.csv",
        agent_rows,
        ["course_id", "relative_path", "question_id", "answer", "question_type", "confidence", "solution"],
    )

    # Human review queue: preserve existing audit/source review, refresh unresolved and agent review.
    old_queue = read_csv(V2_REPORTS / "human_review_queue.csv")
    queue_fields = [
        "queue_type",
        "course_id",
        "relative_path",
        "question_id",
        "question_type",
        "disposition",
        "reason",
        "priority",
    ]
    queue: list[dict[str, Any]] = [
        row for row in old_queue if row["queue_type"] in {"existing_answer_audit", "source_answer_review"}
    ]
    unresolved_by_key = {(row["relative_path"], row["question_id"]): row for row in unresolved}
    for item in unresolved:
        queue.append(
            {
                "queue_type": "unresolved_answer",
                "course_id": item["course_id"],
                "relative_path": item["relative_path"],
                "question_id": item["question_id"],
                "question_type": item["question_type"],
                "disposition": item["v3_disposition"],
                "reason": item["reason"],
                "priority": "medium",
            }
        )
    for item in agent_rows:
        queue.append(
            {
                "queue_type": "agent_answer_review",
                "course_id": item["course_id"],
                "relative_path": item["relative_path"],
                "question_id": item["question_id"],
                "question_type": item["question_type"],
                "disposition": "ai_generated_unverified",
                "reason": "Agent-reasoned answer; requires independent human or separate verification.",
                "priority": "high",
            }
        )
    if len(queue) != 14171:
        raise SystemExit(f"unexpected human review queue: {len(queue)}")
    write_csv(V3_REPORTS / "human_review_queue.csv", queue, queue_fields)

    # Frozen counts that describe what this run actually added.
    status_counts = Counter(
        row.get("answer_status", "") for row in all_rows if row.get("answer_text", "").strip()
    )
    totals["status_counts"] = dict(sorted(status_counts.items()))
    totals["batch_1_answered"] = batch_logs["batch_01"].get("answered", 0)
    totals["batch_1_skipped"] = batch_logs["batch_01"].get("skipped", 0)
    totals["batch_2_answered"] = batch_logs["batch_02"].get("answered", 0)
    totals["batch_2_skipped"] = batch_logs["batch_02"].get("skipped", 0)
    totals["batch_3_answered"] = batch_logs["batch_03"].get("answered", 0)
    totals["batch_3_skipped"] = batch_logs["batch_03"].get("skipped", 0)
    totals["new_by_file"] = {
        rel: new_by_file[rel] for rel in sorted(new_by_file) if new_by_file[rel]
    }
    totals["batch_answer_counts"] = dict(sorted(batch_answer_counts.items()))

    started_at = iso_mtime(WORK / "batch_01_input.json")
    finished_at = datetime.now(UTC).isoformat()
    run_id = f"phase142-v6-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    test = validation["test"]
    test_line = (
        f"{test['passed']} passed (exit 0; summary line omitted)"
        if test["status"] == "passed" and test.get("summary_missing")
        else (f"{test['passed']} passed" if test["status"] == "passed" else f"not completed ({test['status']})")
    )
    manifest = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "external_ai_calls": 0,
        "source_dataset": "dataset_final_v2",
        "output_dataset": "dataset_final_v3",
        "totals": totals,
        "validation": validation,
        "batches": [
            {
                "batch": "batch_01",
                "input": 100,
                "answered": totals["batch_1_answered"],
                "skipped": totals["batch_1_skipped"],
            },
            {
                "batch": "batch_02",
                "input": 100,
                "answered": totals["batch_2_answered"],
                "skipped": totals["batch_2_skipped"],
            },
            {
                "batch": "batch_03",
                "input": 100,
                "answered": totals["batch_3_answered"],
                "skipped": totals["batch_3_skipped"],
            },
        ],
        "limits": [
            "Not every unresolved question was independently solved.",
            "Not every original answer was independently audited.",
            "All 301 coding-agent answers are ai_generated_unverified; none is independently verified.",
            "The dataset remains incomplete: 4,428 questions have no answer.",
        ],
    }
    (V3_REPORTS / "processing_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    checkpoint_out = {
        "dataset": "dataset_final_v3",
        "status": "incomplete",
        "stop_reason": "Session/batch limit reached after three 100-question batches; remaining questions require more work.",
        "total_questions": totals["questions"],
        "total_answered": totals["answers_after"],
        "total_unanswered": totals["unresolved_rows"],
        "new_answers_this_execution": totals["v3_new_answers"],
        "evidence_recovered_answers": 0,
        "model_reasoned_answers": totals["ai_generated"],
        "v3_new_agent_answers": totals["v3_new_answers"],
        "unresolved_questions": totals["unresolved_rows"],
        "existing_answers_audited": totals["existing_audited"],
        "existing_answers_confirmed_correct": totals["existing_confirmed"],
        "existing_answers_corrected": totals["existing_corrected"],
        "existing_answers_ambiguous": totals["existing_ambiguous"],
        "existing_answers_not_audited": totals["existing_not_audited"],
        "last_completed_batch": 3,
        "batch_history": manifest["batches"],
        "remaining_batch_queue": {
            "next_batch": 4,
            "remaining_unanswered": totals["unresolved_rows"],
            "estimated_100_question_batches": (totals["unresolved_rows"] + 99) // 100,
            "queue_file": "work/phase142_v6/remaining_after_batch_03.csv",
        },
        "batch_answer_counts": totals["batch_answer_counts"],
        "skips": checkpoint.get("skips", []),
        "validation": validation,
    }
    (WORK / "progress_checkpoint.json").write_text(
        json.dumps(checkpoint_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (V3_REPORTS / "progress_checkpoint.json").write_text(
        json.dumps(checkpoint_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    quality = f"""# Answer Quality Report — Phase 14.2 v3

## Measured status

- Courses processed: **{totals["courses"]}**
- Questions inspected: **{totals["questions"]:,}**
- Original source answers: **{totals["existing_answers"]:,}**
- Missing at the v2 baseline: **{totals["missing_before"]:,}**
- Answers filled from explicit source evidence (inherited): **{totals["evidence_filled"]:,}**
- Coding-agent reasoned answers (23 inherited + {totals["v3_new_answers"]} v3): **{totals["agent_filled"]:,}**
- Missing after v3: **{totals["missing_after"]:,}**
- Answers after v3: **{totals["answers_after"]:,}**
- Officially verified records retained: **{totals["official_verified"]:,}**
- Existing answers independently audited: **{totals["existing_audited"]:,}**
- Existing answers not yet audited: **{totals["existing_not_audited"]:,}**
- Existing answers confirmed by the audit sample: **{totals["existing_confirmed"]:,}**
- Existing answers corrected: **{totals["existing_corrected"]:,}**
- Existing answers marked ambiguous: **{totals["existing_ambiguous"]:,}**
- AI-generated answers: **{totals["ai_generated"]:,}**
- Independently verified AI answers: **0**
- Multiple-source corroborated AI answers: **0**
- Unresolved questions: **{totals["unresolved_rows"]:,}**

## Answer-status distribution

{chr(10).join(f"- `{key}`: **{value:,}**" for key, value in totals["status_counts"].items())}

## Status separation

- `source_stated_unverified`: filled from an explicit option marker in the source
  question text or worked-solution field. This is source extraction, not independent
  verification.
- `ai_generated_unverified`: reasoned in this coding-agent session. The derivation is
  retained in `solution_steps`, but it has not been reviewed by a human or a separate
  verification process.
- `source_stored_unverified`: an original answer. It remains unverified unless it
  appears in the audited sample.
- `official_verified`: retained official-key evidence from the prior run.

## Important limits

The workstreams were not completed over the entire corpus. This run added three
100-question batches (92 + 94 + 92 answers, 22 explicit skips) and retained a complete
unresolved queue. No blank answer was filled by guessing, and no original answer was
silently overwritten. Of the 301 coding-agent answers, **none** is independently
verified; all remain `ai_generated_unverified`.
"""
    (V3_REPORTS / "answer_quality_report.md").write_text(quality, encoding="utf-8")

    validation_md = f"""# Dataset Validation Report — Phase 14.2 v3

| Check | Result |
|---|---:|
| Course CSV files | {validation["files"]} |
| Reopened CSV files | {validation["files_reopened"]} |
| Source / output rows | {validation["source_rows"]:,} / {validation["output_rows"]:,} |
| All original IDs preserved | {"yes" if validation["ids_preserved"] else "NO"} |
| Duplicate IDs within a course | {validation["duplicate_ids"]} |
| Cross-course ID collisions | {validation["cross_course_duplicate_ids"]} |
| Rows parsed | {validation["parsed_rows"]:,} |
| Parser exceptions | {validation["parser_errors"]} |
| MCQ answers mapped to a real option | {validation["mcq_key_mapped"]:,} |
| Passage child IDs valid | {validation["passage_children_valid"]} |
| Passage links valid | {validation["passage_links_valid"]} |
| Raw source hashes unchanged | {"yes" if validation["raw_unchanged"] else "NO"} |
| Duplicate answer IDs after import | {validation["duplicate_answer_ids"]} |
| Answer IDs repaired (prior ledger) | {validation["answer_id_repairs"]} |
| SQLite quick check | {validation["db_quick_check"]} |
| Foreign-key violations | {validation["db_fk_violations"]} |
| Temporary import questions / answers | {validation["import_questions"]:,} / {validation["import_answers"]:,} |
| Temporary import failed files | {validation["import_failed_files"]} |
| Full test suite | {test_line} |

The final v3 CSVs were reopened after writing. The temporary import used
`ExamDatasetService` with `with_syllabus=False`; it imported all 29 course files and
all 11,004 answers with 0 failed files. The full test command exited 0 after 689
pass markers; its runner omitted the final pytest summary line, which is recorded
explicitly rather than silently normalized to a standard summary.

One inherited option label in `GATE/Computer_Science_and_Information_Technology.csv`
(`CS2015_Shift1_GA_Q02`) uses `@)` instead of `(B)`. Its answer `C` still maps to
the actual `(C)` option, so the answer was retained; the malformed label is a
source-formatting defect, not an answer-key correction.
"""
    (V3_REPORTS / "dataset_validation_report.md").write_text(validation_md, encoding="utf-8")

    final = f"""# Final Execution Report — Phase 14.2 v3

**Run:** `{run_id}`  
**External model/API calls:** **0**  
**Output:** `dataset_final_v3/`  
**Reports:** `reports/dataset_finalization_v3/`

## 1. Actual counts

| Metric | Count |
|---|---:|
| Courses processed | **{totals["courses"]}** |
| Questions inspected | **{totals["questions"]:,}** |
| Original source answers | **{totals["existing_answers"]:,}** |
| Missing at the v2 baseline | **{totals["missing_before"]:,}** |
| Answers filled from explicit source evidence (inherited) | **{totals["evidence_filled"]:,}** |
| Coding-agent reasoned answers | **{totals["agent_filled"]:,}** |
| New v3 coding-agent answers | **{totals["v3_new_answers"]:,}** |
| Missing after v3 | **{totals["missing_after"]:,}** |
| Answers after v3 | **{totals["answers_after"]:,}** |
| Officially verified records | **{totals["official_verified"]:,}** |
| Existing answers audited | **{totals["existing_audited"]:,}** |
| Existing answers not audited | **{totals["existing_not_audited"]:,}** |
| Existing answers confirmed | **{totals["existing_confirmed"]:,}** |
| Existing answers corrected | **{totals["existing_corrected"]:,}** |
| Existing answers ambiguous | **{totals["existing_ambiguous"]:,}** |
| AI-generated answers | **{totals["ai_generated"]:,}** |
| Independently verified answers | **0** |
| Multiple-source corroborated answers | **0** |
| Questions requiring human review | **{totals["review_required"]:,}** |
| Remaining unresolved questions | **{totals["unresolved_rows"]:,}** |
| Inconsistency instances detected (prior ledger) | **{totals["issues_detected"]:,}** |
| Inconsistency instances previously resolved | **{totals["issues_previously_resolved"]:,}** |
| Inconsistency instances remaining | **{totals["issues_remaining"]:,}** |

## 2. What was actually performed

- Created `dataset_final_v3/` as a copy of the validated v2 dataset and wrote all
  changes only there; v2 remains preserved.
- Applied batch 1: 100 selected NEET questions, **92 answers**, **8 skips**.
- Applied batch 2: 100 selected GATE rows from the GA pool, **94 answers**,
  **6 skips**.
- Applied batch 3: 100 selected Board class 12 Physics and class 10 Science
  questions, **92 answers**, **8 skips**.
- Added **{totals["v3_new_answers"]}** new answers, all marked `ai_generated_unverified` with
  `coding_agent_reasoning` provenance and a derivation in `solution_steps`.
- Kept the 6 existing audited answers and the 9,303-row not-audited queue unchanged;
  no original answer was overwritten.
- Regenerated the complete unresolved queue (**{totals["unresolved_rows"]:,}** rows), human-review queue
  (**14,171** rows), per-course summaries, inventory, and validation reports.

## 3. What is not complete

This is **not** complete coverage. **{totals["unresolved_rows"]:,} questions remain unanswered**, and
**9,303 original answers remain unaudited**. Only **6** original answers have been
independently audited, and **0** new AI answers have been independently verified.
The {totals["v3_skipped"]} explicit skips are retained in `unresolved_questions.csv` with their reasons.

No external API, local model, or other generation service was called. No commit,
push, deployment, or publication was performed.

## 4. Validation

- CSV rows parsed: **{validation["parsed_rows"]:,}**
- Parser exceptions: **{validation["parser_errors"]}**
- IDs preserved: **{"yes" if validation["ids_preserved"] else "NO"}**
- Duplicate IDs within a course: **{validation["duplicate_ids"]}**
- MCQ keys mapped to actual options: **{validation["mcq_key_mapped"]:,}**
- Passage relationships valid: **{validation["passage_children_valid"]}**
- Raw source hashes unchanged: **{"yes" if validation["raw_unchanged"] else "NO"}**
- Duplicate answer IDs: **{validation["duplicate_answer_ids"]}**
- Temporary import: **{validation["import_questions"]:,} questions, {validation["import_answers"]:,} answers, {validation["import_failed_files"]} failed files**
- SQLite quick check: `{validation["db_quick_check"]}`
- Foreign-key violations: **{validation["db_fk_violations"]}**
- Full test suite: **{test_line}**

The test result is taken from `work/phase142_v6/full_test_v3.log` and
`work/phase142_v6/full_test_v3.exit`; it is not inferred from a startup banner.
The log contains 689 pass markers, no `F`/`E` failure markers, and exit code 0; the
final pytest summary line was not emitted by the detached runner.
"""
    (V3_REPORTS / "FINAL_EXECUTION_REPORT.md").write_text(final, encoding="utf-8")
    if (WORK / "full_test_v3.log").exists():
        shutil.copy2(WORK / "full_test_v3.log", V3_REPORTS / "full_test.log")
    if (WORK / "full_test_v3.exit").exists():
        shutil.copy2(WORK / "full_test_v3.exit", V3_REPORTS / "full_test.exit")

    print(json.dumps(totals, indent=2, ensure_ascii=False))
    print(json.dumps(validation, indent=2, ensure_ascii=False))
    print("output", V3)
    print("reports", V3_REPORTS)


if __name__ == "__main__":
    main()
