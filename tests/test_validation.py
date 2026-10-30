from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from exam_engine.config import IngestionLimits
from exam_engine.ingest.validation import (
    FileValidationError,
    resolve_source_path,
    sniff_format,
    validate_source_file,
)
from exam_engine.models.enums import SourceFormat
from tests.support import write_docx, write_pdf


def _codes(exc_info) -> str:
    return exc_info.value.code


def test_unsupported_suffix_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "notes.rtf"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(path)
    assert _codes(exc_info) == "unsupported_format"


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(path)
    assert _codes(exc_info) == "empty_file"


def test_size_limit_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text("x" * 200, encoding="utf-8")
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(path, IngestionLimits(max_file_bytes=50))
    assert _codes(exc_info) == "file_too_large"


def test_pdf_suffix_with_wrong_bytes_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"this is not a pdf")
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(path)
    assert _codes(exc_info) == "format_mismatch"


def test_fake_docx_zip_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "fake.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "not a docx")
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(path)
    assert _codes(exc_info) == "format_mismatch"


def test_binary_bytes_in_text_file_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "binary.txt"
    path.write_bytes(b"text\x00\x00more")
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(path)
    assert _codes(exc_info) == "binary_content"


def test_missing_and_directory_paths(tmp_path: Path) -> None:
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(tmp_path / "absent.txt")
    assert _codes(exc_info) == "file_not_found"
    with pytest.raises(FileValidationError) as exc_info:
        validate_source_file(tmp_path)
    assert _codes(exc_info) == "not_a_file"


def test_resolve_source_path_blocks_traversal(tmp_path: Path) -> None:
    base = tmp_path / "dataset"
    base.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(FileValidationError) as exc_info:
        resolve_source_path("../secret.txt", base)
    assert _codes(exc_info) == "path_outside_root"

    with pytest.raises(FileValidationError):
        resolve_source_path(str(outside), base)

    assert resolve_source_path(str(outside), base, allow_external=True) == outside.resolve()


def test_valid_files_pass_and_report_format(tmp_path: Path) -> None:
    pdf = write_pdf(tmp_path / "paper.pdf", [["Question 1"]])
    facts = validate_source_file(pdf)
    assert facts.format is SourceFormat.PDF

    docx = write_docx(tmp_path / "syllabus.docx", ["Unit 1: Data"])
    assert validate_source_file(docx).format is SourceFormat.DOCX

    text = tmp_path / "notes.txt"
    text.write_text("Notes", encoding="utf-8")
    assert validate_source_file(text).format is SourceFormat.TXT


def test_sniff_format_detects_real_container(tmp_path: Path) -> None:
    pdf = write_pdf(tmp_path / "x.pdf", [["hi"]])
    assert sniff_format(pdf) is SourceFormat.PDF
    docx = write_docx(tmp_path / "x.docx", ["hi"])
    assert sniff_format(docx) is SourceFormat.DOCX
    text = tmp_path / "x.txt"
    text.write_text("hi", encoding="utf-8")
    assert sniff_format(text) is None
