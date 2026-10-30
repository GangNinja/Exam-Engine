"""Course-scoped question-understanding pipeline.

Phase 3 consumes stored Phase 1 questions and, when present, Phase 2 vectors.
It never re-parses a source file, never invents missing text or marks, and never
falls back to another course's syllabus. Every course run replaces its own
derived rows, so changed text or a changed syllabus produces fresh results.

Outcomes are allowed to be uncertain: questions can be UNMAPPED, have no type,
have no Bloom level, or carry a provisional difficulty estimate.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.config import UnderstandingSettings
from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    MappingStatus,
    QuestionType,
    Severity,
    UnderstandingStatus,
)
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.understanding import (
    BloomLabel,
    DifficultyAssessment,
    QuestionAnalysis,
    TypeLabel,
    UnderstandingRunReport,
    make_analysis_id,
    make_bloom_id,
    make_difficulty_id,
    make_type_id,
)
from exam_engine.storage.db import Database
from exam_engine.storage.nlp_repositories import EmbeddingRepository, VectorStoreRepository
from exam_engine.storage.question_repositories import (
    QuestionUnderstandingRepository,
    UnderstandingRunRepository,
)
from exam_engine.storage.repositories import (
    CourseRepository,
    QuestionRepository,
    SyllabusRepository,
)
from exam_engine.understanding.classification import (
    Classification,
    DifficultyResult,
    classify_bloom,
    classify_question_types,
    estimate_difficulty,
)
from exam_engine.understanding.mapping import (
    MappingSettings,
    SemanticIndex,
    map_question,
    to_unit_matches,
    unit_sources,
)
from exam_engine.understanding.similarity import (
    ReasoningProfile,
    SimilaritySettings,
    find_similar_questions,
)
from exam_engine.understanding.structure import detect_structure
from exam_engine.understanding.taxonomy import (
    DEFAULT_TAXONOMY,
    Taxonomy,
    TaxonomyError,
    load_taxonomy,
    taxonomy_as_dict,
)

logger = logging.getLogger(__name__)

TYPE_METHOD = "cue_rules"
BLOOM_METHOD = "cue_rules"
DIFFICULTY_METHOD = "observable_features"


def _run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    suffix = hashlib.sha256(str(time.perf_counter_ns()).encode()).hexdigest()[:6]
    return f"und_{stamp}_{suffix}"


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class UnderstandingService:
    """Coordinates Phase 3 analysis for one course at a time."""

    def __init__(
        self,
        database: Database,
        settings: UnderstandingSettings | None = None,
        *,
        taxonomy: Taxonomy | None = None,
    ) -> None:
        self.db = database
        self.settings = settings or UnderstandingSettings()
        self._taxonomy_error: str | None = None
        self.taxonomy = taxonomy or self._load_taxonomy()

    def _load_taxonomy(self) -> Taxonomy:
        if not self.settings.taxonomy_path:
            return DEFAULT_TAXONOMY.validated()
        try:
            return load_taxonomy(Path(self.settings.taxonomy_path))
        except TaxonomyError as exc:
            # Returning the built-in taxonomy is a documented fallback. The
            # failure is also attached to the run report, never silently hidden.
            logger.error("taxonomy could not be loaded: %s", exc)
            self._taxonomy_error = str(exc)
            return DEFAULT_TAXONOMY.validated()

    # -- public API --------------------------------------------------------

    def analyze(self, course_id: str | None = None) -> list[UnderstandingRunReport]:
        if course_id:
            return [self.analyze_course(course_id)]
        with self.db.session() as session:
            course_ids = [course.course_id for course in CourseRepository(session).list()]
        return [self.analyze_course(cid) for cid in course_ids]

    def analyze_course(self, course_id: str) -> UnderstandingRunReport:
        started = time.perf_counter()
        report = UnderstandingRunReport(
            run_id=_run_id(),
            course_id=course_id,
            started_at=utcnow(),
            methods=[
                "structure",
                "tfidf_mapping",
                "semantic_mapping",
                "cue_rules",
                "difficulty_features",
                "tfidf_similarity",
                "embedding_similarity",
            ],
            config={
                "taxonomy_version": self.taxonomy.version,
                "taxonomy_source": self.taxonomy.source,
                "taxonomy": taxonomy_as_dict(self.taxonomy),
                "mapping_threshold": self.settings.mapping_threshold,
                "mapping_margin": self.settings.mapping_margin,
                "mapping_semantic_weight": self.settings.mapping_semantic_weight,
                "type_threshold": self.settings.type_threshold,
                "bloom_threshold": self.settings.bloom_threshold,
                "difficulty_easy_below": self.settings.difficulty_easy_below,
                "difficulty_hard_above": self.settings.difficulty_hard_above,
                "similarity_near_threshold": self.settings.similarity_near_threshold,
                "similarity_semantic_threshold": self.settings.similarity_semantic_threshold,
                "similarity_concept_threshold": self.settings.similarity_concept_threshold,
                "max_questions": self.settings.max_questions,
            },
        )
        if self._taxonomy_error:
            report.warnings.append(
                Uncertainty(
                    code="degraded_fallback",
                    message=(
                        f"custom taxonomy could not be loaded ({self._taxonomy_error}); "
                        "the built-in taxonomy was used"
                    ),
                    severity=Severity.WARNING,
                )
            )
        self._start_run(report)

        try:
            with self.db.transaction() as session:
                self._analyze_in_session(session, course_id, report)
        except Exception as exc:  # noqa: BLE001 - analysis boundary
            logger.error(
                "question understanding failed for course %s: %s: %s",
                course_id,
                type(exc).__name__,
                exc,
            )
            logger.debug("question understanding failure detail", exc_info=True)
            report.status = UnderstandingStatus.FAILED
            report.errors.append(
                Uncertainty(
                    code="question_analysis_failed",
                    message=f"analysis failed: {type(exc).__name__}: {exc}",
                    severity=Severity.ERROR,
                )
            )

        report.finished_at = utcnow()
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        if report.status is UnderstandingStatus.OK and report.warnings:
            report.status = UnderstandingStatus.PARTIAL
        self._finish_run(report)
        return report

    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.db.session() as session:
            courses = CourseRepository(session).list()
            analysis_repo = QuestionUnderstandingRepository(session)
            run_repo = UnderstandingRunRepository(session)
            questions = QuestionRepository(session)
            for course in courses:
                latest = run_repo.latest_for_course(course.course_id)
                counts = analysis_repo.label_counts(course.course_id)
                rows.append(
                    {
                        "course_id": course.course_id,
                        "questions": questions.count_by_course(course.course_id),
                        "analyzed": analysis_repo.count_analyses(course.course_id),
                        "mapped": analysis_repo.count_analyses(
                            course.course_id, mapping_status=MappingStatus.MAPPED
                        ),
                        "ambiguous": analysis_repo.count_analyses(
                            course.course_id, mapping_status=MappingStatus.AMBIGUOUS
                        ),
                        "unmapped": analysis_repo.count_analyses(
                            course.course_id, mapping_status=MappingStatus.UNMAPPED
                        ),
                        "types": sum(counts["types"].values()),
                        "bloom": sum(counts["bloom"].values()),
                        "similarities": analysis_repo.count_similarities(course.course_id),
                        "last_run": latest.run_id if latest else None,
                        "last_status": latest.status.value if latest else None,
                    }
                )
        return rows

    def reset(self, course_id: str | None = None) -> None:
        """Delete derived Phase 3 rows; Phase 1/2 data and run history stay."""
        with self.db.transaction() as session:
            if course_id:
                course_ids = [course_id]
            else:
                course_ids = [course.course_id for course in CourseRepository(session).list()]
            repo = QuestionUnderstandingRepository(session)
            for cid in course_ids:
                repo.clear_for_course(cid)

    # -- implementation ----------------------------------------------------

    def _analyze_in_session(self, session, course_id: str, report: UnderstandingRunReport) -> None:
        repo = QuestionUnderstandingRepository(session)
        repo.clear_for_course(course_id)

        course = CourseRepository(session).get(course_id)
        if course is None:
            raise KeyError(f"unknown course: {course_id}")

        all_questions = QuestionRepository(session).list_by_course(course_id)
        questions = all_questions[: self.settings.max_questions]
        if len(all_questions) > len(questions):
            report.warnings.append(
                Uncertainty(
                    code="analysis_truncated",
                    message=(
                        f"only {len(questions)} of {len(all_questions)} questions were "
                        "analysed (EXAM_ENGINE_Q3_MAX_QUESTIONS)"
                    ),
                    severity=Severity.WARNING,
                    detail={"analysed": len(questions), "total": len(all_questions)},
                )
            )

        syllabi = SyllabusRepository(session).get_by_course(course_id)
        sources = unit_sources(syllabi)
        if not sources:
            report.warnings.append(
                Uncertainty(
                    code="no_syllabus",
                    message=(
                        "this course has no syllabus units; questions can still be "
                        "classified but remain unmapped"
                    ),
                    severity=Severity.INFO,
                    detail={"course_id": course_id},
                )
            )

        semantic, semantic_warning = self._semantic_index(
            session, course_id, questions, [source.unit.unit_id for source in sources]
        )
        if semantic_warning is not None:
            report.warnings.append(semantic_warning)
        if semantic.model_id:
            report.model_ids.append(semantic.model_id)
        report.totals = {
            "questions": len(all_questions),
            "analysed_limit": len(questions),
            "syllabus_units": len(sources),
            "syllabi": len(syllabi),
            "analysed": 0,
            "mapped": 0,
            "ambiguous": 0,
            "unmapped": 0,
            "type_labels": 0,
            "bloom_labels": 0,
            "difficulties": 0,
            "similarities": 0,
        }
        if not questions:
            report.status = UnderstandingStatus.INSUFFICIENT_DATA
            report.errors.append(
                Uncertainty(
                    code="insufficient_data",
                    message="course has no questions to analyse; ingest a question source first",
                    severity=Severity.WARNING,
                    detail={"course_id": course_id},
                )
            )
            return

        profiles: dict[str, ReasoningProfile] = {}
        analysed = 0
        for question in questions:
            try:
                # A savepoint keeps a failed question from leaving partial rows
                # while the rest of the course still commits.
                with session.begin_nested():
                    self._analyze_question(
                        session,
                        repo,
                        question,
                        sources=sources,
                        semantic=semantic,
                        profiles=profiles,
                        report=report,
                    )
                analysed += 1
            except Exception as exc:  # noqa: BLE001 - one bad question must not stop the batch
                logger.error(
                    "question %s could not be analysed: %s: %s",
                    question.question_id,
                    type(exc).__name__,
                    exc,
                )
                report.errors.append(
                    Uncertainty(
                        code="question_analysis_failed",
                        message=(
                            f"question {question.question_id} was skipped: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        severity=Severity.ERROR,
                        detail={"question_id": question.question_id},
                    )
                )

        if analysed == 0:
            report.status = UnderstandingStatus.FAILED
            return

        try:
            similarity_questions = questions[: self.settings.max_similarity_questions]
            if len(similarity_questions) < len(questions):
                report.warnings.append(
                    Uncertainty(
                        code="analysis_truncated",
                        message=(
                            f"similarity compared {len(similarity_questions)} of "
                            f"{len(questions)} questions "
                            "(EXAM_ENGINE_Q3_MAX_SIMILARITY_QUESTIONS)"
                        ),
                        severity=Severity.WARNING,
                        detail={
                            "compared": len(similarity_questions),
                            "total": len(questions),
                        },
                    )
                )
            similarity_settings = self._similarity_settings()
            similarity = find_similar_questions(
                course_id,
                similarity_questions,
                semantic=semantic,
                profiles=profiles,
                settings=similarity_settings,
            )
            repo.replace_similarities(course_id, similarity.matches)
            report.warnings.extend(similarity.uncertainties)
            report.totals["similarities"] = len(similarity.matches)
            report.totals["semantic_similarity"] = int(similarity.semantic_used)
        except Exception as exc:  # noqa: BLE001 - keep the successful classifications
            logger.error("question similarity failed for course %s: %s", course_id, exc)
            report.errors.append(
                Uncertainty(
                    code="model_failure",
                    message=(
                        f"question similarity failed; classifications were kept: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    severity=Severity.ERROR,
                )
            )
            report.status = UnderstandingStatus.PARTIAL
        report.totals["analysed"] = analysed
        if analysed < len(questions):
            report.status = UnderstandingStatus.PARTIAL

    def _analyze_question(
        self,
        session,
        repo: QuestionUnderstandingRepository,
        question: QuestionRecord,
        *,
        sources,
        semantic: SemanticIndex,
        profiles: dict[str, ReasoningProfile],
        report: UnderstandingRunReport,
    ) -> None:
        structure = detect_structure(question)
        mapping = map_question(
            course_id=question.course_id,
            question_id=question.question_id,
            text=question.text,
            question_unit=question.unit,
            sources=sources,
            semantic=semantic,
            settings=self._mapping_settings(),
        )
        types = classify_question_types(
            question.text,
            structure,
            self.taxonomy,
            threshold=self.settings.type_threshold,
        )
        bloom = classify_bloom(
            question.text,
            structure,
            types,
            self.taxonomy,
            threshold=self.settings.bloom_threshold,
        )
        difficulty = estimate_difficulty(
            question.text,
            structure,
            types,
            bloom,
            marks=question.marks,
            easy_below=self.settings.difficulty_easy_below,
            hard_above=self.settings.difficulty_hard_above,
        )
        matches = to_unit_matches(
            mapping, course_id=question.course_id, question_id=question.question_id
        )
        type_labels = self._type_labels(question.course_id, question.question_id, types)
        bloom_labels = self._bloom_labels(question.course_id, question.question_id, bloom)
        difficulty_record = self._difficulty_record(
            question.course_id, question.question_id, difficulty
        )

        uncertainties = [
            *structure.uncertainties,
            *mapping.uncertainties,
            *types.uncertainties,
            *bloom.uncertainties,
            *difficulty.uncertainties,
        ]
        analysis = QuestionAnalysis(
            analysis_id=make_analysis_id(question.course_id, question.question_id),
            question_id=question.question_id,
            course_id=question.course_id,
            document_id=question.document_id,
            text_sha256=_text_sha256(question.text),
            word_count=structure.word_count,
            sentence_count=structure.sentence_count,
            subquestion_count=structure.subquestion_count,
            subparts=structure.subparts,
            references=structure.references,
            mapping_status=mapping.status,
            mapping_confidence=mapping.confidence,
            top_unit_id=mapping.top.unit_id if mapping.top else None,
            primary_type=QuestionType(types.primary) if types.primary else None,
            primary_type_confidence=types.confidence,
            difficulty_label=difficulty.label,
            difficulty_score=difficulty.score,
            difficulty_confidence=difficulty.confidence,
            bloom_primary=BloomLevel(bloom.primary) if bloom.primary else None,
            bloom_confidence=bloom.confidence,
            structure_confidence=structure.confidence,
            uncertainties=uncertainties,
        )
        repo.replace_for_question(
            analysis=analysis,
            matches=matches,
            types=type_labels,
            bloom=bloom_labels,
            difficulty=difficulty_record,
        )

        profiles[question.question_id] = ReasoningProfile(
            question_type=types.primary,
            bloom_level=bloom.primary,
        )
        report.totals["analysed"] += 1
        report.totals[mapping.status.value] += 1
        report.totals["type_labels"] += len(type_labels)
        report.totals["bloom_labels"] += len(bloom_labels)
        report.totals["difficulties"] += 1
        if difficulty.label is DifficultyLabel.UNKNOWN:
            report.totals.setdefault("difficulty_unknown", 0)
            report.totals["difficulty_unknown"] += 1

    def _type_labels(
        self, course_id: str, question_id: str, classification: Classification
    ) -> list[TypeLabel]:
        method = f"{TYPE_METHOD}:{self.taxonomy.version}"
        return [
            TypeLabel(
                type_id=make_type_id(
                    course_id,
                    question_id,
                    QuestionType(label.label),
                    method,
                ),
                question_id=question_id,
                course_id=course_id,
                label=QuestionType(label.label),
                score=label.score,
                rank=rank,
                confidence=label.score,
                method=method,
                evidence=label.evidence,
            )
            for rank, label in enumerate(classification.labels, start=1)
        ]

    def _bloom_labels(
        self, course_id: str, question_id: str, classification: Classification
    ) -> list[BloomLabel]:
        method = f"{BLOOM_METHOD}:{self.taxonomy.version}"
        return [
            BloomLabel(
                bloom_id=make_bloom_id(
                    course_id,
                    question_id,
                    BloomLevel(label.label),
                    method,
                ),
                question_id=question_id,
                course_id=course_id,
                level=BloomLevel(label.label),
                score=label.score,
                rank=rank,
                confidence=(
                    classification.confidence if rank == 1 else round(label.score * 0.8, 4)
                ),
                method=method,
                evidence=label.evidence,
            )
            for rank, label in enumerate(classification.labels, start=1)
        ]

    def _difficulty_record(
        self, course_id: str, question_id: str, result: DifficultyResult
    ) -> DifficultyAssessment:
        return DifficultyAssessment(
            difficulty_id=make_difficulty_id(course_id, question_id, DIFFICULTY_METHOD),
            question_id=question_id,
            course_id=course_id,
            label=result.label,
            score=result.score,
            confidence=result.confidence,
            method=DIFFICULTY_METHOD,
            estimated=True,
            features=result.features,
            explanation=result.explanation,
        )

    def _mapping_settings(self) -> MappingSettings:
        return MappingSettings(
            threshold=self.settings.mapping_threshold,
            margin=self.settings.mapping_margin,
            max_candidates=self.settings.mapping_max_candidates,
            semantic_weight=self.settings.mapping_semantic_weight,
        )

    def _similarity_settings(self) -> SimilaritySettings:
        return SimilaritySettings(
            near_threshold=self.settings.similarity_near_threshold,
            semantic_threshold=self.settings.similarity_semantic_threshold,
            concept_threshold=self.settings.similarity_concept_threshold,
            top_k=self.settings.similarity_top_k,
            max_candidates=self.settings.similarity_max_candidates,
        )

    def _semantic_index(
        self,
        session,
        course_id: str,
        questions: list[QuestionRecord],
        unit_ids: list[str],
    ) -> tuple[SemanticIndex, Uncertainty | None]:
        records = EmbeddingRepository(session).list_by_course(course_id)
        question_ids = {question.question_id for question in questions}
        unit_id_set = set(unit_ids)
        relevant = [
            record
            for record in records
            if record.owner_id in question_ids or record.owner_id in unit_id_set
        ]
        if not relevant:
            return SemanticIndex(), Uncertainty(
                code="embedding_signal_unavailable",
                message=(
                    "Phase 2 embeddings are unavailable for this course; mapping and "
                    "similarity use lexical signals only (run 'nlp analyze' first)"
                ),
                severity=Severity.INFO,
                detail={"course_id": course_id},
            )

        groups: dict[str, dict[str, int]] = {}
        for record in relevant:
            stats = groups.setdefault(record.model_id, {"questions": 0, "units": 0})
            if record.owner_id in question_ids:
                stats["questions"] += 1
            if record.owner_id in unit_id_set:
                stats["units"] += 1
        model_id = max(
            groups,
            key=lambda candidate: (
                groups[candidate]["questions"] + groups[candidate]["units"],
                groups[candidate]["questions"] > 0,
                groups[candidate]["units"] > 0,
                candidate,
            ),
        )
        store = VectorStoreRepository(session)
        question_vectors: dict[str, list[float]] = {}
        unit_vectors: dict[str, list[float]] = {}
        for record in relevant:
            if record.model_id != model_id:
                continue
            vector = store.get(record.model_id, record.text_sha256)
            if vector is None:
                continue
            if record.owner_id in question_ids:
                question_vectors[record.owner_id] = vector
            if record.owner_id in unit_id_set:
                unit_vectors[record.owner_id] = vector

        index = SemanticIndex(
            model_id=model_id,
            question_vectors=question_vectors,
            unit_vectors=unit_vectors,
        )
        warning = None
        if unit_id_set and not unit_vectors:
            warning = Uncertainty(
                code="embedding_signal_unavailable",
                message=(
                    "no usable syllabus-unit embeddings were found; mapping used "
                    "lexical similarity only"
                ),
                severity=Severity.INFO,
                detail={"course_id": course_id, "model_id": model_id},
            )
        elif question_ids and not question_vectors:
            warning = Uncertainty(
                code="embedding_signal_unavailable",
                message=(
                    "no usable question embeddings were found; similarity used lexical signals only"
                ),
                severity=Severity.INFO,
                detail={"course_id": course_id, "model_id": model_id},
            )
        return index, warning

    # -- run bookkeeping ---------------------------------------------------

    def _start_run(self, report: UnderstandingRunReport) -> None:
        try:
            with self.db.transaction() as session:
                UnderstandingRunRepository(session).save(report)
        except Exception:  # noqa: BLE001 - reporting must never block analysis
            logger.debug("could not record question-understanding run start", exc_info=True)

    def _finish_run(self, report: UnderstandingRunReport) -> None:
        try:
            with self.db.transaction() as session:
                UnderstandingRunRepository(session).save(report)
        except Exception:  # noqa: BLE001 - reporting must never block analysis
            logger.debug("could not record question-understanding run result", exc_info=True)


__all__ = ["BLOOM_METHOD", "DIFFICULTY_METHOD", "TYPE_METHOD", "UnderstandingService"]
