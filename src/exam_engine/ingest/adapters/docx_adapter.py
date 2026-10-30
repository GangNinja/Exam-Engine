"""DOCX extraction that preserves paragraph/table order.

DOCX has no fixed page geometry, so a document is emitted as one logical page and
question locators fall back to line ranges within that page.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from exam_engine.ingest.base import (
    ExtractedDocument,
    ExtractedPage,
    ExtractionContext,
    ingestion_warning,
)
from exam_engine.ingest.normalize import normalize_text
from exam_engine.ingest.validation import FileValidationError
from exam_engine.models.enums import ExtractionMethod, Severity, SourceFormat


class DocxAdapter:
    name = "docx"
    suffixes: tuple[str, ...] = (".docx",)

    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument:
        try:
            import docx
            from docx.document import Document as DocxDocument
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise FileValidationError("docx_invalid", "python-docx is not installed") from exc

        try:
            document: DocxDocument = docx.Document(str(path))
        except Exception as exc:  # noqa: BLE001 - python-docx raises varied errors
            raise FileValidationError(
                "docx_invalid",
                f"could not open DOCX '{path.name}': {exc}",
                {"error": str(exc)},
            ) from exc

        blocks = list(_iter_blocks(document))
        lines: list[str] = []
        tables = 0
        paragraphs = 0
        for kind, block in blocks:
            if kind == "paragraph":
                paragraphs += 1
                text = normalize_text(block.text)
                if text:
                    lines.append(text)
            else:
                tables += 1
                lines.extend(_table_lines(block))

        page_text = normalize_text("\n".join(lines))
        page = ExtractedPage(
            page_number=1,
            text=page_text,
            method=ExtractionMethod.EMBEDDED_TEXT if page_text else ExtractionMethod.NONE,
            has_images=_has_images(document),
        )

        needs_ocr: list[int] = []
        warnings = []
        if not page_text.strip():
            needs_ocr.append(1)
            warnings.append(
                ingestion_warning(
                    "empty_document",
                    "DOCX contains no extractable paragraph or table text",
                    severity=Severity.WARNING,
                )
            )

        metadata: dict[str, Any] = {
            "extractor": "python-docx",
            "docx_paragraphs": paragraphs,
            "docx_tables": tables,
            "docx_blocks": len(blocks),
            "pages_unavailable": True,
        }
        core = getattr(document, "core_properties", None)
        if core is not None and getattr(core, "title", None):
            metadata["docx_title"] = str(core.title)

        return ExtractedDocument(
            format=SourceFormat.DOCX,
            pages=[page],
            metadata=metadata,
            warnings=warnings,
            needs_ocr_pages=needs_ocr,
            table_count=tables,
            title_hint=_title_hint(document, page_text),
        )


def _has_images(document) -> bool:
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    body = document.element.body
    return bool(body.findall(f".//{w}drawing")) or bool(body.findall(f".//{a}blip"))


def _iter_blocks(document) -> list[tuple[str, Any]]:
    """Walk the body in document order (paragraphs and tables interleaved)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    blocks: list[tuple[str, Any]] = []
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            blocks.append(("paragraph", Paragraph(child, document)))
        elif tag == "tbl":
            blocks.append(("table", Table(child, document)))
    return blocks


def _table_lines(table) -> list[str]:
    lines: list[str] = []
    for row in table.rows:
        cells = [normalize_text(cell.text).replace("\n", " ").strip() for cell in row.cells]
        # Drop trailing empties caused by merged cells.
        while cells and not cells[-1]:
            cells.pop()
        if cells and any(cells):
            lines.append(" | ".join(cells))
    return lines


def _title_hint(document, page_text: str) -> str | None:
    for paragraph in document.paragraphs[:20]:
        style = (paragraph.style.name or "").lower() if paragraph.style else ""
        text = paragraph.text.strip()
        if text and ("heading" in style or "title" in style) and len(text) <= 160:
            return text
    for line in page_text.split("\n"):
        stripped = line.strip()
        if 3 <= len(stripped) <= 120:
            return stripped
    return None
