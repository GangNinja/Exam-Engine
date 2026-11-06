"""Phase 10 question generation.

The service grounds every request in the stored corpus before anything is
generated:

1. the course must exist, and the syllabus version must belong to it (an
   ambiguous or foreign syllabus is refused, never guessed);
2. target concepts are resolved from Phase 2 concepts, the Phase 5 graph and
   explicit syllabus topics, inside the selected syllabus only;
3. evidence passages come from the syllabus and notes/other documents -
   question papers and question banks are never used as sources;
4. the chosen provider produces a draft, which is assembled with Phase 3
   type/Bloom/difficulty estimates and every piece of provenance;
5. a separate validation layer decides whether the result is validated,
   reviewable or rejected;
6. Phase 3 similarity decides whether the candidate duplicates an existing
   generated or historical question.

Generated questions live in their own tables; historical questions are read and
never modified. Nothing here predicts real examination content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exam_engine.config import GenerationSettings, UnderstandingSettings
from exam_engine.generation import dedupe
from exam_engine.generation.providers import (
    GenerationProvider,
    ProviderError,
    build_provider,
)
from exam_engine.generation.templates import (
    DEFAULT_BLOOM,
    DEFAULT_DIFFICULTY,
    DEFAULT_MARKS,
    MODEL_ONLY_FORMATS,
    OBJECTIVE_VERBS,
    format_support,
    template_for,
)
from exam_engine.generation.validation import (
    ValidationContext,
    validate_question,
    warnings_from_validation,
)
from exam_engine.models.course import CourseRecord, utcnow
from exam_engine.models.document import DocumentRecord
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    DocumentKind,
    GenerationFormat,
    GenerationReviewStatus,
    GenerationStatus,
    GenerationValidationStatus,
    GraphNodeType,
    GraphRelationship,
    Severity,
)
from exam_engine.models.generation import (
    GENERATION_ALGORITHM,
    GENERATION_ALGORITHM_VERSION,
    GENERATION_SCHEMA_VERSION,
    DuplicateResolution,
    GeneratedOption,
    GeneratedQuestion,
    GenerationBatch,
    GenerationCounts,
    GenerationPrompt,
    GenerationQuery,
    GenerationRejection,
    GenerationRequest,
    GenerationScope,
    ReviewEvent,
    SourcePassage,
    ValidationResult,
)
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import SyllabusRecord, SyllabusUnitRecord
from exam_engine.nlp.keywords import normalize_term
from exam_engine.nlp.text import STOPWORDS
from exam_engine.storage.db import Database
from exam_engine.storage.generation_repository import GenerationRepository
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.nlp_repositories import ConceptRepository
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    SyllabusRepository,
)
from exam_engine.understanding.classification import (
    classify_bloom,
    classify_question_types,
    estimate_difficulty,
)
from exam_engine.understanding.mapping import SemanticIndex
from exam_engine.understanding.similarity import ReasoningProfile
from exam_engine.understanding.structure import detect_structure
from exam_engine.understanding.taxonomy import (
    DEFAULT_TAXONOMY,
    Taxonomy,
    TaxonomyError,
    load_taxonomy,
)

logger = logging.getLogger(__name__)

#: Document kinds that may ground a generated question.
GROUNDING_KINDS = frozenset({DocumentKind.SYLLABUS, DocumentKind.NOTES, DocumentKind.OTHER})
#: Document kinds that are examination material and must never be a source.
EXAM_KINDS = frozenset({DocumentKind.QUESTION_PAPER, DocumentKind.QUESTION_BANK})

#: Upper bound on sentence indexing per request; keeps a huge corpus bounded.
MAX_INDEXED_SENTENCES = 50_000
MIN_SENTENCE_CHARS = 30

TYPE_METHOD = "phase3_cue_rules"
BLOOM_METHOD = "phase3_cue_rules"
DIFFICULTY_METHOD = "phase3_observable_features"


class GenerationError(ValueError):
    """Structured request/scope failure for the CLI and other callers."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


# -- loaded context ------------------------------------------------------------


@dataclass(frozen=True)
class _Concept:
    """One resolvable syllabus concept with its unit and provenance."""

    concept_id: str
    label: str
    normalized: str
    unit_id: str
    unit_label: str
    unit_title: str | None
    origin: str
    aliases: tuple[str, ...] = ()
    graph_node_ids: tuple[str, ...] = ()
    verified: bool = False

    def as_metadata(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "label": self.label,
            "unit_id": self.unit_id,
            "origin": self.origin,
            "verified": self.verified,
            "graph_node_ids": list(self.graph_node_ids),
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class _Sentence:
    document: DocumentRecord
    page_number: int | None
    text: str
    priority: int


@dataclass
class _Loaded:
    course: CourseRecord
    syllabus: SyllabusRecord
    units: list[SyllabusUnitRecord]
    concepts: list[_Concept]
    by_id: dict[str, _Concept]
    by_label: dict[str, list[_Concept]]
    unit_concepts: dict[str, list[str]]
    related: dict[str, list[str]]
    documents: dict[str, DocumentRecord]
    sentences: list[_Sentence]
    historical: list[QuestionRecord]
    existing: dict[str, GeneratedQuestion]
    warnings: list[Uncertainty] = field(default_factory=list)

    def concept_for(self, ref: str) -> _Concept:
        concept = self.by_id.get(ref)
        if concept is not None:
            return concept
        matches = self.by_label.get(normalize_term(ref), [])
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise GenerationError(
                "unknown_concept",
                f"no concept in syllabus {self.syllabus.syllabus_id} matches {ref!r}; "
                "run 'exam-engine nlp analyze' or list concepts first",
                detail={"concept": ref, "syllabus_id": self.syllabus.syllabus_id},
            )
        raise GenerationError(
            "ambiguous_concept",
            f"{ref!r} matches several concepts; pass a concept id instead",
            detail={"concept": ref, "candidates": [item.concept_id for item in matches]},
        )


@dataclass(frozen=True)
class _Plan:
    bloom: BloomLevel
    difficulty: DifficultyLabel
    marks: float
    targets: tuple[_Concept, ...]
    reference_style: dict[str, Any]
    defaults_applied: dict[str, str]
    spares: tuple[_Concept, ...] = ()


# -- service -------------------------------------------------------------------


class QuestionGenerationService:
    """Coordinates Phase 10 generation, validation, dedupe and review."""

    def __init__(
        self,
        database: Database,
        settings: GenerationSettings | None = None,
        *,
        understanding_settings: UnderstandingSettings | None = None,
        provider: GenerationProvider | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        taxonomy: Taxonomy | None = None,
    ) -> None:
        self.db = database
        self.settings = settings or GenerationSettings()
        self.understanding_settings = understanding_settings or UnderstandingSettings()
        self._provider = provider
        self._env = env
        self._sleep = sleep
        self.taxonomy = taxonomy or self._load_taxonomy()

    def _load_taxonomy(self) -> Taxonomy:
        path = self.understanding_settings.taxonomy_path
        if not path:
            return DEFAULT_TAXONOMY.validated()
        try:
            return load_taxonomy(Path(path))
        except (TaxonomyError, OSError):
            return DEFAULT_TAXONOMY.validated()

    # -- capabilities ------------------------------------------------------

    def capabilities(self) -> list[dict[str, Any]]:
        provider = self._provider or self._build_provider("auto")
        rows: list[dict[str, Any]] = []
        for support in format_support():
            blocked = [
                level.value
                for level in support.blooms
                if not provider.supports(support.format, level)
            ]
            rows.append(
                {
                    **support.as_dict(),
                    "supported_by_provider": [
                        level.value
                        for level in support.blooms
                        if provider.supports(support.format, level)
                    ],
                    "blocked_by_provider": blocked,
                }
            )
        return rows

    def describe_provider(self, name: str = "auto") -> dict[str, Any]:
        return self._build_provider(name).describe()

    def load_scope(self, request: GenerationRequest) -> ScopeContext:
        """Resolve the course/syllabus/concept scope without generating anything.

        Phase 11 uses this instead of re-deriving concept identity, so a mutated or
        adversarial question names exactly the concepts Phase 10 knows, and its
        evidence and similarity pools come from the same place.
        """

        return ScopeContext(self, self._load(request))

    # -- generation --------------------------------------------------------

    def generate(
        self,
        request: GenerationRequest,
        *,
        provider: str | None = None,
        persist: bool = True,
    ) -> GenerationBatch:
        if request.count > self.settings.max_questions:
            raise GenerationError(
                "request_too_large",
                (
                    f"{request.count} questions requested but the configured limit is "
                    f"{self.settings.max_questions}"
                ),
                detail={"requested": request.count, "limit": self.settings.max_questions},
            )
        if request.language.strip().casefold() not in {"en", "eng", "english"}:
            raise GenerationError(
                "unsupported_language",
                "only English ('en') is supported by this phase's templates and taxonomy",
                detail={"language": request.language},
            )

        loaded = self._load(request)
        chosen = provider or request.provider
        generation_provider = self._provider or self._build_provider(chosen)
        plan = self._plan(request, loaded)
        template = template_for(request.format, plan.bloom)
        if not generation_provider.supports(request.format, plan.bloom):
            note = generation_provider.capability_note(request.format, plan.bloom)
            code = (
                "unsupported_format"
                if request.format in MODEL_ONLY_FORMATS
                else "unsupported_format_bloom"
            )
            raise GenerationError(
                code,
                note,
                detail={"format": request.format.value, "bloom": plan.bloom.value},
            )

        batch_id = self._batch_id(request, loaded, plan, generation_provider)
        scope = self._scope(plan, loaded)
        parameters = self._parameters(request, loaded, plan, generation_provider)
        warnings: list[Uncertainty] = list(loaded.warnings)
        selected_ids = {
            concept.concept_id
            for concept in loaded.concepts
            if concept.concept_id in request.concept_ids
        }
        expanded = [
            concept
            for concept in plan.targets
            if request.concept_ids and concept.concept_id not in selected_ids
        ]
        if request.expand_batch and expanded:
            warnings.append(
                Uncertainty(
                    code="generation_batch_expanded",
                    message=(
                        f"The batch continued across {len(expanded)} additional syllabus "
                        "concept(s) so each requested question is a distinct prompt."
                    ),
                    severity=Severity.INFO,
                    detail={
                        "selected": sorted(selected_ids),
                        "added": [item.concept_id for item in expanded],
                    },
                )
            )
        counts = GenerationCounts(requested=request.count)
        questions: list[GeneratedQuestion] = []
        rejections: list[GenerationRejection] = []
        duplicates: list[DuplicateResolution] = []
        kept_pool: list[dedupe.PoolItem] = []

        semantic = self._semantic_index(loaded)
        existing_pool = self._existing_pool(loaded)
        historical_pool = [self._historical_item(item) for item in loaded.historical]
        concept_cache: dict[str, list[SourcePassage]] = {}

        if request.expand_batch:
            attempts = [*plan.targets, *plan.spares]
        else:
            attempts = [plan.targets[index % len(plan.targets)] for index in range(request.count)]

        for index, concept in enumerate(attempts):
            if len(questions) >= request.count:
                break
            question_id = self._question_id(batch_id, concept, request, plan, index)
            approved = loaded.existing.get(question_id)
            if (
                approved is not None
                and approved.review_status is GenerationReviewStatus.APPROVED
                and not request.allow_overwrite_approved
            ):
                questions.append(approved)
                counts.generated += 1
                counts.reused_approved += 1
                warnings.append(
                    Uncertainty(
                        code="generation_reused_approved",
                        message=(
                            f"question {question_id} was already approved and was returned "
                            "unchanged; pass allow_overwrite_approved to regenerate it"
                        ),
                        severity=Severity.INFO,
                        detail={"question_id": question_id},
                    )
                )
                continue

            try:
                prompt = self._prompt(index, request, plan, loaded, concept, concept_cache)
                draft = generation_provider.generate(prompt)
            except ProviderError as exc:
                rejections.append(
                    GenerationRejection(
                        index=index,
                        concept_id=concept.concept_id,
                        concept_label=concept.label,
                        format=request.format,
                        code=exc.code,
                        message=exc.message,
                    )
                )
                counts.failed += 1
                continue

            counts.generated += 1
            question = self._assemble(
                question_id=question_id,
                batch_id=batch_id,
                request=request,
                plan=plan,
                loaded=loaded,
                concept=concept,
                prompt=prompt,
                question_text=draft.text,
                draft=draft,
            )
            if question.format is GenerationFormat.MULTIPLE_CHOICE:
                question.options = self._options(draft)
                correct = next((option for option in question.options if option.is_correct), None)
                question.correct_option_key = correct.key if correct else None
            question.validation = validate_question(
                question,
                ValidationContext(
                    settings=self.settings,
                    allowed_concept_ids=frozenset(loaded.by_id),
                    concept_labels={item.concept_id: item.label for item in loaded.concepts},
                    allowed_unit_ids=frozenset(unit.unit_id for unit in loaded.units),
                    forbidden_labels=frozenset(self._foreign_labels(loaded)),
                    evidence=tuple(question.source_references),
                    thin_evidence=self._thin_evidence(question),
                    phase3_available=question.estimated_bloom is not None,
                ),
            )
            question.warnings = warnings_from_validation(question.validation)
            counts.validated += int(
                question.validation.status is GenerationValidationStatus.VALIDATED
            )
            counts.needs_review += int(
                question.validation.status is GenerationValidationStatus.NEEDS_REVIEW
            )
            counts.validation_incomplete += int(
                question.validation.status is GenerationValidationStatus.VALIDATION_INCOMPLETE
            )
            if question.validation.status is GenerationValidationStatus.REJECTED:
                rejections.append(
                    GenerationRejection(
                        index=index,
                        concept_id=concept.concept_id,
                        concept_label=concept.label,
                        format=request.format,
                        code="validation_rejected",
                        message="validation rejected the generated question",
                        text=question.text,
                        issues=[
                            issue
                            for issue in question.validation.issues
                            if issue.severity is Severity.ERROR
                        ],
                    )
                )
                counts.rejected += 1
                continue

            findings = dedupe.check_candidate(
                question.text,
                candidate_id=question.question_id,
                # The candidate's own stored revision is an update, not a duplicate.
                pool=[
                    item
                    for item in [*kept_pool, *existing_pool, *historical_pool]
                    if item.question_id != question.question_id
                ],
                course_id=loaded.course.course_id,
                settings=dedupe.DedupeSettings(
                    near_threshold=self.settings.duplicate_near_threshold,
                    semantic_threshold=self.settings.duplicate_semantic_threshold,
                ),
                semantic=semantic,
                profile=ReasoningProfile(
                    question_type=question.question_type,
                    bloom_level=question.requested_bloom.value,
                ),
                concept_ids=tuple(question.concept_ids),
            )
            duplicate = next((item for item in findings if item.is_duplicate), None)
            if duplicate is not None:
                duplicates.append(
                    DuplicateResolution(
                        index=index,
                        concept_id=concept.concept_id,
                        kind=duplicate.kind,
                        matched_question_id=duplicate.matched_question_id,
                        score=duplicate.score,
                        reason=f"{duplicate.reason} (matched {duplicate.source} question)",
                    )
                )
                counts.duplicates += 1
                counts.rejected += 1
                rejections.append(
                    GenerationRejection(
                        index=index,
                        concept_id=concept.concept_id,
                        concept_label=concept.label,
                        format=request.format,
                        code=f"duplicate_{duplicate.kind}",
                        message=f"{duplicate.reason}: {duplicate.matched_question_id}",
                        text=question.text,
                    )
                )
                continue

            variant = next((item for item in findings if not item.is_duplicate), None)
            if variant is not None:
                duplicates.append(
                    DuplicateResolution(
                        index=index,
                        concept_id=concept.concept_id,
                        kind=variant.kind,
                        matched_question_id=variant.matched_question_id,
                        score=variant.score,
                        reason=f"{variant.reason} (matched {variant.source} question)",
                    )
                )
                question.generation_metadata["similar_to"] = {
                    "question_id": variant.matched_question_id,
                    "kind": variant.kind,
                    "score": variant.score,
                }

            question.generation_metadata["planned_template"] = (
                template.template_id if template else None
            )
            questions.append(question)
            kept_pool.append(
                dedupe.PoolItem(
                    question_id=question.question_id,
                    text=question.text,
                    source=dedupe.SOURCE_GENERATED,
                    concept_ids=tuple(question.concept_ids),
                    profile=ReasoningProfile(
                        question_type=question.question_type,
                        bloom_level=question.requested_bloom.value,
                    ),
                )
            )

        accepted = len(questions)
        if accepted == 0:
            status = GenerationStatus.FAILED
        elif accepted < request.count or counts.failed or counts.validation_incomplete:
            status = GenerationStatus.PARTIAL
        else:
            status = GenerationStatus.OK

        if counts.needs_review:
            # Batch status describes completeness only; review need is reported here so
            # a complete batch is not mislabelled partial, and nothing is hidden.
            warnings.append(
                Uncertainty(
                    code="generation_review_required",
                    message=(
                        f"{counts.needs_review} of {accepted} generated question(s) need human "
                        "review before use; the batch itself is complete"
                    ),
                    severity=Severity.INFO,
                    detail={"needs_review": counts.needs_review, "accepted": accepted},
                )
            )

        repeated = self._repeated_texts(questions)
        if repeated:
            warnings.append(
                Uncertainty(
                    code="generation_repetitive_batch",
                    message=(
                        "several questions in this batch share near-identical wording; "
                        "diversity within the batch is limited"
                    ),
                    severity=Severity.WARNING,
                    detail={"texts": repeated[:3]},
                )
            )
        if counts.failed:
            warnings.append(
                Uncertainty(
                    code="generation_partial_failure",
                    message=(
                        f"{counts.failed} of {request.count} candidates could not be generated; "
                        "see the batch rejections for the provider's reason"
                    ),
                    severity=Severity.WARNING,
                    detail={"failed": counts.failed, "requested": request.count},
                )
            )

        batch = GenerationBatch(
            batch_id=batch_id,
            course_id=loaded.course.course_id,
            status=status,
            request=request,
            provider=generation_provider.name,
            model=getattr(generation_provider, "model", None),
            resolved_bloom=plan.bloom,
            resolved_difficulty=plan.difficulty,
            reference_style=plan.reference_style,
            counts=counts,
            questions=questions,
            rejections=rejections,
            duplicates=duplicates,
            scope=scope,
            parameters=parameters,
            warnings=warnings,
            limitations=[
                "Generated questions are proposed practice material, not verified "
                "examination content.",
                "Automated validation cannot prove factual or educational correctness; "
                "a human review step is required before use.",
                "Difficulty and Bloom labels are heuristic estimates from wording and "
                "structure, not calibrated measurements.",
                "Grounding is limited to the documents present in this course; gaps in the "
                "corpus become gaps in the questions.",
            ],
        )
        batch.counts.persisted = 0
        if persist and questions:
            self._persist(batch)
        elif persist:
            with self.db.transaction() as session:
                GenerationRepository(session).upsert_batch(batch)
        return batch

    # -- provider ----------------------------------------------------------

    def _build_provider(self, name: str) -> GenerationProvider:
        try:
            return build_provider(name, self.settings, env=self._env, sleep=self._sleep)
        except ProviderError as exc:
            raise GenerationError(exc.code, exc.message, detail=exc.detail) from exc

    # -- loading -----------------------------------------------------------

    def _load(self, request: GenerationRequest) -> _Loaded:
        warnings: list[Uncertainty] = []
        with self.db.session() as session:
            course = CourseRepository(session).get(request.course_id)
            if course is None:
                raise GenerationError(
                    "unknown_course",
                    f"no course {request.course_id!r} exists in this database",
                    detail={"course_id": request.course_id},
                )
            syllabus_repo = SyllabusRepository(session)
            syllabi = syllabus_repo.get_by_course(course.course_id)
            if not syllabi:
                raise GenerationError(
                    "no_syllabus",
                    (
                        f"course {course.course_id} has no parsed syllabus, so no concept can be "
                        "grounded; ingest a syllabus first"
                    ),
                    detail={"course_id": course.course_id},
                )
            if request.syllabus_id:
                syllabus = next(
                    (item for item in syllabi if item.syllabus_id == request.syllabus_id), None
                )
                if syllabus is None:
                    raise GenerationError(
                        "unknown_syllabus",
                        (
                            f"syllabus {request.syllabus_id!r} does not belong to course "
                            f"{course.course_id}"
                        ),
                        detail={
                            "syllabus_id": request.syllabus_id,
                            "available": [item.syllabus_id for item in syllabi],
                        },
                    )
            elif len(syllabi) > 1:
                raise GenerationError(
                    "ambiguous_syllabus",
                    (
                        f"course {course.course_id} has {len(syllabi)} syllabus versions; "
                        "pass --syllabus to choose one instead of guessing"
                    ),
                    detail={"available": [item.syllabus_id for item in syllabi]},
                )
            else:
                syllabus = syllabi[0]

            units = syllabus_repo.list_units(syllabus.syllabus_id)
            if not units:
                raise GenerationError(
                    "empty_syllabus",
                    f"syllabus {syllabus.syllabus_id} has no parsed units",
                    detail={"syllabus_id": syllabus.syllabus_id},
                )
            unit_by_id = {unit.unit_id: unit for unit in units}

            documents = {
                document.document_id: document
                for document in DocumentRepository(session).list_by_course(course.course_id)
            }
            historical = QuestionRepository(session).list_by_course(course.course_id, limit=2000)
            existing_rows = GenerationRepository(session).list_questions(
                GenerationQuery(
                    course_id=course.course_id,
                    syllabus_id=syllabus.syllabus_id,
                    limit=1000,
                )
            )
            existing = {item.question_id: item for item in existing_rows}

            concept_records = ConceptRepository(session).list_by_course(course.course_id)
            graph_nodes = GraphRepository(session).list_nodes(
                course_id=course.course_id,
                node_type=GraphNodeType.CONCEPT,
                limit=10000,
            )
            related_edges = list(
                GraphRepository(session).list_edges(
                    course_id=course.course_id,
                    relationship=GraphRelationship.RELATED_TO,
                )
            )

        concepts, node_to_concept = self._merge_concepts(
            concept_records, graph_nodes, units, unit_by_id, warnings
        )
        by_id = {item.concept_id: item for item in concepts}
        by_label: dict[str, list[_Concept]] = {}
        for item in concepts:
            by_label.setdefault(item.normalized, []).append(item)
        unit_concepts: dict[str, list[str]] = {}
        for item in concepts:
            unit_concepts.setdefault(item.unit_id, []).append(item.concept_id)
        for ids in unit_concepts.values():
            ids.sort()
        related = self._relations(related_edges, node_to_concept, by_id)
        sentences = self._sentences(request, course, syllabus, documents, warnings)
        return _Loaded(
            course=course,
            syllabus=syllabus,
            units=units,
            concepts=concepts,
            by_id=by_id,
            by_label=by_label,
            unit_concepts=unit_concepts,
            related=related,
            documents=documents,
            sentences=sentences,
            historical=historical,
            existing=existing,
            warnings=warnings,
        )

    def _merge_concepts(
        self,
        records: list[Any],
        nodes: list[Any],
        units: list[SyllabusUnitRecord],
        unit_by_id: dict[str, SyllabusUnitRecord],
        warnings: list[Uncertainty],
    ) -> tuple[list[_Concept], dict[str, str]]:
        """Merge Phase 2 concepts, Phase 5 nodes and syllabus topics.

        One normalized label inside one unit becomes one concept. Phase 2 keeps
        its identity; graph-only concepts use the graph node id; syllabus topics
        without either source get a deterministic topic id.
        """
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        unit_ids = set(unit_by_id)
        node_units: dict[str, set[str]] = {}
        for node in nodes:
            unit_ids_meta = [
                str(item) for item in (node.metadata.get("unit_ids") or []) if str(item) in unit_ids
            ]
            if unit_ids_meta:
                node_units[node.node_id] = set(unit_ids_meta)

        def add(
            *,
            normalized: str,
            label: str,
            unit_id: str,
            origin: str,
            concept_id: str | None,
            node_id: str | None,
            verified: bool,
            aliases: tuple[str, ...] = (),
        ) -> None:
            if not normalized or unit_id not in unit_ids:
                return
            key = (normalized, unit_id)
            topic_key = f"{unit_id}\x1f{normalized}"
            entry = merged.setdefault(
                key,
                {
                    "concept_id": concept_id
                    or f"sylcon_{hashlib.sha256(topic_key.encode()).hexdigest()[:20]}",
                    "label": label.strip() or normalized,
                    "normalized": normalized,
                    "unit_id": unit_id,
                    "origins": [],
                    "graph_node_ids": set(),
                    "aliases": set(),
                    "verified": False,
                },
            )
            if concept_id and entry["concept_id"].startswith("sylcon_"):
                entry["concept_id"] = concept_id
            if label.strip():
                entry["aliases"].add(label.strip())
            entry["origins"].append(origin)
            entry["verified"] = entry["verified"] or verified
            if node_id:
                entry["graph_node_ids"].add(node_id)
            entry["aliases"].update(aliases)

        placed_records = 0
        unplaced_records = 0
        for record in records:
            unit_id = record.unit_id if record.unit_id in unit_ids else None
            if unit_id is None:
                candidates: set[str] = set()
                for node in nodes:
                    if record.concept_id in (node.metadata.get("source_concept_ids") or []):
                        candidates |= node_units.get(node.node_id, set())
                if len(candidates) == 1:
                    unit_id = next(iter(candidates))
            if unit_id is None:
                unplaced_records += 1
                continue
            placed_records += 1
            add(
                normalized=record.normalized or normalize_term(record.term),
                label=record.term,
                unit_id=unit_id,
                origin=f"concept:{getattr(record.origin, 'value', 'unknown')}",
                concept_id=record.concept_id,
                node_id=None,
                verified=bool(record.verified),
                aliases=tuple(str(item) for item in record.evidence[:2] if len(str(item)) < 80),
            )

        placed_nodes = 0
        for node in nodes:
            allowed = sorted(node_units.get(node.node_id, set()))
            if not allowed:
                continue
            placed_nodes += 1
            source_ids = [
                str(item) for item in (node.metadata.get("source_concept_ids") or []) if str(item)
            ]
            add(
                normalized=node.normalized_label or normalize_term(node.label),
                label=node.label,
                unit_id=allowed[0],
                origin="graph_node",
                concept_id=source_ids[0] if source_ids else node.node_id,
                node_id=node.node_id,
                verified=bool(node.metadata.get("verified")),
                aliases=tuple(str(item) for item in (node.metadata.get("aliases") or []))[:3],
            )

        topic_count = 0
        for unit in units:
            for topic in unit.topics:
                normalized = normalize_term(topic)
                if not normalized:
                    continue
                before = len(merged)
                add(
                    normalized=normalized,
                    label=topic,
                    unit_id=unit.unit_id,
                    origin="syllabus_topic",
                    concept_id=None,
                    node_id=None,
                    verified=True,
                )
                if len(merged) > before:
                    topic_count += 1

        concepts: list[_Concept] = []
        node_to_concept: dict[str, str] = {}
        for key in sorted(merged, key=lambda item: (item[1], item[0])):
            entry = merged[key]
            unit = unit_by_id[entry["unit_id"]]
            concept = _Concept(
                concept_id=entry["concept_id"],
                label=entry["label"],
                normalized=entry["normalized"],
                unit_id=unit.unit_id,
                unit_label=unit.unit_label,
                unit_title=unit.title,
                origin="+".join(sorted(set(entry["origins"])))[:60],
                aliases=tuple(sorted({item for item in entry["aliases"] if item})[:4]),
                graph_node_ids=tuple(sorted(entry["graph_node_ids"])),
                verified=entry["verified"],
            )
            concepts.append(concept)
            for node_id in concept.graph_node_ids:
                node_to_concept[node_id] = concept.concept_id

        if unplaced_records:
            warnings.append(
                Uncertainty(
                    code="generation_unplaced_concepts",
                    message=(
                        f"{unplaced_records} stored concept(s) could not be attached to a unit of "
                        "the selected syllabus and were not offered as targets"
                    ),
                    severity=Severity.INFO,
                    detail={"unplaced": unplaced_records, "placed": placed_records},
                )
            )
        if topic_count and not placed_nodes and not placed_records:
            warnings.append(
                Uncertainty(
                    code="generation_concepts_from_syllabus",
                    message=(
                        "no Phase 2 concepts or Phase 5 graph nodes were available; targets come "
                        "from syllabus topics only (run 'exam-engine nlp analyze' for richer "
                        "concept identities)"
                    ),
                    severity=Severity.INFO,
                    detail={"topics": topic_count},
                )
            )
        return concepts, node_to_concept

    def _relations(
        self,
        edges: list[Any],
        node_to_concept: dict[str, str],
        by_id: dict[str, _Concept],
    ) -> dict[str, list[str]]:
        related: dict[str, set[str]] = {}
        for edge in edges:
            left = node_to_concept.get(edge.source_node_id)
            right = node_to_concept.get(edge.target_node_id)
            if not left or not right or left == right:
                continue
            if left not in by_id or right not in by_id:
                continue
            related.setdefault(left, set()).add(right)
            related.setdefault(right, set()).add(left)
        return {key: sorted(value) for key, value in related.items()}

    def _sentences(
        self,
        request: GenerationRequest,
        course: CourseRecord,
        syllabus: SyllabusRecord,
        documents: dict[str, DocumentRecord],
        warnings: list[Uncertainty],
    ) -> list[_Sentence]:
        eligible = {
            document_id: document
            for document_id, document in documents.items()
            if document.kind in GROUNDING_KINDS
        }
        if request.document_ids:
            unknown = [item for item in request.document_ids if item not in documents]
            if unknown:
                raise GenerationError(
                    "unknown_document",
                    "a requested source document does not belong to this course",
                    detail={"document_ids": unknown, "course_id": course.course_id},
                )
            selected = {item: documents[item] for item in request.document_ids}
            excluded = [document for document in selected.values() if document.kind in EXAM_KINDS]
            if excluded:
                warnings.append(
                    Uncertainty(
                        code="generation_exam_source_excluded",
                        message=(
                            "examination papers and question banks were excluded as grounding "
                            "sources; generated questions stay distinct from historical papers"
                        ),
                        severity=Severity.WARNING,
                        detail={"document_ids": sorted(item.document_id for item in excluded)},
                    )
                )
            eligible = {
                document_id: document
                for document_id, document in selected.items()
                if document.kind in GROUNDING_KINDS
            }
            if not eligible:
                raise GenerationError(
                    "no_grounding_documents",
                    "the requested documents are all examination material; provide syllabus, "
                    "notes or other study material instead",
                    detail={"document_ids": list(request.document_ids)},
                )
        else:
            syllabus_document = documents.get(syllabus.document_id)
            if syllabus_document is not None:
                eligible.setdefault(syllabus.document_id, syllabus_document)

        sentences: list[_Sentence] = []
        truncated = False
        priorities = {DocumentKind.SYLLABUS: 0, DocumentKind.NOTES: 1, DocumentKind.OTHER: 2}
        with self.db.session() as session:
            pages_repo = DocumentRepository(session)
            for document in sorted(
                eligible.values(), key=lambda item: (priorities[item.kind], item.document_id)
            ):
                for page in pages_repo.list_pages(document.document_id):
                    for sentence in _split_sentences(page.text):
                        if len(sentences) >= MAX_INDEXED_SENTENCES:
                            truncated = True
                            break
                        sentences.append(
                            _Sentence(
                                document=document,
                                page_number=page.page_number,
                                text=sentence,
                                priority=priorities[document.kind],
                            )
                        )
                    if truncated:
                        break
                if truncated:
                    break
        if truncated:
            warnings.append(
                Uncertainty(
                    code="generation_index_truncated",
                    message=(
                        f"source indexing stopped at {MAX_INDEXED_SENTENCES} sentences; evidence "
                        "selection may miss later material"
                    ),
                    severity=Severity.WARNING,
                    detail={"limit": MAX_INDEXED_SENTENCES},
                )
            )
        if not sentences:
            warnings.append(
                Uncertainty(
                    code="generation_no_source_text",
                    message=(
                        "no syllabus, notes or study text was found; grounding is limited to "
                        "syllabus concept labels"
                    ),
                    severity=Severity.WARNING,
                    detail={"course_id": course.course_id},
                )
            )
        return sentences

    # -- planning ----------------------------------------------------------

    def _plan(self, request: GenerationRequest, loaded: _Loaded) -> _Plan:
        defaults: dict[str, str] = {}
        bloom = request.bloom
        if bloom is None:
            bloom = DEFAULT_BLOOM[request.format]
            defaults["bloom"] = bloom.value
        difficulty = request.difficulty
        if difficulty is None:
            difficulty = DEFAULT_DIFFICULTY
            defaults["difficulty"] = difficulty.value
        style, style_marks = self._reference_style(request, loaded)
        marks = request.marks or style_marks or DEFAULT_MARKS[request.format]
        if request.marks is None:
            defaults["marks"] = f"{marks:g}"

        if request.concept_ids:
            targets = [loaded.concept_for(item) for item in request.concept_ids]
            if request.unit_id is not None:
                wrong = [item.concept_id for item in targets if item.unit_id != request.unit_id]
                if wrong:
                    raise GenerationError(
                        "concept_not_in_unit",
                        "a requested concept does not belong to the requested unit",
                        detail={"concept_ids": wrong, "unit_id": request.unit_id},
                    )
        else:
            unit_ids = (
                [request.unit_id] if request.unit_id else [unit.unit_id for unit in loaded.units]
            )
            for unit_id in unit_ids:
                known = {unit.unit_id for unit in loaded.units}
                if unit_id not in known:
                    raise GenerationError(
                        "unknown_unit",
                        f"unit {unit_id!r} is not part of syllabus {loaded.syllabus.syllabus_id}",
                        detail={
                            "unit_id": unit_id,
                            "available": sorted(known),
                        },
                    )
            ids = sorted(
                {
                    concept_id
                    for unit_id in unit_ids
                    for concept_id in loaded.unit_concepts.get(unit_id, [])
                }
            )
            if not ids:
                raise GenerationError(
                    "no_concepts_in_unit",
                    (
                        "the selected unit has no resolvable concepts; run 'exam-engine nlp "
                        "analyze' or check that the syllabus lists topics for that unit"
                    ),
                    detail={"unit_ids": unit_ids},
                )
            targets = [loaded.by_id[item] for item in ids]

        deduped: list[_Concept] = []
        seen: set[str] = set()
        for concept in targets:
            if concept.concept_id in seen:
                continue
            seen.add(concept.concept_id)
            deduped.append(concept)
        if not deduped:
            raise GenerationError("no_concepts", "no target concept could be resolved")

        # A multi-question batch must not ask the same prompt with a different index.
        # Keep the chosen concept(s) first, then continue through their units and the
        # rest of the syllabus in deterministic order when the caller asked for more.
        spares: tuple[_Concept, ...] = ()
        if request.expand_batch:
            seen = {item.concept_id for item in deduped}
            selected_units = list(dict.fromkeys(item.unit_id for item in deduped))
            unit_order = [
                *selected_units,
                *[unit.unit_id for unit in loaded.units if unit.unit_id not in selected_units],
            ]
            extras: list[_Concept] = []
            for unit_id in unit_order:
                for concept_id in loaded.unit_concepts.get(unit_id, []):
                    if concept_id in seen:
                        continue
                    spare = loaded.by_id.get(concept_id)
                    if spare is None:
                        continue
                    seen.add(concept_id)
                    extras.append(spare)
            needed = max(0, request.count - len(deduped))
            deduped.extend(extras[:needed])
            spares = tuple(extras[needed : needed + max(3, request.count)])

        return _Plan(
            bloom=bloom,
            difficulty=difficulty,
            marks=marks,
            targets=tuple(deduped),
            reference_style=style,
            defaults_applied=defaults,
            spares=spares,
        )

    def _reference_style(
        self, request: GenerationRequest, loaded: _Loaded
    ) -> tuple[dict[str, Any], float | None]:
        if not request.reference_question_ids:
            return {}, None
        by_id = {item.question_id: item for item in loaded.historical}
        unknown = [item for item in request.reference_question_ids if item not in by_id]
        if unknown:
            raise GenerationError(
                "unknown_reference_question",
                "a reference question does not belong to this course",
                detail={"question_ids": unknown},
            )
        references = [by_id[item] for item in request.reference_question_ids]
        marks = [item.marks for item in references if item.marks is not None]
        words = [len(item.text.split()) for item in references]
        style: dict[str, Any] = {
            "reference_question_ids": [item.question_id for item in references],
            "average_words": round(sum(words) / len(words), 1) if words else None,
            "average_marks": round(sum(marks) / len(marks), 2) if marks else None,
            "method": "historical_reference_style",
            "note": (
                "reference questions only inform length and marks conventions; their wording "
                "is never copied into generated questions"
            ),
        }
        with self.db.session() as session:
            repo = QuestionUnderstandingRepository(session)
            types: list[str] = []
            blooms: list[str] = []
            for reference in references:
                analysis = repo.get_analysis(reference.question_id)
                if analysis is None:
                    continue
                if analysis.primary_type:
                    types.append(analysis.primary_type.value)
                if analysis.bloom_primary:
                    blooms.append(analysis.bloom_primary.value)
        if types:
            style["dominant_question_type"] = max(sorted(set(types)), key=types.count)
        if blooms:
            style["dominant_bloom"] = max(sorted(set(blooms)), key=blooms.count)
        return style, (round(sum(marks) / len(marks) * 2) / 2 if marks else None)

    def _scope(self, plan: _Plan, loaded: _Loaded) -> GenerationScope:
        unit_ids = sorted({item.unit_id for item in plan.targets})
        sources = sorted(
            {
                sentence.document.document_id
                for sentence in loaded.sentences
                if sentence.document.kind in GROUNDING_KINDS
            }
            | ({loaded.syllabus.document_id} if loaded.syllabus.document_id else set())
        )
        kinds = sorted(
            {
                sentence.document.kind.value
                for sentence in loaded.sentences
                if sentence.document.document_id in sources
            }
        )
        return GenerationScope(
            document_ids=sources,
            syllabus_id=loaded.syllabus.syllabus_id,
            unit_ids=unit_ids,
            concept_ids=sorted(
                {item.concept_id for item in loaded.concepts if item.unit_id in unit_ids}
            ),
            statement=(
                f"Grounded in syllabus {loaded.syllabus.syllabus_id} of course "
                f"{loaded.course.course_id} and {len(sources)} non-examination source "
                f"document(s) ({', '.join(kinds) or 'none'}); historical question papers are "
                "excluded as sources."
            ),
        )

    def _parameters(
        self,
        request: GenerationRequest,
        loaded: _Loaded,
        plan: _Plan,
        provider: GenerationProvider,
    ) -> dict[str, Any]:
        return {
            "schema_version": GENERATION_SCHEMA_VERSION,
            "algorithm": GENERATION_ALGORITHM,
            "algorithm_version": GENERATION_ALGORITHM_VERSION,
            "provider": provider.describe(),
            "defaults_applied": plan.defaults_applied,
            "thresholds": {
                "duplicate_near": self.settings.duplicate_near_threshold,
                "duplicate_semantic": self.settings.duplicate_semantic_threshold,
            },
            "limits": {
                "max_questions": self.settings.max_questions,
                "min_mcq_options": self.settings.min_mcq_options,
                "max_mcq_options": self.settings.max_mcq_options,
                "max_evidence_passages": self.settings.max_evidence_passages,
            },
            "seed": request.seed,
            "marks": plan.marks,
            "supported_formats": sorted(support.format.value for support in format_support()),
            "documents": sorted(
                document.document_id
                for document in loaded.documents.values()
                if document.kind in GROUNDING_KINDS
            ),
            "excluded_document_kinds": sorted(item.value for item in EXAM_KINDS),
        }

    # -- per-question construction ----------------------------------------

    def _prompt(
        self,
        index: int,
        request: GenerationRequest,
        plan: _Plan,
        loaded: _Loaded,
        concept: _Concept,
        cache: dict[str, list[SourcePassage]],
    ) -> GenerationPrompt:
        passages = cache.get(concept.concept_id)
        if passages is None:
            passages = self._passages(concept, loaded)
            cache[concept.concept_id] = passages
        related_ids = [
            item
            for item in loaded.related.get(concept.concept_id, [])
            if item in loaded.by_id and item != concept.concept_id
        ]
        siblings = [
            item
            for item in loaded.unit_concepts.get(concept.unit_id, [])
            if item != concept.concept_id
        ]
        labels: list[str] = []
        for concept_id in [*siblings, *related_ids]:
            item = loaded.by_id.get(concept_id)
            if item is None or item.label in labels:
                continue
            labels.append(item.label)
        labels = labels[: self.settings.max_related_labels]
        # A comparison needs grounding for the second concept. Same-unit siblings are
        # offered as options first, so they are tried first here too; a graph relation
        # is only a fallback when no sibling has usable evidence.
        related_passages: list[SourcePassage] = []
        considered: set[str] = set()
        for concept_id in [*siblings, *related_ids]:
            if concept_id in considered:
                continue
            considered.add(concept_id)
            other = loaded.by_id.get(concept_id)
            if other is None:
                continue
            other_passages = cache.get(concept_id)
            if other_passages is None:
                other_passages = self._passages(other, loaded)
                cache[concept_id] = other_passages
            if other_passages:
                related_passages.append(other_passages[0])
                break
        terms = _concept_terms(concept)
        for word in _label_words(concept):
            if word.casefold() not in {item.casefold() for item in terms}:
                terms.append(word)
        verb = OBJECTIVE_VERBS[plan.bloom]
        objective = f"{verb} {concept.label} in {concept.unit_label}"
        if concept.unit_title and normalize_term(concept.unit_title) != normalize_term(
            concept.unit_label
        ):
            objective += f" ({concept.unit_title})"
        return GenerationPrompt(
            course_id=loaded.course.course_id,
            syllabus_id=loaded.syllabus.syllabus_id,
            concept_id=concept.concept_id,
            concept_label=concept.label,
            concept_terms=terms,
            unit_id=concept.unit_id,
            unit_label=concept.unit_label,
            unit_title=concept.unit_title,
            learning_objective=objective,
            related_labels=labels,
            passages=passages,
            related_passages=related_passages,
            format=request.format,
            bloom=plan.bloom,
            difficulty=plan.difficulty,
            marks=plan.marks,
            language=request.language,
            index=index,
            count=request.count,
            seed=request.seed,
            include_answer_key=request.include_answer_key,
            include_explanation=request.include_explanation,
            reference_style=plan.reference_style,
        )

    def _passages(self, concept: _Concept, loaded: _Loaded) -> list[SourcePassage]:
        limit = max(1, self.settings.max_evidence_passages)
        full_patterns = [
            re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
            for term in sorted(_concept_terms(concept))
        ]
        word_patterns = [
            re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
            for word in sorted(_label_words(concept))
        ]
        # Sentences that name the concept itself rank above sentences that only use
        # one of its words, so a phrase match always wins over a loose one.
        matches: list[tuple[int, int, int, str, _Sentence]] = []
        for sentence in loaded.sentences:
            exact = any(pattern.search(sentence.text) for pattern in full_patterns)
            if not exact and not any(pattern.search(sentence.text) for pattern in word_patterns):
                continue
            patterns = full_patterns if exact else word_patterns
            excerpt = _focus_excerpt(sentence.text, patterns, self.settings.max_evidence_chars)
            if _structural_fragment(excerpt):
                # A table row or layout fragment mentions the concept but does not
                # describe it; it cannot ground a question.
                continue
            matches.append((0 if exact else 1, sentence.priority, len(excerpt), excerpt, sentence))
        # When the corpus contains sentences that name the concept, loose word
        # matches from unrelated parts of the document are not useful evidence.
        exact_matches = [item for item in matches if item[0] == 0]
        if exact_matches:
            matches = exact_matches
        matches.sort(key=lambda item: item[:4])
        passages = [
            SourcePassage(
                document_id=sentence.document.document_id,
                document_title=sentence.document.title,
                document_kind=sentence.document.kind,
                page_number=sentence.page_number,
                excerpt=excerpt,
                concept_id=concept.concept_id,
                method="document_text",
            )
            for _rank, _priority, _length, excerpt, sentence in matches[:limit]
        ]
        if passages:
            return passages

        unit = next((item for item in loaded.units if item.unit_id == concept.unit_id), None)
        topics = [
            topic
            for topic in (unit.topics if unit else [])
            if normalize_term(topic) == concept.normalized
        ]
        if not topics and concept.normalized:
            topics = [concept.label]
        document_id = loaded.syllabus.document_id or loaded.syllabus.syllabus_id
        title = loaded.syllabus.title or "Syllabus"
        return [
            SourcePassage(
                document_id=document_id,
                document_title=title,
                document_kind=DocumentKind.SYLLABUS,
                page_number=(unit.page_number if unit else None),
                excerpt=_clip(topic, self.settings.max_evidence_chars),
                concept_id=concept.concept_id,
                method=("syllabus_topic" if topic != concept.label else "syllabus_concept_label"),
            )
            for topic in topics[:1]
        ]

    def _assemble(
        self,
        *,
        question_id: str,
        batch_id: str,
        request: GenerationRequest,
        plan: _Plan,
        loaded: _Loaded,
        concept: _Concept,
        prompt: GenerationPrompt,
        question_text: str,
        draft: Any,
    ) -> GeneratedQuestion:
        temp = QuestionRecord(
            question_id=question_id,
            document_id=concept.unit_id,
            course_id=loaded.course.course_id,
            text=question_text,
        )
        structure = detect_structure(temp)
        types = classify_question_types(question_text, structure, self.taxonomy)
        bloom = classify_bloom(question_text, structure, types, self.taxonomy)
        difficulty = estimate_difficulty(question_text, structure, types, bloom, marks=plan.marks)
        estimated_bloom = None
        if bloom.primary:
            try:
                estimated_bloom = BloomLevel(bloom.primary)
            except ValueError:
                estimated_bloom = None
        metadata = dict(draft.metadata)
        metadata.update(
            {
                "batch_id": batch_id,
                "index": prompt.index,
                "concept": concept.as_metadata(),
                "related_labels": list(prompt.related_labels),
                "evidence": [
                    {
                        "document_id": item.document_id,
                        "page_number": item.page_number,
                        "method": item.method,
                    }
                    for item in prompt.passages
                ],
                "seed": request.seed,
                "requested_marks": plan.marks,
                "include_answer_key": request.include_answer_key,
                "include_explanation": request.include_explanation,
                "difficulty_explicit": request.difficulty is not None,
                "bloom_explicit": request.bloom is not None,
                "expand_batch": request.expand_batch,
                "type_method": TYPE_METHOD,
                "bloom_method": BLOOM_METHOD,
                "difficulty_method": DIFFICULTY_METHOD,
            }
        )
        if plan.reference_style:
            metadata["reference_style"] = plan.reference_style
        if draft.template_id:
            metadata["template_id"] = draft.template_id
        return GeneratedQuestion(
            question_id=question_id,
            batch_id=batch_id,
            course_id=loaded.course.course_id,
            syllabus_id=loaded.syllabus.syllabus_id,
            unit_id=concept.unit_id,
            unit_label=concept.unit_label,
            unit_title=concept.unit_title,
            concept_ids=[concept.concept_id],
            concept_labels=[concept.label],
            learning_objective=prompt.learning_objective,
            format=prompt.format,
            question_type=types.primary,
            requested_difficulty=plan.difficulty,
            estimated_difficulty=difficulty.label,
            difficulty_confidence=difficulty.confidence,
            difficulty_method=DIFFICULTY_METHOD,
            requested_bloom=plan.bloom,
            estimated_bloom=estimated_bloom,
            bloom_confidence=bloom.confidence,
            marks=plan.marks,
            text=question_text,
            options=[],
            correct_option_key=None,
            answer_key=draft.answer_key,
            explanation=draft.explanation,
            solution_steps=list(draft.solution_steps),
            distractor_explanations=dict(draft.distractor_explanations),
            source_references=_dedupe_passages([*prompt.passages, *prompt.related_passages]),
            generation_metadata=metadata,
            validation=ValidationResult(
                status=GenerationValidationStatus.VALIDATION_INCOMPLETE,
                issues=[],
                checks=[],
            ),
            review_status=GenerationReviewStatus.PENDING_REVIEW,
            review_history=[
                ReviewEvent(
                    status=GenerationReviewStatus.PENDING_REVIEW,
                    note="generated; awaiting review",
                    reviewer="system",
                    at=utcnow(),
                )
            ],
            limitations=[
                "Generated practice question; not verified examination content.",
            ],
        )

    def _options(self, draft: Any) -> list[GeneratedOption]:
        options: list[GeneratedOption] = []
        correct_index = draft.correct_index
        for position, text in enumerate(draft.options):
            key = "ABCDEFGH"[position]
            options.append(
                GeneratedOption(
                    key=key,
                    text=str(text).strip(),
                    is_correct=correct_index is not None and position == correct_index,
                )
            )
        return options

    def _thin_evidence(self, question: GeneratedQuestion) -> bool:
        return bool(question.source_references) and all(
            item.method in {"syllabus_concept_label", "syllabus_topic"}
            for item in question.source_references
        )

    def _foreign_labels(self, loaded: _Loaded) -> list[str]:
        """Concept terms that belong to *other* courses, used as a leakage check."""
        with self.db.session() as session:
            terms = ConceptRepository(session).list_foreign_terms(
                loaded.course.course_id, limit=800
            )
        own = {item.label.casefold() for item in loaded.concepts}
        own |= {item.normalized for item in loaded.concepts}
        return sorted({term for term in terms if term.casefold() not in own})

    # -- similarity pools --------------------------------------------------

    def _semantic_index(self, loaded: _Loaded) -> SemanticIndex | None:
        """Load Phase 2 question vectors for the historical corpus, if any."""
        with self.db.session() as session:
            history_ids = {item.question_id for item in loaded.historical}
            if not history_ids:
                return None
            try:
                from exam_engine.storage.nlp_repositories import (
                    EmbeddingRepository,
                    VectorStoreRepository,
                )

                records = EmbeddingRepository(session).list_by_course(loaded.course.course_id)
                relevant = [item for item in records if item.owner_id in history_ids]
                if not relevant:
                    return None
                model_id = max(
                    sorted({item.model_id for item in relevant}),
                    key=lambda candidate: sum(1 for item in relevant if item.model_id == candidate),
                )
                store = VectorStoreRepository(session)
                vectors: dict[str, list[float]] = {}
                for record in relevant:
                    if record.model_id != model_id:
                        continue
                    vector = store.get(record.model_id, record.text_sha256)
                    if vector is not None:
                        vectors[record.owner_id] = vector
                if not vectors:
                    return None
                return SemanticIndex(model_id=model_id, question_vectors=vectors)
            except Exception:  # noqa: BLE001 - embeddings are an optional signal
                logger.debug("could not load Phase 2 question vectors", exc_info=True)
                return None

    def _existing_pool(self, loaded: _Loaded) -> list[dedupe.PoolItem]:
        pool: list[dedupe.PoolItem] = []
        for question in loaded.existing.values():
            pool.append(
                dedupe.PoolItem(
                    question_id=question.question_id,
                    text=question.text,
                    source=dedupe.SOURCE_GENERATED,
                    concept_ids=tuple(question.concept_ids),
                    profile=ReasoningProfile(
                        question_type=question.question_type,
                        bloom_level=question.requested_bloom.value,
                    ),
                )
            )
        return pool

    def _historical_item(self, question: QuestionRecord) -> dedupe.PoolItem:
        return dedupe.PoolItem(
            question_id=question.question_id,
            text=question.text,
            source=dedupe.SOURCE_HISTORICAL,
            profile=ReasoningProfile(),
        )

    def _repeated_texts(self, questions: list[GeneratedQuestion]) -> list[str]:
        counts: dict[str, int] = {}
        for question in questions:
            key = dedupe.normalise_question_text(question.text)
            counts[key] = counts.get(key, 0) + 1
        return [text for text, count in counts.items() if count > 1]

    # -- identifiers -------------------------------------------------------

    def _batch_id(
        self,
        request: GenerationRequest,
        loaded: _Loaded,
        plan: _Plan,
        provider: GenerationProvider,
    ) -> str:
        payload = {
            "schema": GENERATION_SCHEMA_VERSION,
            "algorithm": GENERATION_ALGORITHM_VERSION,
            "course": loaded.course.course_id,
            "syllabus": loaded.syllabus.syllabus_id,
            "request": request.model_dump(mode="json"),
            "targets": [item.concept_id for item in plan.targets],
            "bloom": plan.bloom.value,
            "difficulty": plan.difficulty.value,
            "marks": plan.marks,
            "provider": provider.name,
            "model": getattr(provider, "model", None),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return "gb_" + hashlib.sha256(encoded.encode()).hexdigest()[:24]

    def _question_id(
        self,
        batch_id: str,
        concept: _Concept,
        request: GenerationRequest,
        plan: _Plan,
        index: int,
    ) -> str:
        payload = (
            f"{batch_id}\x1f{concept.concept_id}\x1f{request.format.value}\x1f"
            f"{plan.bloom.value}\x1f{plan.difficulty.value}\x1f{index}"
        )
        return "gq_" + hashlib.sha256(payload.encode()).hexdigest()[:24]

    # -- persistence -------------------------------------------------------

    def _persist(self, batch: GenerationBatch) -> None:
        with self.db.transaction() as session:
            repo = GenerationRepository(session)
            repo.upsert_batch(batch)
            persisted = 0
            for question in batch.questions:
                result = repo.save_question(
                    question, allow_overwrite_approved=batch.request.allow_overwrite_approved
                )
                if result in {"created", "updated", "unchanged"}:
                    persisted += 1
                elif result == "approved_locked":
                    batch.warnings.append(
                        Uncertainty(
                            code="generation_approved_locked",
                            message=(
                                f"approved question {question.question_id} was not overwritten"
                            ),
                            severity=Severity.WARNING,
                            detail={"question_id": question.question_id},
                        )
                    )
            batch.counts.persisted = persisted
            repo.upsert_batch(batch)

    # -- queries and review ------------------------------------------------

    def get_batch(self, batch_id: str) -> GenerationBatch:
        with self.db.session() as session:
            batch = GenerationRepository(session).get_batch(batch_id)
        if batch is None:
            raise GenerationError(
                "unknown_batch",
                f"no generation batch {batch_id!r}",
                detail={"batch_id": batch_id},
            )
        return batch

    def list_batches(self, course_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = GenerationRepository(session).list_batches(course_id, limit=limit)
            return [
                {
                    "batch_id": row.batch_id,
                    "course_id": row.course_id,
                    "syllabus_id": row.syllabus_id,
                    "status": row.status,
                    "provider": row.provider,
                    "model": row.model,
                    "created_at": row.created_at,
                    "counts": json.loads(row.counts_json or "{}"),
                }
                for row in rows
            ]

    def list_questions(self, query: GenerationQuery) -> list[GeneratedQuestion]:
        with self.db.session() as session:
            if CourseRepository(session).get(query.course_id) is None:
                raise GenerationError(
                    "unknown_course",
                    f"no course {query.course_id!r} exists in this database",
                    detail={"course_id": query.course_id},
                )
            return GenerationRepository(session).list_questions(query)

    def get_question(self, question_id: str) -> GeneratedQuestion:
        with self.db.session() as session:
            question = GenerationRepository(session).get_question(question_id)
        if question is None:
            raise GenerationError(
                "unknown_generated_question",
                f"no generated question {question_id!r}",
                detail={"question_id": question_id},
            )
        return question

    def set_review_status(
        self,
        question_id: str,
        status: GenerationReviewStatus,
        *,
        note: str | None = None,
        reviewer: str = "local-user",
        expected_revision: int | None = None,
    ) -> GeneratedQuestion:
        with self.db.transaction() as session:
            repo = GenerationRepository(session)
            question = repo.get_question(question_id)
            if question is None:
                raise GenerationError(
                    "unknown_generated_question",
                    f"no generated question {question_id!r}",
                    detail={"question_id": question_id},
                )
            if expected_revision is not None and question.revision != expected_revision:
                raise GenerationError(
                    "revision_conflict",
                    (
                        f"question {question_id} is at revision {question.revision}, but "
                        f"revision {expected_revision} was expected"
                    ),
                    detail={
                        "question_id": question_id,
                        "revision": question.revision,
                        "expected": expected_revision,
                    },
                )
            if question.review_status is status:
                return question
            question.review_status = status
            question.review_history.append(ReviewEvent(status=status, note=note, reviewer=reviewer))
            question.updated_at = utcnow()
            repo.save_question(question, allow_overwrite_approved=True)
            # The repository increments the stored revision; returning the
            # pre-update model would make the next optimistic save conflict.
            stored = repo.get_question(question_id)
            if stored is not None:
                return stored
        return question

    def review_batch(
        self,
        batch_id: str,
        status: GenerationReviewStatus,
        *,
        note: str | None = None,
        reviewer: str = "local-user",
        only_pending: bool = True,
    ) -> list[GeneratedQuestion]:
        batch = self.get_batch(batch_id)
        updated: list[GeneratedQuestion] = []
        for question in batch.questions:
            if only_pending and question.review_status is not GenerationReviewStatus.PENDING_REVIEW:
                continue
            if question.review_status is status:
                updated.append(question)
                continue
            updated.append(
                self.set_review_status(question.question_id, status, note=note, reviewer=reviewer)
            )
        return updated

    def review_summary(self, course_id: str, syllabus_id: str) -> dict[str, int]:
        with self.db.session() as session:
            return GenerationRepository(session).count_by_review_status(course_id, syllabus_id)

    def export_batch(self, batch_id: str, path: Path) -> Path:
        batch = self.get_batch(batch_id)
        payload = batch.model_dump(mode="json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise GenerationError(
                "generation_export_failed",
                f"could not write {path}: {exc}",
                detail={"path": str(path)},
            ) from exc
        return path


#: Separators that mark a table row rather than prose; such fragments never ground a task.
STRUCTURAL_MARKERS = ("|", "\t")


class ScopeContext:
    """Read-only view of one resolved course scope, for downstream phases.

    Everything the wrapper exposes is already loaded: no method here queries the
    database again, so a Phase 11 run sees one consistent snapshot.
    """

    def __init__(self, service: QuestionGenerationService, loaded: _Loaded) -> None:
        self._service = service
        self._loaded = loaded

    @property
    def course_id(self) -> str:
        return self._loaded.course.course_id

    @property
    def syllabus_id(self) -> str:
        return self._loaded.syllabus.syllabus_id

    @property
    def units(self) -> list[SyllabusUnitRecord]:
        return list(self._loaded.units)

    def concepts(self) -> list[ScopeConcept]:
        return [
            ScopeConcept(
                concept_id=item.concept_id,
                label=item.label,
                unit_id=item.unit_id,
                unit_label=item.unit_label,
                unit_title=item.unit_title,
                aliases=item.aliases,
                verified=item.verified,
                graph_node_ids=item.graph_node_ids,
            )
            for item in self._loaded.concepts
        ]

    def concept(self, concept_id: str) -> _Concept | None:
        return self._loaded.by_id.get(concept_id)

    def resolve(self, ref: str) -> _Concept:
        return self._loaded.concept_for(ref)

    def allowed_concept_ids(self) -> frozenset[str]:
        return frozenset(self._loaded.by_id)

    def concept_labels(self) -> dict[str, str]:
        return {item.concept_id: item.label for item in self._loaded.concepts}

    def unit_ids(self) -> frozenset[str]:
        return frozenset(unit.unit_id for unit in self._loaded.units)

    def unit_label(self, unit_id: str) -> str:
        unit = next((item for item in self._loaded.units if item.unit_id == unit_id), None)
        return unit.unit_label if unit else unit_id

    def unit_concepts(self, unit_id: str) -> list[str]:
        return list(self._loaded.unit_concepts.get(unit_id, []))

    def related(self, concept_id: str) -> list[str]:
        """Phase 5 ``related_to`` neighbours, already mapped to concept ids."""

        return [
            item for item in self._loaded.related.get(concept_id, []) if item in self._loaded.by_id
        ]

    def sibling_labels(self, concept_id: str) -> list[str]:
        """Other concept labels in the same unit, for related-concept work."""

        concept = self._loaded.by_id.get(concept_id)
        if concept is None:
            return []
        return [
            self._loaded.by_id[item].label
            for item in self._loaded.unit_concepts.get(concept.unit_id, [])
            if item != concept_id and item in self._loaded.by_id
        ]

    def passages(self, concept_id: str, *, limit: int | None = None) -> list[SourcePassage]:
        concept = self._loaded.by_id.get(concept_id)
        if concept is None:
            return []
        passages = self._service._passages(concept, self._loaded)
        return passages[:limit] if limit is not None else passages

    def historical(self) -> list[QuestionRecord]:
        return list(self._loaded.historical)

    def existing_questions(self) -> dict[str, GeneratedQuestion]:
        return dict(self._loaded.existing)

    def semantic_index(self) -> SemanticIndex | None:
        return self._service._semantic_index(self._loaded)

    def pool(self) -> list[dedupe.PoolItem]:
        """Every stored question this scope can be compared against."""

        items = self._service._existing_pool(self._loaded)
        items.extend(self._service._historical_item(item) for item in self._loaded.historical)
        return items

    def foreign_labels(self) -> list[str]:
        return self._service._foreign_labels(self._loaded)

    def validation_context(
        self, *, evidence: tuple[SourcePassage, ...] = (), thin_evidence: bool = False
    ) -> ValidationContext:
        return ValidationContext(
            settings=self._service.settings,
            allowed_concept_ids=self.allowed_concept_ids(),
            concept_labels=self.concept_labels(),
            allowed_unit_ids=self.unit_ids(),
            forbidden_labels=frozenset(self.foreign_labels()),
            evidence=evidence,
            thin_evidence=thin_evidence,
            phase3_available=True,
        )

    def dedupe_settings(self) -> dedupe.DedupeSettings:
        return dedupe.DedupeSettings(
            near_threshold=self._service.settings.duplicate_near_threshold,
            semantic_threshold=self._service.settings.duplicate_semantic_threshold,
        )


@dataclass(frozen=True)
class ScopeConcept:
    """A public, immutable concept view (no private generation types leak out)."""

    concept_id: str
    label: str
    unit_id: str
    unit_label: str
    unit_title: str | None
    aliases: tuple[str, ...] = ()
    verified: bool = False
    graph_node_ids: tuple[str, ...] = ()


def _structural_fragment(text: str) -> bool:
    """True when a snippet is tabular or numeric book-keeping, not explanation."""
    if any(marker in text for marker in STRUCTURAL_MARKERS):
        return True
    return text.count("%") >= 2


def _dedupe_passages(passages: list[SourcePassage]) -> list[SourcePassage]:
    """The cited evidence for one question, without repeating the same excerpt."""

    seen: set[tuple[str, str]] = set()
    unique: list[SourcePassage] = []
    for passage in passages:
        key = (passage.document_id, passage.excerpt)
        if key in seen:
            continue
        seen.add(key)
        unique.append(passage)
    return unique


def _term_variants(term: str) -> list[str]:
    """Search and mask variants for a concept term: the term plus a simple singular.

    Course material rarely repeats a syllabus label verbatim, so a concept stored as
    "Stacks and queues" must still find a notes sentence that says "stack". Variants
    only locate and mask evidence; the quoted excerpt itself is never rewritten.
    """

    compact = " ".join(term.split())
    variants = [compact]
    folded = compact.casefold()
    if len(compact) > 3 and folded.endswith("s") and not folded.endswith("ss"):
        variants.append(compact[:-1])
    return variants


def _concept_terms(concept: _Concept) -> list[str]:
    """Label, normalised form and aliases, each with its simple variants."""

    terms: list[str] = []
    seen: set[str] = set()
    for term in (concept.label, concept.normalized, *concept.aliases):
        for variant in _term_variants(term):
            if len(variant) < 3 or variant.casefold() in seen:
                continue
            seen.add(variant.casefold())
            terms.append(variant)
    return terms


def _label_words(concept: _Concept) -> list[str]:
    """Significant single words from the label and aliases.

    A syllabus topic is often a phrase ("Stacks and queues") while the notes that
    describe it use one of its words ("A stack is ..."). These words are a *fallback*
    search and mask key, ranked below the concept's own name so a phrase match is
    still preferred.
    """

    words: list[str] = []
    seen: set[str] = set()
    for term in (concept.label, *concept.aliases):
        for word in re.findall(r"[A-Za-z][A-Za-z-]{3,}", term):
            if word.casefold() in STOPWORDS:
                continue
            for variant in _term_variants(word):
                folded = variant.casefold()
                if folded in STOPWORDS or folded in seen:
                    continue
                seen.add(folded)
                words.append(variant)
    return words


def _focus_excerpt(text: str, patterns: list[re.Pattern[str]], limit: int) -> str:
    """Window a long segment around the concept instead of taking its head.

    Ingestion keeps syllabus lines and table rows as single segments, so the first
    characters of a matching segment are often unrelated to the matched concept.
    """
    if len(text) <= limit:
        return _clip(text, limit)
    earliest: int | None = None
    for pattern in patterns:
        found = pattern.search(text)
        if found is not None and (earliest is None or found.start() < earliest):
            earliest = found.start()
    if earliest is None:
        return _clip(text, limit)
    start = max(0, earliest - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    snippet = text[start:end]
    leading = snippet.find(" ")
    if start > 0 and leading != -1:
        snippet = snippet[leading + 1 :]
    trailing = snippet.rfind(" ")
    if end < len(text) and trailing > 0:
        snippet = snippet[:trailing]
    snippet = snippet.strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _split_sentences(text: str) -> list[str]:
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])", cleaned)
    sentences: list[str] = []
    buffer = ""
    for part in parts:
        candidate = f"{buffer} {part}".strip() if buffer else part
        if len(candidate) < MIN_SENTENCE_CHARS:
            buffer = candidate
            continue
        sentences.append(candidate)
        buffer = ""
    if buffer:
        if sentences:
            sentences[-1] = f"{sentences[-1]} {buffer}".strip()
        else:
            sentences.append(buffer)
    return [item for item in sentences if item]


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    cut = compact[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:") + "…"


__all__ = [
    "EXAM_KINDS",
    "GROUNDING_KINDS",
    "GenerationError",
    "QuestionGenerationService",
]
