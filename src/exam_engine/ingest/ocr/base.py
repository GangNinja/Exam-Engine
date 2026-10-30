"""Replaceable OCR interface.

The default provider is deliberately unavailable: a document that needs OCR is
reported as ``needs_ocr`` rather than silently stored as empty text. Operators
opt in to a real engine (or a stub in tests) via configuration.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class OCRUnavailableError(RuntimeError):
    """Raised when OCR is required but no provider can run."""


class OCRFailedError(RuntimeError):
    """Raised when a configured provider fails on a page."""


@runtime_checkable
class OCRProvider(Protocol):
    name: str

    def available(self) -> bool:
        """Whether this provider can run in the current environment."""
        ...

    def extract_text(self, image_bytes: bytes) -> str:
        """Return recognised text for a single page image (PNG/JPEG bytes)."""
        ...


class NullOCRProvider:
    """Explicit 'no OCR configured' provider."""

    name = "none"

    def available(self) -> bool:
        return False

    def extract_text(self, image_bytes: bytes) -> str:
        raise OCRUnavailableError(
            "no OCR provider is configured; set EXAM_ENGINE_OCR_PROVIDER "
            "or install the optional 'ocr' extras"
        )
