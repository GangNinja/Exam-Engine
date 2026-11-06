"""Syllabus segmentation for examination syllabi.

The Phase 1 segmenter recognises UNIT/MODULE/CHAPTER headings and DOCX table
rows. The personal GATE, CBSE, JEE and NEET syllabi use additional structures:
``Section 1:`` headings, numbered topic lists and, for a few JEE documents,
plain layout headings. This module falls back to those structures only when the
existing segmenter found nothing, and marks every inferred unit as review-needed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from exam_engine.ingest.base import ExtractedPage
from exam_engine.ingest.normalize import normalize_text, truncate
from exam_engine.ingest.syllabus_segmenter import (
    ExtractedUnit,
    SyllabusSegmentation,
    roman_to_int,
    segment_syllabus,
)
from exam_engine.models.enums import SegmentationStatus, Severity
from exam_engine.models.question import Uncertainty

_EXPLICIT_RE = re.compile(
    r"^\s*(?P<kind>UNIT|MODULE|CHAPTER|SECTION)\s*[-–—:.]?\s*"
    r"(?P<label>[IVXLCDM]+|\d{1,2})\b\s*(?:[.:\-–—|])?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_NUMBERED_RE = re.compile(r"^\s*(?P<label>\d{1,2})\.\s+(?P<rest>[A-Z][^.]{2,110})$")
_PAGE_FURNITURE_RE = re.compile(
    r"^(?:page\s+\d+\s+of\s+\d+|cbse class .*syllabus 20\d\d-\d\d|"
    r"gate 20\d\d .*\|.*|neet \(ug\) 20\d\d .*\|.*)$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[n•▪◦*·\-–—]|\u2022)\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _flatten(pages: Sequence[ExtractedPage]) -> list[tuple[str, int]]:
    return [(line, page.page_number) for page in pages for line in page.text.split("\n")]


def _is_furniture(line: str) -> bool:
    return bool(_PAGE_FURNITURE_RE.match(line.strip()))


def _body_topics(lines: Sequence[str]) -> list[str]:
    # PDF text arrives hard-wrapped. Join continuation lines before splitting so
    # "the role" is not stored as a topic separate from its sentence.
    paragraphs: list[str] = []
    buffer = ""
    for raw_line in lines:
        line = _BULLET_RE.sub("", raw_line).strip()
        if not line or _is_furniture(line):
            if buffer:
                paragraphs.append(buffer)
                buffer = ""
            continue
        if buffer and (not re.search(r"[.!?]$", buffer) or line[:1].islower()):
            buffer = f"{buffer} {line}"
        else:
            if buffer:
                paragraphs.append(buffer)
            buffer = line
    if buffer:
        paragraphs.append(buffer)

    topics: list[str] = []
    for paragraph in paragraphs:
        for segment in re.split(r"[;|]", paragraph):
            segment = segment.strip(" .:-\u2013\u2014")
            if len(segment) < 3:
                continue
            pieces = [segment]
            if len(segment) > 260:
                sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(segment)]
                pieces = [part for part in sentences if len(part.split()) >= 3] or [segment]
            for piece in pieces:
                cleaned = normalize_text(piece).strip()
                if len(cleaned) >= 3:
                    topics.append(cleaned)
    seen: set[str] = set()
    unique: list[str] = []
    for topic in topics:
        key = topic.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(topic)
    return unique


def _explicit_heading(line: str) -> tuple[str, int | None, str] | None:
    match = _EXPLICIT_RE.match(line)
    if not match:
        return None
    kind = match.group("kind").upper()
    label = match.group("label")
    number = roman_to_int(label)
    title = match.group("rest").strip(" :-\u2013\u2014") or None
    return f"{kind.title()} {label.upper() if label.isalpha() else label}", number, title or ""


def _layout_headings(
    lines: Sequence[tuple[str, int]],
) -> list[tuple[int, str, int | None, str, str]]:
    """Find conservative structural headings in a syllabus without marked units."""
    candidates: list[tuple[int, str, int | None, str, str]] = []
    explicit: list[tuple[int, str, int | None, str, str]] = []
    for index, (line, _page) in enumerate(lines):
        heading = _explicit_heading(line)
        if heading is None:
            continue
        label, number, title = heading
        explicit.append((index, label, number, title, "explicit"))
    if explicit:
        return [(i, label, number, title, kind) for i, label, number, title, kind in explicit]

    numbered: list[tuple[int, str, int | None, str, str]] = []
    for index, (line, _page) in enumerate(lines):
        match = _NUMBERED_RE.match(line.strip())
        if not match:
            continue
        title = match.group("rest").strip()
        if len(title) > 110:
            continue
        numbered.append(
            (
                index,
                f"Unit {match.group('label')}",
                int(match.group("label")),
                title,
                "numbered",
            )
        )
    if numbered:
        return numbered

    # Layout headings: a short standalone title followed by prose. This is only
    # used when the document exposes no explicit or numbered structure.
    for index, (line, _page) in enumerate(lines):
        text = line.strip()
        if not (3 <= len(text) <= 70) or _is_furniture(text):
            continue
        if text.endswith((".", ":", ";", "?")) or len(text.split()) > 7:
            continue
        if not text[:1].isupper() or not any(char.isalpha() for char in text):
            continue
        following = next(
            (
                lines[pos][0].strip()
                for pos in range(index + 1, len(lines))
                if lines[pos][0].strip()
            ),
            "",
        )
        if len(following) >= 70:
            candidates.append((index, text, None, text, "layout"))
    return candidates


def segment_exam_syllabus(pages: Sequence[ExtractedPage]) -> SyllabusSegmentation:
    """Segment an examination syllabus, preferring explicit Phase 1 headings."""
    base = segment_syllabus(pages)
    if base.units:
        return base
    lines = _flatten(pages)
    headings = _layout_headings(lines)
    if not headings:
        return base

    units: list[ExtractedUnit] = []
    for position, (start, label, number, title, style) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        block = lines[start:end]
        raw_text = normalize_text("\n".join(line for line, _page in block))
        body_lines = [line for line, _page in block[1:]]
        topics = _body_topics(body_lines)
        uncertainties: list[Uncertainty] = []
        confidence = 0.88 if style == "explicit" else 0.62
        if style != "explicit":
            uncertainties.append(
                Uncertainty(
                    code="inferred_exam_heading",
                    message=(
                        "the syllabus has no UNIT/MODULE/CHAPTER/SECTION heading here; "
                        "the section boundary was inferred from the document layout"
                    ),
                    severity=Severity.INFO,
                    detail={"style": style},
                )
            )
        if not topics:
            confidence -= 0.25
            uncertainties.append(
                Uncertainty(
                    code="empty_unit_body",
                    message="section has no topic text",
                    severity=Severity.WARNING,
                )
            )
        units.append(
            ExtractedUnit(
                unit_label=label,
                unit_number=number,
                title=truncate(title, 200) if title else None,
                hours=None,
                topics=topics,
                raw_text=raw_text,
                page_number=block[0][1] if block else None,
                confidence=max(0.05, min(0.95, confidence)),
                uncertainties=uncertainties,
                metadata={"segmentation_style": style},
            )
        )

    confidence = round(sum(unit.confidence for unit in units) / len(units), 3) if units else 0.0
    return SyllabusSegmentation(
        units=units,
        status=SegmentationStatus.PARTIAL,
        confidence=confidence,
        uncertainties=[
            Uncertainty(
                code="inferred_syllabus_structure",
                message=(
                    "the standard syllabus segmenter found no unit headings; the structure "
                    "was inferred from Section/numbered/layout headings and needs review"
                ),
                severity=Severity.WARNING,
                detail={"units": len(units)},
            )
        ],
        title_hint=next((line.strip() for line, _page in lines if line.strip()), None),
    )


__all__ = ["segment_exam_syllabus"]
