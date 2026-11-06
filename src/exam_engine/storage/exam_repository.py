"""SQL surface for Phase 13 examination records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from exam_engine.storage.schema import (
    DocumentRow,
    ExamAnswerRow,
    ExamImportFileRow,
    ExamImportRunRow,
    ExamPaperRow,
    ExamQuestionDetailRow,
    QuestionRow,
    SyllabusRow,
    SyllabusUnitRow,
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class ExamDatasetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- projection --------------------------------------------------------
    def replace_document_projection(
        self,
        document_id: str,
        *,
        papers: list[dict[str, Any]],
        details: list[dict[str, Any]],
        answers: list[dict[str, Any]],
    ) -> None:
        question_ids = select(QuestionRow.question_id).where(QuestionRow.document_id == document_id)
        self.session.execute(
            delete(ExamAnswerRow).where(ExamAnswerRow.question_id.in_(question_ids))
        )
        self.session.execute(
            delete(ExamQuestionDetailRow).where(
                ExamQuestionDetailRow.question_id.in_(
                    select(QuestionRow.question_id).where(QuestionRow.document_id == document_id)
                )
            )
        )
        self.session.execute(delete(ExamPaperRow).where(ExamPaperRow.document_id == document_id))
        for paper in papers:
            self.session.add(ExamPaperRow(**paper))
        for detail in details:
            self.session.add(ExamQuestionDetailRow(**detail))
        for answer in answers:
            self.session.add(ExamAnswerRow(**answer))
        self.session.flush()

    def paper_counts(self, course_id: str) -> dict[str, int]:
        rows = self.session.execute(
            select(ExamQuestionDetailRow.paper_id, func.count())
            .where(ExamQuestionDetailRow.course_id == course_id)
            .group_by(ExamQuestionDetailRow.paper_id)
        ).all()
        return {str(paper_id): int(count) for paper_id, count in rows}

    # -- import runs -------------------------------------------------------
    def create_run(self, run_id: str, started_at: datetime) -> None:
        self.session.add(
            ExamImportRunRow(
                run_id=run_id,
                started_at=_iso(started_at),
                status="running",
                totals_json="{}",
                warnings_json="[]",
                errors_json="[]",
            )
        )
        self.session.flush()

    def save_run(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        status: str,
        totals: dict[str, Any],
        warnings: list[str],
        errors: list[str],
        duration_ms: int,
    ) -> None:
        row = self.session.get(ExamImportRunRow, run_id)
        if row is None:
            row = ExamImportRunRow(run_id=run_id, started_at=_iso(finished_at))
            self.session.add(row)
        row.finished_at = _iso(finished_at)
        row.status = status
        row.totals_json = json.dumps(totals, ensure_ascii=False)
        row.warnings_json = json.dumps(warnings, ensure_ascii=False)
        row.errors_json = json.dumps(errors, ensure_ascii=False)
        row.duration_ms = duration_ms
        self.session.flush()

    def add_file(self, run_id: str, report: dict[str, Any]) -> None:
        self.session.add(
            ExamImportFileRow(
                run_id=run_id,
                category_id=report["category_id"],
                subject_code=report["subject_code"],
                course_id=report["course_id"],
                relative_path=report["relative_path"],
                kind=report["kind"],
                status=report["status"],
                row_count=report.get("row_count", 0),
                imported_questions=report.get("imported_questions", 0),
                imported_answers=report.get("imported_answers", 0),
                skipped_count=report.get("skipped_count", 0),
                failed_count=report.get("failed_count", 0),
                duration_ms=report.get("duration_ms", 0),
                report_json=json.dumps(report, ensure_ascii=False, default=str),
            )
        )
        self.session.flush()

    def latest_run(self) -> dict[str, Any] | None:
        row = self.session.scalars(
            select(ExamImportRunRow).order_by(ExamImportRunRow.started_at.desc()).limit(1)
        ).first()
        if row is None:
            return None
        files = self.session.scalars(
            select(ExamImportFileRow)
            .where(ExamImportFileRow.run_id == row.run_id)
            .order_by(ExamImportFileRow.file_id)
        ).all()
        return {
            "run_id": row.run_id,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "status": row.status,
            "totals": json.loads(row.totals_json or "{}"),
            "warnings": json.loads(row.warnings_json or "[]"),
            "errors": json.loads(row.errors_json or "[]"),
            "duration_ms": row.duration_ms,
            "files": [
                {
                    "file_id": item.file_id,
                    "category_id": item.category_id,
                    "subject_code": item.subject_code,
                    "course_id": item.course_id,
                    "relative_path": item.relative_path,
                    "kind": item.kind,
                    "status": item.status,
                    "row_count": item.row_count,
                    "imported_questions": item.imported_questions,
                    "imported_answers": item.imported_answers,
                    "imported_syllabus_units": int(
                        json.loads(item.report_json or "{}").get("imported_syllabus_units", 0)
                    ),
                    "skipped_count": item.skipped_count,
                    "failed_count": item.failed_count,
                    "duration_ms": item.duration_ms,
                    "report": json.loads(item.report_json or "{}"),
                }
                for item in files
            ],
        }

    # -- counts ------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        return {
            "papers": int(self.session.scalar(select(func.count()).select_from(ExamPaperRow)) or 0),
            "questions": int(
                self.session.scalar(select(func.count()).select_from(ExamQuestionDetailRow)) or 0
            ),
            "answers": int(
                self.session.scalar(select(func.count()).select_from(ExamAnswerRow)) or 0
            ),
            "courses": int(
                self.session.scalar(select(func.count(func.distinct(ExamPaperRow.course_id)))) or 0
            ),
            "categories": int(
                self.session.scalar(select(func.count(func.distinct(ExamPaperRow.category_id))))
                or 0
            ),
            "syllabus_documents": int(
                self.session.scalar(
                    select(func.count())
                    .select_from(DocumentRow)
                    .where(DocumentRow.kind == "syllabus")
                )
                or 0
            ),
            "syllabus_units": int(
                self.session.scalar(select(func.count()).select_from(SyllabusUnitRow)) or 0
            ),
            "syllabi": int(self.session.scalar(select(func.count()).select_from(SyllabusRow)) or 0),
        }

    def exam_course_ids(self) -> list[str]:
        return list(
            self.session.scalars(
                select(ExamPaperRow.course_id).distinct().order_by(ExamPaperRow.course_id)
            ).all()
        )


__all__ = ["ExamDatasetRepository"]
