"""Phase 11 tests: question mutation, verification invariants, adversarial generation.

Everything runs offline against a synthetic course. These tests check software
behaviour: that a strategy's promised properties are enforced, that a question is
never silently approved, and that one unusable strategy does not take a batch down.
They are not an educational-quality evaluation.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
from pydantic import ValidationError

from exam_engine.advanced import adversarial, mutation
from exam_engine.advanced.service import AdvancedAssessmentService
from exam_engine.advanced.source import AdvancedError, EvidenceContext, SourceOption, SourceQuestion
from exam_engine.advanced.verification import VerificationRequest, verify
from exam_engine.config import GenerationSettings
from exam_engine.generation import QuestionGenerationService
from exam_engine.generation.validation import ValidationContext
from exam_engine.models.advanced import AdversarialRequest, MutationRequest
from exam_engine.models.document import DocumentRecord, PageRecord
from exam_engine.models.enums import (
    AssessmentOrigin,
    BloomLevel,
    ChallengeStrategy,
    DifficultyLabel,
    DocumentKind,
    ExtractionStatus,
    GenerationFormat,
    GenerationReviewStatus,
    GenerationStatus,
    GenerationValidationStatus,
    MutationStrategy,
    Severity,
    SourceFormat,
)
from exam_engine.models.generation import (
    GeneratedOption,
    GeneratedQuestion,
    GenerationQuery,
    GenerationRequest,
    SourcePassage,
    ValidationResult,
)
from exam_engine.models.syllabus import (
    SyllabusRecord,
    SyllabusUnitRecord,
    make_syllabus_id,
    make_unit_id,
)
from exam_engine.storage.db import Database
from exam_engine.storage.generation_repository import GenerationRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    SyllabusRepository,
)
from tests.support import make_course, make_source

COURSE = "phase11-course-v1"
OTHER_COURSE = "phase11-course-b-v1"
SOURCE_ID = "phase11-fixture"

NOTES = (
    "Stacks A stack is a linear structure with last-in-first-out behaviour, so the item "
    "that was pushed most recently is the first item to be removed. The push operation "
    "adds an item to the top of the stack in constant time, and pop removes the most recent "
    "item. Because both operations only touch the top, a stack backed by a dynamic array "
    "runs in amortised constant time. "
    "Queues A queue is a linear structure with first-in-first-out behaviour; dequeue "
    "removes the item that has waited longest at the front."
)

UNITS = [
    ("Unit 1", "Foundations", ["Asymptotic notation"]),
    ("Unit 2", "Linear Structures", ["Stacks", "Queues", "Linked lists"]),
]


# -- fixtures ------------------------------------------------------------------


def _add_document(
    database: Database,
    *,
    document_id: str,
    kind: DocumentKind,
    title: str,
    pages: list[str],
    course_id: str = COURSE,
) -> DocumentRecord:
    document = DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id=SOURCE_ID,
        kind=kind,
        format=SourceFormat.TXT,
        title=title,
        original_filename=f"{document_id}.txt",
        content_sha256=hashlib.sha256(f"{course_id}:{document_id}".encode()).hexdigest(),
        byte_size=sum(len(page) for page in pages),
        page_count=len(pages),
        char_count=sum(len(page) for page in pages),
        extraction_status=ExtractionStatus.OK,
        pipeline_version="phase11-test",
    )
    with database.transaction() as session:
        DocumentRepository(session).upsert(document)
        DocumentRepository(session).replace_pages(
            document_id,
            [PageRecord(page_number=index, text=text) for index, text in enumerate(pages, start=1)],
        )
    return document


def _add_syllabus(
    database: Database, *, course_id: str = COURSE, suffix: str = "a"
) -> SyllabusRecord:
    document_id = f"doc_syllabus_{suffix}"
    syllabus_id = make_syllabus_id(document_id)
    units = [
        SyllabusUnitRecord(
            unit_id=make_unit_id(syllabus_id, label, title),
            unit_label=label,
            unit_number=index,
            title=title,
            topics=topics,
            page_number=index,
            confidence=0.9,
        )
        for index, (label, title, topics) in enumerate(UNITS, start=1)
    ]
    _add_document(
        database,
        document_id=document_id,
        kind=DocumentKind.SYLLABUS,
        title=f"SYNTHETIC syllabus {suffix}",
        pages=["SYNTHETIC syllabus fixture for Phase 11."],
        course_id=course_id,
    )
    syllabus = SyllabusRecord(
        syllabus_id=syllabus_id,
        course_id=course_id,
        document_id=document_id,
        source_id=SOURCE_ID,
        title=f"SYNTHETIC syllabus {suffix}",
        units=units,
        confidence=0.9,
    )
    with database.transaction() as session:
        SyllabusRepository(session).replace(syllabus)
    return syllabus


def _seed(database: Database, *, notes: bool = True) -> SyllabusRecord:
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id=SOURCE_ID))
        CourseRepository(session).upsert(
            make_course(
                course_id=COURSE,
                course_code="CS110",
                title="Phase 11 synthetic fixture",
            )
        )
    syllabus = _add_syllabus(database)
    if notes:
        _add_document(
            database,
            document_id="doc_notes_phase11",
            kind=DocumentKind.NOTES,
            title="SYNTHETIC lecture notes",
            pages=[NOTES],
        )
    return syllabus


def _service(database: Database) -> AdvancedAssessmentService:
    return AdvancedAssessmentService(database)


def _generate(
    database: Database,
    *,
    concept: str = "Stacks",
    fmt: GenerationFormat = GenerationFormat.EXPLANATION,
    bloom: BloomLevel | None = None,
) -> GeneratedQuestion:
    service = QuestionGenerationService(database, GenerationSettings())
    batch = service.generate(
        GenerationRequest(
            course_id=COURSE,
            concept_ids=[concept],
            count=1,
            format=fmt,
            bloom=bloom,
        )
    )
    assert batch.questions, batch.rejections
    return batch.questions[0]


def _source_for(database: Database) -> SourceQuestion:
    question = _generate(database)
    return SourceQuestion(
        question_id=question.question_id,
        course_id=question.course_id,
        syllabus_id=question.syllabus_id,
        unit_id=question.unit_id,
        unit_label=question.unit_label,
        unit_title=question.unit_title,
        concept_ids=tuple(question.concept_ids),
        concept_labels=tuple(question.concept_labels),
        text=question.text,
        format=question.format,
        bloom=question.requested_bloom,
        difficulty=question.requested_difficulty,
        objective=question.learning_objective,
        marks=question.marks,
        answer_key=question.answer_key,
        explanation=question.explanation,
        source_references=tuple(question.source_references),
    )


def _passage(concept_id: str = "c_stack") -> SourcePassage:
    return SourcePassage(
        document_id="doc_notes_phase11",
        document_title="SYNTHETIC lecture notes",
        document_kind=DocumentKind.NOTES,
        page_number=1,
        excerpt="A stack removes the item that was pushed most recently.",
        concept_id=concept_id,
        method="document_text",
    )


def _context(**overrides: object) -> EvidenceContext:
    base = EvidenceContext(
        passages=(_passage(),),
        sibling_labels=("Queues", "Linked lists"),
        related_labels=("Stacks", "Queues", "Linked lists"),
        prerequisite_label="Arrays",
        prerequisite_in_syllabus=True,
    )
    return EvidenceContext(**{**base.__dict__, **overrides})


def _numeric_source() -> SourceQuestion:
    return SourceQuestion(
        question_id="q_numeric",
        course_id=COURSE,
        syllabus_id="syl",
        unit_id="u2",
        unit_label="Unit 2",
        unit_title="Linear Structures",
        concept_ids=("c_stack",),
        concept_labels=("Stacks",),
        text=(
            "A stack holds 8 items and each push costs 2 operations. "
            "How many operations are needed?"
        ),
        format=GenerationFormat.NUMERICAL,
        bloom=BloomLevel.APPLY,
        difficulty=DifficultyLabel.MODERATE,
        objective="Apply Stacks in Unit 2",
        marks=5.0,
        answer_key="16 operations",
        explanation="8 items x 2 operations",
    )


def _mcq_source() -> SourceQuestion:
    return SourceQuestion(
        question_id="q_mcq",
        course_id=COURSE,
        syllabus_id="syl",
        unit_id="u2",
        unit_label="Unit 2",
        unit_title="Linear Structures",
        concept_ids=("c_stack",),
        concept_labels=("Stacks",),
        text="Which structure removes the most recently added item first?",
        format=GenerationFormat.MULTIPLE_CHOICE,
        bloom=BloomLevel.UNDERSTAND,
        difficulty=DifficultyLabel.EASY,
        objective="Explain Stacks in Unit 2",
        marks=1.0,
        answer_key="A stack",
        options=(
            SourceOption(key="A", text="A stack", is_correct=True),
            SourceOption(key="B", text="A queue", is_correct=False),
            SourceOption(key="C", text="A linked list", is_correct=False),
            SourceOption(key="D", text="A tree", is_correct=False),
        ),
        correct_option_key="A",
    )


def _question(**overrides: object) -> GeneratedQuestion:
    passage = _passage()
    base = {
        "question_id": "gq_fixture",
        "batch_id": "gb_fixture",
        "course_id": COURSE,
        "syllabus_id": "syl",
        "unit_id": "u2",
        "unit_label": "Unit 2",
        "unit_title": "Linear Structures",
        "concept_ids": ["c_stack"],
        "concept_labels": ["Stacks"],
        "learning_objective": "Explain Stacks in Unit 2",
        "format": GenerationFormat.EXPLANATION,
        "requested_difficulty": DifficultyLabel.MODERATE,
        "estimated_difficulty": DifficultyLabel.MODERATE,
        "requested_bloom": BloomLevel.UNDERSTAND,
        "estimated_bloom": BloomLevel.UNDERSTAND,
        "marks": 4.0,
        "text": "Explain how a stack removes items, quoting the course notes.",
        "answer_key": "The most recently pushed item is removed first.",
        "source_references": [passage],
        "validation": ValidationResult(
            status=GenerationValidationStatus.VALIDATED, checks=[], issues=[], confidence=1.0
        ),
    }
    return GeneratedQuestion(**{**base, **overrides})


def _validation_context() -> ValidationContext:
    return ValidationContext(
        settings=GenerationSettings(),
        allowed_concept_ids=frozenset({"c_stack", "c_queue"}),
        concept_labels={"c_stack": "Stacks", "c_queue": "Queues"},
        allowed_unit_ids=frozenset({"u2"}),
        evidence=(_passage(),),
    )


# -- A. question mutation ------------------------------------------------------


def test_rephrase_keeps_task_and_answer_but_changes_wording(database: Database) -> None:
    _seed(database)
    source = _source_for(database)
    candidate = mutation.mutate(
        source,
        MutationStrategy.REPHRASE,
        index=0,
        seed=0,
        context=_context(),
    )
    assert candidate.text != source.text
    assert candidate.answer_key == source.answer_key
    assert candidate.objective == source.objective
    assert candidate.concept_ids == list(source.concept_ids)
    assert candidate.changed == ["wording"]
    assert mutation.INVARIANT_ANSWER in candidate.invariants


def test_rephrase_does_not_repeat_personalisation_already_in_the_source(
    database: Database,
) -> None:
    _seed(database)
    source = dataclasses.replace(
        _source_for(database),
        text="Explain stacks in your own words and give one example from the notes.",
    )
    for index in range(4):
        candidate = mutation.mutate(
            source,
            MutationStrategy.REPHRASE,
            index=index,
            seed=0,
            context=_context(),
        )
        assert candidate.text != source.text
        assert candidate.text.casefold().count("in your own words") == 1


def test_scenario_mutation_preserves_objective_and_concepts(database: Database) -> None:
    _seed(database)
    source = _source_for(database)
    candidate = mutation.mutate(
        source, MutationStrategy.SCENARIO, index=1, seed=0, context=_context()
    )
    assert source.text in candidate.text
    assert candidate.objective == source.objective
    assert candidate.concept_ids == list(source.concept_ids)
    assert "scenario" in candidate.changed


def test_numerical_mutation_removes_the_stale_answer_key() -> None:
    candidate = mutation.mutate(
        _numeric_source(),
        MutationStrategy.NUMERICAL,
        index=0,
        seed=1,
        context=_context(),
    )
    assert "8" not in candidate.text
    assert candidate.answer_key is None
    assert candidate.solution_steps == []
    assert "answer_key_removed" in candidate.changed
    assert any("recomputed" in note for note in candidate.limitations)


def test_numerical_mutation_without_numbers_is_refused() -> None:
    source = dataclasses.replace(_numeric_source(), text="Explain stack behaviour.")
    with pytest.raises(AdvancedError) as error:
        mutation.mutate(source, MutationStrategy.NUMERICAL, index=0, seed=0, context=_context())
    assert error.value.code == "no_numeric_parameters"


def test_distractor_mutation_keeps_exactly_one_correct_option() -> None:
    candidate = mutation.mutate(
        _mcq_source(), MutationStrategy.DISTRACTOR, index=0, seed=0, context=_context()
    )
    options = mutation.option_records(candidate)
    correct = [item for item in options if item.is_correct]
    assert len(options) == 4
    assert len(correct) == 1
    assert correct[0].text == "A stack"
    assert candidate.correct_option_key == correct[0].key


def test_unsupported_strategy_for_format_is_refused_with_a_code() -> None:
    with pytest.raises(AdvancedError) as error:
        mutation.mutate(
            _mcq_source(),
            MutationStrategy.APPLICATION,
            index=0,
            seed=0,
            context=_context(),
        )
    assert error.value.code == "unsupported_mutation_format"
    assert error.value.detail["format"] == "multiple_choice"


def test_related_concept_mutation_changes_the_concept_by_design() -> None:
    source = _numeric_source()
    candidate = mutation.mutate(
        source, MutationStrategy.RELATED_CONCEPT, index=0, seed=0, context=_context()
    )
    assert candidate.concept_labels == ["Queues"]
    assert "concept" in candidate.changed
    assert mutation.SPECS[MutationStrategy.RELATED_CONCEPT].invariants == frozenset()


def test_mutation_service_persists_variants_with_their_source(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    batch = _service(database).mutate(
        MutationRequest(
            course_id=COURSE,
            question_id=source.question_id,
            strategies=[MutationStrategy.REPHRASE, MutationStrategy.SCENARIO],
            count=2,
        )
    )
    assert batch.status is GenerationStatus.OK
    assert batch.capability == "mutation"
    assert batch.counts.persisted == 2
    assert {question.origin for question in batch.questions} == {AssessmentOrigin.MUTATION.value}
    assert {question.source_question_id for question in batch.questions} == {source.question_id}
    stored = _service(database).list_variants(COURSE, origin=AssessmentOrigin.MUTATION)
    assert len(stored) == 2
    assert stored[0].review_status is GenerationReviewStatus.PENDING_REVIEW


def test_mutation_requires_an_existing_question(database: Database) -> None:
    _seed(database)
    with pytest.raises(AdvancedError) as error:
        _service(database).mutate(
            MutationRequest(course_id=COURSE, question_id="q_missing", count=1)
        )
    assert error.value.code == "unknown_source_question"


def test_mutation_refuses_a_question_from_another_course(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id=SOURCE_ID))
        CourseRepository(session).upsert(
            make_course(course_id=OTHER_COURSE, course_code="CS999", title="Other course")
        )
    with pytest.raises(AdvancedError) as error:
        _service(database).mutate(
            MutationRequest(course_id=OTHER_COURSE, question_id=source.question_id, count=1)
        )
    assert error.value.code == "course_mismatch"


def test_mutation_refuses_a_question_from_another_syllabus_version(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    other = _add_syllabus(database, suffix="b")
    with pytest.raises(AdvancedError) as error:
        _service(database).mutate(
            MutationRequest(
                course_id=COURSE,
                syllabus_id=other.syllabus_id,
                question_id=source.question_id,
                strategies=[MutationStrategy.REPHRASE],
                count=1,
            )
        )
    assert error.value.code == "syllabus_mismatch"


def test_mutation_refuses_an_unmapped_historical_question(database: Database) -> None:
    _seed(database)
    from exam_engine.models.question import QuestionRecord
    from exam_engine.storage.repositories import QuestionRepository

    _add_document(
        database,
        document_id="doc_paper",
        kind=DocumentKind.QUESTION_PAPER,
        title="SYNTHETIC question paper",
        pages=["1. Explain how a stack differs from a queue."],
    )
    record = QuestionRecord(
        question_id="q_hist_unmapped",
        document_id="doc_paper",
        course_id=COURSE,
        text="Explain how a stack differs from a queue.",
    )
    with database.transaction() as session:
        QuestionRepository(session).replace_for_document("doc_paper", [record])
    with pytest.raises(AdvancedError) as error:
        _service(database).mutate(
            MutationRequest(course_id=COURSE, question_id=record.question_id, count=1)
        )
    assert error.value.code == "source_unmapped"


def test_mutation_request_validation_rejects_duplicate_strategies() -> None:
    with pytest.raises(ValidationError):
        MutationRequest(
            course_id=COURSE,
            question_id="q1",
            strategies=[MutationStrategy.REPHRASE, MutationStrategy.REPHRASE],
        )


def test_mutation_request_above_the_configured_limit_is_refused(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    with pytest.raises(AdvancedError) as error:
        _service(database).mutate(
            MutationRequest(course_id=COURSE, question_id=source.question_id, count=50)
        )
    assert error.value.code == "request_too_large"


def test_partial_batch_failure_keeps_the_successful_variants(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    batch = _service(database).mutate(
        MutationRequest(
            course_id=COURSE,
            question_id=source.question_id,
            strategies=[MutationStrategy.REPHRASE, MutationStrategy.NUMERICAL],
            count=2,
        )
    )
    assert batch.status is GenerationStatus.PARTIAL
    assert batch.counts.persisted == 1
    assert [item.code for item in batch.rejections] == ["unsupported_mutation_format"]
    assert batch.questions[0].strategy == "rephrase"


def test_a_format_constraint_must_match_the_source_format(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    service = _service(database)
    with pytest.raises(AdvancedError) as error:
        service.mutate(
            MutationRequest(
                course_id=COURSE,
                question_id=source.question_id,
                strategies=[MutationStrategy.REPHRASE],
                count=1,
                format=GenerationFormat.MULTIPLE_CHOICE,
            )
        )
    assert error.value.code == "format_constraint_mismatch"
    accepted = service.mutate(
        MutationRequest(
            course_id=COURSE,
            question_id=source.question_id,
            strategies=[MutationStrategy.REPHRASE],
            count=1,
            format=GenerationFormat.EXPLANATION,
        )
    )
    assert accepted.counts.persisted == 1
    assert accepted.questions[0].format is GenerationFormat.EXPLANATION


def test_identical_variant_is_rejected_as_a_duplicate(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    service = _service(database)
    first = service.mutate(
        MutationRequest(
            course_id=COURSE,
            question_id=source.question_id,
            strategies=[MutationStrategy.REPHRASE],
            count=1,
        )
    )
    variant = first.questions[0]
    copy = variant.model_copy(
        update={
            "question_id": "gq_duplicate_copy",
            "review_status": GenerationReviewStatus.PENDING_REVIEW,
        }
    )
    with database.transaction() as session:
        GenerationRepository(session).save_question(copy)
    second = service.mutate(
        MutationRequest(
            course_id=COURSE,
            question_id=source.question_id,
            strategies=[MutationStrategy.REPHRASE],
            count=1,
        )
    )
    assert second.counts.rejected == 1
    assert second.rejections[0].code == "duplicate_exact"
    assert second.duplicates[0].matched_question_id == "gq_duplicate_copy"


def test_no_strategy_applies_when_no_evidence_or_siblings_exist() -> None:
    bundle = _context(sibling_labels=(), passages=())
    assert bundle.has_evidence() is False


# -- mutation invariants (verification layer) ---------------------------------


def _verify(question: GeneratedQuestion, **overrides: object):
    """Verify a candidate against a fixed reference source question."""

    values: dict[str, object] = {
        "allowed_concept_ids": frozenset({"c_stack", "c_queue"}),
        "source": _source_from_question(_question()),
        "spec": mutation.SPECS[MutationStrategy.REPHRASE],
    }
    values.update(overrides)
    return verify(question, _validation_context(), VerificationRequest(**values))  # type: ignore[arg-type]


def _source_from_question(question: GeneratedQuestion) -> SourceQuestion:
    return SourceQuestion(
        question_id="q_source",
        course_id=question.course_id,
        syllabus_id=question.syllabus_id,
        unit_id=question.unit_id,
        unit_label=question.unit_label,
        unit_title=question.unit_title,
        concept_ids=tuple(question.concept_ids),
        concept_labels=tuple(question.concept_labels),
        text="Explain how a stack removes items.",
        format=question.format,
        bloom=question.requested_bloom,
        difficulty=question.requested_difficulty,
        objective=question.learning_objective,
        marks=question.marks,
        answer_key=question.answer_key,
    )


def test_learning_objective_change_is_an_error_when_preservation_required() -> None:
    question = _question(learning_objective="Design a queue instead")
    result = _verify(question, preserve_objective=True)
    assert result.status is GenerationValidationStatus.REJECTED
    codes = {issue.code for issue in result.issues}
    assert "learning_objective_changed" in codes


def test_concept_change_is_an_error_when_preservation_required() -> None:
    question = _question(concept_ids=["c_queue"], concept_labels=["Queues"])
    result = _verify(question, preserve_concept=True)
    assert result.status is GenerationValidationStatus.REJECTED
    assert "concept_changed" in {issue.code for issue in result.issues}


def test_answer_key_change_is_an_error_when_preservation_required() -> None:
    question = _question(answer_key="A queue removes the oldest item.")
    result = _verify(question, preserve_answer=True)
    assert "answer_key_changed" in {issue.code for issue in result.issues}


def test_missing_source_answer_key_needs_review_not_approval() -> None:
    source = dataclasses.replace(_source_from_question(_question()), answer_key=None)
    result = _verify(_question(), preserve_answer=True, source=source)
    assert "answer_unverifiable" in {issue.code for issue in result.issues}
    assert result.status is GenerationValidationStatus.NEEDS_REVIEW


def test_variant_identical_to_its_source_is_rejected() -> None:
    question = _question(text="Explain how a stack removes items.")
    result = _verify(question)
    assert "mutation_identical_to_source" in {issue.code for issue in result.issues}


def test_format_change_without_permission_is_rejected() -> None:
    question = _question(format=GenerationFormat.MULTIPLE_CHOICE)
    result = _verify(question, allow_format_change=False)
    assert "format_changed" in {issue.code for issue in result.issues}


def test_a_source_without_an_answer_key_is_not_reported_as_a_stale_key() -> None:
    """A carried-over key can only be stale when the source actually had one."""

    source = dataclasses.replace(_source_from_question(_question()), answer_key=None)
    question = _question(answer_key=None, text="Design a cache eviction policy for the stack.")
    result = _verify(
        question,
        preserve_answer=True,
        source=source,
        spec=mutation.SPECS[MutationStrategy.COGNITIVE],
    )
    codes = {issue.code for issue in result.issues}
    assert "stale_answer_key" not in codes
    assert "answer_equivalence_unverified" in codes
    assert result.status is not GenerationValidationStatus.REJECTED


def test_semantic_similarity_is_not_treated_as_equivalence(database: Database) -> None:
    _seed(database)
    source = _source_for(database)
    candidate = mutation.mutate(
        source, MutationStrategy.APPLICATION, index=0, seed=0, context=_context()
    )
    question = _question(
        text=candidate.text,
        answer_key=None,
        learning_objective=candidate.objective,
        source_references=list(candidate.source_references),
    )
    result = verify(
        question,
        _validation_context(),
        VerificationRequest(
            allowed_concept_ids=frozenset({"c_stack"}),
            source=source,
            spec=mutation.SPECS[MutationStrategy.APPLICATION],
        ),
    )
    # The application changes the answer, so equivalence is unproven rather than assumed.
    assert "answer_equivalence_unverified" in {issue.code for issue in result.issues}
    assert result.status is not GenerationValidationStatus.VALIDATED


# -- B. adversarial generation -------------------------------------------------


def _challenge(strategy: ChallengeStrategy, **overrides: object):
    target = adversarial.ChallengeTarget(
        concept_id="c_stack",
        concept_label="Stacks",
        unit_label="Unit 2",
        objective="Analyse Stacks in Unit 2",
        concept_ids=("c_stack",),
        unit_id="u2",
    )
    return adversarial.challenge(
        target,
        strategy,
        index=0,
        seed=0,
        context=_context(**overrides),
        difficulty=DifficultyLabel.HARD,
    )


def test_application_challenge_quotes_real_course_material() -> None:
    candidate = _challenge(ChallengeStrategy.UNFAMILIAR_APPLICATION)
    assert "SYNTHETIC lecture notes" in candidate.text
    assert '"' in candidate.text
    assert candidate.answer_key is None
    assert len(candidate.rubric) >= 3
    assert candidate.source_references


def test_multi_step_challenge_sequences_the_reasoning() -> None:
    candidate = _challenge(ChallengeStrategy.MULTI_STEP)
    assert "(a)" in candidate.text and "(b)" in candidate.text and "(c)" in candidate.text


def test_error_diagnosis_challenge_asks_for_the_missing_step() -> None:
    candidate = _challenge(ChallengeStrategy.ERROR_DIAGNOSIS)
    assert "omits" in candidate.text
    assert any("assumption or omitted step" in item for item in candidate.rubric)


def test_edge_case_challenge_stays_within_the_passage() -> None:
    candidate = _challenge(ChallengeStrategy.EDGE_CASE)
    assert "smallest or emptiest case" in candidate.text
    assert candidate.source_references[0].document_id == "doc_notes_phase11"


def test_a_challenge_without_quoted_context_is_rejected() -> None:
    question = _question(
        text="Explain how a stack differs from a queue.",
        answer_key=None,
        source_references=[],
    )
    result = verify(
        question,
        _validation_context(),
        VerificationRequest(
            challenge=ChallengeStrategy.EDGE_CASE,
            allowed_concept_ids=frozenset({"c_stack"}),
            rubric_required=True,
            extra_checks=["adversarial.context_supplied"],
        ),
    )
    codes = {issue.code for issue in result.issues}
    assert "missing_essential_context" in codes
    assert "missing_grading_rubric" in codes
    assert result.status is GenerationValidationStatus.REJECTED


def test_challenge_without_evidence_is_refused() -> None:
    with pytest.raises(AdvancedError) as error:
        _challenge(ChallengeStrategy.BOUNDARY, passages=(), sibling_passages=())
    assert error.value.code == "insufficient_evidence"


def test_prerequisite_challenge_requires_an_in_scope_prerequisite() -> None:
    with pytest.raises(AdvancedError) as error:
        _challenge(
            ChallengeStrategy.MISSING_PREREQUISITE,
            prerequisite_label=None,
            prerequisite_in_syllabus=False,
        )
    assert error.value.code == "no_prerequisite_in_scope"
    candidate = _challenge(ChallengeStrategy.MISSING_PREREQUISITE)
    assert "Arrays" in candidate.text


def test_ambiguous_challenge_is_flagged_for_review() -> None:
    question = _question(
        text="What is a stack? Why does it matter? When would you use it? Quote the notes.",
        origin=AssessmentOrigin.ADVERSARIAL.value,
    )
    result = verify(
        question,
        _validation_context(),
        VerificationRequest(
            challenge=ChallengeStrategy.MULTI_STEP,
            allowed_concept_ids=frozenset({"c_stack"}),
        ),
    )
    assert "ambiguous_task" in {issue.code for issue in result.issues}


def test_out_of_scope_concept_is_rejected() -> None:
    question = _question(concept_ids=["c_tree"], concept_labels=["Trees"])
    result = verify(
        question,
        _validation_context(),
        VerificationRequest(
            challenge=ChallengeStrategy.TRANSFER,
            allowed_concept_ids=frozenset({"c_stack"}),
        ),
    )
    assert "out_of_scope_concept" in {issue.code for issue in result.issues}
    assert result.status is GenerationValidationStatus.REJECTED


def test_missing_rubric_is_an_error_when_one_is_required() -> None:
    question = _question(answer_key=None, solution_steps=[], explanation=None)
    result = verify(
        question,
        _validation_context(),
        VerificationRequest(
            challenge=ChallengeStrategy.EDGE_CASE,
            allowed_concept_ids=frozenset({"c_stack"}),
            rubric_required=True,
        ),
    )
    assert "missing_grading_rubric" in {issue.code for issue in result.issues}


def test_answer_revealed_in_the_question_is_rejected() -> None:
    key = "The most recently pushed item is removed first."
    question = _question(text=f"Explain why: {key} Quote the notes.", answer_key=key)
    result = verify(
        question,
        _validation_context(),
        VerificationRequest(
            challenge=ChallengeStrategy.WHY_WRONG,
            allowed_concept_ids=frozenset({"c_stack"}),
        ),
    )
    assert "answer_revealed_in_question" in {issue.code for issue in result.issues}


def test_multiple_choice_distractors_are_validated() -> None:
    question = _question(
        format=GenerationFormat.MULTIPLE_CHOICE,
        text="Which structure removes the most recent item first?",
        options=[
            GeneratedOption(key="A", text="A stack", is_correct=True),
            GeneratedOption(key="B", text="A queue", is_correct=True),
            GeneratedOption(key="C", text="A tree", is_correct=False),
        ],
        correct_option_key="A",
    )
    result = verify(
        question,
        _validation_context(),
        VerificationRequest(
            challenge=ChallengeStrategy.DISTINGUISH_CONCEPTS,
            allowed_concept_ids=frozenset({"c_stack"}),
        ),
    )
    assert result.status is GenerationValidationStatus.REJECTED
    errors = {issue.code for issue in result.issues if issue.severity is Severity.ERROR}
    assert "mcq_correct_count" in errors
    assert "mcq_answer_key_mismatch" in errors


def test_adversarial_batch_is_persisted_and_never_auto_approved(database: Database) -> None:
    _seed(database)
    batch = _service(database).generate_adversarial(
        AdversarialRequest(course_id=COURSE, concept_ids=["Stacks"], count=3)
    )
    assert batch.status is GenerationStatus.OK
    assert batch.capability == "adversarial"
    assert batch.counts.persisted == 3
    for question in batch.questions:
        assert question.origin == AssessmentOrigin.ADVERSARIAL.value
        assert question.review_status is GenerationReviewStatus.PENDING_REVIEW
        assert question.validation.status is not GenerationValidationStatus.VALIDATED
        assert question.solution_steps
        assert question.generation_metadata["challenge_rationale"]


def test_adversarial_without_material_reports_the_reason(database: Database) -> None:
    _seed(database, notes=False)
    with pytest.raises(AdvancedError) as error:
        _service(database).generate_adversarial(
            AdversarialRequest(course_id=COURSE, concept_ids=["Stacks"], count=2)
        )
    assert error.value.code in {"no_supported_strategy", "insufficient_evidence"}


def test_adversarial_strategy_rejection_does_not_lose_the_batch(database: Database) -> None:
    _seed(database)
    batch = _service(database).generate_adversarial(
        AdversarialRequest(
            course_id=COURSE,
            concept_ids=["Stacks"],
            strategies=[
                ChallengeStrategy.EDGE_CASE,
                ChallengeStrategy.MISSING_PREREQUISITE,
            ],
            count=2,
        )
    )
    assert batch.status is GenerationStatus.PARTIAL
    assert batch.counts.failed == 1
    assert [item.code for item in batch.rejections] == ["no_prerequisite_in_scope"]
    assert batch.questions[0].strategy == ChallengeStrategy.EDGE_CASE.value


def test_adversarial_all_rejected_is_reported_as_failed(database: Database) -> None:
    _seed(database)
    batch = _service(database).generate_adversarial(
        AdversarialRequest(
            course_id=COURSE,
            concept_ids=["Asymptotic notation"],
            strategies=[
                ChallengeStrategy.EDGE_CASE,
                ChallengeStrategy.DISTINGUISH_CONCEPTS,
            ],
            count=2,
        )
    )
    assert batch.status is GenerationStatus.FAILED
    assert batch.questions == []
    assert {item.code for item in batch.rejections} <= {
        "insufficient_evidence",
        "no_related_concept",
        "validation_rejected",
        "duplicate_near",
        "duplicate_exact",
    }
    assert batch.counts.failed == 2


def test_adversarial_request_needs_a_target() -> None:
    with pytest.raises(ValidationError):
        AdversarialRequest(course_id=COURSE, count=1)


def test_adversarial_unknown_code_is_reported(database: Database) -> None:
    _seed(database)
    with pytest.raises(AdvancedError) as error:
        _service(database).generate_adversarial(
            AdversarialRequest(course_id=COURSE, concept_ids=["Quantum annealing"], count=1)
        )
    assert error.value.code in {"unknown_concept", "ambiguous_concept"}


def test_mutation_and_adversarial_share_storage_but_not_identity(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    service = _service(database)
    mutated = service.mutate(
        MutationRequest(
            course_id=COURSE,
            question_id=source.question_id,
            count=1,
            strategies=[MutationStrategy.REPHRASE],
        )
    )
    adversarial_batch = service.generate_adversarial(
        AdversarialRequest(course_id=COURSE, concept_ids=["Stacks"], count=1)
    )
    rows = service.generation.list_questions(GenerationQuery(course_id=COURSE, limit=100))
    origins = {row.origin for row in rows}
    assert {"generation", "mutation", "adversarial"} <= origins
    assert mutated.questions[0].question_id != adversarial_batch.questions[0].question_id
    assert adversarial_batch.questions[0].source_question_id is None


def test_review_decision_is_recorded_on_a_mutation(database: Database) -> None:
    _seed(database)
    source = _generate(database)
    service = _service(database)
    batch = service.mutate(
        MutationRequest(
            course_id=COURSE,
            question_id=source.question_id,
            count=1,
            strategies=[MutationStrategy.REPHRASE],
        )
    )
    question = batch.questions[0]
    updated = service.set_review_status(
        question.question_id, GenerationReviewStatus.APPROVED, note="checked by hand"
    )
    assert updated.review_status is GenerationReviewStatus.APPROVED
    assert updated.review_history[-1].note == "checked by hand"


def test_configuration_limits_are_reported_by_capabilities(database: Database) -> None:
    _seed(database)
    payload = _service(database).capabilities()
    assert payload["phase"] == 11
    assert len(payload["challenge_strategies"]) == len(ChallengeStrategy)
    assert payload["limits"]["max_variants"] >= 1
    assert GENERATION_MARK in {item["strategy"] for item in payload["mutation_strategies"]}


GENERATION_MARK = MutationStrategy.REPHRASE.value
