from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from exam_engine.models import (
    BatchIngestionResult,
    CourseRecord,
    DatasetSource,
    DatasetType,
    DocumentRecord,
    ExtractionMethod,
    ExtractionStatus,
    IngestionError,
    IngestionResult,
    IngestionStatus,
    PageRecord,
    QuestionRecord,
    SourceFormat,
    Uncertainty,
    make_document_id,
    make_question_id,
)
from tests.support import make_course, make_source


def test_course_normalises_code_and_validates_slug() -> None:
    course = make_course(course_code=" cs101 ", institution_code=" Test-INST ")
    assert course.course_code == "CS101"
    assert course.institution_code == "test-inst"
    with pytest.raises(ValidationError):
        CourseRecord(
            course_id="Not A Slug",
            course_code="CS1",
            title="x",
            institution_code="i",
            institution_name="I",
        )


def test_synthetic_source_is_self_labelling() -> None:
    source = make_source()
    assert source.is_synthetic is True
    assert source.dataset_type is DatasetType.SYNTHETIC
    assert any("synthetic" in warning for warning in source.provenance_warnings())


def test_openly_licensed_requires_a_licence() -> None:
    with pytest.raises(ValidationError):
        DatasetSource(
            source_id="no-licence",
            name="Public but unlicensed",
            dataset_type=DatasetType.OPENLY_LICENSED,
            retrieved_at=date(2026, 1, 1),
        )


def test_source_id_must_be_slug() -> None:
    with pytest.raises(ValidationError):
        DatasetSource(
            source_id="Has Spaces",
            name="x",
            dataset_type=DatasetType.UNKNOWN,
            retrieved_at=date(2026, 1, 1),
        )


def test_page_record_char_count_and_emptiness() -> None:
    page = PageRecord(page_number=1, text="  hello  ")
    assert page.char_count == 9
    assert page.is_empty is False
    assert PageRecord(page_number=2, text="   ").is_empty is True
    with pytest.raises(ValidationError):
        PageRecord(page_number=0, text="x")


def test_question_defaults_do_not_invent_marks_or_answers() -> None:
    question = QuestionRecord(
        question_id="q_1",
        document_id="doc_1",
        course_id="course_1",
        text="Define a stack.",
    )
    assert question.marks is None
    assert question.marks_source is None
    assert question.fingerprint
    assert "answer" not in question.metadata
    assert question.needs_review is False


def test_question_rejects_out_of_range_confidence_and_marks() -> None:
    with pytest.raises(ValidationError):
        QuestionRecord(
            question_id="q",
            document_id="d",
            course_id="c",
            text="x",
            confidence=1.5,
        )
    with pytest.raises(ValidationError):
        QuestionRecord(question_id="q", document_id="d", course_id="c", text="x", marks=-1)


def test_uncertainty_is_structured() -> None:
    uncertainty = Uncertainty(code="numbering_gap", message="jump from 2 to 4")
    assert uncertainty.severity.value == "warning"


def test_document_id_is_deterministic_and_course_scoped() -> None:
    first = make_document_id("course-a", "a" * 64)
    assert first == make_document_id("course-a", "a" * 64)
    assert first != make_document_id("course-b", "a" * 64)
    assert first != make_document_id("course-a", "b" * 64)


def test_question_id_is_deterministic() -> None:
    assert make_question_id("doc", "1", "Hello?") == make_question_id("doc", "1", "Hello?")
    assert make_question_id("doc", "1", "Hello?") != make_question_id("doc", "2", "Hello?")


def test_document_rejects_bad_hash() -> None:
    with pytest.raises(ValidationError):
        DocumentRecord(
            document_id="doc",
            course_id="course",
            source_id="source",
            kind="notes",
            format=SourceFormat.TXT,
            title="t",
            original_filename="t.txt",
            content_sha256="not-a-hash",
            byte_size=1,
        )


def test_result_ok_semantics() -> None:
    ok = IngestionResult(source_path="a.txt", course_id="c", status=IngestionStatus.INGESTED)
    failed = IngestionResult(source_path="b.txt", course_id="c", status=IngestionStatus.FAILED)
    assert ok.ok is True
    assert failed.ok is False
    assert failed.error_codes() == []

    batch = BatchIngestionResult(run_id="run_1", results=[ok, failed])
    assert batch.totals["documents"] == 2
    assert batch.totals["failed"] == 1
    assert batch.ok is False


def test_batch_totals_group_by_course() -> None:
    results = [
        IngestionResult(
            source_path="a", course_id="c1", status=IngestionStatus.INGESTED, question_count=3
        ),
        IngestionResult(source_path="b", course_id="c2", status=IngestionStatus.SKIPPED_DUPLICATE),
    ]
    batch = BatchIngestionResult(run_id="r", results=results)
    assert batch.totals["questions"] == 3
    assert batch.totals["skipped_duplicate"] == 1
    assert sorted(batch.by_course()) == ["c1", "c2"]


def test_document_and_page_models_roundtrip_values() -> None:
    page = PageRecord(page_number=3, text="Unit 1: data", method=ExtractionMethod.OCR)
    document = DocumentRecord(
        document_id="doc_x",
        course_id="course_x",
        source_id="source_x",
        kind="question_paper",
        format=SourceFormat.PDF,
        title="Paper",
        original_filename="paper.pdf",
        content_sha256="0" * 64,
        byte_size=10,
        page_count=3,
        char_count=page.char_count,
        extraction_status=ExtractionStatus.OK,
    )
    assert document.page_count == 3
    assert page.method is ExtractionMethod.OCR


def test_ingestion_error_keeps_severity() -> None:
    error = IngestionError(code="parse_error", message="boom")
    assert error.is_error is True
