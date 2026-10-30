from __future__ import annotations

from pathlib import Path

from exam_engine.config import IngestionLimits, Settings
from exam_engine.ingest.manifest import load_manifest
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.enums import DatasetType, ExtractionStatus
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)


def test_manifest_template_is_valid() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "datasets" / "templates" / "manifest.example.json"
    )
    manifest, base_dir = load_manifest(template)
    assert manifest.dataset.dataset_type.value == "permissioned"
    assert manifest.dataset.license
    assert base_dir.name == "templates"
    assert manifest.document_count == 3


def test_manifest_is_valid_and_synthetic(sample_manifest: Path) -> None:
    manifest, base_dir = load_manifest(sample_manifest)
    assert manifest.dataset.dataset_type is DatasetType.SYNTHETIC
    assert manifest.dataset.is_synthetic is True
    assert manifest.dataset.license
    assert manifest.dataset.retrieved_at.year == 2026
    assert len(manifest.courses) == 2
    assert manifest.document_count == 7
    for course in manifest.courses:
        for document in course.documents:
            path = base_dir / document.path
            assert path.exists(), f"missing sample file: {path}"
            assert path.stat().st_size > 0


def _document_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        import zipfile

        with zipfile.ZipFile(path) as archive:
            return archive.read("word/document.xml").decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def test_sample_files_are_labelled_synthetic(sample_manifest: Path) -> None:
    manifest, base_dir = load_manifest(sample_manifest)
    for course in manifest.courses:
        for document in course.documents:
            if document.path.endswith(".pdf"):
                continue  # binary container; asserted through extraction instead
            text = _document_text(base_dir / document.path)
            assert "SYNTHETIC" in text.upper(), document.path


def test_sample_dataset_has_expected_coverage(database, sample_manifest: Path) -> None:
    pipeline = IngestionPipeline(database, settings=Settings(limits=IngestionLimits()))
    batch = pipeline.ingest_manifest(sample_manifest)
    assert batch.ok

    with database.session() as session:
        documents = DocumentRepository(session).list_all()
        formats = {doc.format.value for doc in documents}
        assert formats == {"pdf", "docx", "txt", "csv", "json"}
        kinds = {doc.kind.value for doc in documents}
        assert kinds == {"question_paper", "syllabus", "notes", "question_bank"}
        for doc in documents:
            assert doc.extraction_status in (ExtractionStatus.OK, ExtractionStatus.EMPTY)
            assert doc.quality_score > 0.5
            assert doc.content_sha256
            assert len(doc.content_sha256) == 64
            assert doc.source_id == "demo-synthetic-2024"

        questions = QuestionRepository(session).list_all()
        assert questions
        for question in questions:
            assert question.text.strip()
            assert question.course_id
            assert 0.0 <= question.confidence <= 1.0
            if question.marks is not None:
                assert 0 < question.marks <= 100
            if question.page_number is not None:
                document = DocumentRepository(session).get(question.document_id)
                assert document is not None
                assert 1 <= question.page_number <= document.page_count

        units = SyllabusRepository(session).count_units_by_course("demo-cs201-2024-v1")
        assert units == 5


def test_document_ids_are_stable_across_imports(database, sample_manifest: Path) -> None:
    pipeline = IngestionPipeline(database, settings=Settings(limits=IngestionLimits()))
    first = pipeline.ingest_manifest(sample_manifest)
    first_ids = {
        result.source_path: result.document.document_id
        for result in first.results
        if result.document
    }
    with database.session() as session:
        first_questions = {q.question_id for q in QuestionRepository(session).list_all()}

    second = pipeline.ingest_manifest(sample_manifest, duplicate_policy=_replace())
    second_ids = {
        result.source_path: result.document.document_id
        for result in second.results
        if result.document
    }
    assert first_ids == second_ids
    with database.session() as session:
        second_questions = {q.question_id for q in QuestionRepository(session).list_all()}
    assert first_questions == second_questions


def test_courses_remain_distinct(database, sample_manifest: Path) -> None:
    IngestionPipeline(database, settings=Settings()).ingest_manifest(sample_manifest)
    with database.session() as session:
        courses = {c.course_id: c for c in CourseRepository(session).list()}
    assert courses["demo-cs201-2024-v1"].course_code == "CS201"
    assert courses["demo-phy101-2024-v1"].course_code == "PHY101"
    assert courses["demo-cs201-2024-v1"].department == "Computer Science and Engineering"
    assert courses["demo-phy101-2024-v1"].department == "Basic Sciences"


def _replace():
    from exam_engine.models.enums import DuplicatePolicy

    return DuplicatePolicy.REPLACE
