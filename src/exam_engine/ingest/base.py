"""Adapter protocol, extracted payloads and the adapter registry.

Every input format is a plugin: adding a format means adding an adapter, not
editing the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from exam_engine.config import IngestionLimits
from exam_engine.models.enums import ExtractionMethod, Severity, SourceFormat
from exam_engine.models.ingestion import IngestionError
from exam_engine.models.question import Uncertainty


@dataclass(frozen=True)
class ExtractionContext:
    path: Path
    limits: IngestionLimits = field(default_factory=IngestionLimits)
    base_dir: Path | None = None


@dataclass
class ExtractedPage:
    page_number: int
    text: str = ""
    method: ExtractionMethod = ExtractionMethod.EMBEDDED_TEXT
    has_images: bool = False
    ocr_attempted: bool = False


@dataclass
class ExtractedQuestion:
    text: str
    question_number: str | None = None
    number_sort: int | None = None
    section: str | None = None
    unit: str | None = None
    marks: float | None = None
    marks_source: str | None = None
    page_number: int | None = None
    source_locator: str | None = None
    confidence: float = 0.5
    uncertainties: list[Uncertainty] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedDocument:
    format: SourceFormat
    pages: list[ExtractedPage] = field(default_factory=list)
    questions: list[ExtractedQuestion] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[IngestionError] = field(default_factory=list)
    needs_ocr_pages: list[int] = field(default_factory=list)
    table_count: int = 0
    title_hint: str | None = None

    @property
    def char_count(self) -> int:
        return sum(len(page.text) for page in self.pages)


@runtime_checkable
class DocumentAdapter(Protocol):
    """Extracts pages (and structured questions, for banks) from one file."""

    name: str
    suffixes: tuple[str, ...]

    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument: ...


class UnsupportedFormatError(LookupError):
    def __init__(self, suffix: str) -> None:
        super().__init__(f"no adapter registered for '{suffix}'")
        self.suffix = suffix


_ADAPTERS: dict[str, DocumentAdapter] = {}


def register_adapter(adapter: DocumentAdapter, *, replace: bool = False) -> None:
    for suffix in adapter.suffixes:
        key = suffix.lower()
        if key in _ADAPTERS and not replace:
            raise ValueError(f"adapter already registered for suffix '{key}'")
        _ADAPTERS[key] = adapter


def get_adapter_for_suffix(suffix: str) -> DocumentAdapter:
    key = suffix.lower()
    if not key.startswith("."):
        key = "." + key
    try:
        return _ADAPTERS[key]
    except KeyError as exc:
        raise UnsupportedFormatError(key) from exc


def adapter_for_path(path: Path) -> DocumentAdapter:
    return get_adapter_for_suffix(path.suffix)


def registered_suffixes() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def ingestion_warning(
    code: str,
    message: str,
    *,
    severity: Severity = Severity.WARNING,
    detail: dict[str, Any] | None = None,
) -> IngestionError:
    return IngestionError(code=code, message=message, severity=severity, detail=detail or {})


def apply_char_limit(
    pages: list[ExtractedPage], limit: int
) -> tuple[list[ExtractedPage], IngestionError | None]:
    """Truncate oversized documents predictably, reporting that it happened."""
    total = sum(len(page.text) for page in pages)
    if total <= limit:
        return pages, None
    remaining = limit
    kept: list[ExtractedPage] = []
    for page in pages:
        if remaining <= 0:
            break
        if len(page.text) <= remaining:
            kept.append(page)
            remaining -= len(page.text)
        else:
            kept.append(
                ExtractedPage(
                    page_number=page.page_number,
                    text=page.text[:remaining],
                    method=page.method,
                    has_images=page.has_images,
                    ocr_attempted=page.ocr_attempted,
                )
            )
            remaining = 0
    warning = ingestion_warning(
        "document_truncated",
        f"document text truncated at {limit} characters ({total - limit} characters dropped)",
        detail={"limit": limit, "original_chars": total},
    )
    return kept, warning
