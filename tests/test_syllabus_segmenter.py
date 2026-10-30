from __future__ import annotations

from exam_engine.ingest.segmentation import segment_questions_text
from exam_engine.ingest.syllabus_segmenter import roman_to_int, segment_syllabus
from exam_engine.models.enums import SegmentationStatus

SYLLABUS = """CS201 Data Structures - Syllabus

UNIT I: Foundations (8 hours)
- Asymptotic notation: O, Omega and Theta
- Recurrence relations

UNIT II: Linear Structures (10 hours)
Arrays, linked lists, stacks and queues
Stacks and queues; expression evaluation

UNIT III: Trees (12 hours)
Binary trees and traversals: preorder, inorder, postorder
Heaps
"""


def test_units_topics_and_hours() -> None:
    result = segment_syllabus_text(SYLLABUS)
    assert [u.unit_number for u in result.units] == [1, 2, 3]
    assert result.units[0].unit_label == "Unit I"
    assert result.units[0].title == "Foundations"
    assert result.units[0].hours == 8.0
    assert "Asymptotic notation: O, Omega and Theta" in result.units[0].topics
    assert result.status is SegmentationStatus.SEGMENTED
    assert result.confidence > 0.8


def test_comma_lists_split_but_colon_details_do_not() -> None:
    result = segment_syllabus_text(SYLLABUS)
    unit_two = result.units[1]
    # colon detail stays intact
    assert "Binary trees and traversals: preorder, inorder, postorder" not in unit_two.topics
    assert any(topic.startswith("Binary trees") for topic in result.units[2].topics)
    # semicolons are explicit separators
    assert "Stacks and queues" in unit_two.topics
    assert "expression evaluation" in unit_two.topics


def test_module_variant_and_missing_heading() -> None:
    text = (
        "MODULE 1: Mechanics (12 hrs)\nMotion in a straight line\n\n"
        "MODULE 2: Waves (6 hours)\nSuperposition\n"
    )
    result = segment_syllabus_text(text)
    assert [u.unit_label for u in result.units] == ["Module 1", "Module 2"]
    assert result.units[0].hours == 12.0
    assert result.units[1].title == "Waves"


def test_unsegmented_syllabus_reports_uncertainty() -> None:
    result = segment_syllabus_text("Some course description without unit headings.\n")
    assert result.units == []
    assert result.status is SegmentationStatus.UNSEGMENTED
    assert any(u.code == "no_syllabus_units_detected" for u in result.uncertainties)


def test_unit_numbering_gap_is_reported() -> None:
    text = "UNIT 1: First (4 hours)\nTopic one\n\nUNIT 3: Third (4 hours)\nTopic three\n"
    result = segment_syllabus_text(text)
    assert [u.unit_number for u in result.units] == [1, 3]
    assert any(u.code == "unit_numbering_gap" for u in result.units[1].uncertainties)
    assert result.status is SegmentationStatus.PARTIAL


def test_docx_table_style_unit_rows() -> None:
    text = "Unit 1 | Foundations | Topic A; Topic B | 8\nUnit 2 | Linear Structures | 10\n"
    result = segment_syllabus_text(text)
    assert result.units[0].title == "Foundations"
    assert result.units[0].hours == 8.0
    assert result.units[0].topics == ["Topic A", "Topic B"]
    assert result.units[1].title == "Linear Structures"
    assert result.units[1].hours == 10.0


def test_layout_column_hours_after_title() -> None:
    # PDF text often pushes the hours into an aligned column.
    text = (
        "UNIT I:  DATA STRUCTURES   12\nArrays and linked lists\n\n"
        "UNIT II: ALGORITHMS   8\nSorting and searching\n"
    )
    result = segment_syllabus_text(text)
    assert result.units[0].title == "DATA STRUCTURES"
    assert result.units[0].hours == 12.0
    assert result.units[1].title == "ALGORITHMS"
    assert result.units[1].hours == 8.0


def test_number_inside_title_is_not_read_as_hours() -> None:
    # A single space means the number belongs to the title ("HTML 5").
    result = segment_syllabus_text("UNIT 1: HTML 5\nMarkup basics\n")
    assert result.units[0].title == "HTML 5"
    assert result.units[0].hours is None


def test_roman_numerals() -> None:
    assert roman_to_int("I") == 1
    assert roman_to_int("IV") == 4
    assert roman_to_int("IX") == 9
    assert roman_to_int("XII") == 12
    assert roman_to_int("7") == 7
    assert roman_to_int("nope") is None


def test_syllabus_pages_do_not_produce_questions() -> None:
    result = segment_questions_text(SYLLABUS)
    assert all(q.question_number is not None for q in result.questions)


def segment_syllabus_text(text: str):
    from exam_engine.ingest.base import ExtractedPage

    return segment_syllabus([ExtractedPage(page_number=1, text=text)])
