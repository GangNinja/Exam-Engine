"""Syllabus unit and topic segmentation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from exam_engine.ingest.base import ExtractedPage
from exam_engine.ingest.normalize import normalize_text, truncate
from exam_engine.models.enums import SegmentationStatus, Severity
from exam_engine.models.question import Uncertainty

_UNIT_RE = re.compile(
    r"^\s*(?P<kind>UNIT|MODULE|CHAPTER)\s*[-–—:.]?\s*(?P<label>[IVXLCDM]+|\d{1,2})\b"
    r"\s*(?:[.:\-–—|])?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_HOURS_RE = re.compile(
    r"\(?\s*(?P<h>\d{1,3}(?:\.\d)?)\s*"
    r"(?:hours?|hrs?|periods?|lectures?|lecture hours?|teaching hours?)\s*\)?",
    re.IGNORECASE,
)
# A bare number pushed to the end of a heading by a layout column gap:
# "Unit I: Data Structures   12". The two-space gap distinguishes it from a
# number that is part of the title ("Unit 3: HTML 5").
_LAYOUT_HOURS_RE = re.compile(r"\s{2,}(?P<h>\d{1,3}(?:\.\d)?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-–—•*·▪◦]\s*")
_NUMBER_PREFIX_RE = re.compile(r"^\s*\(?\d+(?:\.\d+)*\s*[.)]?\s+")
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


@dataclass
class ExtractedUnit:
    unit_label: str
    unit_number: int | None
    title: str | None
    hours: float | None
    topics: list[str]
    raw_text: str
    page_number: int | None
    confidence: float
    uncertainties: list[Uncertainty] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class SyllabusSegmentation:
    units: list[ExtractedUnit] = field(default_factory=list)
    status: SegmentationStatus = SegmentationStatus.UNSEGMENTED
    confidence: float = 0.0
    uncertainties: list[Uncertainty] = field(default_factory=list)
    title_hint: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.status is not SegmentationStatus.SEGMENTED


@dataclass(frozen=True)
class _Line:
    text: str
    page_number: int


def roman_to_int(label: str) -> int | None:
    text = label.strip().upper()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    previous = 0
    for char in reversed(text):
        if char not in _ROMAN:
            return None
        value = _ROMAN[char]
        total = total - value if value < previous else total + value
        previous = max(previous, value)
    return total or None


def _flatten(pages: Sequence[ExtractedPage]) -> list[_Line]:
    lines: list[_Line] = []
    for page in pages:
        for raw in page.text.split("\n"):
            lines.append(_Line(text=raw, page_number=page.page_number))
    return lines


def _split_table_rest(rest: str) -> tuple[str, str | None, float | None]:
    """Interpret a DOCX table row such as ``Foundations | 8``.

    Returns ``(title, extra_body, hours)``. A numeric last cell in 0..200 is
    treated as teaching hours; remaining cells become topic text. Non-table
    text is returned unchanged so ordinary headings are untouched.
    """
    if "|" not in rest:
        return rest, None, None
    cells = [cell.strip() for cell in rest.split("|") if cell.strip()]
    if len(cells) < 2:
        return rest, None, None
    hours: float | None = None
    if re.fullmatch(r"\d{1,3}(?:\.\d)?", cells[-1]):
        candidate = float(cells[-1])
        if 0 < candidate <= 200:
            hours = candidate
            cells = cells[:-1]
    title = cells[0]
    extra = "; ".join(cells[1:]) if len(cells) > 1 else None
    return title, extra, hours


def _strip_hours(text: str, *, allow_layout_gap: bool = False) -> tuple[str, float | None]:
    match = _HOURS_RE.search(text)
    if match:
        hours = float(match.group("h"))
        cleaned = _HOURS_RE.sub("", text).strip(" :-–—|")
        return cleaned, hours
    if allow_layout_gap:
        layout = _LAYOUT_HOURS_RE.search(text.strip())
        if layout:
            cleaned = text.strip()[: layout.start()].strip(" :-–—|")
            # The number is only a column value when a real title remains.
            if cleaned and any(ch.isalpha() for ch in cleaned):
                return cleaned, float(layout.group("h"))
    return text.strip(" :-–—|"), None


def _split_topics(text: str) -> list[str]:
    """Split a unit body into topics without destroying notation."""
    topics: list[str] = []
    for raw_line in text.split("\n"):
        line = _BULLET_RE.sub("", raw_line).strip()
        line = _NUMBER_PREFIX_RE.sub("", line).strip()
        if not line:
            continue
        # Pipe separators come from DOCX table cells; semicolons are explicit
        # topic breaks. Commas are only split when the list is unambiguous.
        segments = re.split(r"[;|]", line)
        expanded: list[str] = []
        for segment in segments:
            segment = segment.strip(" .:-–—")
            if not segment:
                continue
            # Comma lists are only split when the line is unambiguously a list:
            # no colon (which usually introduces detail) and every part starts
            # with a capital letter. "traversals: preorder, inorder" stays whole.
            if "," in segment and ":" not in segment and segment.count(",") >= 2:
                parts = [part.strip(" .:-–—") for part in segment.split(",")]
                if all(len(part) >= 3 and part[:1].isupper() for part in parts):
                    expanded.extend(parts)
                    continue
            expanded.append(segment)
        for topic in expanded:
            cleaned = normalize_text(topic).strip()
            if len(cleaned) >= 3:
                topics.append(cleaned)
    # Stable de-duplication.
    seen: set[str] = set()
    unique: list[str] = []
    for topic in topics:
        key = topic.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(topic)
    return unique


def segment_syllabus(
    pages: Sequence[ExtractedPage], *, min_topics: int = 0
) -> SyllabusSegmentation:
    """Find UNIT/MODULE/CHAPTER blocks and their topics."""
    lines = _flatten(pages)
    if not lines:
        return SyllabusSegmentation(
            uncertainties=[
                Uncertainty(
                    code="no_syllabus_units_detected",
                    message="document has no text to segment",
                    severity=Severity.WARNING,
                )
            ]
        )

    headers: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = _UNIT_RE.match(line.text)
        if match:
            headers.append((index, match))

    title_hint = next(
        (line.text.strip() for line in lines if 3 <= len(line.text.strip()) <= 120),
        None,
    )

    if not headers:
        return SyllabusSegmentation(
            status=SegmentationStatus.UNSEGMENTED,
            uncertainties=[
                Uncertainty(
                    code="no_syllabus_units_detected",
                    message="no UNIT/MODULE/CHAPTER headings found; nothing was extracted",
                    severity=Severity.WARNING,
                )
            ],
            title_hint=title_hint,
        )

    units: list[ExtractedUnit] = []
    gaps: list[tuple[int, int]] = []
    previous_number: int | None = None
    seen_labels: dict[str, int] = {}

    for position, (start, match) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        block_lines = lines[start:end]
        raw_text = normalize_text("\n".join(line.text for line in block_lines))

        label_raw = match.group("label")
        unit_number = roman_to_int(label_raw)
        kind = match.group("kind").upper()
        kind_label = {"UNIT": "Unit", "MODULE": "Module", "CHAPTER": "Chapter"}.get(
            kind, kind.title()
        )
        unit_label = f"{kind_label} {label_raw.upper() if label_raw.isalpha() else label_raw}"

        rest = match.group("rest").lstrip("| ").strip()
        rest, table_body, table_hours = _split_table_rest(rest)
        rest_without_hours, hours = _strip_hours(rest, allow_layout_gap=True)
        if hours is None:
            hours = table_hours
        body_start = 1
        title = rest_without_hours or None
        if title is None and len(block_lines) > 1:
            candidate, candidate_hours = _strip_hours(
                block_lines[1].text.lstrip("| ").strip(), allow_layout_gap=True
            )
            if candidate and len(candidate) <= 160:
                title = candidate
                hours = hours if hours is not None else candidate_hours
                body_start = 2
        if title is not None:
            title = truncate(title, 200)

        body_lines = [line.text for line in block_lines[body_start:]]
        if table_body:
            body_lines.insert(0, table_body)
        body = normalize_text("\n".join(body_lines))
        if hours is None:
            _cleaned, body_hours = _strip_hours(body.split("\n")[0] if body else "")
            if body_hours is not None:
                hours = body_hours

        topics = _split_topics(body)
        uncertainties: list[Uncertainty] = []
        confidence = 0.9
        if unit_number is None:
            confidence -= 0.2
            uncertainties.append(
                Uncertainty(
                    code="unparsed_unit_number",
                    message=f"could not interpret unit label '{label_raw}'",
                )
            )
        if not topics:
            confidence -= 0.3
            uncertainties.append(
                Uncertainty(
                    code="empty_unit_body",
                    message="unit has no topic text",
                    severity=Severity.WARNING,
                )
            )
        if len(topics) < min_topics:
            confidence -= 0.1
        if title is None:
            confidence -= 0.05

        if unit_number is not None:
            if unit_number in seen_labels.values():
                confidence -= 0.1
                uncertainties.append(
                    Uncertainty(
                        code="duplicate_unit_number",
                        message=f"unit number {unit_number} appears more than once",
                        severity=Severity.WARNING,
                    )
                )
            if previous_number is not None and unit_number != previous_number + 1:
                gaps.append((previous_number, unit_number))
                confidence -= 0.05
                uncertainties.append(
                    Uncertainty(
                        code="unit_numbering_gap",
                        message=(
                            f"unit numbering jumps from {previous_number} to {unit_number}; "
                            "no unit was invented"
                        ),
                        severity=Severity.WARNING,
                    )
                )
            previous_number = unit_number
        seen_labels[unit_label] = unit_number or 0

        units.append(
            ExtractedUnit(
                unit_label=unit_label,
                unit_number=unit_number,
                title=title,
                hours=hours,
                topics=topics,
                raw_text=raw_text,
                page_number=block_lines[0].page_number,
                confidence=max(0.05, min(0.99, round(confidence, 3))),
                uncertainties=uncertainties,
            )
        )

    document_uncertainties: list[Uncertainty] = []
    if gaps:
        document_uncertainties.append(
            Uncertainty(
                code="unit_numbering_gap",
                message=f"{len(gaps)} unit numbering gap(s) detected",
                severity=Severity.WARNING,
                detail={"gaps": [list(gap) for gap in gaps[:10]]},
            )
        )
    if not units:
        document_uncertainties.append(
            Uncertainty(
                code="no_syllabus_units_detected",
                message="unit headings were found but no units could be built",
                severity=Severity.WARNING,
            )
        )

    confidence = round(sum(u.confidence for u in units) / len(units), 3) if units else 0.0
    status = SegmentationStatus.UNSEGMENTED
    if units:
        status = (
            SegmentationStatus.SEGMENTED
            if not document_uncertainties and confidence >= 0.6
            else SegmentationStatus.PARTIAL
        )

    return SyllabusSegmentation(
        units=units,
        status=status,
        confidence=confidence,
        uncertainties=document_uncertainties,
        title_hint=title_hint,
    )
