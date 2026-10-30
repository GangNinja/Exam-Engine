from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from exam_engine.config import NlpSettings, Settings
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.enums import ConceptOrigin, DocumentKind, NlpStatus, TextOwnerType
from exam_engine.models.nlp import NlpRunReport
from exam_engine.nlp.service import NlpService
from exam_engine.storage.nlp_repositories import (
    ConceptRepository,
    EmbeddingRepository,
    EntityRepository,
    KeyphraseRepository,
    NlpRunRepository,
    ProcessedTextRepository,
    SectionRepository,
    TopicRepository,
)
from exam_engine.storage.repositories import DocumentRepository
from tests.support import make_course, make_source

HASHING = NlpSettings(embedding_provider="hashing", hashing_dim=64, top_k=10, topics_k=3)


def seed(
    database,
    tmp_path: Path,
    course_id: str,
    documents: dict[str, str],
    *,
    kinds: dict[str, DocumentKind] | None = None,
) -> None:
    course = make_course(course_id=course_id)
    source = make_source()
    pipeline = IngestionPipeline(database, settings=Settings())
    for name, text in documents.items():
        path = tmp_path / f"{course_id}-{name}.txt"
        path.write_text(text, encoding="utf-8")
        kind = (kinds or {}).get(name, DocumentKind.NOTES)
        result = pipeline.ingest_file(path, course=course, source=source, kind=kind)
        assert result.document is not None, result.error_codes()


TEXTS = {
    "trees": (
        "Binary search trees keep keys ordered so lookup is logarithmic. "
        "Tree traversal visits nodes in preorder, inorder and postorder. "
        "Balanced trees rotate nodes after insertion."
    ),
    "sorting": (
        "Quicksort partitions an array around a pivot element. "
        "Merge sort splits the array and merges the sorted halves. "
        "Heapsort builds a heap and extracts the maximum repeatedly."
    ),
    "graphs": (
        "Graph traversal explores vertices and edges. "
        "Dijkstra finds shortest paths with a priority queue. "
        "Topological sorting orders directed acyclic graphs."
    ),
    "syllabus": (
        "Unit 1: Trees\nBinary search trees; tree traversal; balanced trees\n\n"
        "Unit 2: Sorting\nQuicksort; merge sort; heapsort\n\n"
        "Unit 3: Graphs\nGraph traversal; shortest paths; topological sorting\n"
    ),
}


def test_analysis_produces_persisted_derived_data(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", TEXTS, kinds={"syllabus": DocumentKind.SYLLABUS})
    service = NlpService(database, HASHING)
    report = service.analyze_course("course-a")

    assert report.status in (NlpStatus.OK, NlpStatus.PARTIAL)
    assert report.totals["texts"] > 0
    assert report.totals["keyphrases"] > 0
    assert report.totals["entities"] > 0
    assert report.totals["embeddings"] > 0
    assert report.model_ids and report.model_ids[0].startswith("hashing-tfidf")
    assert report.config["lemmatize"] is False
    assert report.config["strip_stopwords"] is True
    assert report.config["keyword_method"] == "tfidf"

    with database.session() as session:
        assert KeyphraseRepository(session).count_by_course("course-a") > 0
        assert EmbeddingRepository(session).count_by_course("course-a") > 0
        stored_run = NlpRunRepository(session).get(report.run_id)
    assert stored_run is not None
    assert stored_run.status is report.status


def test_syllabus_concepts_are_verified_and_inferred_concepts_are_not(
    database, tmp_path: Path
) -> None:
    seed(database, tmp_path, "course-a", TEXTS, kinds={"syllabus": DocumentKind.SYLLABUS})
    NlpService(database, HASHING).analyze_course("course-a")

    with database.session() as session:
        repo = ConceptRepository(session)
        syllabus = repo.list_by_course("course-a", origin=ConceptOrigin.SYLLABUS)
        inferred = repo.list_by_course("course-a", origin=ConceptOrigin.INFERRED)
        mixed = repo.list_by_course("course-a")

    assert syllabus, "syllabus topics must become verified concepts"
    assert all(concept.verified for concept in syllabus)
    assert all(concept.origin is ConceptOrigin.SYLLABUS for concept in syllabus)
    assert all(concept.unit_id for concept in syllabus)
    assert inferred
    assert all(not concept.verified for concept in inferred)
    # A mixed listing leads with the verified concepts.
    assert mixed[0].verified and mixed[len(syllabus) - 1].verified
    assert not mixed[len(syllabus)].verified
    assert {concept.normalized for concept in syllabus}.isdisjoint(
        {concept.normalized for concept in inferred}
    )


def test_multiple_courses_stay_isolated(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", {"trees": TEXTS["trees"]})
    seed(database, tmp_path, "course-b", {"physics": "Wave motion and oscillation periods."})
    service = NlpService(database, HASHING)
    service.analyze_course("course-a")
    service.analyze_course("course-b")

    with database.session() as session:
        for course_id in ("course-a", "course-b"):
            keyphrases = KeyphraseRepository(session).top_for_course(course_id, limit=20)
            assert keyphrases
            assert {record.course_id for record in keyphrases} == {course_id}
            concepts = ConceptRepository(session).list_by_course(course_id)
            assert {concept.course_id for concept in concepts} == {course_id}
            embeddings = EmbeddingRepository(session).list_by_course(course_id)
            assert embeddings
            assert {record.course_id for record in embeddings} == {course_id}

    with database.session() as session:
        physics_terms = {
            record.normalized
            for record in KeyphraseRepository(session).top_for_course("course-b", limit=10)
        }
    assert "binary" not in physics_terms
    assert "quicksort" not in physics_terms


def test_rerun_reuses_cache_and_does_not_duplicate_rows(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", TEXTS)
    service = NlpService(database, HASHING)
    first = service.analyze_course("course-a")
    with database.session() as session:
        keyphrase_ids = {
            record.keyphrase_id
            for record in KeyphraseRepository(session).top_for_course("course-a", limit=100)
        }
        embedding_count = EmbeddingRepository(session).count_by_course("course-a")

    second = service.analyze_course("course-a")
    # Unique texts are cached; owner rows can share a text, so compare text
    # counts rather than embedding row counts.
    assert second.totals["embedding_texts_computed"] == 0
    assert second.totals["embedding_texts_cached"] == (
        first.totals["embedding_texts_computed"] + first.totals["embedding_texts_cached"]
    )
    with database.session() as session:
        again = {
            record.keyphrase_id
            for record in KeyphraseRepository(session).top_for_course("course-a", limit=100)
        }
        assert EmbeddingRepository(session).count_by_course("course-a") == embedding_count
    assert keyphrase_ids == again


def test_changed_document_text_invalidates_cached_representations(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", {"trees": TEXTS["trees"]})
    service = NlpService(database, HASHING)
    service.analyze_course("course-a")

    with database.transaction() as session:
        documents = DocumentRepository(session).list_by_course("course-a")
        documents_repo = DocumentRepository(session)
        document = documents[0]
        documents_repo.replace_pages(
            document.document_id,
            [
                page.model_copy(
                    update={"text": "Completely new content about compilers, parsing and lexing."}
                )
                for page in documents_repo.list_pages(document.document_id)
            ],
        )

    report = service.analyze_course("course-a")
    assert report.totals["embedding_texts_computed"] >= 1

    with database.session() as session:
        processed = ProcessedTextRepository(session).get(
            TextOwnerType.DOCUMENT, document.document_id
        )
    assert processed is not None
    assert "compilers" in processed.text


def test_force_recomputes_processed_text(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", {"trees": TEXTS["trees"]})
    service = NlpService(database, HASHING)
    service.analyze_course("course-a")
    report = service.analyze_course("course-a", force=True)
    # Text is identical, so the embedding cache still hits even when preprocessing reruns.
    assert report.totals["embedding_texts_computed"] == 0
    assert report.totals["texts"] > 0


def test_course_without_text_reports_insufficient_data(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-empty", {"blank": "   \n\n "})
    report = NlpService(database, HASHING).analyze_course("course-empty")
    assert report.status is NlpStatus.INSUFFICIENT_DATA
    assert any(error.code == "empty_text" for error in report.errors)
    assert report.totals["texts"] == 0


def test_unknown_course_fails_cleanly(database) -> None:
    report = NlpService(database, HASHING).analyze_course("does-not-exist")
    assert report.status is NlpStatus.FAILED
    assert report.errors


def test_two_documents_fall_back_to_section_topics(database, tmp_path: Path) -> None:
    seed(
        database,
        tmp_path,
        "course-small",
        {
            "trees": TEXTS["trees"],
            "sorting": TEXTS["sorting"],
        },
    )
    report = NlpService(database, HASHING).analyze_course("course-small")
    with database.session() as session:
        topics = TopicRepository(session).list_by_course("course-small")
    assert any(topic.scope is TextOwnerType.SECTION for topic in topics) or not topics
    assert any("sections" in warning.message for warning in report.warnings)


def test_reset_removes_derived_rows_only(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", TEXTS)
    service = NlpService(database, HASHING)
    service.analyze_course("course-a")
    service.reset("course-a")

    with database.session() as session:
        assert KeyphraseRepository(session).count_by_course("course-a") == 0
        assert EmbeddingRepository(session).count_by_course("course-a") == 0
        assert EntityRepository(session).count_by_course("course-a") == 0
        assert ConceptRepository(session).count_by_course("course-a") == 0
        assert TopicRepository(session).count_by_course("course-a") == 0
        assert SectionRepository(session).count_by_course("course-a") == 0
        assert ProcessedTextRepository(session).count_by_course("course-a") == 0
        # Phase 1 data stays untouched.
        assert DocumentRepository(session).count_by_course("course-a") > 0


def test_status_lists_courses_without_analysis(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", {"trees": TEXTS["trees"]})
    rows = NlpService(database, HASHING).status()
    assert [row["course_id"] for row in rows] == ["course-a"]
    assert rows[0]["last_run"] is None
    assert rows[0]["texts"] == 0


def test_section_spans_slice_the_stored_document_text(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", {"trees": TEXTS["trees"]})
    # Irregular whitespace is collapsed by normalisation. The stored span must
    # still slice the normalised document rather than the raw page text.
    with database.transaction() as session:
        repo = DocumentRepository(session)
        document = repo.list_by_course("course-a")[0]
        repo.replace_pages(
            document.document_id,
            [
                page.model_copy(
                    update={
                        "text": (
                            "Heading\nA  B   C about trees.\n\n"
                            "Second  paragraph  here about sorting."
                        )
                    }
                )
                for page in repo.list_pages(document.document_id)
            ],
        )

    service = NlpService(database, HASHING)
    service.analyze_course("course-a")

    with database.session() as session:
        document = DocumentRepository(session).list_by_course("course-a")[0]
        processed = ProcessedTextRepository(session).get(
            TextOwnerType.DOCUMENT, document.document_id
        )
        sections = SectionRepository(session).list_for_document(document.document_id)

    assert processed is not None
    assert sections
    for section in sections:
        assert 0 <= section.char_start <= section.char_end <= len(processed.text)
        assert processed.text[section.char_start : section.char_end] == section.text


def test_entities_cover_every_processed_owner(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", TEXTS, kinds={"syllabus": DocumentKind.SYLLABUS})
    NlpService(database, HASHING).analyze_course("course-a")

    with database.session() as session:
        owners = {
            entity.owner_type for entity in EntityRepository(session).list_by_course("course-a")
        }

    assert {TextOwnerType.DOCUMENT, TextOwnerType.SECTION, TextOwnerType.SYLLABUS_UNIT} <= owners


def test_shrinking_course_removes_stale_derived_rows(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", {"trees": TEXTS["trees"], "sorting": TEXTS["sorting"]})
    service = NlpService(database, HASHING)
    service.analyze_course("course-a")
    with database.session() as session:
        document = DocumentRepository(session).list_by_course("course-a")[0]
        document_id = document.document_id

    with database.transaction() as session:
        DocumentRepository(session).replace_pages(document_id, [])
    service.analyze_course("course-a")

    with database.session() as session:
        assert (
            KeyphraseRepository(session).list_for_owner(TextOwnerType.DOCUMENT, document_id) == []
        )
        assert SectionRepository(session).list_for_document(document_id) == []
        assert [
            entity
            for entity in EntityRepository(session).list_by_course("course-a")
            if entity.owner_id == document_id
        ] == []
        assert ProcessedTextRepository(session).get(TextOwnerType.DOCUMENT, document_id) is None


def test_lemmatize_setting_changes_keyphrase_terms(database, tmp_path: Path) -> None:
    text = (
        "Graph algorithms process graphs efficiently. The algorithm explores algorithms "
        "and the algorithm sorts arrays. Sorting algorithms repeat algorithm steps."
    )
    seed(database, tmp_path, "course-a", {"algorithms": text})
    service = NlpService(database, HASHING)
    service.analyze_course("course-a")
    with database.session() as session:
        plain = {
            record.normalized
            for record in KeyphraseRepository(session).top_for_course("course-a", limit=100)
        }

    lemmatized = NlpService(database, replace(HASHING, lemmatize=True))
    lemmatized.analyze_course("course-a", force=True)
    with database.session() as session:
        reduced = {
            record.normalized
            for record in KeyphraseRepository(session).top_for_course("course-a", limit=100)
        }

    assert "algorithms" in plain
    assert "algorithms" not in reduced
    assert "algorithm" in reduced


def test_status_reports_the_latest_failed_run(database, tmp_path: Path) -> None:
    seed(database, tmp_path, "course-a", {"trees": TEXTS["trees"]})
    with database.transaction() as session:
        NlpRunRepository(session).save(
            NlpRunReport(run_id="nlp_failed", course_id="course-a", status=NlpStatus.FAILED)
        )

    rows = NlpService(database, HASHING).status()
    assert rows[0]["last_run"] == "nlp_failed"
    assert rows[0]["last_status"] == "failed"
