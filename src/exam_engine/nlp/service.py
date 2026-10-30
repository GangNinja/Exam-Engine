"""Course-scoped NLP analysis: preprocessing, keyphrases, embeddings, topics.

The service is intentionally a coordinator, not an algorithm: each step is a
replaceable component (preprocessor, keyword extractor, embedding provider,
entity extractor, topic modeler) and every result is written through the
repository layer.

Repeatability rules
-------------------
* preprocessing is reused when the raw text hash and config hash are unchanged;
* embeddings are keyed by ``(model_id, sha256(text))`` so unchanged text is never
  re-embedded and changed text cannot hit a stale vector;
* derived rows for a course are replaced per run, never appended, so a rerun does
  not duplicate keyphrases, entities or topics.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from exam_engine.config import NlpSettings
from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    ConceptOrigin,
    NlpMethod,
    NlpStatus,
    Severity,
    TextOwnerType,
)
from exam_engine.models.nlp import (
    ConceptRecord,
    KeyphraseRecord,
    NlpRunReport,
    PreprocessConfig,
    ProcessedText,
    make_concept_id,
    make_keyphrase_id,
)
from exam_engine.models.question import Uncertainty
from exam_engine.nlp.embeddings import EmbeddingChoice, resolve_provider
from exam_engine.nlp.entities import build_extractor, entity_records, lexical_extraction_note
from exam_engine.nlp.keywords import (
    KeywordDocument,
    get_extractor,
    normalize_term,
)
from exam_engine.nlp.preprocess import Preprocessor, raw_sha256, split_section_chunks
from exam_engine.nlp.topics import TopicDocument, get_topic_modeler
from exam_engine.nlp.vectors import EmbeddingService
from exam_engine.storage import nlp_mappers
from exam_engine.storage.db import Database
from exam_engine.storage.nlp_repositories import (
    ConceptRepository,
    EmbeddingRepository,
    EntityRepository,
    KeyphraseRepository,
    NlpRunRepository,
    ProcessedTextRepository,
    SectionRepository,
    TopicRepository,
    VectorStoreRepository,
)
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class CourseBundle:
    course_id: str
    documents: list[Any] = field(default_factory=list)
    document_texts: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)
    questions: list[Any] = field(default_factory=list)
    units: list[Any] = field(default_factory=list)
    warnings: list[Uncertainty] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return any(text.strip() for text in self.document_texts.values())


def _section_id(document_id: str, index: int) -> str:
    return "sec_" + hashlib.sha256(f"{document_id}\x1f{index}".encode()).hexdigest()[:24]


def _run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    suffix = hashlib.sha256(str(time.perf_counter_ns()).encode()).hexdigest()[:6]
    return f"nlp_{stamp}_{suffix}"


class NlpService:
    """Coordinates the Phase 2 analysis for one course at a time."""

    def __init__(self, database: Database, settings: NlpSettings | None = None) -> None:
        self.db = database
        self.settings = settings or NlpSettings()
        self.preprocessor = Preprocessor(PreprocessConfig(language=self.settings.language))

    # -- public API --------------------------------------------------------

    def analyze(self, course_id: str | None = None, *, force: bool = False) -> list[NlpRunReport]:
        """Analyse one course, or every course when ``course_id`` is omitted."""
        if course_id:
            return [self.analyze_course(course_id, force=force)]
        with self.db.session() as session:
            course_ids = [course.course_id for course in CourseRepository(session).list()]
        return [self.analyze_course(cid, force=force) for cid in course_ids]

    def analyze_course(self, course_id: str, *, force: bool = False) -> NlpRunReport:
        started = time.perf_counter()
        report = NlpRunReport(
            run_id=_run_id(),
            course_id=course_id,
            started_at=utcnow(),
            config={
                "preprocess_config": self.preprocessor.config_id,
                "embedding_provider": self.settings.embedding_provider,
                "keyword_method": self.settings.keyword_method,
                "entity_extractor": self.settings.entity_extractor,
                "top_k": self.settings.top_k,
                "topics_k": self.settings.topics_k,
                "lemmatize": self.settings.lemmatize,
                "strip_stopwords": self.settings.strip_stopwords,
                "language": self.settings.language,
                "max_documents": self.settings.max_documents,
                "max_sections": self.settings.max_sections,
            },
        )
        self._start_run(report)

        try:
            with self.db.transaction() as session:
                bundle = self._load_bundle(session, course_id)
                report.warnings.extend(bundle.warnings)
                if not bundle.has_text:
                    # A course that lost all text must not keep derived rows from
                    # an earlier run.
                    self._clear_derived(session, course_id)
                    report.status = NlpStatus.INSUFFICIENT_DATA
                    report.errors.append(
                        Uncertainty(
                            code="empty_text",
                            message=(
                                "course has no extractable text; ingest readable documents first"
                            ),
                            severity=Severity.WARNING,
                        )
                    )
                    report.totals = {"documents": len(bundle.documents), "texts": 0}
                else:
                    self._analyze_in_session(session, bundle, report, force=force)
        except Exception as exc:  # noqa: BLE001 - analysis boundary
            # The report carries the failure; a traceback here would only pollute
            # CLI output for expected errors such as an unknown course id.
            logger.error(
                "NLP analysis failed for course %s: %s: %s",
                course_id,
                type(exc).__name__,
                exc,
            )
            logger.debug("analysis failure detail", exc_info=True)
            report.status = NlpStatus.FAILED
            report.errors.append(
                Uncertainty(
                    code="model_failure",
                    message=f"analysis failed: {type(exc).__name__}: {exc}",
                    severity=Severity.ERROR,
                )
            )

        report.finished_at = utcnow()
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        if report.status is NlpStatus.OK and report.warnings:
            report.status = NlpStatus.PARTIAL
        self._finish_run(report)
        return report

    def status(self) -> list[dict[str, Any]]:
        """Per-course coverage of derived NLP data (for ``nlp status``)."""
        rows: list[dict[str, Any]] = []
        with self.db.session() as session:
            courses = CourseRepository(session).list()
            texts = ProcessedTextRepository(session)
            keyphrases = KeyphraseRepository(session)
            entities = EntityRepository(session)
            concepts = ConceptRepository(session)
            topics = TopicRepository(session)
            embeddings = EmbeddingRepository(session)
            runs = NlpRunRepository(session)
            for course in courses:
                latest = runs.latest_for_course(course.course_id)
                rows.append(
                    {
                        "course_id": course.course_id,
                        "documents": DocumentRepository(session).count_by_course(course.course_id),
                        "texts": texts.count_by_course(course.course_id),
                        "keyphrases": keyphrases.count_by_course(course.course_id),
                        "entities": entities.count_by_course(course.course_id),
                        "concepts": concepts.count_by_course(course.course_id),
                        "topics": topics.count_by_course(course.course_id),
                        "embeddings": embeddings.count_by_course(course.course_id),
                        "last_run": latest.run_id if latest else None,
                        "last_status": latest.status.value if latest else None,
                    }
                )
        return rows

    def reset(self, course_id: str | None = None) -> None:
        """Delete derived NLP rows so the next analysis recomputes from scratch."""
        with self.db.transaction() as session:
            course_ids = (
                [course_id]
                if course_id
                else [course.course_id for course in CourseRepository(session).list()]
            )
            for cid in course_ids:
                self._clear_derived(session, cid)

    def _clear_derived(self, session, course_id: str, *, keep_texts: bool = False) -> None:
        """Remove every derived row for a course (and optionally its text cache)."""
        KeyphraseRepository(session).delete_for_course(course_id)
        EntityRepository(session).delete_for_course(course_id)
        ConceptRepository(session).delete_for_course(course_id)
        TopicRepository(session).delete_for_course(course_id)
        EmbeddingRepository(session).delete_for_course(course_id)
        SectionRepository(session).delete_for_course(course_id)
        if not keep_texts:
            ProcessedTextRepository(session).delete_for_course(course_id)

    # -- bundle ------------------------------------------------------------

    def _load_bundle(self, session, course_id: str) -> CourseBundle:
        bundle = CourseBundle(course_id=course_id)
        courses = CourseRepository(session)
        if courses.get(course_id) is None:
            raise KeyError(f"unknown course: {course_id}")
        documents = DocumentRepository(session)
        bundle.documents = documents.list_by_course(course_id)[: self.settings.max_documents]
        bundle.questions = QuestionRepository(session).list_by_course(course_id)
        questions_by_document: dict[str, list[str]] = defaultdict(list)
        for question in bundle.questions:
            questions_by_document[question.document_id].append(question.text)
        for document in bundle.documents:
            pages = documents.list_pages(document.document_id)
            text = "\n\n".join(page.text for page in pages if page.text.strip()).strip()
            if not text.strip():
                # Question banks have no pages: their content *is* the questions.
                text = "\n\n".join(questions_by_document.get(document.document_id, [])).strip()
            bundle.document_texts[document.document_id] = text
            if not text.strip():
                bundle.warnings.append(
                    Uncertainty(
                        code="empty_text",
                        message=(
                            f"document '{document.title}' has no text "
                            f"(status {document.extraction_status.value}) and was skipped"
                        ),
                        severity=Severity.INFO,
                        detail={"document_id": document.document_id},
                    )
                )
        for syllabus in SyllabusRepository(session).get_by_course(course_id):
            bundle.units.extend(syllabus.units)
        return bundle

    # -- analysis ----------------------------------------------------------

    def _analyze_in_session(
        self, session, bundle: CourseBundle, report: NlpRunReport, *, force: bool
    ) -> None:
        course_id = bundle.course_id
        texts_repo = ProcessedTextRepository(session)
        section_repo = SectionRepository(session)
        keyphrases_repo = KeyphraseRepository(session)
        entity_repo = EntityRepository(session)
        embedding_repo = EmbeddingRepository(session)

        # Derived rows are replaced per run. Text representations stay cached,
        # but anything keyed to owners that no longer exist is pruned below, so
        # a shrunk corpus cannot leave stale keyphrases, entities or sections.
        self._clear_derived(session, course_id, keep_texts=True)

        processed: dict[tuple[TextOwnerType, str], ProcessedText] = {}
        pipeline_items: list[tuple[TextOwnerType, str, str, str | None]] = []

        def load_text(
            owner_type: TextOwnerType,
            owner_id: str,
            item_text: str,
            item_document_id: str | None,
        ) -> ProcessedText:
            cached = texts_repo.get(owner_type, owner_id)
            digest = raw_sha256(item_text)
            if (
                not force
                and cached is not None
                and cached.raw_sha256 == digest
                and cached.config_id == self.preprocessor.config_id
            ):
                record = cached
            else:
                record = self.preprocessor.preprocess(
                    item_text,
                    course_id=course_id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    document_id=item_document_id,
                )
                texts_repo.upsert(record)
                report.warnings.extend(
                    uncertainty
                    for uncertainty in record.uncertainties
                    if uncertainty.code == "language_unsupported"
                )
            processed[(owner_type, owner_id)] = record
            return record

        def add_item(
            owner_type: TextOwnerType,
            owner_id: str,
            item_text: str,
            item_document_id: str | None,
        ) -> None:
            pipeline_items.append((owner_type, owner_id, item_text, item_document_id))
            load_text(owner_type, owner_id, item_text, item_document_id)

        # 1. Documents, then sections split from the normalised text. Splitting
        # the normalised text keeps the stored span a real slice of the record
        # that `nlp preprocess` prints.
        for document in bundle.documents:
            text = bundle.document_texts.get(document.document_id, "")
            if text.strip():
                add_item(TextOwnerType.DOCUMENT, document.document_id, text, document.document_id)

        section_texts: list[tuple[str, str, str, int]] = []
        for document in bundle.documents:
            record = processed.get((TextOwnerType.DOCUMENT, document.document_id))
            if record is None:
                continue
            chunks = split_section_chunks(
                record.text, normalize=False, max_sections=self.settings.max_sections
            )
            rows = []
            for index, chunk in enumerate(chunks):
                section_id = _section_id(document.document_id, index)
                section_texts.append((section_id, document.document_id, chunk.text, index))
                rows.append(
                    nlp_mappers.section_to_row(
                        section_id=section_id,
                        document_id=document.document_id,
                        course_id=course_id,
                        section_index=index,
                        heading=chunk.text.split("\n", 1)[0][:120],
                        text=chunk.text,
                        char_start=chunk.start,
                        char_end=chunk.end,
                        page_start=None,
                        page_end=None,
                    )
                )
                add_item(TextOwnerType.SECTION, section_id, chunk.text, document.document_id)
            section_repo.replace_for_document(document.document_id, rows)

        for question in bundle.questions:
            if question.text.strip():
                add_item(
                    TextOwnerType.QUESTION,
                    question.question_id,
                    question.text,
                    question.document_id,
                )
        for unit in bundle.units:
            unit_text = "\n".join([unit.title or "", *unit.topics]).strip()
            if unit_text:
                add_item(TextOwnerType.SYLLABUS_UNIT, unit.unit_id, unit_text, None)

        texts_repo.delete_missing(course_id, set(processed))
        report.totals["texts"] = len(processed)
        report.totals["sentences"] = sum(record.sentence_count for record in processed.values())

        # 2. Keyphrases: one corpus-relative pass over documents, attributed to
        # the document and aggregated to the course.
        keyword_documents = [
            KeywordDocument(
                owner_type=TextOwnerType.DOCUMENT,
                owner_id=document.document_id,
                text=processed[(TextOwnerType.DOCUMENT, document.document_id)].text,
                course_id=course_id,
                document_id=document.document_id,
            )
            for document in bundle.documents
            if (TextOwnerType.DOCUMENT, document.document_id) in processed
            and processed[(TextOwnerType.DOCUMENT, document.document_id)].text.strip()
        ]
        try:
            extractor = get_extractor(
                self.settings.keyword_method,
                lemmatize_terms=self.settings.lemmatize,
                drop_stopwords=self.settings.strip_stopwords,
            )
        except KeyError as exc:
            report.warnings.append(
                Uncertainty(
                    code="model_unavailable",
                    message=f"{exc}; used tfidf instead",
                    severity=Severity.WARNING,
                )
            )
            extractor = get_extractor(
                "tfidf",
                lemmatize_terms=self.settings.lemmatize,
                drop_stopwords=self.settings.strip_stopwords,
            )
        report.methods.append(extractor.name.value)

        keyphrase_records: list[KeyphraseRecord] = []
        if keyword_documents:
            per_document = extractor.extract(
                keyword_documents, top_k=self.settings.top_k * max(1, len(keyword_documents))
            )
            grouped: dict[str, list[KeyphraseRecord]] = defaultdict(list)
            for keyphrase in per_document:
                grouped[keyphrase.owner_id].append(keyphrase)
            for owner_id, records in grouped.items():
                keyphrases_repo.replace_for_owner(course_id, TextOwnerType.DOCUMENT, owner_id)
                ranked = sorted(records, key=lambda r: (-r.score, r.term))[: self.settings.top_k]
                for rank, keyphrase in enumerate(ranked, start=1):
                    keyphrase.rank = rank
                keyphrases_repo.add_many(ranked)
                keyphrase_records.extend(ranked)

            course_terms = self._course_keyphrases(keyword_documents, per_document)
            keyphrases_repo.replace_for_owner(course_id, TextOwnerType.COURSE, course_id)
            keyphrases_repo.add_many(course_terms)
            keyphrase_records.extend(course_terms)
        report.totals["keyphrases"] = len(keyphrase_records)

        # 3. Entities over every processed owner, each with its own offsets and
        # provenance (documents, sections, questions and syllabus units).
        extractor_entities, entity_warnings = build_extractor(
            self.settings.entity_extractor, spacy_model=self.settings.spacy_model
        )
        report.warnings.extend(entity_warnings)
        note = lexical_extraction_note(extractor_entities)
        if note is not None:
            report.warnings.append(note)
        report.methods.append(extractor_entities.name.value)
        entity_total = 0
        for owner_type, owner_id, _item_text, item_document_id in pipeline_items:
            processed_text = processed[(owner_type, owner_id)]
            if not processed_text.text.strip():
                continue
            entity_repo.replace_for_owner(course_id, owner_type, owner_id)
            entity_items = entity_records(
                extractor_entities,
                processed_text.text,
                course_id=course_id,
                owner_type=owner_type,
                owner_id=owner_id,
                document_id=item_document_id,
            )
            entity_repo.add_many(entity_items)
            entity_total += len(entity_items)
        report.totals["entities"] = entity_total

        # 4. Embeddings (documents, sections, questions, syllabus units).
        corpus_texts = [
            processed[(TextOwnerType.DOCUMENT, document.document_id)].text
            for document in bundle.documents
            if (TextOwnerType.DOCUMENT, document.document_id) in processed
        ]
        choice = self._resolve_provider(corpus_texts, report)
        vector_service = EmbeddingService(choice.provider, VectorStoreRepository(session))
        embed_items = [
            item for item in pipeline_items if processed[(item[0], item[1])].text.strip()
        ]
        batch = vector_service.embed_texts(
            [processed[(owner_type, owner_id)].text for owner_type, owner_id, _t, _d in embed_items]
        )
        if batch.error:
            report.warnings.append(
                Uncertainty(
                    code="model_failure",
                    message=f"embeddings unavailable: {batch.error}",
                    severity=Severity.WARNING,
                )
            )
        report.totals["embedding_texts_computed"] = batch.computed
        report.totals["embedding_texts_cached"] = batch.cached
        stored = 0
        for owner_type, owner_id, _text, embed_document_id in embed_items:
            processed_text = processed[(owner_type, owner_id)]
            if not vector_service.lookup(processed_text.text):
                continue
            embedding_repo.upsert(
                vector_service.record(
                    course_id=course_id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    text=processed_text.text,
                    document_id=embed_document_id,
                )
            )
            stored += 1
        report.totals["embeddings"] = stored
        if batch.dim:
            report.config["embedding_dim"] = batch.dim

        # 5. Topics at document scope (section scope when documents are too few).
        topics_repo = TopicRepository(session)
        topic_documents = [
            TopicDocument(
                owner_type=TextOwnerType.DOCUMENT,
                owner_id=document.document_id,
                text=processed[(TextOwnerType.DOCUMENT, document.document_id)].text,
                document_id=document.document_id,
            )
            for document in bundle.documents
            if (TextOwnerType.DOCUMENT, document.document_id) in processed
        ]
        scope = TextOwnerType.DOCUMENT
        if len([doc for doc in topic_documents if doc.text.strip()]) < 3 and section_texts:
            topic_documents = [
                TopicDocument(
                    owner_type=TextOwnerType.SECTION,
                    owner_id=section_id,
                    text=processed[(TextOwnerType.SECTION, section_id)].text,
                    document_id=document_id,
                )
                for section_id, document_id, _chunk, _index in section_texts
                if (TextOwnerType.SECTION, section_id) in processed
            ]
            scope = TextOwnerType.SECTION
            report.warnings.append(
                Uncertainty(
                    code="insufficient_data",
                    message=(
                        "fewer than 3 documents; topic discovery ran over document sections instead"
                    ),
                    severity=Severity.INFO,
                )
            )
        modeler = get_topic_modeler("kmeans")
        topic_result = modeler.fit(
            topic_documents,
            course_id=course_id,
            scope=scope,
            scope_id=None,
            k=self.settings.topics_k,
        )
        topics_repo.replace_for_course(course_id, TextOwnerType.DOCUMENT)
        topics_repo.replace_for_course(course_id, TextOwnerType.SECTION)
        if topic_result.topics:
            topics_repo.add_many(topic_result.topics, created_at=utcnow())
        report.warnings.extend(topic_result.warnings)
        report.totals["topics"] = len(topic_result.topics)
        report.config["topic_params"] = topic_result.params
        report.methods.append(modeler.name.value)

        # 6. Concepts: syllabus-verified plus inferred candidates.
        concepts_repo = ConceptRepository(session)
        concepts_repo.replace_origin(course_id, ConceptOrigin.SYLLABUS)
        concepts_repo.replace_origin(course_id, ConceptOrigin.INFERRED)
        syllabus_concepts = self._syllabus_concepts(bundle)
        inferred = self._inferred_concepts(
            course_id,
            keyphrase_records,
            entity_repo.list_by_course(course_id),
            verified_terms={concept.normalized for concept in syllabus_concepts},
        )
        concepts_repo.upsert_many(syllabus_concepts)
        concepts_repo.upsert_many(inferred)
        report.totals["concepts_verified"] = len(syllabus_concepts)
        report.totals["concepts_inferred"] = len(inferred)
        report.totals["documents"] = len(bundle.documents)
        report.totals["sections"] = len(section_texts)
        report.totals["questions"] = len(bundle.questions)
        report.totals["units"] = len(bundle.units)

        if not report.methods:
            report.status = NlpStatus.INSUFFICIENT_DATA
        elif report.totals.get("keyphrases", 0) == 0 and report.totals.get("entities", 0) == 0:
            report.status = NlpStatus.INSUFFICIENT_DATA
            report.warnings.append(
                Uncertainty(
                    code="insufficient_data",
                    message=(
                        "no keyphrases or entities were extracted; the course text is "
                        "too short or too uniform for statistics"
                    ),
                    severity=Severity.WARNING,
                )
            )

    def _course_keyphrases(
        self,
        documents: list[KeywordDocument],
        per_document: list[KeyphraseRecord],
    ) -> list[KeyphraseRecord]:
        """Aggregate per-document keyphrases into a course-level ranking.

        The score is the mean of the document scores in which a term occurs, so a
        term that is distinctive in several documents outranks one that is
        distinctive in a single short document.
        """
        totals: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        exemplar: dict[str, KeyphraseRecord] = {}
        for record in per_document:
            totals[record.normalized] += record.score
            counts[record.normalized] += 1
            current = exemplar.get(record.normalized)
            if current is None or record.score > current.score:
                exemplar[record.normalized] = record
        ranked = sorted(
            ((normalized, totals[normalized] / counts[normalized]) for normalized in totals),
            key=lambda item: (-item[1], item[0]),
        )[: self.settings.top_k]
        course_id = documents[0].course_id if documents else ""
        records: list[KeyphraseRecord] = []
        for rank, (normalized, score) in enumerate(ranked, start=1):
            source = exemplar[normalized]
            records.append(
                KeyphraseRecord(
                    keyphrase_id=make_keyphrase_id(
                        TextOwnerType.COURSE, course_id, source.method, normalized
                    ),
                    course_id=course_id,
                    owner_type=TextOwnerType.COURSE,
                    owner_id=course_id,
                    document_id=None,
                    term=source.term,
                    normalized=normalized,
                    score=round(score, 6),
                    method=source.method,
                    rank=rank,
                    n_gram=source.n_gram,
                    metadata={"documents": counts[normalized]},
                )
            )
        return records

    def _syllabus_concepts(self, bundle: CourseBundle) -> list[ConceptRecord]:
        concepts: list[ConceptRecord] = []
        seen: set[str] = set()
        for unit in bundle.units:
            for topic in unit.topics:
                normalized = normalize_term(topic)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                concepts.append(
                    ConceptRecord(
                        concept_id=make_concept_id(
                            bundle.course_id, ConceptOrigin.SYLLABUS, normalized
                        ),
                        course_id=bundle.course_id,
                        term=topic.strip(),
                        normalized=normalized,
                        origin=ConceptOrigin.SYLLABUS,
                        method=NlpMethod.RULE_BASED,
                        confidence=0.9,
                        verified=True,
                        unit_id=unit.unit_id,
                        evidence=[f"syllabus_unit:{unit.unit_id}"],
                    )
                )
        return concepts

    def _inferred_concepts(
        self,
        course_id: str,
        keyphrases: list[KeyphraseRecord],
        entities: list[Any],
        *,
        verified_terms: set[str],
    ) -> list[ConceptRecord]:
        concepts: list[ConceptRecord] = []
        # A syllabus-stated term is verified; the same string must not also
        # appear as an inferred candidate.
        seen: set[str] = set(verified_terms)
        scored = [record for record in keyphrases if record.owner_type is TextOwnerType.COURSE]
        if scored:
            highest = max(record.score for record in scored) or 1.0
            for record in sorted(scored, key=lambda r: -r.score)[: self.settings.top_k]:
                if record.normalized in seen:
                    continue
                seen.add(record.normalized)
                confidence = 0.35 + 0.4 * (record.score / highest if highest else 0.0)
                concepts.append(
                    ConceptRecord(
                        concept_id=make_concept_id(
                            course_id, ConceptOrigin.INFERRED, record.normalized
                        ),
                        course_id=course_id,
                        term=record.term,
                        normalized=record.normalized,
                        origin=ConceptOrigin.INFERRED,
                        method=record.method,
                        confidence=round(min(0.75, confidence), 4),
                        verified=False,
                        evidence=[f"keyphrase:{record.keyphrase_id}"],
                    )
                )
        for entity in entities:
            if entity.label not in {"METHOD", "TECHNIQUE", "ACRONYM", "NOTATION"}:
                continue
            normalized = normalize_term(entity.text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            concepts.append(
                ConceptRecord(
                    concept_id=make_concept_id(course_id, ConceptOrigin.INFERRED, normalized),
                    course_id=course_id,
                    term=entity.text,
                    normalized=normalized,
                    origin=ConceptOrigin.INFERRED,
                    method=entity.method,
                    confidence=round(min(0.7, entity.confidence), 4),
                    verified=False,
                    evidence=[f"entity:{entity.entity_id}"],
                )
            )
        return concepts

    def _resolve_provider(self, corpus: list[str], report: NlpRunReport) -> EmbeddingChoice:
        choice = resolve_provider(
            self.settings.embedding_provider,
            corpus=[text for text in corpus if text.strip()],
            hashing_dim=self.settings.hashing_dim,
            lsa_dim=self.settings.lsa_dim,
            st_model=self.settings.sentence_transformer_model,
            allow_download=self.settings.allow_model_download,
        )
        report.model_ids.append(choice.provider.model_id)
        report.config["embedding_scope"] = getattr(choice.provider.scope, "value", "stateless")
        report.config["embedding_metadata"] = choice.provider.metadata()
        for warning in choice.warnings:
            report.warnings.append(
                Uncertainty(code="model_unavailable", message=warning, severity=Severity.WARNING)
            )
        return choice

    # -- run bookkeeping ---------------------------------------------------

    def _start_run(self, report: NlpRunReport) -> None:
        try:
            with self.db.transaction() as session:
                NlpRunRepository(session).save(report)
        except Exception:  # noqa: BLE001 - reporting must never block analysis
            logger.exception("could not record NLP run start for %s", report.course_id)

    def _finish_run(self, report: NlpRunReport) -> None:
        try:
            with self.db.transaction() as session:
                NlpRunRepository(session).save(report)
        except Exception:  # noqa: BLE001 - reporting must never block analysis
            logger.exception("could not record NLP run result for %s", report.course_id)


__all__ = ["CourseBundle", "NlpService"]
