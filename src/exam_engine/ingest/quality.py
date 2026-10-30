"""Extraction quality assessment and duplicate detection."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from exam_engine.ingest.base import ExtractedPage
from exam_engine.models.enums import Severity
from exam_engine.models.question import QuestionRecord, Uncertainty

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPEATED_RE = re.compile(r"(.)\1{15,}")
_MOJIBAKE_MARKERS = ("Ã", "â€", "Â", "ï¿½")


@dataclass
class QualityReport:
    score: float = 1.0
    flags: list[str] = field(default_factory=list)
    char_count: int = 0
    page_count: int = 0
    nonempty_pages: int = 0
    scanned_pages: list[int] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.char_count > 0 and self.score >= 0.2


def assess_pages(pages: Sequence[ExtractedPage], *, min_page_chars: int = 40) -> QualityReport:
    """Score extracted text and flag known degradation patterns."""
    report = QualityReport(page_count=len(pages))
    if not pages:
        report.score = 0.0
        report.flags.append("empty_document")
        return report

    texts = [page.text for page in pages]
    # Count only meaningful characters: a whitespace-only page carries no text
    # even though it has length.
    report.char_count = sum(len(text.strip()) for text in texts)
    if report.char_count == 0:
        report.score = 0.0
        report.flags.append("empty_document")
        return report

    nonempty = [page for page in pages if len(page.text.strip()) >= min_page_chars]
    report.nonempty_pages = len(nonempty)
    ratio = len(nonempty) / len(pages)
    score = 1.0

    if ratio < 1.0:
        report.flags.append("low_text_yield")
        score -= 0.35 * (1.0 - ratio)

    scanned = [
        page.page_number
        for page in pages
        if page.has_images and len(page.text.strip()) < min_page_chars
    ]
    if scanned:
        report.scanned_pages = scanned
        report.flags.append("scanned_pages")
        score -= 0.25 * (len(scanned) / len(pages))

    joined = "\n".join(texts)
    control = len(_CONTROL_RE.findall(joined))
    if control / max(1, len(joined)) > 0.001:
        report.flags.append("control_characters")
        score -= 0.1

    replacements = joined.count("\ufffd")
    if replacements / max(1, len(joined)) > 0.002:
        report.flags.append("replacement_characters")
        score -= 0.15

    if _REPEATED_RE.search(joined):
        report.flags.append("repeated_characters")
        score -= 0.1

    if nonempty and report.char_count / len(nonempty) < 80:
        report.flags.append("sparse_text")
        score -= 0.1

    if sum(joined.count(marker) for marker in _MOJIBAKE_MARKERS) >= 2:
        report.flags.append("mojibake_suspect")
        score -= 0.15

    report.score = round(max(0.0, min(1.0, score)), 3)
    return report


def mark_question_duplicates(
    questions: list[QuestionRecord],
    known: dict[str, str] | None = None,
) -> int:
    """Flag questions whose text already appeared in this course.

    ``known`` maps fingerprint -> question_id and is updated in place, so a single
    batch also detects duplicates across documents of the same course. Duplicates
    are flagged, never deleted: the same question can legitimately be repeated.
    """
    seen = known if known is not None else {}
    duplicates = 0
    for question in questions:
        fingerprint = question.fingerprint
        existing = seen.get(fingerprint)
        if existing and existing != question.question_id:
            question.duplicate_of = existing
            question.uncertainties.append(
                Uncertainty(
                    code="duplicate_question",
                    message="identical question text already exists in this course",
                    severity=Severity.WARNING,
                    detail={"duplicate_of": existing},
                )
            )
            duplicates += 1
        else:
            seen[fingerprint] = question.question_id
    return duplicates
