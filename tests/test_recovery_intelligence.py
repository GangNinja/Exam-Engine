from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from exam_engine.config import Settings
from exam_engine.datasets.parsing import ParsedOption
from exam_engine.models.recovery import AnswerCandidate
from exam_engine.recovery.answers import LocalAnswer, LocalAnswerIndex, recover_answer
from exam_engine.recovery.sources import (
    AnswerKeyEntry,
    AnswerKeyIndex,
    SourceDiscovery,
    classify_source,
    extract_answer_key_entries,
)
from exam_engine.recovery.service import DatasetRecoveryService
from exam_engine.recovery.verification import answers_equivalent, verify_answer
from exam_engine.storage.db import memory_database
from exam_engine.storage.recovery_repository import RecoveryRepository

FIELDS = [
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


def _write(path: Path, rows: list[dict[str, str]], fields: list[str] | None = None) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(**changes: str) -> dict[str, str]:
    value = {field: "" for field in FIELDS}
    value.update(
        {
            "question_id": "Q1",
            "original_question_number": "1",
            "question_text": "What is the capital of India?",
            "question_type": "MCQ",
            "options": "A. Mumbai B. New Delhi C. Chennai D. Kolkata",
            "marks": "1",
            "answer_text": "B. New Delhi",
            "answer_type": "MCQ option",
        }
    )
    value.update(changes)
    return value


class _StubSearch:
    def search(self, query: str, *, limit: int | None = None):
        return [
            SimpleNamespace(
                title="GATE Chemistry 2015 official answer key",
                url="https://gate.iitb.ac.in/answer-key-2015.pdf",
                snippet="Official final answer key",
                provider="stub",
            ),
            SimpleNamespace(
                title="GATE Chemistry syllabus",
                url="https://example.edu/syllabus.pdf",
                snippet="Course curriculum",
                provider="stub",
            ),
        ]


def test_source_classification_never_treats_syllabus_as_question_paper() -> None:
    kind, tier, reliability, reason = classify_source(
        title="GATE Chemistry Syllabus 2015",
        url="https://gate.iitb.ac.in/syllabus.pdf",
    )
    assert kind == "syllabus"
    assert tier == 1
    assert reliability < 0.5
    assert reason == "syllabus_or_curriculum_term"

    answer_kind, answer_tier, _, _ = classify_source(
        title="Official Final Answer Key",
        url="https://gate.iitb.ac.in/key.pdf",
    )
    assert answer_kind == "answer_key"
    assert answer_tier == 1


def test_source_discovery_is_provenance_only_and_excludes_snippets_as_answers() -> None:
    discovered = SourceDiscovery(_StubSearch(), result_limit=2).discover(
        course_id="gate-chemistry",
        exam_name="GATE",
        subject_name="Chemistry",
        years=[2015],
        query_limit=1,
    )
    assert len(discovered) == 2
    assert {item.document_type for item in discovered} == {"answer_key", "syllabus"}
    assert all(item.evidence["snippet_is_not_answer_evidence"] is True for item in discovered)


def test_answer_key_parsing_and_number_matching() -> None:
    entries = extract_answer_key_entries(
        "Answer Key\n1. B\nQ2) 4.5\n3 | (C)\n4. True\n"
    )
    index = AnswerKeyIndex()
    index.extend(entries)
    assert len(entries) == 4
    assert index.match("Q1")[0].answer == "B"
    assert index.match(2)[0].answer == "4.5"
    assert index.match("3")[0].answer == "C"


def test_verification_is_separate_and_never_promotes_ai_output() -> None:
    options = [
        ParsedOption("A", "Mumbai"),
        ParsedOption("B", "New Delhi"),
        ParsedOption("C", "Chennai"),
        ParsedOption("D", "Kolkata"),
    ]
    official = AnswerCandidate(
        answer_text="B",
        source_type="official_answer_key",
        source_name="official key",
        verification_status="official_verified",
        confidence=0.98,
        method="official_answer_key_lookup",
        official=True,
    )
    outcome = verify_answer(
        question_text="Capital of India?",
        options=options,
        answer_text="B",
        answer_type="mcq_option",
        candidate=official,
    )
    assert outcome.result.status == "official_verified"

    independent = verify_answer(
        question_text="Capital of India?",
        options=options,
        answer_text="B",
        answer_type="mcq_option",
        candidate=official,
        independent_answer="New Delhi",
    )
    assert independent.result.status == "official_verified_with_independent_check"

    ai = AnswerCandidate(
        answer_text="B",
        source_type="ai_model",
        source_name="model",
        verification_status="ai_generated_unverified",
        confidence=0.5,
        method="model",
        official=False,
    )
    ai_result = verify_answer(
        question_text="Capital of India?",
        options=options,
        answer_text="B",
        answer_type="mcq_option",
        candidate=ai,
    )
    assert ai_result.result.status == "ai_generated_unverified"


def test_near_duplicate_index_and_recovery_require_compatible_options() -> None:
    index = LocalAnswerIndex()
    index.add(
        LocalAnswer(
            question_text="What is the capital of India?",
            options=(("A", "Mumbai"), ("B", "New Delhi")),
            answer_text="B. New Delhi",
            course_id="course-a",
            source_file="a.csv",
            source_row=2,
            source_question_id="A1",
        )
    )
    near = index.find_near("What is the capital of India ?", threshold=0.9)
    assert near and near[0][1] >= 0.9

    decision = recover_answer(
        question_text="What is the capital of India ?",
        options=[ParsedOption("A", "Mumbai"), ParsedOption("B", "New Delhi")],
        answer_text=None,
        answer_type="mcq_option",
        solution_steps=None,
        course_id="course-b",
        source_file="b.csv",
        source_row=3,
        source_question_id="B1",
        index=index,
    )
    assert decision.answer_status == "local_duplicate"
    assert decision.answer_text == "B. New Delhi"
    assert decision.candidates[0].evidence["matching_rows"] == 1


def test_cross_row_explicit_passage_creates_parent_child_group(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "passage.csv",
        [
            {
                "question_id": "P1",
                "original_question_number": "1",
                "question_text": "What is the main idea?",
                "question_type": "Reading/comprehension",
                "options": "A. Energy B. Matter C. Motion D. Light",
                "passage": "Read the following passage. The sun is a star that provides energy to the earth and supports life.",
                "marks": "1",
                "answer_text": "",
                "answer_type": "MCQ option",
            },
            {
                "question_id": "P2",
                "original_question_number": "2",
                "question_text": "What does the sun provide?",
                "question_type": "Reading/comprehension",
                "options": "A. Energy B. Matter C. Motion D. Light",
                "passage": "Read the following passage. The sun is a star that provides energy to the earth and supports life.",
                "marks": "1",
                "answer_text": "",
                "answer_type": "MCQ option",
            },
        ],
        fields=FIELDS + ["passage"],
    )
    database = memory_database()
    try:
        run = DatasetRecoveryService(database, Settings.from_env()).analyze(
            tmp_path,
            dry_run=True,
            csv_path=path,
            course_id="passage-course",
        )
    finally:
        database.close()
    course = run.courses[0]
    assert len(course.passages) == 1
    assert course.passages[0].child_record_ids == [record.record_id for record in course.records]
    assert all(record.structure.get("passage_group_id") for record in course.records)


def test_statement_question_is_not_misclassified_as_passage(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "statement.csv",
        [
            _row(
                question_text=(
                    "Given below are two statements followed by two conclusions. "
                    "Statements: I. No manager is a leader. II. All leaders are executives. "
                    "Conclusions: I. No manager is an executive. II. No executive is a manager."
                ),
                answer_text="",
            )
        ],
    )
    database = memory_database()
    try:
        run = DatasetRecoveryService(database, Settings.from_env()).analyze(
            tmp_path,
            dry_run=True,
            csv_path=path,
            course_id="statement-course",
        )
    finally:
        database.close()
    record = run.courses[0].records[0]
    assert record.repaired["question_type"] == "statement_based"
    assert record.structure.get("passage") is None


def test_quality_scores_are_calibrated_and_do_not_share_a_blanket_69() -> None:
    answered_dir = Path(".").resolve() / "answered-quality"
    missing_dir = Path(".").resolve() / "missing-quality"
    # Keep this test inside pytest's temporary tree.
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        answered_dir = base / "answered"
        missing_dir = base / "missing"
        answered_dir.mkdir()
        missing_dir.mkdir()
        answered = _write(answered_dir / "a.csv", [_row(question_id="Q1")])
        missing = _write(
            missing_dir / "m.csv",
            [_row(question_id="Q1", answer_text="")],
        )
        database = memory_database()
        try:
            service = DatasetRecoveryService(database, Settings.from_env())
            answered_run = service.analyze(
                answered_dir,
                dry_run=True,
                csv_path=answered,
                course_id="answered-course",
            )
            missing_run = service.analyze(
                missing_dir,
                dry_run=True,
                csv_path=missing,
                course_id="missing-course",
            )
        finally:
            database.close()
    answered_score = answered_run.courses[0].quality["overall"]
    missing_score = missing_run.courses[0].quality["overall"]
    assert answered_score > missing_score
    assert answered_run.courses[0].quality["indicators"]["answer_verification_coverage"] == 0.0
    assert missing_run.courses[0].quality["evidence_status"] == "insufficient_evidence"


def test_source_candidates_are_persisted_and_read_back(tmp_path: Path) -> None:
    path = _write(tmp_path / "source.csv", [_row(question_id="Q1")])
    database = memory_database()
    try:
        run = DatasetRecoveryService(
            database,
            Settings.from_env(),
            web_search=_StubSearch(),
        ).analyze(
            tmp_path,
            dry_run=False,
            csv_path=path,
            course_id="source-course",
            use_web=True,
            source_limit=1,
        )
        with database.session() as session:
            detail = RecoveryRepository(session).course_detail(
                "source-course", run_id=run.run_id
            )
    finally:
        database.close()
    assert detail is not None
    assert len(detail["source_candidates"]) == 2
    assert {item["document_type"] for item in detail["source_candidates"]} == {
        "answer_key",
        "syllabus",
    }
    assert detail["quality"]["indicators"]["source_verification_coverage"] == 0.0


def test_numeric_equivalence_and_invalid_workflow() -> None:
    assert answers_equivalent("4.0", "4")
    assert not answers_equivalent("4", "5")
    with pytest.raises(ValueError):
        DatasetRecoveryService(memory_database(), Settings.from_env()).analyze(
            ".", workflow="not-a-workflow"
        )
