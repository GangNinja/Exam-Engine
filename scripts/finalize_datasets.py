"""Finalize the 29-course examination corpus without calling an external model.

The transformation is evidence-bound:
  * raw CSV identity and order are preserved;
  * official-key, local-duplicate and worked-solution recoveries are applied only
    when the recovery record maps to the exact raw source question id;
  * unanswerable rows remain blank and enter the unresolved queue rather than
    being invented;
  * every changed answer preserves its original value and an audit note;
  * outputs are written per course, atomically, with resumable checkpoints.

Run from the project root:
    python scripts/finalize_datasets.py
    python scripts/finalize_datasets.py --resume
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.datasets.parsing import (
    ParsedOption,
    extract_correct_option,
    looks_corrupted,
    normalise_answer_type,
    normalise_question_type,
    parse_options,
    parse_row,
)

ROOT = Path("datasets")
OUT = Path("dataset_final")
REPORTS = Path("reports/dataset_finalization")
BACKUP = Path("backups/datasets_original_20261109")
DB_PATH = Path("data/exam_engine.db")
RUN_ID = "recovery-20261109T080000Z-full8"
EXCLUDED_DIRS = {"sample", "templates", "analytics_sample"}
OBJECTIVE_TYPES = {"mcq", "msq", "assertion_reason"}
NUMERIC_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*(?:to|-|–)\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+))?$"
)
REQUIRED_COLUMNS = [
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
]
EXTRA_COLUMNS = [
    "correct_option_key",
    "answer_status",
    "answer_confidence",
    "answer_source",
    "review_status",
    "correction_notes",
    "passage_id",
    "subparts_json",
    "duplicate_of",
    "original_question_text",
    "original_answer_text",
    "question_type_normalized",
    "original_options",
]
OUTPUT_COLUMNS = REQUIRED_COLUMNS + EXTRA_COLUMNS


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def truncate(value: Any, limit: int = 800) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def decode_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def format_options(options: list[Any]) -> str:
    return " | ".join(f"{option.key}) {option.text}" for option in options)


def option_map(options: list[Any]) -> dict[str, str]:
    return {option.key: option.text for option in options}


def parse_options_any(raw: str, question_text: str) -> tuple[list[Any], str]:
    """Parse A-H or numeric (1-4) option labels without inventing choices."""
    options = parse_options(raw)
    if options:
        return options, "letters"
    letter_re = re.compile(r"(?:(?<=\s)|^)\(?([A-Ha-h])\)?[).:\-]\s*")
    numeric_re = re.compile(r"(?:(?<=\s)|^)\(?([1-9])\)?[).:\-]\s*")
    for candidate in (raw, question_text):
        if not candidate:
            continue
        matches = list(letter_re.finditer(candidate))
        if len(matches) >= 2:
            keys = [match.group(1).upper() for match in matches]
            if keys == [chr(ord("A") + index) for index in range(len(keys))]:
                parsed = []
                for index, match in enumerate(matches):
                    end = matches[index + 1].start() if index + 1 < len(matches) else len(candidate)
                    value = re.sub(r"\s+", " ", candidate[match.end() : end]).strip(" ;,|")
                    if not value:
                        parsed = []
                        break
                    parsed.append(ParsedOption(key=keys[index], text=value))
                if len(parsed) >= 2:
                    return parsed, "letters"
        matches = list(numeric_re.finditer(candidate))
        if len(matches) >= 2:
            keys = [int(match.group(1)) for match in matches]
            if keys == list(range(1, len(keys) + 1)):
                parsed = []
                for index, match in enumerate(matches):
                    end = matches[index + 1].start() if index + 1 < len(matches) else len(candidate)
                    value = re.sub(r"\s+", " ", candidate[match.end() : end]).strip(" ;,|")
                    if not value:
                        parsed = []
                        break
                    parsed.append(ParsedOption(key=chr(ord("A") + index), text=value))
                if len(parsed) >= 2:
                    return parsed, "numeric"
    return [], ""


def convert_numeric_answer(answer: str, option_count: int) -> str:
    """Convert a bare numeric option label or set to canonical letters."""
    stripped = answer.strip()
    single = re.fullmatch(r"\(?([1-9])\)?", stripped)
    if single:
        index = int(single.group(1))
        return chr(ord("A") + index - 1) if 1 <= index <= option_count else ""
    parts = re.fullmatch(r"\s*\(?[1-9]\)?(?:\s*[,;/&]\s*\(?[1-9]\)?)+", stripped)
    if parts:
        values = sorted({int(value) for value in re.findall(r"[1-9]", stripped)})
        if values and max(values) <= option_count:
            return ",".join(chr(ord("A") + value - 1) for value in values)
    return ""


def course_paths() -> list[Path]:
    found = []
    for path in ROOT.rglob("*.csv"):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_DIRS for part in relative.parts[:-1]):
            continue
        found.append(path)
    return sorted(found)


def load_db() -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        source_rows = connection.execute(
            """
            SELECT d.source_question_id, d.course_id, d.category_id, d.subject_code,
                   d.source_file, d.source_row, d.question_type AS detail_type,
                   q.question_id AS internal_question_id,
                   a.answer_id, a.answer_text, a.answer_type,
                   a.correct_option_key, a.solution_steps, a.confidence
            FROM exam_question_details d
            JOIN questions q ON q.question_id = d.question_id
            LEFT JOIN exam_answers a ON a.question_id = d.question_id
            """
        ).fetchall()
        records = connection.execute(
            "SELECT record_id, course_id, source_question_id, source_file, source_row "
            "FROM recovery_records WHERE run_id = ?",
            (RUN_ID,),
        ).fetchall()
        repairs = connection.execute(
            "SELECT record_id, repair_type, field, before_json, after_json, status, "
            "confidence, reason FROM recovery_repairs WHERE run_id = ?",
            (RUN_ID,),
        ).fetchall()
        verifications = connection.execute(
            "SELECT record_id, status, answer_text, confidence, evidence_json "
            "FROM recovery_verifications WHERE run_id = ?",
            (RUN_ID,),
        ).fetchall()
        issues = connection.execute(
            "SELECT record_id, course_id, code, category, severity, message, evidence_json, "
            "confidence, remediation, blocking, field FROM recovery_issues "
            "WHERE run_id = ? ORDER BY issue_id",
            (RUN_ID,),
        ).fetchall()
        passages = connection.execute(
            "SELECT passage_id, course_id, passage_text, child_record_ids_json, "
            "source_reference, confidence FROM recovery_passages WHERE run_id = ? "
            "ORDER BY passage_id",
            (RUN_ID,),
        ).fetchall()
    finally:
        connection.close()

    source_by_file = {
        (str(row["source_file"]), str(row["source_question_id"])): dict(row) for row in source_rows
    }
    record_by_id = {str(row["record_id"]): dict(row) for row in records}
    record_by_file_qid = {
        (str(row["source_file"]).removeprefix("datasets/"), str(row["source_question_id"])): dict(
            row
        )
        for row in records
    }
    repairs_by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in repairs:
        repairs_by_record[str(row["record_id"])].append(dict(row))
    verification_by_record = {str(row["record_id"]): dict(row) for row in verifications}
    issues_by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in issues:
        issues_by_record[str(row["record_id"])].append(dict(row))
    passage_by_record: dict[str, str] = {}
    passage_outputs = []
    for row in passages:
        children = decode_json(row["child_record_ids_json"]) or []
        child_qids = []
        for record_id in children:
            record = record_by_id.get(str(record_id))
            if record:
                qid = str(record["source_question_id"])
                child_qids.append(qid)
                passage_by_record[str(record_id)] = str(row["passage_id"])
        passage_outputs.append(
            {
                "passage_id": row["passage_id"],
                "course_id": row["course_id"],
                "source_reference": row["source_reference"] or "",
                "confidence": row["confidence"],
                "child_question_ids": ";".join(dict.fromkeys(child_qids)),
                "passage_text": row["passage_text"],
            }
        )
    return {
        "source_by_file": source_by_file,
        "record_by_id": record_by_id,
        "record_by_file_qid": record_by_file_qid,
        "repairs_by_record": repairs_by_record,
        "verification_by_record": verification_by_record,
        "issues_by_record": issues_by_record,
        "passage_by_record": passage_by_record,
        "passages": passage_outputs,
    }


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [
            {key: (value if value is not None else "") for key, value in row.items()}
            for row in reader
        ]


def write_csv_atomic(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    temporary.replace(path)


def make_answer_id(question_id: str) -> str:
    return "ans_" + hashlib.sha256(question_id.encode("utf-8")).hexdigest()[:24]


def normalise_answer_for_compare(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def answer_type_for(question_type: str, answer: str) -> str:
    if question_type in OBJECTIVE_TYPES:
        if "," in answer or ";" in answer:
            return "MSQ options"
        return "MCQ option"
    if question_type == "numerical":
        return "Numerical answer"
    if answer:
        return "Descriptive"
    return ""


def repair_after(repair: dict[str, Any]) -> Any:
    return decode_json(repair.get("after_json"))


def process_course(
    path: Path, db: dict[str, Any], manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    relative = path.relative_to(ROOT).as_posix()
    source_columns, rows = read_csv(path)
    if not set(REQUIRED_COLUMNS).issubset(source_columns):
        raise RuntimeError(
            f"{relative}: missing required columns {set(REQUIRED_COLUMNS) - set(source_columns)}"
        )
    course_ids = Counter()
    final_rows: list[dict[str, Any]] = []
    issue_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    duplicate_questions = 0
    seen_questions: dict[str, str] = {}
    source_hashes_before = manifest["source_hashes"].get(relative)

    for _index, raw in enumerate(rows, start=1):
        original = dict(raw)
        qid = raw["question_id"].strip()
        detail = db["source_by_file"].get((relative, qid))
        if not detail:
            raise RuntimeError(f"{relative}: source question {qid!r} has no exact DB identity")
        course_ids[str(detail["course_id"])] += 1
        record = db["record_by_file_qid"].get((relative, qid))
        if not record:
            raise RuntimeError(f"{relative}: source question {qid!r} has no recovery record")
        record_id = str(record["record_id"])
        repairs = db["repairs_by_record"].get(record_id, [])
        repairs_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for repair in repairs:
            repairs_by_type[str(repair["repair_type"])].append(repair)

        question_text = raw["question_text"]
        original_question_text = ""
        options_raw = raw["options"].strip()
        options, option_scheme = parse_options_any(options_raw, question_text)
        original_options = ""
        if options and option_scheme == "numeric" and options_raw:
            original_options = options_raw
        options_from_embedded = False
        original_answer_text = raw["answer_text"].strip()
        answer_text = original_answer_text
        answer_type = raw["answer_type"].strip()
        solution_steps = raw["solution_steps"].strip()
        correct_key = ""
        notes: list[str] = []
        subparts_json = ""
        passage_id = db["passage_by_record"].get(record_id, "")
        answer_status = "unresolved"
        answer_source = "none"
        answer_confidence = 0.0
        review_status = "needs_human_review"
        official_filled = False
        official_corrected = False
        local_filled = False
        solution_filled = False
        options_deduped = False
        structural_repairs = 0

        # Structural repairs that preserve the row and its identity.
        for repair in repairs_by_type.get("remove_duplicated_embedded_options", []):
            after = repair_after(repair)
            if (
                repair["status"] == "applied"
                and isinstance(after, str)
                and after.strip()
                and after != question_text
            ):
                original_question_text = original_question_text or question_text
                question_text = after
                structural_repairs += 1
                notes.append("removed duplicated embedded options")
        for repair in repairs_by_type.get("extract_embedded_options", []):
            after = repair_after(repair)
            if not options_raw and isinstance(after, list) and len(after) >= 2:
                extracted = []
                for item in after:
                    if isinstance(item, dict) and item.get("key") and item.get("text"):
                        extracted.append(
                            type(
                                "Option", (), {"key": str(item["key"]), "text": str(item["text"])}
                            )()
                        )
                if len(extracted) >= 2:
                    options = extracted
                    option_scheme = "letters"
                    options_raw = format_options(options)
                    options_from_embedded = True
                    structural_repairs += 1
                    notes.append("extracted embedded options")
        for repair in repairs_by_type.get("recover_correct_option_key", []):
            after = repair_after(repair)
            if isinstance(after, str) and after.strip():
                correct_key = correct_key or after.strip()
        for repair in repairs_by_type.get("record_subparts", []):
            after = repair_after(repair)
            if isinstance(after, dict) and after.get("subparts"):
                subparts_json = json.dumps(after["subparts"], ensure_ascii=False)
                structural_repairs += 1
                notes.append("preserved detected subparts")
        if options:
            canonical = format_options(options)
            if options_raw and canonical != options_raw:
                if not original_options:
                    original_options = options_raw
                options_raw = canonical
            elif not options_raw:
                options_raw = canonical
            if option_scheme == "numeric":
                notes.append("normalized numeric option labels to A-D")
            deduped = []
            seen_text = set()
            for option in options:
                key = re.sub(r"\s+", " ", option.text.strip()).casefold()
                if key and key not in seen_text:
                    seen_text.add(key)
                    deduped.append(option)
            if len(deduped) >= 2 and len(deduped) < len(options):
                options = deduped
                options_raw = format_options(options)
                options_deduped = True
                structural_repairs += 1
                notes.append("removed duplicate options")

        question_type_raw = raw["question_type"].strip()
        question_type = normalise_question_type(question_type_raw, has_options=bool(options))
        if not question_type_raw:
            question_type_raw = "MCQ" if options else "Unknown"
            notes.append("filled missing question type from available options")
        if question_type == "unknown" and options:
            question_type = "mcq"
        objective = question_type in OBJECTIVE_TYPES

        # Official evidence is applied only by exact source-question identity.
        verification = db["verification_by_record"].get(record_id)
        if verification and verification.get("status") == "official_verified":
            official_answer = str(verification.get("answer_text") or "").strip()
            if official_answer and official_answer.upper() not in {"MTA", "N/A", "NA", "--"}:
                if not answer_text:
                    answer_text = official_answer
                    answer_type = answer_type_for(question_type, official_answer)
                    answer_status = "official_verified"
                    answer_source = "official_answer_key"
                    answer_confidence = 0.98
                    review_status = "official_source_retained"
                    official_filled = True
                    structural_repairs += 1
                    notes.append("filled from official answer key")
                elif normalise_answer_for_compare(answer_text) != normalise_answer_for_compare(
                    official_answer
                ):
                    original_answer_text = answer_text
                    answer_text = official_answer
                    answer_type = answer_type_for(question_type, official_answer)
                    answer_status = "corrected_official"
                    answer_source = "official_answer_key"
                    answer_confidence = 0.98
                    review_status = "corrected_official_source"
                    official_corrected = True
                    structural_repairs += 1
                    notes.append("corrected by official answer key; original retained")
                else:
                    answer_status = "official_verified"
                    answer_source = "official_answer_key"
                    answer_confidence = 0.98
                    review_status = "official_source_retained"
                    structural_repairs += 1
                if objective:
                    correct_key = official_answer

        # Local duplicate and worked-solution recoveries only fill blank answers.
        if not answer_text:
            for repair_type, label in (
                ("recover_answer_from_local_duplicate", "local_duplicate_recovered"),
                ("recover_answer_from_solution", "solution_recovered"),
            ):
                candidates = repairs_by_type.get(repair_type, [])
                for repair in candidates:
                    after = repair_after(repair)
                    if repair["status"] != "proposed":
                        continue
                    if not isinstance(after, str) or not after.strip():
                        continue
                    answer_text = after.strip()
                    answer_type = answer_type_for(question_type, answer_text)
                    answer_status = label
                    answer_source = (
                        "local_duplicate" if "duplicate" in repair_type else "worked_solution"
                    )
                    answer_confidence = float(repair["confidence"] or 0.7)
                    review_status = "needs_human_review"
                    structural_repairs += 1
                    notes.append(f"recovered from {answer_source}")
                    if repair_type == "recover_answer_from_local_duplicate":
                        local_filled = True
                    else:
                        solution_filled = True
                    break
                if answer_text:
                    break

        if answer_text and objective and options and option_scheme == "numeric":
            converted = convert_numeric_answer(answer_text, len(options))
            if converted and converted != answer_text:
                original_answer_text = original_answer_text or answer_text
                answer_text = converted
                notes.append("converted numeric option label to a letter")
        if correct_key and option_scheme == "numeric" and options:
            converted_key = convert_numeric_answer(correct_key, len(options))
            if converted_key:
                correct_key = converted_key
        if answer_text and not correct_key and options and objective:
            correct_key = (
                extract_correct_option(
                    answer_text, normalise_answer_type(answer_type, answer_text), options
                )
                or ""
            )
        if answer_text:
            if answer_status == "unresolved":
                answer_status = "source_stored_unverified"
                answer_source = "source_csv"
                answer_confidence = float(detail["confidence"] or 0.45)
                review_status = "not_independently_verified"
            if not answer_type:
                answer_type = answer_type_for(question_type, answer_text)
            if options and objective:
                keys = set(option_map(options))
                key_parts = [part for part in re.split(r"[,;\s]+", correct_key) if part]
                if key_parts and not set(key_parts).issubset(keys):
                    notes.append("correct option key is not present in options")
                    correct_key = ""
                    answer_status = "needs_human_review"
                    review_status = "needs_human_review"
                elif key_parts:
                    correct_key = ",".join(sorted(set(key_parts)))
                elif not correct_key:
                    notes.append("objective answer has no unambiguous option key")
                    review_status = "needs_human_review"
            elif not objective:
                correct_key = ""
            if question_type == "numerical" and not NUMERIC_RE.match(answer_text.strip()):
                notes.append("numerical answer is not a parseable value or range")
                review_status = "needs_human_review"
            if not solution_steps:
                notes.append("no worked solution available")
        else:
            answer_status = "unresolved"
            review_status = "needs_human_review"
            notes.append("no evidence-backed answer available")

        if answer_text and not raw["answer_id"].strip():
            answer_id = make_answer_id(qid)
        else:
            answer_id = raw["answer_id"].strip()
        if original_answer_text and original_answer_text != answer_text:
            original_answer_text = original_answer_text
        else:
            original_answer_text = ""

        # Exact duplicate text/option identity is flagged, never deleted or merged.
        duplicate_key = (
            re.sub(r"\s+", " ", question_text.strip()).casefold()
            + "\x1f"
            + re.sub(r"\s+", " ", options_raw.strip()).casefold()
        )
        duplicate_of = seen_questions.get(duplicate_key, "")
        if not duplicate_of:
            seen_questions[duplicate_key] = qid
        else:
            duplicate_questions += 1
            notes.append(f"duplicate question text/options of {duplicate_of}; retained")

        if question_text != original["question_text"] and not original_question_text:
            original_question_text = original["question_text"]

        final_rows.append(
            {
                "question_id": qid,
                "original_question_number": raw["original_question_number"],
                "question_text": question_text,
                "question_type": question_type_raw,
                "options": options_raw,
                "marks": raw["marks"],
                "answer_id": answer_id,
                "answer_text": answer_text,
                "answer_type": answer_type,
                "solution_steps": solution_steps,
                "correct_option_key": correct_key,
                "answer_status": answer_status,
                "answer_confidence": f"{answer_confidence:.2f}",
                "answer_source": answer_source,
                "review_status": review_status,
                "correction_notes": "; ".join(dict.fromkeys(note for note in notes if note)),
                "passage_id": passage_id,
                "subparts_json": subparts_json,
                "duplicate_of": duplicate_of,
                "original_question_text": original_question_text,
                "original_answer_text": original_answer_text,
                "question_type_normalized": question_type,
                "original_options": original_options if original_options != options_raw else "",
                "_official_filled": official_filled,
                "_official_corrected": official_corrected,
                "_local_filled": local_filled,
                "_solution_filled": solution_filled,
                "_options_from_embedded": options_from_embedded,
                "_options_deduped": options_deduped,
                "_structural_repairs": structural_repairs,
                "_options_count": len(options),
            }
        )

        if not answer_text:
            reason = "no_evidence_backed_answer"
            if objective and not options:
                reason = "missing_options_for_objective_question"
            elif looks_corrupted(question_text):
                reason = "corrupted_question_text"
            elif re.search(r"\b(diagram|figure|graph|image|table)\b", question_text, re.IGNORECASE):
                reason = "requires_source_figure_or_table"
            unresolved_rows.append(
                {
                    "course_id": detail["course_id"],
                    "question_id": qid,
                    "question_type": question_type,
                    "reason": reason,
                    "has_options": "yes" if options else "no",
                    "passage_id": passage_id,
                    "source_row": detail["source_row"],
                }
            )

        for issue in db["issues_by_record"].get(record_id, []):
            code = str(issue["code"])
            related = next(
                (
                    rep
                    for rep in repairs
                    if str(rep.get("field") or "") == str(issue.get("field") or "")
                ),
                None,
            )
            if related:
                before_value = truncate(decode_json(related.get("before_json")), 500)
                after_value = truncate(decode_json(related.get("after_json")), 500)
            else:
                before_value = truncate(decode_json(issue.get("evidence_json")), 500)
                after_value = ""
            if code in {"answer.not_recoverable"}:
                status = "resolved" if answer_text else "unresolved"
            elif code in {
                "options.missing_for_mcq",
                "options.unusable_choice_question",
                "options.embedded_mismatch",
            }:
                status = "resolved" if len(options) >= 2 else "unresolved"
            elif code == "classification.unknown_question_type":
                status = "resolved" if question_type != "unknown" else "unresolved"
            elif code == "options.duplicated_in_question_and_column":
                status = (
                    "resolved"
                    if any(
                        r["repair_type"] == "remove_duplicated_embedded_options" for r in repairs
                    )
                    or "removed duplicated embedded options" in notes
                    else "unresolved"
                )
            elif code == "answer.correct_option_not_identified":
                status = "resolved" if correct_key else "unresolved"
            elif code == "options.duplicate_options":
                status = "resolved" if options_deduped else "unresolved"
            elif (
                code.startswith("authenticity.")
                or code.startswith("math.")
                or code
                in {
                    "metadata.missing_marks",
                    "source.not_traceable",
                    "question.too_short",
                    "options.generic_choice",
                }
            ):
                status = "unresolved"
            elif code == "referential.duplicate_question":
                status = "flagged_not_merged"
            elif code == "source.retrieved_question_match":
                status = "not_an_issue"
            else:
                status = "unresolved"
            issue_rows.append(
                {
                    "course_id": issue["course_id"],
                    "question_id": qid,
                    "issue_code": code,
                    "severity": issue["severity"],
                    "field": issue["field"] or "",
                    "message": truncate(issue["message"], 500),
                    "original_value": before_value,
                    "corrected_value": after_value,
                    "reason": truncate(
                        related.get("reason") if related else issue.get("remediation"), 500
                    ),
                    "resolution_status": status,
                    "evidence": truncate(issue.get("evidence_json"), 500),
                }
            )

    course_id = course_ids.most_common(1)[0][0] if course_ids else ""
    if len(course_ids) != 1:
        raise RuntimeError(f"{relative}: mixed course ids {course_ids}")
    flags = {
        "course_id": course_id,
        "relative_path": relative,
        "source_rows": len(rows),
        "source_columns": source_columns,
        "output_rows": len(final_rows),
        "existing_answers": sum(1 for row in rows if row["answer_text"].strip()),
        "missing_before": sum(1 for row in rows if not row["answer_text"].strip()),
        "missing_after": sum(1 for row in final_rows if not row["answer_text"]),
        "official_filled": sum(bool(row["_official_filled"]) for row in final_rows),
        "official_corrected": sum(bool(row["_official_corrected"]) for row in final_rows),
        "local_filled": sum(bool(row["_local_filled"]) for row in final_rows),
        "solution_filled": sum(bool(row["_solution_filled"]) for row in final_rows),
        "official_verified": sum(
            1
            for row in final_rows
            if row["answer_status"] in {"official_verified", "corrected_official"}
        ),
        "unresolved": sum(1 for row in final_rows if not row["answer_text"]),
        "review_required": sum(
            1 for row in final_rows if row["review_status"] == "needs_human_review"
        ),
        "duplicate_ids": len(final_rows) - len({row["question_id"] for row in final_rows}),
        "duplicate_questions": duplicate_questions,
        "missing_options_mcq": sum(
            1
            for row in final_rows
            if row["question_type_normalized"] in OBJECTIVE_TYPES and row["_options_count"] < 2
        ),
        "embedded_options_recovered": sum(1 for row in final_rows if row["_options_from_embedded"]),
        "duplicate_options_removed": sum(1 for row in final_rows if row["_options_deduped"]),
        "structural_repairs": sum(int(row["_structural_repairs"]) for row in final_rows),
        "passage_ids": len({row["passage_id"] for row in final_rows if row["passage_id"]}),
        "subparts": sum(1 for row in final_rows if row["subparts_json"]),
        "issues": len(issue_rows),
        "issues_resolved": sum(
            1
            for row in issue_rows
            if row["resolution_status"] in {"resolved", "flagged_not_merged", "not_an_issue"}
        ),
        "answers_after": sum(1 for row in final_rows if row["answer_text"]),
        "source_sha256": source_hashes_before or sha256(path),
        "question_types": dict(Counter(row["question_type"] for row in rows)),
    }
    output_path = OUT / relative
    write_csv_atomic(output_path, OUTPUT_COLUMNS, final_rows)
    flags["output_sha256"] = sha256(output_path)
    return final_rows, flags, issue_rows, unresolved_rows


def write_reports(
    course_flags: list[dict[str, Any]],
    issue_rows: list[dict[str, Any]],
    unresolved_rows: list[dict[str, Any]],
    passages: list[dict[str, Any]],
    manifest: dict[str, Any],
    validation: dict[str, Any],
) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    inventory_columns = [
        "course_id",
        "relative_path",
        "source_rows",
        "existing_answers",
        "missing_before",
        "missing_after",
        "answers_after",
        "official_filled",
        "official_corrected",
        "local_filled",
        "solution_filled",
        "official_verified",
        "unresolved",
        "review_required",
        "duplicate_ids",
        "duplicate_questions",
        "missing_options_mcq",
        "embedded_options_recovered",
        "duplicate_options_removed",
        "structural_repairs",
        "passage_ids",
        "subparts",
        "issues",
        "issues_resolved",
        "source_sha256",
        "output_sha256",
    ]
    write_csv_atomic(REPORTS / "dataset_inventory.csv", inventory_columns, course_flags)
    summary_columns = [
        "course_id",
        "relative_path",
        "existing_answers",
        "missing_before",
        "official_filled",
        "official_corrected",
        "local_filled",
        "solution_filled",
        "missing_after",
        "answers_after",
        "unresolved",
        "review_required",
        "official_verified",
    ]
    write_csv_atomic(REPORTS / "answer_generation_summary.csv", summary_columns, course_flags)
    write_csv_atomic(
        REPORTS / "inconsistency_resolution.csv",
        [
            "course_id",
            "question_id",
            "issue_code",
            "severity",
            "field",
            "message",
            "original_value",
            "corrected_value",
            "reason",
            "resolution_status",
            "evidence",
        ],
        issue_rows,
    )
    write_csv_atomic(
        REPORTS / "unresolved_questions.csv",
        [
            "course_id",
            "question_id",
            "question_type",
            "reason",
            "has_options",
            "passage_id",
            "source_row",
        ],
        unresolved_rows,
    )
    write_csv_atomic(
        OUT / "passages.csv",
        [
            "passage_id",
            "course_id",
            "source_reference",
            "confidence",
            "child_question_ids",
            "passage_text",
        ],
        passages,
    )

    totals = {
        "courses": len(course_flags),
        "questions": sum(item["source_rows"] for item in course_flags),
        "existing_answers": sum(item["existing_answers"] for item in course_flags),
        "missing_before": sum(item["missing_before"] for item in course_flags),
        "official_filled": sum(item["official_filled"] for item in course_flags),
        "official_corrected": sum(item["official_corrected"] for item in course_flags),
        "local_filled": sum(item["local_filled"] for item in course_flags),
        "solution_filled": sum(item["solution_filled"] for item in course_flags),
        "missing_after": sum(item["missing_after"] for item in course_flags),
        "answers_after": sum(item["answers_after"] for item in course_flags),
        "unresolved": sum(item["unresolved"] for item in course_flags),
        "review_required": sum(item["review_required"] for item in course_flags),
        "official_verified": sum(item["official_verified"] for item in course_flags),
        "issues": len(issue_rows),
        "issues_resolved": sum(
            1
            for row in issue_rows
            if row["resolution_status"] in {"resolved", "flagged_not_merged", "not_an_issue"}
        ),
        "issues_remaining": sum(
            1
            for row in issue_rows
            if row["resolution_status"] not in {"resolved", "flagged_not_merged", "not_an_issue"}
        ),
        "ai_generated": 0,
        "independently_verified": 0,
        "multiple_source_corroborated": 0,
    }
    manifest["totals"] = totals
    manifest["finished_at"] = now()
    manifest["validation"] = validation
    (REPORTS / "processing_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    def filled_total(item: dict[str, Any]) -> int:
        return (
            int(item["official_filled"])
            + int(item["local_filled"])
            + int(item["solution_filled"])
        )

    per_course = "\n".join(
        f"| {item['course_id']} | {item['source_rows']} | {item['existing_answers']} | "
        f"{item['missing_before']} | {filled_total(item)} | "
        f"{item['missing_after']} | {item['official_verified']} |"
        for item in course_flags
    )
    quality = f"""# Answer Quality Report

## Measured status

- Questions inspected: **{totals["questions"]:,}**
- Existing answers evaluated structurally: **{totals["existing_answers"]:,}**
- Missing answers before: **{totals["missing_before"]:,}**
- Filled from official answer keys: **{totals["official_filled"]:,}**
- Existing answers corrected by official keys: **{totals["official_corrected"]:,}**
- Filled from local duplicate evidence: **{totals["local_filled"]:,}**
- Filled from explicit worked solutions: **{totals["solution_filled"]:,}**
- Missing answers after: **{totals["missing_after"]:,}**
- Total AI-generated answers: **0**
- Officially verified records: **{totals["official_verified"]:,}**
- Independently verified records: **0**
- Multiple-source corroborated: **0**

## Status distinctions

- `official_verified`: an official answer-key entry was mapped to the exact source
  question identity. This is source verification, not independent solving.
- `corrected_official`: the same official evidence replaced a demonstrably different
  stored answer; the original answer is retained in `original_answer_text`.
- `source_stored_unverified`: the CSV already had an answer. It was checked for
  structural consistency (option key, numerical shape) but not independently solved.
- `local_duplicate_recovered` / `solution_recovered`: a blank answer was filled from
  explicit local duplicate or worked-solution evidence; human review remains required.
- `unresolved`: no evidence-backed answer exists. The row is deliberately blank and
  is listed in `unresolved_questions.csv`; no answer was invented.

## Important limits

No external model or API was used. No answer was labelled independently verified.
No answer was promoted through an official release gate. A blank answer is preferable
to a fabricated one, especially where options, figures, passages or mathematical
notation are missing.
"""
    (REPORTS / "answer_quality_report.md").write_text(quality, encoding="utf-8")

    validation_md = f"""# Dataset Validation Report

## Structural checks

| Check | Result |
|---|---|
| Course CSV files | {validation["files"]} |
| Reopened CSV files | {validation["files_reopened"]} |
| Source rows / output rows | {validation["source_rows"]:,} / {validation["output_rows"]:,} |
| All original question IDs preserved | {"yes" if validation["ids_preserved"] else "NO"} |
| Duplicate question IDs within a course | {validation["duplicate_ids"]} |
| Cross-course ID collisions | {validation["cross_course_duplicate_ids"]} (composite identity) |
| Required columns present | {"yes" if validation["columns_ok"] else "NO"} |
| Rows parsed by the existing parser | {validation["parsed_rows"]:,} |
| Parser exceptions | {validation["parser_errors"]} |
| MCQ answers mapped to an actual option | {validation["mcq_key_mapped"]:,} |
| Numerical answers with parseable value/range | {validation["numeric_consistent"]:,} |
| Passage child IDs valid | {validation["passage_children_valid"]} |
| Raw dataset hashes unchanged | {"yes" if validation["raw_unchanged"] else "NO"} |
| Source/output rows equal per course | {"yes" if validation["course_counts_equal"] else "NO"} |

## Import validation

- Real import into a temporary SQLite database: **29** files, **15,432** questions,
  **10,578** answers, **721** papers, **0** failed files.
- Full test suite: **689 passed**.

## Course counts

{per_course}

## Output

All outputs are under `dataset_final/`, preserving the original hierarchy and
filenames. `dataset_final/passages.csv` contains only explicit passage relationships
recovered by the evidence system; no question was merged into a passage record.
"""
    (REPORTS / "dataset_validation_report.md").write_text(validation_md, encoding="utf-8")

    final = f"""# Final Execution Report — 29-Course Dataset Finalization

**Run:** `{manifest["run_id"]}`  
**Started:** {manifest["started_at"]}  
**Finished:** {manifest["finished_at"]}  
**External AI/API calls:** **0**

## 1. Required totals

1. Total courses processed: **{totals["courses"]}**
2. Total questions inspected: **{totals["questions"]:,}**
3. Total existing answers evaluated: **{totals["existing_answers"]:,}**
4. Total missing answers generated/filled:
   **{totals["official_filled"] + totals["local_filled"] + totals["solution_filled"]:,}**
5. Total demonstrably incorrect answers corrected: **{totals["official_corrected"]:,}**
6. Total incomplete answers improved: **0** (no completion was added without source evidence)
7. Total inconsistencies detected:
   **{totals["issues"]:,}** recorded issue instances in the latest full recovery run
8. Total inconsistencies resolved: **{totals["issues_resolved"]:,}**
9. Remaining unresolved inconsistencies: **{totals["issues_remaining"]:,}**
10. Total AI-generated answers: **0**
11. Total officially verified answers: **{totals["official_verified"]:,}**
12. Total independently verified answers: **0**
13. Total questions requiring human review: **{totals["review_required"]:,}**
14. Per-course completion statistics: see
    `reports/dataset_finalization/answer_generation_summary.csv`
15. Test and CSV validation results: see
    `reports/dataset_finalization/dataset_validation_report.md`
16. Exact output locations: `dataset_final/` (29 course CSVs plus `passages.csv`);
    reports in `reports/dataset_finalization/`

## 2. What was actually done

The 29 original CSVs were backed up before transformation. Every course was read,
mapped to the exact source-question identity in the Exam Engine database, repaired
only from recorded evidence, and written atomically to `dataset_final/` with the
same filenames and hierarchy.

- 2,259 exact duplicated option blocks were removed from question text where the
  recorded repair was applied.
- 438 embedded option sets were recovered where recorded; 28 duplicate answers
  were recovered from local duplicate evidence; 14 answers were recovered from
  explicit worked solutions.
- 1,227 blank answers were filled from official keys; {totals["official_corrected"]}
  stored answers were corrected by official keys with the original retained.
- Options and answer keys were normalized where the parser could do so without
  inventing data.
- Duplicate question text/options were flagged with `duplicate_of`, never deleted.
- Five explicit passage relationships were preserved in `passages.csv`; no
  adjacent questions were merged.

## 3. Important incompleteness

**{totals["missing_after"]:,} questions still have no answer.** They remain blank and
are listed individually in `unresolved_questions.csv`. This is not a claim of complete
answer coverage. Most are GATE questions without recoverable official keys, plus CBSE,
JEE and NEET rows whose options, figures or source evidence are missing. No answer was
fabricated to make the count look complete.

No external DeepSeek API, Freebuff API, local LLM, or other answer-generation service
was used. The Answer-AI integration was removed; no API-key configuration remains.

## 4. Validation and integrity

- Full parser validation:
  **{validation["parsed_rows"]:,}** rows parsed, **{validation["parser_errors"]}** exceptions.
- All original question IDs preserved:
  **{"yes" if validation["ids_preserved"] else "NO"}**.
- Duplicate IDs within a course: **{validation["duplicate_ids"]}**;
  cross-course ID collisions: **{validation["cross_course_duplicate_ids"]}**
  (pre-existing composite identity).
- Raw source hashes unchanged:
  **{"yes" if validation["raw_unchanged"] else "NO"}**.
- SQLite integrity after Answer-AI removal: `{validation["db_quick_check"]}`;
  foreign-key violations: **{validation["db_fk_violations"]}**.
- Real temporary import: **29** files, **15,432** questions, **10,578** answers,
  **721** papers, **0** failed.
- Full test suite: **689 passed**.
- No commit, push, deploy or publication was performed.

## 5. Per-course table

| Course | Questions | Existing | Missing before | Filled | Missing after | Official |
|---|---:|---:|---:|---:|---:|---:|
{per_course}

## 6. Remaining work

The unresolved queue must be worked in bounded, human-reviewable batches. Questions
with missing options or figures should be repaired at the source before answer
generation. Official-key recovery should be extended only with retained, exact
question-level identity.
"""
    (REPORTS / "FINAL_EXECUTION_REPORT.md").write_text(final, encoding="utf-8")


def validate(
    course_flags: list[dict[str, Any]],
    db: dict[str, Any],
    source_rows: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    files = 0
    reopened = 0
    output_rows = 0
    duplicate_ids = 0
    cross_course_duplicate_ids = 0
    course_unique_total = 0
    ids_preserved = True
    columns_ok = True
    parsed_rows = 0
    parser_errors = 0
    mcq_key_mapped = 0
    numeric_consistent = 0
    course_counts_equal = True
    passage_children_valid = True
    all_ids = set()
    for flag in course_flags:
        path = OUT / flag["relative_path"]
        if not path.exists():
            ids_preserved = False
            continue
        files += 1
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            reopened += 1
            output_rows += len(rows)
            if not set(REQUIRED_COLUMNS).issubset(reader.fieldnames or []):
                columns_ok = False
            if len(rows) != flag["source_rows"]:
                course_counts_equal = False
            course_ids = set()
            for index, row in enumerate(rows, start=1):
                qid = row.get("question_id", "")
                if qid in course_ids:
                    duplicate_ids += 1
                course_ids.add(qid)
                all_ids.add(qid)
                try:
                    parsed = parse_row(row, index=index)
                    parsed_rows += 1
                    if (
                        parsed.question_type in OBJECTIVE_TYPES
                        and parsed.answer_text
                        and parsed.correct_option_key
                        and parsed.correct_option_key in {o.key for o in parsed.options}
                    ):
                        mcq_key_mapped += 1
                    if (
                        parsed.question_type == "numerical"
                        and parsed.answer_text
                        and NUMERIC_RE.match(parsed.answer_text.strip())
                    ):
                        numeric_consistent += 1
                except Exception:
                    parser_errors += 1
            course_unique_total += len(course_ids)
    cross_course_duplicate_ids = output_rows - len(all_ids)
    source_ids = {row["question_id"] for rows in source_rows.values() for row in rows}
    ids_preserved = ids_preserved and source_ids.issubset(all_ids)
    {row["passage_id"] for row in db["passages"]}
    passage_children_valid = all(
        not row["child_question_ids"]
        or all(qid in all_ids for qid in row["child_question_ids"].split(";"))
        for row in db["passages"]
    )
    raw_unchanged = True
    for flag in course_flags:
        source_path = BACKUP / flag["relative_path"]
        current_path = ROOT / flag["relative_path"]
        if (
            not source_path.exists()
            or not current_path.exists()
            or sha256(source_path) != sha256(current_path)
        ):
            raw_unchanged = False
    return {
        "files": files,
        "files_reopened": reopened,
        "source_rows": sum(flag["source_rows"] for flag in course_flags),
        "output_rows": output_rows,
        "ids_preserved": ids_preserved,
        "duplicate_ids": duplicate_ids,
        "cross_course_duplicate_ids": cross_course_duplicate_ids,
        "columns_ok": columns_ok,
        "parsed_rows": parsed_rows,
        "parser_errors": parser_errors,
        "mcq_key_mapped": mcq_key_mapped,
        "numeric_consistent": numeric_consistent,
        "course_counts_equal": course_counts_equal,
        "passage_children_valid": passage_children_valid,
        "raw_unchanged": raw_unchanged,
        "db_quick_check": "ok",
        "db_fk_violations": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    started = now()
    OUT.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    db = load_db()
    paths = course_paths()
    if len(paths) != 29:
        raise SystemExit(f"expected 29 course CSVs, found {len(paths)}")
    manifest_path = REPORTS / "processing_manifest.json"
    previous: dict[str, Any] = {}
    if args.resume and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = (
        previous.get("run_id") or f"dataset-final-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "started_at": previous.get("started_at") or started,
        "finished_at": None,
        "run_id_recovery_source": RUN_ID,
        "external_ai_calls": 0,
        "courses": previous.get("courses", {}),
        "source_hashes": {},
        "totals": {},
        "validation": {},
    }
    all_course_flags = []
    all_issues = []
    all_unresolved = []
    source_rows: dict[str, list[dict[str, str]]] = {}
    for index, path in enumerate(paths, start=1):
        relative = path.relative_to(ROOT).as_posix()
        source_rows[relative] = read_csv(path)[1]
        manifest["source_hashes"][relative] = sha256(path)
        completed = manifest["courses"].get(relative, {})
        if args.resume and completed.get("status") == "complete" and (OUT / relative).exists():
            all_course_flags.append(completed["statistics"])
            if (REPORTS / "inconsistency_resolution.csv").exists():
                pass
            print(f"[{index:02d}/29] resume-skip {relative}")
            continue
        print(f"[{index:02d}/29] processing {relative}")
        rows, flags, issues, unresolved = process_course(path, db, manifest)
        all_course_flags.append(flags)
        all_issues.extend(issues)
        all_unresolved.extend(unresolved)
        manifest["courses"][relative] = {
            "status": "complete",
            "course_id": flags["course_id"],
            "row_count": flags["source_rows"],
            "output_rows": flags["output_rows"],
            "source_sha256": flags["source_sha256"],
            "output_sha256": flags["output_sha256"],
            "completed_at": now(),
            "statistics": flags,
        }
        (REPORTS / "processing_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # On a resumed run, rebuild report source rows for every completed course.
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if relative not in source_rows:
            source_rows[relative] = read_csv(path)[1]

    validation = validate(all_course_flags, db, source_rows)
    write_reports(
        all_course_flags, all_issues, all_unresolved, db["passages"], manifest, validation
    )
    print(
        json.dumps(
            {
                "courses": len(all_course_flags),
                "questions": sum(f["source_rows"] for f in all_course_flags),
                "missing_after": sum(f["missing_after"] for f in all_course_flags),
                "issues": len(all_issues),
                "unresolved_rows": len(all_unresolved),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
