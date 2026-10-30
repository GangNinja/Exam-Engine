"""Phase 11 tests: evidence-bounded misconception mining.

The fixtures here are synthetic and exist to exercise the pipeline. They are not
evidence about real learners, and several tests assert that the analyser refuses to
conclude more than its evidence supports.
"""

from __future__ import annotations

import json

import pytest

from exam_engine.advanced import misconception
from exam_engine.advanced.service import AdvancedAssessmentService
from exam_engine.advanced.source import AdvancedError
from exam_engine.config import AdvancedSettings, Settings
from exam_engine.models.advanced import (
    MisconceptionQuery,
    ResponseImport,
    ResponseSubmission,
)
from exam_engine.models.enums import (
    GenerationFormat,
    MisconceptionKind,
    MisconceptionStatus,
    ResponseCorrectness,
    SupportLevel,
)
from exam_engine.storage.db import Database
from exam_engine.storage.generation_repository import GenerationRepository
from exam_engine.storage.repositories import CourseRepository, DatasetSourceRepository
from tests.support import make_course, make_source
from tests.test_advanced import (
    COURSE,
    NOTES,
    OTHER_COURSE,
    SOURCE_ID,
    _add_syllabus,
    _generate,
    _seed,
    _service,
)

CONFUSED = (
    "A queue is a linear structure in which the item that was added first is removed "
    "first, because dequeue removes the front item."
)
SHORT = "It removes the oldest item first."


def _import(
    database: Database,
    responses: list[ResponseSubmission],
    *,
    course_id: str = COURSE,
    syllabus_id: str | None = None,
):
    return _service(database).import_responses(
        ResponseImport(
            course_id=course_id,
            syllabus_id=syllabus_id,
            responses=responses,
        )
    )


def _submission(
    question_id: str,
    answer: str,
    *,
    correctness: ResponseCorrectness | None = ResponseCorrectness.INCORRECT,
    marks: float | None = 0.0,
    available: float | None = 4.0,
    student: str | None = None,
    group: str | None = None,
) -> ResponseSubmission:
    return ResponseSubmission(
        question_id=question_id,
        answer=answer,
        correctness=correctness,
        marks_awarded=marks,
        marks_available=available,
        student_ref=student,
        group_ref=group,
    )


def _analyse(database: Database, **kwargs: object):
    return _service(database).analyse_misconceptions(COURSE, **kwargs)


def _records_by_kind(report) -> dict[MisconceptionKind, object]:
    grouped: dict[MisconceptionKind, object] = {}
    for record in report.records:
        grouped.setdefault(record.kind, record)
    return grouped


# -- evidence requirements -----------------------------------------------------


def test_a_single_incorrect_response_is_one_observation_only(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    report = _analyse(database)
    assert report.responses_stored == 1
    assert report.records
    record = report.records[0]
    assert record.kind is MisconceptionKind.CONCEPT_CONFUSION
    assert record.support_count == 1
    assert record.support_level is SupportLevel.SINGLE_OBSERVATION
    assert any("single response" in note for note in record.limitations)
    assert any(
        warning.code == "misconception_below_support_threshold" for warning in report.warnings
    )


def test_distinct_responses_across_questions_become_recurring(database: Database) -> None:
    _seed(database)
    first = _generate(database)
    second = _generate(database, fmt=GenerationFormat.SHORT_ANSWER)
    assert first.question_id != second.question_id
    _import(
        database,
        [
            _submission(first.question_id, CONFUSED, student="s1"),
            _submission(first.question_id, CONFUSED + " The queue waits.", student="s2"),
            _submission(second.question_id, "A queue removes the newest item.", student="s3"),
            _submission(second.question_id, "A queue is a stack.", student="s4"),
        ],
    )
    report = _analyse(database)
    confusion = _records_by_kind(report).get(MisconceptionKind.CONCEPT_CONFUSION)
    assert confusion is not None
    assert confusion.support_count >= 3
    assert confusion.distinct_questions == 2
    assert confusion.support_level is SupportLevel.RECURRING
    assert confusion.confidence > 0.5


def test_duplicate_imports_are_counted_once(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    first = _import(
        database,
        [
            _submission(question.question_id, CONFUSED, student="s1"),
            _submission(question.question_id, CONFUSED, student="s1"),
        ],
    )
    assert first.inserted == 1
    assert first.duplicates == 1
    second = _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    assert second.inserted == 0
    report = _analyse(database)
    assert report.responses_stored == 1
    assert all(record.support_count == 1 for record in report.records)
    assert any(
        warning.code in {"duplicate_responses_skipped", "already_imported"}
        for warning in second.warnings
    )


def test_two_students_with_the_same_answer_stay_independent(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(
        database,
        [
            _submission(question.question_id, CONFUSED, student="s1"),
            _submission(question.question_id, CONFUSED, student="s2"),
        ],
    )
    report = _analyse(database)
    assert report.responses_stored == 2
    record = _records_by_kind(report)[MisconceptionKind.CONCEPT_CONFUSION]
    assert record.support_count == 2
    assert len({item.response_id for item in record.evidence}) == 2


def test_missing_reference_answer_is_discarded_not_guessed(database: Database) -> None:
    _seed(database)
    from exam_engine.models.enums import DocumentKind
    from exam_engine.models.question import QuestionRecord
    from exam_engine.storage.repositories import QuestionRepository
    from tests.test_advanced import _add_document

    _add_document(
        database,
        document_id="doc_paper_mis",
        kind=DocumentKind.QUESTION_PAPER,
        title="SYNTHETIC paper",
        pages=["1. Explain how a stack differs from a queue."],
    )
    record = QuestionRecord(
        question_id="q_hist_no_key",
        document_id="doc_paper_mis",
        course_id=COURSE,
        text="Explain how a stack differs from a queue.",
    )
    with database.transaction() as session:
        QuestionRepository(session).replace_for_document("doc_paper_mis", [record])
    _import(database, [_submission(record.question_id, CONFUSED, student="s1")])
    report = _analyse(database)
    assert report.records == []
    assert [item.reason for item in report.discarded] == ["missing_reference_answer"]
    assert any(warning.code == "no_supported_misconceptions" for warning in report.warnings)


def test_missing_concept_mapping_is_discarded(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    unmapped = question.model_copy(
        update={"question_id": "gq_unmapped", "concept_ids": [], "concept_labels": []}
    )
    with database.transaction() as session:
        GenerationRepository(session).save_question(unmapped)
    _import(database, [_submission(unmapped.question_id, CONFUSED, student="s1")])
    report = _analyse(database)
    assert [item.reason for item in report.discarded] == ["missing_concept_mapping"]
    assert report.records == []


def test_unreadable_and_empty_answers_are_discarded(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(
        database,
        [
            _submission(question.question_id, "", student="s1"),
            _submission(question.question_id, "ok", student="s2"),
            _submission(
                question.question_id,
                "The answer depends on something unrelated entirely.",
                student="s3",
            ),
        ],
    )
    report = _analyse(database)
    reasons = {item.reason for item in report.discarded}
    assert {"empty_answer", "answer_too_short"}.issubset(reasons)


def test_correct_responses_are_not_mined(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(
        database,
        [
            _submission(
                question.question_id,
                question.answer_key or "The most recent item is removed first.",
                correctness=ResponseCorrectness.CORRECT,
                marks=4.0,
                student="s1",
            )
        ],
    )
    report = _analyse(database)
    assert report.records == []
    assert [item.reason for item in report.discarded] == ["response_correct"]


def test_partial_credit_produces_partial_understanding(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    answer = "A stack is a linear structure with last-in-first-out behaviour at the top."
    _import(
        database,
        [
            _submission(
                question.question_id,
                answer,
                correctness=ResponseCorrectness.PARTIALLY_CORRECT,
                marks=2.0,
                student="s1",
            ),
            _submission(
                question.question_id,
                answer + " The top item is removed.",
                correctness=ResponseCorrectness.PARTIALLY_CORRECT,
                marks=2.0,
                student="s2",
            ),
        ],
    )
    report = _analyse(database)
    kinds = set(_records_by_kind(report))
    assert MisconceptionKind.PARTIAL_UNDERSTANDING in kinds
    record = _records_by_kind(report)[MisconceptionKind.PARTIAL_UNDERSTANDING]
    assert "partial" in record.description.lower()


def test_reasoning_without_justification_is_detected(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(
        database,
        [
            _submission(
                question.question_id,
                "A stack is a linear structure with last-in-first-out behaviour at the top.",
                student="s1",
            )
        ],
    )
    report = _analyse(database)
    assert MisconceptionKind.MISSING_REASONING_STEP in set(_records_by_kind(report))


def test_unsupported_conclusions_are_reported_as_not_inferred(database: Database) -> None:
    _seed(database)
    payload = _service(database).capabilities()
    assert "unclear_or_unrelated" in payload["not_inferred"]
    assert all(rule for rule in payload["misconception_kinds"].values())


def test_support_threshold_is_configurable(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(
        database,
        [
            _submission(question.question_id, CONFUSED, student="s1"),
            _submission(question.question_id, CONFUSED + " Front item.", student="s2"),
        ],
    )
    strict = _analyse(database, min_support=5)
    record = strict.records[0]
    assert record.support_level is not SupportLevel.RECURRING
    assert strict.parameters["min_support"] == 5
    assert any(
        warning.code == "misconception_below_support_threshold" for warning in strict.warnings
    )


def test_response_identifiers_are_anonymous(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(
        database,
        [_submission(question.question_id, CONFUSED, student="Narasimha Rao", group="class-A")],
    )
    report = _analyse(database)
    serialized = json.dumps(report.model_dump(mode="json"))
    assert "Narasimha" not in serialized
    assert "class-A" not in serialized
    record = report.records[0]
    assert record.evidence[0].response_id.startswith("resp_")
    stored = _service(database).list_misconceptions(MisconceptionQuery(course_id=COURSE))
    assert stored and stored[0].evidence[0].response_id.startswith("resp_")
    assert all(item.distinct_groups <= 1 for item in stored)


def test_evidence_carries_provenance(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    report = _analyse(database)
    evidence = report.records[0].evidence[0]
    assert evidence.question_id == question.question_id
    assert evidence.marker == "sibling_concept_dominates_answer"
    assert evidence.method.startswith(misconception.METHOD)
    assert evidence.excerpt and len(evidence.excerpt) <= 241
    assert report.records[0].question_ids == [question.question_id]


def test_configured_excerpt_limit_is_capped_at_the_stored_limit(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    long_answer = CONFUSED + " " + ("Queues are linear structures. " * 12)
    _import(database, [_submission(question.question_id, long_answer, student="s1")])
    service = AdvancedAssessmentService(
        database, Settings(advanced=AdvancedSettings(excerpt_chars=800))
    )
    report = service.analyse_misconceptions(COURSE)
    assert report.parameters["excerpt_chars"] == 240
    assert report.records
    assert all(len(item.excerpt) <= 241 for record in report.records for item in record.evidence)


def test_missing_student_references_limit_independence_claims(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(database, [_submission(question.question_id, CONFUSED)])
    report = _analyse(database)
    assert any(warning.code == "independent_responses_unverifiable" for warning in report.warnings)
    assert any(
        "no response carried a student or group reference" in note.lower()
        for note in report.limitations
    )


def test_named_responses_do_not_trigger_the_independence_warning(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    report = _analyse(database)
    assert not any(
        warning.code == "independent_responses_unverifiable" for warning in report.warnings
    )


def test_analysis_is_repeatable(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(
        database,
        [
            _submission(question.question_id, CONFUSED, student="s1"),
            _submission(question.question_id, CONFUSED + " Queues wait.", student="s2"),
        ],
    )
    first = _analyse(database)
    second = _analyse(database)
    assert [item.misconception_id for item in first.records] == [
        item.misconception_id for item in second.records
    ]
    assert [item.support_count for item in first.records] == [
        item.support_count for item in second.records
    ]


def test_review_decision_survives_reanalysis(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    report = _analyse(database)
    record = report.records[0]
    service = _service(database)
    approved = service.set_misconception_status(
        record.misconception_id, MisconceptionStatus.APPROVED, note="checked against rubric"
    )
    assert approved.review_status is MisconceptionStatus.APPROVED
    _import(
        database,
        [_submission(question.question_id, CONFUSED + " Queue front.", student="s2")],
    )
    reanalysed = _analyse(database)
    kept = next(
        item for item in reanalysed.records if item.misconception_id == record.misconception_id
    )
    assert kept.review_status is MisconceptionStatus.APPROVED
    assert kept.support_count >= 2
    assert any(event.note == "checked against rubric" for event in kept.review_history)
    stored = service.list_misconceptions(MisconceptionQuery(course_id=COURSE))
    assert len({item.misconception_id for item in stored}) == len(stored)


def test_export_writes_a_structured_report(database: Database, tmp_path) -> None:
    _seed(database)
    question = _generate(database)
    _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    _analyse(database)
    path, count = _service(database).export_misconceptions(
        COURSE, question.syllabus_id, tmp_path / "misconceptions.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["count"] == count == len(payload["records"])
    assert payload["schema_version"].startswith("phase11")
    assert "not diagnoses" in payload["note"]


# -- isolation and safety ------------------------------------------------------


def test_import_refuses_a_question_from_another_course(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id=SOURCE_ID))
        CourseRepository(session).upsert(
            make_course(course_id=OTHER_COURSE, course_code="CS998", title="Other")
        )
        _add_syllabus(database, course_id=OTHER_COURSE, suffix="b")
    with pytest.raises(AdvancedError) as error:
        _import(
            database,
            [_submission(question.question_id, CONFUSED, student="s1")],
            course_id=OTHER_COURSE,
        )
    assert error.value.code == "unknown_question"


def test_analysis_refuses_a_foreign_syllabus(database: Database) -> None:
    _seed(database)
    with pytest.raises(AdvancedError) as error:
        _analyse(database, syllabus_id="syl_does_not_exist")
    assert error.value.code == "unknown_syllabus"


def test_analysis_without_responses_says_so(database: Database) -> None:
    _seed(database)
    report = _analyse(database)
    assert report.records == []
    assert any(warning.code == "no_responses" for warning in report.warnings)
    assert any("Nothing was analysed" in item for item in report.limitations)


def test_question_contexts_only_include_stored_questions(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    service = _service(database)
    assert service._question_contexts(COURSE, question.syllabus_id, []) == {}
    _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    with database.session() as session:
        from exam_engine.storage.advanced_repository import ResponseRepository

        stored = ResponseRepository(session).list(COURSE)
    contexts = service._question_contexts(COURSE, question.syllabus_id, stored)
    assert set(contexts) == {question.question_id}
    assert contexts[question.question_id].concept_labels == tuple(question.concept_labels)
    assert contexts[question.question_id].reference_answer
    report = _analyse(database)
    assert report.parameters["method_version"] == misconception.METHOD_VERSION


def test_synthetic_fixture_labels_are_present(database: Database) -> None:
    _seed(database)
    question = _generate(database)
    _import(database, [_submission(question.question_id, CONFUSED, student="s1")])
    report = _analyse(database)
    assert any("Synthetic fixtures" in item for item in report.limitations)
    assert NOTES  # the fixture text itself is synthetic, and labelled as such in tests
