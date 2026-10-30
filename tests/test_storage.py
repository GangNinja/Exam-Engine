from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from exam_engine.models.document import DocumentRecord, PageRecord, content_digest
from exam_engine.models.enums import DocumentKind, ExtractionMethod, IngestionStatus, SourceFormat
from exam_engine.models.ingestion import IngestionResult
from exam_engine.models.question import QuestionRecord
from exam_engine.models.syllabus import SyllabusRecord, SyllabusUnitRecord
from exam_engine.storage.mappers import iso
from exam_engine.storage.migrator import (
    MIGRATIONS_DIR,
    MigrationError,
    applied_migrations,
    migrate,
    split_sql_statements,
)
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    RunRepository,
    SyllabusRepository,
)
from exam_engine.storage.schema import Base
from tests.support import make_course, make_source


def document(document_id: str = "doc_1") -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        course_id="test-cs101-2024-v1",
        source_id="test-synthetic",
        kind=DocumentKind.QUESTION_PAPER,
        format=SourceFormat.TXT,
        title="Paper",
        original_filename="paper.txt",
        content_sha256=content_digest(document_id.encode()),
        byte_size=10,
        page_count=1,
        char_count=30,
        pipeline_version="test",
    )


def test_orm_schema_matches_migrations(database) -> None:
    inspector = inspect(database.engine)
    db_tables = set(inspector.get_table_names()) - {"schema_migrations"}
    orm_tables = set(Base.metadata.tables)
    assert db_tables == orm_tables
    for table in sorted(orm_tables):
        db_columns = {column["name"] for column in inspector.get_columns(table)}
        orm_columns = {column.name for column in Base.metadata.tables[table].columns}
        assert db_columns == orm_columns, f"column drift in {table}"
    graph_indexes = {index["name"] for index in inspector.get_indexes("graph_nodes")}
    assert "idx_graph_nodes_entity" in graph_indexes


def test_migrations_are_idempotent(database) -> None:
    assert database.applied_migrations == [
        "0001_initial.sql",
        "0002_question_unit.sql",
        "0003_nlp_foundation.sql",
        "0004_question_understanding.sql",
        "0005_knowledge_graph.sql",
        "0006_graph_entity_index.sql",
        "0007_generated_questions.sql",
        "0008_advanced_assessment.sql",
        "0009_response_identified.sql",
    ]
    assert migrate(database.engine) == []
    assert "0003_nlp_foundation.sql" in applied_migrations(database.engine)


def test_upgrade_from_initial_schema_adds_question_unit(tmp_path: Path) -> None:
    """A database created by 0001 only must upgrade cleanly to 0002."""
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    (old_migrations / "0001_initial.sql").write_text(
        (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    db_path = tmp_path / "upgrade.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    assert migrate(engine, old_migrations) == ["0001_initial.sql"]

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO courses (course_id, course_code, title, institution_code, "
            "institution_name, metadata_json, created_at, updated_at) "
            "VALUES ('c','C','T','i','I','{}','2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.exec_driver_sql(
            "INSERT INTO questions (question_id, document_id, course_id, text, "
            "confidence, needs_review, fingerprint, uncertainties_json, metadata_json) "
            "VALUES ('q','d','c','text',0.9,0,'fp','[]','{}')"
        )

    assert migrate(engine) == [
        "0002_question_unit.sql",
        "0003_nlp_foundation.sql",
        "0004_question_understanding.sql",
        "0005_knowledge_graph.sql",
        "0006_graph_entity_index.sql",
        "0007_generated_questions.sql",
        "0008_advanced_assessment.sql",
        "0009_response_identified.sql",
    ]
    with engine.connect() as conn:
        columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(questions)")]
        unit = conn.exec_driver_sql(
            "SELECT unit FROM questions WHERE question_id = 'q'"
        ).scalar_one()
    assert "unit" in columns
    assert unit is None
    engine.dispose()


def test_modified_migration_is_detected(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    script = migrations / "0001_test.sql"
    script.write_text("CREATE TABLE demo (id INTEGER PRIMARY KEY);\n", encoding="utf-8")
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    try:
        assert migrate(engine, migrations) == ["0001_test.sql"]
        script.write_text(
            "CREATE TABLE demo (id INTEGER PRIMARY KEY, extra TEXT);\n", encoding="utf-8"
        )
        with pytest.raises(MigrationError):
            migrate(engine, migrations)
    finally:
        engine.dispose()


def test_sql_statement_splitter_keeps_comments_with_their_ddl() -> None:
    script = (
        "-- header comment\n"
        "CREATE TABLE a (id INTEGER);\n"
        "-- second table\n"
        "CREATE TABLE b (note TEXT DEFAULT 'x;y');\n"
    )
    statements = split_sql_statements(script)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
    assert "'x;y'" in statements[1]


def test_course_upsert_reports_creation_and_updates(database) -> None:
    course = make_course()
    with database.transaction() as session:
        repo = CourseRepository(session)
        created, is_new = repo.upsert(course)
        assert is_new is True
        assert created.course_id == course.course_id

    updated = course.model_copy(update={"title": "Renamed course"})
    with database.transaction() as session:
        repo = CourseRepository(session)
        stored, is_new = repo.upsert(updated)
        assert is_new is False
        assert stored.title == "Renamed course"
        assert len(repo.list()) == 1


def test_course_created_at_survives_reimport(database) -> None:
    with database.transaction() as session:
        CourseRepository(session).upsert(make_course())
    with database.session() as session:
        first = CourseRepository(session).get("test-cs101-2024-v1")
    assert first is not None

    with database.transaction() as session:
        CourseRepository(session).upsert(make_course().model_copy(update={"title": "Renamed"}))

    with database.session() as session:
        stored = CourseRepository(session).get("test-cs101-2024-v1")
    assert stored is not None
    assert stored.title == "Renamed"
    assert stored.created_at == first.created_at
    assert stored.updated_at >= first.updated_at


def test_conflicting_offering_detection(database) -> None:
    with database.transaction() as session:
        CourseRepository(session).upsert(make_course())

    sibling = make_course(course_id="other-slug", course_code="CS101")
    with database.session() as session:
        conflicts = CourseRepository(session).conflicting_offerings(sibling)
    assert conflicts == ["test-cs101-2024-v1"]


def test_document_pages_questions_and_syllabus_roundtrip(database) -> None:
    with database.transaction() as session:
        CourseRepository(session).upsert(make_course())
        DatasetSourceRepository(session).upsert(make_source())

    doc = document()
    pages = [
        PageRecord(page_number=1, text="1. Define a stack.", method=ExtractionMethod.PLAIN_TEXT),
        PageRecord(page_number=2, text="2. Define a queue.", method=ExtractionMethod.OCR),
    ]
    questions = [
        QuestionRecord(
            question_id="q_2",
            document_id=doc.document_id,
            course_id=doc.course_id,
            text="2. Define a queue.",
            question_number="2",
            number_sort=2,
            page_number=2,
            source_locator="page 2",
            confidence=0.8,
        ),
        QuestionRecord(
            question_id="q_1",
            document_id=doc.document_id,
            course_id=doc.course_id,
            text="1. Define a stack.",
            question_number="1",
            number_sort=1,
            page_number=1,
            source_locator="page 1",
            confidence=0.9,
        ),
    ]
    syllabus = SyllabusRecord(
        syllabus_id="syl_1",
        course_id=doc.course_id,
        document_id=doc.document_id,
        source_id=doc.source_id,
        title="Syllabus",
        units=[
            SyllabusUnitRecord(
                unit_id="unit_1",
                unit_label="Unit 1",
                unit_number=1,
                title="Foundations",
                hours=8.0,
                topics=["Stacks", "Queues"],
                raw_text="Unit 1: Foundations",
                confidence=0.9,
            )
        ],
        confidence=0.9,
    )

    with database.transaction() as session:
        documents = DocumentRepository(session)
        documents.upsert(doc)
        documents.replace_pages(doc.document_id, pages)
        QuestionRepository(session).replace_for_document(doc.document_id, questions)
        SyllabusRepository(session).replace(syllabus)

    with database.session() as session:
        documents = DocumentRepository(session)
        stored = documents.get(doc.document_id)
        assert stored is not None
        assert stored.title == "Paper"
        stored_pages = documents.list_pages(doc.document_id)
        assert [p.text for p in stored_pages] == ["1. Define a stack.", "2. Define a queue."]
        assert stored_pages[1].method is ExtractionMethod.OCR

        repo = QuestionRepository(session)
        assert [q.question_id for q in repo.list_by_document(doc.document_id)] == ["q_1", "q_2"]
        assert repo.count_by_course(doc.course_id) == 2
        assert repo.fingerprint_index(doc.course_id)

        syllabus_repo = SyllabusRepository(session)
        loaded = syllabus_repo.get_by_course(doc.course_id)
        assert len(loaded) == 1
        assert loaded[0].units[0].topics == ["Stacks", "Queues"]
        assert loaded[0].units[0].hours == 8.0
        assert syllabus_repo.count_units_by_course(doc.course_id) == 1


def test_deleting_document_cascades(database) -> None:
    with database.transaction() as session:
        CourseRepository(session).upsert(make_course())
        DatasetSourceRepository(session).upsert(make_source())
        DocumentRepository(session).upsert(document())
        DocumentRepository(session).replace_pages("doc_1", [PageRecord(page_number=1, text="text")])
        QuestionRepository(session).replace_for_document(
            "doc_1",
            [
                QuestionRecord(
                    question_id="q", document_id="doc_1", course_id="test-cs101-2024-v1", text="q"
                )
            ],
        )

    with database.transaction() as session:
        DocumentRepository(session).delete("doc_1")

    with database.session() as session:
        assert DocumentRepository(session).get("doc_1") is None
        assert DocumentRepository(session).list_pages("doc_1") == []
        assert QuestionRepository(session).count_by_document("doc_1") == 0


def test_foreign_keys_are_enforced(database) -> None:
    with pytest.raises(IntegrityError), database.transaction() as session:
        DocumentRepository(session).upsert(document("doc_orphan"))


def test_run_persistence_roundtrip(database) -> None:
    started = datetime.now(UTC)
    results = [
        IngestionResult(
            source_path="a.txt",
            course_id="c1",
            status=IngestionStatus.INGESTED,
            question_count=2,
            duration_ms=5,
        ),
        IngestionResult(
            source_path="b.txt",
            course_id="c1",
            status=IngestionStatus.FAILED,
        ),
    ]
    with database.transaction() as session:
        runs = RunRepository(session)
        runs.create("run_1", started_at=started, manifest_path="m.json", source_id="s")
        runs.save("run_1", finished_at=datetime.now(UTC), results=results)

    with database.session() as session:
        runs = RunRepository(session)
        listed = runs.list_runs()
        assert listed[0]["run_id"] == "run_1"
        assert listed[0]["status"] == "partial"
        assert listed[0]["totals"]["failed"] == 1
        loaded = runs.get_run("run_1")
        assert loaded is not None
        assert len(loaded.results) == 2
        assert loaded.results[0].question_count == 2
        assert loaded.results[1].status is IngestionStatus.FAILED


def test_raw_sql_can_read_stored_timestamps(database) -> None:
    course = make_course()
    with database.transaction() as session:
        CourseRepository(session).upsert(course)
    with database.session() as session:
        raw = session.execute(text("SELECT created_at FROM courses")).scalar_one()
    assert raw == iso(course.created_at)
    assert datetime.fromisoformat(raw).tzinfo is not None
