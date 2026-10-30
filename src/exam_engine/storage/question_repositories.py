"""Repositories for Phase 3 question-understanding data.

All writes are scoped by course or question and are replace-on-run: derived rows
never survive an analysis that no longer produces them. Reads stay explicit so
the CLI can show why a label exists, not just the label.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from exam_engine.models.enums import MappingStatus, SimilarityKind
from exam_engine.models.understanding import (
    BloomLabel,
    DifficultyAssessment,
    QuestionAnalysis,
    SimilarityMatch,
    TypeLabel,
    UnderstandingRunReport,
    UnitMatch,
)
from exam_engine.storage import understanding_mappers as mappers
from exam_engine.storage.schema import (
    QuestionAnalysisRow,
    QuestionBloomRow,
    QuestionDifficultyRow,
    QuestionSimilarityRow,
    QuestionTypeRow,
    QuestionUnitMatchRow,
    UnderstandingRunRow,
)


class QuestionUnderstandingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- writes ------------------------------------------------------------

    def clear_for_course(self, course_id: str) -> None:
        for row in (
            QuestionSimilarityRow,
            QuestionDifficultyRow,
            QuestionBloomRow,
            QuestionTypeRow,
            QuestionUnitMatchRow,
            QuestionAnalysisRow,
        ):
            self.session.execute(delete(row).where(row.course_id == course_id))
        self.session.flush()

    def replace_for_question(
        self,
        *,
        analysis: QuestionAnalysis,
        matches: list[UnitMatch],
        types: list[TypeLabel],
        bloom: list[BloomLabel],
        difficulty: DifficultyAssessment,
    ) -> None:
        question_id = analysis.question_id
        for row in (
            QuestionUnitMatchRow,
            QuestionTypeRow,
            QuestionBloomRow,
            QuestionDifficultyRow,
            QuestionAnalysisRow,
        ):
            self.session.execute(delete(row).where(row.question_id == question_id))
        self.session.flush()
        self.session.add(mappers.analysis_to_row(analysis))
        for match in matches:
            self.session.add(mappers.unit_match_to_row(match))
        for type_label in types:
            self.session.add(mappers.type_to_row(type_label))
        for bloom_label in bloom:
            self.session.add(mappers.bloom_to_row(bloom_label))
        self.session.add(mappers.difficulty_to_row(difficulty))
        self.session.flush()

    def replace_similarities(self, course_id: str, matches: list[SimilarityMatch]) -> None:
        self.session.execute(
            delete(QuestionSimilarityRow).where(QuestionSimilarityRow.course_id == course_id)
        )
        for match in matches:
            self.session.add(mappers.similarity_to_row(match))
        self.session.flush()

    # -- reads -------------------------------------------------------------

    def get_analysis(self, question_id: str) -> QuestionAnalysis | None:
        row = self.session.scalars(
            select(QuestionAnalysisRow).where(QuestionAnalysisRow.question_id == question_id)
        ).first()
        return mappers.row_to_analysis(row) if row else None

    def list_analyses(
        self,
        course_id: str,
        *,
        mapping_status: MappingStatus | None = None,
        limit: int | None = None,
    ) -> list[QuestionAnalysis]:
        stmt = select(QuestionAnalysisRow).where(QuestionAnalysisRow.course_id == course_id)
        if mapping_status is not None:
            stmt = stmt.where(QuestionAnalysisRow.mapping_status == mapping_status.value)
        stmt = stmt.order_by(QuestionAnalysisRow.question_id)
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_analysis(row) for row in self.session.scalars(stmt).all()]

    def count_analyses(self, course_id: str, *, mapping_status: MappingStatus | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(QuestionAnalysisRow)
            .where(QuestionAnalysisRow.course_id == course_id)
        )
        if mapping_status is not None:
            stmt = stmt.where(QuestionAnalysisRow.mapping_status == mapping_status.value)
        return int(self.session.scalar(stmt) or 0)

    def list_unit_matches(self, question_id: str) -> list[UnitMatch]:
        rows = self.session.scalars(
            select(QuestionUnitMatchRow)
            .where(QuestionUnitMatchRow.question_id == question_id)
            .order_by(QuestionUnitMatchRow.rank)
        ).all()
        return [mappers.row_to_unit_match(row) for row in rows]

    def list_unit_matches_for_course(self, course_id: str) -> list[UnitMatch]:
        """Batch read used by analytics; avoids one query per question."""
        rows = self.session.scalars(
            select(QuestionUnitMatchRow)
            .where(QuestionUnitMatchRow.course_id == course_id)
            .order_by(QuestionUnitMatchRow.question_id, QuestionUnitMatchRow.rank)
        ).all()
        return [mappers.row_to_unit_match(row) for row in rows]

    def list_types(self, question_id: str) -> list[TypeLabel]:
        rows = self.session.scalars(
            select(QuestionTypeRow)
            .where(QuestionTypeRow.question_id == question_id)
            .order_by(QuestionTypeRow.rank)
        ).all()
        return [mappers.row_to_type(row) for row in rows]

    def list_types_for_course(self, course_id: str) -> list[TypeLabel]:
        rows = self.session.scalars(
            select(QuestionTypeRow)
            .where(QuestionTypeRow.course_id == course_id)
            .order_by(QuestionTypeRow.question_id, QuestionTypeRow.rank)
        ).all()
        return [mappers.row_to_type(row) for row in rows]

    def list_bloom(self, question_id: str) -> list[BloomLabel]:
        rows = self.session.scalars(
            select(QuestionBloomRow)
            .where(QuestionBloomRow.question_id == question_id)
            .order_by(QuestionBloomRow.rank)
        ).all()
        return [mappers.row_to_bloom(row) for row in rows]

    def get_difficulty(self, question_id: str) -> DifficultyAssessment | None:
        row = self.session.scalars(
            select(QuestionDifficultyRow).where(QuestionDifficultyRow.question_id == question_id)
        ).first()
        return mappers.row_to_difficulty(row) if row else None

    def list_difficulties_for_course(self, course_id: str) -> list[DifficultyAssessment]:
        rows = self.session.scalars(
            select(QuestionDifficultyRow)
            .where(QuestionDifficultyRow.course_id == course_id)
            .order_by(QuestionDifficultyRow.question_id)
        ).all()
        return [mappers.row_to_difficulty(row) for row in rows]

    def list_similarities(
        self,
        course_id: str,
        *,
        question_id: str | None = None,
        kind: SimilarityKind | None = None,
        min_score: float | None = None,
        limit: int | None = None,
    ) -> list[SimilarityMatch]:
        stmt = select(QuestionSimilarityRow).where(QuestionSimilarityRow.course_id == course_id)
        if question_id:
            stmt = stmt.where(
                or_(
                    QuestionSimilarityRow.question_id == question_id,
                    QuestionSimilarityRow.matched_question_id == question_id,
                )
            )
        if kind is not None:
            stmt = stmt.where(QuestionSimilarityRow.kind == kind.value)
        if min_score is not None:
            stmt = stmt.where(QuestionSimilarityRow.score >= min_score)
        stmt = stmt.order_by(QuestionSimilarityRow.score.desc())
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_similarity(row) for row in self.session.scalars(stmt).all()]

    def count_similarities(self, course_id: str, *, kind: SimilarityKind | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(QuestionSimilarityRow)
            .where(QuestionSimilarityRow.course_id == course_id)
        )
        if kind is not None:
            stmt = stmt.where(QuestionSimilarityRow.kind == kind.value)
        return int(self.session.scalar(stmt) or 0)

    def label_counts(self, course_id: str) -> dict[str, dict[str, int]]:
        types = self.session.execute(
            select(QuestionTypeRow.label, func.count())
            .where(QuestionTypeRow.course_id == course_id)
            .group_by(QuestionTypeRow.label)
        ).all()
        bloom = self.session.execute(
            select(QuestionBloomRow.level, func.count())
            .where(QuestionBloomRow.course_id == course_id)
            .group_by(QuestionBloomRow.level)
        ).all()
        difficulty = self.session.execute(
            select(QuestionDifficultyRow.label, func.count())
            .where(QuestionDifficultyRow.course_id == course_id)
            .group_by(QuestionDifficultyRow.label)
        ).all()
        return {
            "types": {str(label): int(count) for label, count in types},
            "bloom": {str(level): int(count) for level, count in bloom},
            "difficulty": {str(label): int(count) for label, count in difficulty},
        }

    def delete_for_course(self, course_id: str) -> None:
        self.clear_for_course(course_id)


class UnderstandingRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, report: UnderstandingRunReport) -> None:
        row = self.session.get(UnderstandingRunRow, report.run_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()
        self.session.add(mappers.run_to_row(report))
        self.session.flush()

    def get(self, run_id: str) -> UnderstandingRunReport | None:
        row = self.session.get(UnderstandingRunRow, run_id)
        return mappers.row_to_run(row) if row else None

    def latest_for_course(self, course_id: str) -> UnderstandingRunReport | None:
        row = self.session.scalars(
            select(UnderstandingRunRow)
            .where(UnderstandingRunRow.course_id == course_id)
            .order_by(UnderstandingRunRow.started_at.desc())
        ).first()
        return mappers.row_to_run(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(UnderstandingRunRow).order_by(UnderstandingRunRow.started_at.desc()).limit(limit)
        ).all()
        return [
            {
                "run_id": row.run_id,
                "course_id": row.course_id,
                "status": row.status,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "totals": mappers.loads(row.totals_json, {}),
                "model_ids": mappers.loads(row.model_ids_json, []),
                "duration_ms": row.duration_ms,
            }
            for row in rows
        ]


def latest_run_at(session: Session, course_id: str) -> datetime | None:
    """Small helper used by status output when a run timestamp is needed."""
    row = session.scalars(
        select(UnderstandingRunRow)
        .where(UnderstandingRunRow.course_id == course_id)
        .order_by(UnderstandingRunRow.started_at.desc())
    ).first()
    return None if row is None else mappers.parse_dt(row.started_at)


__all__ = ["QuestionUnderstandingRepository", "UnderstandingRunRepository", "latest_run_at"]
