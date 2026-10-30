from __future__ import annotations

from exam_engine.ingest.base import ExtractedPage
from exam_engine.ingest.segmentation import (
    extract_marks,
    segment_questions_pages,
    segment_questions_text,
)
from exam_engine.models.enums import SegmentationStatus

PAPER = """CS101 Midterm
SECTION A - Short Answer

1. Define a stack and list two operations. [5 marks]

2. Explain the difference between an array and a linked list. [5 marks]

3. State the complexity of binary search. (4 marks)

SECTION B - Long Answer

1. Derive the recurrence for merge sort. [10 marks]

2. Write pseudocode for cycle detection. [10 marks]
"""


def test_basic_segmentation_numbers_text_and_marks() -> None:
    result = segment_questions_text(PAPER)
    assert len(result.questions) == 5
    assert [q.question_number for q in result.questions] == ["1", "2", "3", "1", "2"]
    assert result.questions[0].marks == 5
    assert result.questions[2].marks == 4
    assert "Define a stack" in result.questions[0].text
    assert result.questions[3].section.startswith("SECTION B")
    assert result.status is SegmentationStatus.SEGMENTED
    assert result.confidence > 0.6


def test_numbering_gap_is_reported_and_not_filled() -> None:
    paper = "1. First question here.\n\n2. Second question here.\n\n4. Fourth question here.\n"
    result = segment_questions_text(paper)
    assert [q.question_number for q in result.questions] == ["1", "2", "4"]
    assert result.numbering_gaps == [(2, 4)]
    gap_question = result.questions[2]
    assert any(u.code == "numbering_gap" for u in gap_question.uncertainties)
    assert result.status is SegmentationStatus.PARTIAL


def test_duplicate_numbering_is_rejected_not_duplicated() -> None:
    paper = "1. First question here.\n\n1. Repeated number here.\n\n2. Third question here.\n"
    result = segment_questions_text(paper)
    assert [q.question_number for q in result.questions] == ["1", "2"]
    assert result.rejected_markers == 1
    assert any(u.code == "numbering_conflict" for u in result.uncertainties)


def test_unsegmented_document_returns_no_questions() -> None:
    result = segment_questions_text("A free-form paragraph with no numbered questions.\n")
    assert result.questions == []
    assert result.status is SegmentationStatus.UNSEGMENTED
    assert result.confidence == 0.0
    assert any(u.code == "no_questions_detected" for u in result.uncertainties)


def test_decimal_numbers_are_not_questions() -> None:
    paper = (
        "1. Compute the following value.\n\nThe value is 3.14 approximately.\n\n2. Explain pi.\n"
    )
    result = segment_questions_text(paper)
    assert [q.question_number for q in result.questions] == ["1", "2"]
    assert "3.14" in result.questions[0].text


def test_instructions_are_flagged_not_silently_dropped() -> None:
    paper = (
        "General Instructions: Answer all questions.\n\n"
        "1. Attempt any four of the following. [20 marks]\n\n"
        "2. Define a graph. [5 marks]\n"
    )
    result = segment_questions_text(paper)
    assert len(result.questions) == 2
    flagged = [
        q
        for q in result.questions
        if any(u.code == "possible_instruction" for u in q.uncertainties)
    ]
    assert len(flagged) == 1
    assert flagged[0].confidence < 0.8


def test_question_spanning_pages_is_tracked() -> None:
    pages = [
        ExtractedPage(page_number=1, text="1. A question that starts on page one and"),
        ExtractedPage(page_number=2, text="continues onto page two without a marker."),
    ]
    result = segment_questions_pages(pages)
    assert len(result.questions) == 1
    question = result.questions[0]
    assert "continues onto page two" in question.text
    assert question.page_number == 1
    assert question.metadata["page_end"] == 2
    assert "pages 1-2" in (question.source_locator or "")
    assert any(u.code == "spans_pages" for u in question.uncertainties)


def test_locator_records_page_and_line_range() -> None:
    result = segment_questions_text("Title\n\n1. First question.\n\n2. Second question.")
    assert result.questions[0].page_number == 1
    assert "page 1" in (result.questions[0].source_locator or "")
    assert "lines" in (result.questions[0].source_locator or "")


def test_marks_extraction_variants() -> None:
    assert extract_marks("Define a stack. [5 marks]")[:2] == (5.0, "explicit")
    assert extract_marks("Define a stack. (4 Marks)")[:2] == (4.0, "explicit")
    assert extract_marks("Define a stack. 7 marks")[:2] == (7.0, "explicit")
    assert extract_marks("Marks: 3 for this part")[:2] == (3.0, "labelled")
    value, source, uncertainties, penalty = extract_marks("Explain it. [6]")
    assert (value, source) == (6.0, "bare_bracket")
    assert any(u.code == "marks_bare_bracket" for u in uncertainties)
    assert penalty > 0
    assert extract_marks("No marks stated here.")[0] is None


def test_multiple_marks_values_prefer_last_and_warn() -> None:
    value, _source, uncertainties, penalty = extract_marks("Part a (2 marks) and part b (3 marks)")
    assert value == 3.0
    assert any(u.code == "multiple_marks_values" for u in uncertainties)
    assert penalty > 0


def test_short_and_empty_bodies_are_handled() -> None:
    result = segment_questions_text("1. Ok\n\n2. \n\n3. A real question with enough words.\n")
    numbers = [q.question_number for q in result.questions]
    assert "2" not in numbers  # a bare marker is not a question
    assert "3" in numbers
    assert any(u.code == "short_question_text" for q in result.questions for u in q.uncertainties)
    assert any(u.code == "empty_question_body" for u in result.uncertainties)


def test_question_limit_is_enforced() -> None:
    paper = "\n\n".join(f"{i}. Question number {i} about data structures." for i in range(1, 11))
    result = segment_questions_text(paper, max_questions=4)
    assert len(result.questions) == 4
    assert any(u.code == "question_limit_exceeded" for u in result.uncertainties)


def test_numbered_instructions_do_not_consume_question_numbers() -> None:
    paper = (
        "1. Answer all questions in the answer booklet.\n"
        "2. Time allowed: 90 minutes.\n"
        "3. All questions are compulsory.\n\n"
        "1. Define a stack. [5 marks]\n\n"
        "2. Define a queue. [5 marks]\n"
    )
    result = segment_questions_text(paper)
    assert [q.question_number for q in result.questions] == ["1", "2"]
    assert "Define a stack" in result.questions[0].text
    assert "Answer all questions" not in result.questions[0].text
    dropped = [u for u in result.uncertainties if u.code == "instruction_markers_dropped"]
    assert len(dropped) == 1
    assert len(dropped[0].detail["markers"]) == 3
    # Informational notes do not downgrade a cleanly segmented paper.
    assert result.status is SegmentationStatus.SEGMENTED


def test_instruction_repair_does_not_hide_real_duplicate_numbers() -> None:
    paper = "1. Define a stack. [5 marks]\n\n1. Define a stack again. [5 marks]\n"
    result = segment_questions_text(paper)
    assert [q.question_number for q in result.questions] == ["1"]
    assert any(u.code == "numbering_conflict" for u in result.uncertainties)


def test_preamble_instructions_before_a_section_are_not_questions() -> None:
    # Real papers put numbered instructions under a heading, then restart the
    # numbering inside each section. The instructions must not become questions.
    paper = (
        "INSTRUCTIONS TO CANDIDATES\n"
        "1. Answer all questions.\n"
        "2. Write your roll number on every page.\n"
        "3. Do not use calculators.\n\n"
        "SECTION A\n\n"
        "1. Define a stack and give one application. [5 marks]\n"
        "2. Compare arrays and linked lists. [5 marks]\n\n"
        "SECTION B\n\n"
        "1. Describe quicksort's partition step. [8 marks]\n"
    )
    result = segment_questions_text(paper)
    assert len(result.questions) == 3
    assert [q.question_number for q in result.questions] == ["1", "2", "1"]
    assert all("Answer all questions" not in q.text for q in result.questions)
    assert all("roll number" not in q.text for q in result.questions)
    dropped = [u for u in result.uncertainties if u.code == "instruction_markers_dropped"]
    assert dropped and len(dropped[0].detail["markers"]) == 3
    assert result.status is SegmentationStatus.SEGMENTED


def test_preamble_line_with_marks_is_kept_as_a_question() -> None:
    paper = "1. Answer all questions in brief. [2 marks]\n\n2. Define a queue. [5 marks]\n"
    result = segment_questions_text(paper)
    assert [q.question_number for q in result.questions] == ["1", "2"]


def test_reference_question_papers_are_not_mistaken_for_questions() -> None:
    paper = (
        "Answer any five questions.\n"
        "All questions carry equal marks.\n\n"
        "1. Define a stack. [5 marks]\n\n"
        "2. Define a queue. [5 marks]\n"
    )
    result = segment_questions_text(paper)
    assert len(result.questions) == 2
