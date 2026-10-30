"""Optional Tesseract OCR provider.

Requires the optional extras (``pip install -e .[ocr]``) plus a system Tesseract
binary. ``available()`` never raises, so callers can fall back cleanly.
"""

from __future__ import annotations

import io

from exam_engine.ingest.ocr.base import OCRFailedError, OCRUnavailableError


class TesseractOCRProvider:
    name = "tesseract"

    def __init__(self, language: str = "eng") -> None:
        self.language = language

    def available(self) -> bool:
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
        except Exception:  # noqa: BLE001 - binary may be missing or misconfigured
            return False
        return True

    def extract_text(self, image_bytes: bytes) -> str:
        if not self.available():
            raise OCRUnavailableError(
                "tesseract OCR provider is not usable (missing pytesseract, Pillow, "
                "or the tesseract binary)"
            )
        import pytesseract
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(image_bytes))
        except Exception as exc:  # noqa: BLE001 - PIL raises several types
            raise OCRFailedError(f"could not decode page image: {exc}") from exc
        try:
            return str(pytesseract.image_to_string(image, lang=self.language))
        except Exception as exc:  # noqa: BLE001 - pytesseract wraps subprocess errors
            raise OCRFailedError(f"tesseract failed on page image: {exc}") from exc
