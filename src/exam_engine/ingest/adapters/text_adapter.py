"""Plain text extraction with encoding detection and form-feed pagination."""

from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes

from exam_engine.ingest.base import (
    ExtractedDocument,
    ExtractedPage,
    ExtractionContext,
    ingestion_warning,
)
from exam_engine.ingest.normalize import normalize_text
from exam_engine.ingest.validation import FileValidationError, read_limited_bytes
from exam_engine.models.enums import ExtractionMethod, Severity, SourceFormat


class TextAdapter:
    name = "text"
    suffixes: tuple[str, ...] = (".txt", ".text", ".md")

    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument:
        data = read_limited_bytes(path, context.limits.max_file_bytes + 1)
        text, encoding, confident = decode_text(data)

        warnings = []
        if not confident:
            warnings.append(
                ingestion_warning(
                    "parse_error",
                    f"text encoding guessed as {encoding} with low confidence; "
                    "characters may be wrong",
                    severity=Severity.WARNING,
                )
            )

        chunks = text.split("\x0c")
        pages: list[ExtractedPage] = []
        for index, chunk in enumerate(chunks, start=1):
            normalised = normalize_text(chunk)
            pages.append(
                ExtractedPage(
                    page_number=index,
                    text=normalised,
                    method=(ExtractionMethod.PLAIN_TEXT if normalised else ExtractionMethod.NONE),
                )
            )
        if len(pages) > context.limits.max_pages:
            raise FileValidationError(
                "page_limit_exceeded",
                f"'{path.name}' has {len(pages)} form-feed pages; limit is "
                f"{context.limits.max_pages}",
                {"pages": len(pages), "limit": context.limits.max_pages},
            )

        if not any(page.text.strip() for page in pages):
            warnings.append(
                ingestion_warning(
                    "empty_document",
                    "text file contains only whitespace",
                    severity=Severity.WARNING,
                )
            )

        return ExtractedDocument(
            format=SourceFormat.TXT,
            pages=pages,
            metadata={"extractor": "plain-text", "encoding": encoding},
            warnings=warnings,
            title_hint=_title_hint(pages),
        )


def decode_text(data: bytes) -> tuple[str, str, bool]:
    """Decode bytes, preferring UTF-8 and falling back to detection.

    charset-normalizer can rank ``utf_16_le`` first for short single-byte text
    (pairs of ASCII bytes look like CJK), so UTF-16/32 guesses are discarded
    unless the payload actually contains the NUL bytes such text requires.
    """
    for candidate in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(candidate), candidate, True
        except UnicodeDecodeError:
            continue

    matches = [match for match in from_bytes(data) if _plausible_match(match, data)]
    if matches:
        match = matches[0]
        text = str(match)
        encoding = match.encoding or "unknown"
        # charset-normalizer reports chaotic matches for noise-like input.
        confident = not match.chaos and match.percent_chaos < 10 and _looks_clean(text)
        if confident:
            return text, encoding, True

    fallback = data.decode("cp1252", errors="replace")
    if _looks_clean(fallback):
        return fallback, "cp1252", False
    return data.decode("latin-1", errors="replace"), "latin-1", False


def _plausible_match(match, data: bytes) -> bool:
    encoding = (match.encoding or "").lower().replace("-", "_")
    if encoding.startswith(("utf_16", "utf_32")):
        # Latin-script UTF-16 contains NUL bytes; without them this is a false
        # positive on ASCII/single-byte text.
        return data.count(0) / max(1, len(data)) >= 0.15
    return match.percent_chaos < 30


def _looks_clean(text: str) -> bool:
    if not text:
        return False
    bad = sum(
        1
        for character in text
        if character == "\ufffd" or (ord(character) < 32 and character not in "\t\n\r")
    )
    return bad / len(text) <= 0.01


def _title_hint(pages: list[ExtractedPage]) -> str | None:
    for page in pages:
        for line in page.text.split("\n"):
            stripped = line.strip()
            if 3 <= len(stripped) <= 120:
                return stripped
    return None
