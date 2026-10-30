from __future__ import annotations

import json
from pathlib import Path

import pytest

from exam_engine.config import IngestionLimits
from exam_engine.ingest.adapters import (
    DocxAdapter,
    ImageAdapter,
    PdfAdapter,
    QuestionBankAdapter,
    TextAdapter,
)
from exam_engine.ingest.base import (
    ExtractionContext,
    UnsupportedFormatError,
    adapter_for_path,
    registered_suffixes,
)
from exam_engine.ingest.validation import FileValidationError
from exam_engine.models.enums import ExtractionMethod, SourceFormat
from tests.support import write_docx, write_pdf, write_scanned_pdf


def ctx(path: Path, **limits) -> ExtractionContext:
    return ExtractionContext(path=path, limits=IngestionLimits(**limits))


# --- text -------------------------------------------------------------------


def test_text_adapter_detects_encoding_and_form_feed_pages(tmp_path: Path) -> None:
    path = tmp_path / "paper.txt"
    path.write_bytes("Question one\n\x0cQuestion two\n".encode("cp1252"))
    extracted = TextAdapter().extract(path, ctx(path))
    assert len(extracted.pages) == 2
    assert extracted.pages[0].page_number == 1
    assert extracted.pages[1].text == "Question two"
    assert extracted.pages[0].method is ExtractionMethod.PLAIN_TEXT
    assert extracted.metadata["encoding"]


def test_text_adapter_handles_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.txt"
    path.write_bytes("\ufeffUnit 1: Foundations".encode("utf-8-sig"))
    extracted = TextAdapter().extract(path, ctx(path))
    assert extracted.pages[0].text.startswith("Unit 1")
    assert "\ufeff" not in extracted.pages[0].text


def test_text_adapter_decodes_non_utf8_bytes(tmp_path: Path) -> None:
    path = tmp_path / "latin.txt"
    path.write_bytes("1. Résumé of café data.\n".encode("cp1252"))
    extracted = TextAdapter().extract(path, ctx(path))
    assert "Résumé" in extracted.pages[0].text
    assert extracted.metadata["encoding"] != "utf-8"


def test_text_adapter_flags_whitespace_only_file(tmp_path: Path) -> None:
    path = tmp_path / "blank.txt"
    path.write_text("   \n\n  ", encoding="utf-8")
    extracted = TextAdapter().extract(path, ctx(path))
    assert any(w.code == "empty_document" for w in extracted.warnings)


# --- docx -------------------------------------------------------------------


def test_docx_adapter_preserves_tables_and_order(tmp_path: Path) -> None:
    path = write_docx(
        tmp_path / "syllabus.docx",
        ["Chapter overview", "Unit 1: Foundations"],
        tables=[[["Component", "Weight"], ["Midterm", "30%"]], [["Unit", "Hours"], ["1", "8"]]],
    )
    extracted = DocxAdapter().extract(path, ctx(path))
    page = extracted.pages[0]
    assert "Unit 1: Foundations" in page.text
    assert "Component | Weight" in page.text
    assert "Midterm | 30%" in page.text
    assert extracted.table_count == 2
    assert extracted.metadata["docx_paragraphs"] == 2
    assert page.method is ExtractionMethod.EMBEDDED_TEXT


def test_docx_adapter_rejects_invalid_container(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a docx")
    with pytest.raises(FileValidationError) as exc_info:
        DocxAdapter().extract(path, ctx(path))
    assert exc_info.value.code == "docx_invalid"


# --- pdf ---------------------------------------------------------------------


def test_pdf_adapter_extracts_pages_and_title(tmp_path: Path) -> None:
    path = write_pdf(
        tmp_path / "paper.pdf",
        [["CS101 Midterm", "1. Define a stack. [5 marks]"], ["2. Explain a queue. [5 marks]"]],
    )
    extracted = PdfAdapter().extract(path, ctx(path))
    assert extracted.format is SourceFormat.PDF
    assert len(extracted.pages) == 2
    assert "Define a stack" in extracted.pages[0].text
    assert "Explain a queue" in extracted.pages[1].text
    assert extracted.title_hint == "CS101 Midterm"
    assert extracted.needs_ocr_pages == []
    assert extracted.pages[0].page_number == 1


def test_pdf_adapter_marks_scanned_pages_for_ocr(tmp_path: Path) -> None:
    path = write_scanned_pdf(tmp_path / "scan.pdf")
    extracted = PdfAdapter().extract(path, ctx(path))
    assert extracted.needs_ocr_pages == [1]
    assert extracted.pages[0].text == ""
    assert extracted.pages[0].has_images is True


def test_pdf_page_limit_is_enforced(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "many.pdf", [["page 1"], ["page 2"], ["page 3"]])
    with pytest.raises(FileValidationError) as exc_info:
        PdfAdapter().extract(path, ctx(path, max_pages=2))
    assert exc_info.value.code == "pdf_page_limit"


# --- question banks ----------------------------------------------------------


def test_csv_bank_aliases_missing_marks_and_locators(tmp_path: Path) -> None:
    path = tmp_path / "bank.csv"
    path.write_text(
        "Q.No,Question,Marks,Section,Topic\n"
        "1,Define a stack,5,A,Stacks\n"
        "2,Explain a queue,,A,Queues\n"
        "3,,[5],A,Arrays\n",
        encoding="utf-8",
    )
    extracted = QuestionBankAdapter().extract(path, ctx(path))
    assert len(extracted.questions) == 2  # the row without text is skipped
    first, second = extracted.questions
    assert first.question_number == "1"
    assert first.marks == 5
    assert first.marks_source == "structured"
    assert first.source_locator == "row 2"
    assert "tags" not in first.metadata
    assert second.marks is None
    assert any(u.code == "no_marks_detected" for u in second.uncertainties)
    assert any(w.code == "missing_manifest_field" for w in extracted.warnings)
    assert second.question_number == "2"


def test_csv_bank_invalid_marks_are_flagged(tmp_path: Path) -> None:
    path = tmp_path / "bank.csv"
    path.write_text("number,text,marks\n1,Define a stack,abc\n", encoding="utf-8")
    extracted = QuestionBankAdapter().extract(path, ctx(path))
    question = extracted.questions[0]
    assert question.marks is None
    assert any(u.code == "invalid_marks_value" for u in question.uncertainties)
    assert question.confidence < 0.95


def test_csv_bank_duplicate_numbers_are_flagged(tmp_path: Path) -> None:
    path = tmp_path / "bank.csv"
    path.write_text("number,text\n1,First question\n1,Second question\n", encoding="utf-8")
    extracted = QuestionBankAdapter().extract(path, ctx(path))
    assert len(extracted.questions) == 2
    assert any(u.code == "duplicate_question_number" for u in extracted.questions[1].uncertainties)


def test_json_bank_sections_and_list_forms(tmp_path: Path) -> None:
    sections = tmp_path / "sections.json"
    sections.write_text(
        json.dumps(
            {
                "sections": [
                    {"name": "PART A", "questions": [{"number": 1, "text": "First?", "marks": 2}]},
                    {"name": "PART B", "questions": [{"number": "2a", "text": "Second?"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    extracted = QuestionBankAdapter().extract(sections, ctx(sections))
    assert [q.question_number for q in extracted.questions] == ["1", "2a"]
    assert extracted.questions[0].section == "PART A"
    assert extracted.questions[1].section == "PART B"
    assert extracted.questions[1].marks is None

    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps([{"text": "Only text", "marks": "3"}]), encoding="utf-8")
    extracted = QuestionBankAdapter().extract(plain, ctx(plain))
    assert extracted.questions[0].marks == 3.0
    assert any(u.code == "missing_question_number" for u in extracted.questions[0].uncertainties)


def test_json_bank_non_finite_marks_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "nan.json"
    path.write_text(
        '{"questions": [{"number": 1, "text": "Define a stack.", "marks": NaN}, '
        '{"number": 2, "text": "Define a queue.", "marks": Infinity}]}',
        encoding="utf-8",
    )
    extracted = QuestionBankAdapter().extract(path, ctx(path))
    assert [q.marks for q in extracted.questions] == [None, None]
    for question in extracted.questions:
        assert any(u.code == "invalid_marks_value" for u in question.uncertainties)


def test_bank_unit_is_a_first_class_field(tmp_path: Path) -> None:
    path = tmp_path / "units.csv"
    path.write_text("number,text,unit,section\n1,Define a stack,Unit 2,A\n", encoding="utf-8")
    extracted = QuestionBankAdapter().extract(path, ctx(path))
    question = extracted.questions[0]
    assert question.unit == "Unit 2"
    assert "unit" not in question.metadata


def test_json_bank_single_object_and_invalid_payload(tmp_path: Path) -> None:
    single = tmp_path / "single.json"
    single.write_text(json.dumps({"text": "What is a tree?", "marks": 4}), encoding="utf-8")
    assert len(QuestionBankAdapter().extract(single, ctx(single)).questions) == 1

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(FileValidationError) as exc_info:
        QuestionBankAdapter().extract(broken, ctx(broken))
    assert exc_info.value.code == "json_invalid"

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(FileValidationError) as exc_info:
        QuestionBankAdapter().extract(empty, ctx(empty))
    assert exc_info.value.code == "empty_document"


def test_csv_without_text_column_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("alpha,beta\n1,2\n", encoding="utf-8")
    with pytest.raises(FileValidationError) as exc_info:
        QuestionBankAdapter().extract(path, ctx(path))
    assert exc_info.value.code == "parse_error"


# --- image -------------------------------------------------------------------


def test_image_adapter_defers_to_ocr(tmp_path: Path) -> None:
    path = tmp_path / "page.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")
    extracted = ImageAdapter().extract(path, ctx(path))
    assert extracted.needs_ocr_pages == [1]
    assert extracted.pages[0].has_images is True
    assert extracted.pages[0].text == ""


# --- registry ----------------------------------------------------------------


def test_registry_resolves_supported_suffixes(tmp_path: Path) -> None:
    assert ".pdf" in registered_suffixes()
    assert ".docx" in registered_suffixes()
    assert ".csv" in registered_suffixes()
    assert isinstance(adapter_for_path(tmp_path / "a.PDF"), PdfAdapter)
    with pytest.raises(UnsupportedFormatError):
        adapter_for_path(tmp_path / "a.xls")
