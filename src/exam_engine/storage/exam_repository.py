"""SQL surface for Phase 13 examination records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from exam_engine.models.enums import ExtractionMethod
from exam_engine.models.provenance import (
    IdentityStatus,
    ProvenanceRecord,
    ProvenanceScope,
    SourceClassification,
)
from exam_engine.storage.schema import (
    CourseRow,
    DatasetSourceRow,
    DocumentRow,
    ExamAnswerRow,
    ExamImportFileRow,
    ExamImportRunRow,
    ExamPaperRow,
    ExamPassageRow,
    ExamProvenanceRow,
    ExamQuestionDetailRow,
    QuestionRow,
    SyllabusRow,
    SyllabusUnitRow,
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _provenance_row(record: ProvenanceRecord) -> ExamProvenanceRow:
    """Map one validated provenance record onto its storage row."""
    return ExamProvenanceRow(
        provenance_id=record.provenance_id,
        scope=record.scope.value,
        course_id=record.course_id,
        document_id=record.document_id,
        paper_id=record.paper_id,
        question_id=record.question_id,
        answer_id=record.answer_id,
        source_id=record.source_id,
        source_url=record.source_url,
        source_publisher=record.source_publisher,
        retrieved_at=record.retrieved_at.isoformat() if record.retrieved_at else None,
        source_document_sha256=record.source_document_sha256,
        examining_authority=record.examining_authority,
        examination_name=record.examination_name,
        exam_year=record.exam_year,
        exam_session=record.exam_session,
        subject_code=record.subject_code,
        paper_code=record.paper_code,
        set_code=record.set_code,
        region=record.region,
        language=record.language,
        original_question_id=record.original_question_id,
        original_option_ids_json=json.dumps(list(record.original_option_ids), ensure_ascii=False),
        original_question_number=record.original_question_number,
        extraction_method=(
            record.extraction_method.value if record.extraction_method is not None else None
        ),
        extraction_confidence=record.extraction_confidence,
        source_classification=record.source_classification.value,
        identity_status=record.identity_status.value,
        paper_identity_status=record.paper_identity_status.value,
        key_identity_status=record.key_identity_status.value,
        notes=record.notes,
        evidence_json=json.dumps(record.evidence, ensure_ascii=False, default=str),
        created_at=_iso(record.created_at),
    )


class ExamDatasetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- passage context ---------------------------------------------------
    def upsert_passages(self, passages: list[Any]) -> dict[str, Any]:
        """Upsert the source passage sidecar and link matching question details.

        A child id that does not resolve is reported and left unlinked; it is
        never guessed from a similar question id or a paper label.
        """
        linked = 0
        missing: list[dict[str, str]] = []
        now = _iso(datetime.now(UTC))
        for passage in passages:
            course = self.session.get(CourseRow, passage.course_id)
            if course is None:
                for child_id in passage.child_question_ids:
                    missing.append(
                        {
                            "passage_id": passage.passage_id,
                            "question_id": child_id,
                            "reason": "unknown_course",
                        }
                    )
                continue
            details = self.session.scalars(
                select(ExamQuestionDetailRow).where(
                    ExamQuestionDetailRow.course_id == passage.course_id,
                    ExamQuestionDetailRow.source_question_id.in_(passage.child_question_ids),
                )
            ).all()
            by_source = {detail.source_question_id: detail for detail in details}
            for child_id in passage.child_question_ids:
                if child_id not in by_source:
                    missing.append(
                        {
                            "passage_id": passage.passage_id,
                            "question_id": child_id,
                            "reason": "missing_question_detail",
                        }
                    )
            ordered = sorted(
                by_source.values(),
                key=lambda detail: (
                    detail.source_row is None,
                    detail.source_row or 0,
                    detail.source_question_id,
                ),
            )
            row = self.session.get(ExamPassageRow, passage.passage_id)
            if row is None:
                row = ExamPassageRow(
                    passage_id=passage.passage_id,
                    course_id=passage.course_id,
                    passage_text=passage.passage_text,
                    source_reference=passage.source_reference,
                    source_file=passage.source_file,
                    child_question_ids_json=json.dumps(
                        list(passage.child_question_ids), ensure_ascii=False
                    ),
                    confidence=passage.confidence,
                    created_at=now,
                )
                self.session.add(row)
            else:
                row.course_id = passage.course_id
                row.passage_text = passage.passage_text
                row.source_reference = passage.source_reference
                row.source_file = passage.source_file
                row.child_question_ids_json = json.dumps(
                    list(passage.child_question_ids), ensure_ascii=False
                )
                row.confidence = passage.confidence
            for order, detail in enumerate(ordered, start=1):
                detail.passage_id = passage.passage_id
                detail.passage_order = order
                linked += 1
        self.session.flush()
        return {
            "passages": len(passages),
            "linked": linked,
            "missing": missing,
            "message": f"{linked} question link(s) stored",
        }

    def update_subparts(self, entries: dict[tuple[str, str], str]) -> int:
        """Store source subparts for linked passage children only."""
        updated = 0
        for (course_id, source_question_id), raw in entries.items():
            detail = self.session.scalars(
                select(ExamQuestionDetailRow).where(
                    ExamQuestionDetailRow.course_id == course_id,
                    ExamQuestionDetailRow.source_question_id == source_question_id,
                )
            ).first()
            if detail is not None and detail.subparts_json != raw:
                detail.subparts_json = raw
                updated += 1
        self.session.flush()
        return updated

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

    # -- provenance --------------------------------------------------------
    def replace_document_provenance(self, document_id: str) -> int:
        """Rebuild provenance rows for one imported document.

        Only values present in the source material are written. Local paper
        labels, source question ids and derived years are recorded inside
        ``evidence_json`` as local values, never in the official-code columns.
        """
        document = self.session.get(DocumentRow, document_id)
        if document is None:
            return 0
        self.session.execute(
            delete(ExamProvenanceRow).where(ExamProvenanceRow.document_id == document_id)
        )
        source = self.session.get(DatasetSourceRow, document.source_id)
        now = datetime.now(UTC)
        base_evidence: dict[str, Any] = {
            "dataset_source_id": document.source_id,
            "local_relative_path": document.relative_path,
            "original_filename": document.original_filename,
            "dataset_source_retrieved_at": (
                source.retrieved_at if source is not None else None
            ),
            "dataset_source_retrieved_at_meaning": (
                "date recorded by the Phase 13 import; not the document acquisition date"
            ),
            "identity_basis": (
                "local corpus only; no official paper, booklet, set or key identity "
                "evidence is attached to this row"
            ),
        }
        extraction_method = (
            ExtractionMethod.STRUCTURED if document.format == "csv" else None
        )
        records: list[ProvenanceRecord] = [
            ProvenanceRecord.build(
                scope=ProvenanceScope.DOCUMENT,
                course_id=document.course_id,
                document_id=document_id,
                created_at=now,
                source_id=document.source_id,
                source_publisher=source.publisher if source is not None else None,
                source_document_sha256=document.content_sha256,
                extraction_method=extraction_method,
                notes=(
                    "local imported document; source URL, examining authority, paper code "
                    "and retrieval date were not recorded and remain unknown"
                ),
                evidence=base_evidence,
            )
        ]

        for paper in self.session.scalars(
            select(ExamPaperRow)
            .where(ExamPaperRow.document_id == document_id)
            .order_by(ExamPaperRow.paper_label)
        ).all():
            records.append(
                ProvenanceRecord.build(
                    scope=ProvenanceScope.PAPER,
                    course_id=paper.course_id,
                    document_id=document_id,
                    paper_id=paper.paper_id,
                    created_at=now,
                    source_id=document.source_id,
                    source_document_sha256=document.content_sha256,
                    notes=(
                        "paper_label is derived from a local compiled question id; it is not "
                        "an official paper, booklet, set or language code"
                    ),
                    evidence={
                        **base_evidence,
                        "local_paper_label": paper.paper_label,
                        "derived_paper_label_from_local_source_id": True,
                        "local_question_count": paper.question_count,
                    },
                )
            )

        rows = self.session.execute(
            select(ExamQuestionDetailRow, ExamAnswerRow)
            .join(QuestionRow, QuestionRow.question_id == ExamQuestionDetailRow.question_id)
            .outerjoin(
                ExamAnswerRow,
                ExamAnswerRow.question_id == ExamQuestionDetailRow.question_id,
            )
            .where(QuestionRow.document_id == document_id)
            .order_by(ExamQuestionDetailRow.source_row, ExamQuestionDetailRow.question_id)
        ).all()
        for detail, answer in rows:
            records.append(
                ProvenanceRecord.build(
                    scope=ProvenanceScope.QUESTION,
                    course_id=detail.course_id,
                    document_id=document_id,
                    paper_id=detail.paper_id,
                    question_id=detail.question_id,
                    created_at=now,
                    source_id=document.source_id,
                    source_document_sha256=document.content_sha256,
                    original_question_number=detail.original_question_number,
                    extraction_method=extraction_method,
                    notes=(
                        "question text comes from the local compiled corpus; no exact "
                        "official wording/option identity has been established"
                    ),
                    evidence={
                        **base_evidence,
                        "local_source_question_id": detail.source_question_id,
                        "local_source_question_id_is_official": False,
                        "local_source_row": detail.source_row,
                        "local_question_type": detail.question_type,
                        "local_paper_id": detail.paper_id,
                    },
                )
            )
            if answer is None:
                continue
            records.append(
                ProvenanceRecord.build(
                    scope=ProvenanceScope.ANSWER,
                    course_id=answer.course_id,
                    document_id=document_id,
                    paper_id=detail.paper_id,
                    question_id=detail.question_id,
                    answer_id=answer.answer_id,
                    created_at=now,
                    source_id=document.source_id,
                    source_document_sha256=document.content_sha256,
                    extraction_method=extraction_method,
                    identity_status=IdentityStatus.UNRESOLVED,
                    key_identity_status=IdentityStatus.UNRESOLVED,
                    source_classification=SourceClassification.UNKNOWN,
                    notes=(
                        "answer text is paired from the same local CSV row; no independent "
                        "verification and no official final-key mapping is implied"
                    ),
                    evidence={
                        **base_evidence,
                        "local_source_answer_id": answer.source_answer_id,
                        "local_match_method": answer.match_method,
                        "local_match_confidence": answer.confidence,
                        "local_answer_type": answer.answer_type,
                        "local_paper_id": detail.paper_id,
                    },
                )
            )

        for record in records:
            self.session.add(_provenance_row(record))
        self.session.flush()
        return len(records)

    def add_provenance(self, records: list[ProvenanceRecord]) -> int:
        """Upsert explicit provenance records supplied by a future importer."""
        for record in records:
            self.session.merge(_provenance_row(record))
        self.session.flush()
        return len(records)

    def provenance_counts(self) -> dict[str, Any]:
        def grouped(column: Any) -> dict[str, int]:
            rows = self.session.execute(select(column, func.count()).group_by(column)).all()
            return {str(value): int(count) for value, count in rows}

        return {
            "total": int(
                self.session.scalar(select(func.count()).select_from(ExamProvenanceRow)) or 0
            ),
            "by_scope": grouped(ExamProvenanceRow.scope),
            "by_source_classification": grouped(ExamProvenanceRow.source_classification),
            "by_identity_status": grouped(ExamProvenanceRow.identity_status),
            "by_paper_identity_status": grouped(ExamProvenanceRow.paper_identity_status),
            "by_key_identity_status": grouped(ExamProvenanceRow.key_identity_status),
        }

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
