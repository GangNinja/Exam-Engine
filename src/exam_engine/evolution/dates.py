"""Explicit source-date resolution for the Phase 9 evolution tracker.

The tracker must never confuse when a document *says* something with when it was
uploaded. This module resolves one date per document from the most relevant
available field and records the field, the date type and any conflict. Ingestion
time is not a candidate. A bare academic year is not converted into an exact
date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from exam_engine.analytics.metadata import ExamPeriod
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import DocumentKind, EvolutionDateType

_DATE_TYPE_ORDER = {
    EvolutionDateType.SYLLABUS_EFFECTIVE: 0,
    EvolutionDateType.EDITION: 1,
    EvolutionDateType.PUBLICATION: 2,
    EvolutionDateType.EXAMINATION: 3,
    EvolutionDateType.UNKNOWN: 4,
}

_EFFECTIVE_KEYS = (
    "effective_date",
    "effective_from",
    "syllabus_effective_date",
    "valid_from",
    "implemented_from",
)
_EDITION_KEYS = ("edition_date", "revision_date", "edition_published")
_PUBLICATION_KEYS = ("publication_date", "published_date", "published_on")


@dataclass(frozen=True)
class DateCandidate:
    """One parsed source date, kept even when it is not the selected date."""

    source: str
    value: date
    date_type: EvolutionDateType


@dataclass(frozen=True)
class DocumentDate:
    """The selected date, every candidate, and any unresolved date problems."""

    value: date | None
    date_type: EvolutionDateType
    source: str
    confident: bool = True
    conflicts: tuple[str, ...] = field(default_factory=tuple)
    invalid: tuple[str, ...] = field(default_factory=tuple)
    candidates: tuple[DateCandidate, ...] = field(default_factory=tuple)

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def sort_key(self) -> tuple[int, str, int, str]:
        return (
            0 if self.value is not None else 1,
            self.value.isoformat() if self.value is not None else "",
            _DATE_TYPE_ORDER[self.date_type],
            self.source,
        )


def parse_date_value(value: object) -> date | None:
    """Parse an ISO date/datetime; partial or free-text values stay unparsed."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = " ".join(str(value).split())
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _metadata_dates(document: DocumentRecord, keys: tuple[str, ...]) -> list[tuple[str, date]]:
    found: list[tuple[str, date]] = []
    for key in keys:
        if key not in document.metadata:
            continue
        parsed = parse_date_value(document.metadata[key])
        if parsed is not None:
            found.append((f"metadata.{key}", parsed))
    return found


def _invalid_metadata(document: DocumentRecord, keys: tuple[str, ...]) -> list[str]:
    invalid: list[str] = []
    for key in keys:
        if key not in document.metadata:
            continue
        value = document.metadata[key]
        if value is None or str(value).strip() == "":
            continue
        if parse_date_value(value) is None:
            invalid.append(f"metadata.{key}={value!r}")
    return invalid


def _group_conflicts(group: list[tuple[str, date]]) -> list[str]:
    """Report disagreeing values inside one date type, never across types."""
    if len({value for _, value in group}) <= 1:
        return []
    return [f"{source}={value.isoformat()}" for source, value in group]


def resolve_document_date(
    document: DocumentRecord, *, period: ExamPeriod | None = None
) -> DocumentDate:
    """Resolve the most relevant source date for one document.

    Precedence is by source kind, not by whichever field happens to exist:

    * a syllabus uses its effective date, then publication date;
    * a non-syllabus, non-paper document with edition metadata uses its edition date,
      then publication date (the schema has no dedicated textbook kind, so an
      edition-bearing document is represented as ``notes``/``other``);
    * a question paper uses the resolved examination date, then publication date;
    * every other document uses its publication date.

    The validated ``document.publication_date`` column outranks a free-form
    ``metadata.publication_date``. Disagreeing values inside the same date type
    (effective, edition or publication) are reported rather than silently resolved,
    and a session such as ``2021-05`` is *not* turned into a day.
    """
    invalid = tuple(
        _invalid_metadata(document, _EFFECTIVE_KEYS + _EDITION_KEYS + _PUBLICATION_KEYS)
    )
    effective = _metadata_dates(document, _EFFECTIVE_KEYS)
    edition = _metadata_dates(document, _EDITION_KEYS)
    # The validated column is the primary record; free-form metadata only fills in
    # for it. Any disagreement is reported as a conflict, never silently resolved.
    publication: list[tuple[str, date]] = []
    if document.publication_date is not None:
        publication.append(("document.publication_date", document.publication_date))
    publication.extend(_metadata_dates(document, _PUBLICATION_KEYS))

    # A syllabus effective date and its publication date are different date types, so
    # only disagreements *within* a type are conflicts.
    conflicts = [
        *_group_conflicts(effective),
        *_group_conflicts(edition),
        *_group_conflicts(publication),
    ]

    kind = document.kind
    exam: list[tuple[str, date]] = []
    if kind is DocumentKind.QUESTION_PAPER and period is not None and period.exact_date:
        exam.append(("exam_period.exact_date", period.exact_date))

    groups = (
        (effective, EvolutionDateType.SYLLABUS_EFFECTIVE),
        (edition, EvolutionDateType.EDITION),
        (exam, EvolutionDateType.EXAMINATION),
        (publication, EvolutionDateType.PUBLICATION),
    )
    candidates = tuple(
        DateCandidate(source, value, date_type)
        for group, date_type in groups
        for source, value in group
    )

    chosen_source: str | None = None
    chosen: date | None = None
    date_type = EvolutionDateType.UNKNOWN
    if kind is DocumentKind.SYLLABUS and effective:
        chosen_source, chosen = effective[0]
        date_type = EvolutionDateType.SYLLABUS_EFFECTIVE
    elif kind not in {DocumentKind.SYLLABUS, DocumentKind.QUESTION_PAPER} and edition:
        chosen_source, chosen = edition[0]
        date_type = EvolutionDateType.EDITION
    elif exam:
        chosen_source, chosen = exam[0]
        date_type = EvolutionDateType.EXAMINATION
    elif publication:
        chosen_source, chosen = publication[0]
        date_type = EvolutionDateType.PUBLICATION

    if chosen is None or chosen_source is None:
        return DocumentDate(
            value=None,
            date_type=EvolutionDateType.UNKNOWN,
            source="none",
            confident=False,
            conflicts=tuple(conflicts),
            invalid=invalid,
            candidates=candidates,
        )

    return DocumentDate(
        value=chosen,
        date_type=date_type,
        source=chosen_source,
        confident=not conflicts,
        conflicts=tuple(conflicts),
        invalid=invalid,
        candidates=candidates,
    )


__all__ = ["DateCandidate", "DocumentDate", "parse_date_value", "resolve_document_date"]
