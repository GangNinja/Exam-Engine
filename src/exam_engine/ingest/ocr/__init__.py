"""OCR provider registry and page rendering."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from exam_engine.ingest.ocr.base import (
    NullOCRProvider,
    OCRFailedError,
    OCRProvider,
    OCRUnavailableError,
)
from exam_engine.ingest.ocr.tesseract import TesseractOCRProvider

_PROVIDERS: dict[str, Callable[[], OCRProvider]] = {
    "none": NullOCRProvider,
    "tesseract": TesseractOCRProvider,
}
_CACHE: dict[str, OCRProvider] = {}


def register_ocr_provider(name: str, factory: Callable[[], OCRProvider]) -> None:
    """Register a provider so deployments and tests can swap the OCR engine."""
    _PROVIDERS[name.lower()] = factory
    _CACHE.pop(name.lower(), None)


def get_ocr_provider(name: str | None = None) -> OCRProvider:
    key = (name or "none").lower()
    if key not in _PROVIDERS:
        raise ValueError(
            f"unknown OCR provider '{name}'; available: {', '.join(sorted(_PROVIDERS))}"
        )
    if key not in _CACHE:
        _CACHE[key] = _PROVIDERS[key]()
    return _CACHE[key]


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def render_pdf_page_png(path: Path, page_number: int, dpi: int = 200) -> bytes:
    """Render one PDF page to PNG bytes for an OCR provider.

    Rendering is isolated here so a different rasteriser can replace PyMuPDF.
    """
    import fitz  # PyMuPDF

    try:
        document = fitz.open(str(path))
    except Exception as exc:  # noqa: BLE001 - fitz raises its own error types
        raise OCRFailedError(f"could not open PDF for OCR: {exc}") from exc
    try:
        if page_number < 1 or page_number > document.page_count:
            raise OCRFailedError(
                f"page {page_number} is outside the PDF's {document.page_count} pages"
            )
        page = document.load_page(page_number - 1)
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return bytes(pixmap.tobytes("png"))
    finally:
        document.close()


__all__ = [
    "NullOCRProvider",
    "OCRFailedError",
    "OCRProvider",
    "OCRUnavailableError",
    "TesseractOCRProvider",
    "available_providers",
    "get_ocr_provider",
    "register_ocr_provider",
    "render_pdf_page_png",
]
