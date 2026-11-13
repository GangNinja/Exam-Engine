"""Read-only Phase 8 Exam DNA profile service.

The engine reuses rather than reimplements earlier phases:

* Phase 1 documents/questions and their provenance for paper identity;
* Phase 3 stored analysis, unit matches, similarity pairs and Bloom labels;
* Phase 4 period resolution, so an examination date is never invented;
* Phase 7 coverage for accepted concept mappings and coverage statuses.

It adds no table and writes nothing. Every recurrence rate carries its paper (and
where available, session) denominator. A duplicate copy of a paper is excluded
from independent evidence and reported explicitly.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select

from exam_engine.analytics.metadata import ExamPeriod, resolve_exam_period
from exam_engine.config import CoverageSettings, ExamDnaSettings
from exam_engine.coverage.service import CoverageError, CoverageService
from exam_engine.exam_dna.structure import (
    detect_choice_rules,
    excerpt,
    extract_command_verb,
    normalise_for_exact,
    wording_difference,
)
from exam_engine.models.analytics import CourseScope
from exam_engine.models.course import CourseRecord
from exam_engine.models.coverage import CoverageFilters, CoverageReport, CoverageRequest
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    DocumentKind,
    EvidenceQuality,
    ExamDnaStatus,
    ExtractionStatus,
    MappingStatus,
    PaperExclusionReason,
    PatternCategory,
    QuestionGroupKind,
    QuestionType,
    Severity,
    SimilarityKind,
)
from exam_engine.models.exam_dna import (
    EXAM_DNA_ALGORITHM,
    EXAM_DNA_ALGORITHM_VERSION,
    EXAM_DNA_SCHEMA_VERSION,
    ExamDnaChoiceRule,
    ExamDnaConceptRef,
    ExamDnaCoverage,
    ExamDnaDataQuality,
    ExamDnaDistributionRow,
    ExamDnaDistributions,
    ExamDnaPaper,
    ExamDnaPattern,
    ExamDnaProfile,
    ExamDnaQuestionGroup,
    ExamDnaRequest,
    ExamDnaScope,
    ExamDnaSession,
    ExamDnaSummary,
    ExamDnaSyllabus,
    ExamDnaTrend,
    PaperExclusion,
    QuestionGroupMember,
    make_pattern_id,
    make_profile_id,
    make_question_group_id,
)
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.understanding import QuestionAnalysis, SimilarityMatch, UnitMatch
from exam_engine.storage.db import Database
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
)
from exam_engine.storage.schema import ExamPaperRow, ExamQuestionDetailRow
from exam_engine.understanding.classification import difficulty_band

logger = logging.getLogger(__name__)

_CATEGORY_ORDER = {category: index for index, category in enumerate(PatternCategory)}
_GROUP_ORDER = {kind: index for index, kind in enumerate(QuestionGroupKind)}
_SIMILARITY_GROUP_KINDS = {
    SimilarityKind.NEAR: QuestionGroupKind.NEAR_DUPLICATE,
    SimilarityKind.SEMANTIC: QuestionGroupKind.SEMANTIC,
    SimilarityKind.CONCEPT_RELATED: QuestionGroupKind.CONCEPT_RELATED,
}
_EXCLUDING_EXTRACTION = {
    ExtractionStatus.FAILED,
    ExtractionStatus.EMPTY,
    ExtractionStatus.NEEDS_OCR,
}
_MARKS_BANDS: tuple[tuple[float, str], ...] = (
    (2.0, "0-2 marks"),
    (5.0, "3-5 marks"),
    (10.0, "6-10 marks"),
)
_OVERFLOW_MARKS_BAND = "11+ marks"
_AMBIGUOUS_CHOICE_RE_HINT = ("any", "either")
_MIN_DATED_SESSIONS_FOR_TRENDS = 3
_TREND_STABLE_BAND = 0.05
_TREND_LIMITATION = (
    "A descriptive change between observed dated sessions only; Phase 8 does not project "
    "it forward, calibrate it as a probability, or claim examiner intent."
)


class ExamDnaError(ValueError):
    """A structured, user-correctable Exam DNA failure."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class _PaperQuestion:
    question: QuestionRecord
    document: DocumentRecord
    period: ExamPeriod
    analysis: QuestionAnalysis | None
    matches: tuple[UnitMatch, ...]
    command_verb: str | None
    choice_rules: tuple[ExamDnaChoiceRule, ...]
    concepts: set[str] = field(default_factory=set)
    review_matches: tuple[UnitMatch, ...] = ()
    foreign_syllabus_only: bool = False

    @property
    def assigned_matches(self) -> tuple[UnitMatch, ...]:
        if self.analysis is None:
            return ()
        if self.analysis.mapping_status is MappingStatus.MAPPED:
            return self.matches[:1]
        if self.analysis.mapping_status is MappingStatus.AMBIGUOUS:
            return self.matches
        return ()

    @property
    def unit_matches(self) -> tuple[UnitMatch, ...]:
        """Assignments used by unit distributions.

        The complete coverage review table supplies one deterministic best-fit
        unit for every question, including questions Phase 3 left unmapped. The
        strict Phase 3 assignment still governs patterns and mapping status.
        """
        return self.review_matches or self.assigned_matches

    @property
    def primary_type(self) -> QuestionType | None:
        return self.analysis.primary_type if self.analysis else None

    @property
    def bloom_level(self) -> BloomLevel | None:
        return self.analysis.bloom_primary if self.analysis else None

    @property
    def difficulty(self) -> DifficultyLabel | None:
        analysis = self.analysis
        if analysis is None:
            return None
        if analysis.difficulty_label is not DifficultyLabel.UNKNOWN:
            return analysis.difficulty_label
        if analysis.word_count <= 0:
            return DifficultyLabel.UNKNOWN
        return difficulty_band(analysis.difficulty_score)

    @property
    def low_confidence(self) -> bool:
        analysis = self.analysis
        if analysis is None:
            return False
        if analysis.primary_type is not None:
            return analysis.primary_type_confidence < 0.5
        if analysis.bloom_primary is not None:
            return analysis.bloom_confidence < 0.5
        return False


@dataclass
class _Paper:
    document: DocumentRecord
    period: ExamPeriod
    questions: list[_PaperQuestion]
    eligible: bool = True
    exclusion_reason: PaperExclusionReason | None = None
    duplicate_of: str | None = None
    signature: str = ""


@dataclass
class _Loaded:
    course: CourseRecord
    documents: list[DocumentRecord]
    periods: dict[str, ExamPeriod]
    papers: list[_Paper]
    similarities: list[SimilarityMatch]
    question_count: int


def _virtual_paper_document(
    parent: DocumentRecord, paper: Any, questions: list[QuestionRecord]
) -> DocumentRecord:
    """Present a stored exam paper as a read-only paper document for Phase 8."""
    body = "\n".join(item.text for item in questions)
    digest = hashlib.sha256(
        f"{parent.course_id}\x1f{paper.paper_id}\x1f{body}".encode()
    ).hexdigest()
    metadata = dict(parent.metadata)
    metadata.update(
        {
            "paper_label": paper.paper_label,
            "exam_year": paper.exam_year,
            "parent_document_id": parent.document_id,
            "derived_paper": True,
            "source_csv": parent.metadata.get("source_csv"),
        }
    )
    return DocumentRecord(
        document_id=paper.paper_id,
        course_id=parent.course_id,
        source_id=parent.source_id,
        kind=DocumentKind.QUESTION_PAPER,
        format=parent.format,
        title=f"{parent.title} — {paper.paper_label}",
        original_filename=parent.original_filename,
        content_sha256=digest,
        text_fingerprint=hashlib.sha256(body.encode()).hexdigest(),
        byte_size=len(body.encode("utf-8")),
        page_count=1,
        char_count=len(body),
        extraction_status=ExtractionStatus.OK,
        quality_score=parent.quality_score,
        language=parent.language,
        academic_year=str(paper.exam_year) if paper.exam_year else parent.academic_year,
        relative_path=parent.relative_path,
        ingested_at=parent.ingested_at,
        pipeline_version="phase13-paper-group-v1",
        metadata=metadata,
        warnings=list(parent.warnings),
    )


def _paper_selected(paper: _Paper, document_ids: set[str]) -> bool:
    if paper.document.document_id in document_ids:
        return True
    parent = paper.document.metadata.get("parent_document_id")
    return bool(parent and str(parent) in document_ids)


@dataclass
class _PatternAcc:
    label: str
    description: str
    documents: set[str] = field(default_factory=set)
    questions: set[str] = field(default_factory=set)
    occurrences: int = 0
    sessions: set[str] = field(default_factory=set)
    periods: set[str] = field(default_factory=set)
    exam_types: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)


@dataclass
class _DistributionAcc:
    questions: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)
    marks: dict[str, float] = field(default_factory=dict)


def _round(value: float) -> float:
    return round(float(value), 4)


def _rate(count: float, total: float) -> float | None:
    return _round(count / total) if total else None


def _warning(
    code: str,
    message: str,
    *,
    severity: Severity = Severity.WARNING,
    detail: dict[str, Any] | None = None,
) -> Uncertainty:
    return Uncertainty(code=code, message=message, severity=severity, detail=detail or {})


def _dedupe_warnings(items: list[Uncertainty]) -> list[Uncertainty]:
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


def _question_signature(questions: list[_PaperQuestion]) -> str:
    if not questions:
        return ""
    joined = "\x1f".join(sorted(normalise_for_exact(item.question.text) for item in questions))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _type_label(item: _PaperQuestion) -> str:
    if item.analysis is None:
        return "not_analysed"
    return item.primary_type.value if item.primary_type else "unclassified"


def _bloom_label(item: _PaperQuestion) -> str:
    if item.analysis is None:
        return "not_analysed"
    return item.bloom_level.value if item.bloom_level else "unclassified"


def _difficulty_label(item: _PaperQuestion) -> str:
    if item.analysis is None:
        return "not_analysed"
    return item.difficulty.value if item.difficulty else "unknown"


def _marks_band(value: float) -> str:
    for limit, label in _MARKS_BANDS:
        if value <= limit:
            return label
    return _OVERFLOW_MARKS_BAND


def _period_matches(period: ExamPeriod, wanted: list[str]) -> bool:
    if not wanted:
        return True
    if not period.known:
        return False
    values = {
        period.label.casefold(),
        period.key.casefold(),
        (period.session or "").casefold(),
    }
    return any(value.casefold() in values for value in wanted)


class ExamDnaService:
    """Build deterministic historical pattern profiles from stored records."""

    def __init__(
        self,
        database: Database,
        settings: ExamDnaSettings | None = None,
        *,
        coverage_settings: CoverageSettings | None = None,
    ) -> None:
        self.db = database
        self.settings = settings or ExamDnaSettings()
        self.coverage_settings = coverage_settings or CoverageSettings()

    # -- public API --------------------------------------------------------

    def analyze(self, request: ExamDnaRequest) -> ExamDnaProfile:
        loaded = self._load(request)
        candidates, date_excluded = self._select_papers(loaded, request)
        self._mark_duplicates(loaded, candidates)
        eligible, truncated = self._truncate([paper for paper in loaded.papers if paper.eligible])
        questions = [item for paper in eligible for item in paper.questions]
        coverage = self._coverage(request, eligible)
        other_syllabus_questions, other_syllabus_papers = self._apply_syllabus_scope(
            loaded, coverage.syllabus.syllabus_id
        )
        self._attach_concepts(loaded, coverage)
        self._attach_unit_assignments(loaded, coverage)

        distributions = self._distributions(eligible)
        sessions, trends = self._session_series(eligible)
        patterns, patterns_truncated = self._patterns(eligible, questions)
        groups, similarity_stats, groups_truncated = self._question_groups(
            loaded, eligible, questions
        )
        quality = self._data_quality(
            loaded,
            eligible,
            questions,
            coverage,
            similarity_stats,
            truncated=truncated,
            patterns_truncated=patterns_truncated,
            groups_truncated=groups_truncated,
            other_syllabus_questions=other_syllabus_questions,
            other_syllabus_papers=other_syllabus_papers,
        )
        status = self._status(eligible, questions, quality)
        warnings = self._warnings(eligible, questions, quality, coverage, date_excluded)
        summary = self._summary(
            eligible,
            questions,
            patterns,
            groups,
            distributions,
            quality,
            sessions=sessions,
            trends=trends,
        )
        parameters = {
            **self.settings.as_config(),
            "coverage_report_id": coverage.report_id,
            "coverage_status": coverage.status.value,
            "selected_syllabus_id": coverage.syllabus.syllabus_id,
            "mapping_policy": "phase7_coverage_accepted_mappings+phase3_assignments",
            "forecasting": "out_of_scope",
        }
        scope = self._scope(request, candidates, date_excluded, eligible, coverage)
        profile_id = self._profile_id(
            request, eligible, questions, patterns, groups, coverage, parameters, status
        )
        return ExamDnaProfile(
            schema_version=EXAM_DNA_SCHEMA_VERSION,
            profile_id=profile_id,
            algorithm=EXAM_DNA_ALGORITHM,
            algorithm_version=EXAM_DNA_ALGORITHM_VERSION,
            course=_course_scope(loaded.course),
            syllabus=ExamDnaSyllabus(
                syllabus_id=coverage.syllabus.syllabus_id,
                title=coverage.syllabus.title,
                document_id=coverage.syllabus.document_id,
                course_syllabus_version=coverage.syllabus.course_syllabus_version,
                unit_count=coverage.syllabus.unit_count,
                concept_count=coverage.syllabus.concept_count,
                confidence=coverage.syllabus.confidence,
                selected_automatically=request.syllabus_id is None,
            ),
            status=status,
            filters=request.filters,
            scope=scope,
            parameters=parameters,
            summary=summary,
            papers=[
                self._paper_summary(paper)
                for paper in loaded.papers
                if paper.document.kind is DocumentKind.QUESTION_PAPER
            ],
            distributions=distributions,
            patterns=patterns,
            question_groups=groups,
            sessions=sessions,
            trends=trends,
            coverage=self._coverage_summary(coverage),
            data_quality=quality,
            warnings=warnings,
            limitations=self._limitations(quality),
        )

    def patterns(
        self,
        request: ExamDnaRequest,
        *,
        category: PatternCategory | None = None,
        recurring_only: bool = False,
    ) -> list[ExamDnaPattern]:
        profile = self.analyze(request)
        items = [
            pattern
            for pattern in profile.patterns
            if (category is None or pattern.category is category)
            and (not recurring_only or pattern.is_recurring)
        ]
        return items

    def question_groups(
        self,
        request: ExamDnaRequest,
        *,
        kind: QuestionGroupKind | None = None,
    ) -> list[ExamDnaQuestionGroup]:
        profile = self.analyze(request)
        return [group for group in profile.question_groups if kind is None or group.kind is kind]

    # -- loading -----------------------------------------------------------

    def _load(self, request: ExamDnaRequest) -> _Loaded:
        with self.db.session() as session:
            course = CourseRepository(session).get(request.course_id)
            if course is None:
                raise ExamDnaError("unknown_course", f"unknown course: {request.course_id}")
            documents = DocumentRepository(session).list_by_course(request.course_id)
            questions = QuestionRepository(session).list_by_course(request.course_id)
            understanding = QuestionUnderstandingRepository(session)
            analyses = {
                analysis.question_id: analysis
                for analysis in understanding.list_analyses(request.course_id)
            }
            matches: dict[str, list[UnitMatch]] = defaultdict(list)
            for match in understanding.list_unit_matches_for_course(request.course_id):
                matches[match.question_id].append(match)
            similarities = understanding.list_similarities(request.course_id)
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

        periods = {
            document.document_id: resolve_exam_period(document, course) for document in documents
        }
        by_document: dict[str, list[_PaperQuestion]] = defaultdict(list)
        for question in questions:
            document = next(
                (item for item in documents if item.document_id == question.document_id), None
            )
            if document is None:
                continue
            by_document[document.document_id].append(
                _PaperQuestion(
                    question=question,
                    document=document,
                    period=periods[document.document_id],
                    analysis=analyses.get(question.question_id),
                    matches=tuple(matches.get(question.question_id, [])),
                    command_verb=extract_command_verb(question.text),
                    choice_rules=tuple(detect_choice_rules(question.text, question.question_id)),
                )
            )
        papers: list[_Paper] = []
        scoped_documents: list[DocumentRecord] = []
        for document in sorted(documents, key=lambda item: item.document_id):
            items = sorted(
                by_document.get(document.document_id, []),
                key=lambda entry: (
                    entry.question.number_sort if entry.question.number_sort is not None else 10**9,
                    entry.question.question_id,
                ),
            )
            if document.kind is DocumentKind.QUESTION_BANK and items:
                grouped: dict[str, list[_PaperQuestion]] = defaultdict(list)
                for item in items:
                    paper_id = paper_by_question.get(item.question.question_id)
                    if paper_id is not None:
                        grouped[paper_id].append(item)
                if grouped and sum(len(group) for group in grouped.values()) == len(items):
                    for paper_id, group in sorted(grouped.items()):
                        paper_row = paper_rows.get(paper_id)
                        if paper_row is None:
                            continue
                        virtual = _virtual_paper_document(
                            document, paper_row, [item.question for item in group]
                        )
                        period = resolve_exam_period(virtual, course)
                        periods[virtual.document_id] = period
                        for item in group:
                            item.document = virtual
                            item.period = period
                        papers.append(
                            _Paper(
                                document=virtual,
                                period=period,
                                questions=group,
                                signature=_question_signature(group),
                            )
                        )
                        scoped_documents.append(virtual)
                    continue
            scoped_documents.append(document)
            papers.append(
                _Paper(
                    document=document,
                    period=periods[document.document_id],
                    questions=items,
                    signature=_question_signature(items),
                )
            )
        papers.sort(key=lambda paper: (paper.period.sort_key, paper.document.document_id))
        return _Loaded(
            course=course,
            documents=scoped_documents,
            periods=periods,
            papers=papers,
            similarities=similarities,
            question_count=len(questions),
        )

    # -- scope -------------------------------------------------------------

    def _select_papers(
        self, loaded: _Loaded, request: ExamDnaRequest
    ) -> tuple[list[_Paper], list[_Paper]]:
        filters = request.filters
        question_papers = [
            paper for paper in loaded.papers if paper.document.kind is DocumentKind.QUESTION_PAPER
        ]
        self._validate_filters(loaded, question_papers, filters)

        document_ids = set(filters.document_ids)
        source_ids = set(filters.source_ids)
        exam_types = {value.casefold() for value in filters.exam_types}
        selected: list[_Paper] = []
        date_excluded: list[_Paper] = []
        for paper in question_papers:
            document = paper.document
            if document_ids and not _paper_selected(paper, document_ids):
                continue
            if source_ids and document.source_id not in source_ids:
                continue
            period = paper.period
            if exam_types and (period.exam_type or "").casefold() not in exam_types:
                continue
            if not _period_matches(period, filters.exam_periods):
                continue
            if filters.date_from is not None or filters.date_to is not None:
                exact = period.exact_date
                if exact is None:
                    date_excluded.append(paper)
                    continue
                if filters.date_from is not None and exact < filters.date_from:
                    date_excluded.append(paper)
                    continue
                if filters.date_to is not None and exact > filters.date_to:
                    date_excluded.append(paper)
                    continue
            selected.append(paper)
        return selected, date_excluded

    def _validate_filters(self, loaded: _Loaded, papers: list[_Paper], filters: Any) -> None:
        known_documents = {document.document_id for document in loaded.documents}
        known_documents.update(
            str(document.metadata["parent_document_id"])
            for document in loaded.documents
            if document.metadata.get("parent_document_id")
        )
        known_sources = {document.source_id for document in loaded.documents}
        known_types = {
            (paper.period.exam_type or "").casefold() for paper in papers if paper.period.exam_type
        }
        known_periods: set[str] = set()
        for paper in papers:
            period = paper.period
            if not period.known:
                continue
            known_periods.update(
                {
                    period.label.casefold(),
                    period.key.casefold(),
                    (period.session or "").casefold(),
                }
            )
        detail = {
            "unknown_document_ids": sorted(set(filters.document_ids) - known_documents),
            "unknown_source_ids": sorted(set(filters.source_ids) - known_sources),
            "unknown_exam_types": sorted(
                value for value in filters.exam_types if value.casefold() not in known_types
            ),
            "unknown_exam_periods": sorted(
                value for value in filters.exam_periods if value.casefold() not in known_periods
            ),
        }
        if any(detail.values()):
            raise ExamDnaError(
                "invalid_exam_dna_filter",
                "an Exam DNA filter names data outside the selected course",
                detail=detail,
            )

    def _mark_duplicates(self, loaded: _Loaded, candidates: list[_Paper]) -> None:
        selected_ids = {paper.document.document_id for paper in candidates}
        parent: dict[str, str] = {
            paper.document.document_id: paper.document.document_id for paper in candidates
        }

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(first: str, second: str) -> None:
            left, right = find(first), find(second)
            if left != right:
                parent[max(left, right)] = min(left, right)

        by_text: dict[str, list[str]] = defaultdict(list)
        by_signature: dict[str, list[str]] = defaultdict(list)
        for paper in candidates:
            if paper.document.text_fingerprint:
                by_text[paper.document.text_fingerprint].append(paper.document.document_id)
            # A very small paper can legitimately repeat one question in another
            # paper; a full-set signature only identifies a copied paper once
            # there is enough content for the match to be meaningful.
            if (
                paper.signature
                and len(paper.questions) >= self.settings.duplicate_signature_min_questions
            ):
                by_signature[paper.signature].append(paper.document.document_id)
        for duplicate_ids in list(by_text.values()) + list(by_signature.values()):
            if len(duplicate_ids) < 2:
                continue
            first = duplicate_ids[0]
            for other in duplicate_ids[1:]:
                union(first, other)

        components: dict[str, list[_Paper]] = defaultdict(list)
        for paper in candidates:
            components[find(paper.document.document_id)].append(paper)
        canonical_by_document: dict[str, str] = {}
        for duplicate_papers in components.values():
            if len(duplicate_papers) < 2:
                continue
            ordered = sorted(
                duplicate_papers,
                key=lambda item: (item.period.sort_key, item.document.document_id),
            )
            canonical = ordered[0].document.document_id
            for paper in ordered[1:]:
                canonical_by_document[paper.document.document_id] = canonical

        for paper in loaded.papers:
            if paper.document.kind is not DocumentKind.QUESTION_PAPER:
                paper.eligible = False
                paper.exclusion_reason = (
                    PaperExclusionReason.QUESTION_BANK
                    if paper.document.kind is DocumentKind.QUESTION_BANK
                    else None
                )
                paper.duplicate_of = None
                continue
            paper.eligible = False
            paper.duplicate_of = None
            if paper.document.document_id not in selected_ids:
                paper.exclusion_reason = PaperExclusionReason.FILTERED
            elif paper.document.document_id in canonical_by_document:
                paper.exclusion_reason = PaperExclusionReason.DUPLICATE_COPY
                paper.duplicate_of = canonical_by_document[paper.document.document_id]
            elif not paper.questions:
                paper.exclusion_reason = PaperExclusionReason.NO_QUESTIONS
            elif paper.document.extraction_status in _EXCLUDING_EXTRACTION:
                paper.exclusion_reason = PaperExclusionReason.INCOMPLETE_EXTRACTION
            else:
                paper.eligible = True
                paper.exclusion_reason = None

    def _truncate(self, eligible: list[_Paper]) -> tuple[list[_Paper], bool]:
        remaining = self.settings.max_questions
        kept: list[_Paper] = []
        truncated = False
        for paper in eligible:
            if remaining <= 0:
                paper.eligible = False
                paper.exclusion_reason = PaperExclusionReason.FILTERED
                truncated = True
                continue
            if len(paper.questions) > remaining:
                paper.questions = paper.questions[:remaining]
                truncated = True
            remaining -= len(paper.questions)
            kept.append(paper)
        return kept, truncated

    def _coverage(self, request: ExamDnaRequest, eligible: list[_Paper]) -> CoverageReport:
        virtual = any(paper.document.metadata.get("derived_paper") for paper in eligible)
        document_ids = sorted(
            {
                str(paper.document.metadata.get("parent_document_id") or paper.document.document_id)
                for paper in eligible
            }
        )
        coverage_request = CoverageRequest(
            course_id=request.course_id,
            syllabus_id=request.syllabus_id,
            filters=CoverageFilters(
                document_ids=document_ids,
                source_ids=request.filters.source_ids,
                exam_periods=[] if virtual else request.filters.exam_periods,
            ),
        )
        service = CoverageService(self.db, self.coverage_settings)
        try:
            return service.analyze(coverage_request, include_question_assignments=True)
        except CoverageError as exc:
            payload = exc.as_dict()
            raise ExamDnaError(
                payload["code"], payload["message"], detail=payload.get("detail") or {}
            ) from exc

    def _apply_syllabus_scope(self, loaded: _Loaded, syllabus_id: str) -> tuple[int, int]:
        """Keep only unit matches from the selected syllabus version.

        A course can carry several syllabus versions. Phase 3 stores unit matches
        per syllabus, so without this filter unit distributions and unit-based
        patterns would merge different versions (and could conflate units that
        share a label). Questions whose only matches belong to another version are
        reported instead of silently merged.
        """
        foreign_questions = 0
        foreign_documents: set[str] = set()
        for paper in loaded.papers:
            for item in paper.questions:
                if not item.matches:
                    continue
                selected = tuple(
                    match for match in item.matches if match.syllabus_id == syllabus_id
                )
                item.matches = selected
                if selected:
                    continue
                if (
                    paper.eligible
                    and paper.document.kind is DocumentKind.QUESTION_PAPER
                    and not item.foreign_syllabus_only
                ):
                    item.foreign_syllabus_only = True
                    foreign_questions += 1
                    foreign_documents.add(paper.document.document_id)
        return foreign_questions, len(foreign_documents)

    def _attach_unit_assignments(self, loaded: _Loaded, coverage: CoverageReport) -> None:
        """Give every question a deterministic unit for unit distributions.

        Phase 7's complete review table already computes one conservative
        best-fit concept for every question. For unit distributions only, that
        concept supplies the review unit, including questions Phase 3 left
        unmapped. The stored mapping status is not changed; strict Phase 3
        assignments still govern patterns and mapping quality.
        """
        assignments = {item.question_id: item for item in coverage.question_assignments}
        for paper in loaded.papers:
            for item in paper.questions:
                if item.foreign_syllabus_only:
                    continue
                assignment = assignments.get(item.question.question_id)
                if assignment is None or not assignment.unit_ids:
                    continue
                labels = list(assignment.unit_labels)
                matches: list[UnitMatch] = []
                for index, unit_id in enumerate(assignment.unit_ids):
                    label = (
                        labels[index]
                        if index < len(labels)
                        else unit_id
                    )
                    matches.append(
                        UnitMatch(
                            match_id=(
                                f"best_fit_unit:{item.question.question_id}:"
                                f"{unit_id}"
                            ),
                            question_id=item.question.question_id,
                            course_id=loaded.course.course_id,
                            unit_id=unit_id,
                            syllabus_id=coverage.syllabus.syllabus_id,
                            unit_label=label,
                            topic=assignment.concept_label,
                            score=assignment.score or 0.0,
                            rank=index + 1,
                            method="best_fit_unit_assignment",
                            matched_terms=list(assignment.matched_terms),
                            evidence=[
                                *assignment.evidence,
                                "best_fit_review_assignment",
                                f"phase7_source:{assignment.assignment_source}",
                            ],
                        )
                    )
                item.review_matches = tuple(matches)

    def _attach_concepts(self, loaded: _Loaded, coverage: CoverageReport) -> None:
        concepts_by_question: dict[str, set[str]] = defaultdict(set)
        for finding in coverage.concepts:
            for evidence in finding.supporting_questions:
                concepts_by_question[evidence.question_id].add(finding.label)
        for paper in loaded.papers:
            for item in paper.questions:
                item.concepts = set(concepts_by_question.get(item.question.question_id, set()))

    # -- per-paper summaries ----------------------------------------------

    def _paper_summary(self, paper: _Paper) -> ExamDnaPaper:
        questions = paper.questions
        with_marks = [item for item in questions if item.question.marks is not None]
        type_counts = Counter(_type_label(item) for item in questions)
        bloom_counts = Counter(_bloom_label(item) for item in questions)
        difficulty_counts = Counter(_difficulty_label(item) for item in questions)
        unit_counts = Counter(
            match.unit_label for item in questions for match in item.unit_matches
        )
        unit_marks: dict[str, float] = defaultdict(float)
        for item in questions:
            if item.question.marks is None:
                continue
            for unit_label in {match.unit_label for match in item.unit_matches}:
                unit_marks[unit_label] += item.question.marks
        sections = sorted({item.question.section for item in questions if item.question.section})
        verbs = Counter(item.command_verb for item in questions if item.command_verb)
        rules: list[ExamDnaChoiceRule] = []
        seen_rules: set[tuple[str, int | None, str]] = set()
        for item in questions:
            for rule in item.choice_rules:
                key = (rule.kind.value, rule.required_count, rule.evidence)
                if key in seen_rules:
                    continue
                seen_rules.add(key)
                rules.append(rule)
        return ExamDnaPaper(
            document_id=paper.document.document_id,
            title=paper.document.title,
            source_id=paper.document.source_id,
            kind=paper.document.kind,
            exam_period=paper.period.label,
            exam_period_source=paper.period.source,
            exam_session=paper.period.session,
            exam_type=paper.period.exam_type,
            exam_date=paper.period.exact_date,
            extraction_status=paper.document.extraction_status,
            eligible=paper.eligible,
            exclusion_reason=paper.exclusion_reason,
            duplicate_of=paper.duplicate_of,
            question_count=len(questions),
            analysed_questions=sum(1 for item in questions if item.analysis is not None),
            subquestion_count=sum(
                item.analysis.subquestion_count for item in questions if item.analysis is not None
            ),
            questions_with_marks=len(with_marks),
            missing_marks=len(questions) - len(with_marks),
            total_known_marks=_round(sum(item.question.marks or 0.0 for item in with_marks)),
            question_type_counts=dict(sorted(type_counts.items())),
            bloom_counts=dict(sorted(bloom_counts.items())),
            difficulty_counts=dict(sorted(difficulty_counts.items())),
            unit_counts=dict(sorted(unit_counts.items())),
            unit_marks={label: _round(value) for label, value in sorted(unit_marks.items())},
            section_labels=sections,
            command_verb_counts=dict(sorted(verbs.items())),
            choice_rules=rules,
            low_confidence_questions=sum(1 for item in questions if item.low_confidence),
        )

    # -- distributions -----------------------------------------------------

    def _distributions(self, eligible: list[_Paper]) -> ExamDnaDistributions:
        questions = [item for paper in eligible for item in paper.questions]
        total = len(questions)
        known_marks = _round(
            sum(item.question.marks or 0.0 for item in questions if item.question.marks is not None)
        )
        type_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        bloom_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        difficulty_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        unit_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        section_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        verb_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        marks_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        band_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)
        choice_acc: dict[str, _DistributionAcc] = defaultdict(_DistributionAcc)

        for item in questions:
            question_id = item.question.question_id
            paper_id = item.document.document_id
            self._dist_add(type_acc[_type_label(item)], item)
            self._dist_add(bloom_acc[_bloom_label(item)], item)
            self._dist_add(difficulty_acc[_difficulty_label(item)], item)
            for unit_label in {match.unit_label for match in item.unit_matches}:
                self._dist_add(unit_acc[unit_label], item)
            if item.question.section:
                self._dist_add(section_acc[item.question.section], item)
            if item.command_verb:
                self._dist_add(verb_acc[item.command_verb], item)
            if item.question.marks is not None:
                self._dist_add(marks_acc[f"{item.question.marks:g}"], item)
                self._dist_add(band_acc[_marks_band(item.question.marks)], item)
            for rule in item.choice_rules:
                label = (
                    f"{rule.kind.value} (choose {rule.required_count})"
                    if rule.required_count
                    else rule.kind.value
                )
                acc = choice_acc[label]
                acc.questions.add(question_id)
                acc.documents.add(paper_id)
                acc.marks.setdefault(question_id, 0.0)

        def rows(
            acc: dict[str, _DistributionAcc], *, limit: int | None = None
        ) -> list[ExamDnaDistributionRow]:
            result = [
                self._dist_row(label, value, total, known_marks, len(eligible))
                for label, value in acc.items()
            ]
            if limit is None:
                result.sort(key=lambda row: (-row.question_count, row.label))
            else:
                result.sort(key=lambda row: (-row.question_count, row.label))
                result = result[:limit]
            return result

        section_rows = rows(section_acc)
        unit_rows = rows(unit_acc)
        marks_rows = rows(marks_acc)
        marks_rows.sort(key=lambda row: float(row.label) if row.label else 0.0)
        band_order = {
            label: index
            for index, label in enumerate(
                [band for _, band in _MARKS_BANDS] + [_OVERFLOW_MARKS_BAND]
            )
        }
        band_rows = rows(band_acc)
        band_rows.sort(key=lambda row: band_order.get(row.label, 99))
        return ExamDnaDistributions(
            total_questions=total,
            questions_with_marks=sum(1 for item in questions if item.question.marks is not None),
            missing_marks=sum(1 for item in questions if item.question.marks is None),
            total_known_marks=known_marks,
            question_types=rows(type_acc),
            bloom_levels=rows(bloom_acc),
            difficulty=rows(difficulty_acc),
            units=unit_rows,
            sections=section_rows,
            command_verbs=rows(verb_acc),
            marks_values=marks_rows,
            marks_bands=band_rows,
            choice_rules=rows(choice_acc),
        )

    def _dist_add(self, acc: _DistributionAcc, item: _PaperQuestion) -> None:
        question_id = item.question.question_id
        acc.questions.add(question_id)
        acc.documents.add(item.document.document_id)
        if item.question.marks is not None:
            acc.marks.setdefault(question_id, item.question.marks)

    def _dist_row(
        self,
        label: str,
        acc: _DistributionAcc,
        total_questions: int,
        total_marks: float,
        eligible_papers: int,
    ) -> ExamDnaDistributionRow:
        marks = _round(sum(acc.marks.values()))
        return ExamDnaDistributionRow(
            label=label,
            question_count=len(acc.questions),
            papers_observed=len(acc.documents),
            share_of_questions=_rate(len(acc.questions), total_questions),
            share_of_papers=_rate(len(acc.documents), eligible_papers),
            marks=marks,
            questions_with_marks=len(acc.marks),
            share_of_marks=_rate(marks, total_marks) if total_marks else None,
        )

    # -- recurring patterns ------------------------------------------------

    def _patterns(
        self, eligible: list[_Paper], questions: list[_PaperQuestion]
    ) -> tuple[list[ExamDnaPattern], bool]:
        eligible_papers = len(eligible)
        eligible_sessions = self._eligible_sessions(eligible)
        accumulators: dict[tuple[PatternCategory, str], _PatternAcc] = {}

        def add(
            category: PatternCategory,
            key: str,
            paper: _Paper,
            label: str,
            description: str,
            matched: list[_PaperQuestion],
        ) -> None:
            if not matched:
                return
            acc = accumulators.setdefault(
                (category, key),
                _PatternAcc(label=label, description=description),
            )
            acc.documents.add(paper.document.document_id)
            acc.questions.update(item.question.question_id for item in matched)
            acc.occurrences += len(matched)
            if paper.period.session_key:
                acc.sessions.add(paper.period.session_key)
            acc.periods.add(paper.period.label)
            if paper.period.exam_type:
                acc.exam_types.add(paper.period.exam_type)
            if len(acc.evidence) < 20:
                acc.evidence.append(f"{paper.document.title}: {len(matched)} question(s)")

        for paper in eligible:
            variation = paper.questions
            by_type: dict[QuestionType, list[_PaperQuestion]] = defaultdict(list)
            by_bloom: dict[BloomLevel, list[_PaperQuestion]] = defaultdict(list)
            by_verb: dict[str, list[_PaperQuestion]] = defaultdict(list)
            by_unit: dict[str, list[_PaperQuestion]] = defaultdict(list)
            by_marks: dict[float, list[_PaperQuestion]] = defaultdict(list)
            for item in variation:
                if item.primary_type is not None:
                    by_type[item.primary_type].append(item)
                if item.bloom_level is not None:
                    by_bloom[item.bloom_level].append(item)
                if item.command_verb:
                    by_verb[item.command_verb].append(item)
                for unit_label in {match.unit_label for match in item.assigned_matches}:
                    by_unit[unit_label].append(item)
                if item.question.marks is not None:
                    by_marks.setdefault(item.question.marks, []).append(item)

            for question_type, matched in by_type.items():
                add(
                    PatternCategory.QUESTION_TYPE,
                    question_type.value,
                    paper,
                    question_type.value,
                    f"Questions whose primary Phase 3 type is {question_type.value}.",
                    matched,
                )
            for bloom, matched in by_bloom.items():
                add(
                    PatternCategory.BLOOM_LEVEL,
                    bloom.value,
                    paper,
                    bloom.value,
                    f"Questions whose primary Bloom level is {bloom.value}.",
                    matched,
                )
            for verb, matched in by_verb.items():
                add(
                    PatternCategory.COMMAND_VERB,
                    verb,
                    paper,
                    verb,
                    f"Question text opening with the command verb {verb!r}.",
                    matched,
                )
            for unit_label, matched in by_unit.items():
                add(
                    PatternCategory.UNIT_PRESENCE,
                    unit_label,
                    paper,
                    unit_label,
                    f"At least one question is mapped to {unit_label!r}.",
                    matched,
                )
                paper_marks = sum(
                    item.question.marks or 0.0
                    for item in variation
                    if item.question.marks is not None
                )
                unit_marks = sum(
                    item.question.marks or 0.0
                    for item in {entry.question.question_id: entry for entry in matched}.values()
                    if item.question.marks is not None
                )
                if (
                    paper_marks > 0
                    and unit_marks / paper_marks >= self.settings.dominant_unit_marks_share
                ):
                    add(
                        PatternCategory.UNIT_MARKS_SHARE,
                        unit_label,
                        paper,
                        unit_label,
                        (
                            f"{unit_label!r} receives at least "
                            f"{self.settings.dominant_unit_marks_share:.0%} of the paper's "
                            "known marks."
                        ),
                        matched,
                    )
            for value, matched in by_marks.items():
                add(
                    PatternCategory.MARKS_VALUE,
                    f"{value:g}",
                    paper,
                    f"{value:g} marks",
                    f"At least one question states {value:g} marks.",
                    matched,
                )
            types_present = sorted(
                {item.primary_type.value for item in variation if item.primary_type}
            )
            if types_present:
                mix_key = "|".join(types_present)
                add(
                    PatternCategory.QUESTION_TYPE_MIX,
                    mix_key,
                    paper,
                    mix_key.replace("|", "+"),
                    "The paper's set of primary question types.",
                    variation,
                )
            sections = sorted(
                {item.question.section for item in variation if item.question.section}
            )
            if sections:
                section_key = "|".join(sections)
                add(
                    PatternCategory.SECTION_STRUCTURE,
                    section_key,
                    paper,
                    "Sections " + " | ".join(sections),
                    "The paper's sorted set of section labels.",
                    variation,
                )
            rules: dict[str, list[_PaperQuestion]] = defaultdict(list)
            for item in variation:
                for rule in item.choice_rules:
                    key = (
                        f"{rule.kind.value}:{rule.required_count}"
                        if rule.required_count
                        else rule.kind.value
                    )
                    rules[key].append(item)
            for rule_key, matched in rules.items():
                add(
                    PatternCategory.CHOICE_STRUCTURE,
                    rule_key,
                    paper,
                    rule_key.replace(":", " "),
                    "Explicit optional/compulsory instruction wording.",
                    matched,
                )
            concept_types: dict[str, list[_PaperQuestion]] = defaultdict(list)
            for item in variation:
                if item.primary_type is None:
                    continue
                for concept in item.concepts:
                    concept_types[f"{concept}|{item.primary_type.value}"].append(item)
            for key, matched in concept_types.items():
                concept, type_label = key.split("|", 1)
                add(
                    PatternCategory.CONCEPT_TYPE_PAIR,
                    key,
                    paper,
                    f"{concept} / {type_label}",
                    f"Concept {concept!r} assessed with {type_label} questions.",
                    matched,
                )

        patterns: list[ExamDnaPattern] = []
        for (category, key), acc in accumulators.items():
            if (
                category is PatternCategory.CONCEPT_TYPE_PAIR
                and len(acc.documents) < self.settings.min_concept_paper_occurrences
            ):
                continue
            patterns.append(
                self._finish_pattern(
                    category,
                    key,
                    acc,
                    eligible_papers,
                    eligible_sessions,
                )
            )
        patterns.sort(
            key=lambda item: (
                _CATEGORY_ORDER[item.category],
                -(item.recurrence_rate if item.recurrence_rate is not None else -1.0),
                item.key,
            )
        )
        truncated = len(patterns) > self.settings.max_patterns
        if truncated:
            patterns = patterns[: self.settings.max_patterns]
        return patterns, truncated

    def _finish_pattern(
        self,
        category: PatternCategory,
        key: str,
        acc: _PatternAcc,
        eligible_papers: int,
        eligible_sessions: int,
    ) -> ExamDnaPattern:
        observed = len(acc.documents)
        rate = _rate(observed, eligible_papers)
        session_rate = _rate(len(acc.sessions), eligible_sessions) if eligible_sessions else None
        is_recurring = bool(
            observed >= 2
            and rate is not None
            and rate >= self.settings.recurrence_threshold
            and eligible_papers >= self.settings.min_papers_for_recurrence
        )
        if is_recurring:
            quality = EvidenceQuality.HIGH
        elif observed >= 2:
            quality = EvidenceQuality.MEDIUM
        else:
            quality = EvidenceQuality.LOW
        limitations: list[str] = []
        if eligible_papers < self.settings.min_papers_for_recurrence:
            limitations.append(
                f"only {eligible_papers} eligible paper(s) are available; "
                f"{self.settings.min_papers_for_recurrence} are required for a recurring pattern"
            )
        if observed == 1:
            limitations.append("observed in a single paper; this is not cross-paper recurrence")
        if eligible_papers > len(acc.sessions) and eligible_sessions < eligible_papers:
            limitations.append("papers sharing an examination session are not independent sittings")
        if eligible_sessions and eligible_sessions < self.settings.min_papers_for_recurrence:
            limitations.append("session recurrence uses too few independent sessions to be stable")
        evidence = [
            f"Observed in {observed} of {eligible_papers} eligible papers"
            + (f" ({rate:.0%})." if rate is not None else ".")
        ]
        evidence.extend(acc.evidence)
        return ExamDnaPattern(
            pattern_id=make_pattern_id(category, key),
            category=category,
            key=key,
            label=acc.label,
            description=acc.description,
            papers_observed=observed,
            eligible_papers=eligible_papers,
            recurrence_rate=rate,
            denominator_valid=eligible_papers > 0,
            within_paper_occurrences=acc.occurrences,
            sessions_observed=len(acc.sessions),
            eligible_sessions=eligible_sessions,
            session_recurrence_rate=session_rate,
            is_recurring=is_recurring,
            evidence_quality=quality,
            supporting_document_ids=sorted(acc.documents),
            supporting_question_ids=sorted(acc.questions),
            periods=sorted(set(acc.periods)),
            sessions=sorted(acc.sessions),
            exam_types=sorted(acc.exam_types),
            evidence=evidence,
            limitations=limitations,
        )

    def _eligible_sessions(self, eligible: list[_Paper]) -> int:
        return len(
            {paper.period.session_key for paper in eligible if paper.period.session_key is not None}
        )

    # -- dated session series ---------------------------------------------

    def _session_series(
        self, eligible: list[_Paper]
    ) -> tuple[list[ExamDnaSession], list[ExamDnaTrend]]:
        buckets: dict[str, list[_Paper]] = defaultdict(list)
        labels: dict[str, str] = {}
        for paper in eligible:
            period = paper.period
            if period.session_key:
                key = period.session_key
                label = period.session or period.label
            elif period.exact_date is not None:
                key = f"period:{period.label}"
                label = period.label
            else:
                continue
            buckets[key].append(paper)
            labels.setdefault(key, label)
        ordered = sorted(
            buckets,
            key=lambda key: (min(paper.period.sort_key for paper in buckets[key]), key),
        )
        sessions = [self._session_row(key, labels[key], buckets[key]) for key in ordered]
        self._fill_session_shares(sessions)
        return sessions, self._trends(sessions)

    @staticmethod
    def _fill_session_shares(sessions: list[ExamDnaSession]) -> None:
        """Give every session the same share keys.

        A label absent from a session with a valid denominator is a real 0.0%;
        a session without analysed questions or marks keeps no share at all. This
        keeps the series comparable without inventing a denominator.
        """
        dimensions = (
            ("question_type_shares", "analysed_questions"),
            ("bloom_shares", "analysed_questions"),
            ("unit_marks_shares", "known_marks"),
        )
        for attribute, denominator in dimensions:
            labels = sorted(
                {label for session in sessions for label in getattr(session, attribute)}
            )
            for session in sessions:
                if getattr(session, denominator) <= 0:
                    continue
                shares = dict(getattr(session, attribute))
                for label in labels:
                    shares.setdefault(label, 0.0)
                setattr(session, attribute, dict(sorted(shares.items())))

    def _session_row(self, key: str, label: str, papers: list[_Paper]) -> ExamDnaSession:
        questions = [item for paper in papers for item in paper.questions]
        analysed = [item for item in questions if item.analysis is not None]
        with_marks = [item for item in questions if item.question.marks is not None]
        known_marks = _round(sum(item.question.marks or 0.0 for item in with_marks))
        type_counts = Counter(item.primary_type.value for item in analysed if item.primary_type)
        bloom_counts = Counter(item.bloom_level.value for item in analysed if item.bloom_level)
        unit_marks: dict[str, float] = defaultdict(float)
        for item in with_marks:
            for unit_label in {match.unit_label for match in item.assigned_matches}:
                unit_marks[unit_label] += item.question.marks or 0.0
        dates = [paper.period.exact_date for paper in papers if paper.period.exact_date]
        return ExamDnaSession(
            session=key,
            label=label,
            exam_period=papers[0].period.label,
            exam_date=min(dates) if dates else None,
            exam_types=sorted(
                {paper.period.exam_type for paper in papers if paper.period.exam_type}
            ),
            document_ids=[paper.document.document_id for paper in papers],
            question_count=len(questions),
            analysed_questions=len(analysed),
            questions_with_marks=len(with_marks),
            known_marks=known_marks,
            question_type_shares=(
                {
                    value: _round(count / len(analysed))
                    for value, count in sorted(type_counts.items())
                }
                if analysed
                else {}
            ),
            bloom_shares=(
                {
                    value: _round(count / len(analysed))
                    for value, count in sorted(bloom_counts.items())
                }
                if analysed
                else {}
            ),
            unit_marks_shares=(
                {
                    unit_label: _round(marks / known_marks)
                    for unit_label, marks in sorted(unit_marks.items())
                }
                if known_marks
                else {}
            ),
        )

    def _trends(self, sessions: list[ExamDnaSession]) -> list[ExamDnaTrend]:
        if len(sessions) < _MIN_DATED_SESSIONS_FOR_TRENDS:
            return []
        trends: list[ExamDnaTrend] = []
        dimensions = (
            ("question_type", "question_type_shares"),
            ("bloom_level", "bloom_shares"),
            ("unit_marks_share", "unit_marks_shares"),
        )
        for dimension, attribute in dimensions:
            labels = sorted(
                {label for session in sessions for label in getattr(session, attribute)}
            )
            for label in labels:
                values = [getattr(session, attribute).get(label) for session in sessions]
                first = values[0] if values and values[0] is not None else None
                last = values[-1] if values and values[-1] is not None else None
                change = _round(last - first) if first is not None and last is not None else None
                direction: Literal["increased", "decreased", "stable", "unknown"]
                if change is None:
                    direction = "unknown"
                elif change >= _TREND_STABLE_BAND:
                    direction = "increased"
                elif change <= -_TREND_STABLE_BAND:
                    direction = "decreased"
                else:
                    direction = "stable"
                trends.append(
                    ExamDnaTrend(
                        dimension=dimension,
                        key=label,
                        label=label,
                        sessions=[session.session for session in sessions],
                        values=values,
                        first_value=first,
                        last_value=last,
                        change=change,
                        direction=direction,
                        evidence=[
                            f"{session.label}: {value:.0%}"
                            for session, value in zip(sessions, values, strict=True)
                            if value is not None
                        ],
                        limitations=[_TREND_LIMITATION],
                    )
                )
        return trends

    # -- repeated and similar questions -----------------------------------

    def _question_groups(
        self, loaded: _Loaded, eligible: list[_Paper], questions: list[_PaperQuestion]
    ) -> tuple[list[ExamDnaQuestionGroup], dict[str, int], bool]:
        by_id = {item.question.question_id: item for item in questions}
        groups: list[ExamDnaQuestionGroup] = []

        by_text: dict[str, list[_PaperQuestion]] = defaultdict(list)
        for item in questions:
            by_text[normalise_for_exact(item.question.text)].append(item)
        for normalized, members in sorted(by_text.items()):
            if len(members) < 2:
                continue
            groups.append(
                self._exact_group(
                    normalized,
                    sorted(
                        members,
                        key=lambda entry: (
                            entry.period.sort_key,
                            entry.document.document_id,
                            entry.question.question_id,
                        ),
                    ),
                )
            )

        considered = [
            pair
            for pair in loaded.similarities
            if pair.kind in _SIMILARITY_GROUP_KINDS
            and pair.question_id in by_id
            and pair.matched_question_id in by_id
        ]
        reused = [pair for pair in considered if pair.score >= self.settings.similarity_min_score]

        by_kind: dict[SimilarityKind, list[SimilarityMatch]] = defaultdict(list)
        for pair in reused:
            by_kind[pair.kind].append(pair)
        for kind in (SimilarityKind.NEAR, SimilarityKind.SEMANTIC, SimilarityKind.CONCEPT_RELATED):
            pairs = by_kind.get(kind, [])
            if not pairs:
                continue
            groups.extend(self._similarity_groups(kind, pairs, by_id))

        groups.sort(
            key=lambda item: (
                _GROUP_ORDER[item.kind],
                -(item.score if item.score is not None else 0.0),
                item.group_id,
            )
        )
        truncated = len(groups) > self.settings.max_question_groups
        if truncated:
            groups = groups[: self.settings.max_question_groups]
        stats = {
            "considered": len(considered),
            "reused": len(reused),
            "below_threshold": len(considered) - len(reused),
        }
        return groups, stats, truncated

    def _exact_group(self, normalized: str, members: list[_PaperQuestion]) -> ExamDnaQuestionGroup:
        member_models = [self._member(item) for item in members]
        shared_concepts = self._intersection([set(item.concepts) for item in members])
        shared_types = self._intersection(
            [{item.primary_type.value} if item.primary_type else set() for item in members]
        )
        verbs = {item.command_verb for item in members}
        shared_verb = verbs.pop() if len(verbs) == 1 else None
        return ExamDnaQuestionGroup(
            group_id=make_question_group_id(
                QuestionGroupKind.EXACT, [item.question.question_id for item in members]
            ),
            kind=QuestionGroupKind.EXACT,
            method="normalised_exact_text",
            question_count=len(members),
            document_count=len({item.document.document_id for item in members}),
            session_count=len(
                {item.period.session_key for item in members if item.period.session_key}
            ),
            document_ids=sorted({item.document.document_id for item in members}),
            periods=sorted({item.period.label for item in members}),
            sessions=sorted(
                {item.period.session_key for item in members if item.period.session_key}
            ),
            score=1.0,
            min_score=1.0,
            shared_concepts=shared_concepts,
            shared_question_types=shared_types,
            shared_command_verb=shared_verb,
            template_variant=False,
            wording_difference=wording_difference(
                members[0].question.text, members[1].question.text
            ),
            review_required=False,
            evidence_quality=EvidenceQuality.HIGH,
            evidence=[
                f"identical normalised wording ({len(normalized)} characters)",
                f"{len(members)} appearances in "
                f"{len({item.document.document_id for item in members})} paper(s)",
            ],
            members=member_models,
            limitations=[
                "Identical wording does not prove that the question played the same role or "
                "had the same expected answer in each paper."
            ],
        )

    def _similarity_groups(
        self,
        kind: SimilarityKind,
        pairs: list[SimilarityMatch],
        by_id: dict[str, _PaperQuestion],
    ) -> list[ExamDnaQuestionGroup]:
        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(first: str, second: str) -> None:
            left, right = find(first), find(second)
            if left != right:
                parent[max(left, right)] = min(left, right)

        for pair in pairs:
            union(pair.question_id, pair.matched_question_id)

        components: dict[str, list[str]] = defaultdict(list)
        for question_id in list(parent):
            components[find(question_id)].append(question_id)

        result: list[ExamDnaQuestionGroup] = []
        for question_ids in components.values():
            if len(question_ids) < 2:
                continue
            ids = sorted(question_ids)
            members = [by_id[question_id] for question_id in ids if question_id in by_id]
            if len(members) < 2:
                continue
            relevant = [
                pair
                for pair in pairs
                if pair.question_id in ids and pair.matched_question_id in ids
            ]
            scores = [pair.score for pair in relevant]
            shared_concepts = self._intersection([set(item.concepts) for item in members])
            shared_types = self._intersection(
                [{item.primary_type.value} if item.primary_type else set() for item in members]
            )
            verbs = {item.command_verb for item in members}
            shared_verb = verbs.pop() if len(verbs) == 1 else None
            group_kind = _SIMILARITY_GROUP_KINDS[kind]
            template = bool(
                shared_concepts
                and shared_types
                and shared_verb
                and group_kind is not QuestionGroupKind.EXACT
            )
            score = _round(max(scores)) if scores else None
            result.append(
                ExamDnaQuestionGroup(
                    group_id=make_question_group_id(group_kind, ids),
                    kind=group_kind,
                    method="+".join(sorted({pair.method for pair in relevant})),
                    question_count=len(members),
                    document_count=len({item.document.document_id for item in members}),
                    session_count=len(
                        {item.period.session_key for item in members if item.period.session_key}
                    ),
                    document_ids=sorted({item.document.document_id for item in members}),
                    periods=sorted({item.period.label for item in members}),
                    sessions=sorted(
                        {item.period.session_key for item in members if item.period.session_key}
                    ),
                    score=score,
                    min_score=_round(min(scores)) if scores else None,
                    shared_concepts=shared_concepts,
                    shared_question_types=shared_types,
                    shared_command_verb=shared_verb,
                    template_variant=template,
                    wording_difference=wording_difference(
                        members[0].question.text, members[1].question.text
                    ),
                    review_required=True,
                    evidence_quality=self._similarity_quality(kind, score),
                    evidence=[
                        f"{len(relevant)} stored Phase 3 pair(s)",
                        f"similarity kind={kind.value}",
                        f"best score={score}",
                    ],
                    members=[self._member(item) for item in members],
                    limitations=self._similarity_limitations(kind, members, shared_concepts),
                )
            )
        return result

    def _similarity_quality(self, kind: SimilarityKind, score: float | None) -> EvidenceQuality:
        if kind is SimilarityKind.NEAR:
            return EvidenceQuality.HIGH if (score or 0.0) >= 0.9 else EvidenceQuality.MEDIUM
        if kind is SimilarityKind.SEMANTIC:
            return EvidenceQuality.MEDIUM if (score or 0.0) >= 0.68 else EvidenceQuality.LOW
        return EvidenceQuality.LOW

    def _similarity_limitations(
        self,
        kind: SimilarityKind,
        members: list[_PaperQuestion],
        shared_concepts: list[str],
    ) -> list[str]:
        limitations = [
            "A stored similarity score is evidence of related wording or concept, not proof "
            "that the questions are equivalent or share an expected answer."
        ]
        if any(
            item.document.document_id == members[0].document.document_id for item in members[1:]
        ):
            limitations.append(
                "some appearances are within one paper; that does not establish "
                "cross-paper repetition"
            )
        if not shared_concepts:
            limitations.append("no accepted concept mapping is shared by every group member")
        if kind is SimilarityKind.CONCEPT_RELATED:
            limitations.append(
                "concept-related questions may test the same concept with different demands"
            )
        return limitations

    def _member(self, item: _PaperQuestion) -> QuestionGroupMember:
        return QuestionGroupMember(
            question_id=item.question.question_id,
            document_id=item.document.document_id,
            document_title=item.document.title,
            question_number=item.question.question_number,
            section=item.question.section,
            page_number=item.question.page_number,
            marks=item.question.marks,
            text_excerpt=excerpt(item.question.text),
            exam_period=item.period.label,
            exam_session=item.period.session,
            exam_type=item.period.exam_type,
            primary_type=item.primary_type,
            bloom_level=item.bloom_level,
            difficulty=item.difficulty,
            command_verb=item.command_verb,
            concepts=sorted(item.concepts),
        )

    @staticmethod
    def _intersection(values: list[set[str]]) -> list[str]:
        if not values:
            return []
        shared = set.intersection(*values) if all(values) else set()
        return sorted(shared)

    # -- quality, status and warnings -------------------------------------

    def _data_quality(
        self,
        loaded: _Loaded,
        eligible: list[_Paper],
        questions: list[_PaperQuestion],
        coverage: CoverageReport,
        similarity_stats: dict[str, int],
        *,
        truncated: bool,
        patterns_truncated: bool,
        groups_truncated: bool,
        other_syllabus_questions: int,
        other_syllabus_papers: int,
    ) -> ExamDnaDataQuality:
        total_questions = len(questions)
        analysed = [item for item in questions if item.analysis is not None]
        with_marks = [item for item in questions if item.question.marks is not None]
        classified = [item for item in questions if item.primary_type is not None]
        bloom_classified = [item for item in questions if item.bloom_level is not None]
        low_confidence = [item for item in questions if item.low_confidence]
        low_structure = [
            item
            for item in questions
            if item.analysis is not None
            and item.analysis.structure_confidence < self.settings.low_confidence_threshold
        ]
        with_subparts = [
            item
            for item in questions
            if item.analysis is not None and item.analysis.subquestion_count > 0
        ]
        sessions = {paper.period.session_key for paper in eligible if paper.period.session_key}
        paper_dates = sum(1 for paper in eligible if paper.period.exact_date is not None)

        exclusions: list[PaperExclusion] = []
        question_banks = [
            paper for paper in loaded.papers if paper.document.kind is DocumentKind.QUESTION_BANK
        ]
        for paper in loaded.papers:
            if paper.document.kind is not DocumentKind.QUESTION_PAPER:
                continue
            if paper.eligible:
                continue
            if paper.exclusion_reason is None:
                continue
            exclusions.append(
                PaperExclusion(
                    document_id=paper.document.document_id,
                    title=paper.document.title,
                    reason=paper.exclusion_reason,
                    duplicate_of=paper.duplicate_of,
                    question_count=len(paper.questions),
                )
            )
        duplicate_groups = {
            paper.duplicate_of
            for paper in loaded.papers
            if paper.exclusion_reason is PaperExclusionReason.DUPLICATE_COPY and paper.duplicate_of
        }
        other_documents = sum(
            1
            for paper in loaded.papers
            if paper.document.kind not in {DocumentKind.QUESTION_PAPER, DocumentKind.QUESTION_BANK}
        )
        return ExamDnaDataQuality(
            question_papers=len(
                [
                    paper
                    for paper in loaded.papers
                    if paper.document.kind is DocumentKind.QUESTION_PAPER
                ]
            ),
            eligible_papers=len(eligible),
            analysed_papers=sum(
                1 for paper in eligible if any(item.analysis for item in paper.questions)
            ),
            excluded_papers=len(
                [
                    paper
                    for paper in loaded.papers
                    if paper.document.kind is DocumentKind.QUESTION_PAPER and not paper.eligible
                ]
            ),
            duplicate_paper_groups=len(duplicate_groups),
            duplicate_copies_excluded=sum(
                1
                for paper in loaded.papers
                if paper.exclusion_reason is PaperExclusionReason.DUPLICATE_COPY
            ),
            question_banks_excluded=len(question_banks),
            other_documents=other_documents,
            questions=total_questions,
            analysed_questions=len(analysed),
            analysis_coverage_rate=_rate(len(analysed), total_questions),
            questions_with_marks=len(with_marks),
            missing_marks=total_questions - len(with_marks),
            marks_coverage_rate=_rate(len(with_marks), total_questions),
            classified_questions=len(classified),
            unclassified_questions=len(analysed) - len(classified),
            bloom_classified_questions=len(bloom_classified),
            low_confidence_questions=len(low_confidence),
            low_structure_confidence_questions=len(low_structure),
            questions_with_subparts=len(with_subparts),
            uncertain_concept_mappings=coverage.summary.uncertain_concepts,
            questions_with_session=sum(
                1 for item in questions if item.period.session_key is not None
            ),
            questions_without_session=sum(
                1 for item in questions if item.period.session_key is None
            ),
            known_sessions=len(sessions),
            papers_with_dates=paper_dates,
            papers_without_dates=len(eligible) - paper_dates,
            exam_types=sorted(
                {paper.period.exam_type for paper in eligible if paper.period.exam_type}
            ),
            papers_with_sections=sum(
                1 for paper in eligible if any(item.question.section for item in paper.questions)
            ),
            papers_without_sections=sum(
                1
                for paper in eligible
                if not any(item.question.section for item in paper.questions)
            ),
            choice_rules_detected=sum(len(item.choice_rules) for item in questions),
            similarity_pairs_considered=similarity_stats["considered"],
            similarity_pairs_reused=similarity_stats["reused"],
            similarity_pairs_below_threshold=similarity_stats["below_threshold"],
            truncated=truncated,
            patterns_truncated=patterns_truncated,
            question_groups_truncated=groups_truncated,
            small_sample=len(eligible) < self.settings.min_papers_for_recurrence,
            questions_mapped_to_other_syllabus_versions=other_syllabus_questions,
            papers_mapped_to_other_syllabus_versions=other_syllabus_papers,
            exclusions=exclusions,
        )

    def _status(
        self,
        eligible: list[_Paper],
        questions: list[_PaperQuestion],
        quality: ExamDnaDataQuality,
    ) -> ExamDnaStatus:
        if not eligible or not questions:
            return ExamDnaStatus.INSUFFICIENT_DATA
        if quality.analysed_questions == 0:
            return ExamDnaStatus.INSUFFICIENT_DATA
        partial = (
            quality.eligible_papers < self.settings.min_papers_for_recurrence
            or quality.duplicate_copies_excluded > 0
            or quality.questions_without_session > 0
            or (quality.analysis_coverage_rate or 0.0) < 1.0
            or (quality.marks_coverage_rate or 0.0) < 1.0
            or quality.low_confidence_questions > 0
            or quality.unclassified_questions > 0
            or quality.low_structure_confidence_questions > 0
            or quality.questions_mapped_to_other_syllabus_versions > 0
            or 0 < quality.papers_with_sections < quality.eligible_papers
            or len(quality.exam_types) > 1
            or quality.truncated
            or any(
                paper.document.extraction_status is ExtractionStatus.PARTIAL for paper in eligible
            )
        )
        return ExamDnaStatus.PARTIAL if partial else ExamDnaStatus.OK

    def _warnings(
        self,
        eligible: list[_Paper],
        questions: list[_PaperQuestion],
        quality: ExamDnaDataQuality,
        coverage: CoverageReport,
        date_excluded: list[_Paper],
    ) -> list[Uncertainty]:
        warnings: list[Uncertainty] = []
        if not eligible:
            warnings.append(
                _warning(
                    "exam_dna_no_questions",
                    "no eligible question papers remain; a historical profile cannot be built",
                )
            )
        elif len(eligible) < self.settings.min_papers_for_recurrence:
            warnings.append(
                _warning(
                    "exam_dna_small_sample",
                    (
                        f"{len(eligible)} eligible paper(s) are available; "
                        f"{self.settings.min_papers_for_recurrence} are required before calling a "
                        "pattern recurring"
                    ),
                )
            )
        if quality.duplicate_copies_excluded:
            warnings.append(
                _warning(
                    "exam_dna_duplicate_paper",
                    (
                        f"{quality.duplicate_copies_excluded} duplicate paper cop(ies) were "
                        "excluded from independent evidence"
                    ),
                    severity=Severity.INFO,
                )
            )
        if quality.questions_without_session:
            warnings.append(
                _warning(
                    "exam_dna_missing_dates",
                    (
                        f"{quality.questions_without_session} question(s) have no explicit "
                        "examination session; session-level recurrence excludes them"
                    ),
                    severity=Severity.INFO,
                )
            )
        if len(quality.exam_types) > 1:
            warnings.append(
                _warning(
                    "exam_dna_mixed_exam_types",
                    "papers of different examination types are included: "
                    + ", ".join(quality.exam_types),
                )
            )
        marks_totals = {
            _round(sum(item.question.marks or 0.0 for item in paper.questions))
            for paper in eligible
            if paper.questions and all(item.question.marks is not None for item in paper.questions)
        }
        if len(marks_totals) > 1:
            warnings.append(
                _warning(
                    "exam_dna_different_marks_totals",
                    "papers state different total marks; raw marks are reported without "
                    "normalising across papers",
                    severity=Severity.INFO,
                )
            )
        if quality.missing_marks:
            warnings.append(
                _warning(
                    "exam_dna_missing_marks",
                    f"{quality.missing_marks} question(s) have no stated marks; "
                    "mark shares exclude them",
                    severity=Severity.INFO,
                )
            )
        if quality.analysed_questions < quality.questions:
            warnings.append(
                _warning(
                    "exam_dna_missing_analysis",
                    (
                        f"{quality.questions - quality.analysed_questions} question(s) have no "
                        "Phase 3 analysis; type, Bloom and mapping patterns are incomplete"
                    ),
                )
            )
        if quality.low_confidence_questions:
            warnings.append(
                _warning(
                    "exam_dna_low_confidence",
                    (
                        f"{quality.low_confidence_questions} question classification(s) are "
                        "below the confidence threshold"
                    ),
                )
            )
        if quality.unclassified_questions:
            warnings.append(
                _warning(
                    "exam_dna_missing_classification",
                    (
                        f"{quality.unclassified_questions} analysed question(s) have no primary "
                        "question type; type patterns exclude them"
                    ),
                    severity=Severity.INFO,
                )
            )
        if quality.low_structure_confidence_questions:
            warnings.append(
                _warning(
                    "exam_dna_uncertain_segmentation",
                    (
                        f"{quality.low_structure_confidence_questions} question(s) have "
                        "low-confidence structure; sub-question relationships may be incomplete"
                    ),
                )
            )
        if quality.questions_mapped_to_other_syllabus_versions:
            warnings.append(
                _warning(
                    "exam_dna_mixed_syllabus_versions",
                    (
                        f"{quality.questions_mapped_to_other_syllabus_versions} question(s) in "
                        f"{quality.papers_mapped_to_other_syllabus_versions} paper(s) are only "
                        "mapped to a different syllabus version; those unit mappings were "
                        "excluded instead of being merged into the selected version"
                    ),
                )
            )
        if quality.uncertain_concept_mappings:
            warnings.append(
                _warning(
                    "exam_dna_uncertain_mapping",
                    (
                        f"{quality.uncertain_concept_mappings} syllabus concept(s) have uncertain "
                        "Phase 7 coverage evidence; concept-based observations inherit that doubt"
                    ),
                    severity=Severity.INFO,
                )
            )
        if any(paper.document.extraction_status is ExtractionStatus.PARTIAL for paper in eligible):
            warnings.append(
                _warning(
                    "exam_dna_partial_extraction",
                    "at least one paper's extraction was partial; its question set may be "
                    "incomplete",
                )
            )
        if quality.papers_without_sections:
            warnings.append(
                _warning(
                    "exam_dna_no_sections",
                    (
                        f"{quality.papers_without_sections} paper(s) have no reliable section "
                        "metadata; section and internal-choice patterns are unavailable for them"
                    ),
                    severity=Severity.INFO,
                )
            )
        ambiguous_choice = sum(
            1
            for item in questions
            if not item.choice_rules
            and any(hint in item.question.text.casefold() for hint in _AMBIGUOUS_CHOICE_RE_HINT)
            and "question" in item.question.text.casefold()
        )
        if ambiguous_choice:
            warnings.append(
                _warning(
                    "exam_dna_choice_uncertain",
                    (
                        f"{ambiguous_choice} question(s) mention 'any'/'either' without an "
                        "explicit choice rule; no optional-question structure was inferred"
                    ),
                    severity=Severity.INFO,
                )
            )
        if quality.similarity_pairs_below_threshold and not quality.similarity_pairs_reused:
            warnings.append(
                _warning(
                    "exam_dna_no_similarity",
                    (
                        f"{quality.similarity_pairs_below_threshold} stored Phase 3 pair(s) are "
                        "below the configured similarity threshold; only exact repetition "
                        "is available"
                    ),
                    severity=Severity.INFO,
                )
            )
        if (
            coverage.data_quality.graph_concepts_matched == 0
            and coverage.data_quality.graph_assessment_edges == 0
        ):
            warnings.append(
                _warning(
                    "exam_dna_graph_unavailable",
                    "no matching Phase 5 concept evidence was found; concept identity comes from "
                    "the Phase 7 syllabus coverage report only",
                    severity=Severity.INFO,
                )
            )
        if date_excluded:
            warnings.append(
                _warning(
                    "exam_dna_missing_dates",
                    (
                        f"{len(date_excluded)} paper(s) could not be placed inside the "
                        "requested date range because no explicit examination date is available"
                    ),
                )
            )
        if quality.truncated:
            warnings.append(
                _warning(
                    "exam_dna_truncated",
                    (
                        f"the selected corpus exceeded {self.settings.max_questions} questions "
                        "and was truncated (EXAM_ENGINE_Q8_MAX_QUESTIONS)"
                    ),
                )
            )
        if quality.patterns_truncated:
            warnings.append(
                _warning(
                    "exam_dna_truncated",
                    (
                        f"patterns were capped at {self.settings.max_patterns} "
                        "(EXAM_ENGINE_Q8_MAX_PATTERNS)"
                    ),
                    severity=Severity.INFO,
                )
            )
        if quality.question_groups_truncated:
            warnings.append(
                _warning(
                    "exam_dna_truncated",
                    (
                        f"question groups were capped at {self.settings.max_question_groups} "
                        "(EXAM_ENGINE_Q8_MAX_GROUPS)"
                    ),
                    severity=Severity.INFO,
                )
            )
        return _dedupe_warnings(warnings)

    def _summary(
        self,
        eligible: list[_Paper],
        questions: list[_PaperQuestion],
        patterns: list[ExamDnaPattern],
        groups: list[ExamDnaQuestionGroup],
        distributions: ExamDnaDistributions,
        quality: ExamDnaDataQuality,
        *,
        sessions: list[ExamDnaSession],
        trends: list[ExamDnaTrend],
    ) -> ExamDnaSummary:
        return ExamDnaSummary(
            total_papers=quality.question_papers,
            eligible_papers=len(eligible),
            excluded_papers=quality.excluded_papers,
            questions=len(questions),
            analysed_questions=sum(1 for item in questions if item.analysis is not None),
            questions_with_marks=distributions.questions_with_marks,
            total_known_marks=distributions.total_known_marks,
            known_sessions=self._eligible_sessions(eligible),
            pattern_count=len(patterns),
            recurring_pattern_count=sum(1 for pattern in patterns if pattern.is_recurring),
            exact_question_groups=sum(
                1 for group in groups if group.kind is QuestionGroupKind.EXACT
            ),
            similar_question_groups=sum(
                1 for group in groups if group.kind is not QuestionGroupKind.EXACT
            ),
            units_observed=len(distributions.units),
            concepts_observed=len({concept for item in questions for concept in item.concepts}),
            dated_sessions=len(sessions),
            trend_count=len(trends),
        )

    def _coverage_summary(self, report: CoverageReport) -> ExamDnaCoverage:
        return ExamDnaCoverage(
            report_id=report.report_id,
            status=report.status,
            total_concepts=report.summary.total_concepts,
            covered_concepts=report.summary.covered_concepts,
            low_coverage_concepts=report.summary.low_coverage_concepts,
            unmapped_concepts=report.summary.unmapped_concepts,
            uncertain_concepts=report.summary.uncertain_concepts,
            insufficient_data_concepts=report.summary.insufficient_data_concepts,
            concept_coverage_rate=report.summary.concept_coverage_rate,
            mapped_question_rate=report.summary.mapped_question_rate,
            total_questions=report.summary.total_questions,
            mapped_marks=report.summary.mapped_marks,
            total_known_marks=report.summary.total_known_marks,
            graph_available=(
                report.data_quality.graph_concepts_matched > 0
                or report.data_quality.graph_assessment_edges > 0
            ),
            concepts=[
                ExamDnaConceptRef(
                    concept_id=finding.concept_id,
                    label=finding.label,
                    status=finding.status,
                    unit_labels=list(finding.unit_labels),
                    distinct_question_count=finding.distinct_question_count,
                )
                for finding in report.concepts
            ],
        )

    def _scope(
        self,
        request: ExamDnaRequest,
        candidates: list[_Paper],
        date_excluded: list[_Paper],
        eligible: list[_Paper],
        coverage: CoverageReport,
    ) -> ExamDnaScope:
        periods = sorted({paper.period.label for paper in eligible})
        sessions = sorted(
            {paper.period.session_key for paper in eligible if paper.period.session_key}
        )
        exam_types = sorted(
            {paper.period.exam_type for paper in eligible if paper.period.exam_type}
        )
        statement = (
            f"{len(eligible)} of {len(candidates) + len(date_excluded)} question paper(s) for "
            f"course {request.course_id}, syllabus {coverage.syllabus.syllabus_id}"
        )
        if exam_types:
            statement += ", exam types " + ", ".join(exam_types)
        if periods:
            statement += ", periods " + ", ".join(periods)
        if request.filters.restricts:
            statement += ", filtered"
        return ExamDnaScope(
            document_ids=[paper.document.document_id for paper in eligible],
            exam_types=exam_types,
            periods=periods,
            sessions=sessions,
            date_from=request.filters.date_from,
            date_to=request.filters.date_to,
            scope_statement=statement,
        )

    def _profile_id(
        self,
        request: ExamDnaRequest,
        eligible: list[_Paper],
        questions: list[_PaperQuestion],
        patterns: list[ExamDnaPattern],
        groups: list[ExamDnaQuestionGroup],
        coverage: CoverageReport,
        parameters: dict[str, Any],
        status: ExamDnaStatus,
    ) -> str:
        payload = "\x1e".join(
            [
                request.course_id,
                coverage.syllabus.syllabus_id,
                request.filters.model_dump_json(),
                str(sorted(parameters.items())),
                status.value,
                ",".join(f"{paper.document.document_id}:{paper.signature}" for paper in eligible),
                ",".join(sorted(item.question.question_id for item in questions)),
                ",".join(f"{pattern.pattern_id}:{pattern.recurrence_rate}" for pattern in patterns),
                ",".join(f"{group.group_id}:{group.score}" for group in groups),
                coverage.report_id,
            ]
        )
        return make_profile_id(payload)

    def _limitations(self, quality: ExamDnaDataQuality) -> list[str]:
        limitations = [
            "This profile describes the available question papers only. It is not a forecast, "
            "and no historical frequency is converted into a probability for a future examination.",
            "Question-type, Bloom-level, difficulty and concept labels are Phase 3/Phase 7 "
            "estimates with confidence and uncertainty; they are not independently validated "
            "educational measurements.",
            "A recurrence rate is papers_observed / eligible_papers; papers sharing an "
            "examination session are separate papers but not independent sittings.",
            "Duplicate copies of the same paper are excluded from independent evidence and "
            "reported in data_quality.exclusions.",
            "Marks are used only when the source states them. Missing marks are counted, never "
            "inferred, and marks are not split between the concepts a question supports.",
            "Section and optional/compulsory patterns come from stored section labels and "
            "explicit wording cues only; ambiguous extraction is not interpreted.",
            "Exact repetition proves identical wording, not identical expected answers. "
            "Near/semantic/concept-related groups are review candidates and remain distinct "
            "categories.",
            "Question banks and other documents are not counted as examination papers.",
            "No labelled educational benchmark is bundled, so no accuracy, generalisation or "
            "predictive claim is made from these synthetic fixture results.",
            "Session series and descriptive changes use only papers with an explicit session or "
            "examination date; they describe the observed papers and are not projections.",
        ]
        if quality.small_sample:
            limitations.append(
                "The collection is smaller than the configured recurrence minimum, so most "
                "patterns are observations rather than established recurrence."
            )
        if quality.duplicate_copies_excluded:
            limitations.append(
                "After excluding duplicate copies, the effective paper count is lower than the "
                "number of stored documents."
            )
        if len(quality.exam_types) > 1:
            limitations.append(
                "The selected papers mix examination types; cross-type comparison is descriptive "
                "and may not be equivalent."
            )
        if quality.questions_mapped_to_other_syllabus_versions:
            limitations.append(
                "Some questions are mapped to a different syllabus version; their unit evidence "
                "is excluded rather than merged into the selected version, so unit distributions "
                "and unit patterns under-count those papers."
            )
        return limitations


__all__ = ["ExamDnaError", "ExamDnaService"]
