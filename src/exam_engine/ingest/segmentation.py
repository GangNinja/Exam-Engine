"""Heuristic question segmentation with explicit uncertainty.

Design rules:
* only real numbered markers create a question - no placeholder questions are
  invented to fill numbering gaps;
* every doubt (gap, reset, missing marks, instruction-like text) becomes an
  :class:`Uncertainty` on the record so later phases can filter it;
* source text is preserved verbatim apart from whitespace normalisation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from exam_engine.ingest.base import ExtractedPage, ExtractedQuestion
from exam_engine.ingest.normalize import (
    drop_page_artifacts,
    normalize_text,
    truncate,
)
from exam_engine.models.enums import SegmentationStatus, Severity
from exam_engine.models.question import Uncertainty

# --- patterns ---------------------------------------------------------------

# Strong marker: "Q1.", "Q.1)", "Question 2:", "Question 3 -"
_Q_PREFIX_RE = re.compile(
    r"^\s*(?:Q(?:uestion)?\s*[.:\-]?\s*)(?P<num>\d{1,3})\s*(?:[.)\]:\-]|\s)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
# Common marker: "1.", "1)", "1]"
_NUMBER_RE = re.compile(r"^\s*(?P<num>\d{1,3})\s*[.)\]]\s*(?P<rest>.*)$")
# Bracketed marker: "(1) text"
_BRACKET_RE = re.compile(r"^\s*\(\s*(?P<num>\d{1,3})\s*\)\s*(?P<rest>.*)$")
# Decimal values such as "3.14" must not be read as question 3.
_DECIMAL_RE = re.compile(r"^\s*\(?\d{1,3}\.\d")

_SECTION_RE = re.compile(
    r"^\s*(?:SECTION|PART)\s*[-–—:.]?\s*(?P<label>[A-Z]{1,3}|\d{1,2})\b"
    r"\s*(?:[.:\-–—])?\s*(?P<title>.*)$",
    re.IGNORECASE,
)

_MARKS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\[(]\s*(?P<v>\d{1,3}(?:\.\d+)?)\s*(?:marks?|mks?|m)\s*[\])]", re.I), "explicit"),
    (re.compile(r"(?P<v>\d{1,3}(?:\.\d+)?)\s*marks?\b", re.I), "explicit"),
    (re.compile(r"\bmarks?\s*[:=]\s*(?P<v>\d{1,3}(?:\.\d+)?)", re.I), "labelled"),
    (re.compile(r"[\[(]\s*(?P<v>\d{1,3}(?:\.\d+)?)\s*[\])]\s*[.!]?\s*$"), "bare_bracket"),
)

_INSTRUCTION_RE = re.compile(
    r"\b(answer all|attempt all|attempt any|all questions are compulsory|"
    r"time allowed|max(?:imum)?\.?\s*marks|duration|general instructions?|"
    r"write your (?:name|roll)|roll no|registration no|"
    r"use of (?:a )?calculator|do not write)\b",
    re.IGNORECASE,
)

MAX_PLAUSIBLE_QUESTION_NUMBER = 150


@dataclass
class QuestionSegmentation:
    questions: list[ExtractedQuestion] = field(default_factory=list)
    status: SegmentationStatus = SegmentationStatus.UNSEGMENTED
    confidence: float = 0.0
    uncertainties: list[Uncertainty] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    numbering_gaps: list[tuple[int, int]] = field(default_factory=list)
    rejected_markers: int = 0

    @property
    def needs_review(self) -> bool:
        return self.status is not SegmentationStatus.SEGMENTED or any(
            q.confidence < 0.6 for q in self.questions
        )


@dataclass(frozen=True)
class _Line:
    text: str
    page_number: int
    page_line: int
    order: int


@dataclass(frozen=True)
class _Candidate:
    order: int
    number: int
    rest: str
    section: str | None
    marker: str
    instruction_like: bool = False


@dataclass(frozen=True)
class _SectionBoundary:
    order: int
    label: str


def _flatten(pages: Sequence[ExtractedPage]) -> list[_Line]:
    lines: list[_Line] = []
    order = 0
    for page in pages:
        for page_line, raw in enumerate(page.text.split("\n"), start=1):
            lines.append(
                _Line(
                    text=raw,
                    page_number=page.page_number,
                    page_line=page_line,
                    order=order,
                )
            )
            order += 1
    return lines


def _match_marker(line: str) -> tuple[int, str, str] | None:
    if _DECIMAL_RE.match(line):
        return None
    marker_patterns = (
        ("q_prefix", _Q_PREFIX_RE),
        ("number", _NUMBER_RE),
        ("bracket", _BRACKET_RE),
    )
    for marker, pattern in marker_patterns:
        match = pattern.match(line)
        if not match:
            continue
        number = int(match.group("num"))
        if number < 1 or number > MAX_PLAUSIBLE_QUESTION_NUMBER:
            continue
        rest = (match.group("rest") or "").strip()
        return number, rest, marker
    return None


def _match_section(line: str) -> str | None:
    match = _SECTION_RE.match(line)
    if not match:
        return None
    label = match.group("label").upper()
    title = match.group("title").strip(" :-–—")
    # A bare "Part I" heading is fine; a sentence starting with the word is not.
    if len(title) > 80:
        return None
    return f"SECTION {label} — {title}" if title else f"SECTION {label}"


def _collect_boundaries(lines: list[_Line]) -> list[_SectionBoundary]:
    boundaries: list[_SectionBoundary] = []
    for line in lines:
        label = _match_section(line.text)
        if label:
            boundaries.append(_SectionBoundary(order=line.order, label=label))
    return boundaries


def _section_for(order: int, boundaries: list[_SectionBoundary]) -> str | None:
    current: str | None = None
    for boundary in boundaries:
        if boundary.order >= order:
            break
        current = boundary.label
    return current


def _collect_candidates(
    lines: list[_Line], boundaries: list[_SectionBoundary]
) -> tuple[list[_Candidate], int]:
    boundary_orders = {b.order for b in boundaries}
    candidates: list[_Candidate] = []
    rejected = 0
    for line in lines:
        if line.order in boundary_orders:
            continue
        match = _match_marker(line.text)
        if not match:
            continue
        number, rest, marker = match
        # A marker with no body is still recorded as a candidate; the body check
        # after text assembly decides whether it becomes a question.
        candidates.append(
            _Candidate(
                order=line.order,
                number=number,
                rest=rest,
                section=_section_for(line.order, boundaries),
                marker=marker,
                instruction_like=bool(_INSTRUCTION_RE.search(rest)),
            )
        )
    return candidates, rejected


def _assemble_text(lines: list[_Line], start: int, end: int) -> tuple[str, int, int]:
    """Join lines [start, end) and return (text, first_line, last_line)."""
    if start >= end:
        return "", start, start
    chunk = [line.text for line in lines[start:end]]
    text = normalize_text(drop_page_artifacts("\n".join(chunk)))
    return text, lines[start].page_line, lines[min(end, len(lines)) - 1].page_line


def segment_questions_pages(
    pages: Sequence[ExtractedPage],
    *,
    min_chars: int = 8,
    max_questions: int = 2000,
    min_confidence: float = 0.6,
) -> QuestionSegmentation:
    """Segment numbered questions from extracted pages."""
    lines = _flatten(pages)
    if not lines:
        return QuestionSegmentation(
            status=SegmentationStatus.UNSEGMENTED,
            uncertainties=[
                Uncertainty(
                    code="no_questions_detected",
                    message="document has no text to segment",
                    severity=Severity.WARNING,
                )
            ],
        )

    boundaries = _collect_boundaries(lines)
    candidates, rejected = _collect_candidates(lines, boundaries)
    sections = [b.label for b in boundaries]

    if not candidates:
        return QuestionSegmentation(
            status=SegmentationStatus.UNSEGMENTED,
            uncertainties=[
                Uncertainty(
                    code="no_questions_detected",
                    message="no numbered question markers found; nothing was extracted",
                    severity=Severity.WARNING,
                    detail={"line_count": len(lines)},
                )
            ],
            sections=sections,
            rejected_markers=rejected,
        )

    # Monotonic acceptance with explicit section resets.
    accepted: list[_Candidate] = []
    conflicts: list[dict[str, object]] = []
    dropped_instructions: list[dict[str, object]] = []
    numbering_gaps: list[tuple[int, int]] = []
    last_number = 0
    last_section: str | None = None

    for candidate in candidates:
        # Numbered items in a paper's preamble ("1. Answer all questions.") are
        # instructions, not questions. They sit before the first section heading
        # and before any real question, so dropping them keeps the real
        # questions' numbering intact. A preamble item that carries marks is a
        # real question and is kept.
        # A sectioned paper that starts with numbered items before its first
        # heading is listing instructions. An unmarked real question there is
        # rare and the drop is reported, so nothing disappears silently.
        preamble = bool(boundaries) and candidate.section is None and not accepted
        preamble_instruction = preamble and extract_marks(candidate.rest)[0] is None
        if preamble_instruction:
            dropped_instructions.append(
                {"number": candidate.number, "line_text": truncate(candidate.rest, 80)}
            )
            continue

        is_reset = (
            candidate.section is not None
            and candidate.section != last_section
            and candidate.number <= 2
        )
        if candidate.number <= last_number and not is_reset:
            # Real questions often restart at 1 after a block of numbered
            # instructions that consumed 1..k. Drop those trailing instruction
            # markers so the real question survives; the drop is reported.
            removed: list[_Candidate] = []
            # Only repair when the run we would undo actually contains
            # instruction-like text, otherwise genuine duplicate numbering must
            # stay visible as a conflict.
            if not candidate.instruction_like and any(c.instruction_like for c in accepted):
                while accepted and accepted[-1].number >= candidate.number:
                    removed.append(accepted.pop())
            if removed:
                dropped_instructions.extend(
                    {
                        "number": dropped.number,
                        "line_text": truncate(dropped.rest, 80),
                    }
                    for dropped in removed
                )
                last_number = accepted[-1].number if accepted else 0
                last_section = accepted[-1].section if accepted else None
                is_reset = True
        if candidate.number <= last_number and not is_reset:
            conflicts.append(
                {
                    "number": candidate.number,
                    "line_text": truncate(candidate.rest, 60),
                    "after": last_number,
                }
            )
            continue
        if not is_reset and last_number and candidate.number != last_number + 1:
            numbering_gaps.append((last_number, candidate.number))
        accepted.append(candidate)
        last_number = candidate.number
        last_section = candidate.section

    truncated = len(accepted) > max_questions
    if truncated:
        accepted = accepted[:max_questions]

    # Boundaries terminate a question body.
    boundary_orders = sorted({b.order for b in boundaries})
    candidate_orders = [c.order for c in accepted]
    gap_by_current = {current: previous for previous, current in numbering_gaps}
    questions: list[ExtractedQuestion] = []
    empty_bodies = 0

    for position, candidate in enumerate(accepted):
        next_candidate = (
            candidate_orders[position + 1] if position + 1 < len(candidate_orders) else len(lines)
        )
        next_boundary = next((o for o in boundary_orders if o > candidate.order), len(lines))
        end_order = min(next_candidate, next_boundary)

        start_index = candidate.order
        end_index = max(start_index + 1, end_order)
        clamped_end = min(end_index, len(lines))
        has_body = bool(candidate.rest.strip()) or any(
            lines[i].text.strip() for i in range(start_index + 1, clamped_end)
        )
        if not has_body:
            empty_bodies += 1
            continue
        text, first_line, last_line = _assemble_text(lines, start_index, clamped_end)
        if not text.strip():
            empty_bodies += 1
            continue

        start_line = lines[start_index]
        end_line = lines[min(end_index, len(lines)) - 1]
        spans_pages = start_line.page_number != end_line.page_number
        locator = (
            f"pages {start_line.page_number}-{end_line.page_number}"
            if spans_pages
            else f"page {start_line.page_number}, lines {first_line}-{last_line}"
        )

        uncertainties: list[Uncertainty] = []
        confidence = 0.92 if candidate.marker == "q_prefix" else 0.86

        if candidate.number in gap_by_current:
            previous = gap_by_current[candidate.number]
            confidence -= 0.08
            uncertainties.append(
                Uncertainty(
                    code="numbering_gap",
                    message=(
                        f"question jumps from {previous} to {candidate.number}; "
                        "no placeholder question was created"
                    ),
                    severity=Severity.WARNING,
                    detail={"previous": previous, "current": candidate.number},
                )
            )
        if (
            candidate.section is not None
            and candidate.number <= 2
            and position > 0
            and accepted[position - 1].section != candidate.section
        ):
            confidence -= 0.05
            uncertainties.append(
                Uncertainty(
                    code="numbering_reset",
                    message=f"numbering restarts at {candidate.number} in {candidate.section}",
                    severity=Severity.INFO,
                )
            )
        if spans_pages:
            uncertainties.append(
                Uncertainty(
                    code="spans_pages",
                    message=f"question text continues onto page {end_line.page_number}",
                    severity=Severity.INFO,
                )
            )

        marks, marks_source, marks_uncertainties, marks_penalty = extract_marks(text)
        confidence -= marks_penalty
        uncertainties.extend(marks_uncertainties)

        stripped_len = len(text.strip())
        if stripped_len < min_chars:
            confidence -= 0.3
            uncertainties.append(
                Uncertainty(
                    code="short_question_text",
                    message=f"question text is only {stripped_len} characters",
                    severity=Severity.WARNING,
                )
            )
        if _INSTRUCTION_RE.search(text):
            confidence -= 0.25
            uncertainties.append(
                Uncertainty(
                    code="possible_instruction",
                    message="text looks like paper instructions rather than a question",
                    severity=Severity.WARNING,
                )
            )

        confidence = max(0.05, min(0.99, round(confidence, 3)))
        question = ExtractedQuestion(
            text=text,
            question_number=str(candidate.number),
            number_sort=candidate.number,
            section=candidate.section,
            marks=marks,
            marks_source=marks_source,
            page_number=start_line.page_number,
            source_locator=locator,
            confidence=confidence,
            uncertainties=uncertainties,
            metadata={
                "line_range": [first_line, last_line],
                "page_end": end_line.page_number,
                "marker": candidate.marker,
            },
        )
        questions.append(question)

    document_uncertainties: list[Uncertainty] = []
    if not questions:
        document_uncertainties.append(
            Uncertainty(
                code="no_questions_detected",
                message=(f"{len(accepted)} markers were found but none had usable question text"),
                severity=Severity.WARNING,
            )
        )
    if conflicts:
        document_uncertainties.append(
            Uncertainty(
                code="numbering_conflict",
                message=(
                    f"{len(conflicts)} marker(s) were rejected because their number did "
                    "not advance (possible duplicate numbering)"
                ),
                severity=Severity.WARNING,
                detail={"conflicts": conflicts[:10]},
            )
        )
    if numbering_gaps:
        document_uncertainties.append(
            Uncertainty(
                code="numbering_gap",
                message=f"{len(numbering_gaps)} numbering gap(s) detected",
                severity=Severity.WARNING,
                detail={"gaps": [list(g) for g in numbering_gaps[:10]]},
            )
        )
    if empty_bodies:
        document_uncertainties.append(
            Uncertainty(
                code="empty_question_body",
                message=f"{empty_bodies} marker(s) had no body text and were not stored",
                severity=Severity.WARNING,
            )
        )
    if truncated:
        document_uncertainties.append(
            Uncertainty(
                code="question_limit_exceeded",
                message=f"segmentation stopped at the {max_questions}-question limit",
                severity=Severity.WARNING,
            )
        )
    if dropped_instructions:
        document_uncertainties.append(
            Uncertainty(
                code="instruction_markers_dropped",
                message=(
                    f"{len(dropped_instructions)} numbered instruction marker(s) were "
                    "recognised as paper instructions and excluded"
                ),
                severity=Severity.INFO,
                detail={"markers": dropped_instructions[:10]},
            )
        )

    low_confidence = [q for q in questions if q.confidence < min_confidence]
    # Informational notes do not degrade a document's segmentation status.
    has_warnings = any(
        uncertainty.severity is not Severity.INFO for uncertainty in document_uncertainties
    )
    if not questions:
        status = SegmentationStatus.UNSEGMENTED
    elif has_warnings or low_confidence:
        status = SegmentationStatus.PARTIAL
    else:
        status = SegmentationStatus.SEGMENTED

    document_confidence = (
        round(sum(q.confidence for q in questions) / len(questions), 3) if questions else 0.0
    )

    return QuestionSegmentation(
        questions=questions,
        status=status,
        confidence=document_confidence,
        uncertainties=document_uncertainties,
        sections=sections,
        numbering_gaps=numbering_gaps,
        rejected_markers=rejected + len(conflicts),
    )


def segment_questions_text(
    text: str,
    *,
    min_chars: int = 8,
    max_questions: int = 2000,
    min_confidence: float = 0.6,
) -> QuestionSegmentation:
    """Convenience wrapper for plain text (one logical page)."""
    return segment_questions_pages(
        [ExtractedPage(page_number=1, text=text)],
        min_chars=min_chars,
        max_questions=max_questions,
        min_confidence=min_confidence,
    )


def extract_marks(text: str) -> tuple[float | None, str | None, list[Uncertainty], float]:
    """Find the marks value stated for a question.

    Returns ``(value, source, uncertainties, confidence_penalty)``. A missing
    value is ``None`` - never 0 - because the source did not state marks.
    """
    found: list[tuple[int, float, str]] = []
    for pattern, source in _MARKS_PATTERNS:
        for match in pattern.finditer(text):
            try:
                value = float(match.group("v"))
            except (TypeError, ValueError):
                continue
            if 0 < value <= 100:
                found.append((match.start(), value, source))

    if not found:
        return (
            None,
            None,
            [
                Uncertainty(
                    code="no_marks_detected",
                    message="no marks value stated for this question",
                    severity=Severity.INFO,
                )
            ],
            0.0,
        )

    found.sort(key=lambda item: item[0])
    _position, value, source = found[-1]
    distinct = sorted({item[1] for item in found})
    uncertainties: list[Uncertainty] = []
    penalty = 0.0
    if len(distinct) > 1:
        penalty = 0.1
        uncertainties.append(
            Uncertainty(
                code="multiple_marks_values",
                message=(
                    "several different marks values appear; the last one was used "
                    "(total marks may have been confused with per-part marks)"
                ),
                severity=Severity.WARNING,
                detail={"values": distinct, "used": value},
            )
        )
    if source == "bare_bracket":
        uncertainties.append(
            Uncertainty(
                code="marks_bare_bracket",
                message="marks inferred from a bare bracketed number at the end of the text",
                severity=Severity.INFO,
            )
        )
        penalty += 0.05
    return value, source, uncertainties, penalty
