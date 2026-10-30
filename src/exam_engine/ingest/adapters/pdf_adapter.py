"""PDF text extraction (pdfplumber primary, PyMuPDF fallback)."""

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


class PdfAdapter:
    name = "pdf"
    suffixes: tuple[str, ...] = (".pdf",)

    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument:
        try:
            return self._extract_with_pdfplumber(path, context)
        except FileValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - parser backends raise many types
            if _is_encryption_error(exc):
                raise FileValidationError(
                    "pdf_encrypted",
                    f"'{path.name}' is encrypted or password protected",
                    {"error": str(exc)},
                ) from exc
            try:
                document = self._extract_with_fitz(path, context)
            except Exception as fallback_exc:  # noqa: BLE001
                raise FileValidationError(
                    "parse_error",
                    f"could not parse PDF '{path.name}': {exc}",
                    {"error": str(exc), "fallback_error": str(fallback_exc)},
                ) from exc
            document.warnings.append(
                ingestion_warning(
                    "parse_error",
                    f"pdfplumber failed ({exc}); text recovered with PyMuPDF",
                    severity=Severity.INFO,
                )
            )
            return document

    # --- backends ---------------------------------------------------------
    def _extract_with_pdfplumber(self, path: Path, context: ExtractionContext) -> ExtractedDocument:
        import pdfplumber

        pages: list[ExtractedPage] = []
        needs_ocr: list[int] = []
        metadata: dict[str, Any] = {"extractor": "pdfplumber"}

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            if page_count > context.limits.max_pages:
                raise FileValidationError(
                    "pdf_page_limit",
                    f"'{path.name}' has {page_count} pages; limit is {context.limits.max_pages}",
                    {"pages": page_count, "limit": context.limits.max_pages},
                )
            metadata["pdf_page_count"] = page_count
            raw_meta = pdf.metadata or {}
            for key in ("Producer", "Creator", "Title", "CreationDate"):
                value = raw_meta.get(key)
                if value:
                    metadata[f"pdf_{key.lower()}"] = _safe_str(value)

            for index, page in enumerate(pdf.pages, start=1):
                try:
                    raw_text = page.extract_text() or ""
                except Exception:  # noqa: BLE001 - one bad page must not fail the doc
                    raw_text = ""
                has_images = bool(getattr(page, "images", None))
                text = normalize_text(raw_text)
                method = ExtractionMethod.EMBEDDED_TEXT if text else ExtractionMethod.NONE
                pages.append(
                    ExtractedPage(
                        page_number=index,
                        text=text,
                        method=method,
                        has_images=has_images,
                    )
                )
                if len(text.strip()) < context.limits.min_page_chars and has_images:
                    needs_ocr.append(index)

        if not any(page.text.strip() for page in pages) and metadata.get("pdf_page_count", 0):
            # No embedded text at all: retry with the second backend in case the
            # PDF uses a text encoding pdfplumber cannot decode.
            fitz_doc = self._extract_with_fitz(path, context, quiet=True)
            if any(page.text.strip() for page in fitz_doc.pages):
                return fitz_doc
            fitz_doc.needs_ocr_pages = sorted(
                set(fitz_doc.needs_ocr_pages)
                | set(needs_ocr)
                | {p.page_number for p in fitz_doc.pages if not p.text.strip()}
            )
            for extracted_page in fitz_doc.pages:
                if not extracted_page.text.strip():
                    extracted_page.method = ExtractionMethod.NONE
            return fitz_doc

        return ExtractedDocument(
            format=SourceFormat.PDF,
            pages=pages,
            metadata=metadata,
            needs_ocr_pages=needs_ocr,
            title_hint=_title_hint(pages),
            warnings=[],
        )

    def _extract_with_fitz(
        self, path: Path, context: ExtractionContext, *, quiet: bool = False
    ) -> ExtractedDocument:
        import fitz

        pages: list[ExtractedPage] = []
        needs_ocr: list[int] = []
        try:
            document = fitz.open(str(path))
        except Exception as exc:  # noqa: BLE001
            if _is_encryption_error(exc):
                raise FileValidationError(
                    "pdf_encrypted",
                    f"'{path.name}' is encrypted or password protected",
                    {"error": str(exc)},
                ) from exc
            raise
        try:
            if document.page_count > context.limits.max_pages:
                raise FileValidationError(
                    "pdf_page_limit",
                    f"'{path.name}' has {document.page_count} pages; limit is "
                    f"{context.limits.max_pages}",
                    {"pages": document.page_count, "limit": context.limits.max_pages},
                )
            for index in range(document.page_count):
                page = document.load_page(index)
                try:
                    raw_text = page.get_text("text") or ""
                except Exception:  # noqa: BLE001
                    raw_text = ""
                has_images = bool(page.get_images(full=True))
                text = normalize_text(raw_text)
                pages.append(
                    ExtractedPage(
                        page_number=index + 1,
                        text=text,
                        method=(ExtractionMethod.EMBEDDED_TEXT if text else ExtractionMethod.NONE),
                        has_images=has_images,
                    )
                )
                if len(text.strip()) < context.limits.min_page_chars and has_images:
                    needs_ocr.append(index + 1)
            page_count = document.page_count
        finally:
            document.close()

        warnings = []
        if not quiet:
            warnings.append(
                ingestion_warning(
                    "parse_error",
                    "extracted with PyMuPDF fallback",
                    severity=Severity.INFO,
                )
            )
        return ExtractedDocument(
            format=SourceFormat.PDF,
            pages=pages,
            metadata={"extractor": "pymupdf", "pdf_page_count": page_count},
            needs_ocr_pages=needs_ocr,
            title_hint=_title_hint(pages),
            warnings=warnings,
        )


def _safe_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_encryption_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "encrypt" in name or "password" in name or "encrypted" in message or "password" in message
    )


def _title_hint(pages: list[ExtractedPage]) -> str | None:
    for page in pages:
        for line in page.text.split("\n"):
            stripped = line.strip()
            if 3 <= len(stripped) <= 120:
                return stripped
    return None
