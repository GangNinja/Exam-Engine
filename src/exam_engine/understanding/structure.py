"""Question structure detection on top of the Phase 1 extraction.

Phase 1 already finds numbered question boundaries, sections, marks and source
locations. This module does not re-detect documents: it reads those records and
adds the details Phase 3 needs - sub-question spans, explicit references to
diagrams/code/tables/equations/case studies, and honest uncertainty when the
upstream extraction itself was weak.

Original question text is preserved; every span indexes the stored question
text exactly, so evidence can be verified without re-parsing the source file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from exam_engine.ingest.normalize import count_words
from exam_engine.models.enums import ReferenceKind, Severity
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.understanding import AssetReference, Subpart

# Sub-markers only count at a line start, after a newline or after a semicolon:
# "(a)", "(iv)", "(2)" or "a." with following text. Inline parenthetical years or
# citations therefore cannot become sub-questions.
_SUBPART_RE = re.compile(
    r"(?:^|(?<=\n)|(?<=;)|(?<=:)|(?<=[.!?]))\s*"
    r"(?:\((?P<paren>[a-zA-Z]|[ivxIVX]{1,4}|\d{1,2})\)"
    r"|(?P<close>[a-zA-Z]|[ivxIVX]{1,4}|\d{1,2})\)"
    r"|(?P<dot>[a-zA-Z])\.)\s+",
    re.MULTILINE,
)
_SUBPART_MARKS_RE = re.compile(
    r"(?:\(|\[)?\s*(?P<value>\d{1,3}(?:\.\d+)?)\s*marks?\s*(?:\)|\])?",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_TERMINAL_PUNCTUATION_RE = re.compile(r"[.!?][\"')\]]?\s*$")
# Phase 1 can keep a trailing marks annotation in the question text; it is not
# evidence that the question itself was truncated.
_TRAILING_MARKS_RE = re.compile(
    r"\s*(?:[\[(]\s*\d{1,3}(?:\.\d+)?\s*marks?\s*[\])]|\d{1,3}(?:\.\d+)?\s*marks?)\s*$",
    re.IGNORECASE,
)

_REFERENCE_PATTERNS: tuple[tuple[ReferenceKind, re.Pattern[str], float], ...] = (
    (
        ReferenceKind.CASE_STUDY,
        re.compile(
            r"\bcase stud(?:y|ies)\b|\bread the (?:following )?(?:case|passage|scenario)\b|"
            r"\bpassage (?:below|above)\b",
            re.IGNORECASE,
        ),
        0.85,
    ),
    (
        ReferenceKind.CODE,
        re.compile(
            r"\b(?:code|program|snippet|pseudocode) (?:below|above|shown|given)\b|"
            r"\bfollowing (?:code|program|snippet)\b|\blisting\s*\(?\d{1,2}\)?\b|```",
            re.IGNORECASE,
        ),
        0.8,
    ),
    (
        ReferenceKind.DIAGRAM,
        re.compile(
            r"\b(?:diagram|figure|flow ?chart|circuit|graph|chart) "
            r"(?:below|above|shown|given)\b|"
            r"\bshown in (?:the )?(?:diagram|figure|graph|chart)\b|"
            r"\brefer to (?:the )?(?:diagram|figure|graph|chart)\b|"
            r"\b(?:diagram|figure|flow ?chart|graph|chart)\s*\(?\d{1,2}\)?\b",
            re.IGNORECASE,
        ),
        0.8,
    ),
    (
        ReferenceKind.TABLE,
        re.compile(
            r"\b(?:table|dataset) (?:below|above|shown|given)\b|"
            r"\bfollowing table\b|\brefer to (?:the )?table\b|"
            r"\btable\s*\(?\d{1,2}\)?\b",
            re.IGNORECASE,
        ),
        0.8,
    ),
    (
        ReferenceKind.EQUATION,
        re.compile(
            r"\b(?:equation|formula|expression) (?:below|above|shown|given)\b|"
            r"\bfollowing (?:equation|formula)\b|\bequation\s*\(?\d",
            re.IGNORECASE,
        ),
        0.75,
    ),
)


@dataclass(frozen=True)
class QuestionStructure:
    """Detected structure for one stored question."""

    word_count: int = 0
    sentence_count: int = 0
    subparts: list[Subpart] = field(default_factory=list)
    references: list[AssetReference] = field(default_factory=list)
    confidence: float = 0.0
    uncertainties: list[Uncertainty] = field(default_factory=list)

    @property
    def subquestion_count(self) -> int:
        return len(self.subparts)


def _extract_subparts(text: str) -> tuple[list[Subpart], list[Uncertainty]]:
    markers = list(_SUBPART_RE.finditer(text))
    if not markers:
        return [], []
    parts: list[Subpart] = []
    uncertainties: list[Uncertainty] = []
    for index, marker in enumerate(markers):
        label = marker.group("paren") or marker.group("close") or marker.group("dot") or ""
        body_start = marker.end()
        body_end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        raw = text[body_start:body_end]
        if not raw.strip():
            continue
        leading = len(raw) - len(raw.lstrip())
        body = raw.strip()
        start = body_start + leading
        end = start + len(body)
        marks_match = _SUBPART_MARKS_RE.search(body)
        marks = float(marks_match.group("value")) if marks_match else None
        if marks is not None and marks > 100:
            # A per-part mark above the question-level cap is almost certainly a
            # total or an extraction artefact. Keep the part, do not invent a
            # smaller mark, and record why the value was discarded.
            uncertainties.append(
                Uncertainty(
                    code="implausible_subpart_marks",
                    message=(
                        f"sub-question '{label}' states {marks:g} marks, above the "
                        "per-question cap; the mark value was not stored"
                    ),
                    severity=Severity.WARNING,
                    detail={"label": label, "marks": marks},
                )
            )
            marks = None
        parts.append(Subpart(label=label, text=body, start=start, end=end, marks=marks))
    # A lone numeric marker is more likely a question number copied into the
    # text than a sub-question; alphabetic/roman markers are safe on their own.
    if len(parts) == 1 and parts[0].label.isdigit():
        return [], []
    return parts, uncertainties


def _merge_upstream_uncertainties(
    question: QuestionRecord, uncertainties: list[Uncertainty]
) -> list[Uncertainty]:
    """Carry Phase 1 doubts (numbering gaps, ambiguous marks) into Phase 3.

    Phase 3 does not re-detect question boundaries, so an upstream boundary
    doubt must stay visible in the analysis instead of being treated as a clean
    extraction just because the text itself looks plausible.
    """
    seen = {item.code for item in uncertainties}
    for item in question.uncertainties:
        if item.code in seen:
            continue
        uncertainties.append(item)
        seen.add(item.code)
    return uncertainties


def _extract_references(text: str) -> list[AssetReference]:
    candidates: list[tuple[int, int, ReferenceKind, str, float]] = []
    for kind, pattern, confidence in _REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0).strip()
            if value:
                candidates.append((match.start(), match.end(), kind, value, confidence))
    # A specific phrase such as "the code below" must not also appear as a
    # generic diagram/table match. Keep the earliest, then longest, span.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    kept: list[AssetReference] = []
    for start, end, kind, value, confidence in candidates:
        if any(not (end <= ref.start or start >= ref.end) for ref in kept):
            continue
        kept.append(
            AssetReference(kind=kind, text=value, start=start, end=end, confidence=confidence)
        )
    return kept


def detect_structure(question: QuestionRecord) -> QuestionStructure:
    """Detect structure without inventing text, marks or references."""
    text = question.text
    if not text.strip():
        return QuestionStructure(
            confidence=0.0,
            uncertainties=_merge_upstream_uncertainties(
                question,
                [
                    Uncertainty(
                        code="empty_text",
                        message="stored question text is empty; no structure was inferred",
                        severity=Severity.WARNING,
                        detail={"question_id": question.question_id},
                    )
                ],
            ),
        )

    uncertainties: list[Uncertainty] = []
    confidence = question.confidence
    if question.needs_review or question.confidence < 0.6:
        uncertainties.append(
            Uncertainty(
                code="low_extraction_confidence",
                message=(
                    "this question came from a low-confidence extraction; "
                    "its boundaries and marks need review"
                ),
                severity=Severity.WARNING,
                detail={
                    "question_id": question.question_id,
                    "upstream_confidence": question.confidence,
                },
            )
        )
        confidence = min(confidence, 0.55)
    elif any(item.code == "low_extraction_confidence" for item in question.uncertainties):
        uncertainties.append(
            Uncertainty(
                code="low_extraction_confidence",
                message="Phase 1 flagged this question's extraction for review",
                severity=Severity.WARNING,
                detail={"question_id": question.question_id},
            )
        )

    words = count_words(text)
    sentences = len([part for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()])
    if not _TERMINAL_PUNCTUATION_RE.search(_TRAILING_MARKS_RE.sub("", text)) and sentences > 0:
        uncertainties.append(
            Uncertainty(
                code="missing_terminal_punctuation",
                message=(
                    "question text does not end with punctuation; it may have been "
                    "truncated or merged with the next question"
                ),
                severity=Severity.INFO,
                detail={"question_id": question.question_id},
            )
        )

    subparts, subpart_uncertainties = _extract_subparts(text)
    uncertainties.extend(subpart_uncertainties)
    return QuestionStructure(
        word_count=words,
        sentence_count=max(1, sentences),
        subparts=subparts,
        references=_extract_references(text),
        confidence=max(0.0, min(1.0, round(confidence, 4))),
        uncertainties=_merge_upstream_uncertainties(question, uncertainties),
    )


__all__ = ["QuestionStructure", "detect_structure"]
