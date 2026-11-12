"""JSON API for the Phase 12 dashboard.

Every route delegates to the Phase 1-11 service layer. The module contains only
presentation concerns: scope checks at the boundary, consistent response shapes,
explicit truncation for large read-only reports, and HTTP error mapping. It never
re-computes an analysis, invents a result or writes assessment data outside the
existing repositories and services.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from pydantic import ValidationError

from exam_engine.advanced.service import AdvancedAssessmentService
from exam_engine.analytics.service import AnalyticsService
from exam_engine.config import Settings
from exam_engine.coverage.service import CoverageService
from exam_engine.dashboard.errors import ApiError, to_api_error
from exam_engine.dashboard.jobs import JobManager
from exam_engine.dashboard.tutor import TutorService
from exam_engine.datasets.queries import ExamDatasetQueries, ExamFilters
from exam_engine.evolution.service import ConceptEvolutionService
from exam_engine.exam_dna.service import ExamDnaService
from exam_engine.generation.grading import MAX_ANSWER_CHARS, grade_answer
from exam_engine.generation.service import QuestionGenerationService
from exam_engine.graph.service import GraphService
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.ingest.validation import FORMAT_BY_SUFFIX
from exam_engine.models.advanced import (
    AdversarialRequest,
    MisconceptionQuery,
    MutationRequest,
)
from exam_engine.models.analytics import AnalyticsFilters
from exam_engine.models.course import CourseRecord
from exam_engine.models.coverage import CoverageFilters, CoverageRequest
from exam_engine.models.enums import (
    AssessmentOrigin,
    CoverageStatus,
    DatasetType,
    DifficultyLabel,
    DocumentKind,
    DuplicatePolicy,
    GenerationFormat,
    GenerationReviewStatus,
    GraphNodeType,
    MisconceptionKind,
    MisconceptionStatus,
    QuestionType,
)
from exam_engine.models.evolution import EvolutionFilters, EvolutionRequest
from exam_engine.models.exam_dna import ExamDnaFilters, ExamDnaRequest
from exam_engine.models.generation import GenerationQuery, GenerationRequest
from exam_engine.models.graph import GraphNode
from exam_engine.models.prerequisite import (
    LearningPathRequest,
    PrerequisiteDiscoveryRequest,
)
from exam_engine.models.provenance import DatasetSource
from exam_engine.nlp.service import NlpService
from exam_engine.prerequisite.discovery import PrerequisiteDiscoveryService
from exam_engine.prerequisite.path import LearningPathService
from exam_engine.recovery.service import DatasetRecoveryService
from exam_engine.storage.db import Database, open_database
from exam_engine.storage.nlp_repositories import NlpRunRepository
from exam_engine.storage.question_repositories import UnderstandingRunRepository
from exam_engine.storage.recovery_repository import RecoveryRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from exam_engine.understanding.service import UnderstandingService
from exam_engine.version import __version__

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 4 * 1024 * 1024


@dataclass
class ApiResponse:
    status: int
    payload: Any


@dataclass(frozen=True)
class _JobSpec:
    label: str
    operation: Any


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _tutor_ask_arguments(body: dict[str, Any]) -> tuple[str, str | None, str | None]:
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ApiError(400, "invalid_request", "question must be a non-empty string")
    concept_id = body.get("concept_id")
    if concept_id is not None and not isinstance(concept_id, str):
        raise ApiError(400, "invalid_request", "concept_id must be a string")
    syllabus_id = body.get("syllabus_id")
    if syllabus_id is not None and not isinstance(syllabus_id, str):
        raise ApiError(400, "invalid_request", "syllabus_id must be a string")
    return question, concept_id, syllabus_id


def _query_one(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    if not values:
        return default
    return values[-1]


def _query_list(query: dict[str, list[str]], key: str) -> list[str]:
    result: list[str] = []
    for raw in query.get(key, []):
        result.extend(part.strip() for part in raw.split(",") if part.strip())
    return result


def _query_int(
    query: dict[str, list[str]],
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 1000,
) -> int:
    raw = _query_one(query, key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_query", f"{key} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ApiError(
            400,
            "invalid_query",
            f"{key} must be between {minimum} and {maximum}",
        )
    return value


def _query_enum(query: dict[str, list[str]], key: str, enum_type: Any, default: Any = None) -> Any:
    raw = _query_one(query, key)
    if raw is None or raw == "":
        return default
    try:
        return enum_type(raw)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ApiError(400, "invalid_query", f"{key} must be one of: {allowed}") from exc


def _enum_list(values: list[str], enum_type: Any, key: str) -> list[Any]:
    result = []
    for value in values:
        try:
            result.append(enum_type(value))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in enum_type)
            raise ApiError(400, "invalid_query", f"{key} values must be one of: {allowed}") from exc
    return result


def _trim(
    payload: dict[str, Any],
    path: tuple[str, ...],
    limit: int,
    truncation: dict[str, Any],
) -> None:
    current: Any = payload
    for key in path[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(key)
    if not isinstance(current, dict):
        return
    items = current.get(path[-1])
    if isinstance(items, list) and len(items) > limit:
        current[path[-1]] = items[:limit]
        truncation[".".join(path)] = {"shown": limit, "total": len(items)}


class DashboardAPI:
    """Request dispatcher. One database connection per request; no global state."""

    def __init__(
        self,
        db_path: Path | str,
        settings: Settings | None = None,
        jobs: JobManager | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.settings = settings or Settings.from_env().with_db(self.db_path)
        self.jobs = jobs or JobManager(self.db_path)

        # Migrate once at startup. Per-request connections skip the migration
        # check, which keeps local SQLite reads fast without hiding schema work.
        startup = open_database(self.db_path)
        startup.close()

    def close(self) -> None:
        self.jobs.close()

    # -- dispatch ----------------------------------------------------------
    def dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]] | None = None,
        body: bytes = b"",
    ) -> ApiResponse:
        query = query or {}
        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        try:
            if parts[:1] != ["api"]:
                raise ApiError(404, "not_found", "no such API route")
            route = parts[1:]
            if method == "GET":
                return self._get(route, query)
            if method == "POST":
                return self._post(route, query, body)
            raise ApiError(405, "method_not_allowed", f"{method} is not supported")
        except Exception as exc:  # noqa: BLE001 - API fault boundary
            error = to_api_error(exc)
            if error.status >= 500:
                logger.exception("dashboard API failure for %s %s", method, path)
            return ApiResponse(error.status, error.as_dict())

    # -- GET ---------------------------------------------------------------
    def _get(self, route: list[str], query: dict[str, list[str]]) -> ApiResponse:
        if route == ["health"]:
            return ApiResponse(200, self._health())
        if route == ["capabilities"]:
            return ApiResponse(200, self._capabilities())
        if route == ["recovery"]:
            return ApiResponse(200, self._recovery_overview(query))
        if route == ["recovery", "courses"]:
            return ApiResponse(200, self._recovery_courses(query))
        if route == ["recovery", "issues"]:
            return ApiResponse(200, self._recovery_issues(query))
        if route == ["recovery", "providers"]:
            return ApiResponse(200, self._recovery_providers())
        if route == ["recovery", "crawl", "health"]:
            from exam_engine.recovery.crawl4ai_adapter import Crawl4AIHealth

            return ApiResponse(200, Crawl4AIHealth.check(force=False))
        if route == ["recovery", "sources"]:
            return ApiResponse(200, self._recovery_sources(query))
        if len(route) == 3 and route[:2] == ["recovery", "courses"]:
            return ApiResponse(200, self._recovery_course(route[2], query))
        if len(route) == 3 and route[:2] == ["recovery", "records"]:
            return ApiResponse(200, self._recovery_record(route[2], query))
        if len(route) == 3 and route[:2] == ["recovery", "runs"]:
            return ApiResponse(200, self._recovery_run_status(route[2]))
        if len(route) == 3 and route[:2] == ["recovery", "exports"]:
            return ApiResponse(200, self._recovery_export(route[2], query))
        if route == ["exams"]:
            return ApiResponse(200, self._exams(query))
        if route == ["exams", "questions"]:
            return ApiResponse(200, self._exam_questions(query))
        if len(route) == 3 and route[:2] == ["exams", "questions"]:
            return ApiResponse(200, self._exam_question(route[2]))
        if len(route) == 3 and route[:2] == ["exams", "passages"]:
            return ApiResponse(200, self._exam_passage(route[2]))
        if route == ["exams", "papers"]:
            return ApiResponse(200, self._exam_papers(query))
        if route == ["exams", "syllabus"]:
            return ApiResponse(200, self._exam_syllabus(query))
        if route == ["courses"]:
            return ApiResponse(200, self._course_list())
        if route == ["jobs"]:
            course_id = _query_one(query, "course_id")
            limit = _query_int(query, "limit", 20, maximum=100)
            return ApiResponse(
                200,
                {
                    "jobs": [
                        job.as_dict() for job in self.jobs.list(course_id=course_id, limit=limit)
                    ]
                },
            )
        if len(route) == 2 and route[0] == "jobs":
            job = self.jobs.get(route[1])
            if job is None:
                raise ApiError(404, "unknown_job", "no such job")
            return ApiResponse(200, job.as_dict())
        if len(route) < 2 or route[0] != "courses":
            raise ApiError(404, "not_found", "no such API route")

        course_id = route[1]
        suffix = route[2:]
        if not suffix:
            return ApiResponse(200, self._course_detail(course_id))
        if suffix == ["documents"]:
            return ApiResponse(200, self._documents(course_id))
        if suffix == ["questions"]:
            return ApiResponse(200, self._questions(course_id, query))
        if suffix == ["analytics"]:
            return ApiResponse(200, self._analytics(course_id, query))
        if suffix == ["coverage"]:
            return ApiResponse(200, self._coverage(course_id, query))
        if len(suffix) == 3 and suffix[0] == "coverage" and suffix[1] == "concepts":
            return ApiResponse(200, self._coverage_concept(course_id, suffix[2], query))
        if suffix == ["exam-dna"]:
            return ApiResponse(200, self._exam_dna(course_id, query))
        if suffix == ["evolution"]:
            return ApiResponse(200, self._evolution(course_id, query))
        if suffix == ["graph"]:
            return ApiResponse(200, self._graph(course_id, query))
        if suffix == ["graph", "neighborhood"]:
            return ApiResponse(200, self._neighborhood(course_id, query))
        if suffix == ["prerequisites"]:
            return ApiResponse(200, self._prerequisites(course_id, query))
        if suffix == ["generated"]:
            return ApiResponse(200, self._generated(course_id, query))
        if suffix == ["generation", "options"]:
            return ApiResponse(200, self._generation_options(course_id, query))
        if suffix == ["advanced"]:
            return ApiResponse(200, self._advanced(course_id))
        if suffix == ["misconceptions"]:
            return ApiResponse(200, self._misconceptions(course_id, query))
        if suffix == ["tutor", "context"]:
            return ApiResponse(200, self._tutor_context(course_id, query))
        raise ApiError(404, "not_found", "no such API route")

    # -- POST --------------------------------------------------------------
    def _post(self, route: list[str], query: dict[str, list[str]], body: bytes) -> ApiResponse:
        if route == ["courses"]:
            return self._create_course(self._json_body(body))
        if route == ["recovery", "run"]:
            return self._recovery_run(self._json_body(body))
        if route == ["recovery", "repairs", "review"]:
            return self._recovery_review(self._json_body(body))
        if route == ["recovery", "rollback"]:
            return self._recovery_rollback(self._json_body(body))
        if route == ["recovery", "crawl"]:
            return self._recovery_crawl(self._json_body(body))
        if len(route) < 2 or route[0] != "courses":
            raise ApiError(404, "not_found", "no such API route")
        course_id = route[1]
        suffix = route[2:]
        if len(suffix) == 2 and suffix[0] == "analysis":
            return self._start_analysis(course_id, suffix[1])
        if suffix == ["learning-path"]:
            return self._learning_path(course_id, self._json_body(body))
        if suffix == ["generate"]:
            return self._generate(course_id, self._json_body(body))
        if len(suffix) == 3 and suffix[0] == "generated" and suffix[2] == "review":
            return self._review(course_id, suffix[1], self._json_body(body))
        if len(suffix) == 3 and suffix[0] == "generated" and suffix[2] == "grade":
            return self._grade(course_id, suffix[1], self._json_body(body))
        if suffix == ["misconceptions", "analyse"]:
            return self._analyse_misconceptions(course_id, self._json_body(body))
        if suffix == ["advanced", "mutate"]:
            return self._mutate(course_id, self._json_body(body))
        if suffix == ["advanced", "adversarial"]:
            return self._adversarial(course_id, self._json_body(body))
        if suffix == ["tutor", "ask"]:
            return self._tutor_ask(course_id, self._json_body(body))
        raise ApiError(404, "not_found", "no such API route")

    # -- capability/health -------------------------------------------------
    def _health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "time": datetime.now(UTC).isoformat(),
            "database": {"configured": True, "name": self.db_path.name},
            "authentication": {
                "enabled": False,
                "detail": "The dashboard is a local, single-user tool and enforces no accounts.",
            },
        }

    def _capabilities(self) -> dict[str, Any]:
        with self._db() as database:
            tutor = TutorService(database, self.settings).capabilities()
        return {
            "phase": 13,
            "version": __version__,
            "modules": [
                {
                    "id": "ingestion",
                    "label": "Document ingestion",
                    "available": True,
                    "detail": "PDF, DOCX, text, CSV/JSON banks and OCR-gated images.",
                },
                {
                    "id": "nlp",
                    "label": "NLP analysis",
                    "available": True,
                    "detail": "Normalised text, keyphrases, concepts, embeddings and topics.",
                },
                {
                    "id": "understanding",
                    "label": "Question understanding",
                    "available": True,
                    "detail": "Structure, syllabus mapping, type, Bloom estimate and similarity.",
                },
                {
                    "id": "analytics",
                    "label": "Question-paper analytics",
                    "available": True,
                    "detail": (
                        "Read-only repetition, distribution, marks and observed-period reports."
                    ),
                },
                {
                    "id": "graph",
                    "label": "Question knowledge graph",
                    "available": True,
                    "detail": "Persistent, evidence-bearing course-scoped nodes and edges.",
                },
                {
                    "id": "prerequisites",
                    "label": "Prerequisites and learning paths",
                    "available": True,
                    "detail": "Reviewable candidates and explainable dependency ordering.",
                },
                {
                    "id": "coverage",
                    "label": "Coverage gaps",
                    "available": True,
                    "detail": "Evidence-bounded coverage statuses with unmapped-question reasons.",
                },
                {
                    "id": "exam_dna",
                    "label": "Exam DNA",
                    "available": True,
                    "detail": "Historical distributions and recurrence, never a forecast.",
                },
                {
                    "id": "evolution",
                    "label": "Concept evolution",
                    "available": True,
                    "detail": "Dated observations and review-only change events.",
                },
                {
                    "id": "generation",
                    "label": "Question generation",
                    "available": True,
                    "detail": "Grounded, validated, review-first practice questions.",
                },
                {
                    "id": "advanced",
                    "label": "Advanced assessment",
                    "available": True,
                    "detail": "Mutation, adversarial challenges and misconception mining.",
                },
                {
                    "id": "exam_datasets",
                    "label": "Personal exam datasets",
                    "available": True,
                    "detail": (
                        "GATE/CBSE/JEE/NEET CSV question banks and syllabus PDFs, "
                        "with source occurrence and answer traceability."
                    ),
                },
                {
                    "id": "dataset_recovery",
                    "label": "Dataset recovery",
                    "available": True,
                    "detail": (
                        "Audits schemas, structure, options, answers and source fidelity; "
                        "keeps repairs and unverified answers quarantined for review."
                    ),
                },
                {
                    "id": "grading",
                    "label": "Answer checks",
                    "available": True,
                    "detail": (
                        "Local checks for stored generated questions: exact multiple-choice "
                        "keys and transparent lexical feedback for written answers."
                    ),
                },
            ],
            "tutor": tutor,
            "artwork": {
                "hairline": {
                    "package": "@lucasmarkes/hairline",
                    "version": "0.5.1",
                    "license": "MIT",
                    "role": "Decorative interactive illustration only; never a data chart.",
                },
                "tutor_avatar": {
                    "implementation": "original SVG component",
                    "license": "MIT (this project)",
                    "studio_bundled": False,
                    "studio_license": "AGPL-3.0-only",
                    "detail": (
                        "Bible Strong Avatar Lab and its renderer packages are AGPL-3.0-only "
                        "and are not copied, bundled or loaded."
                    ),
                },
            },
            "authentication": {"enabled": False},
            "privacy": {
                "network_requests": bool(
                    tutor["web_search"]["available"]
                    and tutor["conversational_reasoning"]["available"]
                ),
                "detail": (
                    "Stored data stays local. When the AI tutor is enabled it may send "
                    "the current question and cited snippets to the configured model and "
                    "search provider; there is no analytics or telemetry. Disable the "
                    "tutor provider to stay fully offline."
                ),
            },
        }

    # -- exam datasets -----------------------------------------------------
    def _exam_filters(self, query: dict[str, list[str]]) -> ExamFilters:
        year_raw = _query_one(query, "year") or _query_one(query, "exam_year")
        year: int | None = None
        if year_raw:
            try:
                year = int(year_raw)
            except ValueError as exc:
                raise ApiError(400, "invalid_query", "year must be an integer") from exc
        return ExamFilters(
            category_id=_query_one(query, "category"),
            subject_code=_query_one(query, "subject"),
            course_id=_query_one(query, "course"),
            exam_year=year,
            question_type=_query_one(query, "type"),
            answer_type=_query_one(query, "answer_type"),
            unit_id=_query_one(query, "unit_id"),
            topic=_query_one(query, "topic"),
            question_id=_query_one(query, "question_id"),
            search=_query_one(query, "search"),
        )

    def _exams(self, query: dict[str, list[str]]) -> dict[str, Any]:
        filters = self._exam_filters(query)
        with self._db() as database, database.session() as session:
            return ExamDatasetQueries(session).overview(filters)

    def _exam_questions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        filters = self._exam_filters(query)
        limit = _query_int(query, "limit", 50, minimum=1, maximum=500)
        offset = _query_int(query, "offset", 0, maximum=100000)
        with self._db() as database, database.session() as session:
            return ExamDatasetQueries(session).questions(filters, limit=limit, offset=offset)

    def _exam_passage(self, passage_id: str) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            payload = ExamDatasetQueries(session).passage(passage_id)
        if payload is None:
            raise ApiError(404, "unknown_passage", f"no passage {passage_id!r} exists")
        return payload

    def _exam_question(self, question_id: str) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            payload = ExamDatasetQueries(session).question(question_id)
        if payload is None:
            raise ApiError(404, "unknown_question", f"no question {question_id!r} exists")
        return payload

    def _exam_papers(self, query: dict[str, list[str]]) -> dict[str, Any]:
        filters = self._exam_filters(query)
        limit = _query_int(query, "limit", 200, minimum=1, maximum=1000)
        with self._db() as database, database.session() as session:
            return ExamDatasetQueries(session).papers(filters, limit=limit)

    def _exam_syllabus(self, query: dict[str, list[str]]) -> dict[str, Any]:
        course_id = _query_one(query, "course")
        if not course_id:
            raise ApiError(400, "invalid_query", "course is required")
        with self._db() as database, database.session() as session:
            payload = ExamDatasetQueries(session).syllabus(course_id)
        if payload.get("error") == "unknown_course":
            raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
        return payload

    # -- course reads ------------------------------------------------------
    def _db(self) -> Database:
        return open_database(self.db_path, migrate_schema=False)

    def _status_rows(
        self, database: Database, course_id: str | None = None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        nlp: dict[str, dict[str, Any]] = {
            str(row["course_id"]): row
            for row in NlpService(database, self.settings.nlp).status(course_id)
        }
        understanding: dict[str, dict[str, Any]] = {
            str(row["course_id"]): row
            for row in UnderstandingService(database, self.settings.understanding).status(course_id)
        }
        graph: dict[str, dict[str, Any]] = {
            str(row["course_id"]): dict(row)
            for row in GraphService(database, self.settings.graph).status(course_id)
        }
        return nlp, understanding, graph

    def _course_list(self) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            # Use one session for all repository reads in this request.
            courses = CourseRepository(session).list()
            nlp_rows, und_rows, graph_rows = self._status_rows(database)
            payload = [
                self._course_summary(session, course, nlp_rows, und_rows, graph_rows)
                for course in courses
            ]
        return {"courses": payload, "total": len(payload)}

    def _course_summary(
        self,
        session: Any,
        course: CourseRecord,
        nlp_rows: dict[str, dict[str, Any]],
        und_rows: dict[str, dict[str, Any]],
        graph_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        documents = DocumentRepository(session)
        questions = QuestionRepository(session)
        syllabi_repo = SyllabusRepository(session)
        syllabi = syllabi_repo.get_by_course(course.course_id)
        counts = {
            "documents": documents.count_by_course(course.course_id),
            "questions": questions.count_by_course(course.course_id),
            "needs_review": questions.count_needing_review(course.course_id),
            "syllabi": len(syllabi),
            "syllabus_units": sum(len(item.units) for item in syllabi),
        }
        nlp = nlp_rows.get(course.course_id, {})
        und = und_rows.get(course.course_id, {})
        graph = graph_rows.get(course.course_id, {})
        data_state = "empty"
        if counts["documents"]:
            data_state = "documents_only"
        if counts["syllabi"]:
            data_state = "ingested"
        if und.get("analyzed", 0) > 0:
            data_state = "analysed"
        if graph.get("concepts", 0) > 0:
            data_state = "graph_ready"
        return {
            "course_id": course.course_id,
            "course_code": course.course_code,
            "title": course.title,
            "institution_code": course.institution_code,
            "institution_name": course.institution_name,
            "department": course.department,
            "programme": course.programme,
            "academic_year": course.academic_year,
            "syllabus_version": course.syllabus_version,
            "description": course.description,
            "counts": counts,
            "syllabi": [
                {
                    "syllabus_id": item.syllabus_id,
                    "title": item.title,
                    "confidence": item.confidence,
                    "needs_review": item.needs_review,
                    "unit_count": len(item.units),
                }
                for item in syllabi
            ],
            "analysis": {
                "nlp": {
                    "texts": nlp.get("texts", 0),
                    "concepts": nlp.get("concepts", 0),
                    "topics": nlp.get("topics", 0),
                    "last_run": nlp.get("last_run"),
                    "last_status": nlp.get("last_status"),
                },
                "understanding": {
                    "analyzed": und.get("analyzed", 0),
                    "mapped": und.get("mapped", 0),
                    "ambiguous": und.get("ambiguous", 0),
                    "unmapped": und.get("unmapped", 0),
                    "similarities": und.get("similarities", 0),
                    "last_run": und.get("last_run"),
                    "last_status": und.get("last_status"),
                },
                "graph": {
                    "nodes": graph.get("nodes", 0),
                    "edges": graph.get("edges", 0),
                    "concepts": graph.get("concepts", 0),
                    "candidate_edges": graph.get("candidate_edges", 0),
                },
            },
            "data_state": data_state,
        }

    def _course_detail(self, course_id: str) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            course = CourseRepository(session).get(course_id)
            if course is None:
                raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            nlp_rows, und_rows, graph_rows = self._status_rows(database, course_id)
            summary = self._course_summary(session, course, nlp_rows, und_rows, graph_rows)
            documents = DocumentRepository(session).list_by_course(course_id)
            sources = {item.source_id: item for item in DatasetSourceRepository(session).list()}
            question_repo = QuestionRepository(session)
            syllabi = SyllabusRepository(session).get_by_course(course_id)
            nlp_run = NlpRunRepository(session).latest_for_course(course_id)
            und_run = UnderstandingRunRepository(session).latest_for_course(course_id)
            prerequisites = GraphService(database, self.settings.graph).prerequisite_edges(
                course_id
            )
            summary.update(
                {
                    "documents": [
                        self._document_payload(
                            document,
                            sources.get(document.source_id),
                            question_repo.count_by_document(document.document_id),
                        )
                        for document in documents
                    ],
                    "syllabus_details": [self._syllabus_payload(item) for item in syllabi],
                    "sources": [
                        self._source_payload(source)
                        for source in sources.values()
                        if any(document.source_id == source.source_id for document in documents)
                    ],
                    "activity": self._activity(documents, nlp_run, und_run),
                    "prerequisites": {
                        "relationships": len(prerequisites),
                        "candidates": sum(
                            1 for edge in prerequisites if edge.status.value == "candidate"
                        ),
                    },
                }
            )
            summary["next_steps"] = self._next_steps(summary, prerequisites)
        return summary

    def _activity(
        self,
        documents: list[Any],
        nlp_run: Any,
        und_run: Any,
    ) -> list[dict[str, Any]]:
        events = [
            {
                "at": _iso(document.ingested_at),
                "kind": "document_ingested",
                "title": document.title,
                "detail": f"{document.kind.value} · {document.page_count} page(s)",
            }
            for document in documents
        ]
        for run, kind, label in (
            (nlp_run, "nlp_run", "NLP analysis"),
            (und_run, "understanding_run", "Question understanding"),
        ):
            if run is None:
                continue
            events.append(
                {
                    "at": _iso(
                        getattr(run, "finished_at", None) or getattr(run, "started_at", None)
                    ),
                    "kind": kind,
                    "title": label,
                    "detail": getattr(getattr(run, "status", None), "value", "unknown"),
                }
            )
        events.sort(key=lambda item: item["at"] or "", reverse=True)
        return events[:12]

    def _next_steps(
        self, summary: dict[str, Any], prerequisites: list[Any]
    ) -> list[dict[str, Any]]:
        counts = summary["counts"]
        analysis = summary["analysis"]
        steps: list[dict[str, Any]] = []
        if counts["documents"] == 0:
            steps.append(
                {
                    "code": "ingest_document",
                    "label": "Add the first document",
                    "detail": "Upload a syllabus or question paper to start the workspace.",
                    "action": "upload",
                }
            )
            return steps
        if counts["syllabi"] == 0:
            steps.append(
                {
                    "code": "add_syllabus",
                    "label": "Add a syllabus",
                    "detail": (
                        "Coverage, Exam DNA and generation need a parsed syllabus version; "
                        "question papers alone cannot establish coverage."
                    ),
                    "action": "upload",
                }
            )
        if counts["questions"] == 0:
            steps.append(
                {
                    "code": "add_questions",
                    "label": "Add a question paper or bank",
                    "detail": "No questions were extracted from the current documents.",
                    "action": "upload",
                }
            )
        elif analysis["nlp"]["texts"] < counts["documents"]:
            steps.append(
                {
                    "code": "run_nlp",
                    "label": "Run text analysis",
                    "detail": (
                        f"{analysis['nlp']['texts']} of {counts['documents']} documents have "
                        "derived NLP records."
                    ),
                    "action": "job",
                    "kind": "nlp",
                }
            )
        if counts["questions"] and analysis["understanding"]["analyzed"] < counts["questions"]:
            steps.append(
                {
                    "code": "run_understanding",
                    "label": "Run question understanding",
                    "detail": (
                        f"{analysis['understanding']['analyzed']} of {counts['questions']} "
                        "questions have question analysis."
                    ),
                    "action": "job",
                    "kind": "understanding",
                }
            )
        if counts["questions"] and analysis["graph"]["concepts"] == 0:
            steps.append(
                {
                    "code": "run_graph",
                    "label": "Build the knowledge graph",
                    "detail": "No course-scoped graph nodes exist yet.",
                    "action": "job",
                    "kind": "graph",
                }
            )
        if (
            analysis["graph"]["concepts"] >= 2
            and len(prerequisites) == 0
            and analysis["understanding"]["analyzed"] > 0
        ):
            steps.append(
                {
                    "code": "run_prerequisites",
                    "label": "Discover prerequisite candidates",
                    "detail": (
                        "Optional: derive reviewable prerequisite edges from syllabus order and "
                        "question co-occurrence."
                    ),
                    "action": "job",
                    "kind": "prerequisites",
                    "optional": True,
                }
            )
        if counts["needs_review"] > 0:
            steps.append(
                {
                    "code": "review_questions",
                    "label": "Review extraction uncertainties",
                    "detail": (
                        f"{counts['needs_review']} extracted question(s) are flagged for review."
                    ),
                    "action": "navigate",
                    "route": "questions",
                }
            )
        if not steps:
            steps.append(
                {
                    "code": "ready",
                    "label": "Workspace is ready",
                    "detail": (
                        "Open Analysis, Learning or Practice. Coverage describes this corpus "
                        "only; it is not a forecast."
                    ),
                    "action": "none",
                }
            )
        return steps

    def _source_payload(self, source: DatasetSource) -> dict[str, Any]:
        return {
            "source_id": source.source_id,
            "name": source.name,
            "dataset_type": source.dataset_type.value,
            "retrieved_at": _iso(source.retrieved_at),
            "license": source.license,
            "license_url": source.license_url,
            "source_url": source.source_url,
            "publisher": source.publisher,
            "usage_terms": source.usage_terms,
            "notes": source.notes,
        }

    def _document_payload(
        self,
        document: Any,
        source: DatasetSource | None,
        question_count: int,
    ) -> dict[str, Any]:
        return {
            "document_id": document.document_id,
            "title": document.title,
            "kind": document.kind.value,
            "format": document.format.value,
            "source_id": document.source_id,
            "source": self._source_payload(source) if source else None,
            "original_filename": document.original_filename,
            "page_count": document.page_count,
            "char_count": document.char_count,
            "byte_size": document.byte_size,
            "quality_score": document.quality_score,
            "quality_flags": document.quality_flags,
            "extraction_status": document.extraction_status.value,
            "language": document.language,
            "academic_year": document.academic_year,
            "publication_date": _iso(document.publication_date),
            "ingested_at": _iso(document.ingested_at),
            "relative_path": document.relative_path,
            "warnings": document.warnings[:8],
            "question_count": question_count,
        }

    def _syllabus_payload(self, syllabus: Any) -> dict[str, Any]:
        return {
            "syllabus_id": syllabus.syllabus_id,
            "course_id": syllabus.course_id,
            "document_id": syllabus.document_id,
            "source_id": syllabus.source_id,
            "title": syllabus.title,
            "confidence": syllabus.confidence,
            "needs_review": syllabus.needs_review,
            "unit_count": len(syllabus.units),
            "uncertainties": [
                {"code": item.code, "message": item.message, "severity": item.severity.value}
                for item in syllabus.uncertainties[:8]
            ],
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "unit_label": unit.unit_label,
                    "unit_number": unit.unit_number,
                    "title": unit.title,
                    "hours": unit.hours,
                    "topics": unit.topics,
                    "page_number": unit.page_number,
                    "confidence": unit.confidence,
                }
                for unit in syllabus.units
            ],
        }

    def _documents(self, course_id: str) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            if CourseRepository(session).get(course_id) is None:
                raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            documents = DocumentRepository(session).list_by_course(course_id)
            sources = {item.source_id: item for item in DatasetSourceRepository(session).list()}
            questions = QuestionRepository(session)
            payload = [
                self._document_payload(
                    document,
                    sources.get(document.source_id),
                    questions.count_by_document(document.document_id),
                )
                for document in documents
            ]
        return {"documents": payload, "total": len(payload)}

    def _questions(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = _query_int(query, "limit", 50, minimum=1, maximum=500)
        needs_review = _query_one(query, "needs_review") in {"1", "true", "yes"}
        document_id = _query_one(query, "document_id")
        with self._db() as database, database.session() as session:
            course = CourseRepository(session).get(course_id)
            if course is None:
                raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            repo = QuestionRepository(session)
            if document_id:
                rows = [
                    item
                    for item in repo.list_by_document(document_id)
                    if item.course_id == course_id
                ][:limit]
            elif needs_review:
                rows = repo.list_needing_review(course_id, limit=limit)
            else:
                rows = repo.list_by_course(course_id, limit=limit)
            total = repo.count_by_course(course_id)
            payload = [
                {
                    "question_id": item.question_id,
                    "document_id": item.document_id,
                    "question_number": item.question_number,
                    "section": item.section,
                    "unit": item.unit,
                    "marks": item.marks,
                    "page_number": item.page_number,
                    "source_locator": item.source_locator,
                    "confidence": item.confidence,
                    "needs_review": item.needs_review,
                    "text": item.text,
                    "uncertainties": [
                        {
                            "code": uncertainty.code,
                            "message": uncertainty.message,
                            "severity": uncertainty.severity.value,
                        }
                        for uncertainty in item.uncertainties[:6]
                    ],
                }
                for item in rows
            ]
        return {"questions": payload, "total": total, "shown": len(payload), "limit": limit}

    # -- analysis reads ----------------------------------------------------
    def _analytics(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        filters = AnalyticsFilters(
            institution_code=_query_one(query, "institution_code"),
            syllabus_version=_query_one(query, "syllabus_version"),
            source_ids=_query_list(query, "source_ids"),
            document_ids=_query_list(query, "document_ids"),
            unit_ids=_query_list(query, "unit_ids"),
            topics=_query_list(query, "topics"),
            exam_types=_query_list(query, "exam_types"),
            periods=_query_list(query, "periods"),
            question_types=_enum_list(
                _query_list(query, "question_types"), QuestionType, "question_types"
            ),
            difficulties=_enum_list(
                _query_list(query, "difficulties"), DifficultyLabel, "difficulties"
            ),
            marks_min=_query_float(query, "marks_min"),
            marks_max=_query_float(query, "marks_max"),
        )
        with self._db() as database:
            report = AnalyticsService(database, self.settings.analytics).analyze_course(
                course_id, filters
            )
        payload = _dump(report)
        payload["view"] = self._view(query)
        if payload["view"] == "summary":
            truncation: dict[str, Any] = {}
            # The paper analyzer needs every adjacent-period comparison so the
            # client can switch years; a flat first-N trim silently hid all
            # comparisons after the first pair.
            trend_changes_limit = _query_int(
                query, "trend_changes_limit", 60, minimum=1, maximum=5000
            )
            for path, limit in (
                (("mapping", "units"), 100),
                (("mapping", "topics"), 100),
                (("mapping", "concepts"), 100),
                (("mapping", "unmapped_references"), 25),
                (("mapping", "ambiguous_references"), 25),
                (("marks", "question_marks"), 25),
                (("marks", "common_allocations"), 20),
                (("repetition", "exact_groups"), 25),
                (("repetition", "similarity_pairs"), 25),
                (("repetition", "document_frequency"), 50),
                (("trends", "periods"), 60),
                (("trends", "changes"), trend_changes_limit),
                (("trends", "new_topics"), 30),
                (("topic_priority", "topics"), 25),
            ):
                _trim(payload, path, limit, truncation)
            payload["truncation"] = truncation
        return payload

    def _coverage(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        request = CoverageRequest(
            course_id=course_id,
            syllabus_id=_query_one(query, "syllabus_id"),
            filters=CoverageFilters(
                document_ids=_query_list(query, "document_ids"),
                source_ids=_query_list(query, "source_ids"),
                unit_ids=_query_list(query, "unit_ids"),
                question_types=_enum_list(
                    _query_list(query, "question_types"), QuestionType, "question_types"
                ),
                exam_periods=_query_list(query, "exam_periods"),
            ),
        )
        with self._db() as database:
            report = CoverageService(database, self.settings.coverage).analyze(
                request,
                include_topic_fallback=True,
                include_question_assignments=True,
            )
        payload = _dump(report)
        view = self._view(query)
        payload["view"] = view
        if view == "summary":
            status = _query_enum(query, "status", CoverageStatus)
            concepts = payload.get("concepts", [])
            if status is not None:
                concepts = [item for item in concepts if item.get("status") == status.value]
            concepts_total = len(concepts)
            limit = _query_int(query, "limit", 100, minimum=1, maximum=2000)
            selected = concepts[:limit]
            for concept in selected:
                concept["supporting_questions"] = concept.get("supporting_questions", [])[:3]
                concept["candidate_mappings"] = concept.get("candidate_mappings", [])[:3]
            unmapped = payload.get("unmapped_questions", [])
            unmapped_total = len(unmapped)
            payload["concepts"] = selected
            payload["concepts_total"] = concepts_total
            payload["unmapped_questions"] = unmapped[:50]
            payload["truncation"] = {
                "concepts": {"shown": len(selected), "total": concepts_total},
                "unmapped_questions": {
                    "shown": len(payload["unmapped_questions"]),
                    "total": unmapped_total,
                },
            }
        return payload

    def _coverage_concept(
        self, course_id: str, concept: str, query: dict[str, list[str]]
    ) -> dict[str, Any]:
        request = CoverageRequest(
            course_id=course_id,
            syllabus_id=_query_one(query, "syllabus_id"),
        )
        with self._db() as database:
            finding = CoverageService(database, self.settings.coverage).concept(request, concept)
        return _dump(finding)

    def _exam_dna(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        request = ExamDnaRequest(
            course_id=course_id,
            syllabus_id=_query_one(query, "syllabus_id"),
            filters=ExamDnaFilters(
                document_ids=_query_list(query, "document_ids"),
                source_ids=_query_list(query, "source_ids"),
                exam_types=_query_list(query, "exam_types"),
                exam_periods=_query_list(query, "exam_periods"),
            ),
        )
        with self._db() as database:
            profile = ExamDnaService(
                database,
                self.settings.exam_dna,
                coverage_settings=self.settings.coverage,
            ).analyze(request)
        payload = _dump(profile)
        payload["view"] = self._view(query)
        if payload["view"] == "summary":
            truncation: dict[str, Any] = {}
            _trim(payload, ("patterns",), 100, truncation)
            _trim(payload, ("question_groups",), 100, truncation)
            payload["truncation"] = truncation
        return payload

    def _evolution(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        request = EvolutionRequest(
            course_id=course_id,
            filters=EvolutionFilters(
                concept_ids=_query_list(query, "concept_ids"),
                document_ids=_query_list(query, "document_ids"),
                syllabus_ids=_query_list(query, "syllabus_ids"),
            ),
            include_assessment=_query_bool(query, "include_assessment", True),
            include_exam_dna=_query_bool(query, "include_exam_dna", True),
            include_relationships=_query_bool(query, "include_relationships", True),
        )
        with self._db() as database:
            report = ConceptEvolutionService(
                database,
                self.settings.evolution,
                coverage_settings=self.settings.coverage,
                exam_dna_settings=self.settings.exam_dna,
            ).analyze(request)
        payload = _dump(report)
        payload["view"] = self._view(query)
        if payload["view"] == "summary":
            truncation: dict[str, Any] = {}
            _trim(payload, ("timelines",), 50, truncation)
            _trim(payload, ("changes",), 100, truncation)
            _trim(payload, ("assessment",), 100, truncation)
            payload["truncation"] = truncation
        return payload

    # -- graph/prerequisites ----------------------------------------------
    def _graph(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = _query_int(query, "limit", 500, minimum=1, maximum=2000)
        with self._db() as database:
            with database.session() as session:
                if CourseRepository(session).get(course_id) is None:
                    raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            service = GraphService(database, self.settings.graph)
            concepts = service.concepts(course_id)
            prerequisites = service.prerequisite_edges(course_id)
            status = next(
                (row for row in service.status() if row.get("course_id") == course_id),
                {},
            )
        return {
            "course_id": course_id,
            "status": status,
            "concepts_total": len(concepts),
            "concepts": [self._node_payload(node) for node in concepts[:limit]],
            "truncated": len(concepts) > limit,
            "prerequisite_edges": [self._edge_payload(edge) for edge in prerequisites],
        }

    def _neighborhood(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        node_id = _query_one(query, "node_id")
        if not node_id:
            raise ApiError(400, "invalid_query", "node_id is required")
        hops = _query_int(query, "hops", 1, minimum=0, maximum=2)
        with self._db() as database:
            service = GraphService(database, self.settings.graph)
            center = service.node(node_id)
            if center is None or center.course_id != course_id:
                raise ApiError(404, "unknown_concept", "no such concept in this course")
            neighborhood = service.neighborhood(node_id, hops=hops, course_id=course_id)
        return _dump(neighborhood)

    def _prerequisites(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        syllabus_id = _query_one(query, "syllabus_id")
        with self._db() as database:
            with database.session() as session:
                if CourseRepository(session).get(course_id) is None:
                    raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            edges = GraphService(database, self.settings.graph).prerequisite_edges(
                course_id, syllabus_id=syllabus_id
            )
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "total": len(edges),
            "candidates": sum(1 for edge in edges if edge.status.value == "candidate"),
            "relationships": [self._edge_payload(edge) for edge in edges],
        }

    def _node_payload(self, node: GraphNode) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "node_type": node.node_type.value,
            "label": node.label,
            "normalized_label": node.normalized_label,
            "entity_id": node.entity_id,
            "document_id": node.document_id,
            "question_id": node.question_id,
            "syllabus_id": node.syllabus_id,
            "metadata": node.metadata,
        }

    def _edge_payload(self, edge: Any) -> dict[str, Any]:
        return {
            "edge_id": edge.edge_id,
            "source_node_id": edge.source_node_id,
            "target_node_id": edge.target_node_id,
            "relationship": edge.relationship.value,
            "status": edge.status.value,
            "confidence": edge.confidence,
            "method": edge.method,
            "evidence": edge.evidence[:4],
            "source_document_id": edge.source_document_id,
            "source_page": edge.source_page,
            "metadata": edge.metadata,
        }

    def _learning_path(self, course_id: str, body: dict[str, Any]) -> ApiResponse:
        target = body.get("target_node_id")
        if not isinstance(target, str) or not target:
            raise ApiError(400, "missing_target", "target_node_id is required")
        known = body.get("known_concept_ids") or []
        if not isinstance(known, list) or not all(isinstance(item, str) for item in known):
            raise ApiError(400, "invalid_request", "known_concept_ids must be a list of strings")
        request = LearningPathRequest(
            course_id=course_id,
            target_node_id=target,
            target_node_type=GraphNodeType.CONCEPT,
            known_concept_ids=known,
            include_candidates=bool(body.get("include_candidates", False)),
            include_inferred=bool(body.get("include_inferred", False)),
            max_depth=int(body.get("max_depth", 8)),
        )
        with self._db() as database:
            plan = LearningPathService(
                GraphService(database, self.settings.graph), self.settings.prerequisite
            ).generate(request)
        return ApiResponse(200, _dump(plan))

    # -- generation --------------------------------------------------------
    def _generated(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        request = GenerationQuery(
            course_id=course_id,
            syllabus_id=_query_one(query, "syllabus_id"),
            batch_id=_query_one(query, "batch_id"),
            concept_id=_query_one(query, "concept_id"),
            format=_query_enum(query, "format", GenerationFormat),
            review_status=_query_enum(query, "review_status", GenerationReviewStatus),
            limit=_query_int(query, "limit", 50, minimum=1, maximum=200),
        )
        with self._db() as database:
            service = QuestionGenerationService(
                database,
                self.settings.generation,
                understanding_settings=self.settings.understanding,
            )
            questions = service.list_questions(request)
        return {
            "questions": [_dump(item) for item in questions],
            "total": len(questions),
        }

    def _generation_options(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        """Resolve the exact concept identities the generator accepts for this scope.

        The tutor and coverage services use their own concept IDs. The generator
        merges Phase 2 concepts, graph nodes and syllabus topics, so its accepted
        IDs cannot be guessed in the browser. This endpoint exposes the resolved
        scope instead of duplicating that merge in dashboard code.
        """

        requested_syllabus = _query_one(query, "syllabus_id")
        with self._db() as database, database.session() as session:
            if CourseRepository(session).get(course_id) is None:
                raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            syllabi = SyllabusRepository(session).get_by_course(course_id)
            if not syllabi:
                raise ApiError(
                    409,
                    "no_syllabus",
                    "generation needs a parsed syllabus with units and concepts",
                    {"course_id": course_id},
                )
            if requested_syllabus:
                syllabus = next(
                    (item for item in syllabi if item.syllabus_id == requested_syllabus), None
                )
                if syllabus is None:
                    raise ApiError(
                        404,
                        "unknown_syllabus",
                        "that syllabus version does not belong to this course",
                        {
                            "syllabus_id": requested_syllabus,
                            "available": [item.syllabus_id for item in syllabi],
                        },
                    )
            elif len(syllabi) > 1:
                raise ApiError(
                    409,
                    "ambiguous_syllabus",
                    "this course has several syllabus versions; choose one explicitly",
                    {"available": [item.syllabus_id for item in syllabi]},
                )
            else:
                syllabus = syllabi[0]
            units = SyllabusRepository(session).list_units(syllabus.syllabus_id)
            if not units:
                raise ApiError(
                    409,
                    "empty_syllabus",
                    "the selected syllabus has no parsed units",
                    {"syllabus_id": syllabus.syllabus_id},
                )
            first_unit_id = units[0].unit_id

        with self._db() as database:
            service = QuestionGenerationService(
                database,
                self.settings.generation,
                understanding_settings=self.settings.understanding,
            )
            scope = service.load_scope(
                GenerationRequest(
                    course_id=course_id,
                    syllabus_id=syllabus.syllabus_id,
                    unit_id=first_unit_id,
                )
            )
            concepts: list[dict[str, Any]] = [
                {
                    "concept_id": item.concept_id,
                    "label": item.label,
                    "unit_id": item.unit_id,
                    "unit_label": item.unit_label,
                    "unit_title": item.unit_title,
                    "verified": item.verified,
                    "aliases": list(item.aliases),
                }
                for item in scope.concepts()
            ]
            unit_counts: dict[str, int] = {}
            for item in concepts:
                unit_counts[item["unit_id"]] = unit_counts.get(item["unit_id"], 0) + 1
            unit_payload = [
                {
                    "unit_id": unit.unit_id,
                    "unit_label": unit.unit_label,
                    "unit_title": unit.title,
                    "concept_count": unit_counts.get(unit.unit_id, 0),
                }
                for unit in scope.units
            ]
        return {
            "course_id": course_id,
            "syllabus_id": scope.syllabus_id,
            "concept_count": len(concepts),
            "concepts": concepts,
            "units": unit_payload,
            "providers": [
                {
                    **service.describe_provider(name),
                    "name": name,
                    "resolved_name": service.describe_provider(name).get("name"),
                }
                for name in ("template", "auto")
            ],
            "formats": service.capabilities(),
        }

    def _generate(self, course_id: str, body: dict[str, Any]) -> ApiResponse:
        if not body.get("concept_ids") and not body.get("unit_id"):
            raise ApiError(
                400,
                "missing_target",
                "provide concept_ids or unit_id; concepts are never guessed",
            )
        payload = dict(body)
        payload["course_id"] = course_id
        persist = bool(payload.pop("persist", True))
        request = GenerationRequest(**payload)
        with self._db() as database:
            batch = QuestionGenerationService(
                database,
                self.settings.generation,
                understanding_settings=self.settings.understanding,
            ).generate(request, persist=persist)
        return ApiResponse(201 if persist else 200, _dump(batch))

    def _review(self, course_id: str, question_id: str, body: dict[str, Any]) -> ApiResponse:
        status = body.get("status")
        if not isinstance(status, str):
            raise ApiError(400, "invalid_request", "status is required")
        try:
            review_status = GenerationReviewStatus(status)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in GenerationReviewStatus)
            raise ApiError(400, "invalid_request", f"status must be one of: {allowed}") from exc
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise ApiError(400, "invalid_request", "note must be a string")
        reviewer = body.get("reviewer") or "dashboard-user"
        if not isinstance(reviewer, str) or len(reviewer) > 80:
            raise ApiError(400, "invalid_request", "reviewer must be a string of at most 80 chars")
        expected = body.get("expected_revision")
        if expected is not None and not isinstance(expected, int):
            raise ApiError(400, "invalid_request", "expected_revision must be an integer")
        with self._db() as database:
            service = QuestionGenerationService(
                database,
                self.settings.generation,
                understanding_settings=self.settings.understanding,
            )
            existing = service.get_question(question_id)
            if existing.course_id != course_id:
                raise ApiError(
                    404,
                    "unknown_generated_question",
                    "that generated question does not belong to this course",
                )
            updated = service.set_review_status(
                question_id,
                review_status,
                note=note[:500] if note else None,
                reviewer=reviewer,
                expected_revision=expected,
            )
        return ApiResponse(200, _dump(updated))

    def _grade(self, course_id: str, question_id: str, body: dict[str, Any]) -> ApiResponse:
        answer = body.get("answer", "")
        option_key = body.get("option_key")
        if not isinstance(answer, str):
            raise ApiError(400, "invalid_request", "answer must be a string")
        if option_key is not None and not isinstance(option_key, str):
            raise ApiError(400, "invalid_request", "option_key must be a string")
        if len(answer) > MAX_ANSWER_CHARS:
            raise ApiError(
                400, "invalid_request", f"answer must be at most {MAX_ANSWER_CHARS} characters"
            )
        if not answer.strip() and not option_key:
            raise ApiError(400, "answer_required", "write an answer or choose an option")
        with self._db() as database:
            question = QuestionGenerationService(
                database,
                self.settings.generation,
                understanding_settings=self.settings.understanding,
            ).get_question(question_id)
            if question.course_id != course_id:
                raise ApiError(
                    404,
                    "unknown_generated_question",
                    "that generated question does not belong to this course",
                )
            result = grade_answer(question, answer=answer, option_key=option_key)
        return ApiResponse(200, {"question_id": question_id, **result})

    # -- advanced assessment -----------------------------------------------
    def _advanced(self, course_id: str) -> dict[str, Any]:
        with self._db() as database:
            with database.session() as session:
                if CourseRepository(session).get(course_id) is None:
                    raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            service = AdvancedAssessmentService(database, self.settings)
            capabilities = service.capabilities()
            mutation = service.list_variants(
                course_id, origin=AssessmentOrigin.MUTATION, limit=1000
            )
            adversarial = service.list_variants(
                course_id, origin=AssessmentOrigin.ADVERSARIAL, limit=1000
            )
        return {
            "course_id": course_id,
            "capabilities": capabilities,
            "counts": {"mutation": len(mutation), "adversarial": len(adversarial)},
            "note": (
                "Variants and challenges are derived review material. Validation is not "
                "proof of educational correctness and nothing is approved automatically."
            ),
        }

    def _mutate(self, course_id: str, body: dict[str, Any]) -> ApiResponse:
        payload = dict(body)
        payload["course_id"] = course_id
        request = MutationRequest(**payload)
        with self._db() as database:
            batch = AdvancedAssessmentService(database, self.settings).mutate(request)
        return ApiResponse(201 if request.persist else 200, _dump(batch))

    def _adversarial(self, course_id: str, body: dict[str, Any]) -> ApiResponse:
        payload = dict(body)
        payload["course_id"] = course_id
        request = AdversarialRequest(**payload)
        with self._db() as database:
            batch = AdvancedAssessmentService(database, self.settings).generate_adversarial(request)
        return ApiResponse(201 if request.persist else 200, _dump(batch))

    # -- misconceptions ----------------------------------------------------
    def _misconceptions(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        request = MisconceptionQuery(
            course_id=course_id,
            status=_query_enum(query, "status", MisconceptionStatus),
            kind=_query_enum(query, "kind", MisconceptionKind),
            concept_id=_query_one(query, "concept_id"),
            min_support=_query_int(query, "min_support", 0, maximum=1000),
            limit=_query_int(query, "limit", 100, minimum=1, maximum=1000),
        )
        with self._db() as database:
            rows = AdvancedAssessmentService(database, self.settings).list_misconceptions(request)
        return {"misconceptions": [_dump(item) for item in rows], "total": len(rows)}

    def _analyse_misconceptions(self, course_id: str, body: dict[str, Any]) -> ApiResponse:
        syllabus_id = body.get("syllabus_id")
        if syllabus_id is not None and not isinstance(syllabus_id, str):
            raise ApiError(400, "invalid_request", "syllabus_id must be a string")
        with self._db() as database:
            report = AdvancedAssessmentService(database, self.settings).analyse_misconceptions(
                course_id, syllabus_id=syllabus_id
            )
        return ApiResponse(200, _dump(report))

    # -- analysis jobs -----------------------------------------------------
    def _start_analysis(self, course_id: str, kind: str) -> ApiResponse:
        specs = {
            "nlp": _JobSpec("Text analysis", self._nlp_operation),
            "understanding": _JobSpec("Question understanding", self._understanding_operation),
            "graph": _JobSpec("Knowledge graph", self._graph_operation),
            "prerequisites": _JobSpec("Prerequisite discovery", self._prerequisite_operation),
        }
        spec = specs.get(kind)
        if spec is None:
            raise ApiError(
                404,
                "unknown_analysis",
                f"analysis kind must be one of: {', '.join(sorted(specs))}",
            )
        with self._db() as database, database.session() as session:
            if CourseRepository(session).get(course_id) is None:
                raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
        job, created = self.jobs.submit(
            course_id=course_id,
            kind=kind,
            label=spec.label,
            operation=lambda database: spec.operation(database, course_id),
        )
        return ApiResponse(202 if created else 200, {"job": job.as_dict(), "created": created})

    def _nlp_operation(self, database: Database, course_id: str) -> dict[str, Any]:
        report = NlpService(database, self.settings.nlp).analyze_course(course_id)
        return {
            "run_id": getattr(report, "run_id", None),
            "status": getattr(getattr(report, "status", None), "value", "unknown"),
            "totals": getattr(report, "totals", {}),
            "duration_ms": getattr(report, "duration_ms", None),
        }

    def _understanding_operation(self, database: Database, course_id: str) -> dict[str, Any]:
        report = UnderstandingService(database, self.settings.understanding).analyze_course(
            course_id
        )
        payload = _dump(report)
        return payload if isinstance(payload, dict) else {"status": "unknown"}

    def _graph_operation(self, database: Database, course_id: str) -> dict[str, Any]:
        report = GraphService(database, self.settings.graph).build_course(course_id)
        payload = _dump(report)
        return payload if isinstance(payload, dict) else {"status": "unknown"}

    def _prerequisite_operation(self, database: Database, course_id: str) -> dict[str, Any]:
        report = PrerequisiteDiscoveryService(
            GraphService(database, self.settings.graph), self.settings.prerequisite
        ).discover(PrerequisiteDiscoveryRequest(course_id=course_id))
        return {
            "course_id": report.course_id,
            "edges_created": report.edges_created,
            "edges_updated": report.edges_updated,
            "candidates": len(report.candidates),
            "conflicts": len(report.conflicts),
            "cycles": len(report.cycles),
        }

    # -- tutor -------------------------------------------------------------
    def _tutor_context(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        syllabus_id = _query_one(query, "syllabus_id")
        limit = _query_int(query, "limit", 12, minimum=1, maximum=100)
        with self._db() as database:
            return TutorService(database, self.settings).context(
                course_id, syllabus_id=syllabus_id, limit=limit
            )

    def _tutor_ask(self, course_id: str, body: dict[str, Any]) -> ApiResponse:
        question, concept_id, syllabus_id = _tutor_ask_arguments(body)
        with self._db() as database:
            answer = TutorService(database, self.settings).ask(
                course_id,
                question,
                concept_id=concept_id,
                syllabus_id=syllabus_id,
            )
        return ApiResponse(200, answer)

    def parse_tutor_ask_stream_request(self, body: bytes) -> dict[str, Any]:
        """Parse and validate a stream request before SSE headers are sent."""

        payload = self._json_body(body)
        _tutor_ask_arguments(payload)
        return payload

    def stream_tutor_ask(
        self,
        course_id: str,
        body: dict[str, Any],
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        question, concept_id, syllabus_id = _tutor_ask_arguments(body)
        with self._db() as database:
            return TutorService(database, self.settings).ask(
                course_id,
                question,
                concept_id=concept_id,
                syllabus_id=syllabus_id,
                on_event=on_event,
            )

    # -- dataset recovery --------------------------------------------------
    def _recovery_overview(self, query: dict[str, list[str]]) -> dict[str, Any]:
        from exam_engine.recovery.archives import load_archive_specs
        from exam_engine.recovery.crawl4ai_adapter import Crawl4AIHealth

        with self._db() as database, database.session() as session:
            payload = RecoveryRepository(session).overview(_query_one(query, "run_id"))
        payload["crawl4ai"] = Crawl4AIHealth.check(force=False)
        payload["official_archives"] = [spec.status() for spec in load_archive_specs()]
        return payload

    def _recovery_courses(self, query: dict[str, list[str]]) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            rows = RecoveryRepository(session).list_courses(_query_one(query, "run_id"))
        return {"courses": rows, "total": len(rows)}

    def _recovery_course(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = _query_int(query, "limit", 50, minimum=1, maximum=200)
        offset = _query_int(query, "offset", 0, maximum=100000)
        with self._db() as database, database.session() as session:
            payload = RecoveryRepository(session).course_detail(
                course_id,
                run_id=_query_one(query, "run_id"),
                limit=limit,
                offset=offset,
            )
        if payload is None:
            raise ApiError(404, "unknown_recovery_course", f"no recovery report for {course_id!r}")
        return payload

    def _recovery_record(self, record_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            payload = RecoveryRepository(session).record_detail(
                record_id, run_id=_query_one(query, "run_id")
            )
        if payload is None:
            raise ApiError(404, "unknown_recovery_record", f"no recovery record {record_id!r}")
        return payload

    def _recovery_providers(self) -> dict[str, Any]:
        from exam_engine.recovery.archives import load_archive_specs
        from exam_engine.recovery.crawl4ai_adapter import Crawl4AIHealth
        from exam_engine.recovery.providers import provider_descriptions

        rows = provider_descriptions(self.settings.tutor)
        rows.append(
            {
                "name": "official_archive",
                "available": bool(load_archive_specs()),
                "configured": bool(load_archive_specs()),
                "kind": "official_media_archive",
                "detail": (
                    "Configured official WordPress media archives; "
                    "PDFs are retrieved separately."
                ),
            }
        )
        rows.append(
            {
                "name": "crawl4ai",
                "available": Crawl4AIHealth.quick_status().get("installed"),
                "configured": True,
                "kind": "browser_crawler",
                "detail": "Crawl4AI renders HTML; it is not a search engine.",
            }
        )
        return {
            "providers": rows,
            "total": len(rows),
            "official_archives": [spec.status() for spec in load_archive_specs()],
            "crawl4ai": Crawl4AIHealth.quick_status(),
        }

    def _recovery_sources(self, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = _query_int(query, "limit", 200, minimum=1, maximum=2000)
        with self._db() as database, database.session() as session:
            rows = RecoveryRepository(session).list_sources(
                _query_one(query, "run_id"),
                course_id=_query_one(query, "course"),
                limit=limit,
            )
        return {"sources": rows, "total": len(rows)}

    def _recovery_issues(self, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = _query_int(query, "limit", 100, minimum=1, maximum=1000)
        offset = _query_int(query, "offset", 0, maximum=100000)
        with self._db() as database, database.session() as session:
            rows = RecoveryRepository(session).list_issues(
                run_id=_query_one(query, "run_id"),
                course_id=_query_one(query, "course"),
                severity=_query_one(query, "severity"),
                limit=limit,
                offset=offset,
            )
        return {"issues": rows, "total": len(rows), "limit": limit, "offset": offset}

    def _recovery_run_status(self, run_id: str) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            repo = RecoveryRepository(session)
            run = repo.get_run(run_id)
            if run is None:
                raise ApiError(404, "unknown_recovery_run", f"no recovery run {run_id!r}")
            return {"run": run, "courses": repo.list_courses(run_id)}

    def _recovery_export(self, course_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        with self._db() as database, database.session() as session:
            rows = RecoveryRepository(session).release_records(
                course_id, run_id=_query_one(query, "run_id")
            )
        return {"course_id": course_id, "released_records": rows, "total": len(rows)}

    def _recovery_run(self, body: dict[str, Any]) -> ApiResponse:
        root = body.get("root") or "datasets"
        if not isinstance(root, str):
            raise ApiError(400, "invalid_request", "root must be a string")
        csv_path = body.get("csv_path")
        if csv_path is not None and not isinstance(csv_path, str):
            raise ApiError(400, "invalid_request", "csv_path must be a string")
        course_ids = body.get("course_ids") or []
        category_ids = body.get("category_ids") or []
        if not isinstance(course_ids, list) or not all(
            isinstance(item, str) for item in course_ids
        ):
            raise ApiError(400, "invalid_request", "course_ids must be a list of strings")
        if not isinstance(category_ids, list) or not all(
            isinstance(item, str) for item in category_ids
        ):
            raise ApiError(400, "invalid_request", "category_ids must be a list of strings")
        mode = str(body.get("mode") or "auto")
        if mode not in {"auto", "review", "audit"}:
            raise ApiError(400, "invalid_request", "mode must be auto, review or audit")
        workflow = str(body.get("workflow") or "full")
        if workflow not in {"inventory", "analyze", "recover", "verify", "reconstruct", "full"}:
            raise ApiError(
                400,
                "invalid_request",
                "workflow must be inventory, analyze, recover, verify, reconstruct or full",
            )
        with self._db() as database:
            result = DatasetRecoveryService(database, self.settings).analyze(
                root,
                course_ids=course_ids or None,
                category_ids=category_ids or None,
                mode=mode,
                workflow=workflow,
                dry_run=bool(body.get("dry_run", True)),
                use_web=bool(body.get("use_web", False)),
                use_model=bool(body.get("use_model", False)),
                provider=str(body.get("provider") or "auto"),
                fetch_sources=bool(body.get("fetch_sources", False)),
                crawl_engine=str(body.get("crawl_engine") or "crawl4ai"),
                retrieval_limit=max(0, min(int(body.get("retrieval_limit", 3)), 8)),
                retrieval_follow_links=bool(body.get("retrieval_follow_links", True)),
                retry_failed=bool(body.get("retry_failed", False)),
                web_limit=max(0, int(body.get("web_limit", 0))),
                source_limit=max(0, int(body.get("source_limit", 0))),
                model_limit=max(0, int(body.get("model_limit", 0))),
                run_id=body.get("run_id") if isinstance(body.get("run_id"), str) else None,
                incremental=bool(body.get("incremental", False)),
                csv_path=csv_path,
                course_id=(
                    body.get("course_id") if isinstance(body.get("course_id"), str) else None
                ),
                course_name=(
                    body.get("course_name") if isinstance(body.get("course_name"), str) else None
                ),
                course_code=(
                    body.get("course_code") if isinstance(body.get("course_code"), str) else None
                ),
                category_id=str(body.get("category_id") or "custom"),
            )
        return ApiResponse(200, result.as_dict(include_courses=True))

    def _recovery_crawl(self, body: dict[str, Any]) -> ApiResponse:
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ApiError(400, "invalid_request", "url must be a public http(s) URL")
        from exam_engine.recovery.crawl4ai_adapter import Crawl4AIFetcher

        fetcher = Crawl4AIFetcher(timeout=float(body.get("timeout") or 30.0))
        try:
            page = fetcher.crawl(url)
        finally:
            fetcher.close()
        return ApiResponse(
            200,
            {
                "url": page.url,
                "final_url": page.final_url,
                "status_code": page.status_code,
                "title": page.title,
                "canonical_url": page.canonical_url,
                "markdown": page.markdown[:50000],
                "links": page.links[:200],
                "tables": page.tables[:20],
                "metadata": page.metadata,
                "elapsed_ms": page.elapsed_ms,
            },
        )

    def _recovery_review(self, body: dict[str, Any]) -> ApiResponse:
        repair_id = body.get("repair_id")
        if not isinstance(repair_id, int):
            raise ApiError(400, "invalid_request", "repair_id must be an integer")
        status = body.get("status")
        if status not in {"approved", "rejected"}:
            raise ApiError(400, "invalid_request", "status must be approved or rejected")
        reviewer = body.get("reviewer") or "dashboard-user"
        if not isinstance(reviewer, str) or len(reviewer) > 80:
            raise ApiError(400, "invalid_request", "reviewer must be at most 80 characters")
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise ApiError(400, "invalid_request", "note must be a string")
        with self._db() as database, database.transaction() as session:
            payload = RecoveryRepository(session).review_repair(
                repair_id,
                status=status,
                reviewer=reviewer,
                note=note,
                edited_after=body.get("edited_after"),
            )
        if payload is None:
            raise ApiError(404, "unknown_recovery_repair", f"no repair {repair_id!r}")
        return ApiResponse(200, {"record": payload})

    def _recovery_rollback(self, body: dict[str, Any]) -> ApiResponse:
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ApiError(400, "invalid_request", "run_id is required")
        if body.get("confirm") is not True:
            raise ApiError(400, "confirmation_required", "set confirm=true to roll back")
        with self._db() as database, database.transaction() as session:
            ok = RecoveryRepository(session).rollback(run_id)
        if not ok:
            raise ApiError(404, "unknown_recovery_run", f"no recovery run {run_id!r}")
        return ApiResponse(200, {"run_id": run_id, "status": "rolled_back"})

    # -- course/document writes -------------------------------------------
    def _create_course(self, body: dict[str, Any]) -> ApiResponse:
        try:
            course = CourseRecord(**body)
        except ValidationError:
            raise
        with self._db() as database, database.transaction() as session:
            repo = CourseRepository(session)
            conflicts = repo.conflicting_offerings(course)
            if conflicts:
                raise ApiError(
                    409,
                    "course_conflict",
                    (
                        "another course id already uses this institution, code, academic "
                        "year and syllabus version"
                    ),
                    {"conflicting_course_ids": conflicts},
                )
            saved, created = repo.upsert(course)
        return ApiResponse(
            201 if created else 200,
            {
                "created": created,
                "course": {
                    "course_id": saved.course_id,
                    "course_code": saved.course_code,
                    "title": saved.title,
                    "institution_code": saved.institution_code,
                    "institution_name": saved.institution_name,
                    "academic_year": saved.academic_year,
                    "syllabus_version": saved.syllabus_version,
                },
            },
        )

    def ingest_upload(
        self,
        course_id: str,
        filename: str,
        data: bytes,
        fields: dict[str, str],
    ) -> ApiResponse:
        safe_name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not safe_name or safe_name in {".", ".."} or "\x00" in safe_name:
            raise ApiError(400, "invalid_filename", "the uploaded file needs a name")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in FORMAT_BY_SUFFIX:
            raise ApiError(
                400,
                "unsupported_format",
                f"unsupported file type {suffix or '(none)'}; supported: "
                + ", ".join(sorted(FORMAT_BY_SUFFIX)),
            )
        if not data:
            raise ApiError(400, "empty_file", "the uploaded file is empty")
        if len(data) > self.settings.limits.max_file_bytes:
            raise ApiError(
                413,
                "file_too_large",
                f"file exceeds the {self.settings.limits.max_file_bytes} byte limit",
                {"byte_size": len(data), "limit": self.settings.limits.max_file_bytes},
            )
        try:
            kind = DocumentKind(fields.get("kind", "other"))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in DocumentKind)
            raise ApiError(400, "invalid_field", f"kind must be one of: {allowed}") from exc
        try:
            dataset_type = DatasetType(fields.get("dataset_type", "unknown"))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in DatasetType)
            raise ApiError(400, "invalid_field", f"dataset_type must be one of: {allowed}") from exc
        try:
            duplicate_policy = DuplicatePolicy(fields.get("duplicate_policy", "skip"))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in DuplicatePolicy)
            raise ApiError(
                400, "invalid_field", f"duplicate_policy must be one of: {allowed}"
            ) from exc
        source_id = (fields.get("source_id") or "dashboard-upload").strip()
        if not source_id:
            raise ApiError(400, "invalid_field", "source_id must not be empty")
        license_text = fields.get("license") or (
            "Synthetic data generated for development testing."
            if dataset_type is DatasetType.SYNTHETIC
            else None
        )
        source = DatasetSource(
            source_id=source_id,
            name=fields.get("source_name") or source_id,
            dataset_type=dataset_type,
            retrieved_at=date.today(),
            license=license_text,
            usage_terms=fields.get("usage_terms") or None,
            source_url=fields.get("source_url") or None,
            publisher=fields.get("publisher") or None,
            notes="Uploaded through the Phase 12 dashboard.",
            external_dataset=True,
        )
        with self._db() as database:
            with database.session() as session:
                course = CourseRepository(session).get(course_id)
                if course is None:
                    raise ApiError(404, "unknown_course", f"no course {course_id!r} exists")
            with tempfile.TemporaryDirectory(prefix="exam-engine-upload-") as directory:
                path = Path(directory) / safe_name
                path.write_bytes(data)
                result = IngestionPipeline(database, settings=self.settings).ingest_file(
                    path,
                    course=course,
                    source=source,
                    kind=kind,
                    title=(fields.get("title") or safe_name),
                    metadata={"uploaded_via": "phase12_dashboard"},
                    duplicate_policy=duplicate_policy,
                )
        payload = _dump(result)
        status = getattr(getattr(result, "status", None), "value", "failed")
        if status in {"failed", "empty"}:
            message = "the document was not ingested"
            if result.errors:
                message = result.errors[0].message
            return ApiResponse(
                422,
                {
                    "error": {
                        "code": "ingestion_failed" if status == "failed" else "ingestion_empty",
                        "message": message,
                        "detail": {"status": status},
                    },
                    "result": payload,
                },
            )
        return ApiResponse(201 if status in {"ingested", "replaced"} else 200, {"result": payload})

    # -- request helpers ---------------------------------------------------
    @staticmethod
    def _view(query: dict[str, list[str]]) -> str:
        view = _query_one(query, "view", "summary") or "summary"
        if view not in {"summary", "full"}:
            raise ApiError(400, "invalid_query", "view must be 'summary' or 'full'")
        return view

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError(413, "request_too_large", "request body is too large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", "request body must be UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")
        return payload


def _query_float(query: dict[str, list[str]], key: str) -> float | None:
    raw = _query_one(query, key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_query", f"{key} must be a number") from exc


def _query_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = _query_one(query, key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().casefold()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ApiError(400, "invalid_query", f"{key} must be true or false")


__all__ = ["ApiResponse", "DashboardAPI"]
