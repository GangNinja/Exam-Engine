"""Phase 7 exam coverage gap detector.

The service is a deterministic, read-only projection over Phase 1 syllabus and
question rows, Phase 3 mapping/analysis rows and the Phase 5 graph's concept
identity and explicit assessment edges. It never rewrites those sources and never
promotes an inferred mapping into a verified fact.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from exam_engine.analytics.metadata import ExamPeriod, resolve_exam_period
from exam_engine.config import CoverageSettings
from exam_engine.graph.service import GraphService
from exam_engine.models.analytics import CourseScope
from exam_engine.models.course import CourseRecord
from exam_engine.models.coverage import (
    COVERAGE_ALGORITHM,
    COVERAGE_ALGORITHM_VERSION,
    COVERAGE_SCHEMA_VERSION,
    ConceptCoverageFinding,
    CoverageDataQuality,
    CoverageFilters,
    CoverageMappingCandidate,
    CoverageQuestionEvidence,
    CoverageReport,
    CoverageRequest,
    CoverageSummary,
    CoverageSyllabus,
    UnitCoverageFinding,
    UnmappedQuestionFinding,
    make_coverage_concept_id,
    make_coverage_report_id,
)
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    CoverageReportStatus,
    CoverageStatus,
    DocumentKind,
    GraphEdgeStatus,
    GraphNodeType,
    MappingStatus,
    Severity,
)
from exam_engine.models.graph import GraphEdge, GraphNode
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import SyllabusRecord, SyllabusUnitRecord
from exam_engine.models.understanding import QuestionAnalysis, UnitMatch
from exam_engine.nlp.keywords import normalize_term
from exam_engine.storage.db import Database
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)

EXCERPT_CHARS = 240
COVERAGE_STATUS_ORDER = (
    CoverageStatus.COVERED,
    CoverageStatus.LOW_COVERAGE,
    CoverageStatus.UNMAPPED,
    CoverageStatus.UNCERTAIN,
    CoverageStatus.INSUFFICIENT_DATA,
)


class CoverageError(ValueError):
    """Structured scope/filter failure for the CLI and other callers."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class _Concept:
    concept_id: str
    label: str
    normalized_label: str
    aliases: tuple[str, ...]
    syllabus_id: str
    unit_ids: tuple[str, ...]
    unit_labels: tuple[str, ...]
    source_document_id: str
    source_page: int | None
    extraction_confidence: float | None
    graph_concept_id: str | None


@dataclass
class _Mapping:
    question: QuestionRecord
    concept_id: str
    unit_ids: tuple[str, ...]
    sources: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)
    scores: list[float] = field(default_factory=list)
    matched_terms: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    graph_edge_ids: set[str] = field(default_factory=set)
    graph_edge_status: GraphEdgeStatus | None = None
    analysis_status: MappingStatus | None = None

    @property
    def score(self) -> float | None:
        return max(self.scores) if self.scores else None

    @property
    def method(self) -> str:
        return "+".join(sorted(self.methods)) or "unknown"

    @property
    def source_names(self) -> list[str]:
        return sorted(self.sources)


@dataclass(frozen=True)
class _QuestionContext:
    record: QuestionRecord
    document: DocumentRecord
    period: ExamPeriod
    analysis: QuestionAnalysis | None
    matches: tuple[UnitMatch, ...]


@dataclass(frozen=True)
class _Loaded:
    course: CourseRecord
    syllabi: tuple[SyllabusRecord, ...]
    selected_syllabus: SyllabusRecord
    documents: dict[str, DocumentRecord]
    periods: dict[str, ExamPeriod]
    questions: tuple[QuestionRecord, ...]
    analyses: dict[str, QuestionAnalysis]
    matches: dict[str, tuple[UnitMatch, ...]]
    graph_concepts_by_label: dict[str, GraphNode]
    graph_concepts_by_alias: dict[str, GraphNode]
    graph_concepts_by_node: dict[str, GraphNode]
    graph_questions_by_id: dict[str, GraphNode]
    graph_question_ids_by_node: dict[str, str]
    graph_assessments: tuple[GraphEdge, ...]
    graph_nodes_available: bool


class CoverageService:
    """Analyse syllabus coverage for exactly one course and syllabus version."""

    def __init__(
        self,
        database: Database,
        settings: CoverageSettings | None = None,
        *,
        graph: GraphService | None = None,
    ) -> None:
        self.db = database
        self.settings = settings or CoverageSettings()
        self.graph = graph or GraphService(database)

    # -- public API --------------------------------------------------------

    def analyze(self, request: CoverageRequest) -> CoverageReport:
        loaded, load_warnings = self._load(request)
        filtered = self._filtered_questions(loaded, request.filters)
        questions, unscoped, truncated = self._scope_questions(loaded, filtered)
        contexts = self._contexts(loaded, questions)
        concepts, units = self._concepts(loaded, request.filters)
        mappings, candidates = self._accepted_mappings(loaded, concepts, contexts)
        findings = self._concept_findings(
            loaded,
            concepts,
            mappings,
            candidates,
            contexts,
        )
        unit_findings = self._unit_findings(
            loaded,
            units,
            findings,
            mappings,
            contexts,
        )
        unmapped = self._unmapped_questions(loaded, mappings, candidates, contexts)
        summary = self._summary(findings, mappings, contexts)
        quality = self._data_quality(
            loaded,
            concepts,
            mappings,
            contexts,
            load_warnings,
            unscoped=unscoped,
            truncated=truncated,
        )
        report_status = self._report_status(findings, summary, quality)
        report_warnings = self._report_warnings(
            loaded,
            concepts,
            findings,
            quality,
            contexts,
            unscoped=unscoped,
            truncated=truncated,
        )
        parameters = self._parameters(loaded, request)
        report_id = self._report_id(
            loaded,
            concepts,
            mappings,
            contexts,
            findings,
            unmapped,
            parameters,
            request.filters,
        )
        return CoverageReport(
            schema_version=COVERAGE_SCHEMA_VERSION,
            report_id=report_id,
            algorithm=COVERAGE_ALGORITHM,
            algorithm_version=COVERAGE_ALGORITHM_VERSION,
            course=_course_scope(loaded.course),
            syllabus=CoverageSyllabus(
                syllabus_id=loaded.selected_syllabus.syllabus_id,
                title=loaded.selected_syllabus.title,
                document_id=loaded.selected_syllabus.document_id,
                course_syllabus_version=loaded.course.syllabus_version,
                unit_count=len(units),
                concept_count=len(concepts),
                confidence=loaded.selected_syllabus.confidence,
                selected_automatically=request.syllabus_id is None,
            ),
            status=report_status,
            filters=request.filters,
            parameters=parameters,
            summary=summary,
            concepts=findings,
            units=unit_findings,
            unmapped_questions=unmapped,
            data_quality=quality,
            warnings=report_warnings,
            limitations=self._limitations(loaded, quality, findings, request.filters),
        )

    def concept(self, request: CoverageRequest, concept: str) -> ConceptCoverageFinding:
        report = self.analyze(request)
        normalized = normalize_term(concept)
        matches = [
            item
            for item in report.concepts
            if item.concept_id == concept or item.normalized_label == normalized
        ]
        if not matches:
            raise KeyError(f"unknown syllabus concept: {concept}")
        if len(matches) > 1:
            raise ValueError(
                f"ambiguous syllabus concept {concept!r}; pass the concept id: "
                + ", ".join(item.concept_id for item in matches)
            )
        return matches[0]

    def unit(self, request: CoverageRequest, unit_id: str) -> UnitCoverageFinding:
        report = self.analyze(request)
        for item in report.units:
            if item.unit_id == unit_id:
                return item
        raise KeyError(f"unknown syllabus unit: {unit_id}")

    def unmapped(self, request: CoverageRequest) -> list[UnmappedQuestionFinding]:
        return self.analyze(request).unmapped_questions

    # -- loading and scope -------------------------------------------------

    def _load(self, request: CoverageRequest) -> tuple[_Loaded, list[Uncertainty]]:
        warnings: list[Uncertainty] = []
        with self.db.session() as session:
            course = CourseRepository(session).get(request.course_id)
            if course is None:
                raise CoverageError("unknown_course", f"unknown course: {request.course_id}")
            syllabi = SyllabusRepository(session).get_by_course(course.course_id)
            documents = DocumentRepository(session).list_by_course(course.course_id)
            questions = QuestionRepository(session).list_by_course(course.course_id)
            understanding = QuestionUnderstandingRepository(session)
            analyses = {
                analysis.question_id: analysis
                for analysis in understanding.list_analyses(course.course_id)
            }
            matches: dict[str, list[UnitMatch]] = defaultdict(list)
            for match in understanding.list_unit_matches_for_course(course.course_id):
                matches[match.question_id].append(match)

        selected = self._select_syllabus(course, syllabi, request.syllabus_id)
        document_map = {document.document_id: document for document in documents}
        periods = {
            document.document_id: resolve_exam_period(document, course) for document in documents
        }

        graph_concepts_by_label: dict[str, GraphNode] = {}
        graph_concepts_by_alias: dict[str, GraphNode] = {}
        graph_concepts_by_node: dict[str, GraphNode] = {}
        graph_questions_by_id: dict[str, GraphNode] = {}
        graph_question_ids_by_node: dict[str, str] = {}
        graph_assessments: tuple[GraphEdge, ...] = ()
        graph_nodes_available = False
        if self.settings.include_graph_explicit_mappings:
            try:
                graph_concepts = self.graph.nodes(
                    course.course_id, node_type=GraphNodeType.CONCEPT, limit=10000
                )
                graph_questions = self.graph.nodes(
                    course.course_id, node_type=GraphNodeType.QUESTION, limit=10000
                )
                graph_nodes_available = bool(graph_concepts or graph_questions)
                graph_concepts_by_label = {
                    node.normalized_label or normalize_term(node.label): node
                    for node in graph_concepts
                }
                graph_concepts_by_alias = _alias_index(graph_concepts, graph_concepts_by_label)
                graph_concepts_by_node = {node.node_id: node for node in graph_concepts}
                graph_questions_by_id = {
                    node.question_id: node for node in graph_questions if node.question_id
                }
                graph_question_ids_by_node = {
                    node.node_id: node.question_id for node in graph_questions if node.question_id
                }
                graph_assessments = tuple(
                    self.graph.assessment_edges(
                        course.course_id,
                        syllabus_id=selected.syllabus_id,
                        include_inferred=False,
                        include_candidates=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - graph is optional derived data
                warnings.append(
                    _warning(
                        "coverage_graph_unavailable",
                        (
                            "Phase 5 graph data could not be read; syllabus topic identity "
                            f"and Phase 3 mappings are still used ({type(exc).__name__}: {exc})"
                        ),
                        Severity.INFO,
                    )
                )

        loaded = _Loaded(
            course=course,
            syllabi=tuple(syllabi),
            selected_syllabus=selected,
            documents=document_map,
            periods=periods,
            questions=tuple(questions),
            analyses=analyses,
            matches={question_id: tuple(items) for question_id, items in matches.items()},
            graph_concepts_by_label=graph_concepts_by_label,
            graph_concepts_by_alias=graph_concepts_by_alias,
            graph_concepts_by_node=graph_concepts_by_node,
            graph_questions_by_id=graph_questions_by_id,
            graph_question_ids_by_node=graph_question_ids_by_node,
            graph_assessments=graph_assessments,
            graph_nodes_available=graph_nodes_available,
        )
        self._validate_filters(loaded, request.filters)
        return loaded, warnings

    def _select_syllabus(
        self,
        course: CourseRecord,
        syllabi: list[SyllabusRecord],
        requested: str | None,
    ) -> SyllabusRecord:
        if not syllabi:
            raise CoverageError(
                "no_syllabus",
                f"course {course.course_id} has no parsed syllabus; coverage cannot be analysed",
                detail={"course_id": course.course_id},
            )
        if requested:
            selected = next((item for item in syllabi if item.syllabus_id == requested), None)
            if selected is None:
                raise CoverageError(
                    "unknown_syllabus",
                    f"unknown syllabus {requested!r} for course {course.course_id}",
                    detail={
                        "course_id": course.course_id,
                        "known_syllabus_ids": [item.syllabus_id for item in syllabi],
                    },
                )
            return selected
        if len(syllabi) > 1:
            raise CoverageError(
                "ambiguous_syllabus",
                (
                    f"course {course.course_id} has {len(syllabi)} syllabus versions; "
                    "pass an explicit syllabus: " + ", ".join(item.syllabus_id for item in syllabi)
                ),
                detail={
                    "course_id": course.course_id,
                    "known_syllabus_ids": [item.syllabus_id for item in syllabi],
                },
            )
        return syllabi[0]

    def _validate_filters(self, loaded: _Loaded, filters: CoverageFilters) -> None:
        document_ids = {document.document_id for document in loaded.documents.values()}
        source_ids = {document.source_id for document in loaded.documents.values()}
        unit_ids = {unit.unit_id for unit in loaded.selected_syllabus.units}
        period_labels = {period.label for period in loaded.periods.values()}
        unknown_documents = sorted(set(filters.document_ids) - document_ids)
        unknown_sources = sorted(set(filters.source_ids) - source_ids)
        unknown_units = sorted(set(filters.unit_ids) - unit_ids)
        unknown_periods = sorted(set(filters.exam_periods) - period_labels)
        if unknown_documents or unknown_sources or unknown_units or unknown_periods:
            raise CoverageError(
                "invalid_coverage_filter",
                "coverage filter names data outside the selected course or syllabus",
                detail={
                    "unknown_document_ids": unknown_documents,
                    "unknown_source_ids": unknown_sources,
                    "unknown_unit_ids": unknown_units,
                    "unknown_exam_periods": unknown_periods,
                },
            )

    def _filtered_questions(
        self, loaded: _Loaded, filters: CoverageFilters
    ) -> list[QuestionRecord]:
        document_ids = set(filters.document_ids)
        source_ids = set(filters.source_ids)
        periods = set(filters.exam_periods)
        question_types = set(filters.question_types)
        filtered: list[QuestionRecord] = []
        for question in loaded.questions:
            document = loaded.documents.get(question.document_id)
            if document is None:
                continue
            if document_ids and document.document_id not in document_ids:
                continue
            if source_ids and document.source_id not in source_ids:
                continue
            period = loaded.periods.get(document.document_id)
            if periods and (period is None or period.label not in periods):
                continue
            if question_types:
                analysis = loaded.analyses.get(question.question_id)
                if analysis is None or analysis.primary_type not in question_types:
                    continue
            filtered.append(question)
        return filtered

    def _scope_questions(
        self, loaded: _Loaded, filtered: list[QuestionRecord]
    ) -> tuple[list[QuestionRecord], int, bool]:
        allowed_ids = {question.question_id for question in filtered}
        selected_match_ids = {
            question_id
            for question_id, matches in loaded.matches.items()
            if question_id in allowed_ids
            and any(match.syllabus_id == loaded.selected_syllabus.syllabus_id for match in matches)
        }
        graph_assessment_ids: set[str] = set()
        for edge in loaded.graph_assessments:
            question_id = loaded.graph_question_ids_by_node.get(edge.source_node_id)
            if question_id is None or question_id not in allowed_ids:
                continue
            if edge.syllabus_id == loaded.selected_syllabus.syllabus_id or (
                edge.syllabus_id is None and len(loaded.syllabi) == 1
            ):
                graph_assessment_ids.add(question_id)
        if len(loaded.syllabi) > 1:
            in_scope = [
                question
                for question in filtered
                if question.question_id in selected_match_ids
                or question.question_id in graph_assessment_ids
            ]
            unscoped = len(filtered) - len(in_scope)
        else:
            in_scope = filtered
            unscoped = 0
        truncated = len(in_scope) > self.settings.max_questions
        if truncated:
            in_scope = in_scope[: self.settings.max_questions]
        return in_scope, unscoped, truncated

    def _contexts(self, loaded: _Loaded, questions: list[QuestionRecord]) -> list[_QuestionContext]:
        contexts: list[_QuestionContext] = []
        for question in questions:
            document = loaded.documents.get(question.document_id)
            period = loaded.periods.get(question.document_id)
            if document is None or period is None:
                continue
            contexts.append(
                _QuestionContext(
                    record=question,
                    document=document,
                    period=period,
                    analysis=loaded.analyses.get(question.question_id),
                    matches=loaded.matches.get(question.question_id, ()),
                )
            )
        return contexts

    # -- concepts ----------------------------------------------------------

    def _concepts(
        self, loaded: _Loaded, filters: CoverageFilters
    ) -> tuple[dict[str, _Concept], list[SyllabusUnitRecord]]:
        selected_units = set(filters.unit_ids)
        units = [
            unit
            for unit in loaded.selected_syllabus.units
            if not selected_units or unit.unit_id in selected_units
        ]
        seed_labels: dict[str, Counter[str]] = defaultdict(Counter)
        seed_units: dict[str, list[str]] = defaultdict(list)
        seed_unit_labels: dict[str, dict[str, str]] = defaultdict(dict)
        seed_confidences: dict[str, list[float]] = defaultdict(list)
        seed_pages: dict[str, list[int]] = defaultdict(list)
        unit_order = {unit.unit_id: index for index, unit in enumerate(units)}
        for unit in units:
            for topic in unit.topics:
                normalized = normalize_term(topic)
                label = " ".join(topic.split())
                if not normalized or not label:
                    continue
                seed_labels[normalized][label] += 1
                if unit.unit_id not in seed_units[normalized]:
                    seed_units[normalized].append(unit.unit_id)
                seed_unit_labels[normalized][unit.unit_id] = unit.unit_label
                seed_confidences[normalized].append(unit.confidence)
                if unit.page_number is not None:
                    seed_pages[normalized].append(unit.page_number)

        concepts: dict[str, _Concept] = {}
        for normalized in sorted(seed_labels):
            labels = seed_labels[normalized]
            label = sorted(labels, key=lambda value: (-labels[value], value.casefold(), value))[0]
            unit_ids = sorted(
                seed_units[normalized],
                key=lambda item: (unit_order.get(item, 9999), item),
            )
            graph_node = loaded.graph_concepts_by_label.get(
                normalized
            ) or loaded.graph_concepts_by_alias.get(normalized)
            aliases = set(labels)
            if graph_node is not None:
                aliases.add(graph_node.label)
                aliases.update(
                    str(item)
                    for item in graph_node.metadata.get("aliases", [])
                    if str(item).strip()
                )
            concepts[normalized] = _Concept(
                concept_id=make_coverage_concept_id(
                    loaded.course.course_id,
                    loaded.selected_syllabus.syllabus_id,
                    normalized,
                ),
                label=label,
                normalized_label=normalized,
                aliases=tuple(sorted(aliases, key=lambda item: (item.casefold(), item))),
                syllabus_id=loaded.selected_syllabus.syllabus_id,
                unit_ids=tuple(unit_ids),
                unit_labels=tuple(seed_unit_labels[normalized][unit_id] for unit_id in unit_ids),
                source_document_id=loaded.selected_syllabus.document_id,
                source_page=min(seed_pages[normalized]) if seed_pages[normalized] else None,
                extraction_confidence=(
                    max(seed_confidences[normalized]) if seed_confidences[normalized] else None
                ),
                graph_concept_id=graph_node.node_id if graph_node is not None else None,
            )
        return concepts, units

    # -- mapping -----------------------------------------------------------

    def _accepted_mappings(
        self,
        loaded: _Loaded,
        concepts: dict[str, _Concept],
        contexts: list[_QuestionContext],
    ) -> tuple[dict[tuple[str, str], _Mapping], dict[str, list[CoverageMappingCandidate]]]:
        accepted: dict[tuple[str, str], _Mapping] = {}
        candidates: dict[str, list[CoverageMappingCandidate]] = defaultdict(list)
        context_by_id = {context.record.question_id: context for context in contexts}
        concept_by_graph_id = {
            concept.graph_concept_id: concept
            for concept in concepts.values()
            if concept.graph_concept_id is not None
        }

        for context in contexts:
            analysis = context.analysis
            for match in context.matches:
                if match.syllabus_id != loaded.selected_syllabus.syllabus_id:
                    continue
                concept = concepts.get(normalize_term(match.topic or ""))
                if concept is None:
                    continue
                reason = self._mapping_rejection_reason(analysis, match, concept)
                if reason is not None:
                    candidates[concept.concept_id].append(
                        self._candidate_from_match(context, match, reason)
                    )
                    continue
                self._add_phase3_mapping(accepted, context, match, concept)

        if self.settings.include_graph_explicit_mappings:
            for edge in loaded.graph_assessments:
                question_id = loaded.graph_question_ids_by_node.get(edge.source_node_id)
                if question_id is None:
                    continue
                graph_context = context_by_id.get(question_id)
                if graph_context is None:
                    continue
                target_node = loaded.graph_concepts_by_node.get(edge.target_node_id)
                if target_node is None:
                    continue
                concept = concept_by_graph_id.get(target_node.node_id)
                if concept is None:
                    normalized = target_node.normalized_label or normalize_term(target_node.label)
                    concept = concepts.get(normalized)
                if concept is None:
                    continue
                if edge.syllabus_id is None and len(loaded.syllabi) > 1:
                    continue
                self._add_graph_mapping(accepted, graph_context, edge, concept)

        # Rejected candidates never explain an accepted question/concept pair.
        filtered_candidates: dict[str, list[CoverageMappingCandidate]] = {}
        for concept_id, items in candidates.items():
            unique: dict[tuple[str, str, str, str], CoverageMappingCandidate] = {}
            for item in items:
                if (item.question_id, concept_id) in accepted:
                    continue
                unique[(item.question_id, item.unit_id or "", item.topic or "", item.reason)] = item
            filtered_candidates[concept_id] = sorted(
                unique.values(),
                key=lambda item: (
                    item.question_id,
                    item.unit_id or "",
                    item.rank or 999,
                    item.score if item.score is not None else -1.0,
                ),
            )
        return accepted, filtered_candidates

    def _mapping_rejection_reason(
        self,
        analysis: QuestionAnalysis | None,
        match: UnitMatch,
        concept: _Concept,
    ) -> str | None:
        if match.unit_id not in concept.unit_ids:
            return "unit_not_in_selected_syllabus"
        if analysis is None:
            return "no_analysis"
        if analysis.mapping_status is MappingStatus.UNMAPPED:
            return "analysis_unmapped"
        if analysis.mapping_status is MappingStatus.AMBIGUOUS:
            return "ambiguous_mapping"
        if match.rank != 1:
            return "secondary_mapping"
        if match.score < self.settings.mapping_confidence_floor:
            return "below_confidence_floor"
        if not match.topic:
            return "unit_only_mapping"
        return None

    def _add_phase3_mapping(
        self,
        accepted: dict[tuple[str, str], _Mapping],
        context: _QuestionContext,
        match: UnitMatch,
        concept: _Concept,
    ) -> None:
        key = (context.record.question_id, concept.concept_id)
        mapping = accepted.get(key)
        if mapping is None:
            mapping = _Mapping(
                question=context.record,
                concept_id=concept.concept_id,
                unit_ids=(match.unit_id,),
                analysis_status=context.analysis.mapping_status if context.analysis else None,
            )
            accepted[key] = mapping
        mapping.sources.add("phase3_primary_mapping")
        mapping.methods.add(match.method)
        mapping.scores.append(match.score)
        mapping.matched_terms.update(match.matched_terms)
        mapping.evidence.update(match.evidence)
        mapping.evidence.add(f"phase3_unit_match:{match.match_id}")

    def _add_graph_mapping(
        self,
        accepted: dict[tuple[str, str], _Mapping],
        context: _QuestionContext,
        edge: GraphEdge,
        concept: _Concept,
    ) -> None:
        key = (context.record.question_id, concept.concept_id)
        mapping = accepted.get(key)
        if mapping is None:
            mapping = _Mapping(
                question=context.record,
                concept_id=concept.concept_id,
                unit_ids=concept.unit_ids,
                analysis_status=context.analysis.mapping_status if context.analysis else None,
            )
            accepted[key] = mapping
        mapping.sources.add("phase5_explicit_assessment")
        mapping.methods.add(edge.method)
        if edge.confidence is not None:
            mapping.scores.append(edge.confidence)
        mapping.evidence.update(edge.evidence)
        mapping.evidence.add(f"graph_assessment:{edge.edge_id}")
        mapping.graph_edge_ids.add(edge.edge_id)
        mapping.graph_edge_status = edge.status

    def _candidate_from_match(
        self, context: _QuestionContext, match: UnitMatch, reason: str
    ) -> CoverageMappingCandidate:
        return CoverageMappingCandidate(
            question_id=context.record.question_id,
            document_id=context.record.document_id,
            question_number=context.record.question_number,
            page_number=context.record.page_number,
            marks=context.record.marks,
            text_excerpt=_excerpt(context.record.text),
            unit_id=match.unit_id,
            unit_label=match.unit_label,
            topic=match.topic,
            syllabus_id=match.syllabus_id,
            mapping_source="phase3_unit_match",
            mapping_method=match.method,
            score=match.score,
            rank=match.rank,
            analysis_status=context.analysis.mapping_status if context.analysis else None,
            matched_terms=match.matched_terms,
            evidence=match.evidence,
            reason=reason,
        )

    # -- findings ----------------------------------------------------------

    def _concept_findings(
        self,
        loaded: _Loaded,
        concepts: dict[str, _Concept],
        mappings: dict[tuple[str, str], _Mapping],
        candidates: dict[str, list[CoverageMappingCandidate]],
        contexts: list[_QuestionContext],
    ) -> list[ConceptCoverageFinding]:
        context_by_id = {context.record.question_id: context for context in contexts}
        total_questions = len(contexts)
        analysed = sum(1 for context in contexts if context.analysis is not None)
        analysis_coverage = analysed / total_questions if total_questions else None
        small_corpus = total_questions < self.settings.small_corpus_questions
        by_concept: dict[str, list[_Mapping]] = defaultdict(list)
        for mapping in mappings.values():
            by_concept[mapping.concept_id].append(mapping)

        findings: list[ConceptCoverageFinding] = []
        for normalized in sorted(concepts):
            concept = concepts[normalized]
            accepted = sorted(
                by_concept.get(concept.concept_id, []),
                key=lambda item: (item.question.question_id, item.concept_id),
            )
            concept_candidates = candidates.get(concept.concept_id, [])
            status, reasons, warnings = self._concept_status(
                accepted=accepted,
                candidates=concept_candidates,
                total_questions=total_questions,
                analysed=analysed,
                analysis_coverage=analysis_coverage,
                small_corpus=small_corpus,
            )
            support: list[CoverageQuestionEvidence] = []
            question_ids: set[str] = set()
            document_ids: set[str] = set()
            session_keys: set[str] = set()
            type_counts: Counter[str] = Counter()
            unknown_types = 0
            mapped_marks = 0.0
            with_marks = 0
            scores: list[float] = []
            for mapping in accepted:
                context = context_by_id.get(mapping.question.question_id)
                if context is None:
                    continue
                question_ids.add(context.record.question_id)
                document_ids.add(context.document.document_id)
                if context.period.session_key is not None:
                    session_keys.add(context.period.session_key)
                if context.analysis is not None and context.analysis.primary_type is not None:
                    type_counts[context.analysis.primary_type.value] += 1
                else:
                    unknown_types += 1
                if context.record.marks is not None:
                    mapped_marks += context.record.marks
                    with_marks += 1
                if mapping.score is not None:
                    scores.append(mapping.score)
                support.append(
                    CoverageQuestionEvidence(
                        question_id=context.record.question_id,
                        document_id=context.document.document_id,
                        document_title=context.document.title,
                        document_kind=context.document.kind,
                        source_id=context.document.source_id,
                        question_number=context.record.question_number,
                        section=context.record.section,
                        page_number=context.record.page_number,
                        source_locator=context.record.source_locator,
                        marks=context.record.marks,
                        text_excerpt=_excerpt(context.record.text),
                        mapping_sources=mapping.source_names,
                        mapping_method=mapping.method,
                        mapping_score=mapping.score,
                        graph_edge_ids=sorted(mapping.graph_edge_ids),
                        graph_edge_status=mapping.graph_edge_status,
                        analysis_status=(
                            context.analysis.mapping_status
                            if context.analysis
                            else mapping.analysis_status
                        ),
                        primary_type=(context.analysis.primary_type if context.analysis else None),
                        matched_terms=sorted(mapping.matched_terms),
                        evidence=sorted(mapping.evidence),
                        exam_period=context.period.label,
                        exam_period_source=context.period.source,
                        exam_session=context.period.session,
                        exam_type=context.period.exam_type,
                    )
                )
            warning_list = list(warnings)
            if len(concept.unit_ids) > 1:
                warning_list.append(
                    _warning(
                        "coverage_shared_concept_units",
                        (
                            f"concept {concept.label!r} belongs to {len(concept.unit_ids)} "
                            "selected units; unit totals overlap and are not additive"
                        ),
                        Severity.INFO,
                        detail={
                            "concept_id": concept.concept_id,
                            "unit_ids": list(concept.unit_ids),
                        },
                    )
                )
            findings.append(
                ConceptCoverageFinding(
                    concept_id=concept.concept_id,
                    graph_concept_id=concept.graph_concept_id,
                    label=concept.label,
                    normalized_label=concept.normalized_label,
                    aliases=list(concept.aliases),
                    syllabus_id=concept.syllabus_id,
                    unit_ids=list(concept.unit_ids),
                    unit_labels=list(concept.unit_labels),
                    source_document_id=concept.source_document_id,
                    source_page=concept.source_page,
                    extraction_confidence=concept.extraction_confidence,
                    status=status,
                    distinct_question_count=len(question_ids),
                    mapped_marks=round(mapped_marks, 4),
                    questions_with_marks=with_marks,
                    document_count=len(document_ids),
                    session_count=len(session_keys),
                    question_type_counts=dict(sorted(type_counts.items())),
                    unknown_question_type_count=unknown_types,
                    scored_mapping_count=len(scores),
                    average_mapping_confidence=(
                        round(sum(scores) / len(scores), 4) if scores else None
                    ),
                    minimum_mapping_confidence=round(min(scores), 4) if scores else None,
                    maximum_mapping_confidence=round(max(scores), 4) if scores else None,
                    supporting_questions=sorted(
                        support, key=lambda item: (item.question_id, item.document_id)
                    ),
                    candidate_mappings=concept_candidates,
                    reasons=reasons,
                    warnings=warning_list,
                    limitations=[
                        "A concept absent from the available corpus has not necessarily "
                        "been absent from examinations.",
                        "Question counts are distinct questions; a question may support "
                        "more than one concept and marks are not split between concepts.",
                    ],
                )
            )
        return findings

    def _concept_status(
        self,
        *,
        accepted: list[_Mapping],
        candidates: list[CoverageMappingCandidate],
        total_questions: int,
        analysed: int,
        analysis_coverage: float | None,
        small_corpus: bool,
    ) -> tuple[CoverageStatus, list[str], list[Uncertainty]]:
        warnings: list[Uncertainty] = []
        if total_questions == 0:
            return (
                CoverageStatus.INSUFFICIENT_DATA,
                ["No usable questions remain in the selected corpus after filtering."],
                warnings,
            )
        if analysed == 0 and not accepted:
            warnings.append(
                _warning(
                    "coverage_partial_analysis",
                    "No Phase 3 analysis exists for the selected corpus.",
                    Severity.WARNING,
                )
            )
            return (
                CoverageStatus.INSUFFICIENT_DATA,
                [
                    "No Phase 3 question analysis is available, so concept coverage "
                    "cannot be evaluated reliably."
                ],
                warnings,
            )
        if accepted:
            scores = [mapping.score for mapping in accepted if mapping.score is not None]
            average = sum(scores) / len(scores) if scores else None
            if average is not None and average < self.settings.low_confidence_threshold:
                warnings.append(
                    _warning(
                        "coverage_low_confidence",
                        (
                            f"accepted mappings average {average:.3f}, below the configured "
                            f"confidence threshold {self.settings.low_confidence_threshold:.3f}"
                        ),
                        Severity.WARNING,
                    )
                )
                return (
                    CoverageStatus.UNCERTAIN,
                    [
                        f"{len(accepted)} accepted question mapping(s) average "
                        f"{average:.3f} confidence, below the configured coverage "
                        f"threshold {self.settings.low_confidence_threshold:.3f}.",
                        "The mappings are retained as uncertain evidence rather than "
                        "promoted to a confident coverage result.",
                    ],
                    warnings,
                )
            if len(accepted) >= self.settings.covered_min_questions:
                reasons = [
                    f"{len(accepted)} distinct question(s) have accepted mappings; "
                    f"the configured covered minimum is "
                    f"{self.settings.covered_min_questions}."
                ]
                if average is not None:
                    reasons.append(f"Average accepted mapping confidence is {average:.3f}.")
                if candidates:
                    warnings.append(
                        _warning(
                            "coverage_ambiguous_evidence",
                            (
                                f"{len(candidates)} rejected or secondary mapping candidate(s) "
                                "remain visible for review"
                            ),
                            Severity.INFO,
                        )
                    )
                return CoverageStatus.COVERED, reasons, warnings
            reasons = [
                f"{len(accepted)} distinct question(s) have accepted mappings, below the "
                f"configured covered minimum {self.settings.covered_min_questions}.",
                "Low question representation is reported descriptively; it does not mean "
                "the concept is unimportant.",
            ]
            if average is not None:
                reasons.append(f"Average accepted mapping confidence is {average:.3f}.")
            return CoverageStatus.LOW_COVERAGE, reasons, warnings

        if candidates:
            reasons = [
                f"No accepted mapping exists, but {len(candidates)} candidate mapping(s) "
                "were retained because they were ambiguous, secondary or below the "
                "configured confidence floor.",
                "Candidate evidence is not treated as coverage.",
            ]
            warnings.append(
                _warning(
                    "coverage_ambiguous_evidence",
                    f"{len(candidates)} candidate mapping(s) require review",
                    Severity.WARNING,
                )
            )
            return CoverageStatus.UNCERTAIN, reasons, warnings
        if small_corpus or (
            analysis_coverage is not None
            and analysis_coverage < self.settings.min_analysis_coverage
        ):
            if small_corpus:
                warnings.append(
                    _warning(
                        "coverage_small_corpus",
                        (
                            f"the selected corpus has {total_questions} question(s), below the "
                            f"configured small-corpus threshold "
                            f"{self.settings.small_corpus_questions}"
                        ),
                        Severity.WARNING,
                    )
                )
            if (
                analysis_coverage is not None
                and analysis_coverage < self.settings.min_analysis_coverage
            ):
                warnings.append(
                    _warning(
                        "coverage_partial_analysis",
                        (
                            f"only {analysis_coverage:.0%} of questions have Phase 3 analysis, "
                            "below the configured minimum "
                            f"{self.settings.min_analysis_coverage:.0%}"
                        ),
                        Severity.WARNING,
                    )
                )
            return (
                CoverageStatus.UNCERTAIN,
                [
                    "No accepted mapping was found, but the available corpus or analysis "
                    "coverage is too limited to conclude that the concept is unrepresented."
                ],
                warnings,
            )
        return (
            CoverageStatus.UNMAPPED,
            [
                f"No accepted mapping was found among {analysed} analysed question(s) in the "
                "available corpus.",
                "This describes the available corpus only; it does not mean the concept "
                "was never examined.",
            ],
            warnings,
        )

    def _unit_findings(
        self,
        loaded: _Loaded,
        units: list[SyllabusUnitRecord],
        findings: list[ConceptCoverageFinding],
        mappings: dict[tuple[str, str], _Mapping],
        contexts: list[_QuestionContext],
    ) -> list[UnitCoverageFinding]:
        finding_by_id = {item.concept_id: item for item in findings}
        mapping_by_concept: dict[str, list[_Mapping]] = defaultdict(list)
        for mapping in mappings.values():
            mapping_by_concept[mapping.concept_id].append(mapping)
        total_questions = len(contexts)
        total_known_marks = sum(
            context.record.marks or 0.0 for context in contexts if context.record.marks is not None
        )
        rows: list[UnitCoverageFinding] = []
        for unit in units:
            unit_concepts = sorted(
                {
                    make_coverage_concept_id(
                        loaded.course.course_id,
                        loaded.selected_syllabus.syllabus_id,
                        normalize_term(topic),
                    )
                    for topic in unit.topics
                    if normalize_term(topic)
                }
            )
            unit_findings = [
                finding_by_id[concept_id]
                for concept_id in unit_concepts
                if concept_id in finding_by_id
            ]
            status_counts = {status.value: 0 for status in COVERAGE_STATUS_ORDER}
            for finding in unit_findings:
                status_counts[finding.status.value] += 1
            qids: dict[str, float | None] = {}
            for concept_id in unit_concepts:
                for mapping in mapping_by_concept.get(concept_id, []):
                    if unit.unit_id not in mapping.unit_ids:
                        continue
                    # A question may support several concepts in this unit; it is
                    # still one question and its marks are counted once here.
                    qids[mapping.question.question_id] = mapping.question.marks
            mapped_marks = sum(marks for marks in qids.values() if marks is not None)
            with_marks = sum(1 for marks in qids.values() if marks is not None)
            covered = status_counts[CoverageStatus.COVERED.value]
            low = status_counts[CoverageStatus.LOW_COVERAGE.value]
            count = len(unit_findings)
            needs_attention = [
                finding.label
                for finding in unit_findings
                if finding.status is not CoverageStatus.COVERED
            ]
            unit_warnings: list[Uncertainty] = []
            if not unit.topics:
                unit_warnings.append(
                    _warning(
                        "coverage_unit_no_concepts",
                        "No syllabus topics were parsed for this unit; it cannot be assessed.",
                        Severity.WARNING,
                    )
                )
            elif count and all(
                finding.status is CoverageStatus.INSUFFICIENT_DATA for finding in unit_findings
            ):
                unit_warnings.append(
                    _warning(
                        "coverage_unit_insufficient_data",
                        "Every concept in this unit lacks sufficient evidence for classification.",
                        Severity.WARNING,
                    )
                )
            if qids and with_marks < len(qids):
                unit_warnings.append(
                    _warning(
                        "coverage_missing_marks",
                        (
                            f"{len(qids) - with_marks} mapped question(s) in this unit have "
                            "no stated marks; mark totals exclude them"
                        ),
                        Severity.INFO,
                    )
                )
            if qids and not any(
                context.period.session_key is not None
                for context in contexts
                if context.record.question_id in qids
            ):
                unit_warnings.append(
                    _warning(
                        "missing_period_metadata",
                        (
                            "No mapped question in this unit has an explicit examination "
                            "session; session counts are unavailable."
                        ),
                        Severity.INFO,
                    )
                )
            if not qids and count:
                unit_warnings.append(
                    _warning(
                        "coverage_unit_no_mapped_questions",
                        "No question was confidently mapped to a concept in this unit.",
                        Severity.INFO,
                    )
                )
            rows.append(
                UnitCoverageFinding(
                    unit_id=unit.unit_id,
                    unit_label=unit.unit_label,
                    unit_title=unit.title,
                    unit_number=unit.unit_number,
                    syllabus_id=loaded.selected_syllabus.syllabus_id,
                    source_document_id=loaded.selected_syllabus.document_id,
                    source_page=unit.page_number,
                    concept_count=count,
                    concept_ids=unit_concepts,
                    status_counts=status_counts,
                    distinct_question_count=len(qids),
                    mapped_marks=round(mapped_marks, 4),
                    questions_with_marks=with_marks,
                    concept_coverage_rate=round(covered / count, 4) if count else None,
                    assessed_concept_rate=(round((covered + low) / count, 4) if count else None),
                    question_share=(
                        round(len(qids) / total_questions, 4) if total_questions else None
                    ),
                    mapped_marks_share=(
                        round(mapped_marks / total_known_marks, 4) if total_known_marks else None
                    ),
                    needs_attention=needs_attention,
                    warnings=unit_warnings,
                )
            )
        return rows

    def _unmapped_questions(
        self,
        loaded: _Loaded,
        mappings: dict[tuple[str, str], _Mapping],
        candidates: dict[str, list[CoverageMappingCandidate]],
        contexts: list[_QuestionContext],
    ) -> list[UnmappedQuestionFinding]:
        mapped_qids = {mapping.question.question_id for mapping in mappings.values()}
        candidate_by_qid: dict[str, list[CoverageMappingCandidate]] = defaultdict(list)
        for items in candidates.values():
            for item in items:
                candidate_by_qid[item.question_id].append(item)
        rows: list[UnmappedQuestionFinding] = []
        for context in contexts:
            if context.record.question_id in mapped_qids:
                continue
            reason, explanation = self._unmapped_reason(context, loaded)
            existing = [
                self._candidate_from_match(
                    context,
                    match,
                    self._existing_mapping_reason(match, loaded, context.analysis),
                )
                for match in context.matches
            ]
            existing.extend(candidate_by_qid.get(context.record.question_id, []))
            unique: dict[tuple[str, str, str, str], CoverageMappingCandidate] = {}
            for item in existing:
                unique[(item.question_id, item.unit_id or "", item.topic or "", item.reason)] = item
            rows.append(
                UnmappedQuestionFinding(
                    question_id=context.record.question_id,
                    document_id=context.document.document_id,
                    document_title=context.document.title,
                    document_kind=context.document.kind,
                    source_id=context.document.source_id,
                    question_number=context.record.question_number,
                    section=context.record.section,
                    page_number=context.record.page_number,
                    source_locator=context.record.source_locator,
                    marks=context.record.marks,
                    text_excerpt=_excerpt(context.record.text),
                    analysis_status=(context.analysis.mapping_status if context.analysis else None),
                    reason=reason,
                    explanation=explanation,
                    exam_period=context.period.label,
                    exam_period_source=context.period.source,
                    exam_session=context.period.session,
                    exam_type=context.period.exam_type,
                    existing_mappings=sorted(
                        unique.values(),
                        key=lambda item: (
                            item.unit_id or "",
                            item.rank or 999,
                            item.score if item.score is not None else -1.0,
                        ),
                    ),
                )
            )
        return rows

    def _unmapped_reason(self, context: _QuestionContext, loaded: _Loaded) -> tuple[str, str]:
        if context.analysis is None:
            return (
                "not_analysed",
                "No Phase 3 analysis exists. The question is unresolved, not proven "
                "to be outside the syllabus.",
            )
        if context.analysis.mapping_status is MappingStatus.AMBIGUOUS:
            return (
                "ambiguous_mapping",
                "Several syllabus candidates matched within the ambiguity margin. "
                "None is treated as coverage without review.",
            )
        if context.analysis.mapping_status is MappingStatus.UNMAPPED:
            messages = [
                item.message
                for item in context.analysis.uncertainties
                if item.code == "unmapped_question"
            ]
            return (
                "no_mapping_above_threshold",
                messages[0]
                if messages
                else "Phase 3 found no syllabus mapping above its configured threshold.",
            )
        selected_matches = [
            match
            for match in context.matches
            if match.syllabus_id == loaded.selected_syllabus.syllabus_id
        ]
        if not selected_matches:
            return (
                "other_syllabus_only",
                "The stored mappings point to another syllabus version; none belongs to "
                "the selected version.",
            )
        if any(not match.topic for match in selected_matches):
            return (
                "unit_only_mapping",
                "The stored mapping identifies a unit but not a topic, so it cannot "
                "establish concept coverage.",
            )
        return (
            "no_accepted_mapping",
            "Mappings exist but none meets the Phase 7 topic/confidence acceptance "
            "policy; they remain review evidence only.",
        )

    def _existing_mapping_reason(
        self,
        match: UnitMatch,
        loaded: _Loaded,
        analysis: QuestionAnalysis | None,
    ) -> str:
        if match.syllabus_id != loaded.selected_syllabus.syllabus_id:
            return "other_syllabus"
        if analysis is not None and analysis.mapping_status is MappingStatus.AMBIGUOUS:
            return "ambiguous_mapping"
        if analysis is not None and analysis.mapping_status is MappingStatus.UNMAPPED:
            return "analysis_unmapped"
        if not match.topic:
            return "unit_only_mapping"
        if match.rank != 1:
            return "secondary_mapping"
        if match.score < self.settings.mapping_confidence_floor:
            return "below_confidence_floor"
        return "not_accepted"

    # -- summary and quality ----------------------------------------------

    def _summary(
        self,
        findings: list[ConceptCoverageFinding],
        mappings: dict[tuple[str, str], _Mapping],
        contexts: list[_QuestionContext],
    ) -> CoverageSummary:
        counts = Counter(finding.status for finding in findings)
        mapped_qids = {mapping.question.question_id for mapping in mappings.values()}
        analysed = sum(1 for context in contexts if context.analysis is not None)
        ambiguous = sum(
            1
            for context in contexts
            if context.analysis is not None
            and context.analysis.mapping_status is MappingStatus.AMBIGUOUS
        )
        not_analysed = sum(1 for context in contexts if context.analysis is None)
        total = len(contexts)
        known_marks = sum(
            context.record.marks or 0.0 for context in contexts if context.record.marks is not None
        )
        mapped_marks = sum(
            context.record.marks or 0.0
            for context in contexts
            if context.record.question_id in mapped_qids and context.record.marks is not None
        )
        questions_with_marks = sum(1 for context in contexts if context.record.marks is not None)
        covered = counts[CoverageStatus.COVERED]
        return CoverageSummary(
            total_concepts=len(findings),
            covered_concepts=covered,
            low_coverage_concepts=counts[CoverageStatus.LOW_COVERAGE],
            unmapped_concepts=counts[CoverageStatus.UNMAPPED],
            uncertain_concepts=counts[CoverageStatus.UNCERTAIN],
            insufficient_data_concepts=counts[CoverageStatus.INSUFFICIENT_DATA],
            total_questions=total,
            analysed_questions=analysed,
            mapped_questions=len(mapped_qids),
            unmapped_questions=total - len(mapped_qids),
            ambiguous_questions=ambiguous,
            not_analysed_questions=not_analysed,
            analysis_coverage_rate=round(analysed / total, 4) if total else None,
            mapped_question_rate=round(len(mapped_qids) / total, 4) if total else None,
            concept_coverage_rate=round(covered / len(findings), 4) if findings else None,
            known_sessions=len(
                {
                    context.period.session_key
                    for context in contexts
                    if context.period.session_key is not None
                }
            ),
            total_known_marks=round(known_marks, 4),
            mapped_marks=round(mapped_marks, 4),
            questions_with_marks=questions_with_marks,
            missing_marks=total - questions_with_marks,
            mapped_marks_share=(round(mapped_marks / known_marks, 4) if known_marks else None),
            shared_concept_units=sum(1 for finding in findings if len(finding.unit_ids) > 1),
        )

    def _data_quality(
        self,
        loaded: _Loaded,
        concepts: dict[str, _Concept],
        mappings: dict[tuple[str, str], _Mapping],
        contexts: list[_QuestionContext],
        load_warnings: list[Uncertainty],
        *,
        unscoped: int,
        truncated: bool,
    ) -> CoverageDataQuality:
        docs = {context.document.document_id: context.document for context in contexts}
        mapped_qids = {mapping.question.question_id for mapping in mappings.values()}
        analysed = sum(1 for context in contexts if context.analysis is not None)
        total = len(contexts)
        known_marks = sum(1 for context in contexts if context.record.marks is not None)
        sessions = {
            context.period.session_key
            for context in contexts
            if context.period.session_key is not None
        }
        warnings = list(load_warnings)
        if 0 < total < self.settings.small_corpus_questions:
            warnings.append(
                _warning(
                    "coverage_small_corpus",
                    (
                        f"only {total} question(s) are in the selected corpus; absence "
                        "of a mapping is weak evidence"
                    ),
                    Severity.WARNING,
                )
            )
        if analysed < total:
            warnings.append(
                _warning(
                    "coverage_partial_analysis",
                    f"{total - analysed} of {total} question(s) have no Phase 3 analysis",
                    Severity.WARNING,
                )
            )
        if not self.settings.include_graph_explicit_mappings:
            warnings.append(
                _warning(
                    "coverage_graph_disabled",
                    (
                        "Phase 5 graph mappings are disabled by configuration; only "
                        "Phase 3 syllabus mappings are used"
                    ),
                    Severity.INFO,
                )
            )
        elif not loaded.graph_nodes_available:
            warnings.append(
                _warning(
                    "coverage_graph_unavailable",
                    (
                        "no matching Phase 5 graph nodes were found; syllabus topic "
                        "identity is used without graph aliases or explicit graph mappings"
                    ),
                    Severity.INFO,
                )
            )
        return CoverageDataQuality(
            documents=len(docs),
            document_ids=sorted(docs),
            source_ids=sorted({document.source_id for document in docs.values()}),
            question_papers=sum(
                1 for document in docs.values() if document.kind is DocumentKind.QUESTION_PAPER
            ),
            question_banks=sum(
                1 for document in docs.values() if document.kind is DocumentKind.QUESTION_BANK
            ),
            sources=len({document.source_id for document in docs.values()}),
            questions=total,
            analysed_questions=analysed,
            not_analysed_questions=total - analysed,
            mapped_questions=len(mapped_qids),
            ambiguous_questions=sum(
                1
                for context in contexts
                if context.analysis is not None
                and context.analysis.mapping_status is MappingStatus.AMBIGUOUS
            ),
            unmapped_questions=total - len(mapped_qids),
            questions_with_marks=known_marks,
            missing_marks=total - known_marks,
            marks_coverage_rate=round(known_marks / total, 4) if total else None,
            known_sessions=len(sessions),
            questions_with_session=sum(
                1 for context in contexts if context.period.session_key is not None
            ),
            questions_without_session=sum(
                1 for context in contexts if context.period.session_key is None
            ),
            graph_concepts_matched=sum(
                1 for concept in concepts.values() if concept.graph_concept_id is not None
            ),
            graph_assessment_edges=len(
                {edge_id for mapping in mappings.values() for edge_id in mapping.graph_edge_ids}
            ),
            unscoped_questions_excluded=unscoped,
            truncated=truncated,
            small_corpus=total < self.settings.small_corpus_questions,
            warnings=warnings,
        )

    def _report_status(
        self,
        findings: list[ConceptCoverageFinding],
        summary: CoverageSummary,
        quality: CoverageDataQuality,
    ) -> CoverageReportStatus:
        if not findings or summary.total_questions == 0:
            return CoverageReportStatus.INSUFFICIENT_DATA
        if summary.analysed_questions == 0 and summary.mapped_questions == 0:
            return CoverageReportStatus.INSUFFICIENT_DATA
        if (
            quality.small_corpus
            or quality.truncated
            or quality.unscoped_questions_excluded
            or (summary.analysis_coverage_rate or 0.0) < self.settings.min_analysis_coverage
            or any(
                finding.status in (CoverageStatus.UNCERTAIN, CoverageStatus.INSUFFICIENT_DATA)
                for finding in findings
            )
        ):
            return CoverageReportStatus.PARTIAL
        return CoverageReportStatus.OK

    def _report_warnings(
        self,
        loaded: _Loaded,
        concepts: dict[str, _Concept],
        findings: list[ConceptCoverageFinding],
        quality: CoverageDataQuality,
        contexts: list[_QuestionContext],
        *,
        unscoped: int,
        truncated: bool,
    ) -> list[Uncertainty]:
        warnings: list[Uncertainty] = list(quality.warnings)
        if truncated:
            warnings.append(
                _warning(
                    "coverage_truncated",
                    (
                        f"the corpus was capped at {self.settings.max_questions} "
                        "questions; later questions were not analysed"
                    ),
                    Severity.WARNING,
                )
            )
        if unscoped:
            warnings.append(
                _warning(
                    "coverage_unattributed_questions",
                    (
                        f"{unscoped} question(s) could not be attributed to the selected "
                        "syllabus version and were excluded"
                    ),
                    Severity.INFO,
                )
            )
        if (
            self.settings.include_graph_explicit_mappings
            and loaded.graph_nodes_available
            and any(concept.graph_concept_id is None for concept in concepts.values())
        ):
            warnings.append(
                _warning(
                    "coverage_graph_unavailable",
                    (
                        "some syllabus concepts have no matching Phase 5 graph node; "
                        "topic identity is still used and provenance retained"
                    ),
                    Severity.INFO,
                )
            )
        if quality.ambiguous_questions:
            warnings.append(
                _warning(
                    "coverage_ambiguous_evidence",
                    f"{quality.ambiguous_questions} question(s) have ambiguous mappings; "
                    "they remain unresolved candidates",
                    Severity.INFO,
                )
            )
        if any(len(finding.unit_ids) > 1 for finding in findings):
            warnings.append(
                _warning(
                    "coverage_shared_concept_units",
                    "some concepts occur in multiple units; unit concept counts overlap",
                    Severity.INFO,
                )
            )
        if quality.missing_marks:
            warnings.append(
                _warning(
                    "coverage_missing_marks",
                    (
                        f"{quality.missing_marks} selected question(s) have no stated marks; "
                        "mark totals and shares exclude them"
                    ),
                    Severity.INFO,
                )
            )
        if contexts and not any(context.period.session_key is not None for context in contexts):
            warnings.append(
                _warning(
                    "missing_period_metadata",
                    "no selected question has an explicit examination session; session "
                    "metrics are unavailable",
                    Severity.INFO,
                )
            )
        return _dedupe_warnings(warnings)

    def _parameters(self, loaded: _Loaded, request: CoverageRequest) -> dict[str, Any]:
        return {
            "covered_min_questions": self.settings.covered_min_questions,
            "low_confidence_threshold": self.settings.low_confidence_threshold,
            "mapping_confidence_floor": self.settings.mapping_confidence_floor,
            "small_corpus_questions": self.settings.small_corpus_questions,
            "min_analysis_coverage": self.settings.min_analysis_coverage,
            "max_questions": self.settings.max_questions,
            "include_graph_explicit_mappings": self.settings.include_graph_explicit_mappings,
            "selected_syllabus_id": loaded.selected_syllabus.syllabus_id,
            "course_syllabus_count": len(loaded.syllabi),
            "requested_syllabus_id": request.syllabus_id,
            "filter_active": request.filters.restricts,
            "mapping_policy": "phase3_primary_mapping+phase5_explicit_assessment",
            "unmapped_semantics": (
                "not represented by confidently mapped questions in the available corpus; "
                "not proof of never being examined"
            ),
        }

    def _report_id(
        self,
        loaded: _Loaded,
        concepts: dict[str, _Concept],
        mappings: dict[tuple[str, str], _Mapping],
        contexts: list[_QuestionContext],
        findings: list[ConceptCoverageFinding],
        unmapped: list[UnmappedQuestionFinding],
        parameters: dict[str, Any],
        filters: CoverageFilters,
    ) -> str:
        payload = {
            "course": loaded.course.course_id,
            "syllabus": loaded.selected_syllabus.syllabus_id,
            "parameters": parameters,
            "filters": filters.model_dump(mode="json"),
            "mappings": sorted(
                (
                    mapping.question.question_id,
                    mapping.concept_id,
                    tuple(mapping.source_names),
                    mapping.method,
                    mapping.score,
                    tuple(sorted(mapping.graph_edge_ids)),
                    tuple(sorted(mapping.evidence)),
                )
                for mapping in mappings.values()
            ),
            "concepts": sorted(
                (
                    concept.concept_id,
                    concept.normalized_label,
                    list(concept.unit_ids),
                )
                for concept in concepts.values()
            ),
            "questions": sorted(
                (
                    context.record.question_id,
                    context.record.fingerprint,
                    context.record.marks,
                    context.analysis.mapping_status.value if context.analysis else None,
                )
                for context in contexts
            ),
            "findings": sorted((finding.concept_id, finding.status.value) for finding in findings),
            "unmapped": sorted(item.question_id for item in unmapped),
        }
        return make_coverage_report_id(
            json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        )

    def _limitations(
        self,
        loaded: _Loaded,
        quality: CoverageDataQuality,
        findings: list[ConceptCoverageFinding],
        filters: CoverageFilters,
    ) -> list[str]:
        limitations = [
            "Coverage describes the available question corpus only. A concept absent here "
            "has not necessarily been absent from examinations, and it is not a forecast.",
            "A question may support several concepts; concept counts are not additive, and "
            "full question marks are attributed to each supporting concept rather than split.",
            "Unit question and mark totals count each distinct question once per unit, but a "
            "question mapped to concepts in two units contributes to both units.",
            "Syllabus concepts are the topics parsed from syllabus units; a topic that "
            "segmentation missed is absent from this report rather than silently invented.",
            "Mapping scores are Phase 3 confidence estimates, not calibrated probabilities.",
            "Frequency is not educational importance; low coverage does not mean a concept "
            "is unimportant.",
            "Explicit/reviewed/verified Phase 5 ASSESSES edges are accepted as existing "
            "mappings. Inferred and candidate edges are not promoted into coverage.",
            "Question analysis and the graph are derived state; rerun the relevant phase "
            "after changing documents, syllabi or questions.",
        ]
        if filters.unit_ids:
            limitations.append(
                "Unit filtering restricts the syllabus concepts under review but keeps the "
                "whole selected question corpus as the denominator. Questions belonging only "
                "to other units therefore appear unresolved in report-level mapped/unmapped "
                "counts and are not evidence about the selected unit."
            )
        if len(loaded.syllabi) > 1:
            limitations.append(
                "Only questions attributable to the selected syllabus version are in the "
                "denominator. Course-wide questions without any version-specific mapping "
                "are excluded and counted in data quality. The course-level syllabus_version "
                "is not a per-document version label; use syllabus_id as the version identity."
            )
        if quality.small_corpus:
            limitations.append(
                "The selected corpus is below the configured small-corpus threshold, so "
                "negative findings are explicitly uncertain."
            )
        if not self.settings.include_graph_explicit_mappings:
            limitations.append(
                "Phase 5 graph mappings were disabled for this analysis; only Phase 3 "
                "syllabus mappings were used."
            )
        elif not loaded.graph_nodes_available:
            limitations.append(
                "No matching Phase 5 graph nodes were found; concept identity and aliases "
                "come from syllabus topics only."
            )
        if quality.not_analysed_questions:
            limitations.append(
                f"{quality.not_analysed_questions} selected question(s) have no Phase 3 "
                "analysis and cannot contribute accepted mappings."
            )
        if quality.missing_marks:
            limitations.append(
                f"{quality.missing_marks} selected question(s) have no stated marks; mark "
                "totals and shares exclude them rather than inferring values."
            )
        if quality.questions_without_session:
            limitations.append(
                "Some selected questions have no explicit examination session; session "
                "counts are lower bounds, not inferred dates or attendance history."
            )
        uncertain_extraction = [
            finding
            for finding in findings
            if finding.extraction_confidence is not None and finding.extraction_confidence < 0.6
        ]
        if uncertain_extraction:
            limitations.append(
                f"{len(uncertain_extraction)} syllabus concept(s) came from low-confidence "
                "unit extraction; their identity or scope may be incomplete."
            )
        return limitations


def _alias_index(
    nodes: Iterable[GraphNode], by_label: dict[str, GraphNode]
) -> dict[str, GraphNode]:
    """Map normalized graph aliases to nodes without overriding canonical labels."""
    index: dict[str, GraphNode] = {}
    ambiguous: set[str] = set()
    for node in nodes:
        raw_aliases = node.metadata.get("aliases", []) or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        for raw in raw_aliases:
            term = normalize_term(str(raw))
            if not term or term in by_label:
                continue
            existing = index.get(term)
            if existing is not None and existing.node_id != node.node_id:
                ambiguous.add(term)
            else:
                index[term] = node
    for term in ambiguous:
        index.pop(term, None)
    return index


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


def _excerpt(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= EXCERPT_CHARS:
        return compact
    return compact[: EXCERPT_CHARS - 3].rstrip() + "..."


def _warning(
    code: str,
    message: str,
    severity: Severity = Severity.WARNING,
    *,
    detail: dict[str, Any] | None = None,
) -> Uncertainty:
    return Uncertainty(
        code=code,
        message=message,
        severity=severity,
        detail=detail or {},
    )


def _dedupe_warnings(items: Iterable[Uncertainty]) -> list[Uncertainty]:
    seen: set[tuple[str, str, str]] = set()
    result: list[Uncertainty] = []
    for item in items:
        key = (item.code, item.message, item.severity.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


__all__ = ["CoverageError", "CoverageService"]
