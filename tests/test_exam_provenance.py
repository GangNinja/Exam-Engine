"""Phase 14.2 provenance import, migration and retention tests.

The fixtures use synthetic NEET-style ids that contain ``Code50`` on purpose:
a local label must never become official paper metadata.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from exam_engine.config import Settings
from exam_engine.datasets.service import ExamDatasetService
from exam_engine.models.provenance import (
    IdentityStatus,
    ProvenanceRecord,
    ProvenanceScope,
    SourceClassification,
)
from exam_engine.storage.db import open_database
from exam_engine.storage.exam_repository import ExamDatasetRepository
from exam_engine.storage.migrator import applied_migrations, migrate
from exam_engine.storage.schema import (
    ExamAnswerRow,
    ExamPaperRow,
    ExamProvenanceRow,
    ExamQuestionDetailRow,
)

COLUMNS = [
    "question_id",
    "original_question_number",
    "question_text",
    "question_type",
    "options",
    "marks",
    "answer_id",
    "answer_text",
    "answer_type",
    "solution_steps",
]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)


def _fixture(root: Path) -> Path:
    _write_csv(
        root / "NEET" / "Physics.csv",
        [
            [
                "NEET2026_ReExam_Code50_Q01",
                "1",
                "A body starts from rest with acceleration 2 m/s2 for 3 s. Find its speed.",
                "Numerical answer",
                "",
                "4",
                "NEET2026_ReExam_Code50_Q01",
                "6 m/s",
                "Numeric",
                "v = u + at = 6 m/s",
            ],
            [
                "NEET2026_ReExam_Code50_Q02",
                "2",
                "Which quantity is a vector?",
                "MCQ",
                "A) Speed B) Velocity C) Mass D) Time",
                "4",
                "NEET2026_ReExam_Code50_Q02",
                "B",
                "MCQ option",
                "",
            ],
        ],
    )
    _write_csv(
        root / "NEET" / "Chemistry.csv",
        [
            [
                "NEET2026_ReExam_Code50_Q03",
                "3",
                "Which orbital has the lowest energy in a hydrogen atom?",
                "MCQ",
                "A) 2s B) 2p C) 3s D) 3p",
                "4",
                "NEET2026_ReExam_Code50_Q03",
                "A",
                "MCQ option",
                "",
            ]
        ],
    )
    _write_csv(
        root / "NEET" / "Biology.csv",
        [
            [
                "NEET2026_ReExam_Code50_Q04",
                "4",
                "The number of vertebrae in a human is ________.",
                "MCQ",
                "(1) 7 (2) 12 (3) 26 (4) 206",
                "4",
                "NEET2026_ReExam_Code50_Q04",
                "3",
                "MCQ option",
                "",
            ]
        ],
    )
    return root


def _imported(tmp_path: Path) -> tuple[object, ExamDatasetService]:
    root = _fixture(tmp_path)
    db_path = tmp_path / "exam.db"
    settings = Settings.from_env().with_db(db_path)
    database = open_database(db_path)
    service = ExamDatasetService(database, settings)
    report = service.import_dataset(root, output_dir=tmp_path / "out", with_syllabus=False)
    assert report.status == "ok"
    return database, service


def test_projection_records_explicit_unknown_provenance(tmp_path: Path) -> None:
    database, _ = _imported(tmp_path)
    try:
        with database.session() as session:  # type: ignore[attr-defined]
            rows = session.scalars(select(ExamProvenanceRow)).all()
        assert len(rows) == 14
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.scope] = counts.get(row.scope, 0) + 1
        assert counts == {"document": 3, "paper": 3, "question": 4, "answer": 4}

        for row in rows:
            assert row.source_url is None
            assert row.source_publisher is None
            assert row.retrieved_at is None
            assert row.examining_authority is None
            assert row.examination_name is None
            assert row.exam_year is None
            assert row.exam_session is None
            assert row.paper_code is None
            assert row.set_code is None
            assert row.region is None
            assert row.language is None
            assert row.source_classification == "unknown"
            assert row.identity_status == "unresolved"
            assert row.paper_identity_status == "unresolved"
            if row.scope == "answer":
                assert row.key_identity_status == "unresolved"
            else:
                assert row.key_identity_status == "not_applicable"

        papers = [row for row in rows if row.scope == "paper"]
        for paper in papers:
            evidence = json.loads(paper.evidence_json)
            assert "Code50" in evidence["local_paper_label"]
            assert evidence["derived_paper_label_from_local_source_id"] is True
            # The local label is evidence of a local id only, never an official code.
            assert paper.paper_code is None
            assert paper.set_code is None

        questions = [row for row in rows if row.scope == "question"]
        for question in questions:
            assert question.original_question_id is None
            assert question.original_option_ids_json == "[]"
            evidence = json.loads(question.evidence_json)
            assert "Code50" in evidence["local_source_question_id"]
            assert evidence["local_source_question_id_is_official"] is False

        documents = [row for row in rows if row.scope == "document"]
        assert all(len(document.source_document_sha256 or "") == 64 for document in documents)
        assert all(document.extraction_method == "structured" for document in documents)

        answers = [row for row in rows if row.scope == "answer"]
        assert all(json.loads(row.evidence_json)["local_match_method"] for row in answers)
    finally:
        database.close()  # type: ignore[attr-defined]


def _official_answer(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "document_id": "doc_1",
        "paper_id": "pap_1",
        "question_id": "q_1",
        "answer_id": "ans_1",
        "source_url": "https://example.gov.in/key.pdf",
        "examining_authority": "National Testing Agency",
        "examination_name": "NEET (UG) 2026 Re-Examination",
        "exam_year": 2026,
        "paper_code": "50",
        "set_code": "50",
        "retrieved_at": date(2026, 7, 16),
        "source_document_sha256": "a" * 64,
        "source_classification": SourceClassification.OFFICIAL,
        "identity_status": IdentityStatus.CONFIRMED,
        "paper_identity_status": IdentityStatus.CONFIRMED,
        "key_identity_status": IdentityStatus.CONFIRMED,
        "evidence": {
            "answer_key_source_url": "https://example.gov.in/key.pdf",
            "answer_key_sha256": "b" * 64,
        },
    }
    fields.update(overrides)
    return fields


def _official_record(**overrides: object) -> ProvenanceRecord:
    return ProvenanceRecord.build(
        scope=ProvenanceScope.ANSWER,
        course_id="neet-physics",
        created_at=datetime.now(UTC),
        **_official_answer(**overrides),
    )


def test_official_promotion_requires_paper_and_key_identity() -> None:
    assert _official_record().is_officially_promotable
    assert not _official_record(
        key_identity_status=IdentityStatus.UNRESOLVED
    ).is_officially_promotable
    assert not _official_record(
        paper_identity_status=IdentityStatus.UNRESOLVED
    ).is_officially_promotable
    assert not _official_record(
        source_classification=SourceClassification.THIRD_PARTY
    ).is_officially_promotable
    assert not _official_record(
        source_classification=SourceClassification.UNKNOWN,
        source_url=None,
    ).is_officially_promotable


def test_neet_final_key_without_paper_identity_cannot_promote() -> None:
    """A confirmed official key alone is not enough to promote a NEET answer."""
    record = _official_record(
        paper_identity_status=IdentityStatus.UNRESOLVED,
        paper_code=None,
        set_code=None,
    )
    assert record.key_identity_status is IdentityStatus.CONFIRMED
    assert record.identity_status is IdentityStatus.CONFIRMED
    assert record.is_officially_promotable is False


def test_official_claims_without_evidence_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _official_record(source_url=None)
    with pytest.raises(ValidationError):
        _official_record(paper_code=None, set_code=None)
    with pytest.raises(ValidationError):
        _official_record(source_document_sha256=None)
    with pytest.raises(ValidationError):
        _official_record(evidence={"answer_key_source_url": "https://example.gov.in/key.pdf"})
    with pytest.raises(ValidationError):
        _official_record(retrieved_at=None)


def test_unknown_provenance_stays_unknown_by_default() -> None:
    record = ProvenanceRecord.build(
        scope=ProvenanceScope.QUESTION,
        course_id="neet-physics",
        document_id="doc_1",
        paper_id="pap_1",
        question_id="q_1",
        created_at=datetime.now(UTC),
    )
    assert record.source_url is None
    assert record.examining_authority is None
    assert record.paper_code is None
    assert record.set_code is None
    assert record.exam_year is None
    assert record.language is None
    assert record.source_classification is SourceClassification.UNKNOWN
    assert record.identity_status is IdentityStatus.UNRESOLVED
    assert record.paper_identity_status is IdentityStatus.UNRESOLVED
    assert record.is_officially_promotable is False


def test_provenance_rebuild_is_idempotent_and_scoped(tmp_path: Path) -> None:
    database, _ = _imported(tmp_path)
    try:
        with database.session() as session:  # type: ignore[attr-defined]
            documents = session.scalars(
                select(ExamProvenanceRow).where(ExamProvenanceRow.scope == "document")
            ).all()
            physics_doc = next(
                row.document_id for row in documents if row.course_id == "neet-physics"
            )
            other_doc = next(
                row.document_id for row in documents if row.course_id != "neet-physics"
            )
            others_before = sorted(
                session.scalars(
                    select(ExamProvenanceRow.provenance_id).where(
                        ExamProvenanceRow.document_id == other_doc
                    )
                ).all()
            )
            total_before = session.scalar(select(func.count()).select_from(ExamProvenanceRow))

        with database.transaction() as session:  # type: ignore[attr-defined]
            first = ExamDatasetRepository(session).replace_document_provenance(physics_doc)
        with database.transaction() as session:  # type: ignore[attr-defined]
            second = ExamDatasetRepository(session).replace_document_provenance(physics_doc)

        assert first == second
        with database.session() as session:  # type: ignore[attr-defined]
            others_after = sorted(
                session.scalars(
                    select(ExamProvenanceRow.provenance_id).where(
                        ExamProvenanceRow.document_id == other_doc
                    )
                ).all()
            )
            total_after = session.scalar(select(func.count()).select_from(ExamProvenanceRow))
        assert total_after == total_before
        assert others_after == others_before
    finally:
        database.close()  # type: ignore[attr-defined]


def test_explicit_third_party_provenance_is_retained_without_promotion(tmp_path: Path) -> None:
    database, service = _imported(tmp_path)
    try:
        with database.session() as session:  # type: ignore[attr-defined]
            paper = session.scalars(
                select(ExamProvenanceRow).where(ExamProvenanceRow.scope == "paper")
            ).first()
            assert paper is not None
            course_id, document_id, paper_id = paper.course_id, paper.document_id, paper.paper_id

        record = ProvenanceRecord.build(
            scope=ProvenanceScope.PAPER,
            course_id=course_id,
            document_id=document_id,
            paper_id=paper_id,
            created_at=datetime.now(UTC),
            source_url="https://example.org/coaching-paper.pdf",
            source_publisher="Coaching publisher",
            retrieved_at=date(2026, 6, 21),
            source_document_sha256="c" * 64,
            source_classification=SourceClassification.THIRD_PARTY,
            identity_status=IdentityStatus.CONFIRMED,
            paper_identity_status=IdentityStatus.PARTIAL,
            evidence={"retrieved_for": "provenance test"},
        )
        assert service.record_provenance([record]) == 1

        with database.session() as session:  # type: ignore[attr-defined]
            row = session.get(ExamProvenanceRow, record.provenance_id)
            assert row is not None
            assert row.source_classification == "third_party"
            assert row.source_url == "https://example.org/coaching-paper.pdf"
            assert row.paper_code is None
            assert json.loads(row.evidence_json)["retrieved_for"] == "provenance test"
    finally:
        database.close()  # type: ignore[attr-defined]


def test_provenance_rebuild_keeps_projected_and_historical_counts(tmp_path: Path) -> None:
    database, _ = _imported(tmp_path)
    try:
        def counts() -> dict[str, int]:
            with database.session() as session:  # type: ignore[attr-defined]
                return {
                    "papers": int(
                        session.scalar(select(func.count()).select_from(ExamPaperRow)) or 0
                    ),
                    "questions": int(
                        session.scalar(select(func.count()).select_from(ExamQuestionDetailRow)) or 0
                    ),
                    "answers": int(
                        session.scalar(select(func.count()).select_from(ExamAnswerRow)) or 0
                    ),
                    "provenance": int(
                        session.scalar(select(func.count()).select_from(ExamProvenanceRow)) or 0
                    ),
                }

        before = counts()
        with database.session() as session:  # type: ignore[attr-defined]
            document_ids = list(
                session.scalars(select(ExamProvenanceRow.document_id).distinct()).all()
            )
        for document_id in document_ids:
            if document_id is None:
                continue
            with database.transaction() as session:  # type: ignore[attr-defined]
                ExamDatasetRepository(session).replace_document_provenance(document_id)
        assert counts() == before
    finally:
        database.close()  # type: ignore[attr-defined]


def test_migration_adds_provenance_without_removing_recovery_evidence(tmp_path: Path) -> None:
    database = open_database(tmp_path / "migrate.db")
    try:
        with database.engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(exam_provenance)")
            }
            quick_check = connection.exec_driver_sql("PRAGMA quick_check").scalar()
        assert "exam_provenance" in tables
        assert {
            "documents",
            "questions",
            "exam_papers",
            "exam_answers",
            "recovery_runs",
            "recovery_answer_candidates",
            "recovery_verifications",
            "recovery_retrieval_cache",
        } <= tables
        assert {
            "provenance_id",
            "scope",
            "paper_code",
            "set_code",
            "identity_status",
            "paper_identity_status",
            "key_identity_status",
            "evidence_json",
        } <= columns
        assert quick_check == "ok"
        assert "0016_exam_provenance.sql" in applied_migrations(database.engine)
        assert migrate(database.engine) == []
    finally:
        database.close()
