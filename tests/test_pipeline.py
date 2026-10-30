from __future__ import annotations

import json
from pathlib import Path

import pytest

from exam_engine.config import IngestionLimits, Settings
from exam_engine.ingest.ocr import get_ocr_provider, register_ocr_provider
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.enums import (
    DocumentKind,
    DuplicatePolicy,
    ExtractionMethod,
    ExtractionStatus,
    IngestionStatus,
)
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    RunRepository,
    SyllabusRepository,
)
from tests.support import (
    StubOCRProvider,
    make_course,
    make_source,
    write_docx,
    write_scanned_pdf,
)

STUB = StubOCRProvider()
register_ocr_provider("stub", lambda: STUB)

DATASET = {
    "source_id": "test-synthetic",
    "name": "Test synthetic dataset",
    "dataset_type": "synthetic",
    "retrieved_at": "2026-01-01",
    "license": "CC0-1.0",
    "publisher": "test-suite",
}


def write_manifest(tmp_path: Path, courses: list[dict], dataset: dict | None = None) -> Path:
    manifest = {
        "version": 1,
        "dataset": dataset or DATASET,
        "courses": courses,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def course_block(course_id: str, documents: list[dict], **overrides) -> dict:
    block = {
        "course_id": course_id,
        "course_code": course_id.split("-")[1].upper(),
        "title": f"Course {course_id}",
        "institution_code": "test-inst",
        "institution_name": "Test Institute",
        "academic_year": "2024-25",
        "syllabus_version": "v1",
        "documents": documents,
    }
    block.update(overrides)
    return block


def pipeline(
    database, limits: IngestionLimits | None = None, ocr: str = "none"
) -> IngestionPipeline:
    settings = Settings(limits=limits or IngestionLimits())
    return IngestionPipeline(database, settings=settings, ocr_provider=ocr)


# --- sample dataset ---------------------------------------------------------


def test_sample_dataset_ingests_two_courses(database, sample_manifest: Path) -> None:
    batch = pipeline(database).ingest_manifest(sample_manifest)
    assert batch.ok is True
    assert batch.totals["documents"] == 7
    assert batch.totals["failed"] == 0
    assert batch.totals["questions"] == 26
    assert batch.totals["syllabus_units"] == 8

    with database.session() as session:
        courses = CourseRepository(session).list()
        assert [c.course_id for c in courses] == [
            "demo-cs201-2024-v1",
            "demo-phy101-2024-v1",
        ]
        source = DatasetSourceRepository(session).get("demo-synthetic-2024")
        assert source is not None
        assert source.is_synthetic is True
        assert source.license == "CC0-1.0"

        documents = DocumentRepository(session)
        cs_docs = documents.list_by_course("demo-cs201-2024-v1")
        phy_docs = documents.list_by_course("demo-phy101-2024-v1")
        assert len(cs_docs) == 4
        assert len(phy_docs) == 3
        for doc in cs_docs + phy_docs:
            assert doc.course_id in ("demo-cs201-2024-v1", "demo-phy101-2024-v1")
            assert doc.source_id == "demo-synthetic-2024"
            assert doc.relative_path is not None
            assert not Path(doc.relative_path).is_absolute()

        questions = QuestionRepository(session)
        cs_questions = questions.list_by_course("demo-cs201-2024-v1")
        phy_questions = questions.list_by_course("demo-phy101-2024-v1")
        assert len(cs_questions) == 15
        assert len(phy_questions) == 11
        assert all(q.course_id == "demo-cs201-2024-v1" for q in cs_questions)
        assert all(q.document_id for q in cs_questions)

        pdf_questions = [q for q in cs_questions if q.page_number is not None]
        assert pdf_questions
        assert all("page" in (q.source_locator or "") for q in pdf_questions)
        assert any(q.marks == 10 for q in pdf_questions)

        bank_questions = [q for q in cs_questions if (q.source_locator or "").startswith("row")]
        assert len(bank_questions) == 8
        assert any(q.marks is None for q in bank_questions)

        syllabi = SyllabusRepository(session).get_by_course("demo-cs201-2024-v1")
        assert len(syllabi) == 1
        units = syllabi[0].units
        assert len(units) == 5
        assert units[0].hours == 8.0
        assert units[0].topics


def test_sample_reimport_is_idempotent(database, sample_manifest: Path) -> None:
    first = pipeline(database).ingest_manifest(sample_manifest)
    assert first.totals["ingested"] == 7

    second = pipeline(database).ingest_manifest(sample_manifest)
    assert second.totals["skipped_duplicate"] == 7
    assert second.totals["ingested"] == 0
    assert second.totals["questions"] == 0
    assert second.ok is True

    with database.session() as session:
        assert QuestionRepository(session).count_by_course("demo-cs201-2024-v1") == 15
        assert DocumentRepository(session).count_by_course("demo-cs201-2024-v1") == 4


def test_replace_policy_swaps_records_without_duplication(database, sample_manifest: Path) -> None:
    pipeline(database).ingest_manifest(sample_manifest)
    replaced = pipeline(database).ingest_manifest(
        sample_manifest, duplicate_policy=DuplicatePolicy.REPLACE
    )
    assert replaced.totals["ingested"] == 7
    assert all(result.status is IngestionStatus.REPLACED for result in replaced.results)
    with database.session() as session:
        assert DocumentRepository(session).count_by_course("demo-cs201-2024-v1") == 4
        assert QuestionRepository(session).count_by_course("demo-cs201-2024-v1") == 15
        assert SyllabusRepository(session).count_units_by_course("demo-cs201-2024-v1") == 5


def test_fail_policy_reports_duplicate(database, sample_manifest: Path) -> None:
    pipeline(database).ingest_manifest(sample_manifest)
    batch = pipeline(database).ingest_manifest(
        sample_manifest, duplicate_policy=DuplicatePolicy.FAIL
    )
    assert batch.ok is False
    assert batch.totals["failed"] == 7
    codes = {error.code for result in batch.results for error in result.errors}
    assert codes == {"duplicate_document"}


def test_dry_run_writes_nothing(database, sample_manifest: Path) -> None:
    batch = pipeline(database).ingest_manifest(sample_manifest, dry_run=True)
    assert batch.totals["ingested"] == 7
    assert batch.totals["questions"] == 26
    with database.session() as session:
        assert CourseRepository(session).list() == []
        assert DocumentRepository(session).list_all() == []
        assert RunRepository(session).list_runs() == []


# --- resilience -------------------------------------------------------------


def test_partial_failure_continues_batch(database, tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text(
        "1. Define a stack. [5 marks]\n\n2. Define a queue. [5 marks]\n", encoding="utf-8"
    )
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4 but truncated garbage")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")

    manifest = write_manifest(
        tmp_path,
        [
            course_block(
                "test-cs101-2024-v1",
                [
                    {"path": "good.txt", "kind": "question_paper"},
                    {"path": "missing.txt", "kind": "notes"},
                    {"path": "corrupt.pdf", "kind": "question_paper"},
                    {"path": "empty.txt", "kind": "notes"},
                ],
            )
        ],
    )
    batch = pipeline(database).ingest_manifest(manifest)
    statuses = {Path(r.source_path).name: r.status for r in batch.results}
    assert statuses["good.txt"] is IngestionStatus.INGESTED
    assert statuses["missing.txt"] is IngestionStatus.FAILED
    assert statuses["corrupt.pdf"] is IngestionStatus.FAILED
    assert statuses["empty.txt"] is IngestionStatus.FAILED
    assert batch.ok is False
    assert batch.totals["ingested"] == 1

    with database.session() as session:
        run = RunRepository(session).get_run(batch.run_id)
        assert run is not None
        assert len(run.results) == 4
        assert run.results[0].question_count == 2
        assert DocumentRepository(session).count_by_course("test-cs101-2024-v1") == 1


def test_path_traversal_in_manifest_is_rejected(database, tmp_path: Path) -> None:
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("1. Secret question. [5 marks]", encoding="utf-8")
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    copy = dataset_dir / "ok.txt"
    copy.write_text("1. Fine question. [5 marks]", encoding="utf-8")
    manifest = write_manifest(
        dataset_dir,
        [
            course_block(
                "test-cs101-2024-v1",
                [
                    {"path": "ok.txt", "kind": "question_paper"},
                    {"path": "../outside-secret.txt", "kind": "notes"},
                ],
            )
        ],
    )
    batch = pipeline(database).ingest_manifest(manifest)
    by_path = {result.source_path: result for result in batch.results}
    assert by_path["../outside-secret.txt"].status is IngestionStatus.FAILED
    assert by_path["../outside-secret.txt"].errors[0].code == "path_outside_root"
    assert by_path["ok.txt"].status is IngestionStatus.INGESTED

    relaxed = pipeline(database).ingest_manifest(
        write_manifest(
            dataset_dir,
            [
                course_block(
                    "test-cs101-2024-v1",
                    [{"path": "../outside-secret.txt", "kind": "question_paper"}],
                )
            ],
        ),
        allow_external=True,
    )
    assert relaxed.results[0].status in (
        IngestionStatus.INGESTED,
        IngestionStatus.SKIPPED_DUPLICATE,
    )


def test_empty_document_status(database, tmp_path: Path) -> None:
    path = tmp_path / "blank.txt"
    path.write_text("   \n\n  ", encoding="utf-8")
    result = pipeline(database).ingest_file(
        path, course=make_course(), source=make_source(), kind=DocumentKind.NOTES
    )
    assert result.status is IngestionStatus.EMPTY
    assert result.document is not None
    assert result.document.extraction_status is ExtractionStatus.EMPTY


def test_ingest_file_with_relative_nested_path(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ingest sub/doc.txt` must resolve once, not as sub/sub/doc.txt."""
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "doc.txt").write_text("1. Define a stack. [5 marks]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = pipeline(database).ingest_file(
        Path("sub") / "doc.txt",
        course=make_course(),
        source=make_source(),
        kind=DocumentKind.QUESTION_PAPER,
    )
    assert result.status is IngestionStatus.INGESTED, result.error_codes()
    assert result.question_count == 1
    assert result.document is not None
    assert result.document.relative_path == "doc.txt"


# --- isolation --------------------------------------------------------------


def test_same_content_different_courses_is_isolated(database, tmp_path: Path) -> None:
    shared = tmp_path / "shared.txt"
    shared.write_text("1. Shared question text. [5 marks]\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            course_block("test-cs101-2024-v1", [{"path": "shared.txt", "kind": "question_paper"}]),
            course_block("test-cs102-2024-v1", [{"path": "shared.txt", "kind": "question_paper"}]),
        ],
    )
    batch = pipeline(database).ingest_manifest(manifest)
    assert batch.totals["ingested"] == 2
    assert batch.results[0].document is not None
    assert batch.results[1].document is not None
    assert batch.results[0].document.document_id != batch.results[1].document.document_id

    with database.session() as session:
        assert QuestionRepository(session).count_by_course("test-cs101-2024-v1") == 1
        assert QuestionRepository(session).count_by_course("test-cs102-2024-v1") == 1
        assert DocumentRepository(session).count_by_course("test-cs101-2024-v1") == 1


def test_course_metadata_is_preserved(database, tmp_path: Path) -> None:
    path = tmp_path / "q.txt"
    path.write_text("1. A question about vectors. [5 marks]\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            course_block(
                "test-ma101-2023-v2",
                [
                    {
                        "path": "q.txt",
                        "kind": "question_paper",
                        "academic_year": "2023-24",
                        "language": "en",
                        "metadata": {"paper": "midterm"},
                    }
                ],
                course_code="MA101",
                department="Mathematics",
                programme="B.Sc",
                academic_year="2023-24",
                syllabus_version="v2",
            )
        ],
    )
    batch = pipeline(database).ingest_manifest(manifest)
    document = batch.results[0].document
    assert document is not None
    assert document.academic_year == "2023-24"
    assert document.language == "en"
    assert document.metadata["paper"] == "midterm"

    with database.session() as session:
        course = CourseRepository(session).get("test-ma101-2023-v2")
        assert course is not None
        assert course.department == "Mathematics"
        assert course.syllabus_version == "v2"
        assert course.academic_year == "2023-24"
        assert course.course_code == "MA101"


def test_duplicate_questions_across_documents_are_flagged(database, tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    first.write_text("1. Explain binary search in detail. [5 marks]\n", encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text("1. explain  binary SEARCH in detail [5 marks]\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            course_block(
                "test-cs101-2024-v1",
                [
                    {"path": "first.txt", "kind": "question_paper"},
                    {"path": "second.txt", "kind": "question_paper"},
                ],
            )
        ],
    )
    batch = pipeline(database).ingest_manifest(manifest)
    assert batch.totals["ingested"] == 2
    with database.session() as session:
        questions = QuestionRepository(session).list_by_course("test-cs101-2024-v1")
    duplicates = [q for q in questions if q.duplicate_of]
    assert len(duplicates) == 1
    assert any(u.code == "duplicate_question" for u in duplicates[0].uncertainties)


def test_course_conflict_warns(database, tmp_path: Path) -> None:
    path = tmp_path / "q.txt"
    path.write_text("1. Question one. [5 marks]\n", encoding="utf-8")
    with database.transaction() as session:
        CourseRepository(session).upsert(make_course())
        DatasetSourceRepository(session).upsert(make_source())

    result = pipeline(database).ingest_file(
        path,
        course=make_course(course_id="different-slug"),
        source=make_source(),
        kind=DocumentKind.QUESTION_PAPER,
    )
    assert any(w.code == "course_conflict" for w in result.warnings)
    assert result.status is IngestionStatus.INGESTED


# --- OCR --------------------------------------------------------------------


def test_ocr_stub_unlocks_scanned_pdf(database, tmp_path: Path) -> None:
    path = write_scanned_pdf(tmp_path / "scan.pdf")
    result = pipeline(database, ocr="stub").ingest_file(
        path, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_PAPER
    )
    assert result.status is IngestionStatus.INGESTED
    assert result.question_count == 1
    assert STUB.calls
    with database.session() as session:
        pages = DocumentRepository(session).list_pages(result.document.document_id)
    assert pages[0].method is ExtractionMethod.OCR
    assert pages[0].ocr_attempted is True
    assert "OCR pipeline" in pages[0].text


def test_scanned_pdf_without_ocr_needs_ocr(database, tmp_path: Path) -> None:
    path = write_scanned_pdf(tmp_path / "scan.pdf")
    result = pipeline(database, ocr="none").ingest_file(
        path, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_PAPER
    )
    assert result.status is IngestionStatus.NEEDS_OCR
    assert result.document is not None
    assert result.document.extraction_status is ExtractionStatus.NEEDS_OCR
    assert any(w.code == "ocr_unavailable" for w in result.warnings)


def test_ocr_provider_is_replaceable() -> None:
    provider = get_ocr_provider("stub")
    assert provider.available() is True
    assert "OCR" in provider.extract_text(b"fake image bytes")


# --- parsing quality --------------------------------------------------------


def test_question_paper_without_markers_reports_warning(database, tmp_path: Path) -> None:
    path = tmp_path / "freeform.txt"
    path.write_text("This paper has no numbered questions at all.\n", encoding="utf-8")
    result = pipeline(database).ingest_file(
        path, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_PAPER
    )
    assert result.status is IngestionStatus.INGESTED
    assert result.question_count == 0
    assert any(w.code == "no_questions_detected" for w in result.warnings)


def test_char_limit_truncates_with_warning(database, tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    path.write_text("1. " + "word " * 500, encoding="utf-8")
    result = pipeline(database, limits=IngestionLimits(max_document_chars=100)).ingest_file(
        path, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_PAPER
    )
    assert any(w.code == "document_truncated" for w in result.warnings)
    assert result.document is not None
    assert result.document.char_count <= 100


def test_provenance_warnings_are_surfaced(database, tmp_path: Path) -> None:
    path = tmp_path / "q.txt"
    path.write_text("1. A question. [5 marks]\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [course_block("test-cs101-2024-v1", [{"path": "q.txt", "kind": "question_paper"}])],
        dataset={
            "source_id": "external-official",
            "name": "External official paper",
            "dataset_type": "official",
            "retrieved_at": "2026-02-02",
            "external_dataset": True,
            "source_url": "https://example.invalid/paper.pdf",
        },
    )
    batch = pipeline(database).ingest_manifest(manifest)
    codes = {warning.code for warning in batch.warnings}
    assert "dataset_provenance" in codes
    messages = " ".join(w.message for w in batch.warnings)
    assert "no licence" in messages
    assert "official" in messages
    result = batch.results[0]
    assert any(w.code == "dataset_provenance" for w in result.warnings)


def test_non_finite_marks_do_not_crash_the_document(database, tmp_path: Path) -> None:
    bank = tmp_path / "nan.json"
    bank.write_text(
        '{"questions": [{"number": 1, "text": "Define a stack.", "marks": NaN}]}',
        encoding="utf-8",
    )
    result = pipeline(database).ingest_file(
        bank, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_BANK
    )
    assert result.status is IngestionStatus.INGESTED
    assert result.question_count == 1
    with database.session() as session:
        questions = QuestionRepository(session).list_by_document(result.document.document_id)
    assert questions[0].marks is None


def test_unexpected_error_is_contained_per_document(database, tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "a.txt"
    first.write_text("1. Break the segmenter. [5 marks]\n", encoding="utf-8")
    second = tmp_path / "b.txt"
    second.write_text("Unstructured course notes.\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            course_block(
                "test-cs101-2024-v1",
                [
                    {"path": "a.txt", "kind": "question_paper"},
                    {"path": "b.txt", "kind": "notes"},
                ],
            )
        ],
    )

    def explode(*args, **kwargs):
        raise RuntimeError("simulated segmenter bug")

    monkeypatch.setattr("exam_engine.ingest.pipeline.segment_questions_pages", explode)
    batch = pipeline(database).ingest_manifest(manifest)
    by_name = {Path(result.source_path).name: result for result in batch.results}
    assert by_name["a.txt"].status is IngestionStatus.FAILED
    assert by_name["a.txt"].errors[0].code == "unexpected_error"
    assert by_name["b.txt"].status is IngestionStatus.INGESTED
    with database.session() as session:
        run = RunRepository(session).get_run(batch.run_id)
    assert run is not None and len(run.results) == 2


def test_bank_question_cap_is_enforced(database, tmp_path: Path) -> None:
    bank = tmp_path / "many.csv"
    rows = "\n".join(f"{i},Question number {i} about stacks,2" for i in range(1, 6))
    bank.write_text("number,text,marks\n" + rows + "\n", encoding="utf-8")
    result = pipeline(database, limits=IngestionLimits(max_questions_per_document=2)).ingest_file(
        bank, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_BANK
    )
    assert result.question_count == 2
    assert any(w.code == "question_limit_exceeded" for w in result.warnings)


def test_identical_bank_rows_collapse_honestly(database, tmp_path: Path) -> None:
    bank = tmp_path / "dup.csv"
    bank.write_text("number,text,marks\n1,Same question,2\n1,Same question,2\n", encoding="utf-8")
    result = pipeline(database).ingest_file(
        bank, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_BANK
    )
    assert result.status is IngestionStatus.INGESTED
    assert result.question_count == 1
    with database.session() as session:
        stored = QuestionRepository(session).list_by_document(result.document.document_id)
    assert len(stored) == 1
    assert any(w.code == "duplicate_question_record" for w in result.warnings)


def test_identical_syllabus_units_do_not_fail_the_document(database, tmp_path: Path) -> None:
    path = tmp_path / "syl.txt"
    path.write_text("UNIT 1: Same\nTopic A\nUNIT 1: Same\nTopic A\n", encoding="utf-8")
    result = pipeline(database).ingest_file(
        path, course=make_course(), source=make_source(), kind=DocumentKind.SYLLABUS
    )
    assert result.status is IngestionStatus.INGESTED
    assert result.syllabus_unit_count == 1
    with database.session() as session:
        syllabi = SyllabusRepository(session).get_by_course("test-cs101-2024-v1")
    assert len(syllabi) == 1
    assert any(u.code == "duplicate_syllabus_unit" for u in syllabi[0].uncertainties)


def test_docx_table_syllabus_hours_and_units(database, tmp_path: Path) -> None:
    path = write_docx(
        tmp_path / "syl.docx",
        ["Syllabus"],
        tables=[
            [
                ["Unit", "Title", "Hours"],
                ["Unit 1", "Foundations", "8"],
                ["Unit 2", "Trees", "10"],
            ]
        ],
    )
    result = pipeline(database).ingest_file(
        path, course=make_course(), source=make_source(), kind=DocumentKind.SYLLABUS
    )
    assert result.status is IngestionStatus.INGESTED
    assert result.syllabus_unit_count == 2
    with database.session() as session:
        syllabi = SyllabusRepository(session).get_by_course("test-cs101-2024-v1")
    assert [unit.hours for unit in syllabi[0].units] == [8.0, 10.0]
    assert [unit.title for unit in syllabi[0].units] == ["Foundations", "Trees"]


def test_question_unit_roundtrips_through_storage(database, tmp_path: Path) -> None:
    bank = tmp_path / "units.csv"
    bank.write_text(
        "number,text,unit\n1,Define a stack,Unit 2\n2,Define a queue,Unit 3\n",
        encoding="utf-8",
    )
    pipeline(database).ingest_file(
        bank, course=make_course(), source=make_source(), kind=DocumentKind.QUESTION_BANK
    )
    with database.session() as session:
        units = {
            q.question_number: q.unit
            for q in QuestionRepository(session).list_by_course("test-cs101-2024-v1")
        }
    assert units == {"1": "Unit 2", "2": "Unit 3"}


def test_manifest_validation_errors(tmp_path: Path) -> None:
    from exam_engine.ingest.manifest import ManifestError, load_manifest

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ManifestError) as exc_info:
        load_manifest(broken)
    assert exc_info.value.code == "invalid_manifest"

    duplicate = write_manifest(
        tmp_path,
        [
            course_block(
                "test-cs101-2024-v1",
                [
                    {"path": "q.txt", "kind": "notes"},
                    {"path": "./q.txt", "kind": "notes"},
                ],
            )
        ],
    )
    with pytest.raises(ManifestError):
        load_manifest(duplicate)
