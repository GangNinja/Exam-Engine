"""Read-only Phase 9 concept evolution tracker.

The tracker composes existing records instead of rebuilding them:

* Phase 1 documents, pages, questions and extraction status;
* Phase 3 stored question analysis, unit matches and type/Bloom labels;
* Phase 4 period resolution, so an examination date is never invented;
* Phase 5 concept identity, aliases and evidence-bearing graph edges;
* Phase 7 coverage for concept-to-question mappings and report ids;
* Phase 8 Exam DNA profiles for paper/session context, reused by reference.

It adds no table and writes nothing. Every comparison names its date type and
source, every change keeps both sides and its evidence, and no inferred change is
applied to a canonical concept.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select

from exam_engine.analytics.metadata import ExamPeriod, resolve_exam_period
from exam_engine.config import CoverageSettings, EvolutionSettings, ExamDnaSettings
from exam_engine.coverage.service import CoverageError, CoverageService
from exam_engine.evolution.dates import DocumentDate, resolve_document_date
from exam_engine.exam_dna.service import (
    ExamDnaError,
    ExamDnaService,
    _virtual_paper_document,
)
from exam_engine.graph.service import GraphService
from exam_engine.models.analytics import CourseScope
from exam_engine.models.course import CourseRecord
from exam_engine.models.coverage import CoverageFilters, CoverageRequest
from exam_engine.models.document import DocumentRecord, PageRecord
from exam_engine.models.enums import (
    ConceptChangeCategory,
    DocumentKind,
    EvidenceQuality,
    EvolutionDateType,
    EvolutionReviewStatus,
    EvolutionStatus,
    ExtractionStatus,
    GraphNodeType,
    GraphRelationship,
    QuestionGroupKind,
    Severity,
)
from exam_engine.models.evolution import (
    EVOLUTION_ALGORITHM,
    EVOLUTION_ALGORITHM_VERSION,
    EVOLUTION_SCHEMA_VERSION,
    AssessmentWindow,
    ChangeSide,
    ConceptAssessment,
    ConceptChange,
    ConceptTimeline,
    DateCandidate,
    DescriptionChange,
    EvolutionDataQuality,
    EvolutionDocument,
    EvolutionExclusion,
    EvolutionObservation,
    EvolutionReport,
    EvolutionRequest,
    EvolutionScope,
    EvolutionSummary,
    SyllabusComparison,
    SyllabusVersionRef,
    UnitMove,
    make_change_id,
    make_observation_id,
    make_report_id,
)
from exam_engine.models.exam_dna import ExamDnaRequest
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import SyllabusRecord, SyllabusUnitRecord
from exam_engine.models.understanding import QuestionAnalysis, UnitMatch
from exam_engine.nlp.keywords import normalize_term
from exam_engine.storage.db import Database
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from exam_engine.storage.schema import ExamPaperRow, ExamQuestionDetailRow

logger = logging.getLogger(__name__)

_WARNING = {
    "evolution_no_documents": (
        "No course documents are available; a concept timeline cannot be built."
    ),
    "evolution_no_observations": (
        "No concept term was observed in the selected documents; no evolution claim is made."
    ),
    "evolution_no_dated_observations": (
        "No observation carries a usable source date; chronological change cannot be established."
    ),
    "evolution_missing_date": (
        "A document has no usable source date; it is reported but cannot be placed on the "
        "timeline by date."
    ),
    "evolution_conflicting_dates": (
        "A document states more than one source date; the selected date is reported and the "
        "conflict is flagged instead of silently resolved."
    ),
    "evolution_invalid_date": (
        "A source date could not be parsed and was not replaced with a guessed value."
    ),
    "evolution_graph_unavailable": (
        "Phase 5 concept nodes are unavailable; concept identity falls back to syllabus topics."
    ),
    "evolution_unknown_version_order": (
        "Syllabus versions lack reliable effective dates; their order is not assumed."
    ),
    "evolution_incomplete_extraction": (
        "A document's extraction was incomplete; absence of a concept is not proof of removal."
    ),
    "evolution_undated_excluded": (
        "Undated records were excluded from a date filter rather than silently included."
    ),
    "evolution_insufficient_observations": (
        "A concept has too few dated observations to support a change claim."
    ),
    "evolution_context_similarity": (
        "Contextual comparison uses lexical similarity between excerpts; it is a review "
        "signal, not proof that the concept's meaning changed."
    ),
    "evolution_ambiguous_alias": (
        "A term maps to several concept identities; it was not used as single-concept evidence."
    ),
    "evolution_mixed_formats": (
        "Documents of different kinds or examination types were compared descriptively; their "
        "metrics may not be equivalent."
    ),
    "evolution_no_coverage": (
        "Phase 7 coverage is unavailable; assessment evolution is limited to raw records."
    ),
    "evolution_small_assessment": (
        "A concept has too few dated questions in one or both comparison windows to report an "
        "assessment change."
    ),
    "evolution_truncated": "A configured Phase 9 limit truncated the analyzed corpus.",
    "evolution_context_unavailable": (
        "No comparable excerpts were available for a contextual observation."
    ),
}

_EXCLUDING_EXTRACTION = {
    ExtractionStatus.FAILED,
    ExtractionStatus.EMPTY,
    ExtractionStatus.NEEDS_OCR,
}
_ASSESSMENT_KINDS = {DocumentKind.QUESTION_PAPER, DocumentKind.QUESTION_BANK}
_EXAM_PERIOD_SOURCES = {"exam_session", "exam_date"}
# ``concept_related`` only means two questions share a concept, which is already the
# subject of the assessment, so it is not counted as repetition evidence.
_REPETITION_KINDS = {
    QuestionGroupKind.EXACT,
    QuestionGroupKind.NEAR_DUPLICATE,
    QuestionGroupKind.SEMANTIC,
}
_RELATIONSHIP_KINDS = {
    GraphRelationship.RELATED_TO,
    GraphRelationship.PREREQUISITE_OF,
    GraphRelationship.SIMILAR_TO,
}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_TOKEN_STRIP = re.compile(r"[^a-z0-9 ]+")


class EvolutionError(ValueError):
    """Structured scope/filter failure for the CLI and other callers."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class _ConceptRef:
    concept_id: str
    label: str
    normalized: str
    aliases: dict[str, str] = field(default_factory=dict)
    verified: bool = True
    quality: float = 0.6
    unit_ids: set[str] = field(default_factory=set)
    syllabus_ids: set[str] = field(default_factory=set)
    document_ids: set[str] = field(default_factory=set)


@dataclass
class _Placement:
    concept_id: str
    label: str
    alias: str
    unit_id: str
    unit_label: str
    unit_number: int | None
    syllabus_id: str


@dataclass
class _QuestionRow:
    """One dated question used by an assessment window.

    The exact date is optional: an examination metadata entry such as ``2020-05``
    names a period, not a day. The period key orders such a row without inventing a
    date, exactly as Phase 4 already does.
    """

    question_id: str
    question: QuestionRecord
    date_value: date | None
    period_key: str | None
    period_label: str | None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        anchor = self.date_value.isoformat() if self.date_value else self.period_key or "~"
        return (anchor, self.period_key or "", self.question_id)


@dataclass
class _SyllabusData:
    syllabus: SyllabusRecord
    document: DocumentRecord
    resolution: DocumentDate
    placements: dict[str, list[_Placement]] = field(default_factory=dict)
    units: dict[str, SyllabusUnitRecord] = field(default_factory=dict)


@dataclass
class _Loaded:
    course: CourseRecord
    documents: list[DocumentRecord]
    syllabi: list[SyllabusRecord]
    questions: dict[str, QuestionRecord]
    analyses: dict[str, QuestionAnalysis]
    matches: dict[str, list[UnitMatch]]
    periods: dict[str, ExamPeriod]
    resolutions: dict[str, DocumentDate]
    pages: dict[str, list[PageRecord]]
    concepts: dict[str, _ConceptRef]
    alias_index: dict[str, set[str]]
    ambiguous_aliases: dict[str, set[str]]
    syllabus_data: list[_SyllabusData]
    graph_available: bool
    syllabus_by_document: dict[str, SyllabusRecord]


def _round(value: float) -> float:
    return round(float(value), 4)


def _rate(count: float, total: float) -> float | None:
    return _round(count / total) if total else None


def _warning(code: str, message: str | None = None, *, severity: Severity = Severity.WARNING):
    return Uncertainty(code=code, message=message or _WARNING.get(code, code), severity=severity)


def _dedupe_warnings(items: Iterable[Uncertainty]) -> list[Uncertainty]:
    seen: set[tuple[str, str]] = set()
    result: list[Uncertainty] = []
    for item in items:
        key = (item.code, item.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _course_scope(course: CourseRecord) -> CourseScope:
    return CourseScope(
        course_id=course.course_id,
        course_code=course.course_code,
        title=course.title,
        institution_code=course.institution_code,
        institution_name=course.institution_name,
        department=course.department,
        programme=course.programme,
        academic_year=course.academic_year,
        syllabus_version=course.syllabus_version,
    )


def _truncate_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _tokens(text: str) -> set[str]:
    cleaned = _TOKEN_STRIP.sub(" ", text.casefold())
    return {word for word in cleaned.split() if len(word) > 2}


def _jaccard(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    return _round(len(left_tokens & right_tokens) / len(union))


def _contains(normalized_text: str, normalized_term: str) -> bool:
    if not normalized_term:
        return False
    return f" {normalized_term} " in f" {normalized_text} "


def _excerpt(text: str, term: str, limit: int) -> tuple[str, int | None]:
    """First sentence containing the term, with a 1-based sentence index."""
    for index, sentence in enumerate(_SENTENCE_SPLIT.split(text), start=1):
        if _contains(normalize_term(sentence), term):
            return _truncate_text(sentence, limit), index
    return _truncate_text(text, limit), None


class ConceptEvolutionService:
    """Analyse concept appearances and changes inside one course corpus."""

    def __init__(
        self,
        database: Database,
        settings: EvolutionSettings | None = None,
        *,
        coverage_settings: CoverageSettings | None = None,
        exam_dna_settings: ExamDnaSettings | None = None,
        graph: GraphService | None = None,
    ) -> None:
        self.db = database
        self.settings = settings or EvolutionSettings()
        self.coverage_settings = coverage_settings or CoverageSettings()
        self.exam_dna_settings = exam_dna_settings or ExamDnaSettings()
        self.graph = graph or GraphService(database)
        self._assessment_by_concept: dict[str, ConceptAssessment] = {}

    # -- public API --------------------------------------------------------

    def analyze(self, request: EvolutionRequest) -> EvolutionReport:
        loaded, exclusions, truncated = self._load(request)
        selected_docs = [
            document
            for document in loaded.documents
            if document.document_id not in {item.document_id for item in exclusions}
        ]
        observations, doc_observations = self._observations(loaded, selected_docs, request)
        timelines, timeline_warnings = self._timelines(loaded, observations, request)
        comparisons, comparison_warnings = self._syllabus_comparisons(loaded, request)
        assessment, exam_ids, coverage_ids, assessment_warnings = self._assessment(loaded, request)
        changes, change_warnings = self._changes(loaded, timelines, comparisons, request)
        quality = self._data_quality(
            loaded,
            selected_docs,
            observations,
            exclusions,
            comparisons,
            coverage_ids,
            exam_ids,
            truncated=truncated,
        )
        warnings = _dedupe_warnings(
            [
                *timeline_warnings,
                *change_warnings,
                *comparison_warnings,
                *assessment_warnings,
                *self._report_warnings(loaded, selected_docs, observations, comparisons),
            ]
        )
        status = self._status(loaded, observations, quality, comparisons, warnings)
        summary = self._summary(
            selected_docs, observations, timelines, changes, comparisons, assessment
        )
        parameters = {
            **self.settings.as_config(),
            "date_policy": (
                "per_source: syllabus=effective_then_publication, textbook=edition_then_"
                "publication, question_paper=examination_then_publication, other=publication"
            ),
            "ingestion_time_used": False,
            "metrics": {
                "observation_order": (
                    "date ascending, then date-type priority, then document id, then term"
                ),
                "context_similarity": (
                    "token Jaccard between the earliest and latest dated excerpts"
                ),
                "assessment_share": (
                    "questions with a label / analysed questions in the same window"
                ),
                "assessment_marks_share": (
                    "questions stating a marks value / questions with known marks in the window"
                ),
                "assessment_unit_counts": "top Phase 3 unit match per question in the window",
                "assessment_repetition": (
                    "Phase 8 exact/near/semantic group membership; concept_related is excluded"
                ),
                "recurrence_note": ("counts are raw; shares carry their own window denominator"),
            },
            "coverage_report_ids": coverage_ids,
            "exam_dna_profile_ids": exam_ids,
        }
        scope = self._scope(loaded, selected_docs, request, observations)
        report_id = make_report_id(
            "\x1e".join(
                [
                    request.course_id,
                    request.filters.model_dump_json(),
                    str(sorted(parameters.items())),
                    ",".join(
                        f"{document.document_id}:{loaded.resolutions[document.document_id].value}"
                        for document in selected_docs
                    ),
                    ",".join(f"{item.observation_id}:{item.date}" for item in observations),
                    ",".join(change.change_id for change in changes),
                    ",".join(f"{item.concept_id}:{item.changed}" for item in assessment),
                    ",".join(coverage_ids),
                    ",".join(exam_ids),
                ]
            )
        )
        return EvolutionReport(
            schema_version=EVOLUTION_SCHEMA_VERSION,
            report_id=report_id,
            algorithm=EVOLUTION_ALGORITHM,
            algorithm_version=EVOLUTION_ALGORITHM_VERSION,
            course=_course_scope(loaded.course),
            status=status,
            filters=request.filters,
            scope=scope,
            parameters=parameters,
            summary=summary,
            documents=[
                self._document_row(loaded, document, doc_observations.get(document.document_id, 0))
                for document in selected_docs
            ],
            timelines=timelines,
            changes=changes,
            syllabus_comparisons=comparisons,
            assessment=assessment,
            data_quality=quality,
            warnings=warnings,
            limitations=self._limitations(loaded, quality, comparisons),
        )

    def timeline(self, request: EvolutionRequest, concept: str) -> ConceptTimeline:
        report = self.analyze(request)
        normalized = normalize_term(concept)
        matches = [
            item
            for item in report.timelines
            if item.concept_id == concept or normalize_term(item.canonical_label) == normalized
        ]
        if not matches:
            raise EvolutionError(
                "unknown_concept",
                f"unknown concept: {concept}",
                detail={"course_id": request.course_id, "concept": concept},
            )
        if len(matches) > 1:
            raise EvolutionError(
                "ambiguous_concept",
                f"ambiguous concept {concept!r}; pass the concept id",
                detail={"concept_ids": [item.concept_id for item in matches]},
            )
        return matches[0]

    def changes(
        self,
        request: EvolutionRequest,
        *,
        category: ConceptChangeCategory | None = None,
        min_confidence: float | None = None,
        review_only: bool = False,
        report: EvolutionReport | None = None,
    ) -> list[ConceptChange]:
        """Filter detected changes; pass ``report`` to avoid a second analysis."""
        value = self.analyze(request) if report is None else report
        cutoff = self.settings.min_change_confidence if min_confidence is None else min_confidence
        return [
            change
            for change in value.changes
            if (category is None or change.category is category)
            and change.confidence >= cutoff
            and (not review_only or change.review_required)
        ]

    def compare_syllabi(
        self, request: EvolutionRequest, earlier: str, later: str
    ) -> SyllabusComparison:
        filters = request.filters.model_copy(update={"syllabus_ids": [earlier, later]})
        scoped = request.model_copy(update={"filters": filters})
        report = self.analyze(scoped)
        for comparison in report.syllabus_comparisons:
            ids = {comparison.earlier.syllabus_id, comparison.later.syllabus_id}
            if ids == {earlier, later}:
                return comparison
        raise EvolutionError(
            "unknown_syllabus_comparison",
            "the requested syllabus versions were not both selected and comparable",
            detail={"earlier": earlier, "later": later},
        )

    # -- loading -----------------------------------------------------------

    def _load(self, request: EvolutionRequest) -> tuple[_Loaded, list[EvolutionExclusion], bool]:
        with self.db.session() as session:
            course = CourseRepository(session).get(request.course_id)
            if course is None:
                raise EvolutionError("unknown_course", f"unknown course: {request.course_id}")
            documents = DocumentRepository(session).list_by_course(request.course_id)
            syllabi = SyllabusRepository(session).get_by_course(request.course_id)
            questions = QuestionRepository(session).list_by_course(request.course_id)
            from exam_engine.storage.question_repositories import (
                QuestionUnderstandingRepository,
            )

            understanding = QuestionUnderstandingRepository(session)
            analyses = {
                item.question_id: item for item in understanding.list_analyses(request.course_id)
            }
            matches: dict[str, list[UnitMatch]] = defaultdict(list)
            for match in understanding.list_unit_matches_for_course(request.course_id):
                matches[match.question_id].append(match)
            paper_rows = {
                row.paper_id: row
                for row in session.scalars(
                    select(ExamPaperRow).where(ExamPaperRow.course_id == request.course_id)
                ).all()
            }
            paper_by_question: dict[str, str] = {
                row[0]: row[1]
                for row in session.execute(
                    select(
                        ExamQuestionDetailRow.question_id,
                        ExamQuestionDetailRow.paper_id,
                    ).where(ExamQuestionDetailRow.course_id == request.course_id)
                ).all()
            }

        questions_by_document: dict[str, list[QuestionRecord]] = defaultdict(list)
        for question in questions:
            questions_by_document[question.document_id].append(question)
        scoped_documents: list[DocumentRecord] = []
        question_documents: dict[str, str] = {}
        for document in documents:
            items = questions_by_document.get(document.document_id, [])
            if document.kind is DocumentKind.QUESTION_BANK and items:
                grouped: dict[str, list[QuestionRecord]] = defaultdict(list)
                for question in items:
                    paper_id = paper_by_question.get(question.question_id)
                    if paper_id is not None:
                        grouped[paper_id].append(question)
                if grouped and sum(len(group) for group in grouped.values()) == len(items):
                    for paper_id, group in sorted(grouped.items()):
                        paper_row = paper_rows.get(paper_id)
                        if paper_row is None:
                            continue
                        virtual = _virtual_paper_document(document, paper_row, group)
                        for question in group:
                            question_documents[question.question_id] = virtual.document_id
                        scoped_documents.append(virtual)
                    continue
            scoped_documents.append(document)
        documents = scoped_documents
        if question_documents:
            questions = [
                question.model_copy(
                    update={"document_id": question_documents[question.question_id]}
                )
                if question.question_id in question_documents
                else question
                for question in questions
            ]

        periods = {
            document.document_id: resolve_exam_period(document, course) for document in documents
        }
        resolutions = {
            document.document_id: resolve_document_date(
                document, period=periods[document.document_id]
            )
            for document in documents
        }
        syllabus_documents = {syllabus.document_id: syllabus.syllabus_id for syllabus in syllabi}
        exclusions = self._select_documents(request, documents, resolutions, syllabus_documents)
        selected_ids = {
            document.document_id
            for document in documents
            if document.document_id not in {item.document_id for item in exclusions}
        }
        selected_documents = [
            document for document in documents if document.document_id in selected_ids
        ]
        truncated = False
        if len(selected_documents) > self.settings.max_documents:
            selected_documents = sorted(
                selected_documents,
                key=lambda item: (
                    resolutions[item.document_id].sort_key,
                    item.document_id,
                ),
            )[: self.settings.max_documents]
            selected_ids = {item.document_id for item in selected_documents}
            truncated = True

        pages: dict[str, list[PageRecord]] = {}
        with self.db.session() as session:
            repository = DocumentRepository(session)
            for document in selected_documents:
                pages[document.document_id] = repository.list_pages(document.document_id)

        selected_syllabi = [
            syllabus for syllabus in syllabi if syllabus.document_id in selected_ids
        ]
        (
            concepts,
            alias_index,
            ambiguous_aliases,
            syllabus_data,
            graph_available,
        ) = self._concept_registry(
            request.course_id, selected_syllabi, selected_documents, resolutions
        )
        syllabus_by_document = {item.document_id: item for item in selected_syllabi}

        self._validate_filters(
            request,
            documents,
            syllabi,
            concepts,
            alias_index,
        )

        concept_ids = set(concepts)
        if request.filters.concept_ids:
            requested = set(request.filters.concept_ids)
            normalized = {normalize_term(value) for value in requested}
            concept_ids = {
                cid
                for cid in concepts
                if cid in requested or concepts[cid].normalized in normalized
            }
            for value in requested:
                concept_ids.update(alias_index.get(normalize_term(value), set()))
        if len(concept_ids) > self.settings.max_concepts:
            concept_ids = set(sorted(concept_ids)[: self.settings.max_concepts])
            truncated = True
        concepts = {cid: value for cid, value in concepts.items() if cid in concept_ids}
        alias_index = {
            alias: {cid for cid in ids if cid in concepts} for alias, ids in alias_index.items()
        }
        alias_index = {alias: ids for alias, ids in alias_index.items() if ids}
        syllabus_data = [
            data
            for data in syllabus_data
            if request.filters.syllabus_ids == []
            or data.syllabus.syllabus_id in request.filters.syllabus_ids
        ]

        return (
            _Loaded(
                course=course,
                documents=selected_documents,
                syllabi=selected_syllabi,
                questions={item.question_id: item for item in questions},
                analyses=analyses,
                matches=matches,
                periods=periods,
                resolutions=resolutions,
                pages=pages,
                concepts=concepts,
                alias_index=alias_index,
                ambiguous_aliases=ambiguous_aliases,
                syllabus_data=syllabus_data,
                graph_available=graph_available,
                syllabus_by_document=syllabus_by_document,
            ),
            exclusions,
            truncated,
        )

    def _select_documents(
        self,
        request: EvolutionRequest,
        documents: list[DocumentRecord],
        resolutions: dict[str, DocumentDate],
        syllabus_documents: dict[str, str],
    ) -> list[EvolutionExclusion]:
        filters = request.filters
        known_ids = {document.document_id for document in documents}
        unknown_documents = sorted(set(filters.document_ids) - known_ids)
        if unknown_documents:
            raise EvolutionError(
                "invalid_evolution_filter",
                "a document filter names data outside the selected course",
                detail={"unknown_document_ids": unknown_documents},
            )
        exclusions: list[EvolutionExclusion] = []
        for document in documents:
            resolution = resolutions[document.document_id]
            if filters.document_ids and document.document_id not in filters.document_ids:
                exclusions.append(
                    EvolutionExclusion(
                        document_id=document.document_id,
                        title=document.title,
                        reason="filtered_document",
                    )
                )
                continue
            if filters.document_kinds and document.kind not in filters.document_kinds:
                exclusions.append(
                    EvolutionExclusion(
                        document_id=document.document_id,
                        title=document.title,
                        reason="filtered_kind",
                    )
                )
                continue
            if (
                filters.syllabus_ids
                and document.document_id in syllabus_documents
                and syllabus_documents[document.document_id] not in filters.syllabus_ids
            ):
                exclusions.append(
                    EvolutionExclusion(
                        document_id=document.document_id,
                        title=document.title,
                        reason="filtered_syllabus",
                    )
                )
                continue
            if filters.date_from is not None or filters.date_to is not None:
                if resolution.value is None:
                    exclusions.append(
                        EvolutionExclusion(
                            document_id=document.document_id,
                            title=document.title,
                            reason="filtered_undated",
                        )
                    )
                    continue
                if filters.date_from is not None and resolution.value < filters.date_from:
                    exclusions.append(
                        EvolutionExclusion(
                            document_id=document.document_id,
                            title=document.title,
                            reason="filtered_date",
                        )
                    )
                    continue
                if filters.date_to is not None and resolution.value > filters.date_to:
                    exclusions.append(
                        EvolutionExclusion(
                            document_id=document.document_id,
                            title=document.title,
                            reason="filtered_date",
                        )
                    )
                    continue
        return exclusions

    def _concept_registry(
        self,
        course_id: str,
        syllabi: list[SyllabusRecord],
        documents: list[DocumentRecord],
        resolutions: dict[str, DocumentDate],
    ) -> tuple[
        dict[str, _ConceptRef],
        dict[str, set[str]],
        dict[str, set[str]],
        list[_SyllabusData],
        bool,
    ]:
        concepts: dict[str, _ConceptRef] = {}
        alias_index: dict[str, set[str]] = defaultdict(set)
        ambiguous: dict[str, set[str]] = defaultdict(set)

        graph_nodes = []
        try:
            graph_nodes = self.graph.concepts(course_id)
        except Exception as exc:  # noqa: BLE001 - graph is optional evidence
            logger.info("graph concepts unavailable: %s", exc)
        for node in graph_nodes:
            aliases = {node.label} | {
                str(alias) for alias in node.metadata.get("aliases", []) if str(alias).strip()
            }
            normalized = node.normalized_label or normalize_term(node.label)
            ref = _ConceptRef(
                concept_id=node.node_id,
                label=node.label,
                normalized=normalized,
                aliases={normalize_term(alias): alias for alias in aliases},
                verified=bool(node.metadata.get("verified", False)),
                quality=float(
                    node.metadata.get("confidence")
                    or (0.8 if node.metadata.get("verified") else 0.5)
                ),
            )
            concepts[ref.concept_id] = ref
            for alias in ref.aliases:
                alias_index[alias].add(ref.concept_id)
        graph_available = bool(concepts)

        syllabus_data: list[_SyllabusData] = []
        documents_by_id = {document.document_id: document for document in documents}
        for syllabus in syllabi:
            document = documents_by_id.get(syllabus.document_id)
            if document is None:
                continue
            data = _SyllabusData(
                syllabus=syllabus,
                document=document,
                resolution=resolutions[document.document_id],
            )
            for unit in syllabus.units:
                data.units[normalize_term(unit.unit_label)] = unit
                for topic in unit.topics:
                    alias = normalize_term(topic)
                    if not alias:
                        continue
                    ids = alias_index.get(alias, set())
                    if not ids:
                        concept_id = f"concept:{alias}"
                        if concept_id not in concepts:
                            concepts[concept_id] = _ConceptRef(
                                concept_id=concept_id,
                                label=topic,
                                normalized=alias,
                                aliases={alias: topic},
                                verified=True,
                                quality=unit.confidence,
                            )
                        ids = {concept_id}
                        alias_index[alias].add(concept_id)
                    elif len(ids) > 1:
                        ambiguous[alias].update(ids)
                    for concept_id in sorted(ids):
                        data.placements.setdefault(concept_id, []).append(
                            _Placement(
                                concept_id=concept_id,
                                label=topic,
                                alias=alias,
                                unit_id=unit.unit_id,
                                unit_label=unit.unit_label,
                                unit_number=unit.unit_number,
                                syllabus_id=syllabus.syllabus_id,
                            )
                        )
                        ref = concepts[concept_id]
                        ref.unit_ids.add(unit.unit_id)
                        ref.syllabus_ids.add(syllabus.syllabus_id)
            syllabus_data.append(data)

        for ref in concepts.values():
            ref.aliases.setdefault(ref.normalized, ref.label)
            alias_index[ref.normalized].add(ref.concept_id)
        return concepts, dict(alias_index), dict(ambiguous), syllabus_data, graph_available

    def _validate_filters(
        self,
        request: EvolutionRequest,
        documents: list[DocumentRecord],
        syllabi: list[SyllabusRecord],
        concepts: dict[str, _ConceptRef],
        alias_index: dict[str, set[str]],
    ) -> None:
        filters = request.filters
        known_syllabi = {syllabus.syllabus_id for syllabus in syllabi}
        unknown_syllabi = sorted(set(filters.syllabus_ids) - known_syllabi)
        if unknown_syllabi:
            raise EvolutionError(
                "invalid_evolution_filter",
                "a syllabus filter names data outside the selected course",
                detail={"unknown_syllabus_ids": unknown_syllabi},
            )
        if filters.concept_ids:
            normalized = {normalize_term(value) for value in filters.concept_ids}
            known_ids = {
                cid
                for cid, ref in concepts.items()
                if cid in filters.concept_ids or ref.normalized in normalized
            }
            if not known_ids:
                raise EvolutionError(
                    "invalid_evolution_filter",
                    "a concept filter matches no concept in the selected course",
                    detail={"unknown_concept_ids": sorted(set(filters.concept_ids))},
                )
        if aliases := set(filters.concept_ids) - set(concepts):
            unresolved = [value for value in aliases if normalize_term(value) not in alias_index]
            if unresolved:
                raise EvolutionError(
                    "invalid_evolution_filter",
                    "a concept filter matches no concept in the selected course",
                    detail={"unknown_concept_ids": sorted(unresolved)},
                )

    # -- observations ------------------------------------------------------

    def _observations(
        self,
        loaded: _Loaded,
        documents: list[DocumentRecord],
        request: EvolutionRequest,
    ) -> tuple[list[EvolutionObservation], dict[str, int]]:
        observations: list[EvolutionObservation] = []
        per_document: dict[str, int] = defaultdict(int)
        for document in documents:
            items: list[EvolutionObservation] = []
            if document.kind is DocumentKind.SYLLABUS:
                items = self._syllabus_observations(loaded, document)
            elif document.kind in _ASSESSMENT_KINDS:
                items = self._question_observations(loaded, document)
            else:
                items = self._text_observations(loaded, document)
            per_document[document.document_id] = len(items)
            observations.extend(items)

        per_concept: dict[str, list[EvolutionObservation]] = defaultdict(list)
        seen: set[str] = set()
        for observation in observations:
            if observation.observation_id in seen:
                continue
            seen.add(observation.observation_id)
            per_concept[observation.concept_id].append(observation)
        kept: list[EvolutionObservation] = []
        for concept_id in sorted(per_concept):
            items = sorted(
                per_concept[concept_id],
                key=lambda item: (
                    item.date is None,
                    item.date.isoformat() if item.date else "",
                    item.date_type.value,
                    item.document_id,
                    item.observed_term.casefold(),
                    item.observation_id,
                ),
            )
            kept.extend(items[: self.settings.max_observations_per_concept])
        kept.sort(
            key=lambda item: (
                item.date is None,
                item.date.isoformat() if item.date else "",
                item.date_type.value,
                item.concept_id,
                item.document_id,
                item.observed_term.casefold(),
                item.observation_id,
            )
        )
        return kept, per_document

    def _observation(
        self,
        loaded: _Loaded,
        concept: _ConceptRef,
        *,
        document: DocumentRecord,
        term: str,
        alias: str,
        method: str,
        confidence: float,
        excerpt: str,
        unit: SyllabusUnitRecord | None = None,
        unit_label: str | None = None,
        syllabus_id: str | None = None,
        question_ids: list[str] | None = None,
        page_number: int | None = None,
        related: list[str] | None = None,
    ) -> EvolutionObservation:
        resolution = loaded.resolutions[document.document_id]
        observation_id = make_observation_id(
            "\x1f".join(
                [
                    concept.concept_id,
                    document.document_id,
                    alias,
                    unit.unit_id if unit else (unit_label or ""),
                    ",".join(sorted(question_ids or [])),
                ]
            )
        )
        return EvolutionObservation(
            observation_id=observation_id,
            concept_id=concept.concept_id,
            canonical_label=concept.label,
            observed_term=term,
            verified_term=concept.verified,
            document_id=document.document_id,
            document_title=document.title,
            document_kind=document.kind,
            date=resolution.value,
            date_type=resolution.date_type,
            date_source=resolution.source,
            date_confident=resolution.confident,
            syllabus_id=syllabus_id,
            unit_label=unit.unit_label if unit else unit_label,
            unit_number=unit.unit_number if unit else None,
            excerpt=_truncate_text(excerpt, self.settings.max_excerpt_chars),
            page_number=page_number or (unit.page_number if unit else None),
            question_ids=sorted(set(question_ids or [])),
            related_concepts=sorted(set(related or []))[:10],
            mapping_method=method,
            mapping_confidence=_round(min(1.0, max(0.0, confidence))),
        )

    def _syllabus_observations(
        self, loaded: _Loaded, document: DocumentRecord
    ) -> list[EvolutionObservation]:
        data = next(
            (
                item
                for item in loaded.syllabus_data
                if item.syllabus.document_id == document.document_id
            ),
            None,
        )
        if data is None:
            return []
        observations: list[EvolutionObservation] = []
        for concept_id in sorted(data.placements):
            concept = loaded.concepts.get(concept_id)
            if concept is None:
                continue
            placements = {item.unit_id: item for item in data.placements[concept_id]}
            for placement in placements.values():
                unit = data.units.get(normalize_term(placement.unit_label))
                if unit is None:
                    continue
                siblings = sorted(
                    {
                        other_id
                        for other_id, other_placements in data.placements.items()
                        if any(item.unit_id == unit.unit_id for item in other_placements)
                    }
                    - {concept_id}
                )
                observations.append(
                    self._observation(
                        loaded,
                        concept,
                        document=document,
                        term=placement.label,
                        alias=placement.alias,
                        method="syllabus_topic",
                        confidence=data.syllabus.confidence or unit.confidence,
                        excerpt=unit.title or unit.raw_text or placement.label,
                        unit=unit,
                        syllabus_id=data.syllabus.syllabus_id,
                        related=siblings,
                    )
                )
        return observations

    def _question_observations(
        self, loaded: _Loaded, document: DocumentRecord
    ) -> list[EvolutionObservation]:
        questions = [
            item for item in loaded.questions.values() if item.document_id == document.document_id
        ]
        questions.sort(
            key=lambda item: (item.number_sort is None, item.number_sort or 0, item.question_id)
        )
        observations: list[EvolutionObservation] = []
        for question in questions:
            normalized_text = normalize_term(question.text)
            matched: dict[str, tuple[str, str, float, str | None, str | None]] = {}
            # A Phase 3 unit mapping is the strongest available identity evidence.
            for match in loaded.matches.get(question.question_id, []):
                for alias in {
                    normalize_term(match.topic or ""),
                    normalize_term(match.unit_label),
                }:
                    ids = loaded.alias_index.get(alias, set())
                    if not alias or len(ids) != 1:
                        continue
                    concept_id = next(iter(ids))
                    if concept_id not in loaded.concepts:
                        continue
                    if not _contains(normalized_text, alias):
                        continue
                    matched[concept_id] = (
                        match.topic or match.unit_label,
                        alias,
                        match.score,
                        match.unit_label,
                        match.syllabus_id,
                    )
            # Direct mentions still count as observations, at lower confidence.
            for alias, ids in sorted(loaded.alias_index.items()):
                if len(ids) != 1 or len(alias) < 3:
                    continue
                concept_id = next(iter(ids))
                if concept_id in matched or concept_id not in loaded.concepts:
                    continue
                if not _contains(normalized_text, alias):
                    continue
                concept = loaded.concepts[concept_id]
                matched[concept_id] = (
                    concept.aliases.get(alias, concept.label),
                    alias,
                    0.55,
                    None,
                    None,
                )
            if not matched:
                continue
            for concept_id, (term, alias, score, unit_label, syllabus_id) in sorted(
                matched.items()
            ):
                concept = loaded.concepts[concept_id]
                observations.append(
                    self._observation(
                        loaded,
                        concept,
                        document=document,
                        term=term,
                        alias=alias,
                        method="question_mapping" if syllabus_id else "question_text",
                        confidence=score,
                        excerpt=question.text,
                        unit_label=unit_label,
                        syllabus_id=syllabus_id,
                        question_ids=[question.question_id],
                        page_number=question.page_number,
                        related=[other for other in matched if other != concept_id],
                    )
                )
        return observations

    def _text_observations(
        self, loaded: _Loaded, document: DocumentRecord
    ) -> list[EvolutionObservation]:
        pages = loaded.pages.get(document.document_id, [])
        if not pages:
            return []
        text = "\n".join(page.text for page in pages if page.text.strip())
        if not text.strip():
            return []
        normalized_text = normalize_term(text)
        observations: list[EvolutionObservation] = []
        for concept_id in sorted(loaded.concepts):
            concept = loaded.concepts[concept_id]
            aliases = sorted(
                alias
                for alias, ids in loaded.alias_index.items()
                if concept_id in ids and len(ids) == 1 and len(alias) >= 3
            )
            for alias in aliases:
                if not _contains(normalized_text, alias):
                    continue
                page_number = next(
                    (
                        page.page_number
                        for page in pages
                        if page.text and _contains(normalize_term(page.text), alias)
                    ),
                    None,
                )
                excerpt, _ = _excerpt(text, alias, self.settings.max_excerpt_chars)
                term = concept.aliases.get(alias, concept.label)
                observations.append(
                    self._observation(
                        loaded,
                        concept,
                        document=document,
                        term=term,
                        alias=alias,
                        method="document_text",
                        confidence=0.5,
                        excerpt=excerpt,
                        page_number=page_number,
                    )
                )
                break
        return observations

    def _document_row(
        self,
        loaded: _Loaded,
        document: DocumentRecord,
        observation_count: int,
    ) -> EvolutionDocument:
        resolution = loaded.resolutions[document.document_id]
        syllabus = loaded.syllabus_by_document.get(document.document_id)
        warnings: list[str] = []
        if resolution.value is None:
            warnings.append("evolution_missing_date")
        if resolution.conflicts:
            warnings.append("evolution_conflicting_dates")
        if resolution.invalid:
            warnings.append("evolution_invalid_date")
        if document.extraction_status in {
            ExtractionStatus.PARTIAL,
            *_EXCLUDING_EXTRACTION,
        }:
            warnings.append("evolution_incomplete_extraction")
        question_count = sum(
            1 for item in loaded.questions.values() if item.document_id == document.document_id
        )
        return EvolutionDocument(
            document_id=document.document_id,
            title=document.title,
            kind=document.kind,
            source_id=document.source_id,
            syllabus_id=syllabus.syllabus_id if syllabus else None,
            date=resolution.value,
            date_type=resolution.date_type,
            date_source=resolution.source,
            date_confident=resolution.confident,
            date_candidates=[
                DateCandidate(source=item.source, date=item.value, date_type=item.date_type)
                for item in resolution.candidates
            ],
            extraction_status=document.extraction_status,
            question_count=question_count,
            observation_count=observation_count,
            warnings=warnings,
        )

    # -- timelines ---------------------------------------------------------

    def _timelines(
        self,
        loaded: _Loaded,
        observations: list[EvolutionObservation],
        request: EvolutionRequest,
    ) -> tuple[list[ConceptTimeline], list[Uncertainty]]:
        by_concept: dict[str, list[EvolutionObservation]] = defaultdict(list)
        for observation in observations:
            by_concept[observation.concept_id].append(observation)
        warnings: list[Uncertainty] = []
        timelines: list[ConceptTimeline] = []
        insufficient = 0
        for concept_id in sorted(loaded.concepts):
            if concept_id not in by_concept:
                continue
            concept = loaded.concepts[concept_id]
            items = by_concept[concept_id]
            dated = [item for item in items if item.date is not None]
            limitations: list[str] = []
            if len(dated) < self.settings.min_dated_observations:
                limitations.append(
                    "fewer than the configured minimum of dated observations; no temporal "
                    "claim is made"
                )
                insufficient += 1
            if any(item.date is None for item in items):
                limitations.append(
                    "some observations have no source date and are ordered after dated ones"
                )
            if len({item.document_id for item in dated}) < 2:
                limitations.append("dated observations come from fewer than two documents")
            if concept.quality < self.settings.min_syllabus_confidence:
                limitations.append(
                    "concept identity relies on low-confidence syllabus segmentation"
                )
            timelines.append(
                ConceptTimeline(
                    concept_id=concept.concept_id,
                    canonical_label=concept.label,
                    aliases=sorted(set(concept.aliases.values()), key=str.casefold),
                    first_observed=dated[0].date if dated else None,
                    last_observed=dated[-1].date if dated else None,
                    observation_count=len(items),
                    dated_observation_count=len(dated),
                    document_count=len({item.document_id for item in items}),
                    syllabus_ids=sorted({item.syllabus_id for item in items if item.syllabus_id}),
                    unit_labels=sorted({item.unit_label for item in items if item.unit_label}),
                    observations=items,
                    evidence_quality=(
                        EvidenceQuality.HIGH
                        if len(dated) >= 3 and len({item.document_id for item in dated}) >= 2
                        else EvidenceQuality.MEDIUM
                        if len(dated) >= 2
                        else EvidenceQuality.LOW
                    ),
                    limitations=limitations,
                )
            )
        timelines.sort(key=lambda item: (item.canonical_label.casefold(), item.concept_id))
        if insufficient:
            warnings.append(
                _warning(
                    "evolution_insufficient_observations",
                    f"{insufficient} concept(s) have fewer dated observations than the "
                    f"configured minimum of {self.settings.min_dated_observations}",
                    severity=Severity.INFO,
                )
            )
        return timelines, warnings

    # -- change detection --------------------------------------------------

    def _changes(
        self,
        loaded: _Loaded,
        timelines: list[ConceptTimeline],
        comparisons: list[SyllabusComparison],
        request: EvolutionRequest,
    ) -> tuple[list[ConceptChange], list[Uncertainty]]:
        changes: list[ConceptChange] = []
        warnings: list[Uncertainty] = []
        for timeline in timelines:
            changes.extend(self._terminology_changes(timeline))
            context = self._context_change(timeline)
            if context is not None:
                changes.append(context)
            assessment_change = self._assessment_change_for(timeline, loaded)
            if assessment_change is not None:
                changes.append(assessment_change)
        changes.extend(self._syllabus_changes(loaded, comparisons))
        if request.include_relationships:
            relationship_changes, relationship_warnings = self._relationship_changes(
                loaded, timelines
            )
            changes.extend(relationship_changes)
            warnings.extend(relationship_warnings)
        if request.filters.categories:
            allowed = set(request.filters.categories)
            changes = [item for item in changes if item.category in allowed]
        changes = [
            item
            for item in changes
            if item.confidence >= self.settings.min_change_confidence
            or item.category is ConceptChangeCategory.INSUFFICIENT_EVIDENCE
        ]
        if len(changes) > self.settings.max_changes:
            changes = changes[: self.settings.max_changes]
            warnings.append(_warning("evolution_truncated", severity=Severity.INFO))
        changes.sort(
            key=lambda item: (
                item.category.value,
                str(item.earlier.date if item.earlier else ""),
                item.concept_ids[0] if item.concept_ids else "",
                item.change_id,
            )
        )
        return changes, warnings

    def _terminology_changes(self, timeline: ConceptTimeline) -> list[ConceptChange]:
        dated = [item for item in timeline.observations if item.date is not None]
        if len(dated) < self.settings.min_dated_observations:
            return []
        first_date, last_date = dated[0].date, dated[-1].date
        if first_date is None or last_date is None or first_date == last_date:
            return []
        early = [item for item in dated if item.date == first_date]
        late = [item for item in dated if item.date == last_date]
        early_terms = {item.observed_term for item in early}
        late_terms = {item.observed_term for item in late}
        if early_terms == late_terms:
            return []
        new_terms = late_terms - early_terms
        gone_terms = early_terms - late_terms
        if not new_terms:
            return []
        changes: list[ConceptChange] = []
        if gone_terms and not (early_terms & late_terms):
            after = sorted(new_terms)[0]
            changes.append(
                self._change(
                    ConceptChangeCategory.TERMINOLOGY_CHANGED,
                    timeline,
                    earlier=early[0],
                    later=self._first_with_term(late, after),
                    method="earliest_latest_term_sets",
                    confidence=0.7,
                    quality=EvidenceQuality.MEDIUM,
                    explanation=(
                        f"Earlier observations use {', '.join(sorted(early_terms))}; later "
                        f"observations use {', '.join(sorted(new_terms))} with no shared term."
                    ),
                    review_required=True,
                )
            )
            return changes
        for term in sorted(new_terms):
            observation = self._first_with_term(late, term)
            verified = observation.verified_term
            changes.append(
                self._change(
                    ConceptChangeCategory.ALIAS_INTRODUCED,
                    timeline,
                    earlier=early[0],
                    later=observation,
                    method="alias_appears_in_later_observation",
                    confidence=0.8 if verified else 0.55,
                    quality=EvidenceQuality.MEDIUM if verified else EvidenceQuality.LOW,
                    explanation=(
                        f"The term {term!r} appears in later observations of "
                        f"{timeline.canonical_label!r} but not in the earlier ones."
                    ),
                    review_required=not verified,
                )
            )
        return changes

    @staticmethod
    def _first_with_term(items: list[EvolutionObservation], term: str) -> EvolutionObservation:
        return next(item for item in items if item.observed_term == term)

    def _context_change(self, timeline: ConceptTimeline) -> ConceptChange | None:
        dated = [
            item
            for item in timeline.observations
            if item.date is not None and len(item.excerpt.strip()) >= 10
        ]
        if len(dated) < self.settings.min_dated_observations:
            return None
        early, late = dated[0], dated[-1]
        if early.date == late.date or early.document_id == late.document_id:
            return None
        similarity = _jaccard(early.excerpt, late.excerpt)
        if similarity >= self.settings.context_similarity_threshold:
            return None
        return self._change(
            ConceptChangeCategory.CONTEXT_CHANGED,
            timeline,
            earlier=early,
            later=late,
            method="lexical_excerpt_jaccard",
            confidence=0.55,
            quality=EvidenceQuality.LOW,
            explanation=(
                f"The earliest and latest dated excerpts for {timeline.canonical_label!r} "
                f"share only {similarity:.0%} of their normalized tokens; the surrounding "
                "context appears to differ."
            ),
            review_required=True,
            evidence=[
                f"lexical similarity {similarity:.4f}",
                f"threshold {self.settings.context_similarity_threshold:.2f}",
            ],
            warnings=[_warning("evolution_context_similarity")],
            limitations=[
                "Lexical similarity is not a semantic measure; a low score only marks the "
                "pair for manual review.",
            ],
        )

    def _assessment_change_for(
        self, timeline: ConceptTimeline, loaded: _Loaded
    ) -> ConceptChange | None:
        assessment = self._assessment_by_concept.get(timeline.concept_id)
        if assessment is None or not assessment.changed:
            return None
        titles = {item.document_id: item.title for item in loaded.documents}
        earlier_document = (
            assessment.earlier.document_ids[0] if assessment.earlier.document_ids else ""
        )
        later_document = assessment.later.document_ids[0] if assessment.later.document_ids else ""

        def side(window: AssessmentWindow, document_id: str) -> ChangeSide:
            period = window.period_from or ""
            if window.period_to and window.period_to != period:
                period = f"{period}..{window.period_to}" if period else window.period_to
            return ChangeSide(
                document_id=document_id,
                document_title=titles.get(document_id, document_id),
                date=window.date_from,
                date_type=(
                    EvolutionDateType.EXAMINATION
                    if window.date_from is not None
                    else EvolutionDateType.UNKNOWN
                ),
                excerpt=(
                    f"{window.questions} question(s), {window.known_marks:g} known marks"
                    + (f", period {period}" if period else "")
                ),
            )

        order_known = bool(
            assessment.earlier.period_from
            and assessment.later.period_from
            and (assessment.earlier.period_from, assessment.earlier.period_to or "")
            < (assessment.later.period_from, assessment.later.period_to or "")
        )
        return ConceptChange(
            change_id=make_change_id(
                f"{ConceptChangeCategory.ASSESSMENT_PATTERN_CHANGED.value}\x1f{timeline.concept_id}"
            ),
            category=ConceptChangeCategory.ASSESSMENT_PATTERN_CHANGED,
            concept_ids=[timeline.concept_id],
            labels=[timeline.canonical_label],
            earlier=side(assessment.earlier, earlier_document),
            later=side(assessment.later, later_document),
            temporal_order_known=order_known,
            method="dated_question_windows",
            confidence=0.65,
            evidence_quality=EvidenceQuality.MEDIUM
            if min(assessment.earlier.questions, assessment.later.questions)
            >= self.settings.min_assessment_questions + 1
            else EvidenceQuality.LOW,
            review_required=True,
            explanation=(
                f"Question-mapped assessment of {timeline.canonical_label!r} differs between "
                f"the earlier and later dated windows: " + "; ".join(assessment.reasons)
            ),
            evidence=[
                f"earlier: {assessment.earlier.questions} question(s)",
                f"later: {assessment.later.questions} question(s)",
                f"earlier period: {assessment.earlier.period_from or 'unknown'}",
                f"later period: {assessment.later.period_from or 'unknown'}",
                f"max type share delta: {assessment.max_type_share_delta}",
                f"max Bloom share delta: {assessment.max_bloom_share_delta}",
                f"max marks share delta: {assessment.max_marks_share_delta}",
                f"repetition counts: {assessment.later.repetition_counts}",
            ],
            limitations=[
                "Question counts and shares describe the analyzed corpus only; they are not a "
                "measure of educational importance.",
            ],
        )

    def _change(
        self,
        category: ConceptChangeCategory,
        timeline: ConceptTimeline,
        *,
        earlier: EvolutionObservation | None,
        later: EvolutionObservation | None,
        method: str,
        confidence: float,
        quality: EvidenceQuality,
        explanation: str,
        review_required: bool,
        evidence: list[str] | None = None,
        warnings: list[Uncertainty] | None = None,
        limitations: list[str] | None = None,
    ) -> ConceptChange:
        def side(observation: EvolutionObservation | None) -> ChangeSide | None:
            if observation is None:
                return None
            return ChangeSide(
                document_id=observation.document_id,
                document_title=observation.document_title,
                date=observation.date,
                date_type=observation.date_type,
                term=observation.observed_term,
                unit_label=observation.unit_label,
                excerpt=observation.excerpt,
                question_ids=observation.question_ids,
                syllabus_id=observation.syllabus_id,
            )

        return ConceptChange(
            change_id=make_change_id(
                "\x1f".join(
                    [
                        category.value,
                        timeline.concept_id,
                        earlier.observation_id if earlier else "",
                        later.observation_id if later else "",
                    ]
                )
            ),
            category=category,
            concept_ids=[timeline.concept_id],
            labels=[timeline.canonical_label],
            earlier=side(earlier),
            later=side(later),
            temporal_order_known=bool(
                earlier and later and earlier.date and later.date and earlier.date <= later.date
            ),
            method=method,
            confidence=_round(confidence),
            evidence_quality=quality,
            review_required=review_required,
            review_status=(
                EvolutionReviewStatus.PENDING_REVIEW
                if review_required
                else EvolutionReviewStatus.NOT_REQUIRED
            ),
            explanation=explanation,
            evidence=evidence or [],
            warnings=warnings or [],
            limitations=limitations
            or [
                "Observed inside the analyzed corpus only; absence from this collection is not "
                "proof of absence from practice.",
            ],
        )

    # -- syllabus comparison ----------------------------------------------

    def _syllabus_comparisons(
        self, loaded: _Loaded, request: EvolutionRequest
    ) -> tuple[list[SyllabusComparison], list[Uncertainty]]:
        del request
        data = sorted(
            loaded.syllabus_data,
            key=lambda item: (item.resolution.sort_key, item.syllabus.syllabus_id),
        )
        comparisons: list[SyllabusComparison] = []
        warnings: list[Uncertainty] = []
        for earlier, later in zip(data, data[1:], strict=False):
            comparison = self._compare_pair(loaded, earlier, later)
            comparisons.append(comparison)
            warnings.extend(comparison.warnings)
        if len(data) > 1 and any(not item.resolution.known for item in data):
            warnings.append(_warning("evolution_unknown_version_order"))
        return comparisons, warnings

    def _compare_pair(
        self, loaded: _Loaded, earlier: _SyllabusData, later: _SyllabusData
    ) -> SyllabusComparison:
        early_ref = self._syllabus_ref(earlier)
        late_ref = self._syllabus_ref(later)
        order_known = bool(
            earlier.resolution.known
            and later.resolution.known
            and earlier.resolution.value
            and later.resolution.value
            and earlier.resolution.value < later.resolution.value
        )
        early_ids = set(earlier.placements)
        late_ids = set(later.placements)
        introduced_ids = sorted(late_ids - early_ids)
        removed_ids = sorted(early_ids - late_ids)
        shared = sorted(early_ids & late_ids)
        moved: list[UnitMove] = []
        for concept_id in shared:
            early_units = sorted({item.unit_label for item in earlier.placements[concept_id]})
            late_units = sorted({item.unit_label for item in later.placements[concept_id]})
            if early_units != late_units:
                moved.append(
                    UnitMove(
                        concept_id=concept_id,
                        label=loaded.concepts[concept_id].label
                        if concept_id in loaded.concepts
                        else concept_id,
                        earlier_unit=", ".join(early_units),
                        later_unit=", ".join(late_units),
                    )
                )
        descriptions: list[DescriptionChange] = []
        for label in sorted(set(earlier.units) & set(later.units)):
            early_unit = earlier.units[label]
            late_unit = later.units[label]
            early_topics = {normalize_term(topic) for topic in early_unit.topics}
            late_topics = {normalize_term(topic) for topic in late_unit.topics}
            if early_topics == late_topics and (early_unit.title or "") == (late_unit.title or ""):
                continue
            descriptions.append(
                DescriptionChange(
                    unit_label=late_unit.unit_label,
                    added_topics=sorted(
                        {
                            topic
                            for topic in late_unit.topics
                            if normalize_term(topic) not in early_topics
                        }
                    ),
                    removed_topics=sorted(
                        {
                            topic
                            for topic in early_unit.topics
                            if normalize_term(topic) not in late_topics
                        }
                    ),
                    earlier_title=early_unit.title,
                    later_title=late_unit.title,
                )
            )
        extraction_ok = (
            later.document.extraction_status not in _EXCLUDING_EXTRACTION
            and later.document.extraction_status is not ExtractionStatus.PARTIAL
            and later.syllabus.confidence >= self.settings.min_syllabus_confidence
        )
        warnings: list[Uncertainty] = []
        limitations: list[str] = []
        if not order_known:
            warnings.append(_warning("evolution_unknown_version_order"))
            limitations.append(
                "The two versions have no reliable effective dates; introduction and removal "
                "are not asserted."
            )
        if not extraction_ok:
            warnings.append(_warning("evolution_incomplete_extraction"))
            limitations.append(
                "The later version's extraction or segmentation is incomplete; a concept that "
                "is absent from it may have been missed rather than removed."
            )
        if (not order_known or not extraction_ok) and (introduced_ids or removed_ids):
            limitations.append(
                "Version differences are reported as insufficient evidence, not as "
                "introduction or removal."
            )
            warnings.append(
                _warning(
                    "evolution_insufficient_observations",
                    "Version differences could not be classified as introduction or removal.",
                    severity=Severity.INFO,
                )
            )
        return SyllabusComparison(
            earlier=early_ref,
            later=late_ref,
            order_known=order_known,
            shared_concepts=shared,
            introduced=introduced_ids,
            no_longer_observed=removed_ids,
            moved_units=moved,
            description_changes=descriptions,
            warnings=warnings,
            limitations=limitations,
        )

    def _syllabus_ref(self, data: _SyllabusData) -> SyllabusVersionRef:
        return SyllabusVersionRef(
            syllabus_id=data.syllabus.syllabus_id,
            document_id=data.syllabus.document_id,
            title=data.syllabus.title,
            date=data.resolution.value,
            date_type=data.resolution.date_type,
            date_source=data.resolution.source,
            date_confident=data.resolution.confident,
            extraction_status=data.document.extraction_status,
            unit_count=len(data.units),
            concept_count=len(data.placements),
        )

    # -- syllabus and relationship change events --------------------------

    def _syllabus_changes(
        self, loaded: _Loaded, comparisons: list[SyllabusComparison]
    ) -> list[ConceptChange]:
        changes: list[ConceptChange] = []
        data_by_id = {item.syllabus.syllabus_id: item for item in loaded.syllabus_data}
        for comparison in comparisons:
            earlier = data_by_id.get(comparison.earlier.syllabus_id)
            later = data_by_id.get(comparison.later.syllabus_id)
            if earlier is None or later is None:
                continue
            early_side = self._version_side(comparison.earlier)
            late_side = self._version_side(comparison.later)
            extraction_ok = (
                later.document.extraction_status is ExtractionStatus.OK
                and later.syllabus.confidence >= self.settings.min_syllabus_confidence
            )
            if comparison.order_known and extraction_ok:
                for concept_id in comparison.introduced:
                    changes.append(
                        self._pair_change(
                            ConceptChangeCategory.CONCEPT_INTRODUCED,
                            [concept_id],
                            [self._concept_label(loaded, concept_id)],
                            early_side,
                            late_side,
                            order_known=True,
                            method="syllabus_version_concept_presence",
                            confidence=0.7,
                            quality=EvidenceQuality.MEDIUM,
                            explanation=(
                                f"{self._concept_label(loaded, concept_id)!r} is present in the "
                                "later syllabus version but not in the earlier one."
                            ),
                        )
                    )
                for concept_id in comparison.no_longer_observed:
                    changes.append(
                        self._pair_change(
                            ConceptChangeCategory.CONCEPT_NO_LONGER_OBSERVED,
                            [concept_id],
                            [self._concept_label(loaded, concept_id)],
                            early_side,
                            late_side,
                            order_known=True,
                            method="syllabus_version_concept_presence",
                            confidence=0.65,
                            quality=EvidenceQuality.MEDIUM,
                            explanation=(
                                f"{self._concept_label(loaded, concept_id)!r} is present in the "
                                "earlier syllabus version but not in the later one; absence is "
                                "not proof that it was removed from teaching."
                            ),
                        )
                    )
                for move in comparison.moved_units:
                    changes.append(
                        self._pair_change(
                            ConceptChangeCategory.SYLLABUS_UNIT_CHANGED,
                            [move.concept_id],
                            [move.label],
                            early_side,
                            late_side,
                            order_known=True,
                            method="syllabus_unit_membership",
                            confidence=0.7,
                            quality=EvidenceQuality.MEDIUM,
                            explanation=(
                                f"{move.label!r} moved from {move.earlier_unit!r} to "
                                f"{move.later_unit!r} between the two versions."
                            ),
                        )
                    )
                for description in comparison.description_changes:
                    changes.append(
                        self._pair_change(
                            ConceptChangeCategory.DESCRIPTION_CHANGED,
                            [],
                            [description.unit_label],
                            early_side,
                            late_side,
                            order_known=True,
                            method="syllabus_unit_description_diff",
                            confidence=0.6,
                            quality=EvidenceQuality.LOW,
                            explanation=(
                                f"The description of {description.unit_label!r} differs between "
                                "the two versions."
                            ),
                            evidence=[
                                f"added topics: {', '.join(description.added_topics) or 'none'}",
                                f"removed topics: "
                                f"{', '.join(description.removed_topics) or 'none'}",
                            ],
                        )
                    )
            elif comparison.introduced or comparison.no_longer_observed:
                differing = sorted(
                    {
                        *comparison.introduced,
                        *comparison.no_longer_observed,
                    }
                )
                reason = (
                    "no reliable effective dates"
                    if not comparison.order_known
                    else "incomplete later-version extraction or low segmentation confidence"
                )
                changes.append(
                    self._pair_change(
                        ConceptChangeCategory.INSUFFICIENT_EVIDENCE,
                        differing[:20],
                        [self._concept_label(loaded, item) for item in differing[:20]],
                        early_side,
                        late_side,
                        order_known=False,
                        method="syllabus_version_diff_without_order",
                        confidence=0.3,
                        quality=EvidenceQuality.LOW,
                        explanation=(
                            "The two syllabus versions differ in concept membership, but "
                            f"{reason}, so introduction and removal are not asserted."
                        ),
                        evidence=[
                            f"only in later: {len(comparison.introduced)} concept(s)",
                            f"only in earlier: {len(comparison.no_longer_observed)} concept(s)",
                        ],
                        limitations=[
                            "A concept can be missing because extraction or segmentation "
                            "failed, not because the syllabus changed."
                        ],
                    )
                )
            changes.extend(self._merge_split_changes(loaded, earlier, later, comparison))
        return changes

    def _merge_split_changes(
        self,
        loaded: _Loaded,
        earlier: _SyllabusData,
        later: _SyllabusData,
        comparison: SyllabusComparison,
    ) -> list[ConceptChange]:
        if not loaded.graph_available or not comparison.order_known:
            return []
        try:
            candidates = self.graph.concept_duplicates(loaded.course.course_id)
        except Exception as exc:  # noqa: BLE001 - optional review signal
            logger.info("concept duplicate candidates unavailable: %s", exc)
            return []
        early_ids = set(earlier.placements)
        late_ids = set(later.placements)
        changes: list[ConceptChange] = []
        for candidate in candidates:
            left, right = candidate.left_concept_id, candidate.right_concept_id
            if left not in loaded.concepts or right not in loaded.concepts:
                continue
            left_early, left_late = left in early_ids, left in late_ids
            right_early, right_late = right in early_ids, right in late_ids
            split_pair: tuple[str, list[str]] | None = None
            merge_pair: tuple[str, list[str]] | None = None
            if left_early and left_late and right_late and not right_early:
                split_pair = (left, [right])
            elif right_early and right_late and left_late and not left_early:
                split_pair = (right, [left])
            elif left_early and right_early and right_late and not left_late:
                merge_pair = (right, [left])
            elif left_early and right_early and left_late and not right_late:
                merge_pair = (left, [right])
            early_side = self._version_side(comparison.earlier)
            late_side = self._version_side(comparison.later)
            if split_pair is not None:
                survivor, newcomers = split_pair
                changes.append(
                    self._pair_change(
                        ConceptChangeCategory.POSSIBLE_CONCEPT_SPLIT,
                        [survivor, *newcomers],
                        [
                            self._concept_label(loaded, survivor),
                            *[self._concept_label(loaded, item) for item in newcomers],
                        ],
                        early_side,
                        late_side,
                        order_known=True,
                        method="graph_duplicate_candidate_plus_version_presence",
                        confidence=min(0.6, candidate.score),
                        quality=EvidenceQuality.LOW,
                        explanation=(
                            f"The later version places {self._concept_label(loaded, survivor)!r} "
                            "and also a near-duplicate concept that is absent earlier; the "
                            "distinction may be a split, but it is a review candidate only."
                        ),
                        evidence=[
                            f"label similarity {candidate.score:.2f} ({candidate.method})",
                        ],
                    )
                )
            elif merge_pair is not None:
                survivor, absorbed = merge_pair
                changes.append(
                    self._pair_change(
                        ConceptChangeCategory.POSSIBLE_CONCEPT_MERGE,
                        [survivor, *absorbed],
                        [
                            self._concept_label(loaded, survivor),
                            *[self._concept_label(loaded, item) for item in absorbed],
                        ],
                        early_side,
                        late_side,
                        order_known=True,
                        method="graph_duplicate_candidate_plus_version_presence",
                        confidence=min(0.6, candidate.score),
                        quality=EvidenceQuality.LOW,
                        explanation=(
                            "A near-duplicate concept is no longer placed in the later version "
                            "while the other persists; the pair may be one concept, but nothing "
                            "is merged automatically."
                        ),
                        evidence=[
                            f"label similarity {candidate.score:.2f} ({candidate.method})",
                        ],
                    )
                )
        return changes

    def _version_side(self, reference: SyllabusVersionRef) -> ChangeSide:
        return ChangeSide(
            document_id=reference.document_id,
            document_title=reference.title or reference.document_id,
            date=reference.date,
            date_type=reference.date_type,
            excerpt=reference.title or "",
            syllabus_id=reference.syllabus_id,
        )

    @staticmethod
    def _concept_label(loaded: _Loaded, concept_id: str) -> str:
        concept = loaded.concepts.get(concept_id)
        return concept.label if concept else concept_id

    def _pair_change(
        self,
        category: ConceptChangeCategory,
        concept_ids: list[str],
        labels: list[str],
        earlier: ChangeSide,
        later: ChangeSide,
        *,
        order_known: bool,
        method: str,
        confidence: float,
        quality: EvidenceQuality,
        explanation: str,
        evidence: list[str] | None = None,
        limitations: list[str] | None = None,
    ) -> ConceptChange:
        return ConceptChange(
            change_id=make_change_id(
                "\x1f".join(
                    [
                        category.value,
                        ",".join(sorted(concept_ids)),
                        earlier.syllabus_id or earlier.document_id,
                        later.syllabus_id or later.document_id,
                        ",".join(sorted(labels)),
                    ]
                )
            ),
            category=category,
            concept_ids=concept_ids,
            labels=labels,
            earlier=earlier,
            later=later,
            temporal_order_known=order_known,
            method=method,
            confidence=_round(confidence),
            evidence_quality=quality,
            review_required=True,
            review_status=EvolutionReviewStatus.PENDING_REVIEW,
            explanation=explanation,
            evidence=evidence or [],
            limitations=limitations
            or [
                "Observed inside the analyzed syllabus versions only; absence is not proof of "
                "removal from teaching practice."
            ],
        )

    def _relationship_changes(
        self, loaded: _Loaded, timelines: list[ConceptTimeline]
    ) -> tuple[list[ConceptChange], list[Uncertainty]]:
        if not loaded.graph_available:
            return [], []
        with self.db.session() as session:
            repository = GraphRepository(session)
            nodes = repository.list_nodes(
                course_id=loaded.course.course_id,
                node_type=GraphNodeType.CONCEPT,
                limit=10000,
            )
            edges = repository.list_edges(course_id=loaded.course.course_id, limit=20000)
        concept_nodes = {node.node_id: node for node in nodes if node.node_id in loaded.concepts}
        timeline_by_concept = {item.concept_id: item for item in timelines}
        documents = {item.document_id: item for item in loaded.documents}
        grouped: dict[tuple[str, str, str], list[tuple[date, Any]]] = defaultdict(list)
        for edge in edges:
            if edge.relationship not in _RELATIONSHIP_KINDS:
                continue
            if edge.source_node_id not in concept_nodes or edge.target_node_id not in concept_nodes:
                continue
            if not edge.source_document_id:
                continue
            resolution = loaded.resolutions.get(edge.source_document_id)
            if resolution is None or resolution.value is None:
                continue
            grouped[(edge.source_node_id, edge.target_node_id, edge.relationship.value)].append(
                (resolution.value, edge)
            )
        changes: list[ConceptChange] = []
        for (source, target, relationship), items in sorted(grouped.items()):
            source_timeline = timeline_by_concept.get(source)
            target_timeline = timeline_by_concept.get(target)
            if source_timeline is None or target_timeline is None:
                continue
            first_dates = [
                item
                for item in (source_timeline.first_observed, target_timeline.first_observed)
                if item is not None
            ]
            last_dates = [
                item
                for item in (source_timeline.last_observed, target_timeline.last_observed)
                if item is not None
            ]
            if not first_dates or not last_dates:
                continue
            first_concept_date = min(first_dates)
            last_concept_date = max(last_dates)
            items.sort(key=lambda item: (item[0], item[1].edge_id))
            first_edge_date, first_edge = items[0]
            last_edge_date, last_edge = items[-1]
            earlier: ChangeSide | None = None
            later: ChangeSide | None = None
            if first_edge_date > first_concept_date and last_edge_date <= last_concept_date:
                later = self._edge_side(first_edge, documents, loaded, relationship)
            elif last_edge_date < last_concept_date:
                earlier = self._edge_side(last_edge, documents, loaded, relationship)
            else:
                continue
            changes.append(
                ConceptChange(
                    change_id=make_change_id(
                        f"{ConceptChangeCategory.RELATIONSHIP_CHANGED.value}\x1f"
                        f"{source}\x1f{target}\x1f{relationship}"
                    ),
                    category=ConceptChangeCategory.RELATIONSHIP_CHANGED,
                    concept_ids=[source, target],
                    labels=[
                        self._concept_label(loaded, source),
                        self._concept_label(loaded, target),
                    ],
                    earlier=earlier,
                    later=later,
                    temporal_order_known=earlier is not None and later is not None,
                    method="graph_edge_document_dates",
                    confidence=0.5,
                    evidence_quality=EvidenceQuality.LOW,
                    review_required=True,
                    review_status=EvolutionReviewStatus.PENDING_REVIEW,
                    explanation=(
                        f"The {relationship.replace('_', ' ')} relationship between "
                        f"{self._concept_label(loaded, source)!r} and "
                        f"{self._concept_label(loaded, target)!r} is only observed "
                        + ("in later documents." if earlier is None else "in earlier documents.")
                    ),
                    evidence=[
                        f"{len(items)} dated graph edge observation(s)",
                        f"earliest edge document {first_edge_date.isoformat()}",
                        f"latest edge document {last_edge_date.isoformat()}",
                    ],
                    limitations=[
                        "A relationship that is not observed in a document is not proof that it "
                        "did not exist; extraction and graph coverage limit this comparison.",
                    ],
                )
            )
        return changes, []

    def _edge_side(
        self,
        edge: Any,
        documents: dict[str, DocumentRecord],
        loaded: _Loaded,
        relationship: str,
    ) -> ChangeSide:
        document = documents.get(edge.source_document_id or "")
        resolution = loaded.resolutions.get(edge.source_document_id or "")
        return ChangeSide(
            document_id=edge.source_document_id or "",
            document_title=document.title if document else (edge.source_document_id or ""),
            date=resolution.value if resolution else None,
            date_type=resolution.date_type if resolution else EvolutionDateType.UNKNOWN,
            term=relationship,
            excerpt=f"{relationship} relationship observed in this document",
            syllabus_id=edge.syllabus_id,
        )

    # -- assessment --------------------------------------------------------

    def _assessment(
        self, loaded: _Loaded, request: EvolutionRequest
    ) -> tuple[list[ConceptAssessment], list[str], list[str], list[Uncertainty]]:
        self._assessment_by_concept = {}
        warnings: list[Uncertainty] = []
        if not request.include_assessment or not loaded.syllabus_data:
            return [], [], [], warnings
        coverage_ids: list[str] = []
        concept_questions: dict[str, set[str]] = defaultdict(set)
        concept_syllabus: dict[str, str] = {}
        exam_ids: list[str] = []
        exam_by_syllabus: dict[str, str] = {}
        repetition_by_syllabus: dict[str, dict[str, set[str]]] = {}
        coverage = CoverageService(self.db, self.coverage_settings)
        for data in loaded.syllabus_data:
            year = data.syllabus.syllabus_id
            try:
                document_ids = sorted(
                    {
                        str(document.metadata.get("parent_document_id") or document.document_id)
                        for document in loaded.documents
                    }
                )
                report = coverage.analyze(
                    CoverageRequest(
                        course_id=loaded.course.course_id,
                        syllabus_id=year,
                        filters=CoverageFilters(document_ids=document_ids),
                    )
                )
            except (CoverageError, ValueError) as exc:
                warnings.append(
                    _warning(
                        "evolution_no_coverage",
                        f"coverage for {year} was unavailable: {exc}",
                    )
                )
                continue
            coverage_ids.append(report.report_id)
            for finding in report.concepts:
                concept_id = self._coverage_concept_id(loaded, finding)
                if concept_id is None:
                    continue
                for evidence in finding.supporting_questions:
                    concept_questions[concept_id].add(evidence.question_id)
                concept_syllabus.setdefault(concept_id, year)
            if request.include_exam_dna and self.settings.include_exam_dna:
                try:
                    profile = ExamDnaService(self.db, self.exam_dna_settings).analyze(
                        ExamDnaRequest(course_id=loaded.course.course_id, syllabus_id=year)
                    )
                    exam_ids.append(profile.profile_id)
                    exam_by_syllabus[year] = profile.profile_id
                    per_question: dict[str, set[str]] = defaultdict(set)
                    for group in profile.question_groups:
                        if group.kind not in _REPETITION_KINDS:
                            continue
                        for member in group.members:
                            per_question[member.question_id].add(group.kind.value)
                    repetition_by_syllabus[year] = per_question
                except (ExamDnaError, ValueError) as exc:
                    logger.info("exam dna profile unavailable for %s: %s", year, exc)

        assessment: list[ConceptAssessment] = []
        small_samples = 0
        for concept_id in sorted(concept_questions):
            if concept_id not in loaded.concepts:
                continue
            rows = self._concept_question_rows(loaded, concept_questions[concept_id])
            if not rows:
                continue
            block = self._assessment_block(
                loaded,
                concept_id,
                rows,
                coverage_ids,
                exam_by_syllabus.get(concept_syllabus.get(concept_id, "")),
                repetition_by_syllabus.get(concept_syllabus.get(concept_id, ""), {}),
            )
            assessment.append(block)
            self._assessment_by_concept[concept_id] = block
            if not block.changed:
                small_samples += 1
        if small_samples:
            warnings.append(
                _warning(
                    "evolution_small_assessment",
                    f"{small_samples} concept(s) lack enough dated questions in both windows "
                    "for an assessment-change claim",
                    severity=Severity.INFO,
                )
            )
        return assessment, exam_ids, coverage_ids, warnings

    def _coverage_concept_id(self, loaded: _Loaded, finding: Any) -> str | None:
        if finding.graph_concept_id and finding.graph_concept_id in loaded.concepts:
            return finding.graph_concept_id
        normalized = normalize_term(finding.label)
        for concept_id, ref in loaded.concepts.items():
            if ref.normalized == normalized:
                return concept_id
        for alias, ids in loaded.alias_index.items():
            if alias == normalized and len(ids) == 1:
                return next(iter(ids))
        return None

    def _concept_question_rows(self, loaded: _Loaded, question_ids: set[str]) -> list[_QuestionRow]:
        rows: list[_QuestionRow] = []
        for question_id in sorted(question_ids):
            question = loaded.questions.get(question_id)
            if question is None:
                continue
            resolution = loaded.resolutions.get(question.document_id)
            if resolution is None:
                continue
            period = loaded.periods.get(question.document_id)
            # Only explicit examination metadata may order a question. The coarse
            # academic-year fallback Phase 4 allows is course-wide, so using it here
            # would silently place an undated paper in an assessment window.
            period_key = (
                period.sort_key
                if period is not None and period.known and period.source in _EXAM_PERIOD_SOURCES
                else None
            )
            if resolution.value is None and period_key is None:
                continue
            rows.append(
                _QuestionRow(
                    question_id=question_id,
                    question=question,
                    date_value=resolution.value,
                    period_key=period_key,
                    period_label=period.label if period is not None and period.known else None,
                )
            )
        rows.sort(key=lambda item: item.sort_key)
        return rows

    def _assessment_block(
        self,
        loaded: _Loaded,
        concept_id: str,
        rows: list[_QuestionRow],
        coverage_ids: list[str],
        exam_profile: str | None,
        repetition: dict[str, set[str]],
    ) -> ConceptAssessment:
        concept = loaded.concepts[concept_id]
        # Group by date/period anchor first, so a window is a contiguous slice of time
        # and never mixes two sessions on either side of an arbitrary question count.
        groups: list[list[_QuestionRow]] = []
        for row in rows:
            if groups and groups[-1][0].sort_key[0] == row.sort_key[0]:
                groups[-1].append(row)
            else:
                groups.append([row])
        split = (len(groups) + 1) // 2
        early_rows = [row for group in groups[:split] for row in group]
        late_rows = [row for group in groups[split:] for row in group]
        minimum = self.settings.min_assessment_questions
        limitations: list[str] = []
        if len(early_rows) < minimum or len(late_rows) < minimum:
            limitations.append(
                f"needs at least {minimum} dated questions per window; observed "
                f"{len(early_rows)} earlier and {len(late_rows)} later"
            )
        early = self._assessment_window("earlier", early_rows, loaded, repetition)
        later = self._assessment_window("later", late_rows, loaded, repetition)
        type_deltas = self._share_deltas(early.type_shares, later.type_shares)
        bloom_deltas = self._share_deltas(early.bloom_shares, later.bloom_shares)
        marks_deltas = self._share_deltas(early.marks_shares, later.marks_shares)
        max_type = max(type_deltas.values()) if type_deltas else None
        max_bloom = max(bloom_deltas.values()) if bloom_deltas else None
        max_marks = max(marks_deltas.values()) if marks_deltas else None
        reasons: list[str] = []
        if len(early_rows) >= minimum and len(late_rows) >= minimum:
            if max_type is not None and max_type >= self.settings.assessment_share_delta:
                label = max(type_deltas.items(), key=lambda item: item[1])[0]
                reasons.append(f"question-type share for {label!r} changed by {max_type:.0%}")
            if max_bloom is not None and max_bloom >= self.settings.assessment_share_delta:
                label = max(bloom_deltas.items(), key=lambda item: item[1])[0]
                reasons.append(f"Bloom-level share for {label!r} changed by {max_bloom:.0%}")
            if max_marks is not None and max_marks >= self.settings.assessment_share_delta:
                label = max(marks_deltas.items(), key=lambda item: item[1])[0]
                reasons.append(f"marks-value share for {label!r} changed by {max_marks:.0%}")
        return ConceptAssessment(
            concept_id=concept_id,
            label=concept.label,
            earlier=early,
            later=later,
            question_delta=later.questions - early.questions,
            marks_delta=_round(later.known_marks - early.known_marks),
            max_type_share_delta=max_type,
            max_bloom_share_delta=max_bloom,
            max_marks_share_delta=max_marks,
            changed=bool(reasons),
            reasons=reasons,
            coverage_report_ids=sorted(coverage_ids),
            exam_dna_profile_id=exam_profile,
            limitations=limitations,
        )

    @staticmethod
    def _share_deltas(early: dict[str, float], later: dict[str, float]) -> dict[str, float]:
        labels = set(early) | set(later)
        return {
            label: _round(abs(later.get(label, 0.0) - early.get(label, 0.0))) for label in labels
        }

    def _assessment_window(
        self,
        label: str,
        rows: list[_QuestionRow],
        loaded: _Loaded,
        repetition: dict[str, set[str]],
    ) -> AssessmentWindow:
        questions = [row.question for row in rows]
        dates = [row.date_value for row in rows if row.date_value is not None]
        periods = [row.period_key for row in rows if row.period_key]
        dates.sort()
        periods.sort()
        analyses = [loaded.analyses.get(item.question_id) for item in questions]
        type_counts = Counter(
            analysis.primary_type.value
            for analysis in analyses
            if analysis is not None and analysis.primary_type is not None
        )
        bloom_counts = Counter(
            analysis.bloom_primary.value
            for analysis in analyses
            if analysis is not None and analysis.bloom_primary is not None
        )
        analysed = sum(1 for analysis in analyses if analysis is not None)
        with_marks = [item for item in questions if item.marks is not None]
        known_marks = _round(sum(item.marks or 0.0 for item in with_marks))
        marks_counts = Counter(f"{item.marks:g}" for item in with_marks)
        unit_counts = Counter(
            top.unit_label
            for top in (
                min(
                    loaded.matches.get(question.question_id, []),
                    key=lambda match: (match.rank, match.match_id),
                    default=None,
                )
                for question in questions
            )
            if top is not None and top.unit_label
        )
        repetition_counts: Counter[str] = Counter()
        for question in questions:
            for kind in sorted(repetition.get(question.question_id, set())):
                repetition_counts[kind] += 1
        sessions = {
            loaded.periods[question.document_id].session_key
            for question in questions
            if loaded.periods.get(question.document_id)
            and loaded.periods[question.document_id].session_key
        }
        return AssessmentWindow(
            label=label,
            date_from=dates[0] if dates else None,
            date_to=dates[-1] if dates else None,
            period_from=periods[0] if periods else None,
            period_to=periods[-1] if periods else None,
            questions=len(questions),
            papers=len({question.document_id for question in questions}),
            sessions=len(sessions),
            document_ids=sorted({question.document_id for question in questions}),
            questions_with_marks=len(with_marks),
            known_marks=known_marks,
            type_counts=dict(sorted(type_counts.items())),
            bloom_counts=dict(sorted(bloom_counts.items())),
            marks_counts=dict(sorted(marks_counts.items())),
            unit_counts=dict(sorted(unit_counts.items())),
            repetition_counts=dict(sorted(repetition_counts.items())),
            type_shares={
                value: _round(count / analysed)
                for value, count in sorted(type_counts.items())
                if analysed
            },
            bloom_shares={
                value: _round(count / analysed)
                for value, count in sorted(bloom_counts.items())
                if analysed
            },
            marks_shares={
                value: _round(count / len(with_marks))
                for value, count in sorted(marks_counts.items())
                if with_marks
            },
        )

    # -- quality, status, warnings ---------------------------------------

    def _data_quality(
        self,
        loaded: _Loaded,
        documents: list[DocumentRecord],
        observations: list[EvolutionObservation],
        exclusions: list[EvolutionExclusion],
        comparisons: list[SyllabusComparison],
        coverage_ids: list[str],
        exam_ids: list[str],
        *,
        truncated: bool,
    ) -> EvolutionDataQuality:
        dated = [item for item in observations if item.date is not None]
        conflicts = sum(
            1 for document in documents if loaded.resolutions[document.document_id].conflicts
        )
        invalid = sum(
            1 for document in documents if loaded.resolutions[document.document_id].invalid
        )
        incomplete = sum(
            1
            for document in documents
            if document.extraction_status in {ExtractionStatus.PARTIAL, *_EXCLUDING_EXTRACTION}
        )
        low_confidence = sum(
            1
            for item in observations
            if item.mapping_confidence is not None
            and item.mapping_confidence < self.settings.min_syllabus_confidence
        )
        return EvolutionDataQuality(
            documents=len(documents),
            dated_documents=sum(
                1 for document in documents if loaded.resolutions[document.document_id].known
            ),
            undated_documents=sum(
                1 for document in documents if not loaded.resolutions[document.document_id].known
            ),
            syllabus_versions=len(loaded.syllabus_data),
            concepts=len(loaded.concepts),
            observations=len(observations),
            dated_observations=len(dated),
            conflicting_dates=conflicts,
            invalid_dates=invalid,
            incomplete_extractions=incomplete,
            low_confidence_observations=low_confidence,
            graph_available=loaded.graph_available,
            coverage_report_ids=sorted(coverage_ids),
            exam_dna_profile_ids=sorted(exam_ids),
            truncated=truncated,
            excluded_documents=exclusions,
            warnings=[],
        )

    def _report_warnings(
        self,
        loaded: _Loaded,
        documents: list[DocumentRecord],
        observations: list[EvolutionObservation],
        comparisons: list[SyllabusComparison],
    ) -> list[Uncertainty]:
        warnings: list[Uncertainty] = []
        if not documents:
            warnings.append(_warning("evolution_no_documents"))
        if not observations:
            warnings.append(_warning("evolution_no_observations"))
        elif not any(item.date is not None for item in observations):
            warnings.append(_warning("evolution_no_dated_observations"))
        undated = [
            document for document in documents if not loaded.resolutions[document.document_id].known
        ]
        if undated:
            warnings.append(
                _warning(
                    "evolution_missing_date",
                    f"{len(undated)} document(s) have no usable source date",
                    severity=Severity.INFO,
                )
            )
        conflicts = [
            document for document in documents if loaded.resolutions[document.document_id].conflicts
        ]
        if conflicts:
            warnings.append(
                _warning(
                    "evolution_conflicting_dates",
                    f"{len(conflicts)} document(s) state conflicting publication dates",
                )
            )
        invalid = [
            document for document in documents if loaded.resolutions[document.document_id].invalid
        ]
        if invalid:
            warnings.append(
                _warning(
                    "evolution_invalid_date",
                    f"{len(invalid)} document(s) contain an unparsable date field",
                )
            )
        if not loaded.graph_available:
            warnings.append(
                _warning(
                    "evolution_graph_unavailable",
                    severity=Severity.INFO,
                )
            )
        incomplete = [
            document
            for document in documents
            if document.extraction_status in {ExtractionStatus.PARTIAL, *_EXCLUDING_EXTRACTION}
        ]
        if incomplete:
            warnings.append(
                _warning(
                    "evolution_incomplete_extraction",
                    f"{len(incomplete)} document(s) have incomplete extraction",
                )
            )
        if loaded.ambiguous_aliases:
            warnings.append(
                _warning(
                    "evolution_ambiguous_alias",
                    f"{len(loaded.ambiguous_aliases)} term(s) map to several concepts",
                )
            )
        kinds = {document.kind for document in documents}
        if len(kinds) > 1:
            warnings.append(
                _warning(
                    "evolution_mixed_formats",
                    "documents of different kinds are included: "
                    + ", ".join(sorted(kind.value for kind in kinds)),
                    severity=Severity.INFO,
                )
            )
        exam_types = {
            loaded.periods[document.document_id].exam_type
            for document in documents
            if loaded.periods.get(document.document_id)
            and loaded.periods[document.document_id].exam_type
        }
        if len(exam_types) > 1:
            warnings.append(
                _warning(
                    "evolution_mixed_formats",
                    "examination types differ: "
                    + ", ".join(sorted(value for value in exam_types if value)),
                    severity=Severity.INFO,
                )
            )
        if any(not item.order_known for item in comparisons):
            warnings.append(
                _warning(
                    "evolution_unknown_version_order",
                    severity=Severity.INFO,
                )
            )
        return warnings

    def _status(
        self,
        loaded: _Loaded,
        observations: list[EvolutionObservation],
        quality: EvolutionDataQuality,
        comparisons: list[SyllabusComparison],
        warnings: list[Uncertainty],
    ) -> EvolutionStatus:
        if not loaded.documents or not observations:
            return EvolutionStatus.INSUFFICIENT_DATA
        if quality.dated_observations == 0:
            return EvolutionStatus.INSUFFICIENT_DATA
        partial = (
            quality.undated_documents > 0
            or quality.conflicting_dates > 0
            or quality.invalid_dates > 0
            or quality.incomplete_extractions > 0
            or not quality.graph_available
            or quality.truncated
            or loaded.ambiguous_aliases != {}
            or any(not item.order_known for item in comparisons)
            or any(
                warning.code
                in {
                    "evolution_no_coverage",
                    "evolution_mixed_formats",
                    "evolution_insufficient_observations",
                }
                for warning in warnings
            )
        )
        return EvolutionStatus.PARTIAL if partial else EvolutionStatus.OK

    def _summary(
        self,
        documents: list[DocumentRecord],
        observations: list[EvolutionObservation],
        timelines: list[ConceptTimeline],
        changes: list[ConceptChange],
        comparisons: list[SyllabusComparison],
        assessment: list[ConceptAssessment],
    ) -> EvolutionSummary:
        counts = Counter(change.category.value for change in changes)
        return EvolutionSummary(
            documents=len(documents),
            dated_documents=len(
                {item.document_id for item in observations if item.date is not None}
            ),
            concepts=len(timelines),
            observations=len(observations),
            dated_observations=sum(1 for item in observations if item.date is not None),
            timelines=len(timelines),
            changes=len(changes),
            review_required_changes=sum(1 for item in changes if item.review_required),
            insufficient_evidence_changes=counts.get(
                ConceptChangeCategory.INSUFFICIENT_EVIDENCE.value, 0
            ),
            changes_by_category=dict(sorted(counts.items())),
            syllabus_comparisons=len(comparisons),
            assessment_concepts=len(assessment),
        )

    def _scope(
        self,
        loaded: _Loaded,
        documents: list[DocumentRecord],
        request: EvolutionRequest,
        observations: list[EvolutionObservation],
    ) -> EvolutionScope:
        dated = [item.date for item in observations if item.date is not None]
        statement = (
            f"{len(documents)} document(s) and {len(loaded.syllabus_data)} syllabus "
            f"version(s) for course {request.course_id}"
        )
        if dated:
            statement += f", dated {min(dated).isoformat()} to {max(dated).isoformat()}"
        if request.filters.restricts:
            statement += ", filtered"
        return EvolutionScope(
            document_ids=[document.document_id for document in documents],
            syllabus_ids=[item.syllabus.syllabus_id for item in loaded.syllabus_data],
            concept_ids=sorted(loaded.concepts),
            date_from=request.filters.date_from,
            date_to=request.filters.date_to,
            scope_statement=statement,
        )

    def _limitations(
        self,
        loaded: _Loaded,
        quality: EvolutionDataQuality,
        comparisons: list[SyllabusComparison],
    ) -> list[str]:
        limitations = [
            "This report describes evolution inside the analyzed corpus only. It does not "
            "establish the complete historical development of any concept.",
            "Observation order uses one source date per document, with its date type and field "
            "recorded; ingestion time is never used as a publication date.",
            "A missing mention is not proof that a concept was removed, renamed or forgotten.",
            "Contextual changes compare lexical excerpts; they are review signals, not semantic "
            "verdicts.",
            "Assessment counts and shares describe the analyzed questions only and do not "
            "measure educational importance.",
            "No canonical concept is merged, split, renamed or deleted from an inferred change; "
            "every change is review-only.",
            "Syllabus comparisons depend on extraction and segmentation quality; incomplete "
            "later versions are reported as insufficient evidence, not as removals.",
        ]
        if not loaded.graph_available:
            limitations.append(
                "Phase 5 graph identity was unavailable; concepts come from syllabus topics only."
            )
        if quality.undated_documents:
            limitations.append(
                "Some documents have no source date and cannot be placed on the timeline."
            )
        if quality.conflicting_dates:
            limitations.append(
                "Conflicting source dates are flagged; the selected date may not be the only "
                "relevant one."
            )
        if any(not item.order_known for item in comparisons):
            limitations.append(
                "At least one syllabus pair lacks a reliable chronological order; version "
                "differences are not classified as introduction or removal."
            )
        return limitations


def format_insufficient(label: str) -> str:
    return (
        f"{label!r} has fewer dated observations than required; only a partial timeline is "
        "reported."
    )


__all__ = ["ConceptEvolutionService", "EvolutionError"]
