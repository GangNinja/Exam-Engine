"""Repository for Phase 10 generation batches and generated questions.

Generated questions are stored apart from the Phase 1 ``questions`` table, so
historical examination data can never be mutated by generation or review.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from exam_engine.models.enums import GenerationReviewStatus
from exam_engine.models.generation import (
    GeneratedQuestion,
    GenerationBatch,
    GenerationQuery,
)
from exam_engine.storage import generation_mappers as mappers
from exam_engine.storage.schema import GeneratedQuestionRow, GenerationBatchRow


class GenerationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- batches -----------------------------------------------------------

    def upsert_batch(self, batch: GenerationBatch) -> GenerationBatchRow:
        row = mappers.batch_to_row(batch)
        existing = self.session.get(GenerationBatchRow, row.batch_id)
        if existing is not None:
            row.created_at = existing.created_at
        self.session.merge(row)
        self.session.flush()
        return row

    def get_batch(self, batch_id: str) -> GenerationBatch | None:
        row = self.session.get(GenerationBatchRow, batch_id)
        if row is None:
            return None
        batch = mappers.row_to_batch(row)
        batch.questions = self.list_questions(
            GenerationQuery(course_id=batch.course_id, batch_id=batch_id, limit=1000)
        )
        return batch

    def list_batches(self, course_id: str, *, limit: int = 50) -> list[GenerationBatchRow]:
        return list(
            self.session.scalars(
                select(GenerationBatchRow)
                .where(GenerationBatchRow.course_id == course_id)
                .order_by(GenerationBatchRow.created_at.desc(), GenerationBatchRow.batch_id)
                .limit(limit)
            ).all()
        )

    # -- questions ---------------------------------------------------------

    def get_question(self, question_id: str) -> GeneratedQuestion | None:
        row = self.session.get(GeneratedQuestionRow, question_id)
        return mappers.row_to_question(row) if row is not None else None

    def save_question(
        self, question: GeneratedQuestion, *, allow_overwrite_approved: bool = False
    ) -> str:
        """Insert or update one generated question.

        Returns ``created``, ``updated``, ``approved_locked`` or ``unchanged``.
        A previously approved question is never overwritten without explicit
        authorization, and a byte-identical rewrite is reported as unchanged.
        """
        existing = self.session.get(GeneratedQuestionRow, question.question_id)
        if existing is None:
            self.session.merge(mappers.question_to_row(question))
            self.session.flush()
            return "created"
        if (
            existing.review_status == GenerationReviewStatus.APPROVED.value
            and not allow_overwrite_approved
        ):
            return "approved_locked"
        replacement = mappers.question_to_row(question)
        replacement.created_at = existing.created_at
        if (
            replacement.text == existing.text
            and replacement.review_status == existing.review_status
            and replacement.validation_json == existing.validation_json
            and replacement.answer_key == existing.answer_key
        ):
            return "unchanged"
        replacement.revision = max(existing.revision + 1, question.revision)
        self.session.merge(replacement)
        self.session.flush()
        return "updated"

    def list_questions(self, query: GenerationQuery) -> list[GeneratedQuestion]:
        statement = select(GeneratedQuestionRow).where(
            GeneratedQuestionRow.course_id == query.course_id
        )
        if query.syllabus_id is not None:
            statement = statement.where(GeneratedQuestionRow.syllabus_id == query.syllabus_id)
        if query.batch_id is not None:
            statement = statement.where(GeneratedQuestionRow.batch_id == query.batch_id)
        if query.format is not None:
            statement = statement.where(GeneratedQuestionRow.format == query.format.value)
        if query.review_status is not None:
            statement = statement.where(
                GeneratedQuestionRow.review_status == query.review_status.value
            )
        if query.validation_status is not None:
            statement = statement.where(
                GeneratedQuestionRow.validation_status == query.validation_status.value
            )
        statement = statement.order_by(
            GeneratedQuestionRow.batch_id,
            GeneratedQuestionRow.unit_id,
            GeneratedQuestionRow.question_id,
        ).limit(query.limit)
        rows = list(self.session.scalars(statement).all())
        questions = [mappers.row_to_question(row) for row in rows]
        if query.concept_id is not None:
            questions = [
                question for question in questions if query.concept_id in question.concept_ids
            ]
        return questions

    def count_by_review_status(self, course_id: str, syllabus_id: str) -> dict[str, int]:
        rows = self.session.execute(
            select(GeneratedQuestionRow.review_status, func.count())
            .where(
                GeneratedQuestionRow.course_id == course_id,
                GeneratedQuestionRow.syllabus_id == syllabus_id,
            )
            .group_by(GeneratedQuestionRow.review_status)
        ).all()
        return {str(status): int(count) for status, count in rows}

    def delete_batch(self, batch_id: str) -> int:
        """Remove a batch and its questions; approved questions are protected."""
        approved = self.session.scalar(
            select(func.count())
            .select_from(GeneratedQuestionRow)
            .where(
                GeneratedQuestionRow.batch_id == batch_id,
                GeneratedQuestionRow.review_status == GenerationReviewStatus.APPROVED.value,
            )
        )
        if approved:
            return 0
        rows = list(
            self.session.scalars(
                select(GeneratedQuestionRow).where(GeneratedQuestionRow.batch_id == batch_id)
            ).all()
        )
        for row in rows:
            self.session.delete(row)
        batch_row = self.session.get(GenerationBatchRow, batch_id)
        if batch_row is not None:
            self.session.delete(batch_row)
        self.session.flush()
        return len(rows)


__all__ = ["GenerationRepository"]
