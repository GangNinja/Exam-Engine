"""Phase 10 tests: grounded generation, validation, dedupe, persistence and review.

Every test runs offline. Model-provider behaviour is exercised through injected
openers, so no network call or paid API is involved. These tests check software
behaviour only; they are not an educational-quality evaluation.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest
from pydantic import ValidationError

from exam_engine.config import GenerationSettings
from exam_engine.generation import (
    GenerationError,
    OpenAICompatibleProvider,
    ProviderError,
    QuestionGenerationService,
    TemplateProvider,
    build_provider,
)
from exam_engine.generation.providers import masked_excerpt
from exam_engine.generation.templates import format_support, template_for
from exam_engine.generation.validation import ValidationContext, validate_question
from exam_engine.models.document import DocumentRecord, PageRecord
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    DocumentKind,
    ExtractionStatus,
    GenerationFormat,
    GenerationReviewStatus,
    GenerationStatus,
    GenerationValidationStatus,
    SourceFormat,
)
from exam_engine.models.generation import (
    GeneratedOption,
    GeneratedQuestion,
    GenerationPrompt,
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
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from tests.support import make_course, make_source

COURSE = "phase10-course-v1"
OTHER_COURSE = "phase10-course-b-v1"
SOURCE = "phase10-fixture"

SYLLABUS_PAGE = (
    "SYNTHETIC Phase 10 syllabus fixture. Topic lists are stored in the parsed "
    "records of this syllabus, not in this sentence."
)

STACK_NOTES = (
    "Stacks A stack is a linear structure with last-in-first-out behaviour, so the "
    "item that was pushed most recently is the first one to be removed. The push "
    "operation adds an item to the top of the stack in constant time, and pop removes "
    "the most recent item. Because both operations only touch the top, a stack backed "
    "by a dynamic array runs in amortised constant time. "
    "Queues A queue is a linear structure with first-in-first-out behaviour; dequeue "
    "removes the item that has waited longest at the front."
)

DEFAULT_UNITS = [
    ("Unit 1", "Foundations", ["Asymptotic notation", "Recurrence relations"]),
    ("Unit 2", "Linear Structures", ["Stacks", "Queues", "Linked lists"]),
    ("Unit 3", "Trees", ["Binary search trees", "Tree traversal"]),
]


# -- fixtures ------------------------------------------------------------------


def _add_course(database: Database, course_id: str = COURSE) -> None:
    with database.transaction() as session:
        DatasetSourceRepository(session).upsert(make_source(source_id=SOURCE))
        CourseRepository(session).upsert(
            make_course(
                course_id=course_id,
                course_code="CS100",
                title="Phase 10 synthetic generation fixture",
            )
        )


def _unit_records(
    syllabus_id: str, units: list[tuple[str, str, list[str]]]
) -> list[SyllabusUnitRecord]:
    return [
        SyllabusUnitRecord(
            unit_id=make_unit_id(syllabus_id, label, title),
            unit_label=label,
            unit_number=index,
            title=title,
            topics=topics,
            page_number=index,
            confidence=0.9,
        )
        for index, (label, title, topics) in enumerate(units, start=1)
    ]


def _add_document(
    database: Database,
    *,
    course_id: str,
    document_id: str,
    kind: DocumentKind,
    title: str,
    pages: list[str],
) -> DocumentRecord:
    document = DocumentRecord(
        document_id=document_id,
        course_id=course_id,
        source_id=SOURCE,
        kind=kind,
        format=SourceFormat.TXT,
        title=title,
        original_filename=f"{document_id}.txt",
        content_sha256=hashlib.sha256(f"{course_id}:{document_id}".encode()).hexdigest(),
        byte_size=sum(len(page) for page in pages),
        page_count=len(pages),
        char_count=sum(len(page) for page in pages),
        extraction_status=ExtractionStatus.OK,
        pipeline_version="phase10-test",
    )
    records = [
        PageRecord(page_number=index, text=text) for index, text in enumerate(pages, start=1)
    ]
    with database.transaction() as session:
        DocumentRepository(session).upsert(document)
        DocumentRepository(session).replace_pages(document_id, records)
    return document


def _add_syllabus(
    database: Database,
    *,
    course_id: str = COURSE,
    suffix: str = "a",
    units: list[tuple[str, str, list[str]]] | None = None,
    page_text: str = SYLLABUS_PAGE,
) -> SyllabusRecord:
    document_id = f"doc_syllabus_{course_id}_{suffix}"
    syllabus_id = make_syllabus_id(document_id)
    syllabus = SyllabusRecord(
        syllabus_id=syllabus_id,
        course_id=course_id,
        document_id=document_id,
        source_id=SOURCE,
        title=f"SYNTHETIC syllabus {suffix}",
        units=_unit_records(syllabus_id, units or DEFAULT_UNITS),
        confidence=0.9,
    )
    _add_document(
        database,
        course_id=course_id,
        document_id=document_id,
        kind=DocumentKind.SYLLABUS,
        title=f"SYNTHETIC syllabus {suffix}",
        pages=[page_text],
    )
    with database.transaction() as session:
        SyllabusRepository(session).replace(syllabus)
    return syllabus


def _add_notes(
    database: Database,
    *,
    course_id: str = COURSE,
    document_id: str = "doc_notes_phase10",
    pages: list[str] | None = None,
) -> DocumentRecord:
    return _add_document(
        database,
        course_id=course_id,
        document_id=document_id,
        kind=DocumentKind.NOTES,
        title="SYNTHETIC lecture notes",
        pages=pages if pages is not None else [STACK_NOTES],
    )


def _add_question_paper(
    database: Database,
    *,
    course_id: str = COURSE,
    document_id: str = "doc_paper_phase10",
    questions: list[tuple[str, str]] | None = None,
) -> dict[str, str]:
    questions = questions or [
        ("q_hist_1", "Explain how a stack differs from a queue. [5 marks]"),
        ("q_hist_2", "Define asymptotic notation and give one example. [3 marks]"),
    ]
    _add_document(
        database,
        course_id=course_id,
        document_id=document_id,
        kind=DocumentKind.QUESTION_PAPER,
        title="SYNTHETIC question paper",
        pages=[
            "\n".join(text for _qid, text in questions),
        ],
    )
    from exam_engine.models.question import QuestionRecord

    records = [
        QuestionRecord(
            question_id=qid,
            document_id=document_id,
            course_id=course_id,
            text=text,
            question_number=str(index),
            number_sort=index,
            marks=5.0,
            page_number=1,
            confidence=0.9,
        )
        for index, (qid, text) in enumerate(questions, start=1)
    ]
    with database.transaction() as session:
        QuestionRepository(session).replace_for_document(document_id, records)
    return dict(questions)


def _seed(database: Database, *, notes: bool = True) -> SyllabusRecord:
    _add_course(database)
    syllabus = _add_syllabus(database)
    if notes:
        _add_notes(database)
    return syllabus


def _service(database: Database, **settings: object) -> QuestionGenerationService:
    return QuestionGenerationService(database, GenerationSettings(**settings))


def _explain(
    database: Database,
    *,
    concepts: list[str] | None = None,
    unit_id: str | None = None,
    count: int = 2,
    fmt: GenerationFormat = GenerationFormat.EXPLANATION,
    bloom: BloomLevel | None = None,
    difficulty: DifficultyLabel | None = None,
    syllabus_id: str | None = None,
    seed: int = 0,
    provider: str = "auto",
    **settings: object,
):
    request = GenerationRequest(
        course_id=COURSE,
        syllabus_id=syllabus_id,
        concept_ids=concepts or [],
        unit_id=unit_id,
        count=count,
        format=fmt,
        bloom=bloom,
        difficulty=difficulty,
        seed=seed,
        provider=provider,
    )
    return _service(database, **settings).generate(request)


def _unit(database: Database, title: str) -> str:
    with database.session() as session:
        syllabus = SyllabusRepository(session).get_by_course(COURSE)[0]
    for unit in syllabus.units:
        if unit.title == title:
            return unit.unit_id
    raise AssertionError(f"no unit titled {title!r}")


def _concept_label(database: Database, unit_id: str, label: str) -> str:
    with database.session() as session:
        syllabus = SyllabusRepository(session).get_by_course(COURSE)[0]
    unit = next(item for item in syllabus.units if item.unit_id == unit_id)
    assert label in unit.topics
    return label


# -- requests and configuration ------------------------------------------------


def test_generation_persists_a_grounded_question(database: Database) -> None:
    _seed(database)
    batch = _explain(database, unit_id=_unit(database, "Linear Structures"), count=2)

    # Both requested questions were produced, so the batch is complete even though
    # every item still needs a human decision.
    assert batch.status is GenerationStatus.OK
    assert batch.counts.requested == 2
    assert batch.counts.persisted == 2
    assert {warning.code for warning in batch.warnings} >= {"generation_review_required"}
    assert batch.provider == "template"
    assert batch.algorithm_version == "phase10.v1"

    for question in batch.questions:
        assert question.course_id == COURSE
        assert question.syllabus_id
        assert question.concept_ids
        assert question.learning_objective
        assert question.format is GenerationFormat.EXPLANATION
        assert question.requested_bloom is BloomLevel.UNDERSTAND
        assert question.requested_difficulty is DifficultyLabel.MODERATE
        assert question.source_references
        assert question.generation_metadata["provider"] == "template"
        assert question.review_status is GenerationReviewStatus.PENDING_REVIEW
        assert question.validation.status is not GenerationValidationStatus.REJECTED
        assert "not verified examination content" in question.limitations[0]

    stored = _service(database).list_questions(GenerationQuery(course_id=COURSE))
    assert {item.question_id for item in stored} == {item.question_id for item in batch.questions}
    assert _service(database).get_batch(batch.batch_id).batch_id == batch.batch_id


def test_generation_is_reproducible_for_the_same_request(database: Database) -> None:
    _seed(database)
    unit_id = _unit(database, "Linear Structures")
    first = _explain(database, unit_id=unit_id, count=2)
    second = _explain(database, unit_id=unit_id, count=2)

    assert first.batch_id == second.batch_id
    assert [item.question_id for item in first.questions] == [
        item.question_id for item in second.questions
    ]
    assert [item.text for item in first.questions] == [item.text for item in second.questions]
    stored = _service(database).list_questions(GenerationQuery(course_id=COURSE))
    assert len(stored) == 2


def test_unknown_course_is_rejected(database: Database) -> None:
    _seed(database)
    with pytest.raises(GenerationError) as exc:
        _service(database).generate(
            GenerationRequest(course_id="no-such-course", concept_ids=["Stacks"], count=1)
        )
    assert exc.value.code == "unknown_course"
    assert "no-such-course" in exc.value.message


def test_unknown_concept_is_rejected(database: Database) -> None:
    _seed(database)
    with pytest.raises(GenerationError) as exc:
        _service(database).generate(
            GenerationRequest(course_id=COURSE, concept_ids=["quantum chromodynamics"], count=1)
        )
    assert exc.value.code == "unknown_concept"


def test_concept_from_another_course_is_not_substituted(database: Database) -> None:
    _seed(database)
    _add_course(database, OTHER_COURSE)
    _add_syllabus(
        database,
        course_id=OTHER_COURSE,
        suffix="b",
        units=[("Unit 1", "Optics", ["Lenses"])],
    )
    with pytest.raises(GenerationError) as exc:
        _service(database).generate(
            GenerationRequest(course_id=COURSE, concept_ids=["Lenses"], count=1)
        )
    assert exc.value.code == "unknown_concept"


def test_unknown_and_ambiguous_syllabus_versions(database: Database) -> None:
    _seed(database)
    service = _service(database)
    with pytest.raises(GenerationError) as exc:
        service.generate(
            GenerationRequest(
                course_id=COURSE,
                syllabus_id="syl_missing",
                concept_ids=["Stacks"],
                count=1,
            )
        )
    assert exc.value.code == "unknown_syllabus"

    _add_syllabus(
        database,
        suffix="v2",
        units=[("Unit 1", "Linear Structures", ["Stacks", "Queues", "Linked lists"])],
    )
    with pytest.raises(GenerationError) as exc:
        service.generate(GenerationRequest(course_id=COURSE, concept_ids=["Stacks"], count=1))
    assert exc.value.code == "ambiguous_syllabus"
    assert len(exc.value.detail["available"]) == 2


def test_syllabus_versions_stay_isolated(database: Database) -> None:
    first = _seed(database)
    second = _add_syllabus(
        database,
        suffix="v2",
        units=[("Unit 1", "Linear Structures", ["Stacks", "Queues", "Linked lists"])],
    )
    batch = _explain(database, concepts=["Stacks"], count=1, syllabus_id=second.syllabus_id)
    assert batch.request.syllabus_id == second.syllabus_id
    assert batch.questions[0].syllabus_id == second.syllabus_id
    assert batch.questions[0].syllabus_id != first.syllabus_id
    stored_old = _service(database).list_questions(
        GenerationQuery(course_id=COURSE, syllabus_id=first.syllabus_id)
    )
    assert stored_old == []


def test_unsupported_format_for_requested_bloom(database: Database) -> None:
    _seed(database)
    with pytest.raises(GenerationError) as exc:
        _service(database).generate(
            GenerationRequest(
                course_id=COURSE,
                concept_ids=["Stacks"],
                count=1,
                format=GenerationFormat.MULTIPLE_CHOICE,
                bloom=BloomLevel.CREATE,
            )
        )
    assert exc.value.code == "unsupported_format_bloom"
    assert "deterministic template provider" in exc.value.message


def test_numerical_format_needs_a_model_provider(database: Database) -> None:
    _seed(database)
    with pytest.raises(GenerationError) as exc:
        _service(database).generate(
            GenerationRequest(
                course_id=COURSE,
                concept_ids=["Stacks"],
                count=1,
                format=GenerationFormat.NUMERICAL,
            )
        )
    assert exc.value.code == "unsupported_format"
    assert "language-model provider" in exc.value.message


def test_request_size_limit_is_enforced(database: Database) -> None:
    _seed(database)
    with pytest.raises(GenerationError) as exc:
        _service(database, max_questions=3).generate(
            GenerationRequest(course_id=COURSE, concept_ids=["Stacks"], count=4)
        )
    assert exc.value.code == "request_too_large"
    assert exc.value.detail == {"requested": 4, "limit": 3}


def test_invalid_enum_values_are_rejected_by_the_request_model() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(
            course_id=COURSE,
            concept_ids=["Stacks"],
            format="not_a_format",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        GenerationRequest(
            course_id=COURSE,
            concept_ids=["Stacks"],
            bloom="remembering_everything",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        GenerationRequest(course_id=COURSE, concept_ids=["Stacks"], count=0)


def test_request_needs_a_concept_or_unit() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(course_id=COURSE)


def test_unsupported_language_is_rejected(database: Database) -> None:
    _seed(database)
    with pytest.raises(GenerationError) as exc:
        _service(database).generate(
            GenerationRequest(course_id=COURSE, concept_ids=["Stacks"], count=1, language="fr")
        )
    assert exc.value.code == "unsupported_language"


# -- formats and cognitive levels ---------------------------------------------


def test_multiple_choice_masks_the_answer(database: Database) -> None:
    _seed(database)
    batch = _explain(
        database,
        concepts=["Stacks"],
        count=1,
        fmt=GenerationFormat.MULTIPLE_CHOICE,
    )
    question = batch.questions[0]
    assert question.format is GenerationFormat.MULTIPLE_CHOICE
    assert 3 <= len(question.options) <= 6
    assert sum(1 for option in question.options if option.is_correct) == 1
    correct = next(option for option in question.options if option.is_correct)
    assert question.correct_option_key == correct.key
    assert question.answer_key == correct.text
    assert "_____" in question.text
    assert correct.text.casefold() not in question.text.casefold()

    codes = {issue.code for issue in question.validation.issues}
    assert "answer_leakage" not in codes
    assert "mcq_ambiguous_stem" not in codes
    assert "mcq_duplicate_options" not in codes
    assert set(question.distractor_explanations) == {
        option.key for option in question.options if not option.is_correct
    }


def _mcq_prompt() -> GenerationPrompt:
    return GenerationPrompt(
        course_id=COURSE,
        syllabus_id="syl_1",
        concept_id="concept_stacks",
        concept_label="Stacks",
        concept_terms=["Stacks", "stack"],
        unit_id="unit_2",
        unit_label="Unit 2",
        learning_objective="Explain Stacks",
        related_labels=["Queues", "Linked lists"],
        passages=[
            SourcePassage(
                document_id="doc_syllabus",
                document_title="Syllabus",
                document_kind=DocumentKind.SYLLABUS,
                excerpt="Unit 2 covers Stacks; Queues; Linked lists; and their operations.",
            ),
            SourcePassage(
                document_id="doc_notes",
                document_title="Lecture notes",
                document_kind=DocumentKind.NOTES,
                excerpt="A stack is a linear structure with last-in-first-out discipline.",
            ),
        ],
        format=GenerationFormat.MULTIPLE_CHOICE,
        bloom=BloomLevel.UNDERSTAND,
        difficulty=DifficultyLabel.MODERATE,
        marks=1.0,
        language="en",
    )


def test_comparison_grounds_and_cites_a_same_unit_sibling(database: Database) -> None:
    """A comparison needs evidence for the second concept, not only a graph edge."""
    _seed(database)
    batch = _explain(
        database,
        concepts=["Stacks"],
        count=1,
        fmt=GenerationFormat.COMPARISON,
        bloom=BloomLevel.ANALYZE,
    )
    assert batch.questions, [item.code for item in batch.rejections]
    question = batch.questions[0]
    assert question.format is GenerationFormat.COMPARISON
    assert "Queues" in question.text or "Linked lists" in question.text
    assert question.answer_key is not None
    assert "Reference material:" in question.answer_key
    assert len(question.source_references) >= 2
    assert question.validation.status is not GenerationValidationStatus.REJECTED


def test_answer_key_may_quote_a_passage_the_question_cites() -> None:
    other = SourcePassage(
        document_id="doc_sibling",
        document_title="Notes 2",
        document_kind=DocumentKind.NOTES,
        excerpt="A queue is a linear structure with first-in-first-out behaviour.",
    )
    question = _question(
        answer_key=(
            'Reference material: Notes - "a stack is a linear structure"; '
            'Notes 2 - "a queue is a linear structure with first-in-first-out behaviour".'
        ),
        source_references=[other],
        marks=6.0,
    )
    result = validate_question(question, _context())
    assert "answer_key_unverified" not in _codes(result)


def test_multiple_choice_avoids_a_passage_that_lists_its_siblings() -> None:
    draft = TemplateProvider(GenerationSettings()).generate(_mcq_prompt())
    assert "Lecture notes" in draft.text
    assert "Queues" not in draft.text
    assert "Linked lists" not in draft.text
    assert draft.answer_key == "Stacks"


def test_multiple_choice_rejects_an_ambiguous_stem(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passage that lists several sibling concepts cannot ground a cloze item."""
    _seed(database)
    unit_id = _unit(database, "Linear Structures")
    _add_notes(
        database,
        document_id="doc_notes_listing",
        pages=["Unit 2 covers Stacks; Queues; Linked lists; and their operations."],
    )
    # Force the listing passage to be the only evidence for the concept.
    service = _service(database)
    with database.transaction() as session:
        DocumentRepository(session).delete("doc_notes_phase10")
    batch = service.generate(
        GenerationRequest(
            course_id=COURSE,
            unit_id=unit_id,
            count=2,
            format=GenerationFormat.MULTIPLE_CHOICE,
        )
    )
    assert batch.counts.rejected >= 1
    codes = {issue.code for rejection in batch.rejections for issue in rejection.issues}
    assert "mcq_ambiguous_stem" in codes
    # Nothing persisted may carry the ambiguity it was rejected for.
    assert all(
        "mcq_ambiguous_stem" not in {issue.code for issue in question.validation.issues}
        for question in batch.questions
    )


def test_unsupported_format_is_reported_for_thin_evidence(database: Database) -> None:
    _seed(database, notes=False)
    batch = _explain(
        database,
        concepts=["Stacks"],
        count=1,
        fmt=GenerationFormat.MULTIPLE_CHOICE,
    )
    assert batch.questions == []
    assert batch.rejections[0].code == "insufficient_evidence"
    assert "describes the concept" in batch.rejections[0].message


def test_capabilities_report_model_only_formats(database: Database) -> None:
    _seed(database)
    rows = _service(database).capabilities()
    assert {row["format"] for row in rows} >= {"multiple_choice", "explanation"}
    numerical = next(row for row in rows if row["format"] == "numerical")
    assert numerical["requires_model"] is True
    assert numerical["supported_by_provider"] == []
    assert template_for(GenerationFormat.NUMERICAL, BloomLevel.APPLY) is not None
    for support in format_support():
        assert support.blooms
        assert support.templates


def test_masked_excerpt_hides_aliases() -> None:
    excerpt = "A binary search tree keeps keys sorted so that lookup is logarithmic."
    masked = masked_excerpt(excerpt, ["Binary search trees", "binary search tree"])
    assert masked is not None
    assert "binary search tree" not in masked.casefold()
    assert "logarithmic" in masked
    assert masked_excerpt("Stacks", ["Stacks"]) is None
    assert masked_excerpt(STACK_NOTES, ["Treaps"]) is None


# -- providers -----------------------------------------------------------------


def _draft_payload(**overrides: object) -> str:
    payload = {
        "text": "Explain why a stack reverses the order of its inputs.",
        "answer_key": "Because the most recent item is removed first.",
        "explanation": "Last-in-first-out ordering follows from the push/pop discipline.",
        "solution_steps": [],
    }
    payload.update(overrides)
    return json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]})


class _Response:
    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def _provider(
    opener, *, retries: int = 2, sleeps: list[float] | None = None
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key="test-key",
        settings=GenerationSettings(provider_max_retries=retries),
        opener=opener,
        sleep=(sleeps.append if sleeps is not None else (lambda _delay: None)),
    )


_STUB_PROMPT = None


def _prompt(database: Database):
    global _STUB_PROMPT
    if _STUB_PROMPT is None:
        service = _service(database)
        request = GenerationRequest(course_id=COURSE, concept_ids=["Stacks"], count=1)
        loaded = service._load(request)  # noqa: SLF001 - test seam
        plan = service._plan(request, loaded)  # noqa: SLF001
        concept = loaded.concept_for("Stacks")
        _STUB_PROMPT = service._prompt(  # noqa: SLF001
            loaded=loaded, plan=plan, request=request, concept=concept, index=0, cache={}
        )
    return _STUB_PROMPT


def test_provider_parses_a_valid_response(database: Database) -> None:
    _seed(database)
    provider = _provider(lambda request, timeout: _Response(_draft_payload()))
    draft = provider.generate(_prompt(database))
    assert draft.text.startswith("Explain why a stack")
    assert draft.metadata["provider"] == "openai"
    assert draft.metadata["attempts"] == 1


def test_provider_rejects_malformed_json(database: Database) -> None:
    _seed(database)
    provider = _provider(lambda request, timeout: _Response("{not json"), retries=0)
    with pytest.raises(ProviderError) as exc:
        provider.generate(_prompt(database))
    assert exc.value.code == "provider_invalid_response"


def test_provider_rejects_an_empty_response(database: Database) -> None:
    _seed(database)
    provider = _provider(lambda request, timeout: _Response(""), retries=0)
    with pytest.raises(ProviderError) as exc:
        provider.generate(_prompt(database))
    assert exc.value.code == "provider_empty_response"


def test_provider_rejects_a_schema_violation(database: Database) -> None:
    _seed(database)
    provider = _provider(lambda request, timeout: _Response(_draft_payload(text="")), retries=0)
    with pytest.raises(ProviderError) as exc:
        provider.generate(_prompt(database))
    assert exc.value.code == "provider_invalid_response"
    assert "schema" in exc.value.message


def test_provider_timeout_retries_then_fails(database: Database) -> None:
    _seed(database)
    calls: list[float] = []

    def opener(request, timeout):
        calls.append(timeout)
        raise TimeoutError("slow")

    sleeps: list[float] = []
    provider = _provider(opener, retries=2, sleeps=sleeps)
    with pytest.raises(ProviderError) as exc:
        provider.generate(_prompt(database))
    assert exc.value.code == "provider_timeout"
    assert len(calls) == 3
    assert exc.value.detail["attempts"] == 3
    assert sleeps == [0.5, 1.0]


def test_provider_rate_limit_retries_and_succeeds(database: Database) -> None:
    _seed(database)
    calls: list[int] = []

    def opener(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid/v1/chat/completions",
                429,
                "Too Many Requests",
                {"Retry-After": "0"},
                None,
            )
        return _Response(_draft_payload())

    sleeps: list[float] = []
    provider = _provider(opener, retries=2, sleeps=sleeps)
    draft = provider.generate(_prompt(database))
    assert draft.metadata["attempts"] == 2
    assert len(calls) == 2


def test_provider_server_error_is_not_hidden(database: Database) -> None:
    _seed(database)

    def opener(request, timeout):
        raise urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions", 503, "Service Unavailable", {}, None
        )

    provider = _provider(opener, retries=1, sleeps=[])
    with pytest.raises(ProviderError) as exc:
        provider.generate(_prompt(database))
    assert exc.value.code == "provider_error"
    assert exc.value.detail["attempts"] == 2


def test_auto_provider_falls_back_to_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EXAM_ENGINE_Q10_LLM_MODEL", raising=False)
    provider = build_provider("auto", GenerationSettings(), env={})
    assert isinstance(provider, TemplateProvider)


def test_explicit_model_provider_requires_configuration() -> None:
    with pytest.raises(ProviderError) as exc:
        build_provider("openai", GenerationSettings(), env={})
    assert exc.value.code == "provider_not_configured"
    with pytest.raises(ProviderError) as exc:
        build_provider("mystery", GenerationSettings(), env={})
    assert exc.value.code == "invalid_provider"


def test_generated_question_can_be_produced_by_a_model_provider(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(database)
    monkeypatch.setenv("EXAM_ENGINE_Q10_LLM_MODEL", "test-model")
    monkeypatch.setenv("EXAM_ENGINE_Q10_LLM_API_KEY", "test-key")
    monkeypatch.setenv("EXAM_ENGINE_Q10_LLM_BASE_URL", "https://example.invalid/v1")

    def opener(request, timeout):
        return _Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "text": (
                                            "Explain, in your own words, why the "
                                            "last-in-first-out rule follows from the "
                                            "stack operations described in the notes."
                                        ),
                                        "answer_key": "Because pop removes the most recent item.",
                                        "explanation": "The notes describe LIFO behaviour.",
                                        "solution_steps": [],
                                    }
                                )
                            }
                        }
                    ]
                }
            )
        )

    service = QuestionGenerationService(database, GenerationSettings(), provider=_provider(opener))
    batch = service.generate(
        GenerationRequest(
            course_id=COURSE,
            concept_ids=["Stacks"],
            count=1,
            provider="openai",
            format=GenerationFormat.EXPLANATION,
            bloom=BloomLevel.UNDERSTAND,
        )
    )
    assert batch.provider == "openai"
    assert batch.model == "test-model"
    assert batch.questions and batch.questions[0].generation_metadata["provider"] == "openai"


def test_model_output_without_options_is_rejected(database: Database) -> None:
    _seed(database)

    def opener(request, timeout):
        return _Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "text": "Which option explains stack behaviour?",
                                        "correct_index": 0,
                                    }
                                )
                            }
                        }
                    ]
                }
            )
        )

    service = QuestionGenerationService(
        database, GenerationSettings(provider_max_retries=0), provider=_provider(opener, retries=0)
    )
    batch = service.generate(
        GenerationRequest(
            course_id=COURSE,
            concept_ids=["Stacks"],
            count=1,
            provider="openai",
            format=GenerationFormat.MULTIPLE_CHOICE,
        )
    )
    assert batch.questions == []
    assert batch.rejections[0].code == "validation_rejected"
    codes = {issue.code for issue in batch.rejections[0].issues}
    assert "format_mismatch" in codes
    assert batch.status is GenerationStatus.FAILED


def test_partial_batch_success_is_reported(database: Database) -> None:
    _seed(database, notes=False)
    _add_notes(
        database,
        document_id="doc_notes_long",
        pages=[
            "Stacks A stack is a linear structure with last-in-first-out behaviour "
            "where the item pushed most recently is removed first, so a stack backed "
            "by a dynamic array runs both push and pop in amortised constant time "
            "even when the array has to grow during a long sequence of insertions "
            "and the old storage must be copied into a larger buffer before use."
        ],
    )
    unit_id = _unit(database, "Linear Structures")
    batch = _explain(database, unit_id=unit_id, count=3, fmt=GenerationFormat.CASE_BASED)
    assert batch.counts.requested == 3
    assert batch.counts.persisted >= 1
    assert batch.counts.failed >= 1
    assert batch.status is GenerationStatus.PARTIAL
    codes = {rejection.code for rejection in batch.rejections}
    assert "insufficient_case_context" in codes


# -- validation ----------------------------------------------------------------


def _question(
    *,
    text: str = "Explain why a stack removes the most recently added item.",
    fmt: GenerationFormat = GenerationFormat.EXPLANATION,
    options: list[GeneratedOption] | None = None,
    correct_key: str | None = None,
    answer_key: str | None = "Because the top item is removed first.",
    concept_ids: list[str] | None = None,
    unit_id: str = "unit_1",
    requested_bloom: BloomLevel = BloomLevel.UNDERSTAND,
    estimated_bloom: BloomLevel | None = BloomLevel.UNDERSTAND,
    difficulty: DifficultyLabel = DifficultyLabel.MODERATE,
    estimated_difficulty: DifficultyLabel = DifficultyLabel.MODERATE,
    source_references: list[SourcePassage] | None = None,
    solution_steps: list[str] | None = None,
    marks: float = 4.0,
) -> GeneratedQuestion:
    return GeneratedQuestion(
        question_id="gq_test",
        batch_id="gb_test",
        course_id=COURSE,
        syllabus_id="syl_test",
        unit_id=unit_id,
        unit_label="Unit 1",
        concept_ids=concept_ids or ["concept_stacks"],
        concept_labels=["Stacks"],
        learning_objective="Explain Stacks in Unit 1",
        format=fmt,
        requested_difficulty=difficulty,
        estimated_difficulty=estimated_difficulty,
        requested_bloom=requested_bloom,
        estimated_bloom=estimated_bloom,
        marks=marks,
        text=text,
        options=options or [],
        correct_option_key=correct_key,
        answer_key=answer_key,
        source_references=source_references
        if source_references is not None
        else [
            SourcePassage(
                document_id="doc_notes",
                document_title="Notes",
                document_kind=DocumentKind.NOTES,
                excerpt="A stack is a linear structure with last-in-first-out behaviour.",
            )
        ],
        solution_steps=solution_steps or [],
        validation=ValidationResult(status=GenerationValidationStatus.VALIDATION_INCOMPLETE),
    )


def _context(**overrides: object) -> ValidationContext:
    defaults = {
        "settings": GenerationSettings(),
        "allowed_concept_ids": frozenset({"concept_stacks"}),
        "concept_labels": {"concept_stacks": "Stacks"},
        "allowed_unit_ids": frozenset({"unit_1"}),
        "evidence": (
            SourcePassage(
                document_id="doc_notes",
                document_title="Notes",
                document_kind=DocumentKind.NOTES,
                excerpt="A stack is a linear structure with last-in-first-out behaviour.",
            ),
        ),
    }
    defaults.update(overrides)
    return ValidationContext(**defaults)  # type: ignore[arg-type]


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_validation_accepts_a_well_formed_question() -> None:
    result = validate_question(_question(), _context())
    assert "structure.required_fields" in result.checks
    assert "alignment.source_evidence" in result.checks
    assert "mcq.option_count" not in result.checks
    assert result.status is not GenerationValidationStatus.REJECTED


def test_validation_flags_structure_and_scope_problems() -> None:
    question = _question(
        text="Explain stacks.",
        concept_ids=["concept_unknown"],
        unit_id="unit_other",
        source_references=[],
    )
    result = validate_question(question, _context(evidence=()))
    codes = _codes(result)
    assert result.status is GenerationValidationStatus.REJECTED
    assert {"concept_out_of_scope", "unit_out_of_scope", "question_too_short"} <= codes
    assert "missing_source_evidence" in codes


def test_validation_flags_missing_answer_key_and_solution_steps() -> None:
    numerical = _question(
        fmt=GenerationFormat.NUMERICAL,
        answer_key="42",
        solution_steps=[],
        marks=5.0,
    )
    result = validate_question(numerical, _context())
    assert "missing_solution_steps" in _codes(result)

    short = _question(
        fmt=GenerationFormat.SHORT_ANSWER,
        answer_key=None,
        marks=3.0,
    )
    result = validate_question(short, _context())
    assert "missing_answer_key" in _codes(result)
    assert result.status is GenerationValidationStatus.REJECTED


def test_validation_flags_unverified_answer_keys() -> None:
    fabricated = _question(
        answer_key='The course says "stacks are always faster than queues in every case".',
        marks=4.0,
    )
    result = validate_question(fabricated, _context())
    assert "answer_key_unverified" in _codes(result)


def test_answer_evidence_requirement_decides_whether_a_fabricated_key_rejects() -> None:
    fabricated = _question(
        answer_key='The course says "stacks are always faster than queues in every case".',
        marks=4.0,
    )
    strict = validate_question(fabricated, _context())
    assert strict.status is GenerationValidationStatus.REJECTED

    lenient = validate_question(
        fabricated,
        _context(settings=GenerationSettings(require_evidence_for_answer=False)),
    )
    assert lenient.status is GenerationValidationStatus.NEEDS_REVIEW
    assert "answer_key_unverified" in _codes(lenient)


def test_multiple_choice_validation_rules() -> None:
    options = [
        GeneratedOption(key="A", text="A stack", is_correct=True),
        GeneratedOption(key="B", text="A stack", is_correct=False),
        GeneratedOption(key="C", text="None of the above", is_correct=False),
    ]
    question = _question(
        fmt=GenerationFormat.MULTIPLE_CHOICE,
        text="Which structure removes the most recent item?",
        options=options,
        correct_key="A",
        answer_key="A stack",
        marks=1.0,
    )
    result = validate_question(question, _context())
    codes = _codes(result)
    assert {"mcq_duplicate_options", "mcq_meta_option", "mcq_missing_distractor_notes"} <= codes
    assert result.status is GenerationValidationStatus.REJECTED

    leaky = _question(
        fmt=GenerationFormat.MULTIPLE_CHOICE,
        text="A stack keeps last-in-first-out order. Which structure is this?",
        options=[
            GeneratedOption(key="A", text="A stack", is_correct=True),
            GeneratedOption(key="B", text="A queue", is_correct=False),
            GeneratedOption(key="C", text="A tree", is_correct=False),
        ],
        correct_key="A",
        answer_key="A stack",
        marks=1.0,
    )
    assert "answer_leakage" in _codes(validate_question(leaky, _context()))


def test_validation_flags_bloom_and_difficulty_mismatch() -> None:
    question = _question(
        requested_bloom=BloomLevel.EVALUATE,
        estimated_bloom=BloomLevel.REMEMBER,
        difficulty=DifficultyLabel.HARD,
        estimated_difficulty=DifficultyLabel.EASY,
    )
    result = validate_question(question, _context())
    codes = _codes(result)
    assert "bloom_mismatch" in codes
    assert "difficulty_mismatch" in codes
    assert result.status is GenerationValidationStatus.NEEDS_REVIEW


def test_validation_reports_thin_evidence_and_foreign_material() -> None:
    question = _question(
        text="Explain Stacks using the syllabus entry.",
    )
    result = validate_question(
        question,
        _context(
            thin_evidence=True,
            forbidden_labels=frozenset({"Stacks of photon lenses"}),
        ),
    )
    assert "thin_evidence" in _codes(result)


# -- dedupe and persistence ----------------------------------------------------


def test_exact_duplicate_of_an_existing_generated_question_is_skipped(
    database: Database,
) -> None:
    _seed(database)
    unit_id = _unit(database, "Linear Structures")
    first = _explain(database, unit_id=unit_id, count=1)
    assert first.counts.persisted == 1
    expected = first.questions[0].text

    # A second request with a different seed and count still targets the same
    # concept; write the identical text into the store to force the collision.
    service = _service(database)
    stored = service.list_questions(GenerationQuery(course_id=COURSE))
    with database.transaction() as session:
        from exam_engine.storage.generation_repository import GenerationRepository

        duplicate = stored[0].model_copy(deep=True)
        duplicate.question_id = "gq_duplicate"
        duplicate.text = expected
        GenerationRepository(session).save_question(duplicate, allow_overwrite_approved=True)

    batch = _explain(database, unit_id=unit_id, count=1, seed=1)
    assert batch.counts.duplicates == 1
    assert batch.duplicates[0].kind == "exact"
    assert batch.questions == []
    assert batch.status is GenerationStatus.FAILED


def test_near_duplicate_of_a_historical_question_is_flagged(database: Database) -> None:
    _seed(database)
    _add_question_paper(
        database,
        questions=[
            (
                "q_hist_1",
                "Explain Stacks and why it matters in Unit 2. Use your own words and "
                "refer to the course material where it helps.",
            )
        ],
    )
    batch = _explain(database, concepts=["Stacks"], count=1)
    assert batch.counts.duplicates == 1
    assert "historical" in batch.duplicates[0].reason
    assert batch.duplicates[0].matched_question_id == "q_hist_1"
    assert batch.duplicates[0].kind in {"exact", "near", "semantic"}


def test_a_variant_on_the_same_concept_is_kept(database: Database) -> None:
    _seed(database)
    _add_question_paper(
        database,
        questions=[
            (
                "q_hist_1",
                "Evaluate how Stacks are presented in Unit 2 and justify each judgement.",
            )
        ],
    )
    batch = _explain(database, concepts=["Stacks"], count=1)
    assert batch.counts.persisted == 1
    assert batch.counts.duplicates == 0
    assert batch.questions[0].format is GenerationFormat.EXPLANATION


def test_historical_questions_are_never_modified(database: Database) -> None:
    _seed(database)
    historical = _add_question_paper(database)
    before = dict(historical)
    _explain(database, unit_id=_unit(database, "Linear Structures"), count=2)
    with database.session() as session:
        records = QuestionRepository(session).list_by_course(COURSE, limit=100)
    assert {item.question_id: item.text for item in records} == before


def test_review_transitions_and_approved_questions_are_protected(
    database: Database,
) -> None:
    _seed(database)
    unit_id = _unit(database, "Linear Structures")
    batch = _explain(database, unit_id=unit_id, count=1)
    question = batch.questions[0]
    service = _service(database)

    approved = service.set_review_status(
        question.question_id, GenerationReviewStatus.APPROVED, note="reviewer ok"
    )
    assert approved.review_status is GenerationReviewStatus.APPROVED
    assert approved.review_history[-1].note == "reviewer ok"
    assert service.review_summary(COURSE, approved.syllabus_id)["approved"] == 1

    regenerated = service.generate(
        GenerationRequest(
            course_id=COURSE,
            unit_id=unit_id,
            count=1,
            format=GenerationFormat.EXPLANATION,
        )
    )
    assert regenerated.counts.reused_approved == 1
    assert regenerated.questions[0].review_status is GenerationReviewStatus.APPROVED

    with pytest.raises(GenerationError) as exc:
        service.set_review_status(
            question.question_id,
            GenerationReviewStatus.REJECTED,
            expected_revision=approved.revision + 99,
        )
    assert exc.value.code == "revision_conflict"

    reopened = service.set_review_status(
        question.question_id, GenerationReviewStatus.PENDING_REVIEW
    )
    assert reopened.review_status is GenerationReviewStatus.PENDING_REVIEW
    assert len(reopened.review_history) == 3


def test_batch_review_and_export(database: Database, tmp_path: Path) -> None:
    _seed(database)
    batch = _explain(database, unit_id=_unit(database, "Linear Structures"), count=2)
    service = _service(database)
    updated = service.review_batch(batch.batch_id, GenerationReviewStatus.APPROVED, note="batch ok")
    assert len(updated) == 2
    assert all(item.review_status is GenerationReviewStatus.APPROVED for item in updated)

    path = service.export_batch(batch.batch_id, tmp_path / "batch.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["batch_id"] == batch.batch_id
    assert len(payload["questions"]) == 2
    assert payload["schema_version"] == "phase10-v1"

    with pytest.raises(GenerationError) as exc:
        service.get_batch("gb_missing")
    assert exc.value.code == "unknown_batch"
    with pytest.raises(GenerationError) as exc:
        service.get_question("gq_missing")
    assert exc.value.code == "unknown_generated_question"


def test_failed_batch_records_why_no_question_was_produced(database: Database) -> None:
    _seed(database, notes=False)
    batch = _explain(
        database,
        concepts=["Stacks"],
        count=1,
        fmt=GenerationFormat.MULTIPLE_CHOICE,
    )
    assert batch.status is GenerationStatus.FAILED
    assert batch.counts.requested == 1
    assert batch.counts.failed == 1
    assert batch.rejections[0].concept_label == "Stacks"
    stored = _service(database).list_batches(COURSE)
    assert stored[0]["batch_id"] == batch.batch_id
    assert stored[0]["status"] == "failed"


def test_batch_report_serialises_stably(database: Database) -> None:
    _seed(database)
    batch = _explain(database, unit_id=_unit(database, "Linear Structures"), count=1)
    first = batch.model_dump(mode="json")
    reloaded = _service(database).get_batch(batch.batch_id).model_dump(mode="json")
    assert json.dumps(first, sort_keys=True) == json.dumps(reloaded, sort_keys=True)
    assert first["scope"]["statement"]
    assert first["parameters"]["provider"]["name"] == "template"


def test_evidence_excludes_examination_documents(database: Database) -> None:
    _seed(database)
    _add_question_paper(database)
    batch = _explain(database, unit_id=_unit(database, "Linear Structures"), count=2)
    for question in batch.questions:
        for reference in question.source_references:
            assert reference.document_kind not in {
                DocumentKind.QUESTION_PAPER,
                DocumentKind.QUESTION_BANK,
            }
    assert any("historical" in item.lower() for item in [batch.scope.statement])
