"""Read-only Phase 4 analytics over stored Phase 1–3 records.

The engine deliberately has no dashboard, no new derived table and no inference
step that could become stale. Each call reads the current questions, mappings,
classifications, similarities and provenance, applies one explicit filter set,
and returns a validated report. Course boundaries are always preserved: a
multi-course CLI request produces separate reports rather than pooled totals.

Core metric rules:

* ``total_questions`` is the number of stored question appearances after filters.
* ``distinct_question_texts`` uses the same normalisation as exact repetition
  (case/whitespace, leading question marker and trailing marks annotation).
* Mapping/topic shares use ``total_questions`` as denominator; a question may
  contribute to several unit/topic buckets, so bucket counts need not add up.
* Marks use only stated marks. Missing marks are counted, never replaced by 0.
* Type/difficulty distributions use the Phase 3 primary label; the multi-label
  assignment counts are reported separately.
* Trends use explicit examination metadata or academic year. Upload time is not
  an examination date, and no missing period is interpolated.
"""

from __future__ import annotations

import hashlib
import logging
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from exam_engine.analytics.metadata import ExamPeriod, resolve_exam_period
from exam_engine.config import AnalyticsSettings
from exam_engine.models.analytics import (
    AnalyticsFilters,
    AnalyticsReport,
    ConceptFrequencyRow,
    CourseScope,
    DataQualityReport,
    DifficultyReport,
    DocumentFrequencyRow,
    ExactDuplicateGroup,
    LabelCountRow,
    MappingFrequencyReport,
    MarksAllocationRow,
    MarksAnomaly,
    MarksBreakdownRow,
    MarksReport,
    NewTopic,
    QuestionMarksRow,
    QuestionReference,
    QuestionTypeReport,
    RepetitionReport,
    SimilarityCoverageRow,
    SimilarityPair,
    TopicFrequencyRow,
    TopicPriorityReport,
    TopicPriorityRow,
    TrendChange,
    TrendPeriodSummary,
    TrendReport,
    UnitFrequencyRow,
)
from exam_engine.models.course import CourseRecord
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    DifficultyLabel,
    DocumentKind,
    MappingStatus,
    QuestionType,
    Severity,
    SimilarityKind,
    TrendStatus,
)
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import SyllabusUnitRecord
from exam_engine.models.understanding import (
    DifficultyAssessment,
    QuestionAnalysis,
    SimilarityMatch,
    TypeLabel,
    UnitMatch,
)
from exam_engine.nlp.concept_quality import concept_noise_reason
from exam_engine.storage.db import Database
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from exam_engine.storage.schema import ExamPaperRow, ExamQuestionDetailRow
from exam_engine.understanding.classification import difficulty_band
from exam_engine.understanding.similarity import normalise_question_text

logger = logging.getLogger(__name__)

_TYPE_ORDER = {kind: index for index, kind in enumerate(SimilarityKind)}
_DIFFICULTY_ORDER = {
    DifficultyLabel.EASY: 0,
    DifficultyLabel.MODERATE: 1,
    DifficultyLabel.HARD: 2,
    DifficultyLabel.UNKNOWN: 3,
}
_SPECIAL_UNIT_LABELS = {"unmapped", "not analysed", "unassigned", "unknown"}
_MARKS_ANOMALY_CODES = frozenset({"multiple_marks_values", "invalid_marks_value"})
_TRAILING_MARKS_RE = re.compile(
    r"\s*(?:\[|\(|\{)?\s*\d+(?:\.\d+)?\s*marks?\s*(?:\]|\)|\})?\s*$",
    re.IGNORECASE,
)


def _exact_question_text(text: str) -> str:
    """Normalised wording used for exact repetition.

    Phase 3 already ignores a leading question marker. A trailing marks
    annotation is presentation metadata, not wording, so it is stripped too;
    the exact group still retains each appearance's original text and marks.
    """
    return _TRAILING_MARKS_RE.sub("", normalise_question_text(text)).strip()


def _exact_fingerprint(text: str) -> str:
    return hashlib.sha256(_exact_question_text(text).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class _QuestionContext:
    question: QuestionRecord
    document: DocumentRecord
    period: ExamPeriod
    analysis: QuestionAnalysis | None
    matches: tuple[UnitMatch, ...] = ()
    types: tuple[TypeLabel, ...] = ()
    difficulty: DifficultyAssessment | None = None

    @property
    def exact_key(self) -> str:
        return _exact_question_text(self.question.text)

    @property
    def fingerprint(self) -> str:
        return _exact_fingerprint(self.question.text)

    @property
    def assigned_matches(self) -> tuple[UnitMatch, ...]:
        """Only candidates the Phase 3 mapping actually selected.

        ``question_unit_matches`` retains ranked evidence, including runner-up
        candidates for a confidently mapped question. Those are useful for review
        but are not assignments. A mapped question has one selected candidate; an
        ambiguous question keeps every selected candidate; an unmapped question
        has no assignment.
        """
        if self.analysis is None:
            return ()
        if self.analysis.mapping_status is MappingStatus.MAPPED:
            return self.matches[:1]
        if self.analysis.mapping_status is MappingStatus.AMBIGUOUS:
            return self.matches
        return ()

    @property
    def primary_match(self) -> UnitMatch | None:
        assigned = self.assigned_matches
        return assigned[0] if assigned else None

    @property
    def primary_type(self) -> QuestionType | None:
        return self.analysis.primary_type if self.analysis else None

    @property
    def primary_difficulty(self) -> DifficultyLabel | None:
        return self.analysis.difficulty_label if self.analysis else None

    @property
    def unit_label(self) -> str:
        if self.analysis is None:
            return "not analysed"
        if self.primary_match is None:
            return "unmapped"
        return self.primary_match.unit_label

    @property
    def topic_label(self) -> str:
        if self.analysis is None:
            return "not analysed"
        if self.primary_match is None:
            return "unmapped"
        topic = self.primary_match.topic
        if not topic or concept_noise_reason(topic) is not None:
            return "unassigned"
        return topic

    @property
    def type_label(self) -> str:
        if self.analysis is None:
            return "not analysed"
        return self.primary_type.value if self.primary_type else "unclassified"

    @property
    def difficulty_label(self) -> str:
        if self.analysis is None:
            return "not analysed"
        return self.primary_difficulty.value if self.primary_difficulty else "unknown"


@dataclass
class _CourseData:
    course: CourseRecord
    documents: list[DocumentRecord]
    papers: int
    paper_by_question: dict[str, tuple[str, int | None]]
    questions: list[QuestionRecord]
    analyses: dict[str, QuestionAnalysis]
    matches: dict[str, list[UnitMatch]]
    types: dict[str, list[TypeLabel]]
    difficulties: dict[str, DifficultyAssessment]
    similarities: list[SimilarityMatch]
    units: list[tuple[str, SyllabusUnitRecord]]
    periods: dict[str, ExamPeriod]


@dataclass
class _TopicStat:
    labels: Counter[str]
    unit_ids: set[str] = field(default_factory=set)
    questions: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)
    papers: set[str] = field(default_factory=set)
    sessions: set[str] = field(default_factory=set)
    periods: set[str] = field(default_factory=set)
    marks: list[float] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)


def _round(value: float) -> float:
    return round(float(value), 4)


def _share(count: int, total: int) -> float:
    return _round(count / total) if total else 0.0


def _normalise_topic(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _dedupe_warnings(warnings: list[Uncertainty]) -> list[Uncertainty]:
    seen: set[tuple[str, str]] = set()
    result: list[Uncertainty] = []
    for warning in warnings:
        key = (warning.code, warning.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return result


def _warning(
    code: str,
    message: str,
    *,
    severity: Severity = Severity.WARNING,
    detail: dict[str, Any] | None = None,
) -> Uncertainty:
    return Uncertainty(
        code=code,
        message=message,
        severity=severity,
        detail=detail or {},
    )


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


def _matches_period(period: ExamPeriod, wanted: list[str]) -> bool:
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


def _share_of_marks(value: float, total: float) -> float | None:
    return _round(value / total) if total > 0 else None


class AnalyticsService:
    """Build deterministic analytics reports from stored records."""

    def _resolved_difficulty_from_analysis(
        self, analysis: QuestionAnalysis | None
    ) -> DifficultyLabel | None:
        """Resolve a stored UNKNOWN through its already-computed score.

        Older Phase 3 rows were produced before the classifier stopped treating
        missing type/Bloom cues as a reason to discard a valid text score. This
        read-time fallback keeps those rows usable without rewriting the store.
        """
        if analysis is None:
            return None
        if analysis.difficulty_label is not DifficultyLabel.UNKNOWN:
            return analysis.difficulty_label
        if analysis.word_count <= 0:
            return DifficultyLabel.UNKNOWN
        return difficulty_band(
            analysis.difficulty_score,
            easy_below=self.settings.difficulty_easy_below,
            hard_above=self.settings.difficulty_hard_above,
        )

    def _resolved_difficulty(self, item: _QuestionContext) -> DifficultyLabel | None:
        return self._resolved_difficulty_from_analysis(item.analysis)

    def _difficulty_label(self, item: _QuestionContext) -> str:
        label = self._resolved_difficulty(item)
        return label.value if label is not None else "unknown"

    def __init__(self, database: Database, settings: AnalyticsSettings | None = None) -> None:
        self.db = database
        self.settings = settings or AnalyticsSettings()

    def analyze(
        self,
        course_id: str | None = None,
        filters: AnalyticsFilters | None = None,
    ) -> list[AnalyticsReport]:
        """Analyse one course, or every course matching the scope filters.

        Multiple courses are always returned as separate reports. Pooling
        question counts across different syllabi or grading schemes is not done.
        """
        filters = filters or AnalyticsFilters()
        with self.db.session() as session:
            courses = CourseRepository(session).list()
        if course_id is not None:
            course = next((item for item in courses if item.course_id == course_id), None)
            if course is None:
                raise KeyError(f"unknown course: {course_id}")
            if not self._course_matches(course, filters):
                raise ValueError(f"course {course_id} does not match the supplied course filters")
            selected = [course]
        else:
            selected = [course for course in courses if self._course_matches(course, filters)]
        return [self.analyze_course(course.course_id, filters) for course in selected]

    def analyze_course(
        self, course_id: str, filters: AnalyticsFilters | None = None
    ) -> AnalyticsReport:
        filters = filters or AnalyticsFilters()
        with self.db.session() as session:
            data = self._load(session, course_id, filters)
        return self._build(data, filters)

    # -- loading -----------------------------------------------------------

    def _load(
        self,
        session: Any,
        course_id: str,
        filters: AnalyticsFilters | None = None,
    ) -> _CourseData:
        courses = CourseRepository(session)
        course = courses.get(course_id)
        if course is None:
            raise KeyError(f"unknown course: {course_id}")
        if filters is not None and not self._course_matches(course, filters):
            raise ValueError(f"course {course_id} does not match the supplied course filters")
        documents = DocumentRepository(session).list_by_course(course_id)
        questions = QuestionRepository(session).list_by_course(course_id)
        understanding = QuestionUnderstandingRepository(session)
        analyses = {
            analysis.question_id: analysis for analysis in understanding.list_analyses(course_id)
        }
        matches: dict[str, list[UnitMatch]] = defaultdict(list)
        for match in understanding.list_unit_matches_for_course(course_id):
            matches[match.question_id].append(match)
        types: dict[str, list[TypeLabel]] = defaultdict(list)
        for label in understanding.list_types_for_course(course_id):
            types[label.question_id].append(label)
        difficulties = {
            item.question_id: item for item in understanding.list_difficulties_for_course(course_id)
        }
        similarities = understanding.list_similarities(course_id)
        units = [
            (syllabus.syllabus_id, unit)
            for syllabus in SyllabusRepository(session).get_by_course(course_id)
            for unit in syllabus.units
        ]
        periods = {
            document.document_id: resolve_exam_period(document, course) for document in documents
        }
        paper_count = int(
            session.scalar(
                select(func.count()).select_from(ExamPaperRow).where(
                    ExamPaperRow.course_id == course_id
                )
            )
            or 0
        )
        paper_by_question = {
            row[0]: (row[1], int(row[2]) if row[2] is not None else None)
            for row in session.execute(
                select(
                    ExamQuestionDetailRow.question_id,
                    ExamQuestionDetailRow.paper_id,
                    ExamQuestionDetailRow.exam_year,
                ).where(ExamQuestionDetailRow.course_id == course_id)
            ).all()
        }
        return _CourseData(
            course=course,
            documents=documents,
            papers=paper_count,
            paper_by_question=paper_by_question,
            questions=questions,
            analyses=analyses,
            matches=dict(matches),
            types=dict(types),
            difficulties=difficulties,
            similarities=similarities,
            units=units,
            periods=periods,
        )

    def _course_matches(self, course: CourseRecord, filters: AnalyticsFilters) -> bool:
        if (
            filters.institution_code
            and course.institution_code.casefold() != filters.institution_code.casefold()
        ):
            return False
        return not (
            filters.syllabus_version
            and (course.syllabus_version or "").casefold() != filters.syllabus_version.casefold()
        )

    # -- report assembly ---------------------------------------------------

    def _build(self, data: _CourseData, filters: AnalyticsFilters) -> AnalyticsReport:
        selected_documents = self._filter_documents(data, filters)
        selected_ids = {document.document_id for document in selected_documents}
        contexts = self._build_contexts(data, selected_ids, filters)
        available_questions = len(contexts)
        warnings: list[Uncertainty] = []
        truncated = available_questions > self.settings.max_questions
        if truncated:
            contexts = contexts[: self.settings.max_questions]
            warnings.append(
                _warning(
                    "analytics_truncated",
                    (
                        f"only {len(contexts)} of {available_questions} matching questions were "
                        "included (EXAM_ENGINE_Q4_MAX_QUESTIONS)"
                    ),
                    detail={"included": len(contexts), "total": available_questions},
                )
            )
        for document in selected_documents:
            period = data.periods[document.document_id]
            for message in period.metadata_warnings:
                warnings.append(
                    _warning(
                        "missing_period_metadata",
                        f"{document.title}: {message}",
                        detail={"document_id": document.document_id},
                    )
                )
        if not contexts:
            warnings.append(
                _warning(
                    "insufficient_data",
                    "no questions match the selected course and filters; "
                    "no analytics can be derived",
                )
            )
        if contexts and all(context.analysis is None for context in contexts):
            warnings.append(
                _warning(
                    "missing_analysis",
                    "no Phase 3 analysis was found; mapping, type and difficulty metrics are empty",
                )
            )

        repetition = self._build_repetition(data, contexts, selected_documents)
        mapping = self._build_mapping(data, contexts)
        marks = self._build_marks(contexts)
        question_types = self._build_types(contexts)
        difficulty = self._build_difficulty(contexts)
        trends = self._build_trends(contexts)
        topic_priority = self._build_priority(data, contexts)
        data_quality = self._build_quality(
            data,
            contexts,
            selected_documents,
            repetition,
            truncated,
        )

        all_warnings = _dedupe_warnings(
            [
                *warnings,
                *data_quality.warnings,
                *repetition.warnings,
                *mapping.warnings,
                *marks.warnings,
                *question_types.warnings,
                *difficulty.warnings,
                *trends.warnings,
                *topic_priority.warnings,
            ]
        )
        report = AnalyticsReport(
            course=_course_scope(data.course),
            settings={
                **self.settings.as_config(),
                "priority_weights": self.settings.priority_weights(),
            },
            filters=filters.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
            data_quality=data_quality,
            mapping=mapping,
            marks=marks,
            question_types=question_types,
            difficulty=difficulty,
            repetition=repetition,
            trends=trends,
            topic_priority=topic_priority,
            warnings=all_warnings,
            limitations=_limitations(),
        )
        return report

    def _filter_documents(
        self, data: _CourseData, filters: AnalyticsFilters
    ) -> list[DocumentRecord]:
        selected: list[DocumentRecord] = []
        source_ids = set(filters.source_ids)
        document_ids = set(filters.document_ids)
        exam_types = {value.casefold() for value in filters.exam_types}
        for document in data.documents:
            if source_ids and document.source_id not in source_ids:
                continue
            if document_ids and document.document_id not in document_ids:
                continue
            period = data.periods[document.document_id]
            if exam_types and (period.exam_type is None or period.exam_type not in exam_types):
                continue
            if not _matches_period(period, filters.periods):
                continue
            selected.append(document)
        return selected

    def _build_contexts(
        self,
        data: _CourseData,
        selected_document_ids: set[str],
        filters: AnalyticsFilters,
    ) -> list[_QuestionContext]:
        units = set(filters.unit_ids)
        topics = {_normalise_topic(value) for value in filters.topics}
        wanted_types = set(filters.question_types)
        wanted_difficulties = set(filters.difficulties)
        documents = {document.document_id: document for document in data.documents}
        contexts: list[_QuestionContext] = []
        for question in data.questions:
            document = documents.get(question.document_id)
            if document is None or document.document_id not in selected_document_ids:
                continue
            analysis = data.analyses.get(question.question_id)
            matches = tuple(data.matches.get(question.question_id, []))
            assigned = (
                matches[:1]
                if analysis is not None and analysis.mapping_status is MappingStatus.MAPPED
                else matches
                if analysis is not None and analysis.mapping_status is MappingStatus.AMBIGUOUS
                else ()
            )
            if units and not any(match.unit_id in units for match in assigned):
                continue
            if topics and not any(_normalise_topic(match.topic) in topics for match in assigned):
                continue
            if wanted_types and (analysis is None or analysis.primary_type not in wanted_types):
                continue
            if wanted_difficulties and (
                analysis is None
                or self._resolved_difficulty_from_analysis(analysis) not in wanted_difficulties
            ):
                continue
            if filters.marks_min is not None and (
                question.marks is None or question.marks < filters.marks_min
            ):
                continue
            if filters.marks_max is not None and (
                question.marks is None or question.marks > filters.marks_max
            ):
                continue
            period = data.periods[document.document_id]
            paper = data.paper_by_question.get(question.question_id)
            if paper is not None and paper[1] is not None and not period.known:
                year = paper[1]
                period = ExamPeriod(
                    label=str(year),
                    source="exam_paper_year",
                    sort_key=f"{year:04d}",
                    session=None,
                    exam_type=None,
                    known=True,
                )
            contexts.append(
                _QuestionContext(
                    question=question,
                    document=document,
                    period=period,
                    analysis=analysis,
                    matches=matches,
                    types=tuple(data.types.get(question.question_id, [])),
                    difficulty=data.difficulties.get(question.question_id),
                )
            )
        contexts.sort(
            key=lambda item: (
                item.document.document_id,
                item.question.number_sort if item.question.number_sort is not None else 10**9,
                item.question.question_id,
            )
        )
        return contexts

    # -- repetition --------------------------------------------------------

    def _build_repetition(
        self,
        data: _CourseData,
        contexts: list[_QuestionContext],
        documents: list[DocumentRecord],
    ) -> RepetitionReport:
        warnings: list[Uncertainty] = []
        by_fingerprint: dict[str, list[_QuestionContext]] = defaultdict(list)
        for context in contexts:
            by_fingerprint[context.exact_key].append(context)

        exact_groups: list[ExactDuplicateGroup] = []
        for _fingerprint, group_contexts in sorted(by_fingerprint.items()):
            if len(group_contexts) < 2:
                continue
            group_contexts = sorted(
                group_contexts,
                key=lambda item: (
                    item.period.sort_key,
                    item.document.document_id,
                    item.question.question_id,
                ),
            )
            document_ids = {item.document.document_id for item in group_contexts}
            sessions = {
                item.period.session_key
                for item in group_contexts
                if item.period.session_key is not None
            }
            exact_groups.append(
                ExactDuplicateGroup(
                    fingerprint=_exact_fingerprint(group_contexts[0].question.text),
                    question_count=len(group_contexts),
                    document_count=len(document_ids),
                    known_session_count=len(sessions),
                    within_document=len(document_ids) == 1,
                    across_documents=len(document_ids) > 1,
                    across_sessions=len(sessions) > 1,
                    appearances=[self._reference(item) for item in group_contexts],
                )
            )
        exact_groups.sort(key=lambda group: (-group.question_count, group.fingerprint))

        context_by_id = {item.question.question_id: item for item in contexts}
        pairs: list[SimilarityPair] = []
        for similarity in data.similarities:
            if similarity.kind is SimilarityKind.EXACT:
                continue
            if similarity.score < self.settings.similarity_threshold:
                continue
            left = context_by_id.get(similarity.question_id)
            right = context_by_id.get(similarity.matched_question_id)
            if left is None or right is None:
                continue
            # Exact repetition is represented by its stronger group record; do
            # not also count the same relationship as a near/semantic pair.
            if left.exact_key == right.exact_key:
                continue
            left_session = left.period.session_key
            right_session = right.period.session_key
            same_session = (
                left_session == right_session
                if left_session is not None and right_session is not None
                else None
            )
            pairs.append(
                SimilarityPair(
                    kind=similarity.kind,
                    score=similarity.score,
                    lexical_score=similarity.lexical_score,
                    semantic_score=similarity.semantic_score,
                    method=similarity.method,
                    evidence=similarity.evidence,
                    same_document=left.document.document_id == right.document.document_id,
                    same_session=same_session,
                    left=self._reference(left),
                    right=self._reference(right),
                )
            )
        pairs.sort(
            key=lambda pair: (
                _TYPE_ORDER.get(pair.kind, 99),
                -pair.score,
                pair.left.question_id,
                pair.right.question_id,
            )
        )
        coverage_stats: dict[SimilarityKind, dict[str, Any]] = {}
        for pair in pairs:
            stat = coverage_stats.setdefault(
                pair.kind,
                {
                    "pairs": 0,
                    "questions": set(),
                    "documents": set(),
                    "sessions": set(),
                    "within_document_pairs": 0,
                    "across_document_pairs": 0,
                    "same_session_pairs": 0,
                    "different_session_pairs": 0,
                    "unknown_session_pairs": 0,
                },
            )
            stat["pairs"] += 1
            stat["questions"].update({pair.left.question_id, pair.right.question_id})
            stat["documents"].update({pair.left.document_id, pair.right.document_id})
            left_context = context_by_id[pair.left.question_id]
            right_context = context_by_id[pair.right.question_id]
            stat["sessions"].update(
                session
                for session in (
                    left_context.period.session_key,
                    right_context.period.session_key,
                )
                if session is not None
            )
            if pair.same_document:
                stat["within_document_pairs"] += 1
            else:
                stat["across_document_pairs"] += 1
            if pair.same_session is True:
                stat["same_session_pairs"] += 1
            elif pair.same_session is False:
                stat["different_session_pairs"] += 1
            else:
                stat["unknown_session_pairs"] += 1
        similarity_coverage = [
            SimilarityCoverageRow(
                kind=kind,
                pair_count=int(stat["pairs"]),
                question_count=len(stat["questions"]),
                document_count=len(stat["documents"]),
                known_session_count=len(stat["sessions"]),
                within_document_pairs=int(stat["within_document_pairs"]),
                across_document_pairs=int(stat["across_document_pairs"]),
                same_session_pairs=int(stat["same_session_pairs"]),
                different_session_pairs=int(stat["different_session_pairs"]),
                unknown_session_pairs=int(stat["unknown_session_pairs"]),
            )
            for kind, stat in sorted(
                coverage_stats.items(), key=lambda item: _TYPE_ORDER.get(item[0], 99)
            )
        ]

        documents_with_questions = {item.document.document_id for item in contexts}
        document_rows: list[DocumentFrequencyRow] = []
        for document in documents:
            if document.document_id not in documents_with_questions:
                continue
            items = [item for item in contexts if item.document.document_id == document.document_id]
            marked = [item.question.marks for item in items if item.question.marks is not None]
            period = data.periods[document.document_id]
            document_rows.append(
                DocumentFrequencyRow(
                    document_id=document.document_id,
                    title=document.title,
                    kind=document.kind,
                    source_id=document.source_id,
                    exam_period=period.label,
                    exam_period_source=period.source,
                    exam_type=period.exam_type,
                    question_count=len(items),
                    distinct_question_texts=len({item.fingerprint for item in items}),
                    total_marks=_round(sum(marked)),
                    questions_with_marks=len(marked),
                    missing_marks=len(items) - len(marked),
                )
            )
        document_rows.sort(key=lambda row: (row.title.casefold(), row.document_id))

        exact_questions = sum(group.question_count for group in exact_groups)
        exact_repeated = sum(group.question_count - 1 for group in exact_groups)
        known_sessions = {
            item.period.session_key for item in contexts if item.period.session_key is not None
        }
        if contexts and not pairs:
            warnings.append(
                _warning(
                    "insufficient_data",
                    "no stored near/semantic/concept pairs are available; exact repeated text is "
                    "still reported",
                    severity=Severity.INFO,
                )
            )
        return RepetitionReport(
            total_questions=len(contexts),
            distinct_question_texts=len(by_fingerprint),
            documents=len(documents_with_questions),
            question_papers=data.papers
            or len(
                {
                    item.document.document_id
                    for item in contexts
                    if item.document.kind is DocumentKind.QUESTION_PAPER
                }
            ),
            known_sessions=len(known_sessions),
            exact_groups=exact_groups,
            exact_group_count=len(exact_groups),
            exact_questions=exact_questions,
            exact_repeated_appearances=exact_repeated,
            within_document_groups=sum(group.within_document for group in exact_groups),
            across_document_groups=sum(group.across_documents for group in exact_groups),
            across_session_groups=sum(group.across_sessions for group in exact_groups),
            near_pairs=sum(pair.kind is SimilarityKind.NEAR for pair in pairs),
            semantic_pairs=sum(pair.kind is SimilarityKind.SEMANTIC for pair in pairs),
            concept_related_pairs=sum(
                pair.kind is SimilarityKind.CONCEPT_RELATED for pair in pairs
            ),
            similarity_coverage=similarity_coverage,
            similarity_pairs=pairs,
            document_frequency=document_rows,
            similarity_threshold=self.settings.similarity_threshold,
            warnings=warnings,
        )

    # -- mapping -----------------------------------------------------------

    def _build_mapping(
        self, data: _CourseData, contexts: list[_QuestionContext]
    ) -> MappingFrequencyReport:
        total = len(contexts)
        analysed = [item for item in contexts if item.analysis is not None]
        statuses = Counter(
            item.analysis.mapping_status for item in analysed if item.analysis is not None
        )
        selected_matches = [match for item in contexts for match in item.assigned_matches]
        questions_with_unit = {
            item.question.question_id for item in contexts if item.assigned_matches
        }
        multi_unit = {
            item.question.question_id
            for item in contexts
            if len({match.unit_id for match in item.assigned_matches}) > 1
        }
        topic_keys_by_question: dict[str, set[str]] = defaultdict(set)
        invalid_topic_matches = 0
        for item in contexts:
            for match in item.assigned_matches:
                if match.topic is not None and concept_noise_reason(match.topic) is not None:
                    invalid_topic_matches += 1
                    continue
                if match.topic is not None:
                    topic_keys_by_question[item.question.question_id].add(
                        _normalise_topic(match.topic)
                    )
        multi_topic = {
            question_id for question_id, keys in topic_keys_by_question.items() if len(keys) > 1
        }
        unassigned_topic = {
            item.question.question_id
            for item in contexts
            if item.assigned_matches
            and any(
                match.topic is None or concept_noise_reason(match.topic) is not None
                for match in item.assigned_matches
            )
        }

        unit_stats: dict[str, dict[str, Any]] = {}
        for syllabus_id, unit in data.units:
            stat = unit_stats.setdefault(
                unit.unit_id,
                {
                    "label": unit.unit_label,
                    "title": unit.title,
                    "syllabus_ids": set(),
                    "questions": set(),
                    "documents": set(),
                    "sessions": set(),
                    "scores": [],
                },
            )
            stat["syllabus_ids"].add(syllabus_id)
        for item in contexts:
            for match in item.assigned_matches:
                stat = unit_stats.setdefault(
                    match.unit_id,
                    {
                        "label": match.unit_label,
                        "title": match.unit_title,
                        "syllabus_ids": set(),
                        "questions": set(),
                        "documents": set(),
                        "sessions": set(),
                        "scores": [],
                    },
                )
                stat["syllabus_ids"].add(match.syllabus_id)
                stat["questions"].add(item.question.question_id)
                stat["documents"].add(item.document.document_id)
                if item.period.session_key is not None:
                    stat["sessions"].add(item.period.session_key)
                stat["scores"].append(match.score)

        unit_rows = [
            UnitFrequencyRow(
                unit_id=unit_id,
                unit_label=str(stat["label"]),
                unit_title=stat["title"],
                syllabus_ids=sorted(stat["syllabus_ids"]),
                document_ids=sorted(stat["documents"]),
                question_count=len(stat["questions"]),
                share_of_questions=_share(len(stat["questions"]), total),
                distinct_documents=len(stat["documents"]),
                distinct_sessions=len(stat["sessions"]),
                average_confidence=_round(
                    statistics.mean(stat["scores"]) if stat["scores"] else 0.0
                ),
            )
            for unit_id, stat in unit_stats.items()
        ]
        unit_rows.sort(key=lambda row: (-row.question_count, row.unit_label.casefold()))

        topic_stats: dict[tuple[str, str | None], dict[str, Any]] = {}
        for item in contexts:
            seen: set[tuple[str, str | None]] = set()
            for match in item.assigned_matches:
                if match.topic is not None and concept_noise_reason(match.topic) is not None:
                    continue
                key = (match.unit_id, _normalise_topic(match.topic) if match.topic else None)
                if key in seen:
                    continue
                seen.add(key)
                stat = topic_stats.setdefault(
                    key,
                    {
                        "unit_label": match.unit_label,
                        "topic": match.topic,
                        "questions": set(),
                        "documents": set(),
                        "sessions": set(),
                        "scores": [],
                    },
                )
                stat["questions"].add(item.question.question_id)
                stat["documents"].add(item.document.document_id)
                if item.period.session_key is not None:
                    stat["sessions"].add(item.period.session_key)
                stat["scores"].append(match.score)
        topic_rows = [
            TopicFrequencyRow(
                unit_id=unit_id,
                unit_label=str(stat["unit_label"]),
                topic=stat["topic"],
                label=stat["topic"] or "unassigned",
                document_ids=sorted(stat["documents"]),
                question_count=len(stat["questions"]),
                share_of_questions=_share(len(stat["questions"]), total),
                distinct_documents=len(stat["documents"]),
                distinct_sessions=len(stat["sessions"]),
                average_confidence=_round(
                    statistics.mean(stat["scores"]) if stat["scores"] else 0.0
                ),
            )
            for (unit_id, _), stat in topic_stats.items()
        ]
        topic_rows.sort(key=lambda row: (-row.question_count, row.unit_label, row.label.casefold()))

        concept_stats: dict[str, dict[str, Any]] = {}
        for item in contexts:
            seen_concepts: set[str] = set()
            for match in item.assigned_matches:
                if match.topic is None or concept_noise_reason(match.topic) is not None:
                    continue
                normalized = _normalise_topic(match.topic)
                if normalized in seen_concepts:
                    continue
                seen_concepts.add(normalized)
                stat = concept_stats.setdefault(
                    normalized,
                    {
                        "labels": Counter(),
                        "unit_ids": set(),
                        "questions": set(),
                        "documents": set(),
                        "sessions": set(),
                        "scores": [],
                    },
                )
                stat["labels"][match.topic] += 1
                stat["unit_ids"].add(match.unit_id)
                stat["questions"].add(item.question.question_id)
                stat["documents"].add(item.document.document_id)
                if item.period.session_key is not None:
                    stat["sessions"].add(item.period.session_key)
                stat["scores"].append(match.score)
        concept_rows: list[ConceptFrequencyRow] = []
        for _normalized, stat in concept_stats.items():
            labels: Counter[str] = stat["labels"]
            label = sorted(labels, key=lambda value: (-labels[value], value.casefold()))[0]
            concept_rows.append(
                ConceptFrequencyRow(
                    concept=label,
                    unit_ids=sorted(stat["unit_ids"]),
                    document_ids=sorted(stat["documents"]),
                    question_count=len(stat["questions"]),
                    share_of_questions=_share(len(stat["questions"]), total),
                    distinct_documents=len(stat["documents"]),
                    distinct_sessions=len(stat["sessions"]),
                    average_confidence=_round(
                        statistics.mean(stat["scores"]) if stat["scores"] else 0.0
                    ),
                )
            )
        concept_rows.sort(
            key=lambda row: (-row.question_count, row.concept.casefold(), row.concept)
        )

        warnings: list[Uncertainty] = []
        if invalid_topic_matches:
            warnings.append(
                _warning(
                    "invalid_topic_labels",
                    f"{invalid_topic_matches} mapping label(s) were excluded from topic "
                    "analytics because they are option/page/instruction fragments",
                    severity=Severity.INFO,
                    detail={"excluded_matches": invalid_topic_matches},
                )
            )
        if total and not data.units:
            warnings.append(
                _warning(
                    "no_syllabus",
                    "this course has no syllabus units; all questions remain unmapped",
                )
            )
        if total and not questions_with_unit:
            warnings.append(
                _warning(
                    "unmapped_question",
                    "no question has a selected syllabus unit; topic/unit analytics are empty",
                )
            )
        return MappingFrequencyReport(
            total_questions=total,
            analysed_questions=len(analysed),
            mapped_questions=statuses.get(MappingStatus.MAPPED, 0),
            ambiguous_questions=statuses.get(MappingStatus.AMBIGUOUS, 0),
            unmapped_questions=statuses.get(MappingStatus.UNMAPPED, 0),
            not_analysed_questions=total - len(analysed),
            questions_with_any_unit=len(questions_with_unit),
            unit_assignments=len(selected_matches),
            multi_unit_questions=len(multi_unit),
            questions_with_any_topic=sum(
                1
                for item in contexts
                if any(
                    match.topic is not None and concept_noise_reason(match.topic) is None
                    for match in item.assigned_matches
                )
            ),
            topic_assignments=sum(
                1
                for match in selected_matches
                if match.topic is not None and concept_noise_reason(match.topic) is None
            ),
            multi_topic_questions=len(multi_topic),
            unassigned_topic_questions=len(unassigned_topic),
            mapping_coverage=_share(len(questions_with_unit), total),
            units=unit_rows,
            topics=topic_rows,
            concepts=concept_rows,
            unmapped_references=[
                self._reference(item)
                for item in contexts
                if item.analysis is not None
                and item.analysis.mapping_status is MappingStatus.UNMAPPED
            ],
            ambiguous_references=[
                self._reference(item)
                for item in contexts
                if item.analysis is not None
                and item.analysis.mapping_status is MappingStatus.AMBIGUOUS
            ],
            warnings=warnings,
        )

    # -- marks -------------------------------------------------------------

    def _build_marks(self, contexts: list[_QuestionContext]) -> MarksReport:
        total = len(contexts)
        marked = [item for item in contexts if item.question.marks is not None]
        marks = [item.question.marks for item in marked if item.question.marks is not None]
        total_marks = _round(sum(marks))
        source_counts = Counter(item.question.marks_source or "unspecified" for item in marked)
        rows = [
            QuestionMarksRow(
                question_id=item.question.question_id,
                document_id=item.document.document_id,
                marks=item.question.marks,
                section=item.question.section,
                question_number=item.question.question_number,
                unit_label=(
                    item.primary_match.unit_label
                    if item.primary_match
                    else ("not analysed" if item.analysis is None else "unmapped")
                ),
                topic=(
                    item.primary_match.topic
                    if item.primary_match and item.primary_match.topic
                    else None
                ),
                question_type=item.primary_type.value if item.primary_type else None,
                difficulty=(
                    self._resolved_difficulty(item).value
                    if self._resolved_difficulty(item) is not None
                    else None
                ),
            )
            for item in marked
            if item.question.marks is not None
        ]
        rows.sort(key=lambda row: (row.document_id, row.question_id))

        allocations = Counter(
            item.question.marks for item in marked if item.question.marks is not None
        )
        allocation_rows = [
            MarksAllocationRow(
                marks=value,
                question_count=count,
                total_marks=_round(value * count),
            )
            for value, count in sorted(allocations.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

        by_section = self._marks_breakdown(
            contexts,
            lambda item: item.question.section or "unassigned",
            total_marks,
        )
        by_unit = self._marks_breakdown(contexts, lambda item: item.unit_label, total_marks)
        by_topic = self._marks_breakdown(contexts, lambda item: item.topic_label, total_marks)
        by_type = self._marks_breakdown(contexts, lambda item: item.type_label, total_marks)
        by_difficulty = self._marks_breakdown(
            contexts, self._difficulty_label, total_marks
        )

        subpart_mismatches: list[MarksAnomaly] = []
        anomalies: list[MarksAnomaly] = []
        for item in contexts:
            # Phase 1 marks doubts are evidence in their own right; they must
            # remain visible even when Phase 3 has not analysed the question.
            for uncertainty in item.question.uncertainties:
                if uncertainty.code in _MARKS_ANOMALY_CODES:
                    anomalies.append(
                        MarksAnomaly(
                            code=uncertainty.code,
                            message=uncertainty.message,
                            question_id=item.question.question_id,
                            detail=uncertainty.detail,
                        )
                    )
            if item.analysis is None:
                continue
            parts = item.analysis.subparts
            if (
                item.question.marks is not None
                and parts
                and all(part.marks is not None for part in parts)
            ):
                part_total = _round(sum(part.marks or 0.0 for part in parts))
                if abs(part_total - item.question.marks) > 0.01:
                    subpart_mismatches.append(
                        MarksAnomaly(
                            code="subpart_marks_mismatch",
                            message=(
                                "stated sub-part marks do not add up to the parent question's marks"
                            ),
                            question_id=item.question.question_id,
                            detail={
                                "parent_marks": item.question.marks,
                                "subpart_marks": part_total,
                                "subpart_count": len(parts),
                            },
                        )
                    )

        warnings: list[Uncertainty] = []
        missing = total - len(marked)
        if missing:
            warnings.append(
                _warning(
                    "insufficient_data",
                    f"{missing} question(s) have no stated marks; totals include only stated marks",
                    severity=Severity.WARNING,
                    detail={"missing_marks": missing, "questions_with_marks": len(marked)},
                )
            )
        if subpart_mismatches:
            warnings.append(
                _warning(
                    "subpart_marks_mismatch",
                    f"{len(subpart_mismatches)} question(s) have sub-part marks that do not "
                    "add up to the parent marks",
                    detail={"count": len(subpart_mismatches)},
                )
            )
        if anomalies:
            warnings.append(
                _warning(
                    "inconsistent_marks_metadata",
                    f"{len(anomalies)} question(s) have marks metadata that needs review",
                    detail={
                        "count": len(anomalies),
                        "codes": sorted({anomaly.code for anomaly in anomalies}),
                    },
                )
            )
        return MarksReport(
            total_questions=total,
            questions_with_marks=len(marked),
            missing_marks=missing,
            marks_coverage=_share(len(marked), total),
            total_marks=total_marks,
            mean_marks=_round(statistics.mean(marks)) if marks else None,
            median_marks=_round(statistics.median(marks)) if marks else None,
            min_marks=_round(min(marks)) if marks else None,
            max_marks=_round(max(marks)) if marks else None,
            zero_marks_questions=sum(1 for value in marks if value == 0),
            marks_sources=dict(sorted(source_counts.items())),
            question_marks=rows,
            common_allocations=allocation_rows,
            by_section=by_section,
            by_unit=by_unit,
            by_topic=by_topic,
            by_question_type=by_type,
            by_difficulty=by_difficulty,
            subpart_mismatches=subpart_mismatches,
            anomalies=anomalies,
            warnings=warnings,
        )

    def _marks_breakdown(
        self,
        contexts: list[_QuestionContext],
        labeller: Callable[[_QuestionContext], str],
        overall_marks: float,
    ) -> list[MarksBreakdownRow]:
        groups: dict[str, list[_QuestionContext]] = defaultdict(list)
        for item in contexts:
            groups[labeller(item)].append(item)
        rows: list[MarksBreakdownRow] = []
        for label, items in groups.items():
            values = [item.question.marks for item in items if item.question.marks is not None]
            total = _round(sum(values))
            rows.append(
                MarksBreakdownRow(
                    label=label,
                    question_count=len(items),
                    questions_with_marks=len(values),
                    missing_marks=len(items) - len(values),
                    total_marks=total,
                    mean_marks=_round(statistics.mean(values)) if values else None,
                    share_of_questions=_share(len(items), len(contexts)),
                    share_of_marks=_share_of_marks(total, overall_marks),
                )
            )
        rows.sort(key=lambda row: (-row.question_count, row.label.casefold(), row.label))
        return rows

    # -- classifications ---------------------------------------------------

    def _build_types(self, contexts: list[_QuestionContext]) -> QuestionTypeReport:
        total = len(contexts)
        analyses = [item for item in contexts if item.analysis is not None]
        primary_counts: Counter[str] = Counter()
        all_counts: Counter[str] = Counter()
        low_confidence = 0
        multi_label = 0
        label_assignments = 0
        for item in analyses:
            assert item.analysis is not None
            if item.analysis.primary_type is not None:
                primary_counts[item.analysis.primary_type.value] += 1
                if item.analysis.primary_type_confidence < self.settings.low_confidence_threshold:
                    low_confidence += 1
            seen_labels: set[QuestionType] = set()
            for label in item.types:
                if label.label in seen_labels:
                    continue
                seen_labels.add(label.label)
                all_counts[label.label.value] += 1
                label_assignments += 1
            if len(seen_labels) > 1:
                multi_label += 1
        primary_rows = [
            LabelCountRow(
                label=label,
                question_count=count,
                share_of_questions=_share(count, total),
            )
            for label, count in sorted(primary_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]
        all_rows = [
            LabelCountRow(
                label=label,
                question_count=count,
                share_of_questions=_share(count, total),
            )
            for label, count in sorted(all_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]
        warnings: list[Uncertainty] = []
        unclassified = total - sum(primary_counts.values())
        if unclassified:
            warnings.append(
                _warning(
                    "unknown_question_type",
                    f"{unclassified} question(s) have no primary question type",
                    severity=Severity.WARNING,
                    detail={"unclassified": unclassified},
                )
            )
        if low_confidence:
            warnings.append(
                _warning(
                    "insufficient_data",
                    f"{low_confidence} primary question-type label(s) are below the confidence "
                    "threshold",
                    severity=Severity.INFO,
                    detail={"low_confidence": low_confidence},
                )
            )
        return QuestionTypeReport(
            total_questions=total,
            analysed_questions=len(analyses),
            unclassified_questions=unclassified,
            not_analysed_questions=total - len(analyses),
            low_confidence_questions=low_confidence,
            multi_label_questions=multi_label,
            primary_assignments=sum(primary_counts.values()),
            label_assignments=label_assignments,
            primary_labels=primary_rows,
            all_labels=all_rows,
            warnings=warnings,
        )

    def _build_difficulty(self, contexts: list[_QuestionContext]) -> DifficultyReport:
        total = len(contexts)
        analyses = [item for item in contexts if item.analysis is not None]
        counts: Counter[str] = Counter()
        low_confidence = 0
        scores: list[float] = []
        estimated = 0
        for item in analyses:
            assert item.analysis is not None
            label = self._resolved_difficulty(item) or DifficultyLabel.UNKNOWN
            counts[label.value] += 1
            if item.difficulty is not None and item.difficulty.estimated:
                estimated += 1
            if label is not DifficultyLabel.UNKNOWN:
                scores.append(item.analysis.difficulty_score)
                if item.analysis.difficulty_confidence < self.settings.low_confidence_threshold:
                    low_confidence += 1
        label_rows = [
            LabelCountRow(
                label=label,
                question_count=count,
                share_of_questions=_share(count, total),
            )
            for label, count in sorted(
                counts.items(),
                key=lambda pair: (
                    _DIFFICULTY_ORDER.get(DifficultyLabel(pair[0]), 99),
                    pair[0],
                ),
            )
        ]
        unknown = counts.get(DifficultyLabel.UNKNOWN.value, 0)
        warnings: list[Uncertainty] = []
        if unknown:
            warnings.append(
                _warning(
                    "difficulty_uncertain",
                    f"{unknown} question(s) have an unknown difficulty estimate",
                    severity=Severity.WARNING,
                    detail={"unknown": unknown},
                )
            )
        if low_confidence:
            warnings.append(
                _warning(
                    "insufficient_data",
                    f"{low_confidence} known difficulty estimate(s) are below the confidence "
                    "threshold",
                    severity=Severity.INFO,
                    detail={"low_confidence": low_confidence},
                )
            )
        return DifficultyReport(
            total_questions=total,
            analysed_questions=len(analyses),
            estimated_questions=estimated,
            not_analysed_questions=total - len(analyses),
            unknown_questions=unknown,
            low_confidence_questions=low_confidence,
            labels=label_rows,
            mean_score=_round(statistics.mean(scores)) if scores else None,
            estimated=True,
            empirical_measurement_available=False,
            warnings=warnings,
        )

    # -- trends ------------------------------------------------------------

    def _build_trends(self, contexts: list[_QuestionContext]) -> TrendReport:
        known = [item for item in contexts if item.period.known]
        unknown = len(contexts) - len(known)
        sessions = {
            item.period.session_key for item in known if item.period.session_key is not None
        }
        warnings: list[Uncertainty] = []
        if unknown:
            warnings.append(
                _warning(
                    "missing_period_metadata",
                    f"{unknown} question(s) have no examination period and are "
                    "excluded from trends",
                    severity=Severity.INFO,
                    detail={"questions_without_period": unknown},
                )
            )
        if not known:
            warnings.append(
                _warning(
                    "missing_period_metadata",
                    "no document has an examination session, examination date or academic year; "
                    "trend analysis is unavailable",
                )
            )
            return TrendReport(
                status=TrendStatus.NO_PERIOD_METADATA,
                available=False,
                reason="no documented examination period is available",
                questions_with_period=0,
                questions_without_period=unknown,
                known_sessions=0,
                minimum_period_questions=self.settings.min_period_questions,
                warnings=warnings,
                notes=_trend_notes(),
            )

        grouped: dict[str, list[_QuestionContext]] = defaultdict(list)
        periods: dict[str, ExamPeriod] = {}
        for item in known:
            grouped[item.period.key].append(item)
            periods[item.period.key] = item.period
        ordered_keys = sorted(
            grouped,
            key=lambda key: (periods[key].sort_key, periods[key].label.casefold(), key),
        )
        period_summaries: list[TrendPeriodSummary] = []
        for key in ordered_keys:
            items = grouped[key]
            period = periods[key]
            units = Counter(item.unit_label for item in items)
            topics = Counter(item.topic_label for item in items)
            types = Counter(item.type_label for item in items)
            difficulties = Counter(self._difficulty_label(item) for item in items)
            marks = [item.question.marks for item in items if item.question.marks is not None]
            period_sessions = {
                item.period.session_key for item in items if item.period.session_key is not None
            }
            period_types = sorted(
                {item.period.exam_type for item in items if item.period.exam_type is not None}
            )
            period_summaries.append(
                TrendPeriodSummary(
                    period=period.label,
                    source=period.source,
                    sort_key=period.sort_key,
                    question_count=len(items),
                    document_count=len({item.document.document_id for item in items}),
                    document_ids=sorted({item.document.document_id for item in items}),
                    session_count=len(period_sessions),
                    exam_types=period_types,
                    total_marks=_round(sum(marks)),
                    questions_with_marks=len(marks),
                    missing_marks=len(items) - len(marks),
                    low_sample=len(items) < self.settings.min_period_questions,
                    unit_counts=dict(sorted(units.items())),
                    topic_counts=dict(sorted(topics.items())),
                    question_type_counts=dict(sorted(types.items())),
                    difficulty_counts=dict(sorted(difficulties.items())),
                )
            )
        period_sources = sorted({summary.source for summary in period_summaries})
        if len(period_sources) > 1:
            warnings.append(
                _warning(
                    "mixed_period_sources",
                    "periods come from different metadata kinds; comparisons are descriptive "
                    "and may not be chronologically equivalent",
                    detail={"sources": period_sources},
                )
            )
        mixed_type_periods = [
            summary.period for summary in period_summaries if len(summary.exam_types) > 1
        ]
        if mixed_type_periods:
            warnings.append(
                _warning(
                    "mixed_exam_types",
                    "one or more periods contain more than one examination type; the period is "
                    "a coarse grouping, not one session",
                    detail={"periods": mixed_type_periods},
                )
            )
        low_sample = any(summary.low_sample for summary in period_summaries)
        if len(period_summaries) < self.settings.min_trend_periods:
            status = TrendStatus.INSUFFICIENT_DATA
            reason = (
                f"only {len(period_summaries)} observed period(s); at least "
                f"{self.settings.min_trend_periods} are required"
            )
            warnings.append(
                _warning(
                    "insufficient_periods",
                    reason,
                    detail={"periods": len(period_summaries)},
                )
            )
            available = False
        else:
            status = TrendStatus.LOW_SAMPLE if low_sample else TrendStatus.OK
            reason = None
            available = True
            if low_sample:
                warnings.append(
                    _warning(
                        "small_sample",
                        f"at least one period has fewer than "
                        f"{self.settings.min_period_questions} questions; changes are marked "
                        "unreliable",
                    )
                )

        changes: list[TrendChange] = []
        if len(period_summaries) >= self.settings.min_trend_periods:
            for previous, current in zip(period_summaries, period_summaries[1:], strict=False):
                reliable = (
                    not previous.low_sample
                    and not current.low_sample
                    and previous.question_count >= self.settings.min_period_questions
                    and current.question_count >= self.settings.min_period_questions
                )
                changes.append(
                    TrendChange(
                        metric="question_count",
                        label="all questions",
                        previous_period=previous.period,
                        current_period=current.period,
                        previous_value=float(previous.question_count),
                        current_value=float(current.question_count),
                        delta=float(current.question_count - previous.question_count),
                        previous_sample=previous.question_count,
                        current_sample=current.question_count,
                        reliable=reliable,
                    )
                )
                if previous.questions_with_marks and current.questions_with_marks:
                    marks_reliable = (
                        reliable
                        and previous.questions_with_marks >= self.settings.min_period_questions
                        and current.questions_with_marks >= self.settings.min_period_questions
                    )
                    changes.append(
                        TrendChange(
                            metric="total_marks",
                            label="stated marks",
                            previous_period=previous.period,
                            current_period=current.period,
                            previous_value=previous.total_marks,
                            current_value=current.total_marks,
                            delta=_round(current.total_marks - previous.total_marks),
                            previous_sample=previous.questions_with_marks,
                            current_sample=current.questions_with_marks,
                            reliable=marks_reliable,
                        )
                    )
                for metric, before, after in (
                    ("unit", previous.unit_counts, current.unit_counts),
                    ("topic", previous.topic_counts, current.topic_counts),
                    ("question_type", previous.question_type_counts, current.question_type_counts),
                    ("difficulty", previous.difficulty_counts, current.difficulty_counts),
                ):
                    for label in sorted(
                        set(before) | set(after), key=lambda value: value.casefold()
                    ):
                        if before.get(label, 0) == 0 and after.get(label, 0) == 0:
                            continue
                        changes.append(
                            TrendChange(
                                metric=metric,
                                label=label,
                                previous_period=previous.period,
                                current_period=current.period,
                                previous_value=float(before.get(label, 0)),
                                current_value=float(after.get(label, 0)),
                                delta=float(after.get(label, 0) - before.get(label, 0)),
                                previous_sample=previous.question_count,
                                current_sample=current.question_count,
                                reliable=reliable,
                            )
                        )

        new_topics: list[NewTopic] = []
        if len(period_summaries) >= self.settings.min_trend_periods:
            seen: set[str] = set(period_summaries[0].topic_counts)
            for summary in period_summaries[1:]:
                for topic in sorted(summary.topic_counts):
                    if topic in _SPECIAL_UNIT_LABELS or topic in {
                        "unclassified",
                        "unassigned",
                    }:
                        continue
                    if topic not in seen:
                        new_topics.append(
                            NewTopic(
                                topic=topic,
                                normalized_topic=_normalise_topic(topic),
                                period=summary.period,
                                question_count=summary.topic_counts[topic],
                                reliable=not summary.low_sample,
                            )
                        )
                    seen.add(topic)

        return TrendReport(
            status=status,
            available=available,
            reason=reason,
            periods=period_summaries,
            changes=changes,
            new_topics=new_topics,
            questions_with_period=len(known),
            questions_without_period=unknown,
            known_sessions=len(sessions),
            first_period=period_summaries[0].period,
            last_period=period_summaries[-1].period,
            period_sources=period_sources,
            minimum_period_questions=self.settings.min_period_questions,
            warnings=warnings,
            notes=_trend_notes(),
        )

    # -- historical topic priority ----------------------------------------

    def _build_priority(
        self, data: _CourseData, contexts: list[_QuestionContext]
    ) -> TopicPriorityReport:
        topic_stats: dict[str, _TopicStat] = {}
        for item in contexts:
            seen: set[str] = set()
            for match in item.assigned_matches:
                if match.topic is None or concept_noise_reason(match.topic) is not None:
                    continue
                normalized = _normalise_topic(match.topic)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                stat = topic_stats.setdefault(normalized, _TopicStat(labels=Counter()))
                stat.labels[match.topic] += 1
                stat.unit_ids.add(match.unit_id)
                stat.questions.add(item.question.question_id)
                stat.documents.add(item.document.document_id)
                if item.document.kind is DocumentKind.QUESTION_PAPER:
                    stat.papers.add(item.document.document_id)
                if item.period.known:
                    # Recency is available for any resolved period, including a
                    # coarse academic year; it is not restricted to sessions.
                    stat.periods.add(item.period.key)
                if item.period.session_key is not None:
                    stat.sessions.add(item.period.session_key)
                if item.question.marks is not None:
                    stat.marks.append(item.question.marks)
                stat.scores.append(match.score)

        periods = sorted(
            {item.period.key for item in contexts if item.period.known},
            key=lambda key: (
                next(item.period.sort_key for item in contexts if item.period.key == key),
                key,
            ),
        )
        period_sources = {item.period.source for item in contexts if item.period.known}
        recency_available = len(periods) >= 2 and len(period_sources) == 1
        total_sessions = len(
            {item.period.session_key for item in contexts if item.period.session_key is not None}
        )
        coverage_available = total_sessions > 0
        frequency_basis = (
            "question_papers"
            if any(item.document.kind is DocumentKind.QUESTION_PAPER for item in contexts)
            else "documents"
        )
        max_frequency = max(
            (
                len(stat.papers) if frequency_basis == "question_papers" else len(stat.documents)
                for stat in topic_stats.values()
            ),
            default=0,
        )
        max_marks = max(
            (statistics.mean(stat.marks) for stat in topic_stats.values() if stat.marks),
            default=0.0,
        )
        # Explicit zero marks are data, but they provide no usable marks
        # variation for a 0-1 ranking factor.
        marks_available = max_marks > 0
        period_index = {key: index for index, key in enumerate(periods)}
        weights = self.settings.priority_weights()
        rows: list[TopicPriorityRow] = []
        for normalized, stat in topic_stats.items():
            labels: Counter[str] = stat.labels
            label = sorted(labels, key=lambda value: (-labels[value], value.casefold()))[0]
            frequency_raw = (
                len(stat.papers) if frequency_basis == "question_papers" else len(stat.documents)
            )
            average_marks = statistics.mean(stat.marks) if stat.marks else None
            latest_period = None
            if stat.periods:
                latest_key = max(stat.periods, key=lambda key: period_index.get(key, -1))
                latest_period = next(
                    item.period.label for item in contexts if item.period.key == latest_key
                )
            factors: dict[str, float | None] = {
                "frequency": _round(frequency_raw / max_frequency) if max_frequency else None,
                "recency": (
                    _round(
                        period_index[max(stat.periods, key=lambda key: period_index[key])]
                        / (len(periods) - 1)
                    )
                    if recency_available and stat.periods and len(periods) > 1
                    else None
                ),
                "marks": (
                    _round(average_marks / max_marks)
                    if marks_available and average_marks is not None and max_marks > 0
                    else None
                ),
                "coverage": (
                    _round(len(stat.sessions) / total_sessions) if coverage_available else None
                ),
                "confidence": _round(statistics.mean(stat.scores)) if stat.scores else None,
            }
            available_weights = {
                name: weight for name, weight in weights.items() if factors.get(name) is not None
            }
            weight_sum = sum(available_weights.values())
            effective = (
                {name: _round(weight / weight_sum) for name, weight in available_weights.items()}
                if weight_sum > 0
                else {}
            )
            score = (
                sum((factors[name] or 0.0) * weight for name, weight in available_weights.items())
                / weight_sum
                if weight_sum > 0
                else 0.0
            )
            explanation = [
                f"frequency: {frequency_raw} distinct {frequency_basis.replace('_', ' ')}",
                f"coverage: {len(stat.sessions)} of {total_sessions} known examination session(s)",
                (
                    f"stated marks: average {average_marks:.2f} across "
                    f"{len(stat.marks)} question(s)"
                    if average_marks is not None
                    else "stated marks: unavailable; marks factor omitted"
                ),
                f"mapping confidence: {statistics.mean(stat.scores):.2f}",
                (
                    f"recency: latest observed period is {latest_period}"
                    if factors["recency"] is not None
                    else (
                        "recency: omitted (topic has no resolved period)"
                        if latest_period is None
                        else "recency: omitted (no comparable multi-period date metadata)"
                    )
                ),
            ]
            rows.append(
                TopicPriorityRow(
                    rank=0,
                    topic=label,
                    normalized_topic=normalized,
                    unit_ids=sorted(stat.unit_ids),
                    document_ids=sorted(stat.documents),
                    question_count=len(stat.questions),
                    document_count=len(stat.documents),
                    paper_count=len(stat.papers),
                    session_count=len(stat.sessions),
                    frequency_basis=frequency_basis,
                    total_marks=_round(sum(stat.marks)),
                    questions_with_marks=len(stat.marks),
                    average_marks=_round(average_marks) if average_marks is not None else None,
                    average_mapping_confidence=(
                        _round(statistics.mean(stat.scores)) if stat.scores else 0.0
                    ),
                    latest_period=latest_period,
                    factors=factors,
                    weights_used=effective,
                    score=_round(score),
                    explanation=explanation,
                )
            )
        rows.sort(key=lambda row: (-row.score, -row.question_count, row.topic.casefold()))
        for index, row in enumerate(rows, start=1):
            row.rank = index

        warnings: list[Uncertainty] = []
        if not rows:
            warnings.append(
                _warning(
                    "insufficient_data",
                    "no topic has a syllabus mapping; historical topic priority is empty",
                    severity=Severity.INFO,
                )
            )
        if rows and not any(row.weights_used for row in rows):
            warnings.append(
                _warning(
                    "priority_weights_unavailable",
                    "all configured topic-priority weights are zero or unavailable; scores are "
                    "zero and the ranking is not meaningful",
                    severity=Severity.WARNING,
                )
            )
        if rows and not recency_available:
            warnings.append(
                _warning(
                    "missing_period_metadata",
                    "recency was omitted from topic priority because fewer than two comparable "
                    "periods are available",
                    severity=Severity.INFO,
                )
            )
        if rows and not marks_available:
            warnings.append(
                _warning(
                    "insufficient_data",
                    "no usable stated-marks variation is available; the marks factor was "
                    "omitted and weights were renormalised",
                    severity=Severity.INFO,
                )
            )
        return TopicPriorityReport(
            topics=rows,
            weights=weights,
            recency_available=recency_available,
            coverage_available=coverage_available,
            marks_available=marks_available,
            notes=[
                "Ranking describes historical prominence in the available dataset, not a "
                "prediction of future examinations.",
                "Factors are normalized to 0–1 and weights are renormalised over the factors "
                "that are actually available.",
                "A topic absent from the dataset was not necessarily absent from every exam.",
                "Topics are syllabus topic labels, not validated semantic concepts.",
            ],
            warnings=warnings,
        )

    # -- data quality ------------------------------------------------------

    def _build_quality(
        self,
        data: _CourseData,
        contexts: list[_QuestionContext],
        selected_documents: list[DocumentRecord],
        repetition: RepetitionReport,
        truncated: bool,
    ) -> DataQualityReport:
        total = len(contexts)
        analysed = [item for item in contexts if item.analysis is not None]
        statuses = Counter(
            item.analysis.mapping_status for item in analysed if item.analysis is not None
        )
        with_unit = sum(1 for item in contexts if item.assigned_matches)
        classified = sum(1 for item in analysed if item.analysis and item.analysis.primary_type)
        low_confidence = sum(
            1
            for item in contexts
            if item.analysis is not None
            and (
                (
                    item.analysis.primary_type is not None
                    and item.analysis.primary_type_confidence
                    < self.settings.low_confidence_threshold
                )
                or (
                    self._resolved_difficulty(item) is not DifficultyLabel.UNKNOWN
                    and item.analysis.difficulty_confidence < self.settings.low_confidence_threshold
                )
                or (
                    item.analysis.mapping_status in (MappingStatus.MAPPED, MappingStatus.AMBIGUOUS)
                    and item.analysis.mapping_confidence < self.settings.low_confidence_threshold
                )
            )
        )
        with_marks = sum(1 for item in contexts if item.question.marks is not None)
        known_periods = {item.period.key for item in contexts if item.period.known}
        known_sessions = {
            item.period.session_key for item in contexts if item.period.session_key is not None
        }
        document_groups: dict[str, list[str]] = defaultdict(list)
        for document in selected_documents:
            if document.text_fingerprint:
                document_groups[document.text_fingerprint].append(document.document_id)
        duplicate_groups = [group for group in document_groups.values() if len(group) > 1]
        warnings: list[Uncertainty] = []
        if total < self.settings.small_sample_questions:
            warnings.append(
                _warning(
                    "small_sample",
                    f"only {total} question(s) are available; descriptive metrics are unstable",
                    severity=Severity.INFO,
                    detail={"questions": total, "threshold": self.settings.small_sample_questions},
                )
            )
        if repetition.exact_group_count:
            warnings.append(
                _warning(
                    "exact_repetition",
                    (
                        f"{repetition.exact_group_count} exact repeated-text group(s) were found; "
                        "they are counted as separate appearances unless you deduplicate the source"
                    ),
                    severity=Severity.INFO,
                    detail={"exact_groups": repetition.exact_group_count},
                )
            )
        if duplicate_groups:
            warnings.append(
                _warning(
                    "duplicate_document_text",
                    f"{len(duplicate_groups)} group(s) of documents share an extracted-text "
                    "fingerprint; check for repeated imports",
                    detail={
                        "groups": len(duplicate_groups),
                        "documents": sum(len(group) for group in duplicate_groups),
                        "document_ids": sorted(
                            document_id for group in duplicate_groups for document_id in group
                        ),
                    },
                )
            )
        if total and not analysed:
            warnings.append(
                _warning(
                    "missing_analysis",
                    "questions are present but none has Phase 3 analysis; run "
                    "'understanding analyze'",
                )
            )
        return DataQualityReport(
            questions=total,
            distinct_question_texts=repetition.distinct_question_texts,
            documents=len(selected_documents),
            documents_with_questions=len({item.document.document_id for item in contexts}),
            question_papers=data.papers
            or sum(
                document.kind is DocumentKind.QUESTION_PAPER for document in selected_documents
            ),
            question_banks=sum(
                document.kind is DocumentKind.QUESTION_BANK for document in selected_documents
            ),
            sources=len({document.source_id for document in selected_documents}),
            known_sessions=len(known_sessions),
            known_periods=len(known_periods),
            questions_with_period=sum(1 for item in contexts if item.period.known),
            questions_without_period=sum(1 for item in contexts if not item.period.known),
            analysed_questions=len(analysed),
            analysis_coverage=_share(len(analysed), total),
            mapped_questions=statuses.get(MappingStatus.MAPPED, 0),
            ambiguous_questions=statuses.get(MappingStatus.AMBIGUOUS, 0),
            unmapped_questions=statuses.get(MappingStatus.UNMAPPED, 0),
            not_analysed_questions=total - len(analysed),
            mapping_coverage=_share(with_unit, total),
            classified_questions=classified,
            unclassified_questions=total - classified,
            low_confidence_questions=low_confidence,
            questions_with_marks=with_marks,
            missing_marks=total - with_marks,
            marks_coverage=_share(with_marks, total),
            needs_review_questions=sum(1 for item in contexts if item.question.needs_review),
            exact_duplicate_groups=repetition.exact_group_count,
            exact_duplicate_appearances=repetition.exact_questions,
            duplicate_document_text_groups=len(duplicate_groups),
            duplicate_document_text_documents=sum(len(group) for group in duplicate_groups),
            small_sample=total < self.settings.small_sample_questions,
            truncated=truncated,
            warnings=warnings,
        )

    def _reference(self, context: _QuestionContext) -> QuestionReference:
        return QuestionReference(
            question_id=context.question.question_id,
            document_id=context.document.document_id,
            document_title=context.document.title,
            document_kind=context.document.kind,
            source_id=context.document.source_id,
            question_number=context.question.question_number,
            section=context.question.section,
            page_number=context.question.page_number,
            source_locator=context.question.source_locator,
            marks=context.question.marks,
            text=context.question.text,
            exam_period=context.period.label,
            exam_period_source=context.period.source,
            exam_type=context.period.exam_type,
            session=context.period.session,
        )

    # -- diagnostics -------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """Return lightweight question/analysis counts without building reports."""
        with self.db.session() as session:
            courses = CourseRepository(session).list()
            repo = QuestionUnderstandingRepository(session)
            questions = QuestionRepository(session)
            rows = []
            for course in courses:
                rows.append(
                    {
                        "course_id": course.course_id,
                        "questions": questions.count_by_course(course.course_id),
                        "analysed": repo.count_analyses(course.course_id),
                        "mapped": repo.count_analyses(
                            course.course_id, mapping_status=MappingStatus.MAPPED
                        ),
                        "ambiguous": repo.count_analyses(
                            course.course_id, mapping_status=MappingStatus.AMBIGUOUS
                        ),
                        "unmapped": repo.count_analyses(
                            course.course_id, mapping_status=MappingStatus.UNMAPPED
                        ),
                        "similarities": repo.count_similarities(course.course_id),
                    }
                )
        return rows


def _trend_notes() -> list[str]:
    return [
        "Only observed periods are shown; missing periods are not interpolated.",
        "Periods come from explicit exam_session/exam_date metadata, otherwise academic year; "
        "upload time is never used.",
        "Changes are descriptive and are not statistical significance tests or forecasts.",
        "Sample sizes are shown for every period; a low-sample period is marked unreliable.",
    ]


def _limitations() -> list[str]:
    return [
        "Difficulty and question types are Phase 3 estimates, not measured student outcomes.",
        "Only identical normalised text is treated as an exact duplicate; near/semantic matches "
        "are review evidence, not proof of duplication.",
        "Topic/concept frequencies use Phase 3 syllabus mappings; a question can contribute to "
        "several unit/topic buckets, so bucket totals may exceed question totals.",
        "Concept frequency uses syllabus topic labels, not a validated semantic concept "
        "classifier.",
        "Marks totals include only stated marks; missing values are never inferred.",
        "Analytics describe the available dataset and do not establish that a topic was absent "
        "from examinations outside it.",
        "Reports are recalculated from current stored rows; a stale database produces stale "
        "observations, not cached analytics.",
    ]


__all__ = ["AnalyticsService"]
