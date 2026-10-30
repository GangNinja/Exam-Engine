"""Configurable preprocessing: raw text in, normalised representation out.

The original text is never replaced. Each :class:`ProcessedText` records the
config hash, the SHA-256 of the raw input and the language hint, so a rerun can
tell whether its cached representation is still valid.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from exam_engine.ingest.normalize import normalize_text
from exam_engine.models.enums import Severity, TextOwnerType
from exam_engine.models.nlp import (
    PreprocessConfig,
    ProcessedText,
    Sentence,
    make_text_id,
)
from exam_engine.models.question import Uncertainty
from exam_engine.nlp.text import (
    detect_language,
    normalize_for_analysis,
    segment_sentences,
    tokenize,
)

SUPPORTED_LANGUAGES = frozenset({"en"})


def raw_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Preprocessor:
    """Produces :class:`ProcessedText` objects; holds no per-document state."""

    def __init__(self, config: PreprocessConfig | None = None) -> None:
        self.config = config or PreprocessConfig()

    @property
    def config_id(self) -> str:
        return self.config.config_id()

    def preprocess(
        self,
        text: str,
        *,
        course_id: str,
        owner_type: TextOwnerType,
        owner_id: str,
        document_id: str | None = None,
    ) -> ProcessedText:
        normalized = (
            normalize_for_analysis(text, unicode_form=self.config.unicode_form)
            if self.config.preserve_notation
            else normalize_text(text)
        )
        uncertainties: list[Uncertainty] = []
        if not normalized.strip():
            uncertainties.append(
                Uncertainty(
                    code="empty_text",
                    message="text is empty after normalisation; nothing to analyse",
                    severity=Severity.WARNING,
                )
            )
        language = detect_language(normalized) if normalized.strip() else None
        expected = (self.config.language or "en").casefold()
        if expected not in SUPPORTED_LANGUAGES:
            uncertainties.append(
                Uncertainty(
                    code="language_unsupported",
                    message=(
                        f"configured language '{expected}' is outside the default analysers; "
                        "statistical results will be unreliable"
                    ),
                    severity=Severity.WARNING,
                    detail={"language": expected, "source": "config"},
                )
            )
        if language is not None and language not in SUPPORTED_LANGUAGES:
            uncertainties.append(
                Uncertainty(
                    code="language_unsupported",
                    message=(
                        f"detected language '{language}' is outside the default analysers; "
                        "statistical results will be unreliable"
                    ),
                    severity=Severity.WARNING,
                    detail={"language": language, "source": "detected"},
                )
            )
        sentences: list[Sentence] = []
        if normalized.strip():
            sentences = segment_sentences(normalized, min_chars=self.config.min_sentence_chars)
            if not sentences:
                uncertainties.append(
                    Uncertainty(
                        code="empty_text",
                        message="text produced no usable sentences",
                        severity=Severity.WARNING,
                    )
                )
        tokens = tokenize(normalized)
        return ProcessedText(
            text_id=make_text_id(owner_type, owner_id),
            course_id=course_id,
            owner_type=owner_type,
            owner_id=owner_id,
            document_id=document_id,
            raw_sha256=raw_sha256(text),
            config_id=self.config_id,
            text=normalized,
            sentences=sentences,
            language=language,
            token_count=sum(1 for token in tokens if token.is_word),
            char_count=len(normalized),
            uncertainties=uncertainties,
        )


_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?$")


@dataclass(frozen=True)
class SectionChunk:
    """One section plus its character span in the split text."""

    text: str
    start: int
    end: int


def split_section_chunks(
    text: str,
    *,
    max_sections: int = 200,
    min_chars: int = 120,
    normalize: bool = True,
) -> list[SectionChunk]:
    """Split a document into coarse sections and record their spans.

    Blocks are separated by blank lines. A short block that looks like a heading
    (no terminal punctuation, or ends with a colon, or is upper case) starts a new
    section; other short blocks are merged into the section that follows so that
    wrapped paragraphs are not fragmented.

    ``start``/``end`` index the string that was split, so callers that pass an
    already-normalised text (``normalize=False``) get offsets into that exact
    text. Normalisation collapses whitespace, which is why searching the raw
    source for a section afterwards is not reliable.
    """
    if not text.strip():
        return []
    source = normalize_text(text) if normalize else text
    blocks = [block.strip("\n") for block in re.split(r"\n\s*\n", source)]
    blocks = [block for block in blocks if block.strip()]
    if not blocks:
        return []

    sections: list[list[str]] = []
    pending: list[str] = []
    for block in blocks:
        first_line = block.split("\n", 1)[0].strip()
        is_heading = _looks_like_heading(first_line, block)
        if is_heading:
            if pending:
                sections.append(pending)
                pending = []
            sections.append([block])
        elif len(block) < min_chars and sections and not _SENTENCE_END_RE.search(block):
            pending.append(block)
        else:
            joined = "\n\n".join(pending + [block]) if pending else block
            pending = []
            if (
                sections
                and len(sections[-1][-1]) < min_chars
                and not _SENTENCE_END_RE.search(sections[-1][-1])
            ):
                sections[-1].append(joined)
            else:
                sections.append([joined])
    if pending:
        if sections:
            sections[-1].append("\n\n".join(pending))
        else:
            sections.append(pending)

    merged = ["\n\n".join(part for part in section if part.strip()) for section in sections]
    merged = [section for section in merged if section.strip()]

    chunks: list[SectionChunk] = []
    cursor = 0
    for section in merged[:max_sections]:
        start = source.find(section, cursor)
        if start < 0:
            start = source.find(section)
        if start < 0:  # pragma: no cover - sections are substrings by construction
            start = min(cursor, len(source))
        end = start + len(section)
        chunks.append(SectionChunk(text=section, start=start, end=end))
        cursor = end
    return chunks


def split_sections(text: str, *, max_sections: int = 200, min_chars: int = 120) -> list[str]:
    """Split a document into coarse sections (text only; see the chunk variant)."""
    return [
        chunk.text
        for chunk in split_section_chunks(text, max_sections=max_sections, min_chars=min_chars)
    ]


def _looks_like_heading(first_line: str, block: str) -> bool:
    line = first_line.strip()
    if not line or len(line) > 90:
        return False
    if _SENTENCE_END_RE.search(line):
        return False
    words = line.split()
    if len(words) > 12:
        return False
    letters = [char for char in line if char.isalpha()]
    if letters and all(char.isupper() for char in letters):
        return True
    if line.endswith(":"):
        return True
    return block.count("\n") == 0 and len(line) <= 60 and line[:1].isupper()


__all__ = [
    "Preprocessor",
    "SUPPORTED_LANGUAGES",
    "SectionChunk",
    "raw_sha256",
    "split_section_chunks",
    "split_sections",
]
