from __future__ import annotations

from exam_engine.ingest.base import ExtractedPage
from exam_engine.ingest.quality import assess_pages, mark_question_duplicates
from exam_engine.models.question import QuestionRecord


def page(number: int, text: str, has_images: bool = False) -> ExtractedPage:
    return ExtractedPage(page_number=number, text=text, has_images=has_images)


def question(question_id: str, text: str) -> QuestionRecord:
    return QuestionRecord(
        question_id=question_id,
        document_id="doc",
        course_id="course",
        text=text,
        confidence=0.9,
    )


def test_empty_document_scores_zero() -> None:
    report = assess_pages([])
    assert report.score == 0.0
    assert "empty_document" in report.flags

    blank = assess_pages([page(1, "   ")])
    assert blank.score == 0.0
    assert blank.is_usable is False


def test_healthy_document_scores_high() -> None:
    text = "1. Define a stack and its operations in detail. [5 marks] " * 4
    report = assess_pages([page(1, text), page(2, text)])
    assert report.score > 0.9
    assert report.flags == []
    assert report.nonempty_pages == 2


def test_scanned_pages_and_low_yield_are_flagged() -> None:
    report = assess_pages(
        [page(1, "", has_images=True), page(2, "", has_images=True), page(3, "Short text")]
    )
    assert "scanned_pages" in report.flags
    assert "low_text_yield" in report.flags
    assert report.scanned_pages == [1, 2]
    assert report.score < 0.7


def test_replacement_characters_and_control_bytes_penalised() -> None:
    degraded = "A" * 400 + "\ufffd" * 20 + "\x01\x02" * 20
    report = assess_pages([page(1, degraded)])
    assert "replacement_characters" in report.flags
    assert "control_characters" in report.flags
    assert report.score < 1.0


def test_mark_question_duplicates_marks_known_and_batch_repeats() -> None:
    first = question("q1", "Define a stack.")
    second = question("q2", "define  a STACK")
    known: dict[str, str] = {"existingfingerprint": "q_old"}

    duplicates = mark_question_duplicates([first, second], known)
    assert duplicates == 1
    assert second.duplicate_of == "q1"
    assert any(u.code == "duplicate_question" for u in second.uncertainties)
    assert known[first.fingerprint] == "q1"


def test_mark_question_duplicates_detects_stored_question() -> None:
    stored = question("q_stored", "Explain binary search.")
    incoming = question("q_new", "Explain binary search!")
    duplicates = mark_question_duplicates([incoming], {stored.fingerprint: stored.question_id})
    assert duplicates == 1
    assert incoming.duplicate_of == "q_stored"
