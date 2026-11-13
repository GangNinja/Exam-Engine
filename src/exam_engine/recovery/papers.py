"""Evidence-bounded extraction of questions and shared passages from papers.

The parser is intentionally conservative.  It only creates a passage group when
the source contains an explicit passage/case-study heading and at least two
following numbered questions.  A question with no matched local record is kept
as source evidence but never used to invent a dataset record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

from exam_engine.ingest.normalize import fingerprint_text

_QUESTION_RE = re.compile(
    r"^\s*(?:Q(?:uestion)?\s*\.?\s*)?(\d{1,3})\s*[.)\]:-]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_QUESTION_MARKER_RE = re.compile(r"^\s*Q\.?\s*(\d{1,3})\s*\.?\s*$", re.IGNORECASE)
_PAGE_NOISE_RE = re.compile(
    r"^(?:page\s+\d+\s+of\s+\d+|organizing institute:|general aptitude\b)",
    re.IGNORECASE,
)
_OPTION_START_RE = re.compile(r"\([A-H]\)\s*", re.IGNORECASE)
_SUBPART_RE = re.compile(r"^\s*\(?([a-h])\)\s*[.)]?\s*(.+?)\s*$", re.IGNORECASE)
_PASSAGE_RE = re.compile(
    r"^\s*(?:read\s+the\s+following\s+passage|passage|comprehension|"
    r"case\s+study|data\s+interpretation|directions?)\b",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(
    r"^\s*(?:section|part)\s+[A-Z0-9]+\b|^\s*(?:general\s+aptitude|subject\s+section)\b",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"^(?:page|q\.?\s*no|question\s+no|answer\s+key|key|solutions?)\b", re.IGNORECASE
)


@dataclass
class PaperQuestion:
    number: str
    text: str
    page_hint: int | None = None
    passage: str | None = None
    confidence: float = 0.75
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaperPassage:
    passage_text: str
    children: list[PaperQuestion] = field(default_factory=list)
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)


def _lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in (text or "").splitlines()]


def _question_body(lines: Sequence[str], start: int) -> tuple[str, int]:
    parts: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if index > start and (
            _QUESTION_RE.match(line)
            or _QUESTION_MARKER_RE.match(line)
            or _PASSAGE_RE.match(line)
        ):
            break
        if index > start and _SECTION_RE.match(line):
            break
        if _PAGE_NOISE_RE.match(line):
            index += 1
            continue
        if line:
            parts.append(line)
        index += 1
    return " ".join(parts).strip(), index


def extract_paper_questions(text: str) -> list[PaperQuestion]:
    """Extract numbered questions with conservative boundaries."""

    lines = _lines(text)
    questions: list[PaperQuestion] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _QUESTION_RE.match(line)
        marker = _QUESTION_MARKER_RE.match(line)
        if (not match and not marker) or _NOISE_RE.match(line):
            index += 1
            continue
        number = str(int((match or marker).group(1)))
        body, next_index = _question_body(lines, index + (1 if marker else 0))
        body = body.strip()
        if marker:
            # GATE and several other boards put only "Q.12" on its own line.
            # Stop the question at the first option so matching uses the stem,
            # not a concatenation of all options and diagrams.
            option = _OPTION_START_RE.search(body)
            if option and option.start() >= 40:
                body = body[: option.start()].strip()
        if len(body.split()) < 3:
            index = max(index + 1, next_index)
            continue
        key = fingerprint_text(body)
        if key and key not in seen:
            seen.add(key)
            questions.append(
                PaperQuestion(
                    number=number,
                    text=body[:5000],
                    confidence=0.82,
                    evidence={
                        "method": "question_marker_line" if marker else "numbered_question_line",
                        "source_line": index + 1,
                    },
                )
            )
        index = max(index + 1, next_index)
    return questions


def extract_passage_groups(text: str) -> list[PaperPassage]:
    """Extract explicit shared-context groups from a paper's text."""

    lines = _lines(text)
    groups: list[PaperPassage] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _PASSAGE_RE.match(line) or _NOISE_RE.match(line):
            index += 1
            continue
        passage_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and not _QUESTION_RE.match(lines[cursor]):
            if _PASSAGE_RE.match(lines[cursor]):
                break
            if lines[cursor]:
                passage_lines.append(lines[cursor])
            cursor += 1
        passage_text = " ".join(passage_lines).strip()
        children: list[PaperQuestion] = []
        while cursor < len(lines):
            line = lines[cursor]
            if _PASSAGE_RE.match(line) or _SECTION_RE.match(line):
                break
            match = _QUESTION_RE.match(line)
            if match:
                body, next_cursor = _question_body(lines, cursor)
                if len(body.split()) >= 3:
                    children.append(
                        PaperQuestion(
                            number=str(int(match.group(1))),
                            text=body[:5000],
                            passage=passage_text[:4000],
                            confidence=0.8,
                            evidence={
                                "method": "explicit_passage_heading",
                                "source_line": cursor + 1,
                            },
                        )
                    )
                cursor = max(cursor + 1, next_cursor)
                continue
            cursor += 1
        if len(passage_text.split()) >= 20 and len(children) >= 2:
            groups.append(
                PaperPassage(
                    passage_text=passage_text[:4000],
                    children=children,
                    confidence=0.84,
                    evidence={
                        "method": "explicit_passage_heading",
                        "child_count": len(children),
                        "source_line": index + 1,
                    },
                )
            )
        index = max(index + 1, cursor)
    return groups


def _normalise(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def match_question_text(
    source_text: str,
    record_text: str,
    *,
    threshold: float = 0.93,
) -> float:
    """Return a bounded similarity for source-to-record matching."""

    left = _normalise(source_text)
    right = _normalise(record_text)
    if not left or not right:
        return 0.0
    if left == right or fingerprint_text(left) == fingerprint_text(right):
        return 1.0
    if len(left) < 25 or len(right) < 25:
        return 0.0
    # A local question is often a clean substring of a PDF question stem that
    # also contains instructions or the next diagram.  SequenceMatcher alone
    # penalises that extra source context, so use token containment as an
    # additional, conservative signal.
    left_tokens = set(re.findall(r"[a-z0-9]+", left))
    right_tokens = set(re.findall(r"[a-z0-9]+", right))
    if left_tokens and right_tokens:
        # Compare against the shorter side.  The source stem may contain all of
        # the record's tokens plus options; dividing by the longer source would
        # wrongly reject a complete local question.
        coverage = len(left_tokens & right_tokens) / min(
            len(left_tokens), len(right_tokens)
        )
        if coverage >= threshold and min(len(right_tokens), len(left_tokens)) >= 5:
            return round(coverage, 4)
    matcher = SequenceMatcher(None, right, left)
    if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
        return 0.0
    score = matcher.ratio()
    return score if score >= threshold else 0.0


def match_paper_questions(
    records: Iterable[Any],
    questions: Iterable[PaperQuestion],
    *,
    threshold: float = 0.93,
    max_per_question: int = 3,
) -> dict[str, dict[str, Any]]:
    """Match paper questions to local records without changing either side."""

    record_rows = [
        (
            getattr(record, "record_id", ""),
            str((getattr(record, "repaired", {}) or {}).get("question_text") or ""),
            str(
                (getattr(record, "original", {}) or {}).get(
                    "original_question_number"
                )
                or ""
            ),
        )
        for record in records
        if getattr(record, "record_id", "")
    ]
    matches: dict[str, dict[str, Any]] = {}
    for question in questions:
        scored: list[tuple[float, str, bool]] = []
        for record_id, text, local_number in record_rows:
            score = match_question_text(question.text, text, threshold=threshold)
            if not score:
                continue
            # Question-number agreement is supporting evidence, never the only
            # evidence.  A conflicting number is only tolerated for an exact
            # text fingerprint (for example a re-ordered practice paper).
            number_agrees = True
            if local_number.strip().isdigit() and str(question.number).isdigit():
                number_agrees = int(local_number) == int(question.number)
                if not number_agrees and score < 0.995:
                    continue
            scored.append((score, record_id, number_agrees))
        scored.sort(reverse=True)
        for score, record_id, number_agrees in scored[:max_per_question]:
            current = matches.get(record_id)
            payload = {
                "question_number": question.number,
                "similarity": round(score, 4),
                "question_number_agrees": number_agrees,
                "question_text": question.text[:1200],
                "passage": question.passage,
                "confidence": round(question.confidence * score, 3),
                "source_url": question.evidence.get("source_url"),
                "source_sha256": question.evidence.get("source_sha256"),
                "source_kind": question.evidence.get("source_kind"),
                "source_identity": question.evidence.get("source_identity"),
                "source_tier": question.evidence.get("source_tier"),
                "source_provider": question.evidence.get("source_provider"),
            }
            if current is None or payload["similarity"] > current["similarity"]:
                matches[record_id] = payload
    return matches


def match_passage_children(
    groups: Iterable[PaperPassage],
    records: Iterable[Any],
    *,
    threshold: float = 0.93,
) -> list[dict[str, Any]]:
    """Return passage groups whose child questions match at least two records."""

    record_list = list(records)
    result: list[dict[str, Any]] = []
    for group in groups:
        matches = match_paper_questions(record_list, group.children, threshold=threshold)
        if len(matches) < 2:
            continue
        result.append(
            {
                "passage_text": group.passage_text,
                "child_numbers": [child.number for child in group.children],
                "record_ids": sorted(matches),
                "matches": matches,
                "confidence": group.confidence,
                "evidence": group.evidence,
            }
        )
    return result


__all__ = [
    "PaperPassage",
    "PaperQuestion",
    "extract_paper_questions",
    "extract_passage_groups",
    "match_paper_questions",
    "match_passage_children",
    "match_question_text",
]
