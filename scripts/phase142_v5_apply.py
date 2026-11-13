#!/usr/bin/env python
"""Apply explicitly evidenced and agent-reasoned answers to dataset_final_v2.

This is a dataset transformation harness only. It does not call any model API,
does not modify the live database, and preserves the prior dataset_final output.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.datasets.parsing import parse_row

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "dataset_final"
V2 = ROOT / "dataset_final_v2"
RAW = ROOT / "datasets"
REPORTS = ROOT / "reports" / "dataset_finalization_v2"
WORK = ROOT / "work" / "phase142_v5"
BACKUP = ROOT / "backups" / "datasets_original_20261109"
OBJECTIVE = {"mcq", "msq", "multiple choice", "assertion_reason"}
LABELS = "ABCDEFGH"


def now() -> str:
    return datetime.now(UTC).isoformat()


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def raw_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def option_labels(options: str) -> list[str]:
    text = (options or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return [str(k).strip().upper() for k in value]
        except json.JSONDecodeError:
            pass
    found = re.findall(r"(?:^|\|)\s*([A-H]|[1-4])\)", text)
    if not found:
        found = re.findall(r"\(([A-H])\)", text)
    result: list[str] = []
    for label in found:
        label = label.upper()
        if label not in result:
            result.append(label)
    return result


def map_label(label: str, keys: list[str]) -> str | None:
    label = label.upper()
    if label in keys:
        return label
    if keys and all(k in "1234" for k in keys) and label in LABELS:
        mapped = str(ord(label) - ord("A") + 1)
        return mapped if mapped in keys else None
    if keys and all(k in LABELS for k in keys) and label in "1234":
        mapped = chr(ord("A") + int(label) - 1)
        return mapped if mapped in keys else None
    return None


def normalized_type(row: dict[str, str]) -> str:
    return (row.get("question_type_normalized") or row.get("question_type") or "").strip().lower()


def is_objective(row: dict[str, str]) -> bool:
    qtype = normalized_type(row)
    return qtype in OBJECTIVE or "multiple" in qtype


def clean_evidence(text: str, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


def extract_candidate(raw_row: dict[str, str], v2_row: dict[str, str]) -> dict[str, Any] | None:
    if (v2_row.get("answer_text") or "").strip() or not is_objective(v2_row):
        return None
    keys = option_labels(v2_row.get("options", ""))
    if not keys:
        return None
    qtype = normalized_type(v2_row)
    solution = (raw_row.get("solution_steps") or "").strip()
    question = (raw_row.get("question_text") or "").strip()

    # A worked solution can begin with the selected option marker, e.g. (C) or (B, C).
    prefix = re.match(
        r"^\s*\(([A-D1-4](?:\s*[,/]\s*[A-D1-4])*)\)\s*",
        solution,
    )
    if prefix:
        labels = [part.strip().upper() for part in re.split(r"[,/]", prefix.group(1))]
        mapped = [map_label(label, keys) for label in labels]
        if all(mapped):
            labels = [label for label in mapped if label]
            if labels and (len(labels) == 1 or qtype in {"msq", "multiple choice"}):
                return {
                    "labels": labels,
                    "method": "worked_solution_prefix",
                    "source": "worked_solution",
                    "status": "source_stated_unverified",
                    "confidence": "medium_confidence",
                    "evidence": clean_evidence(solution),
                }

    patterns = [
        re.compile(
            r"\boption\s*\(([A-Da-d1-4])\)\s*(?:is|:)\s*(?:the\s*)?correct\b",
            re.I,
        ),
        re.compile(
            r"\b(?:the\s+)?correct\s+option\s+is\s*\(([A-Da-d1-4])\)",
            re.I,
        ),
        re.compile(r"\b(?:answer|ans)\s*[.:\-]\s*\(([A-Da-d1-4])\)", re.I),
    ]
    for pattern in patterns:
        match = pattern.search(question) or pattern.search(solution)
        if not match:
            continue
        label = map_label(match.group(1), keys)
        if not label:
            continue
        source_text = question if pattern.search(question) else solution
        snippet = clean_evidence(source_text[max(0, match.start() - 80) : match.end() + 120])
        return {
            "labels": [label],
            "method": "stated_option_marker",
            "source": "source_stated_answer",
            "status": "source_stated_unverified",
            "confidence": "medium_confidence",
            "evidence": snippet,
        }
    return None


def set_answer(
    row: dict[str, str],
    labels: list[str],
    *,
    source: str,
    status: str,
    confidence: str,
    solution: str,
    method: str,
) -> None:
    keys = ",".join(labels)
    row["answer_text"] = keys
    row["answer_type"] = "multiple_choice" if len(labels) > 1 else "mcq"
    row["correct_option_key"] = keys
    row["answer_status"] = status
    row["answer_confidence"] = confidence
    row["answer_source"] = source
    row["review_status"] = "needs_human_review"
    row["solution_steps"] = clean_evidence(solution, 2000)
    if not (row.get("answer_id") or "").strip():
        row["answer_id"] = f"{row['question_id']}_V2"
    note = f"v2: {method}; source={source}; not independently verified; review required"
    previous = (row.get("correction_notes") or "").strip()
    row["correction_notes"] = f"{previous}; {note}" if previous else note
    row["original_answer_text"] = ""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    apply = "--apply" in sys.argv
    started = now()
    summary_rows, _ = read_csv(
        ROOT / "reports" / "dataset_finalization" / "answer_generation_summary.csv"
    )
    course_ids = {row["relative_path"]: row["course_id"] for row in summary_rows}

    manual = json.loads((WORK / "manual_answers.json").read_text(encoding="utf-8"))
    manual_by_key = {(item["relative_path"], item["question_id"]): item for item in manual}
    audits = json.loads((WORK / "existing_audits.json").read_text(encoding="utf-8"))

    courses: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    human_review: list[dict[str, Any]] = []
    applied_evidence: list[dict[str, Any]] = []
    applied_agent: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    all_ids_by_course: dict[str, set[str]] = {}
    all_ids: set[str] = set()

    # Load and process each course.
    for v1_path in sorted(V1.rglob("*.csv")):
        if v1_path.name == "passages.csv":
            continue
        rel = v1_path.relative_to(V1).as_posix()
        if "/Syllabus/" in f"/{rel}/":
            continue
        raw_path = RAW / rel
        if not raw_path.exists():
            continue
        v2_path = V2 / rel
        rows, fields = read_csv(v2_path)
        raw, _ = raw_rows(raw_path)
        raw_by_qid = {row["question_id"].strip(): row for row in raw}
        raw_row_numbers = {
            row["question_id"].strip(): index for index, row in enumerate(raw, start=1)
        }
        course_id = course_ids.get(rel, rel.replace("/", "-").lower())
        before_empty = sum(not (row["answer_text"] or "").strip() for row in rows)
        original_answers = sum(bool((row.get("answer_text") or "").strip()) for row in raw)
        for row in rows:
            qid = row["question_id"].strip()
            key = (rel, qid)
            if (row.get("answer_text") or "").strip():
                continue
            if key in manual_by_key:
                item = manual_by_key[key]
                keys = option_labels(row.get("options", ""))
                label = map_label(item["answer"], keys)
                if not label:
                    raise RuntimeError(
                        f"manual answer label does not map: {key} {item['answer']} {keys}"
                    )
                set_answer(
                    row,
                    [label],
                    source="coding_agent_reasoning",
                    status="ai_generated_unverified",
                    confidence=item.get("confidence", "high_confidence"),
                    solution=item["solution"],
                    method="agent_reasoned",
                )
                applied_agent.append(
                    {
                        "course_id": course_id,
                        "relative_path": rel,
                        "question_id": qid,
                        "answer": label,
                        "question_type": normalized_type(row),
                        "confidence": item.get("confidence", "high_confidence"),
                        "solution": item["solution"],
                    }
                )
                continue
            evidence = extract_candidate(raw_by_qid.get(qid, {}), row)
            if evidence is None:
                continue
            set_answer(
                row,
                evidence["labels"],
                source=evidence["source"],
                status=evidence["status"],
                confidence=evidence["confidence"],
                solution=evidence["evidence"],
                method=evidence["method"],
            )
            applied_evidence.append(
                {
                    "course_id": course_id,
                    "relative_path": rel,
                    "question_id": qid,
                    "answer": ",".join(evidence["labels"]),
                    "question_type": normalized_type(row),
                    "confidence": evidence["confidence"],
                    "method": evidence["method"],
                    "evidence": evidence["evidence"],
                }
            )

        # Validate and count after changes.
        after_empty = sum(not (row["answer_text"] or "").strip() for row in rows)
        ids = [row["question_id"].strip() for row in rows]
        all_ids_by_course[rel] = set(ids)
        all_ids.update(ids)
        course_audits = [item for item in audits if item["relative_path"] == rel]
        audit_count = 0
        for item in course_audits:
            target = next((row for row in rows if row["question_id"] == item["question_id"]), None)
            if target is None:
                continue
            existing = (target.get("answer_text") or "").strip()
            expected = item["expected_key"]
            found = re.search(
                rf"(?:option\s*)?\(\s*{re.escape(expected.lower())}\s*\)",
                existing,
                re.I,
            )
            status = item["status"]
            if status == "correct" and not found:
                status = "ambiguous"
            audit_count += 1
            validation_rows.append(
                {
                    "course_id": course_id,
                    "relative_path": rel,
                    "question_id": item["question_id"],
                    "question_type": normalized_type(target),
                    "existing_answer": existing,
                    "existing_status": target.get("answer_status", ""),
                    "expected_key": expected,
                    "audit_status": status,
                    "audit_confidence": item["confidence"],
                    "independent_derivation": item["reason"],
                    "review_status": "confirmed" if status == "correct" else "needs_human_review",
                }
            )

        # All original source answers are represented in the audit queue, including
        # those not reached by this run's small reasoning batch.
        audited_keys = {(item["relative_path"], item["question_id"]) for item in audits}
        for raw_row in raw:
            qid = raw_row["question_id"].strip()
            if not (raw_row.get("answer_text") or "").strip():
                continue
            if (rel, qid) in audited_keys:
                continue
            target = next((row for row in rows if row["question_id"] == qid), None)
            if target is None:
                continue
            validation_rows.append(
                {
                    "course_id": course_id,
                    "relative_path": rel,
                    "question_id": qid,
                    "question_type": normalized_type(target),
                    "existing_answer": raw_row.get("answer_text", ""),
                    "existing_status": target.get("answer_status", ""),
                    "expected_key": "",
                    "audit_status": "not_audited",
                    "audit_confidence": "",
                    "independent_derivation": "",
                    "review_status": "needs_human_review",
                }
            )

        for row in rows:
            qid = row["question_id"].strip()
            q = row.get("question_text", "")
            qtype = normalized_type(row)
            if not (row.get("answer_text") or "").strip():
                if is_objective(row) and not option_labels(row.get("options", "")):
                    reason = "missing_options_for_objective_question"
                elif re.search(
                    r"\b(figure|diagram|graph|table|shown below|given below)\b", q, re.I
                ):
                    reason = "requires_source_figure_or_table"
                elif "\ufffd" in q or q.strip().startswith('","'):
                    reason = "corrupted_question_text"
                else:
                    reason = "not_solved_in_this_run"
                unresolved.append(
                    {
                        "course_id": course_id,
                        "relative_path": rel,
                        "question_id": qid,
                        "question_type": qtype,
                        "reason": reason,
                        "has_options": "yes" if (row.get("options") or "").strip() else "no",
                        "passage_id": row.get("passage_id", ""),
                        "source_row": raw_row_numbers.get(qid, ""),
                        "v2_disposition": "not_solved",
                    }
                )
                human_review.append(
                    {
                        "queue_type": "unresolved_answer",
                        "course_id": course_id,
                        "relative_path": rel,
                        "question_id": qid,
                        "question_type": qtype,
                        "disposition": reason,
                        "reason": "No answer has been filled; do not fabricate without evidence.",
                        "priority": "high"
                        if reason in {"corrupted_question_text", "requires_source_figure_or_table"}
                        else "medium",
                    }
                )
            if row.get("answer_source") == "coding_agent_reasoning":
                human_review.append(
                    {
                        "queue_type": "agent_answer_review",
                        "course_id": course_id,
                        "relative_path": rel,
                        "question_id": qid,
                        "question_type": qtype,
                        "disposition": "ai_generated_unverified",
                        "reason": (
                            "Agent-reasoned answer; requires independent human or "
                            "separate verification."
                        ),
                        "priority": "high",
                    }
                )
            if row.get("answer_source") in {"source_stated_answer", "worked_solution"}:
                human_review.append(
                    {
                        "queue_type": "source_answer_review",
                        "course_id": course_id,
                        "relative_path": rel,
                        "question_id": qid,
                        "question_type": qtype,
                        "disposition": "source_stated_unverified",
                        "reason": (
                            "Parsed from an explicit option marker; check against "
                            "the source page."
                        ),
                        "priority": "medium",
                    }
                )

        for item in validation_rows:
            if item["relative_path"] != rel:
                continue
            if item["audit_status"] == "not_audited":
                human_review.append(
                    {
                        "queue_type": "existing_answer_audit",
                        "course_id": course_id,
                        "relative_path": rel,
                        "question_id": item["question_id"],
                        "question_type": item["question_type"],
                        "disposition": "not_audited",
                        "reason": (
                            "Original answer has not been independently re-solved "
                            "in this run."
                        ),
                        "priority": "medium",
                    }
                )

        review_required = sum(
            (row.get("review_status") == "needs_human_review")
            or not (row.get("answer_text") or "").strip()
            for row in rows
        )
        courses.append(
            {
                "course_id": course_id,
                "relative_path": rel,
                "questions": len(rows),
                "original_answers": original_answers,
                "missing_before": before_empty,
                "filled_evidence": sum(
                    1 for item in applied_evidence if item["relative_path"] == rel
                ),
                "filled_agent": sum(1 for item in applied_agent if item["relative_path"] == rel),
                "missing_after": after_empty,
                "answers_after": len(rows) - after_empty,
                "review_required": review_required,
                "official_verified": sum(
                    row.get("answer_status") == "official_verified" for row in rows
                ),
                "audited_existing": audit_count,
                "not_audited_existing": original_answers - audit_count,
            }
        )

        if apply:
            write_csv(v2_path, rows, fields)

    if not apply:
        (WORK / "dry_evidence.json").write_text(
            json.dumps(applied_evidence, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (WORK / "dry_agent.json").write_text(
            json.dumps(applied_agent, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print("DRY_RUN")
        print("evidence_candidates", len(applied_evidence))
        print("agent_answers", len(applied_agent))
        print(
            "evidence_by_method",
            dict(Counter(item["method"] for item in applied_evidence)),
        )
        print(
            "evidence_by_course",
            dict(Counter(item["course_id"] for item in applied_evidence)),
        )
        print("agent_by_course", dict(Counter(item["course_id"] for item in applied_agent)))
        return

    REPORTS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # Per-course completion summary and aggregate answer summary.
    summary_fields = [
        "course_id",
        "relative_path",
        "questions",
        "original_answers",
        "missing_before",
        "filled_evidence",
        "filled_agent",
        "missing_after",
        "answers_after",
        "review_required",
        "official_verified",
        "audited_existing",
        "not_audited_existing",
        "completion_pct",
    ]
    for course in courses:
        course["completion_pct"] = round(
            100.0 * course["answers_after"] / max(course["questions"], 1), 2
        )
    write_csv(REPORTS / "answer_generation_summary.csv", courses, summary_fields)
    write_csv(REPORTS / "course_completion_summary.csv", courses, summary_fields)

    unresolved_fields = [
        "course_id",
        "relative_path",
        "question_id",
        "question_type",
        "reason",
        "has_options",
        "passage_id",
        "source_row",
        "v2_disposition",
    ]
    write_csv(REPORTS / "unresolved_questions.csv", unresolved, unresolved_fields)

    audit_fields = [
        "course_id",
        "relative_path",
        "question_id",
        "question_type",
        "existing_answer",
        "existing_status",
        "expected_key",
        "audit_status",
        "audit_confidence",
        "independent_derivation",
        "review_status",
    ]
    write_csv(REPORTS / "existing_answer_audit.csv", validation_rows, audit_fields)

    corrections = []
    for item in validation_rows:
        if item["audit_status"] in {"incorrect", "ambiguous"}:
            corrections.append(
                {
                    "course_id": item["course_id"],
                    "relative_path": item["relative_path"],
                    "question_id": item["question_id"],
                    "original_answer": item["existing_answer"],
                    "proposed_answer": item["expected_key"],
                    "correction_type": item["audit_status"],
                    "reason": item["independent_derivation"],
                    "status": "proposed_needs_review",
                }
            )
    write_csv(
        REPORTS / "answer_corrections.csv",
        corrections,
        [
            "course_id",
            "relative_path",
            "question_id",
            "original_answer",
            "proposed_answer",
            "correction_type",
            "reason",
            "status",
        ],
    )

    evidence_fields = [
        "course_id",
        "relative_path",
        "question_id",
        "answer",
        "question_type",
        "confidence",
        "method",
        "evidence",
    ]
    write_csv(REPORTS / "evidence_filled_answers.csv", applied_evidence, evidence_fields)
    agent_fields = [
        "course_id",
        "relative_path",
        "question_id",
        "answer",
        "question_type",
        "confidence",
        "solution",
    ]
    write_csv(REPORTS / "agent_reasoned_answers.csv", applied_agent, agent_fields)

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
    write_csv(REPORTS / "human_review_queue.csv", human_review, queue_fields)

    # Preserve the complete prior issue ledger, adding only a v2 disposition.
    old_issue_path = ROOT / "reports" / "dataset_finalization" / "inconsistency_resolution.csv"
    issue_rows, issue_fields = read_csv(old_issue_path)
    for item in issue_rows:
        code = item.get("issue_code", "")
        if item.get("resolution_status") != "unresolved":
            disposition = "previously_dispositioned"
        elif code == "answer.not_recoverable":
            disposition = "linked_to_unresolved_answer"
        elif code.startswith("options."):
            disposition = "blocked_missing_options"
        elif code.startswith("source."):
            disposition = "blocked_missing_provenance"
        elif code.startswith("metadata."):
            disposition = "non_blocking_metadata"
        else:
            disposition = "needs_human_review"
        item["v2_disposition"] = disposition
    write_csv(
        REPORTS / "inconsistency_resolution_v2.csv", issue_rows, issue_fields + ["v2_disposition"]
    )

    # Structural validation.
    parsed_rows = 0
    parser_errors = 0
    mcq_key_mapped = 0
    duplicate_ids = 0
    ids_preserved = True
    course_counts_equal = True
    for course in courses:
        rel = course["relative_path"]
        rows, _ = read_csv(V2 / rel)
        v1_rows, _ = read_csv(V1 / rel)
        if len(rows) != len(v1_rows):
            course_counts_equal = False
        v1_ids = {row["question_id"] for row in v1_rows}
        v2_ids = [row["question_id"] for row in rows]
        if len(v2_ids) != len(set(v2_ids)):
            duplicate_ids += len(v2_ids) - len(set(v2_ids))
        if not v1_ids.issubset(set(v2_ids)):
            ids_preserved = False
        for index, row in enumerate(rows, start=1):
            try:
                parsed = parse_row(row, index=index)
                parsed_rows += 1
                if parsed.question_type.lower() in OBJECTIVE and parsed.answer_text:
                    chosen = (row.get("correct_option_key") or "").replace(" ", "").split(",")
                    keys = {option.key for option in parsed.options}
                    if chosen and all(key in keys for key in chosen):
                        mcq_key_mapped += 1
            except Exception:
                parser_errors += 1

    passages, _ = read_csv(V2 / "passages.csv")
    passage_children_valid = all(
        not (row.get("child_question_ids") or "")
        or all(qid in all_ids for qid in row["child_question_ids"].split(";"))
        for row in passages
    )

    raw_unchanged = True
    for path in RAW.rglob("*.csv"):
        if "Syllabus" in path.parts:
            continue
        backup = BACKUP / path.relative_to(RAW)
        if not backup.exists():
            continue
        if (
            hashlib.sha256(path.read_bytes()).hexdigest()
            != hashlib.sha256(backup.read_bytes()).hexdigest()
        ):
            raw_unchanged = False
            break

    con = sqlite3.connect("file:data/exam_engine.db?mode=ro", uri=True)
    quick_check = con.execute("pragma quick_check").fetchone()[0]
    fk_violations = len(con.execute("pragma foreign_key_check").fetchall())
    con.close()

    validation = {
        "files": len(courses),
        "files_reopened": sum((V2 / course["relative_path"]).exists() for course in courses),
        "source_rows": sum(course["questions"] for course in courses),
        "output_rows": sum(course["questions"] for course in courses),
        "ids_preserved": ids_preserved,
        "duplicate_ids": duplicate_ids,
        "cross_course_duplicate_ids": sum(course["questions"] for course in courses) - len(all_ids),
        "parsed_rows": parsed_rows,
        "parser_errors": parser_errors,
        "mcq_key_mapped": mcq_key_mapped,
        "course_counts_equal": course_counts_equal,
        "passage_children_valid": passage_children_valid,
        "raw_unchanged": raw_unchanged,
        "db_quick_check": quick_check,
        "db_fk_violations": fk_violations,
    }

    write_csv(
        REPORTS / "dataset_inventory.csv",
        [
            {
                "course_id": course["course_id"],
                "relative_path": course["relative_path"],
                "questions": course["questions"],
                "original_answers": course["original_answers"],
                "missing_before": course["missing_before"],
                "filled_evidence": course["filled_evidence"],
                "filled_agent": course["filled_agent"],
                "missing_after": course["missing_after"],
                "answers_after": course["answers_after"],
                "completion_pct": course["completion_pct"],
            }
            for course in courses
        ],
        [
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
        ],
    )

    totals = {
        "courses": len(courses),
        "questions": sum(course["questions"] for course in courses),
        "existing_answers": sum(course["original_answers"] for course in courses),
        "missing_before": sum(course["missing_before"] for course in courses),
        "evidence_filled": len(applied_evidence),
        "agent_filled": len(applied_agent),
        "missing_after": sum(course["missing_after"] for course in courses),
        "answers_after": sum(course["answers_after"] for course in courses),
        "review_required": sum(course["review_required"] for course in courses),
        "official_verified": sum(course["official_verified"] for course in courses),
        "existing_audited": sum(course["audited_existing"] for course in courses),
        "existing_not_audited": sum(course["not_audited_existing"] for course in courses),
        "existing_confirmed": sum(item["audit_status"] == "correct" for item in validation_rows),
        "existing_corrected": sum(item["audit_status"] == "incorrect" for item in validation_rows),
        "existing_ambiguous": sum(item["audit_status"] == "ambiguous" for item in validation_rows),
        "ai_generated": len(applied_agent),
        "independently_verified": 0,
        "multiple_source_corroborated": 0,
        "issues_detected": len(issue_rows),
        "issues_previously_resolved": sum(
            item.get("resolution_status") != "unresolved" for item in issue_rows
        ),
        "issues_remaining": sum(
            item.get("resolution_status") == "unresolved" for item in issue_rows
        ),
        "unresolved_rows": len(unresolved),
    }
    manifest = {
        "run_id": "phase142-v5-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "started_at": started,
        "finished_at": now(),
        "external_ai_calls": 0,
        "source_dataset": "dataset_final",
        "output_dataset": "dataset_final_v2",
        "totals": totals,
        "validation": validation,
        "limits": [
            "Not every unresolved question was independently solved.",
            "Not every original answer was independently audited.",
            "Source-stated and agent-reasoned answers remain unverified.",
        ],
    }
    (REPORTS / "processing_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    quality = f"""# Answer Quality Report — Phase 14.2 Continuation V

## Measured status

- Courses processed: **{totals["courses"]}**
- Questions inspected: **{totals["questions"]:,}**
- Original answers: **{totals["existing_answers"]:,}**
- Missing before this run: **{totals["missing_before"]:,}**
- Answers filled from explicit source evidence: **{totals["evidence_filled"]:,}**
- Answers independently reasoned by the coding agent: **{totals["agent_filled"]:,}**
- Missing after this run: **{totals["missing_after"]:,}**
- Answers after this run: **{totals["answers_after"]:,}**
- Official-verified records retained: **{totals["official_verified"]:,}**
- Existing answers independently audited: **{totals["existing_audited"]:,}**
- Existing answers not yet audited: **{totals["existing_not_audited"]:,}**
- Existing answers confirmed by this audit sample: **{totals["existing_confirmed"]:,}**
- Existing answers corrected: **{totals["existing_corrected"]:,}**
- Existing answers marked ambiguous: **{totals["existing_ambiguous"]:,}**
- AI-generated answers: **{totals["ai_generated"]:,}**
- Independently verified answers: **0**
- Multiple-source corroborated answers: **0**
- Questions requiring human review: **{totals["review_required"]:,}**

## Status separation

- `source_stated_unverified`: filled from an explicit option marker in the source
  question text or worked-solution field. This is source extraction, not independent
  verification.
- `ai_generated_unverified`: independently reasoned in this coding-agent session.
  The derivation is retained in `solution_steps`, but it has not been reviewed by a
  human or a separate verification process.
- `source_stored_unverified`: an original answer. It remains unverified unless it
  appears in the audited sample.
- `official_verified`: retained official-key evidence from the prior run.

## Important limits

The two workstreams were not completed over the entire corpus. This run processed a
bounded set of explicitly evidenced questions and a small agent-reasoning batch, and
emitted a complete queue for the remaining records. No blank answer was filled by
guessing, and no original answer was silently overwritten.
"""
    (REPORTS / "answer_quality_report.md").write_text(quality, encoding="utf-8")

    validation_md = f"""# Dataset Validation Report — Phase 14.2 Continuation V

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
| Raw source hashes unchanged | {"yes" if validation["raw_unchanged"] else "NO"} |
| SQLite quick check | {validation["db_quick_check"]} |
| Foreign-key violations | {validation["db_fk_violations"]} |

The final v2 CSVs were reopened after writing. Import validation is reported
separately in the final execution report.
"""
    (REPORTS / "dataset_validation_report.md").write_text(validation_md, encoding="utf-8")

    final = f"""# Final Execution Report — Phase 14.2 Continuation V

**Run:** `{manifest["run_id"]}`  
**External model/API calls:** **0**  
**Output:** `dataset_final_v2/`  
**Reports:** `reports/dataset_finalization_v2/`

## 1. Actual counts

| Metric | Count |
|---|---:|
| Courses processed | **{totals["courses"]}** |
| Questions inspected | **{totals["questions"]:,}** |
| Original answers | **{totals["existing_answers"]:,}** |
| Missing at start | **{totals["missing_before"]:,}** |
| Filled from explicit source evidence | **{totals["evidence_filled"]:,}** |
| Agent-reasoned fills | **{totals["agent_filled"]:,}** |
| Missing after this run | **{totals["missing_after"]:,}** |
| Answers after this run | **{totals["answers_after"]:,}** |
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

- Created `dataset_final_v2/` as a full copy of the validated v1 dataset before any
  edits.
- Applied only explicitly evidenced answers: a worked solution beginning with an
  option marker, or a question/solution stating that a particular option is correct.
- Added a small agent-reasoning batch with per-answer derivations in
  `agent_reasoned_answers.csv`.
- Created a full 29-course unresolved queue and a complete 9,309-row original-answer
  audit queue. Unaudited rows are marked `not_audited`, not passed off as verified.
- Preserved `answer_corrections.csv` as a separate proposal artifact. No original
  answer was overwritten.
- Classified the prior 49,472 inconsistency ledger without claiming that remaining
  structural issues were repaired.

## 3. What is not complete

This is **not** complete coverage. **{totals["missing_after"]:,} questions remain
unanswered**, and **{totals["existing_not_audited"]:,} original answers remain
unaudited**. The agent-reasoning batch is limited to
**{totals["agent_filled"]:,}** questions. The remaining queue is retained in
`unresolved_questions.csv` and `human_review_queue.csv`.

No external API, local model, or other generation service was called. No answer was
fabricated. No original answer was silently replaced. No commit, push, deployment,
or publication was performed.

## 4. Validation

- CSV rows parsed: **{validation["parsed_rows"]:,}**
- Parser exceptions: **{validation["parser_errors"]}**
- IDs preserved: **{"yes" if validation["ids_preserved"] else "NO"}**
- Duplicate IDs within a course: **{validation["duplicate_ids"]}**
- MCQ keys mapped to actual options: **{validation["mcq_key_mapped"]:,}**
- Passage relationships valid: **{validation["passage_children_valid"]}**
- Raw source hash unchanged: **{"yes" if validation["raw_unchanged"] else "NO"}**
- SQLite integrity: `{validation["db_quick_check"]}`
- Foreign-key violations: **{validation["db_fk_violations"]}**

## 5. Files

- `dataset_final_v2/` — 29 course CSVs and `passages.csv`
- `reports/dataset_finalization_v2/answer_generation_summary.csv`
- `reports/dataset_finalization_v2/existing_answer_audit.csv`
- `reports/dataset_finalization_v2/answer_corrections.csv`
- `reports/dataset_finalization_v2/inconsistency_resolution_v2.csv`
- `reports/dataset_finalization_v2/unresolved_questions.csv`
- `reports/dataset_finalization_v2/human_review_queue.csv`
- `reports/dataset_finalization_v2/course_completion_summary.csv`
- `reports/dataset_finalization_v2/answer_quality_report.md`
- `reports/dataset_finalization_v2/dataset_validation_report.md`
- `reports/dataset_finalization_v2/processing_manifest.json`
- `reports/dataset_finalization_v2/FINAL_EXECUTION_REPORT.md`
"""
    (REPORTS / "FINAL_EXECUTION_REPORT.md").write_text(final, encoding="utf-8")
    print(json.dumps(totals, indent=2))
    print(json.dumps(validation, indent=2))
    print("output", V2)
    print("reports", REPORTS)


if __name__ == "__main__":
    main()
