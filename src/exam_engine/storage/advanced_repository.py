"""The SQL surface for Phase 11 responses and misconception records.

The repository never logs or returns a student identifier beyond the anonymised
``response_id``. Analysis upserts preserve a human review decision: recomputing a
pattern must not silently re-open an approved or rejected record.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from exam_engine.models.advanced import (
    MisconceptionQuery,
    MisconceptionRecord,
    ResponseRecord,
)
from exam_engine.models.course import utcnow
from exam_engine.models.enums import GenerationReviewStatus, MisconceptionStatus
from exam_engine.models.generation import ReviewEvent
from exam_engine.storage import advanced_mappers
from exam_engine.storage.schema import MisconceptionRow, StudentResponseRow


class ResponseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_many(self, records: Iterable[ResponseRecord]) -> tuple[int, int]:
        """Insert new responses; return ``(inserted, already_present)``."""

        inserted = 0
        skipped = 0
        for record in records:
            if self.session.get(StudentResponseRow, record.response_id) is not None:
                skipped += 1
                continue
            self.session.merge(advanced_mappers.response_to_row(record))
            inserted += 1
        self.session.flush()
        return inserted, skipped

    def get(self, response_id: str) -> ResponseRecord | None:
        row = self.session.get(StudentResponseRow, response_id)
        return advanced_mappers.row_to_response(row) if row is not None else None

    def list(
        self,
        course_id: str,
        *,
        syllabus_id: str | None = None,
        question_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[ResponseRecord]:
        stmt = select(StudentResponseRow).where(StudentResponseRow.course_id == course_id)
        if syllabus_id is not None:
            stmt = stmt.where(StudentResponseRow.syllabus_id == syllabus_id)
        if question_ids:
            stmt = stmt.where(StudentResponseRow.question_id.in_(question_ids))
        stmt = stmt.order_by(StudentResponseRow.question_id, StudentResponseRow.response_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [advanced_mappers.row_to_response(row) for row in self.session.scalars(stmt).all()]

    def count(self, course_id: str, *, syllabus_id: str | None = None) -> int:
        stmt = select(StudentResponseRow.response_id).where(
            StudentResponseRow.course_id == course_id
        )
        if syllabus_id is not None:
            stmt = stmt.where(StudentResponseRow.syllabus_id == syllabus_id)
        return len(self.session.scalars(stmt).all())

    def delete_for_course(self, course_id: str, *, syllabus_id: str | None = None) -> int:
        """Remove imported responses, e.g. to re-import a corrected export."""

        stmt = select(StudentResponseRow).where(StudentResponseRow.course_id == course_id)
        if syllabus_id is not None:
            stmt = stmt.where(StudentResponseRow.syllabus_id == syllabus_id)
        rows = list(self.session.scalars(stmt).all())
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)


class MisconceptionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_analysis(self, records: Iterable[MisconceptionRecord]) -> list[MisconceptionRecord]:
        """Store recomputed records without overwriting a human decision."""

        stored: list[MisconceptionRecord] = []
        for record in records:
            existing = self.session.get(MisconceptionRow, record.misconception_id)
            if existing is not None:
                current = advanced_mappers.row_to_misconception(existing)
                if current.review_status is not MisconceptionStatus.CANDIDATE:
                    # Keep the reviewer's decision and history; refresh the evidence.
                    record.review_status = current.review_status
                    record.review_history = current.review_history
                record.created_at = current.created_at
            row = advanced_mappers.misconception_to_row(record)
            self.session.merge(row)
            stored.append(record)
        self.session.flush()
        return stored

    def get(self, misconception_id: str) -> MisconceptionRecord | None:
        row = self.session.get(MisconceptionRow, misconception_id)
        return advanced_mappers.row_to_misconception(row) if row is not None else None

    def list(self, query: MisconceptionQuery) -> list[MisconceptionRecord]:
        stmt = select(MisconceptionRow).where(MisconceptionRow.course_id == query.course_id)
        if query.syllabus_id:
            stmt = stmt.where(MisconceptionRow.syllabus_id == query.syllabus_id)
        if query.kind is not None:
            stmt = stmt.where(MisconceptionRow.kind == query.kind.value)
        if query.status is not None:
            stmt = stmt.where(MisconceptionRow.review_status == query.status.value)
        if query.concept_id:
            stmt = stmt.where(MisconceptionRow.concept_ids_json.contains(query.concept_id))
        if query.min_support:
            stmt = stmt.where(MisconceptionRow.support_count >= query.min_support)
        stmt = stmt.order_by(
            MisconceptionRow.support_count.desc(),
            MisconceptionRow.confidence.desc(),
            MisconceptionRow.misconception_id,
        ).limit(query.limit)
        return [
            advanced_mappers.row_to_misconception(row) for row in self.session.scalars(stmt).all()
        ]

    def set_review_status(
        self,
        misconception_id: str,
        status: MisconceptionStatus,
        *,
        note: str | None = None,
        reviewer: str = "local-user",
    ) -> MisconceptionRecord:
        row = self.session.get(MisconceptionRow, misconception_id)
        if row is None:
            raise KeyError(misconception_id)
        record = advanced_mappers.row_to_misconception(row)
        if record.review_status is status:
            return record
        record.review_status = status
        record.review_history.append(
            ReviewEvent(status=_question_status(status), note=note, reviewer=reviewer)
        )
        record.updated_at = utcnow()
        self.session.merge(advanced_mappers.misconception_to_row(record))
        self.session.flush()
        return record

    def summary(self, course_id: str, *, syllabus_id: str | None = None) -> dict[str, int]:
        stmt = select(MisconceptionRow).where(MisconceptionRow.course_id == course_id)
        if syllabus_id:
            stmt = stmt.where(MisconceptionRow.syllabus_id == syllabus_id)
        counts: dict[str, int] = {}
        for row in self.session.scalars(stmt).all():
            counts[row.review_status] = counts.get(row.review_status, 0) + 1
        return counts


def _question_status(status: MisconceptionStatus) -> GenerationReviewStatus:
    """Map onto Phase 10's review event enum for a consistent history shape."""

    if status is MisconceptionStatus.CANDIDATE:
        return GenerationReviewStatus.PENDING_REVIEW
    return GenerationReviewStatus(status.value)


__all__ = ["MisconceptionRepository", "ResponseRepository"]
