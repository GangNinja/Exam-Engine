"""Read models for the Phase 13 exam dataset workspace."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from exam_engine.datasets.parsing import (
    ParsedOption,
    extract_correct_option,
    extract_passage_subparts,
    is_passage_question,
    parse_options,
    parse_options_from_text,
    plausible_passage_options,
    strip_embedded_options,
)
from exam_engine.storage.exam_repository import ExamDatasetRepository
from exam_engine.storage.schema import (
    CourseRow,
    DocumentRow,
    ExamAnswerRow,
    ExamPaperRow,
    ExamPassageRow,
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
    question_id: str | None = None
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
            "question_id": self.question_id,
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
        if filters.question_id:
            conditions.append(ExamQuestionDetailRow.question_id == filters.question_id)
        if filters.search:
            pattern = f"%{filters.search}%"
            conditions.append(
                or_(
                    ExamQuestionDetailRow.question_id.in_(
                        select(QuestionRow.question_id).where(QuestionRow.text.ilike(pattern))
                    ),
                    ExamQuestionDetailRow.source_question_id.ilike(pattern),
                    ExamQuestionDetailRow.question_id.ilike(pattern),
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
    @staticmethod
    def _passage_payload(
        passage: ExamPassageRow, question_count: int | None = None
    ) -> dict[str, Any]:
        try:
            child_ids = json.loads(passage.child_question_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            child_ids = []
        return {
            "passage_id": passage.passage_id,
            "course_id": passage.course_id,
            "text": passage.passage_text,
            "source_reference": passage.source_reference,
            "source_file": passage.source_file,
            "confidence": passage.confidence,
            "child_question_ids": child_ids if isinstance(child_ids, list) else [],
            "question_count": (
                question_count if question_count is not None else len(child_ids)
            ),
        }

    def _question_payload(
        self,
        detail: ExamQuestionDetailRow,
        question: QuestionRow,
        answer: ExamAnswerRow | None,
        paper: ExamPaperRow,
        passage: ExamPassageRow | None,
    ) -> dict[str, Any]:
        question_text = str(question.text or "")
        answer_type = answer.answer_type if answer is not None else None
        passage_question = is_passage_question(question_text)
        passage_subparts = (
            extract_passage_subparts(question_text)
            if passage_question and answer_type != "mcq_option"
            else []
        )
        try:
            options = json.loads(detail.options_json or "[]")
        except (TypeError, json.JSONDecodeError):
            options = []
        parsed_options = []
        if isinstance(options, list):
            for item in options:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                text = str(item.get("text") or "").strip()
                if key and text:
                    parsed_options.append(ParsedOption(key=key, text=text))
        # Lettered passage questions are prompts, not answer choices.  Keep
        # stored A-D options for a single passage MCQ, but drop options on a
        # descriptive passage with detected subparts.
        if passage_question and answer_type != "mcq_option" and passage_subparts:
            parsed_options = []
            options = []
        if passage_question and not plausible_passage_options(parsed_options):
            parsed_options = []
            options = []
        # Older imports retained the source options in question.metadata_json
        # but left the structured detail column empty. Read-time recovery makes
        # those options visible without rewriting production rows. It also
        # handles source option lists copied into the question text.
        if not parsed_options and (
            not passage_question
            or answer_type == "mcq_option"
            or not passage_subparts
        ):
            try:
                metadata = json.loads(question.metadata_json or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            raw_options = metadata.get("options") if isinstance(metadata, dict) else None
            answer_text = answer.answer_text if answer is not None else None
            if isinstance(raw_options, str):
                parsed_options = parse_options(raw_options, answer_text=answer_text)
            if not parsed_options and (
                detail.question_type in {"mcq", "msq", "assertion_reason"}
                or (
                    passage_question
                    and (answer_type == "mcq_option" or not passage_subparts)
                )
            ):
                parsed_options = parse_options_from_text(
                    question.text, answer_text=answer_text
                )
            options = [option.as_dict() for option in parsed_options]
        else:
            options = [option.as_dict() for option in parsed_options]
        if passage_question and len(parsed_options) > 5:
            # A passage row with more than five recovered choices is a
            # concatenated set of subquestions, not one MCQ option list.
            parsed_options = []
            options = []
        if passage_question and not plausible_passage_options(parsed_options):
            parsed_options = []
            options = []
        if passage_question and answer_type != "mcq_option" and passage_subparts:
            parsed_options = []
            options = []
        correct_option_key = (
            answer.correct_option_key if answer is not None and parsed_options else None
        )
        if correct_option_key is None and answer is not None and parsed_options:
            correct_option_key = extract_correct_option(
                answer.answer_text,
                answer.answer_type,
                parsed_options,
            )
        try:
            subparts = json.loads(detail.subparts_json or "[]")
        except (TypeError, json.JSONDecodeError):
            subparts = []
        if passage_subparts:
            subparts = passage_subparts
        display_text = (
            strip_embedded_options(question_text, parsed_options)
            if detail.question_type in {"mcq", "msq", "assertion_reason"}
            else question_text
        )
        return {
            "question_id": detail.question_id,
            "source_question_id": detail.source_question_id,
            "source_row": detail.source_row,
            "category_id": detail.category_id,
            "subject_code": detail.subject_code,
            "course_id": detail.course_id,
            "paper_id": detail.paper_id,
            "paper_label": paper.paper_label,
            "exam_year": detail.exam_year,
            "question_type": "passage_based" if passage_question else detail.question_type,
            "question_type_raw": detail.question_type_raw,
            "original_question_number": detail.original_question_number,
            "marks": question.marks,
            "text": display_text,
            "options": options if isinstance(options, list) else [],
            "subparts": subparts if isinstance(subparts, list) else [],
            "has_options": bool(options),
            "source_file": detail.source_file,
            "passage_id": detail.passage_id,
            "passage_order": detail.passage_order,
            "passage": self._passage_payload(passage) if passage is not None else None,
            "answer": (
                {
                    "answer_text": answer.answer_text,
                    "answer_type": answer.answer_type,
                    "answer_type_raw": answer.answer_type_raw,
                    "correct_option_key": correct_option_key,
                    "solution_steps": answer.solution_steps,
                    "source_answer_id": answer.source_answer_id,
                    "match_method": answer.match_method,
                }
                if answer is not None
                else None
            ),
        }

    def questions(
        self,
        filters: ExamFilters | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = filters or ExamFilters()
        conditions = self._conditions(filters)
        option_first = filters.question_type in {None, "mcq", "msq"}
        order_by = []
        if option_first:
            order_by.append(ExamQuestionDetailRow.has_options.desc())
        order_by.extend(
            [
                ExamQuestionDetailRow.category_id,
                ExamQuestionDetailRow.subject_code,
                ExamQuestionDetailRow.exam_year,
                ExamQuestionDetailRow.paper_id,
                QuestionRow.number_sort,
                ExamQuestionDetailRow.source_question_id,
            ]
        )
        stmt: Select[Any] = (
            select(
                ExamQuestionDetailRow,
                QuestionRow,
                ExamAnswerRow,
                ExamPaperRow,
                ExamPassageRow,
            )
            .join(QuestionRow, QuestionRow.question_id == ExamQuestionDetailRow.question_id)
            .join(ExamPaperRow, ExamPaperRow.paper_id == ExamQuestionDetailRow.paper_id)
            .outerjoin(
                ExamAnswerRow,
                ExamAnswerRow.question_id == ExamQuestionDetailRow.question_id,
            )
            .outerjoin(
                ExamPassageRow,
                ExamPassageRow.passage_id == ExamQuestionDetailRow.passage_id,
            )
            .where(*conditions)
            .order_by(*order_by)
            .limit(limit)
            .offset(offset)
        )
        rows = self.session.execute(stmt).all()
        payload = [
            self._question_payload(detail, question, answer, paper, passage)
            for detail, question, answer, paper, passage in rows
        ]
        return {
            "questions": payload,
            "total": self._question_count(filters),
            "shown": len(payload),
            "limit": limit,
            "offset": offset,
        }

    def passage(self, passage_id: str) -> dict[str, Any] | None:
        """Return one passage with every linked question in stored order."""
        passage = self.session.get(ExamPassageRow, passage_id)
        if passage is None:
            return None
        rows = self.session.execute(
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
            .where(ExamQuestionDetailRow.passage_id == passage_id)
            .order_by(
                ExamQuestionDetailRow.passage_order.is_(None),
                ExamQuestionDetailRow.passage_order,
                ExamQuestionDetailRow.source_row,
                QuestionRow.number_sort,
                ExamQuestionDetailRow.source_question_id,
            )
        ).all()
        questions = [
            self._question_payload(detail, question, answer, paper, passage)
            for detail, question, answer, paper in rows
        ]
        payload = self._passage_payload(passage, len(questions))
        payload["questions"] = questions
        payload["missing_child_question_ids"] = [
            child_id
            for child_id in payload["child_question_ids"]
            if child_id not in {item["source_question_id"] for item in questions}
        ]
        return payload

    def question(self, question_id: str) -> dict[str, Any] | None:
        """Return one independently addressable question, retaining its passage."""
        row = self.session.execute(
            select(
                ExamQuestionDetailRow,
                QuestionRow,
                ExamAnswerRow,
                ExamPaperRow,
                ExamPassageRow,
            )
            .join(QuestionRow, QuestionRow.question_id == ExamQuestionDetailRow.question_id)
            .join(ExamPaperRow, ExamPaperRow.paper_id == ExamQuestionDetailRow.paper_id)
            .outerjoin(
                ExamAnswerRow,
                ExamAnswerRow.question_id == ExamQuestionDetailRow.question_id,
            )
            .outerjoin(
                ExamPassageRow,
                ExamPassageRow.passage_id == ExamQuestionDetailRow.passage_id,
            )
            .where(ExamQuestionDetailRow.question_id == question_id)
        ).first()
        if row is None:
            return None
        detail, question, answer, paper, passage = row
        payload = self._question_payload(detail, question, answer, paper, passage)
        return {"question": payload, "passage": payload["passage"]}

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
