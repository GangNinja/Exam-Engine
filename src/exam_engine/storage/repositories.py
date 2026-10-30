"""Repository layer: the only place that talks SQLAlchemy rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from exam_engine.models.course import CourseRecord, utcnow
from exam_engine.models.document import DocumentRecord, PageRecord
from exam_engine.models.enums import IngestionStatus
from exam_engine.models.ingestion import (
    BatchIngestionResult,
    IngestionError,
    IngestionResult,
)
from exam_engine.models.provenance import DatasetSource
from exam_engine.models.question import QuestionRecord
from exam_engine.models.syllabus import SyllabusRecord
from exam_engine.storage import mappers
from exam_engine.storage.schema import (
    CourseRow,
    DatasetSourceRow,
    DocumentRow,
    IngestionResultRow,
    IngestionRunRow,
    PageRow,
    QuestionRow,
    SyllabusRow,
    SyllabusUnitRow,
)


class CourseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, course_id: str) -> CourseRecord | None:
        row = self.session.get(CourseRow, course_id)
        return mappers.row_to_course(row) if row else None

    def upsert(self, course: CourseRecord) -> tuple[CourseRecord, bool]:
        row = self.session.get(CourseRow, course.course_id)
        created = row is None
        now = utcnow()
        if row is None:
            row = mappers.course_to_row(course)
            row.created_at = mappers.iso(course.created_at or now) or ""
        else:
            # Re-imports must not rewrite the original creation timestamp.
            created_at = row.created_at
            row = mappers.course_to_row(course, row)
            row.created_at = created_at
        row.updated_at = mappers.iso(now) or ""
        self.session.add(row)
        self.session.flush()
        return mappers.row_to_course(row), created

    def conflicting_offerings(self, course: CourseRecord) -> list[str]:
        stmt = select(CourseRow.course_id).where(
            CourseRow.institution_code == course.institution_code.strip().lower(),
            CourseRow.course_code == course.course_code,
            CourseRow.course_id != course.course_id,
        )
        if course.academic_year is None:
            stmt = stmt.where(CourseRow.academic_year.is_(None))
        else:
            stmt = stmt.where(CourseRow.academic_year == course.academic_year)
        if course.syllabus_version is None:
            stmt = stmt.where(CourseRow.syllabus_version.is_(None))
        else:
            stmt = stmt.where(CourseRow.syllabus_version == course.syllabus_version)
        return list(self.session.scalars(stmt).all())

    def list(self, limit: int | None = None) -> list[CourseRecord]:
        stmt = select(CourseRow).order_by(
            CourseRow.institution_code, CourseRow.course_code, CourseRow.academic_year
        )
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_course(r) for r in self.session.scalars(stmt).all()]


class DatasetSourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, source_id: str) -> DatasetSource | None:
        row = self.session.get(DatasetSourceRow, source_id)
        return mappers.row_to_source(row) if row else None

    def upsert(self, source: DatasetSource) -> DatasetSource:
        row = self.session.get(DatasetSourceRow, source.source_id)
        row = mappers.source_to_row(source, row)
        self.session.add(row)
        self.session.flush()
        return mappers.row_to_source(row)

    def list(self) -> list[DatasetSource]:
        rows = self.session.scalars(select(DatasetSourceRow)).all()
        return [mappers.row_to_source(r) for r in rows]


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, document_id: str) -> DocumentRecord | None:
        row = self.session.get(DocumentRow, document_id)
        return mappers.row_to_document(row) if row else None

    def find_by_content(self, course_id: str, content_sha256: str) -> DocumentRecord | None:
        row = self.session.scalars(
            select(DocumentRow).where(
                DocumentRow.course_id == course_id,
                DocumentRow.content_sha256 == content_sha256,
            )
        ).first()
        return mappers.row_to_document(row) if row else None

    def find_by_text_fingerprint(self, course_id: str, fingerprint: str) -> DocumentRecord | None:
        if not fingerprint:
            return None
        row = self.session.scalars(
            select(DocumentRow).where(
                DocumentRow.course_id == course_id,
                DocumentRow.text_fingerprint == fingerprint,
            )
        ).first()
        return mappers.row_to_document(row) if row else None

    def upsert(self, document: DocumentRecord) -> None:
        row = self.session.get(DocumentRow, document.document_id)
        self.session.add(mappers.document_to_row(document, row))
        self.session.flush()

    def delete(self, document_id: str) -> None:
        self.session.execute(delete(DocumentRow).where(DocumentRow.document_id == document_id))
        self.session.flush()

    def list_by_course(self, course_id: str) -> list[DocumentRecord]:
        rows = self.session.scalars(
            select(DocumentRow)
            .where(DocumentRow.course_id == course_id)
            .order_by(DocumentRow.kind, DocumentRow.title)
        ).all()
        return [mappers.row_to_document(r) for r in rows]

    def list_all(self, limit: int | None = None) -> list[DocumentRecord]:
        stmt = select(DocumentRow).order_by(DocumentRow.course_id, DocumentRow.title)
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_document(r) for r in self.session.scalars(stmt).all()]

    def count_by_course(self, course_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(DocumentRow)
                .where(DocumentRow.course_id == course_id)
            )
            or 0
        )

    # --- pages
    def replace_pages(self, document_id: str, pages: list[PageRecord]) -> None:
        self.session.execute(delete(PageRow).where(PageRow.document_id == document_id))
        for page in pages:
            self.session.add(mappers.page_to_row(document_id, page))
        self.session.flush()

    def list_pages(self, document_id: str) -> list[PageRecord]:
        rows = self.session.scalars(
            select(PageRow).where(PageRow.document_id == document_id).order_by(PageRow.page_number)
        ).all()
        return [mappers.row_to_page(r) for r in rows]


class QuestionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_document(self, document_id: str, questions: list[QuestionRecord]) -> None:
        self.session.execute(delete(QuestionRow).where(QuestionRow.document_id == document_id))
        for question in questions:
            self.session.merge(mappers.question_to_row(question))
        self.session.flush()

    def get(self, question_id: str) -> QuestionRecord | None:
        row = self.session.get(QuestionRow, question_id)
        return mappers.row_to_question(row) if row else None

    def list_by_document(self, document_id: str) -> list[QuestionRecord]:
        rows = self.session.scalars(
            select(QuestionRow)
            .where(QuestionRow.document_id == document_id)
            .order_by(QuestionRow.number_sort, QuestionRow.question_id)
        ).all()
        return [mappers.row_to_question(r) for r in rows]

    def list_by_course(self, course_id: str, limit: int | None = None) -> list[QuestionRecord]:
        stmt = (
            select(QuestionRow)
            .where(QuestionRow.course_id == course_id)
            .order_by(QuestionRow.document_id, QuestionRow.number_sort)
        )
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_question(r) for r in self.session.scalars(stmt).all()]

    def fingerprint_index(
        self, course_id: str, *, exclude_document_id: str | None = None
    ) -> dict[str, str]:
        """Map fingerprint -> question_id for duplicate detection within a course."""
        stmt = select(QuestionRow.fingerprint, QuestionRow.question_id).where(
            QuestionRow.course_id == course_id,
            QuestionRow.fingerprint != "",
        )
        if exclude_document_id:
            stmt = stmt.where(QuestionRow.document_id != exclude_document_id)
        return dict(self.session.execute(stmt).tuples().all())

    def count_by_course(self, course_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(QuestionRow)
                .where(QuestionRow.course_id == course_id)
            )
            or 0
        )

    def count_needing_review(self, course_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(QuestionRow).where(QuestionRow.needs_review == 1)
        if course_id:
            stmt = stmt.where(QuestionRow.course_id == course_id)
        return int(self.session.scalar(stmt) or 0)

    def count_by_document(self, document_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(QuestionRow)
                .where(QuestionRow.document_id == document_id)
            )
            or 0
        )

    def list_all(self, limit: int | None = None) -> list[QuestionRecord]:
        stmt = select(QuestionRow).order_by(QuestionRow.course_id, QuestionRow.number_sort)
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_question(r) for r in self.session.scalars(stmt).all()]

    def list_needing_review(
        self, course_id: str | None = None, limit: int | None = None
    ) -> list[QuestionRecord]:
        stmt = select(QuestionRow).where(QuestionRow.needs_review == 1)
        if course_id:
            stmt = stmt.where(QuestionRow.course_id == course_id)
        stmt = stmt.order_by(QuestionRow.course_id, QuestionRow.number_sort)
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_question(r) for r in self.session.scalars(stmt).all()]


class SyllabusRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace(self, syllabus: SyllabusRecord) -> None:
        self.session.execute(
            delete(SyllabusRow).where(SyllabusRow.document_id == syllabus.document_id)
        )
        self.session.add(mappers.syllabus_to_row(syllabus))
        for unit in syllabus.units:
            self.session.add(mappers.unit_to_row(unit, syllabus.syllabus_id))
        self.session.flush()

    def get_by_document(self, document_id: str) -> SyllabusRecord | None:
        row = self.session.scalars(
            select(SyllabusRow).where(SyllabusRow.document_id == document_id)
        ).first()
        if row is None:
            return None
        return mappers.row_to_syllabus(row, self.list_units(row.syllabus_id))

    def get_by_course(self, course_id: str) -> list[SyllabusRecord]:
        rows = self.session.scalars(
            select(SyllabusRow)
            .where(SyllabusRow.course_id == course_id)
            .order_by(SyllabusRow.ingested_at)
        ).all()
        return [mappers.row_to_syllabus(r, self.list_units(r.syllabus_id)) for r in rows]

    def list_units(self, syllabus_id: str) -> list:
        rows = self.session.scalars(
            select(SyllabusUnitRow)
            .where(SyllabusUnitRow.syllabus_id == syllabus_id)
            .order_by(SyllabusUnitRow.unit_number, SyllabusUnitRow.unit_id)
        ).all()
        return [mappers.row_to_unit(r) for r in rows]

    def count_units_by_course(self, course_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(SyllabusUnitRow)
            .join(SyllabusRow, SyllabusRow.syllabus_id == SyllabusUnitRow.syllabus_id)
            .where(SyllabusRow.course_id == course_id)
        )
        return int(self.session.scalar(stmt) or 0)


class RunRepository:
    """Persisted batch reports so failures can be inspected after the fact."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        run_id: str,
        *,
        started_at: datetime,
        manifest_path: str | None,
        source_id: str | None,
    ) -> None:
        self.session.add(
            IngestionRunRow(
                run_id=run_id,
                started_at=mappers.iso(started_at) or "",
                finished_at=None,
                manifest_path=manifest_path,
                source_id=source_id,
                status="running",
                totals_json="{}",
            )
        )
        self.session.flush()

    def save(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        results: list[IngestionResult],
    ) -> None:
        row = self.session.get(IngestionRunRow, run_id)
        if row is None:
            raise KeyError(f"unknown ingestion run: {run_id}")
        self.session.execute(delete(IngestionResultRow).where(IngestionResultRow.run_id == run_id))
        for result in results:
            self.session.add(
                IngestionResultRow(
                    run_id=run_id,
                    source_path=result.source_path,
                    course_id=result.course_id,
                    status=result.status.value,
                    document_id=result.document.document_id if result.document else None,
                    page_count=result.page_count,
                    question_count=result.question_count,
                    syllabus_unit_count=result.syllabus_unit_count,
                    duplicate_of=result.duplicate_of,
                    errors_json=mappers.dumps([e.model_dump(mode="json") for e in result.errors]),
                    warnings_json=mappers.dumps(
                        [w.model_dump(mode="json") for w in result.warnings]
                    ),
                    duration_ms=result.duration_ms,
                )
            )
        failed = sum(1 for r in results if r.status is IngestionStatus.FAILED)
        row.status = (
            "failed" if failed == len(results) and results else ("partial" if failed else "ok")
        )
        row.finished_at = mappers.iso(finished_at)
        totals = BatchIngestionResult(run_id=run_id, results=results).totals
        row.totals_json = mappers.dumps(totals)
        self.session.flush()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(IngestionRunRow).order_by(IngestionRunRow.started_at.desc()).limit(limit)
        ).all()
        return [
            {
                "run_id": r.run_id,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "manifest_path": r.manifest_path,
                "source_id": r.source_id,
                "status": r.status,
                "totals": mappers.loads(r.totals_json, {}),
            }
            for r in rows
        ]

    def get_run(self, run_id: str) -> BatchIngestionResult | None:
        row = self.session.get(IngestionRunRow, run_id)
        if row is None:
            return None
        result_rows = self.session.scalars(
            select(IngestionResultRow)
            .where(IngestionResultRow.run_id == run_id)
            .order_by(IngestionResultRow.result_id)
        ).all()
        documents = DocumentRepository(self.session)
        results: list[IngestionResult] = []
        for r in result_rows:
            document = documents.get(r.document_id) if r.document_id else None
            results.append(
                IngestionResult(
                    source_path=r.source_path,
                    course_id=r.course_id,
                    status=IngestionStatus(r.status),
                    document=document,
                    page_count=r.page_count,
                    question_count=r.question_count,
                    syllabus_unit_count=r.syllabus_unit_count,
                    duplicate_of=r.duplicate_of,
                    errors=[
                        IngestionError.model_validate(e) for e in mappers.loads(r.errors_json, [])
                    ],
                    warnings=[
                        IngestionError.model_validate(w) for w in mappers.loads(r.warnings_json, [])
                    ],
                    duration_ms=r.duration_ms,
                )
            )
        return BatchIngestionResult(
            run_id=row.run_id,
            started_at=mappers.parse_dt(row.started_at) or utcnow(),
            finished_at=mappers.parse_dt(row.finished_at),
            manifest_path=row.manifest_path,
            source_id=row.source_id,
            results=results,
        )
