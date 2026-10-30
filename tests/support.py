"""Shared test builders. Not a test module (no test_ prefix)."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from exam_engine.models.course import CourseRecord
from exam_engine.models.enums import DatasetType
from exam_engine.models.provenance import DatasetSource


def make_course(
    course_id: str = "test-cs101-2024-v1",
    *,
    course_code: str = "CS101",
    title: str = "Introduction to Computing",
    institution_code: str = "test-inst",
    academic_year: str = "2024-25",
    syllabus_version: str = "v1",
) -> CourseRecord:
    return CourseRecord(
        course_id=course_id,
        course_code=course_code,
        title=title,
        institution_code=institution_code,
        institution_name="Test Institute",
        department="Computing",
        academic_year=academic_year,
        syllabus_version=syllabus_version,
    )


def make_source(
    source_id: str = "test-synthetic",
    *,
    dataset_type: DatasetType = DatasetType.SYNTHETIC,
    license: str | None = "CC0-1.0",
) -> DatasetSource:
    return DatasetSource(
        source_id=source_id,
        name="Test synthetic dataset",
        dataset_type=dataset_type,
        retrieved_at=date(2026, 1, 1),
        license=license,
        usage_terms=None if license else "Generated for testing only.",
        publisher="test-suite",
        notes="Synthetic fixture data.",
    )


def write_pdf(path: Path, pages: list[list[str]]) -> Path:
    """Write a text-layer PDF with one list of lines per page."""
    import fitz

    document = fitz.open()
    for lines in pages:
        page = document.new_page(width=595, height=842)
        y = 60.0
        for line in lines:
            page.insert_text((50, y), line, fontsize=11, fontname="helv")
            y += 16.0
    document.save(str(path))
    document.close()
    return path


def write_scanned_pdf(path: Path) -> Path:
    """Write a PDF whose only page content is a raster image (no text layer)."""
    import fitz
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    for row in range(10):
        draw.rectangle((80, 120 + row * 90, 1150, 150 + row * 90), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buffer.getvalue())
    document.save(str(path))
    document.close()
    return path


def write_docx(
    path: Path,
    paragraphs: list[str],
    tables: list[list[list[str]]] | None = None,
) -> Path:
    """Write a DOCX with paragraphs and optional tables appended after them."""
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    for rows in tables or []:
        table = document.add_table(rows=0, cols=len(rows[0]))
        table.style = "Table Grid"
        for row in rows:
            cells = table.add_row().cells
            for index, value in enumerate(row):
                cells[index].text = value
    document.save(str(path))
    return path


class StubOCRProvider:
    """Deterministic OCR provider used to prove the interface is replaceable."""

    name = "stub"

    def __init__(self, text: str = "1. Explain the OCR pipeline. [5 marks]") -> None:
        self.text = text
        self.calls: list[int] = []

    def available(self) -> bool:
        return True

    def extract_text(self, image_bytes: bytes) -> str:
        self.calls.append(len(image_bytes))
        return self.text


class BrokenOCRProvider:
    name = "broken"

    def available(self) -> bool:
        return True

    def extract_text(self, image_bytes: bytes) -> str:
        raise RuntimeError("simulated OCR engine crash")
