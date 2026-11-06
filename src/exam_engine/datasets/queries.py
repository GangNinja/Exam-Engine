"""Read models for the Phase 13 exam dataset workspace."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from exam_engine.storage.exam_repository import ExamDatasetRepository
from exam_engine.storage.schema import (
    CourseRow,
    DocumentRow,
    ExamAnswerRow,
    ExamPaperRow,
    ExamQuestionDetailRow,
    QuestionRow,
    QuestionUnitMatchRow,
    SyllabusRow,
    SyllabusUnitRow,
)


@dataclass(frozen=True)
class ExamFilters:
    category_id: str | None = None
    subject_code: str | None = None
    course_id: str | None = None
    exam_year: int | None = None
    question_type: str | None = None
    answer_type: str | None = None
    unit_id: str | None = None
    topic: str | None = None
    search: str | None = None

    def as_query(self) -> dict[str, Any]:
        return {
            "category_id": self.category_id,
            "subject_code": self.subject_code,
            "course_id": self.course_id,
            "exam_year": self.exam_year,
            "question_type": self.question_type,
            "answer_type": self.answer_type,
            "unit_id": self.unit_id,
            "topic": self.topic,
            "search": self.search,
        }


class ExamDatasetQueries:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- filters -----------------------------------------------------------
    def _conditions(self, filters: ExamFilters) -> list[Any]:
        conditions: list[Any] = []
        if filters.category_id:
            conditions.append(ExamQuestionDetailRow.category_id == filters.category_id)
        if filters.subject_code:
            conditions.append(ExamQuestionDetailRow.subject_code == filters.subject_code)
        if filters.course_id:
            conditions.append(ExamQuestionDetailRow.course_id == filters.course_id)
        if filters.exam_year is not None:
            conditions.append(ExamQuestionDetailRow.exam_year == filters.exam_year)
        if filters.question_type:
            conditions.append(ExamQuestionDetailRow.question_type == filters.question_type)
        if filters.answer_type:
            conditions.append(
                ExamQuestionDetailRow.question_id.in_(
                    select(ExamAnswerRow.question_id).where(
                        ExamAnswerRow.answer_type == filters.answer_type
                    )
                )
            )
        if filters.unit_id:
            conditions.append(
                ExamQuestionDetailRow.question_id.in_(
                    select(QuestionUnitMatchRow.question_id).where(
                        QuestionUnitMatchRow.unit_id == filters.unit_id,
                        QuestionUnitMatchRow.rank == 1,
                    )
                )
            )
        if filters.topic:
            conditions.append(
                ExamQuestionDetailRow.question_id.in_(
                    select(QuestionUnitMatchRow.question_id).where(
                        QuestionUnitMatchRow.topic == filters.topic,
                        QuestionUnitMatchRow.rank == 1,
                    )
                )
            )
        if filters.search:
            conditions.append(
                ExamQuestionDetailRow.question_id.in_(
                    select(QuestionRow.question_id).where(
                        QuestionRow.text.ilike(f"%{filters.search}%")
                    )
                )
            )
        return conditions

    def _question_count(self, filters: ExamFilters) -> int:
        stmt = (
            select(func.count())
            .select_from(ExamQuestionDetailRow)
            .join(QuestionRow, QuestionRow.question_id == ExamQuestionDetailRow.question_id)
            .where(*self._conditions(filters))
        )
        return int(self.session.scalar(stmt) or 0)

    def filters(self) -> dict[str, Any]:
        categories = self.session.execute(
            select(
                ExamPaperRow.category_id,
                func.count(func.distinct(ExamPaperRow.course_id)),
            )
            .group_by(ExamPaperRow.category_id)
            .order_by(ExamPaperRow.category_id)
        ).all()
        subjects = self.session.execute(
            select(
                ExamPaperRow.course_id,
                ExamPaperRow.category_id,
                ExamPaperRow.subject_code,
                CourseRow.title,
            )
            .join(CourseRow, CourseRow.course_id == ExamPaperRow.course_id)
            .distinct()
            .order_by(ExamPaperRow.category_id, CourseRow.title)
        ).all()
        years = [
            int(year)
            for year in self.session.scalars(
                select(ExamQuestionDetailRow.exam_year)
                .where(ExamQuestionDetailRow.exam_year.is_not(None))
                .distinct()
                .order_by(ExamQuestionDetailRow.exam_year)
            ).all()
            if year is not None
        ]
        question_types = [
            str(value)
            for value in self.session.scalars(
                select(ExamQuestionDetailRow.question_type)
                .distinct()
                .order_by(ExamQuestionDetailRow.question_type)
            ).all()
        ]
        answer_types = [
            str(value)
            for value in self.session.scalars(
                select(ExamAnswerRow.answer_type).distinct().order_by(ExamAnswerRow.answer_type)
            ).all()
        ]
        return {
            "categories": [
                {"category_id": category_id, "courses": int(count)}
                for category_id, count in categories
            ],
            "subjects": [
                {
                    "course_id": course_id,
                    "category_id": category_id,
                    "subject_code": subject_code,
                    "title": title,
                }
                for course_id, category_id, subject_code, title in subjects
            ],
            "years": years,
            "question_types": question_types,
            "answer_types": answer_types,
        }

    # -- overview ----------------------------------------------------------
    def overview(self, filters: ExamFilters | None = None) -> dict[str, Any]:
        filters = filters or ExamFilters()
        conditions = self._conditions(filters)
        question_total = self._question_count(filters)
        answer_total = int(
            self.session.scalar(
                select(func.count())
                .select_from(ExamAnswerRow)
                .join(
                    ExamQuestionDetailRow,
                    ExamQuestionDetailRow.question_id == ExamAnswerRow.question_id,
                )
                .join(QuestionRow, QuestionRow.question_id == ExamAnswerRow.question_id)
                .where(*conditions)
            )
            or 0
        )
        paper_total = int(
            self.session.scalar(
                select(func.count(func.distinct(ExamQuestionDetailRow.paper_id))).where(*conditions)
            )
            or 0
        )
        course_total = int(
            self.session.scalar(
                select(func.count(func.distinct(ExamQuestionDetailRow.course_id))).where(
                    *conditions
                )
            )
            or 0
        )
        category_total = int(
            self.session.scalar(
                select(func.count(func.distinct(ExamQuestionDetailRow.category_id))).where(
                    *conditions
                )
            )
            or 0
        )
        years = [
            int(year)
            for year in self.session.scalars(
                select(ExamQuestionDetailRow.exam_year)
                .where(*conditions, ExamQuestionDetailRow.exam_year.is_not(None))
                .distinct()
                .order_by(ExamQuestionDetailRow.exam_year)
            ).all()
            if year is not None
        ]

        question_types = self.session.execute(
            select(ExamQuestionDetailRow.question_type, func.count())
            .where(*conditions)
            .group_by(ExamQuestionDetailRow.question_type)
            .order_by(func.count().desc())
        ).all()
        categories = self.session.execute(
            select(ExamQuestionDetailRow.category_id, func.count())
            .where(*conditions)
            .group_by(ExamQuestionDetailRow.category_id)
            .order_by(func.count().desc())
        ).all()
        subjects = self.session.execute(
            select(
                ExamQuestionDetailRow.course_id,
                ExamQuestionDetailRow.subject_code,
                CourseRow.title,
                func.count(),
            )
            .join(CourseRow, CourseRow.course_id == ExamQuestionDetailRow.course_id)
            .where(*conditions)
            .group_by(
                ExamQuestionDetailRow.course_id,
                ExamQuestionDetailRow.subject_code,
                CourseRow.title,
            )
            .order_by(func.count().desc())
        ).all()
        year_rows = self.session.execute(
            select(ExamQuestionDetailRow.exam_year, func.count())
            .where(*conditions)
            .group_by(ExamQuestionDetailRow.exam_year)
            .order_by(ExamQuestionDetailRow.exam_year)
        ).all()
        answer_types = self.session.execute(
            select(ExamAnswerRow.answer_type, func.count())
            .join(
                ExamQuestionDetailRow,
                ExamQuestionDetailRow.question_id == ExamAnswerRow.question_id,
            )
            .where(*conditions)
            .group_by(ExamAnswerRow.answer_type)
            .order_by(func.count().desc())
        ).all()

        quality = {
            "missing_options": int(
                self.session.scalar(
                    select(func.count())
                    .select_from(ExamQuestionDetailRow)
                    .where(
                        *conditions,
                        ExamQuestionDetailRow.question_type.in_(["mcq", "msq", "assertion_reason"]),
                        ExamQuestionDetailRow.has_options == 0,
                    )
                )
                or 0
            ),
            "unknown_question_type": int(
                self.session.scalar(
                    select(func.count())
                    .select_from(ExamQuestionDetailRow)
                    .where(*conditions, ExamQuestionDetailRow.question_type == "unknown")
                )
                or 0
            ),
            "missing_year": int(
                self.session.scalar(
                    select(func.count())
                    .select_from(ExamQuestionDetailRow)
                    .where(*conditions, ExamQuestionDetailRow.exam_year.is_(None))
                )
                or 0
            ),
            "unanswered_questions": max(0, question_total - answer_total),
            "missing_correct_option": int(
                self.session.scalar(
                    select(func.count())
                    .select_from(ExamAnswerRow)
                    .join(
                        ExamQuestionDetailRow,
                        ExamQuestionDetailRow.question_id == ExamAnswerRow.question_id,
                    )
                    .where(
                        *conditions,
                        ExamAnswerRow.answer_type == "mcq_option",
                        ExamAnswerRow.correct_option_key.is_(None),
                    )
                )
                or 0
            ),
        }

        matched_course_ids = select(ExamQuestionDetailRow.course_id).where(*conditions)
        syllabus_documents = int(
            self.session.scalar(
                select(func.count())
                .select_from(DocumentRow)
                .where(
                    DocumentRow.kind == "syllabus",
                    DocumentRow.course_id.in_(matched_course_ids),
                )
            )
            or 0
        )
        syllabus_units = int(
            self.session.scalar(
                select(func.count())
                .select_from(SyllabusUnitRow)
                .join(SyllabusRow, SyllabusRow.syllabus_id == SyllabusUnitRow.syllabus_id)
                .where(SyllabusRow.course_id.in_(matched_course_ids))
            )
            or 0
        )
        latest_run = ExamDatasetRepository(self.session).latest_run()
        return {
            "filters_applied": filters.as_query(),
            "summary": {
                "categories": category_total,
                "subjects": course_total,
                "papers": paper_total,
                "questions": question_total,
                "answers": answer_total,
                "matched_pairs": answer_total,
                "syllabus_documents": syllabus_documents,
                "syllabus_units": syllabus_units,
                "years": len(years),
                "year_min": years[0] if years else None,
                "year_max": years[-1] if years else None,
            },
            "years": [{"year": year, "questions": int(count)} for year, count in year_rows],
            "question_types": [
                {"label": label, "questions": int(count)} for label, count in question_types
            ],
            "answer_types": [
                {"label": label, "answers": int(count)} for label, count in answer_types
            ],
            "categories": [
                {"category_id": label, "questions": int(count)} for label, count in categories
            ],
            "subjects": [
                {
                    "course_id": course_id,
                    "subject_code": subject_code,
                    "title": title,
                    "questions": int(count),
                }
                for course_id, subject_code, title, count in subjects
            ],
            "quality": quality,
            "latest_import": latest_run,
            "filters": self.filters(),
        }

    # -- questions ---------------------------------------------------------
    def questions(
        self,
        filters: ExamFilters | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = filters or ExamFilters()
        conditions = self._conditions(filters)
        stmt: Select[Any] = (
            select(
                ExamQuestionDetailRow,
                QuestionRow,
                ExamAnswerRow,
                ExamPaperRow,
            )
            .join(QuestionRow, QuestionRow.question_id == ExamQuestionDetailRow.question_id)
            .join(ExamPaperRow, ExamPaperRow.paper_id == ExamQuestionDetailRow.paper_id)
            .outerjoin(
                ExamAnswerRow,
                ExamAnswerRow.question_id == ExamQuestionDetailRow.question_id,
            )
            .where(*conditions)
            .order_by(
                ExamQuestionDetailRow.category_id,
                ExamQuestionDetailRow.subject_code,
                ExamQuestionDetailRow.exam_year,
                ExamQuestionDetailRow.paper_id,
                QuestionRow.number_sort,
                ExamQuestionDetailRow.source_question_id,
            )
            .limit(limit)
            .offset(offset)
        )
        rows = self.session.execute(stmt).all()
        payload = []
        for detail, question, answer, paper in rows:
            payload.append(
                {
                    "question_id": detail.question_id,
                    "source_question_id": detail.source_question_id,
                    "source_row": detail.source_row,
                    "category_id": detail.category_id,
                    "subject_code": detail.subject_code,
                    "course_id": detail.course_id,
                    "paper_id": detail.paper_id,
                    "paper_label": paper.paper_label,
                    "exam_year": detail.exam_year,
                    "question_type": detail.question_type,
                    "question_type_raw": detail.question_type_raw,
                    "original_question_number": detail.original_question_number,
                    "marks": question.marks,
                    "text": question.text,
                    "options": json.loads(detail.options_json or "[]"),
                    "has_options": bool(detail.has_options),
                    "source_file": detail.source_file,
                    "answer": (
                        {
                            "answer_text": answer.answer_text,
                            "answer_type": answer.answer_type,
                            "answer_type_raw": answer.answer_type_raw,
                            "correct_option_key": answer.correct_option_key,
                            "solution_steps": answer.solution_steps,
                            "source_answer_id": answer.source_answer_id,
                            "match_method": answer.match_method,
                        }
                        if answer is not None
                        else None
                    ),
                }
            )
        return {
            "questions": payload,
            "total": self._question_count(filters),
            "shown": len(payload),
            "limit": limit,
            "offset": offset,
        }

    # -- papers ------------------------------------------------------------
    def papers(
        self,
        filters: ExamFilters | None = None,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        filters = filters or ExamFilters()
        conditions = self._conditions(filters)
        answer_count = (
            select(
                ExamAnswerRow.question_id,
                func.count().label("answers"),
            )
            .group_by(ExamAnswerRow.question_id)
            .subquery()
        )
        stmt = (
            select(
                ExamPaperRow.paper_id,
                ExamPaperRow.category_id,
                ExamPaperRow.subject_code,
                ExamPaperRow.course_id,
                ExamPaperRow.paper_label,
                ExamPaperRow.exam_year,
                ExamPaperRow.document_id,
                ExamPaperRow.source_question_prefix,
                func.count(ExamQuestionDetailRow.question_id).label("questions"),
                func.sum(func.coalesce(answer_count.c.answers, 0)).label("answers"),
            )
            .join(
                ExamQuestionDetailRow,
                ExamQuestionDetailRow.paper_id == ExamPaperRow.paper_id,
            )
            .outerjoin(
                answer_count,
                answer_count.c.question_id == ExamQuestionDetailRow.question_id,
            )
            .where(*conditions)
            .group_by(
                ExamPaperRow.paper_id,
                ExamPaperRow.category_id,
                ExamPaperRow.subject_code,
                ExamPaperRow.course_id,
                ExamPaperRow.paper_label,
                ExamPaperRow.exam_year,
                ExamPaperRow.document_id,
                ExamPaperRow.source_question_prefix,
            )
            .order_by(
                ExamPaperRow.category_id,
                ExamPaperRow.subject_code,
                ExamPaperRow.exam_year,
                ExamPaperRow.paper_label,
            )
            .limit(limit)
        )
        rows = self.session.execute(stmt).all()
        total = int(
            self.session.scalar(
                select(func.count(func.distinct(ExamQuestionDetailRow.paper_id))).where(*conditions)
            )
            or 0
        )
        return {
            "papers": [
                {
                    "paper_id": paper_id,
                    "category_id": category_id,
                    "subject_code": subject_code,
                    "course_id": course_id,
                    "paper_label": paper_label,
                    "exam_year": exam_year,
                    "document_id": document_id,
                    "source_question_prefix": source_prefix,
                    "questions": int(questions),
                    "answers": int(answers or 0),
                }
                for (
                    paper_id,
                    category_id,
                    subject_code,
                    course_id,
                    paper_label,
                    exam_year,
                    document_id,
                    source_prefix,
                    questions,
                    answers,
                ) in rows
            ],
            "total": total,
        }

    # -- syllabus ----------------------------------------------------------
    def syllabus(self, course_id: str) -> dict[str, Any]:
        course = self.session.get(CourseRow, course_id)
        if course is None:
            return {"course_id": course_id, "syllabi": [], "error": "unknown_course"}
        rows = self.session.scalars(
            select(SyllabusRow)
            .where(SyllabusRow.course_id == course_id)
            .order_by(SyllabusRow.ingested_at)
        ).all()
        syllabi: list[dict[str, Any]] = []
        for syllabus in rows:
            units = self.session.scalars(
                select(SyllabusUnitRow)
                .where(SyllabusUnitRow.syllabus_id == syllabus.syllabus_id)
                .order_by(SyllabusUnitRow.unit_number, SyllabusUnitRow.unit_id)
            ).all()
            syllabi.append(
                {
                    "syllabus_id": syllabus.syllabus_id,
                    "title": syllabus.title,
                    "confidence": syllabus.confidence,
                    "needs_review": bool(syllabus.needs_review),
                    "document_id": syllabus.document_id,
                    "units": [
                        {
                            "unit_id": unit.unit_id,
                            "unit_label": unit.unit_label,
                            "unit_number": unit.unit_number,
                            "title": unit.title,
                            "hours": unit.hours,
                            "topics": json.loads(unit.topics_json or "[]"),
                            "confidence": unit.confidence,
                        }
                        for unit in units
                    ],
                }
            )
        return {
            "course_id": course_id,
            "title": course.title,
            "syllabi": syllabi,
            "unit_count": sum(len(item["units"]) for item in syllabi),
        }


def filter_options(filters: ExamFilters) -> Sequence[Any]:
    """Small helper kept public for tests and future API adapters."""
    return (filters.category_id, filters.subject_code, filters.course_id)


__all__ = ["ExamDatasetQueries", "ExamFilters"]
