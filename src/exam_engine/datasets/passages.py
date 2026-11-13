"""Passage context records and structural validation.

The source dataset keeps passages in a sidecar ``passages.csv``.  This module
reads that file without changing it, and validates the relationship between each
passage and the question rows that reference it.  It deliberately performs no
semantic answer checking and never repairs malformed source text.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exam_engine.datasets.parsing import parse_options, parse_options_from_text

OBJECTIVE_TYPES = {"mcq", "msq", "assertion_reason", "multiple_choice", "multiple choice"}
_REPLACEMENT_RE = re.compile(r"(?:\ufffd|Ã.|Â.|â€|â€™|â€œ)")
_PAGE_ARTIFACT_RE = re.compile(r"\bPage\s+\d+\s+of\s+\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExamPassageRecord:
    passage_id: str
    course_id: str
    passage_text: str
    child_question_ids: tuple[str, ...] = ()
    source_reference: str | None = None
    source_file: str = "passages.csv"
    confidence: float = 0.0

    @property
    def child_question_count(self) -> int:
        return len(self.child_question_ids)


def read_passages_csv(path: Path | str) -> list[ExamPassageRecord]:
    """Read the source passage sidecar without normalising its prose."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        records: list[ExamPassageRecord] = []
        for row in reader:
            passage_id = str(row.get("passage_id") or "").strip()
            course_id = str(row.get("course_id") or "").strip()
            text = str(row.get("passage_text") or "")
            if not passage_id or not course_id or not text.strip():
                continue
            child_ids = tuple(
                item.strip()
                for item in str(row.get("child_question_ids") or "").split(";")
                if item.strip()
            )
            try:
                confidence = float(str(row.get("confidence") or 0).strip() or 0)
            except ValueError:
                confidence = 0.0
            records.append(
                ExamPassageRecord(
                    passage_id=passage_id,
                    course_id=course_id,
                    passage_text=text,
                    child_question_ids=child_ids,
                    source_reference=str(row.get("source_reference") or "").strip() or None,
                    source_file=path.name,
                    confidence=max(0.0, min(1.0, confidence)),
                )
            )
        return records


@dataclass
class PassageValidationReport:
    passage_count: int
    passage_question_count: int
    valid_relationships: int
    orphan_questions: int
    unused_passages: int
    malformed_subquestions: int
    missing_or_invalid_options: int
    corrupted_or_incomplete_passages: int
    manual_review_records: int
    duplicate_question_id_groups: int
    unanswered_passage_questions: int
    issues: list[dict[str, Any]] = field(default_factory=list)
    passages: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "passage_count": self.passage_count,
                "passage_question_count": self.passage_question_count,
                "valid_relationships": self.valid_relationships,
                "orphan_questions": self.orphan_questions,
                "unused_passages": self.unused_passages,
                "malformed_subquestions": self.malformed_subquestions,
                "missing_or_invalid_options": self.missing_or_invalid_options,
                "corrupted_or_incomplete_passages": self.corrupted_or_incomplete_passages,
                "manual_review_records": self.manual_review_records,
                "duplicate_question_id_groups": self.duplicate_question_id_groups,
                "unanswered_passage_questions": self.unanswered_passage_questions,
            },
            "issues": self.issues,
            "passages": self.passages,
        }


def _is_objective(question_type: str | None) -> bool:
    return str(question_type or "").strip().lower() in OBJECTIVE_TYPES


def _option_count(raw: str | None) -> int:
    if not raw or not str(raw).strip():
        return 0
    return len(parse_options(str(raw))) or len(parse_options_from_text(str(raw)))


def _issue(
    *,
    code: str,
    severity: str,
    message: str,
    passage_id: str | None,
    question_id: str | None,
    source_file: str | None,
    source_reference: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "passage_id": passage_id,
        "question_id": question_id,
        "source_file": source_file,
        "source_reference": source_reference,
    }


def validate_passage_dataset(root: Path | str) -> PassageValidationReport:
    """Validate passages against every question row in a dataset root.

    A relationship is counted as valid only when the child question exists once,
    has the matching non-empty ``passage_id`` and is structurally usable.  The
    function reports problems but never edits a source row.
    """
    root = Path(root)
    passages = read_passages_csv(root / "passages.csv")
    passage_ids = {item.passage_id for item in passages}
    question_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(root.rglob("*.csv")):
        if path.name == "passages.csv":
            continue
        relative = path.relative_to(root).as_posix()
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                qid = str(row.get("question_id") or "").strip()
                if qid:
                    item = dict(row)
                    item["_source_file"] = relative
                    question_rows[qid].append(item)

    issues: list[dict[str, Any]] = []
    passage_payloads: list[dict[str, Any]] = []
    valid_relationships = 0
    unanswered = 0
    malformed_subquestions = 0
    missing_or_invalid_options = 0
    corrupted_ids: set[str] = set()
    invalid_option_ids: set[tuple[str, str]] = set()
    unused = 0
    total_children = 0

    for passage in passages:
        child_ids = list(passage.child_question_ids)
        total_children += len(child_ids)
        if not child_ids:
            unused += 1
            issues.append(
                _issue(
                    code="passage.no_children",
                    severity="warning",
                    message="Passage has no child question ids.",
                    passage_id=passage.passage_id,
                    question_id=None,
                    source_file=passage.source_file,
                    source_reference=passage.source_reference,
                )
            )
        if len(set(child_ids)) != len(child_ids):
            issues.append(
                _issue(
                    code="passage.duplicate_child_id",
                    severity="warning",
                    message="The same child question id appears more than once in the passage.",
                    passage_id=passage.passage_id,
                    question_id=None,
                    source_file=passage.source_file,
                    source_reference=passage.source_reference,
                )
            )
        text = passage.passage_text
        if not text.strip() or len(text.split()) < 20:
            corrupted_ids.add(passage.passage_id)
            issues.append(
                _issue(
                    code="passage.incomplete_text",
                    severity="error",
                    message="Passage text is empty or shorter than 20 words.",
                    passage_id=passage.passage_id,
                    question_id=None,
                    source_file=passage.source_file,
                    source_reference=passage.source_reference,
                )
            )
        if _REPLACEMENT_RE.search(text):
            corrupted_ids.add(passage.passage_id)
            issues.append(
                _issue(
                    code="passage.corrupted_text",
                    severity="error",
                    message="Passage text contains replacement or mojibake characters.",
                    passage_id=passage.passage_id,
                    question_id=None,
                    source_file=passage.source_file,
                    source_reference=passage.source_reference,
                )
            )
        if _PAGE_ARTIFACT_RE.search(text):
            corrupted_ids.add(passage.passage_id)
            issues.append(
                _issue(
                    code="passage.source_page_artifact",
                    severity="warning",
                    message=(
                        "Passage text contains a page header/footer artifact; the source "
                        "record needs manual review and was not truncated automatically."
                    ),
                    passage_id=passage.passage_id,
                    question_id=None,
                    source_file=passage.source_file,
                    source_reference=passage.source_reference,
                )
            )

        child_payloads: list[dict[str, Any]] = []
        for child_id in child_ids:
            matches = question_rows.get(child_id, [])
            duplicate_match = len(matches) > 1
            if not matches:
                issues.append(
                    _issue(
                        code="passage.missing_child_question",
                        severity="error",
                        message="Passage references a question id that is not present in the dataset.",
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file=None,
                        source_reference=passage.source_reference,
                    )
                )
                continue
            if len(matches) > 1:
                issues.append(
                    _issue(
                        code="question.duplicate_id",
                        severity="warning",
                        message="Question id is not unique across source files; relationship needs manual review.",
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file="; ".join(item["_source_file"] for item in matches),
                        source_reference=passage.source_reference,
                    )
                )
            row = matches[0]
            source_file = row["_source_file"]
            row_passage = str(row.get("passage_id") or "").strip()
            if not row_passage:
                issues.append(
                    _issue(
                        code="question.missing_passage_reference",
                        severity="error",
                        message="Passage child question has no passage_id.",
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file=source_file,
                        source_reference=passage.source_reference,
                    )
                )
            elif row_passage != passage.passage_id:
                issues.append(
                    _issue(
                        code="question.invalid_passage_reference",
                        severity="error",
                        message="Passage child question references a different or unknown passage_id.",
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file=source_file,
                        source_reference=passage.source_reference,
                    )
                )
            elif not duplicate_match:
                valid_relationships += 1

            if not str(row.get("answer_text") or "").strip():
                unanswered += 1

            qtype = str(row.get("question_type") or "").strip().lower()
            option_text = str(row.get("options") or "")
            if _is_objective(qtype) and _option_count(option_text) < 2:
                invalid_option_ids.add((passage.passage_id, child_id))
                issues.append(
                    _issue(
                        code="question.missing_or_invalid_options",
                        severity="warning",
                        message="Objective passage question has fewer than two parseable options.",
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file=source_file,
                        source_reference=passage.source_reference,
                    )
                )
            if option_text and _PAGE_ARTIFACT_RE.search(option_text):
                invalid_option_ids.add((passage.passage_id, child_id))
                issues.append(
                    _issue(
                        code="question.option_page_artifact",
                        severity="warning",
                        message=(
                            "Question options contain a page header/footer artifact; the source "
                            "record needs manual review and was not rewritten."
                        ),
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file=source_file,
                        source_reference=passage.source_reference,
                    )
                )
            question_text = str(row.get("question_text") or "")
            if _PAGE_ARTIFACT_RE.search(question_text):
                issues.append(
                    _issue(
                        code="question.page_artifact",
                        severity="warning",
                        message=(
                            "Question text contains a page header/footer artifact; the source "
                            "record needs manual review and was not rewritten."
                        ),
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file=source_file,
                        source_reference=passage.source_reference,
                    )
                )

            raw_subparts = str(row.get("subparts_json") or "").strip()
            if raw_subparts:
                try:
                    subparts = json.loads(raw_subparts)
                except json.JSONDecodeError:
                    malformed_subquestions += 1
                    issues.append(
                        _issue(
                            code="question.malformed_subquestions",
                            severity="warning",
                            message="subparts_json is not valid JSON.",
                            passage_id=passage.passage_id,
                            question_id=child_id,
                            source_file=source_file,
                            source_reference=passage.source_reference,
                        )
                    )
                    subparts = []
                if subparts and not isinstance(subparts, list):
                    malformed_subquestions += 1
                    issues.append(
                        _issue(
                            code="question.malformed_subquestions",
                            severity="warning",
                            message="subparts_json is not a list.",
                            passage_id=passage.passage_id,
                            question_id=child_id,
                            source_file=source_file,
                            source_reference=passage.source_reference,
                        )
                    )

            if not str(row.get("original_question_number") or "").strip():
                issues.append(
                    _issue(
                        code="question.missing_order",
                        severity="info",
                        message="Question has no original question number for ordering.",
                        passage_id=passage.passage_id,
                        question_id=child_id,
                        source_file=source_file,
                        source_reference=passage.source_reference,
                    )
                )
            child_payloads.append(
                {
                    "question_id": child_id,
                    "source_file": source_file,
                    "question_type": row.get("question_type"),
                    "original_question_number": row.get("original_question_number"),
                    "answer_present": bool(str(row.get("answer_text") or "").strip()),
                    "options_present": bool(str(row.get("options") or "").strip()),
                }
            )

        passage_payloads.append(
            {
                "passage_id": passage.passage_id,
                "course_id": passage.course_id,
                "source_reference": passage.source_reference,
                "passage_chars": len(text),
                "child_question_ids": child_ids,
                "questions": child_payloads,
            }
        )

    orphan_questions = 0
    for qid, rows in question_rows.items():
        for row in rows:
            row_passage = str(row.get("passage_id") or "").strip()
            if row_passage and row_passage not in passage_ids:
                orphan_questions += 1
                issues.append(
                    _issue(
                        code="question.orphan_passage_reference",
                        severity="error",
                        message="Question references a passage_id that has no passage record.",
                        passage_id=row_passage,
                        question_id=qid,
                        source_file=row["_source_file"],
                    )
                )

    duplicate_qids = [qid for qid, rows in question_rows.items() if len(rows) > 1]
    review_keys = {
        (issue["passage_id"], issue["question_id"], issue["source_file"], issue["code"])
        for issue in issues
        if issue["severity"] in {"error", "warning"}
    }
    return PassageValidationReport(
        passage_count=len(passages),
        passage_question_count=total_children,
        valid_relationships=valid_relationships,
        orphan_questions=orphan_questions,
        unused_passages=unused,            malformed_subquestions=malformed_subquestions,
            missing_or_invalid_options=len(invalid_option_ids),
            corrupted_or_incomplete_passages=len(corrupted_ids),
        manual_review_records=len(review_keys),
        duplicate_question_id_groups=len(duplicate_qids),
        unanswered_passage_questions=unanswered,
        issues=issues,
        passages=passage_payloads,
    )


__all__ = [
    "ExamPassageRecord",
    "PassageValidationReport",
    "read_passages_csv",
    "validate_passage_dataset",
]
