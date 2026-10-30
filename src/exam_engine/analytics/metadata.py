"""Examination-period resolution for analytics.

Only metadata that describes the examination itself is used. ``ingested_at`` is
never an examination date, and ``publication_date`` is not silently treated as
one either: a paper can be published after the exam, and a syllabus can be
published before it. Supported explicit keys are:

* ``exam_session`` / ``examination_session`` (for example ``2024-05``)
* ``exam_date`` / ``examination_date`` (ISO date)
* ``exam_type`` / ``examination_type`` (for example ``midterm``)

When none is present, the document or course academic year is used as a coarse
period. If that is absent too, the document remains in the report but contributes
to "unknown period" counts and is excluded from trends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from exam_engine.models.course import CourseRecord
from exam_engine.models.document import DocumentRecord

_SESSION_RE = re.compile(r"(?P<year>\d{4})(?:[-_/ .]?(?P<month>\d{1,2}))?")
_YEAR_RE = re.compile(r"(?P<year>\d{4})")


@dataclass(frozen=True)
class ExamPeriod:
    """A document's examination period and the evidence used to derive it."""

    label: str
    source: str
    sort_key: str
    session: str | None
    exam_type: str | None
    exact_date: date | None = None
    known: bool = True
    metadata_warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.label}"

    @property
    def session_key(self) -> str | None:
        """Stable key for an explicitly labelled examination session.

        A known period is not necessarily a session: an academic year, or an
        examination date without an explicit session label, is a period that is
        useful for trends but has no session identity. Counting it as a session
        would make ``known_sessions`` indistinguishable from ``known_periods``
        and would wrongly mark two papers from the same coarse period as the
        same sitting.
        """
        if not self.known or self.session is None:
            return None
        base = f"{self.source}:{self.session.casefold()}"
        return f"{base}|type:{self.exam_type.casefold()}" if self.exam_type else base


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    cleaned = " ".join(str(value).split())
    return cleaned or None


def _first(metadata: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _parse_date(value: Any) -> tuple[date | None, str | None, str | None]:
    """Return (date, sort_key, warning). Partial months are not given a day."""
    if isinstance(value, datetime):
        return value.date(), value.date().isoformat(), None
    if isinstance(value, date):
        return value, value.isoformat(), None
    text = _text(value)
    if not text:
        return None, None, None
    try:
        parsed = date.fromisoformat(text)
        return parsed, parsed.isoformat(), None
    except ValueError:
        pass
    try:
        parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed_dt.date(), parsed_dt.date().isoformat(), None
    except ValueError:
        pass
    match = _SESSION_RE.fullmatch(text)
    if match:
        year = int(match.group("year"))
        month_raw = match.group("month")
        if month_raw is not None:
            month = int(month_raw)
            if 1 <= month <= 12:
                return None, f"{year:04d}-{month:02d}", None
        return None, f"{year:04d}", None
    return None, None, f"exam_date could not be parsed as ISO or YYYY-MM: {text!r}"


def _session_sort_key(session: str) -> str:
    match = _SESSION_RE.search(session)
    if match:
        year = int(match.group("year"))
        month_raw = match.group("month")
        if month_raw is not None:
            month = int(month_raw)
            if 1 <= month <= 12:
                return f"{year:04d}-{month:02d}"
        return f"{year:04d}"
    return session.casefold()


def _academic_year_sort_key(value: str) -> str:
    match = _YEAR_RE.search(value)
    return f"{int(match.group('year')):04d}-AY" if match else value.casefold()


def resolve_exam_period(document: DocumentRecord, course: CourseRecord) -> ExamPeriod:
    """Resolve explicit exam metadata, then academic year, never upload time."""
    metadata = document.metadata if isinstance(document.metadata, dict) else {}
    warnings: list[str] = []

    raw_date = _first(metadata, "exam_date", "examination_date")
    parsed_date, date_sort, date_warning = _parse_date(raw_date)
    if date_warning:
        warnings.append(date_warning)

    session = _text(_first(metadata, "exam_session", "examination_session"))
    exam_type = _text(_first(metadata, "exam_type", "examination_type"))
    if exam_type is not None:
        exam_type = exam_type.casefold()

    if session:
        label = session
        source = "exam_session"
        sort_key = date_sort or _session_sort_key(session)
    elif raw_date is not None and date_sort is not None:
        label = parsed_date.isoformat() if parsed_date else _text(raw_date) or "unknown"
        source = "exam_date"
        sort_key = date_sort
    else:
        academic_year = document.academic_year or course.academic_year
        if academic_year:
            label = academic_year
            source = "academic_year"
            sort_key = _academic_year_sort_key(academic_year)
        else:
            return ExamPeriod(
                label="unknown",
                source="unknown",
                sort_key="",
                session=None,
                exam_type=exam_type,
                known=False,
                metadata_warnings=tuple(warnings),
            )

    return ExamPeriod(
        label=label,
        source=source,
        sort_key=sort_key,
        session=session,
        exam_type=exam_type,
        exact_date=parsed_date,
        known=True,
        metadata_warnings=tuple(warnings),
    )


__all__ = ["ExamPeriod", "resolve_exam_period"]
