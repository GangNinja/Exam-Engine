"""Phase 11 service: mutation, adversarial generation and misconception mining.

The three capabilities share one storage and validation surface:

* a mutation or challenge is an ordinary :class:`GeneratedQuestion` with an
  ``origin`` of ``mutation`` or ``adversarial``, so Phase 10's review, export and
  dedupe queries keep working unchanged;
* Phase 10 validation runs first, then Phase 11 adds the invariant/fairness
  checks it cannot make;
* misconception records live in their own table and never modify a question.

Failures are isolated: one unusable strategy becomes a rejection on the batch,
and a failed analysis run changes no question and no response.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exam_engine.advanced import adversarial, misconception, mutation
from exam_engine.advanced.source import (
    AdvancedError,
    EvidenceContext,
    SourceOption,
    SourceQuestion,
)
from exam_engine.advanced.verification import VerificationRequest, verify
from exam_engine.config import Settings
from exam_engine.generation import dedupe
from exam_engine.generation.service import QuestionGenerationService, ScopeContext
from exam_engine.generation.templates import DEFAULT_MARKS, OBJECTIVE_VERBS
from exam_engine.generation.validation import warnings_from_validation
from exam_engine.ingest.normalize import fingerprint_text
from exam_engine.models.advanced import (
    AdversarialRequest,
    MisconceptionQuery,
    MisconceptionRecord,
    MisconceptionReport,
    MutationRequest,
    ResponseImport,
    ResponseRecord,
    response_ref,
)
from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    AssessmentOrigin,
    BloomLevel,
    ChallengeStrategy,
    DifficultyLabel,
    GenerationFormat,
    GenerationReviewStatus,
    GenerationStatus,
    GenerationValidationStatus,
    GraphRelationship,
    MisconceptionStatus,
    QuestionType,
    ResponseCorrectness,
    Severity,
    SimilarityKind,
)
from exam_engine.models.generation import (
    DuplicateResolution,
    GeneratedOption,
    GeneratedQuestion,
    GenerationBatch,
    GenerationCounts,
    GenerationQuery,
    GenerationRejection,
    GenerationRequest,
    GenerationScope,
    SourcePassage,
    ValidationResult,
)
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.storage.advanced_repository import (
    MisconceptionRepository,
    ResponseRepository,
)
from exam_engine.storage.db import Database
from exam_engine.storage.generation_repository import GenerationRepository
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.question_repositories import QuestionUnderstandingRepository
from exam_engine.storage.repositories import (
    CourseRepository,
    QuestionRepository,
    SyllabusRepository,
)
from exam_engine.understanding.classification import (
    classify_bloom,
    classify_question_types,
    estimate_difficulty,
)
from exam_engine.understanding.structure import detect_structure

logger = logging.getLogger(__name__)

ALGORITHM = "phase11_advanced_assessment"
VERSION = "phase11.v1"

#: Phase 3 question type -> response format, used when a stored question has no
#: explicit format. Estimates, never authoritative.
_TYPE_TO_FORMAT: dict[QuestionType, GenerationFormat] = {
    QuestionType.DEFINITION: GenerationFormat.SHORT_ANSWER,
    QuestionType.EXPLANATION: GenerationFormat.EXPLANATION,
    QuestionType.COMPARISON: GenerationFormat.COMPARISON,
    QuestionType.APPLICATION: GenerationFormat.APPLICATION,
    QuestionType.NUMERICAL: GenerationFormat.NUMERICAL,
    QuestionType.SCENARIO: GenerationFormat.CASE_BASED,
    QuestionType.PROOF: GenerationFormat.LONG_ANSWER,
    QuestionType.PROGRAMMING: GenerationFormat.LONG_ANSWER,
    QuestionType.DESIGN: GenerationFormat.DESIGN,
    QuestionType.EVALUATION: GenerationFormat.EVALUATION,
}

_PHASE10_DOWNGRADES = {"missing_answer_key"}

#: Passage methods that only anchor a concept label, with no explanatory text.
_THIN_METHODS = frozenset({"syllabus_concept_label", "syllabus_topic"})


@dataclass(frozen=True)
class SourceBundle:
    """The source question plus everything that may ground variants of it."""

    question: SourceQuestion
    context: EvidenceContext
    scope: ScopeContext


@dataclass(frozen=True)
class ImportSummary:
    course_id: str
    syllabus_id: str
    supplied: int
    inserted: int
    duplicates: int
    inferred_correctness: int
    warnings: list[Uncertainty]

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "syllabus_id": self.syllabus_id,
            "supplied": self.supplied,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "inferred_correctness": self.inferred_correctness,
            "warnings": [item.model_dump(mode="json") for item in self.warnings],
        }


class AdvancedAssessmentService:
    """Coordinates Phase 11 work; all three capabilities are independent calls."""

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        generation_service: QuestionGenerationService | None = None,
    ) -> None:
        self.db = database
        base = settings or Settings()
        self.settings = base
        self.advanced = base.advanced
        self.generation = generation_service or QuestionGenerationService(
            database, base.generation, understanding_settings=base.understanding
        )

    # -- capabilities ------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        return {
            "phase": 11,
            "algorithm": ALGORITHM,
            "version": VERSION,
            "mutation_strategies": [spec.as_dict() for spec in mutation.SPECS.values()],
            "challenge_strategies": [spec.as_dict() for spec in adversarial.SPECS.values()],
            "misconception_kinds": {
                kind.value: rule for kind, rule in misconception.KIND_RULES.items()
            },
            "not_inferred": {
                kind.value: reason for kind, reason in misconception.UNSUPPORTED_KINDS.items()
            },
            "limits": self.advanced.as_config(),
        }

    # -- A: mutation -------------------------------------------------------

    def mutate(self, request: MutationRequest) -> GenerationBatch:
        if request.count > self.advanced.max_variants:
            raise AdvancedError(
                "request_too_large",
                f"{request.count} variants requested but the configured limit is "
                f"{self.advanced.max_variants}",
                detail={"requested": request.count, "limit": self.advanced.max_variants},
            )
        bundle = self._load_source(request.course_id, request.syllabus_id, request.question_id)
        source = bundle.question
        scope = bundle.scope
        if request.format is not None and request.format is not source.format:
            raise AdvancedError(
                "format_constraint_mismatch",
                (
                    f"the request constrains the response format to {request.format.value}, but "
                    f"the source question is {source.format.value}; a mutation never converts "
                    "between formats"
                ),
                detail={"requested": request.format.value, "source": source.format.value},
            )
        strategies = request.strategies or self._default_mutation_strategies(bundle)
        if not strategies:
            raise AdvancedError(
                "no_supported_strategy",
                (
                    f"no mutation strategy can be applied to this {source.format.value} question "
                    "with the material available"
                ),
                detail={"format": source.format.value},
            )

        batch_id = self._batch_id("mutation", request.course_id, scope.syllabus_id, request)
        counts = GenerationCounts(requested=request.count)
        questions: list[GeneratedQuestion] = []
        rejections: list[GenerationRejection] = []
        duplicates: list[DuplicateResolution] = []
        warnings: list[Uncertainty] = []
        kept: list[dedupe.PoolItem] = []
        pool = scope.pool()
        semantic = scope.semantic_index()
        dedupe_settings = scope.dedupe_settings()

        for index in range(request.count):
            strategy = strategies[index % len(strategies)]
            spec = mutation.spec_for(strategy)
            try:
                candidate = mutation.mutate(
                    source,
                    strategy,
                    index=index,
                    seed=request.seed,
                    context=bundle.context,
                    difficulty=request.difficulty,
                    bloom=request.bloom,
                )
            except AdvancedError as exc:
                rejections.append(
                    GenerationRejection(
                        index=index,
                        concept_id=source.concept_ids[0] if source.concept_ids else source.unit_id,
                        concept_label=source.concept_label,
                        format=source.format,
                        code=exc.code,
                        message=exc.message,
                    )
                )
                counts.failed += 1
                continue

            counts.generated += 1
            question = self._assemble_mutation(
                batch_id, index, candidate, source, scope, explicit_difficulty=request.difficulty
            )
            question.validation = verify(
                question,
                scope.validation_context(evidence=tuple(question.source_references)),
                VerificationRequest(
                    preserve_objective=request.preserve_objective,
                    preserve_concept=request.preserve_concept,
                    preserve_answer=request.preserve_answer,
                    allow_format_change=request.allow_format_change,
                    allowed_concept_ids=scope.allowed_concept_ids(),
                    source=source,
                    spec=spec,
                ),
                downgrade=(frozenset(_PHASE10_DOWNGRADES) if spec.changes_answer else frozenset()),
            )
            question.warnings = warnings_from_validation(question.validation)
            self._count_validation(counts, question)
            if question.validation.status is GenerationValidationStatus.REJECTED:
                rejections.append(
                    GenerationRejection(
                        index=index,
                        concept_id=source.concept_ids[0] if source.concept_ids else source.unit_id,
                        concept_label=source.concept_label,
                        format=question.format,
                        code="validation_rejected",
                        message=f"validation rejected the {strategy.value} variant",
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

            # A variant is meant to resemble the question it derives from, so only an
            # identical copy is a duplicate here. Near-duplicates are recorded as
            # similarity evidence and left for the reviewer to judge.
            duplicate = self._duplicate(
                question,
                pool=[
                    item
                    for item in [*kept, *pool]
                    if item.question_id not in {question.question_id, source.question_id}
                ],
                course_id=request.course_id,
                settings=dedupe_settings,
                semantic=semantic,
            )
            if duplicate is not None:
                if duplicate.kind == SimilarityKind.EXACT.value:
                    self._record_duplicate(
                        duplicate, question, index, duplicates, rejections, counts
                    )
                    continue
                question.generation_metadata["similar_to"] = {
                    "question_id": duplicate.matched_question_id,
                    "kind": duplicate.kind,
                    "score": duplicate.score,
                }
                duplicates.append(
                    DuplicateResolution(
                        index=index,
                        concept_id=(question.concept_ids[0] if question.concept_ids else ""),
                        kind=duplicate.kind,
                        matched_question_id=duplicate.matched_question_id,
                        score=duplicate.score,
                        reason=(
                            f"{duplicate.reason} (expected for a derived variant; kept because "
                            "only an identical copy is rejected here)"
                        ),
                    )
                )

            questions.append(question)
            kept.append(
                dedupe.PoolItem(
                    question_id=question.question_id,
                    text=question.text,
                    source=dedupe.SOURCE_GENERATED,
                    concept_ids=tuple(question.concept_ids),
                )
            )

        warnings.extend(self._batch_warnings("mutation", counts))
        batch = GenerationBatch(
            batch_id=batch_id,
            algorithm=ALGORITHM,
            algorithm_version=VERSION,
            course_id=request.course_id,
            capability="mutation",
            status=self._status(counts, len(questions), request.count),
            request=self._generation_request(request, source),
            provider="deterministic",
            model=None,
            resolved_bloom=source.bloom,
            resolved_difficulty=source.difficulty,
            counts=counts,
            questions=questions,
            rejections=rejections,
            duplicates=duplicates,
            scope=self._scope(request.course_id, scope, source),
            parameters={
                "origin": AssessmentOrigin.MUTATION.value,
                "source_question_id": source.question_id,
                "strategies": [item.value for item in strategies],
                "seed": request.seed,
                "preserve_objective": request.preserve_objective,
                "preserve_concept": request.preserve_concept,
                "preserve_answer": request.preserve_answer,
                "allow_format_change": request.allow_format_change,
            },
            warnings=warnings,
            limitations=[
                "Mutated questions are derived practice material, not verified examination "
                "content.",
                "Automated checks confirm observable properties only; semantic equivalence and "
                "answer validity still need a reviewer.",
                "Embedding or wording similarity is never treated as proof of equivalent meaning.",
            ],
        )
        if request.persist and questions:
            self._persist(batch)
        elif request.persist:
            self._persist_batch_row(batch)
        return batch

    def _default_mutation_strategies(self, bundle: SourceBundle) -> list[Any]:
        available: list[Any] = []
        for spec in mutation.supported_strategies(bundle.question.format):
            if self._requirements_met(spec.requires, bundle):
                available.append(spec.strategy)
        return available

    def _requirements_met(self, requires: frozenset[str], bundle: SourceBundle) -> bool:
        source, context = bundle.question, bundle.context
        if mutation.NEED_EVIDENCE in requires and not context.has_evidence():
            return False
        if mutation.NEED_NUMBERS in requires and not mutation.has_numeric_parameters(source.text):
            return False
        if mutation.NEED_RELATED in requires and not context.sibling_labels:
            return False
        if mutation.NEED_MCQ_OPTIONS in requires and not any(
            option.is_correct for option in source.options
        ):
            return False
        return not (
            mutation.NEED_PARTS in requires and not mutation.has_reorderable_parts(source.text)
        )

    def _load_source(
        self, course_id: str, syllabus_id: str | None, question_id: str
    ) -> SourceBundle:
        generated: GeneratedQuestion | None = None
        historical: QuestionRecord | None = None
        with self.db.session() as session:
            if CourseRepository(session).get(course_id) is None:
                raise AdvancedError(
                    "unknown_course",
                    f"no course {course_id!r} exists in this database",
                    detail={"course_id": course_id},
                )
            generated = GenerationRepository(session).get_question(question_id)
            if generated is None:
                historical = QuestionRepository(session).get(question_id)
        if generated is not None and generated.course_id != course_id:
            raise AdvancedError(
                "course_mismatch",
                (
                    f"question {question_id} belongs to course {generated.course_id}, not "
                    f"{course_id}"
                ),
                detail={"question_course": generated.course_id, "course_id": course_id},
            )
        if historical is not None and historical.course_id != course_id:
            raise AdvancedError(
                "course_mismatch",
                (
                    f"question {question_id} belongs to course {historical.course_id}, not "
                    f"{course_id}"
                ),
                detail={"question_course": historical.course_id, "course_id": course_id},
            )
        if generated is None and historical is None:
            raise AdvancedError(
                "unknown_source_question",
                f"no question {question_id!r} in course {course_id}",
                detail={"question_id": question_id, "course_id": course_id},
            )

        if generated is not None:
            if syllabus_id and syllabus_id != generated.syllabus_id:
                raise AdvancedError(
                    "syllabus_mismatch",
                    (
                        f"question {question_id} belongs to syllabus {generated.syllabus_id}, "
                        f"not {syllabus_id}"
                    ),
                    detail={"question_syllabus": generated.syllabus_id},
                )
            scope = self._scope_for(
                course_id,
                generated.syllabus_id,
                unit_id=generated.unit_id,
                concept_ids=list(generated.concept_ids),
            )
            source = self._source_from_generated(generated, scope)
        else:
            assert historical is not None
            unit_id, analysis = self._analysed_unit(historical)
            if not unit_id:
                raise AdvancedError(
                    "source_unmapped",
                    (
                        f"question {historical.question_id} has no syllabus unit mapping; run "
                        "'exam-engine understand run' for this course before mutating it"
                    ),
                    detail={"question_id": historical.question_id},
                )
            scope = self._scope_for(course_id, syllabus_id, unit_id=unit_id)
            source = self._source_from_historical(historical, analysis, scope)
        return SourceBundle(question=source, context=self._evidence(scope, source), scope=scope)

    def _scope_for(
        self,
        course_id: str,
        syllabus_id: str | None,
        *,
        unit_id: str | None,
        concept_ids: Sequence[str] = (),
    ) -> ScopeContext:
        if unit_id is None and not concept_ids:
            raise AdvancedError(
                "no_target_scope",
                "no syllabus unit or concept could be resolved for this request",
                detail={"course_id": course_id, "syllabus_id": syllabus_id},
            )
        request = GenerationRequest(
            course_id=course_id,
            syllabus_id=syllabus_id,
            unit_id=unit_id,
            concept_ids=list(concept_ids) if concept_ids else [],
            count=1,
        )
        try:
            return self.generation.load_scope(request)
        except Exception as exc:  # noqa: BLE001 - re-raised with a stable code
            code = getattr(exc, "code", "scope_unavailable")
            message = getattr(exc, "message", str(exc))
            detail = getattr(exc, "detail", {})
            raise AdvancedError(code, message, detail=detail) from exc

    def _analysed_unit(self, question: QuestionRecord) -> tuple[str | None, Any]:
        with self.db.session() as session:
            analysis = QuestionUnderstandingRepository(session).get_analysis(question.question_id)
        return (analysis.top_unit_id if analysis else None), analysis

    def _source_from_generated(
        self, question: GeneratedQuestion, scope: ScopeContext
    ) -> SourceQuestion:
        return SourceQuestion(
            question_id=question.question_id,
            course_id=question.course_id,
            syllabus_id=question.syllabus_id,
            unit_id=question.unit_id,
            unit_label=question.unit_label,
            unit_title=question.unit_title,
            concept_ids=tuple(question.concept_ids),
            concept_labels=tuple(question.concept_labels),
            text=question.text,
            format=question.format,
            bloom=question.requested_bloom,
            difficulty=question.requested_difficulty,
            objective=question.learning_objective,
            marks=question.marks,
            answer_key=question.answer_key,
            explanation=question.explanation,
            solution_steps=tuple(question.solution_steps),
            options=tuple(
                SourceOption(key=item.key, text=item.text, is_correct=item.is_correct)
                for item in question.options
            ),
            correct_option_key=question.correct_option_key,
            source_references=tuple(question.source_references),
            question_type=question.question_type,
        )

    def _source_from_historical(
        self, question: QuestionRecord, analysis: Any, scope: ScopeContext
    ) -> SourceQuestion:
        unit_id = analysis.top_unit_id if analysis else None
        if not unit_id:
            raise AdvancedError(
                "source_unmapped",
                (
                    f"question {question.question_id} has no syllabus unit mapping; run "
                    "'exam-engine understand run' for this course before mutating it"
                ),
                detail={"question_id": question.question_id},
            )
        unit = next((item for item in scope.units if item.unit_id == unit_id), None)
        if unit is None:
            raise AdvancedError(
                "source_unmapped",
                f"the mapped unit {unit_id} is not part of the selected syllabus version",
                detail={"unit_id": unit_id, "syllabus_id": scope.syllabus_id},
            )
        concepts = [
            concept
            for concept in (scope.concept(item) for item in scope.unit_concepts(unit_id))
            if concept is not None
        ]
        lowered = question.text.casefold()
        matched = [
            item
            for item in concepts
            if any(alias and alias.casefold() in lowered for alias in {item.label, *item.aliases})
        ]
        if not matched:
            raise AdvancedError(
                "source_concept_unresolved",
                (
                    "no syllabus concept of the mapped unit is named in this question's text, so "
                    "its assessed concept cannot be established"
                ),
                detail={
                    "question_id": question.question_id,
                    "unit_id": unit_id,
                    "candidates": [item.label for item in concepts],
                },
            )
        format_label = GenerationFormat.SHORT_ANSWER
        bloom = BloomLevel.UNDERSTAND
        difficulty = DifficultyLabel.UNKNOWN
        if analysis is not None:
            if analysis.primary_type is not None:
                format_label = _TYPE_TO_FORMAT.get(
                    QuestionType(analysis.primary_type), GenerationFormat.SHORT_ANSWER
                )
            if analysis.bloom_primary:
                bloom = BloomLevel(analysis.bloom_primary)
            difficulty = DifficultyLabel(analysis.difficulty_label)
        objective = f"{OBJECTIVE_VERBS[bloom]} {matched[0].label} in {unit.unit_label}"
        return SourceQuestion(
            question_id=question.question_id,
            course_id=question.course_id,
            syllabus_id=scope.syllabus_id,
            unit_id=unit.unit_id,
            unit_label=unit.unit_label,
            unit_title=unit.title,
            concept_ids=tuple(item.concept_id for item in matched),
            concept_labels=tuple(item.label for item in matched),
            text=question.text,
            format=format_label,
            bloom=bloom,
            difficulty=difficulty,
            objective=objective,
            marks=(float(question.marks) if question.marks else DEFAULT_MARKS[format_label]),
            answer_key=None,
            explanation=None,
            solution_steps=(),
            options=(),
            correct_option_key=None,
            source_references=(),
            question_type=None,
        )

    def _evidence(self, scope: ScopeContext, source: SourceQuestion) -> EvidenceContext:
        concept_id = source.concept_ids[0] if source.concept_ids else None
        passages: list[SourcePassage] = []
        siblings: list[str] = []
        related: list[str] = []
        prerequisite: str | None = None
        prerequisite_in_scope = False
        if concept_id:
            # A syllabus label is an anchor, not material: quoting "Stacks" as the
            # situation a challenge refers to would be misleading, so only real
            # document text can ground a variant or a challenge.
            passages = [
                item
                for item in scope.passages(
                    concept_id, limit=self.generation.settings.max_evidence_passages
                )
                if item.method not in _THIN_METHODS
            ]
            siblings = scope.sibling_labels(concept_id)
            related = [
                scope.concept(item).label  # type: ignore[union-attr]
                for item in scope.unit_concepts(source.unit_id)
                if scope.concept(item) is not None
            ]
            prerequisite, prerequisite_in_scope = self._prerequisite(
                scope, concept_id, source.course_id
            )
        missing: list[str] = []
        if not passages:
            missing.append("grounding_passage")
        if not siblings:
            missing.append("sibling_concept")
        return EvidenceContext(
            passages=tuple(passages),
            sibling_labels=tuple(label for label in siblings if label),
            prerequisite_label=prerequisite,
            prerequisite_in_syllabus=prerequisite_in_scope,
            related_labels=tuple(label for label in related if label),
            concept_ids=scope.allowed_concept_ids(),
            missing=tuple(missing),
        )

    def _prerequisite(
        self, scope: ScopeContext, concept_id: str, course_id: str
    ) -> tuple[str | None, bool]:
        concept = scope.concept(concept_id)
        if concept is None or not concept.graph_node_ids:
            return None, False
        node_ids = set(concept.graph_node_ids)
        with self.db.session() as session:
            edges = GraphRepository(session).list_edges(
                course_id=course_id, relationship=GraphRelationship.PREREQUISITE_OF
            )
        targets = {edge.target_node_id for edge in edges if edge.target_node_id in node_ids}
        if not targets:
            return None, False
        sources = {edge.source_node_id for edge in edges if edge.target_node_id in targets}
        for candidate in scope.concepts():
            node_ids_from = set(candidate.graph_node_ids)
            if node_ids_from and node_ids_from & sources:
                return candidate.label, True
        return None, False

    def _assemble_mutation(
        self,
        batch_id: str,
        index: int,
        candidate: Any,
        source: SourceQuestion,
        scope: ScopeContext,
        *,
        explicit_difficulty: DifficultyLabel | None = None,
    ) -> GeneratedQuestion:
        structure = detect_structure(_temp(candidate.text, source.course_id))
        types = classify_question_types(candidate.text, structure, self.generation.taxonomy)
        bloom = classify_bloom(candidate.text, structure, types, self.generation.taxonomy)
        difficulty = estimate_difficulty(
            candidate.text, structure, types, bloom, marks=source.marks
        )
        estimated_bloom: BloomLevel | None = None
        if bloom.primary:
            try:
                estimated_bloom = BloomLevel(bloom.primary)
            except ValueError:
                estimated_bloom = None
        options = [
            GeneratedOption(key=item.key, text=item.text, is_correct=item.is_correct)
            for item in mutation.option_records(candidate)
        ]
        # A label is only asserted when the caller asked for it or the strategy
        # deliberately changes it; otherwise the observed estimate stands alone, so a
        # rephrasing is not reported as a difficulty mismatch it never claimed.
        if explicit_difficulty is not None:
            requested_difficulty = explicit_difficulty
        elif candidate.strategy is mutation.MutationStrategy.DIFFICULTY:
            requested_difficulty = candidate.difficulty
        else:
            requested_difficulty = difficulty.label
        limitations = list(candidate.limitations)
        return GeneratedQuestion(
            question_id=self._question_id(
                batch_id, source.question_id, candidate.strategy.value, index
            ),
            batch_id=batch_id,
            course_id=source.course_id,
            syllabus_id=source.syllabus_id,
            origin=AssessmentOrigin.MUTATION.value,
            source_question_id=source.question_id,
            strategy=candidate.strategy.value,
            unit_id=source.unit_id,
            unit_label=source.unit_label,
            unit_title=source.unit_title,
            concept_ids=list(candidate.concept_ids),
            concept_labels=list(candidate.concept_labels),
            learning_objective=candidate.objective,
            format=source.format,
            question_type=types.primary,
            requested_difficulty=requested_difficulty,
            estimated_difficulty=difficulty.label,
            difficulty_confidence=difficulty.confidence,
            requested_bloom=candidate.bloom,
            estimated_bloom=estimated_bloom,
            bloom_confidence=bloom.confidence,
            marks=source.marks,
            text=candidate.text,
            options=options,
            correct_option_key=candidate.correct_option_key,
            answer_key=candidate.answer_key,
            explanation=candidate.explanation,
            solution_steps=list(candidate.solution_steps),
            source_references=list(candidate.source_references),
            generation_metadata={
                "phase": 11,
                "origin": AssessmentOrigin.MUTATION.value,
                "strategy": candidate.strategy.value,
                "index": index,
                "invariants": candidate.invariants,
                "changed": candidate.changed,
                "source_question_id": source.question_id,
                "source_format": source.format.value,
            },
            validation=ValidationResult(
                status=GenerationValidationStatus.VALIDATION_INCOMPLETE,
                checks=[],
                issues=[],
                confidence=0.0,
            ),
            limitations=limitations,
        )

    # -- B: adversarial ----------------------------------------------------

    def generate_adversarial(self, request: AdversarialRequest) -> GenerationBatch:
        if request.count > self.advanced.max_variants:
            raise AdvancedError(
                "request_too_large",
                f"{request.count} questions requested but the configured limit is "
                f"{self.advanced.max_variants}",
                detail={"requested": request.count, "limit": self.advanced.max_variants},
            )
        if request.language.strip().casefold() not in {"en", "eng", "english"}:
            raise AdvancedError(
                "unsupported_language",
                "only English ('en') is supported by this phase's templates",
                detail={"language": request.language},
            )
        scope = self._scope_for(
            request.course_id,
            request.syllabus_id,
            unit_id=request.unit_id,
            concept_ids=list(request.concept_ids),
        )
        targets = self._targets(request, scope)
        strategies = request.strategies or [
            strategy
            for strategy in adversarial.DEFAULT_CHALLENGE_ORDER
            if self._challenge_available(strategy, scope, targets)
        ]
        if not strategies:
            raise AdvancedError(
                "no_supported_strategy",
                (
                    "no adversarial strategy can be grounded for these concepts; add course "
                    "material for the unit or generate prerequisites first"
                ),
                detail={"concept_ids": [item.concept_id for item in targets]},
            )

        batch_id = self._batch_id("adversarial", request.course_id, scope.syllabus_id, request)
        counts = GenerationCounts(requested=request.count)
        questions: list[GeneratedQuestion] = []
        rejections: list[GenerationRejection] = []
        duplicates: list[DuplicateResolution] = []
        warnings: list[Uncertainty] = []
        kept: list[dedupe.PoolItem] = []
        pool = scope.pool()
        semantic = scope.semantic_index()
        dedupe_settings = scope.dedupe_settings()
        allowed = scope.allowed_concept_ids()

        for index in range(request.count):
            target = targets[index % len(targets)]
            strategy = strategies[index % len(strategies)]
            spec = adversarial.spec_for(strategy)
            context = self._challenge_context(scope, target, request)
            try:
                candidate = adversarial.challenge(
                    target,
                    strategy,
                    index=index,
                    seed=request.seed,
                    context=context,
                    difficulty=request.difficulty or DifficultyLabel.HARD,
                    bloom=request.bloom,
                )
            except AdvancedError as exc:
                rejections.append(
                    GenerationRejection(
                        index=index,
                        concept_id=target.concept_id,
                        concept_label=target.concept_label,
                        format=spec.format,
                        code=exc.code,
                        message=exc.message,
                    )
                )
                counts.failed += 1
                continue

            counts.generated += 1
            question = self._assemble_adversarial(
                batch_id, index, candidate, target, scope, request
            )
            question.validation = verify(
                question,
                scope.validation_context(evidence=tuple(question.source_references)),
                VerificationRequest(
                    allow_out_of_scope=request.allow_out_of_scope,
                    rubric_required=request.include_rubric,
                    allowed_concept_ids=allowed,
                    challenge=strategy,
                ),
            )
            question.warnings = warnings_from_validation(question.validation)
            self._count_validation(counts, question)
            if question.validation.status is GenerationValidationStatus.REJECTED:
                rejections.append(
                    GenerationRejection(
                        index=index,
                        concept_id=target.concept_id,
                        concept_label=target.concept_label,
                        format=question.format,
                        code="validation_rejected",
                        message=f"fairness checks rejected the {strategy.value} question",
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

            duplicate = self._duplicate(
                question,
                pool=[item for item in [*kept, *pool] if item.question_id != question.question_id],
                course_id=request.course_id,
                settings=dedupe_settings,
                semantic=semantic,
            )
            if (
                duplicate is not None
                and duplicate.is_duplicate
                and self._record_duplicate(
                    duplicate, question, index, duplicates, rejections, counts
                )
            ):
                continue

            questions.append(question)
            kept.append(
                dedupe.PoolItem(
                    question_id=question.question_id,
                    text=question.text,
                    source=dedupe.SOURCE_GENERATED,
                    concept_ids=tuple(question.concept_ids),
                )
            )

        warnings.extend(self._batch_warnings("adversarial", counts))
        batch = GenerationBatch(
            batch_id=batch_id,
            algorithm=ALGORITHM,
            algorithm_version=VERSION,
            course_id=request.course_id,
            capability="adversarial",
            status=self._status(counts, len(questions), request.count),
            request=self._adversarial_generation_request(request, targets),
            provider="deterministic",
            model=None,
            resolved_bloom=request.bloom or adversarial.spec_for(strategies[0]).default_bloom,
            resolved_difficulty=request.difficulty or DifficultyLabel.HARD,
            counts=counts,
            questions=questions,
            rejections=rejections,
            duplicates=duplicates,
            scope=GenerationScope(
                document_ids=sorted(
                    {
                        item.document_id
                        for question in questions
                        for item in question.source_references
                    }
                ),
                syllabus_id=scope.syllabus_id,
                unit_ids=sorted({item.unit_id for item in targets if item.unit_id}),
                concept_ids=[item.concept_id for item in targets],
                statement=(
                    f"challenges for {len(targets)} concept(s) inside syllabus "
                    f"{scope.syllabus_id}, grounded in course material only"
                ),
            ),
            parameters={
                "origin": AssessmentOrigin.ADVERSARIAL.value,
                "strategies": [item.value for item in strategies],
                "seed": request.seed,
                "allow_out_of_scope": request.allow_out_of_scope,
                "include_rubric": request.include_rubric,
            },
            warnings=warnings,
            limitations=[
                "Adversarial questions are review material; automated fairness checks cannot "
                "prove a question is unambiguous.",
                "No model answer is invented: each question carries a rubric and the reference "
                "passage instead.",
                "Difficulty is a design target, not a calibrated measurement.",
            ],
        )
        if request.persist and questions:
            self._persist(batch)
        elif request.persist:
            self._persist_batch_row(batch)
        return batch

    def _targets(
        self, request: AdversarialRequest, scope: ScopeContext
    ) -> list[adversarial.ChallengeTarget]:
        targets: list[adversarial.ChallengeTarget] = []
        if request.concept_ids:
            for ref in request.concept_ids:
                try:
                    resolved = scope.resolve(ref)
                except Exception as exc:  # noqa: BLE001 - normalised below
                    code = getattr(exc, "code", "unknown_concept")
                    message = getattr(exc, "message", str(exc))
                    raise AdvancedError(code, message) from exc
                targets.append(self._target(scope, resolved))
        else:
            assert request.unit_id is not None
            for concept_id in scope.unit_concepts(request.unit_id):
                concept = scope.concept(concept_id)
                if concept is not None:
                    targets.append(self._target(scope, concept))
        if not targets:
            raise AdvancedError(
                "no_target_concept",
                "the request resolves to no syllabus concept inside this syllabus version",
                detail={"unit_id": request.unit_id, "concept_ids": request.concept_ids},
            )
        return targets[: self.generation.settings.max_related_labels]

    def _target(self, scope: ScopeContext, concept: Any) -> adversarial.ChallengeTarget:
        label = concept.label
        unit_label = concept.unit_label
        bloom = BloomLevel.ANALYZE
        return adversarial.ChallengeTarget(
            concept_id=concept.concept_id,
            concept_label=label,
            unit_label=unit_label,
            objective=f"{OBJECTIVE_VERBS[bloom]} {label} in {unit_label}",
            concept_ids=(concept.concept_id,),
            unit_id=concept.unit_id,
        )

    def _challenge_available(
        self,
        strategy: ChallengeStrategy,
        scope: ScopeContext,
        targets: Sequence[adversarial.ChallengeTarget],
    ) -> bool:
        spec = adversarial.spec_for(strategy)
        for target in targets:
            context = self._evidence(
                scope,
                SourceQuestion(
                    question_id=target.concept_id,
                    course_id=scope.course_id,
                    syllabus_id=scope.syllabus_id,
                    unit_id=scope.concept(target.concept_id).unit_id,  # type: ignore[union-attr]
                    unit_label=target.unit_label,
                    unit_title=None,
                    concept_ids=target.concept_ids,
                    concept_labels=(target.concept_label,),
                    text=target.concept_label,
                    format=GenerationFormat.EXPLANATION,
                    bloom=BloomLevel.ANALYZE,
                    difficulty=DifficultyLabel.HARD,
                    objective=target.objective,
                    marks=DEFAULT_MARKS[GenerationFormat.EXPLANATION],
                ),
            )
            missing = set()
            if adversarial.NEED_EVIDENCE in spec.requires and not context.has_evidence():
                missing.add("evidence")
            if adversarial.NEED_SIBLING in spec.requires and not context.sibling_labels:
                missing.add("sibling")
            if adversarial.NEED_PREREQUISITE in spec.requires and not (
                context.prerequisite_label and context.prerequisite_in_syllabus
            ):
                missing.add("prerequisite")
            if not missing:
                return True
        return False

    def _challenge_context(
        self, scope: ScopeContext, target: adversarial.ChallengeTarget, request: AdversarialRequest
    ) -> EvidenceContext:
        concept = scope.concept(target.concept_id)
        source = SourceQuestion(
            question_id=target.concept_id,
            course_id=scope.course_id,
            syllabus_id=scope.syllabus_id,
            unit_id=concept.unit_id if concept else "",
            unit_label=target.unit_label,
            unit_title=concept.unit_title if concept else None,
            concept_ids=target.concept_ids,
            concept_labels=(target.concept_label,),
            text=target.concept_label,
            format=GenerationFormat.EXPLANATION,
            bloom=BloomLevel.ANALYZE,
            difficulty=DifficultyLabel.HARD,
            objective=target.objective,
            marks=DEFAULT_MARKS[GenerationFormat.EXPLANATION],
        )
        context = self._evidence(scope, source)
        if request.allow_out_of_scope:
            return context
        return context

    def _assemble_adversarial(
        self,
        batch_id: str,
        index: int,
        candidate: Any,
        target: adversarial.ChallengeTarget,
        scope: ScopeContext,
        request: AdversarialRequest,
    ) -> GeneratedQuestion:
        concept = scope.concept(target.concept_id)
        unit_label = concept.unit_label if concept else target.unit_label
        structure = detect_structure(_temp(candidate.text, scope.course_id))
        types = classify_question_types(candidate.text, structure, self.generation.taxonomy)
        bloom = classify_bloom(candidate.text, structure, types, self.generation.taxonomy)
        marks = DEFAULT_MARKS[candidate.format]
        difficulty = estimate_difficulty(candidate.text, structure, types, bloom, marks=marks)
        estimated_bloom: BloomLevel | None = None
        if bloom.primary:
            try:
                estimated_bloom = BloomLevel(bloom.primary)
            except ValueError:
                estimated_bloom = None
        rubric = list(candidate.rubric) if request.include_rubric else []
        return GeneratedQuestion(
            question_id=self._question_id(
                batch_id, target.concept_id, candidate.strategy.value, index
            ),
            batch_id=batch_id,
            course_id=scope.course_id,
            syllabus_id=scope.syllabus_id,
            origin=AssessmentOrigin.ADVERSARIAL.value,
            source_question_id=None,
            strategy=candidate.strategy.value,
            unit_id=concept.unit_id if concept else "",
            unit_label=unit_label,
            unit_title=concept.unit_title if concept else None,
            concept_ids=list(candidate.concept_ids),
            concept_labels=list(candidate.concept_labels),
            learning_objective=candidate.objective,
            format=candidate.format,
            question_type=types.primary,
            requested_difficulty=candidate.difficulty,
            estimated_difficulty=difficulty.label,
            difficulty_confidence=difficulty.confidence,
            requested_bloom=candidate.bloom,
            estimated_bloom=estimated_bloom,
            bloom_confidence=bloom.confidence,
            marks=marks,
            text=candidate.text,
            options=[],
            correct_option_key=None,
            answer_key=None,
            explanation=candidate.explanation,
            solution_steps=rubric,
            source_references=list(candidate.source_references),
            generation_metadata={
                "phase": 11,
                "origin": AssessmentOrigin.ADVERSARIAL.value,
                "strategy": candidate.strategy.value,
                "index": index,
                "challenge_rationale": candidate.rationale,
                "teacher_notes": {
                    "rubric": rubric,
                    "explanation": candidate.explanation,
                },
            },
            validation=ValidationResult(
                status=GenerationValidationStatus.VALIDATION_INCOMPLETE,
                checks=[],
                issues=[],
                confidence=0.0,
            ),
            limitations=[*candidate.limitations],
        )

    # -- C: misconceptions -------------------------------------------------

    def import_responses(self, payload: ResponseImport) -> ImportSummary:
        with self.db.session() as session:
            if CourseRepository(session).get(payload.course_id) is None:
                raise AdvancedError(
                    "unknown_course",
                    f"no course {payload.course_id!r} exists in this database",
                    detail={"course_id": payload.course_id},
                )
            syllabi = SyllabusRepository(session).get_by_course(payload.course_id)
            if not syllabi:
                raise AdvancedError(
                    "no_syllabus",
                    f"course {payload.course_id} has no parsed syllabus",
                    detail={"course_id": payload.course_id},
                )
            syllabus = self._pick_syllabus(payload.syllabus_id, syllabi, payload.course_id)
            known = self._known_question_ids(
                session, payload.course_id, [item.question_id for item in payload.responses]
            )

        warnings: list[Uncertainty] = []
        records: list[ResponseRecord] = []
        duplicate_count = 0
        inferred = 0
        seen: set[str] = set()
        for submission in payload.responses:
            if submission.question_id not in known:
                raise AdvancedError(
                    "unknown_question",
                    (
                        f"response refers to question {submission.question_id}, which does not "
                        f"exist in course {payload.course_id}"
                    ),
                    detail={"question_id": submission.question_id},
                )
            correctness = submission.correctness or self._infer_correctness(submission)
            if submission.correctness is None:
                inferred += 1
            normalized = " ".join(submission.answer.split())
            fingerprint = fingerprint_text(
                f"{submission.question_id}\x1f{normalized}\x1f"
                f"{fingerprint_text(submission.student_ref) if submission.student_ref else ''}"
            )
            if fingerprint in seen:
                duplicate_count += 1
                continue
            seen.add(fingerprint)
            records.append(
                ResponseRecord(
                    response_id=response_ref(
                        payload.course_id, submission.question_id, fingerprint
                    ),
                    course_id=payload.course_id,
                    syllabus_id=syllabus.syllabus_id,
                    question_id=submission.question_id,
                    answer_text=normalized,
                    correctness=correctness,
                    marks_awarded=submission.marks_awarded,
                    marks_available=submission.marks_available,
                    group_ref=(
                        response_ref("group", submission.group_ref)
                        if submission.group_ref
                        else None
                    ),
                    identified=bool(submission.student_ref or submission.group_ref),
                    source=payload.source,
                    fingerprint=fingerprint,
                )
            )
        if inferred:
            warnings.append(
                Uncertainty(
                    code="correctness_inferred",
                    message=(
                        f"{inferred} response(s) carried no correctness label; it was inferred "
                        "from the marks awarded"
                    ),
                    severity=Severity.INFO,
                    detail={"inferred": inferred},
                )
            )
        if duplicate_count:
            warnings.append(
                Uncertainty(
                    code="duplicate_responses_skipped",
                    message=(
                        f"{duplicate_count} response(s) repeated an answer already in this import "
                        "and were counted once"
                    ),
                    severity=Severity.INFO,
                    detail={"duplicates": duplicate_count},
                )
            )
        with self.db.transaction() as session:
            inserted, already = ResponseRepository(session).add_many(records)
        if already:
            warnings.append(
                Uncertainty(
                    code="already_imported",
                    message=(
                        f"{already} response(s) were already stored and were not counted twice"
                    ),
                    severity=Severity.INFO,
                    detail={"already_present": already},
                )
            )
        return ImportSummary(
            course_id=payload.course_id,
            syllabus_id=syllabus.syllabus_id,
            supplied=len(payload.responses),
            inserted=inserted,
            duplicates=duplicate_count + already,
            inferred_correctness=inferred,
            warnings=warnings,
        )

    def _infer_correctness(self, submission: Any) -> ResponseCorrectness:
        if submission.marks_awarded is None or submission.marks_available is None:
            return ResponseCorrectness.UNCLEAR
        if submission.marks_awarded >= submission.marks_available:
            return ResponseCorrectness.CORRECT
        if submission.marks_awarded <= 0:
            return ResponseCorrectness.INCORRECT
        return ResponseCorrectness.PARTIALLY_CORRECT

    def analyse_misconceptions(
        self,
        course_id: str,
        *,
        syllabus_id: str | None = None,
        question_ids: Sequence[str] = (),
        min_support: int | None = None,
        persist: bool = True,
    ) -> MisconceptionReport:
        with self.db.session() as session:
            if CourseRepository(session).get(course_id) is None:
                raise AdvancedError(
                    "unknown_course",
                    f"no course {course_id!r} exists in this database",
                    detail={"course_id": course_id},
                )
            syllabi = SyllabusRepository(session).get_by_course(course_id)
            if not syllabi:
                raise AdvancedError(
                    "no_syllabus",
                    f"course {course_id} has no parsed syllabus",
                    detail={"course_id": course_id},
                )
            syllabus = self._pick_syllabus(syllabus_id, syllabi, course_id)
            responses = ResponseRepository(session).list(
                course_id,
                syllabus_id=syllabus.syllabus_id,
                question_ids=list(question_ids) if question_ids else None,
            )
        if not responses:
            report = MisconceptionReport(
                course_id=course_id,
                syllabus_id=syllabus.syllabus_id,
                warnings=[
                    Uncertainty(
                        code="no_responses",
                        message=(
                            "no stored responses exist for this course and syllabus, so no "
                            "misconception was mined; import responses first"
                        ),
                        severity=Severity.WARNING,
                    )
                ],
                limitations=[
                    "Nothing was analysed. Synthetic fixtures validate the pipeline; they are "
                    "not evidence about real learners.",
                ],
            )
            return report

        questions = self._question_contexts(course_id, syllabus.syllabus_id, responses)
        report = misconception.analyse(
            responses,
            questions,
            settings=self.advanced,
            course_id=course_id,
            syllabus_id=syllabus.syllabus_id,
            min_support=min_support,
        )
        if persist and report.records:
            with self.db.transaction() as session:
                stored = MisconceptionRepository(session).upsert_analysis(report.records)
            report.records = stored
        return report

    def _question_contexts(
        self, course_id: str, syllabus_id: str, responses: Sequence[ResponseRecord]
    ) -> dict[str, misconception.QuestionContext]:
        wanted = sorted({item.question_id for item in responses})
        contexts: dict[str, misconception.QuestionContext] = {}
        generated, historical = self._load_questions(wanted, course_id, syllabus_id)
        scope = self._scope_for(
            course_id, syllabus_id, unit_id=self._first_unit(course_id, syllabus_id)
        )
        sibling_cache: dict[str, list[str]] = {}
        for question_id, question in generated.items():
            reference = question.answer_key
            source = "answer_key"
            if reference is None and question.solution_steps:
                reference = " ".join(question.solution_steps)
                source = "rubric"
            concept_id = question.concept_ids[0] if question.concept_ids else None
            siblings = sibling_cache.setdefault(
                question.unit_id,
                self._sibling_labels(scope, concept_id, question.unit_id),
            )
            prerequisite, _ = (
                self._prerequisite(scope, concept_id, course_id) if concept_id else (None, False)
            )
            contexts[question_id] = misconception.QuestionContext(
                question_id=question_id,
                text=question.text,
                reference_answer=reference,
                concept_ids=tuple(question.concept_ids),
                concept_labels=tuple(question.concept_labels),
                sibling_labels=tuple(siblings),
                prerequisite_labels=(prerequisite,) if prerequisite else (),
                marks_available=question.marks,
                reference_source=source,
            )
        for question_id, record in historical.items():
            if question_id in contexts:
                continue
            with self.db.session() as session:
                analysis = QuestionUnderstandingRepository(session).get_analysis(question_id)
            unit_id = analysis.top_unit_id if analysis else None
            concept_ids: tuple[str, ...] = ()
            labels: tuple[str, ...] = ()
            other_labels: list[str] = []
            if unit_id:
                concepts = [
                    scope.concept(item)
                    for item in scope.unit_concepts(unit_id)
                    if scope.concept(item) is not None
                ]
                lowered = record.text.casefold()
                matched = [
                    item
                    for item in concepts
                    if item is not None
                    and any(
                        alias and alias.casefold() in lowered
                        for alias in {item.label, *item.aliases}
                    )
                ]
                concept_ids = tuple(item.concept_id for item in matched if item is not None)
                labels = tuple(item.label for item in matched if item is not None)
                other_labels = [
                    item.label for item in concepts if item is not None and item not in matched
                ]
            contexts[question_id] = misconception.QuestionContext(
                question_id=question_id,
                text=record.text,
                reference_answer=None,
                concept_ids=concept_ids,
                concept_labels=labels,
                sibling_labels=tuple(other_labels),
                marks_available=record.marks,
                reference_source="none",
            )
        return contexts

    def _known_question_ids(
        self, session: Any, course_id: str, question_ids: Sequence[str]
    ) -> set[str]:
        """Which of these ids exist in the course, without scanning every question."""

        known: set[str] = set()
        generation = GenerationRepository(session)
        questions = QuestionRepository(session)
        for question_id in set(question_ids):
            row = generation.get_question(question_id)
            if row is not None and row.course_id == course_id:
                known.add(question_id)
                continue
            record = questions.get(question_id)
            if record is not None and record.course_id == course_id:
                known.add(question_id)
        return known

    def _load_questions(
        self, question_ids: Sequence[str], course_id: str, syllabus_id: str
    ) -> tuple[dict[str, GeneratedQuestion], dict[str, QuestionRecord]]:
        """Fetch exactly the requested questions, refusing cross-course data."""

        generated: dict[str, GeneratedQuestion] = {}
        historical: dict[str, QuestionRecord] = {}
        with self.db.session() as session:
            generation = GenerationRepository(session)
            questions = QuestionRepository(session)
            for question_id in question_ids:
                row = generation.get_question(question_id)
                if (
                    row is not None
                    and row.course_id == course_id
                    and row.syllabus_id == syllabus_id
                ):
                    generated[question_id] = row
                    continue
                record = questions.get(question_id)
                if record is not None and record.course_id == course_id:
                    historical[question_id] = record
        return generated, historical

    def _first_unit(self, course_id: str, syllabus_id: str) -> str | None:
        with self.db.session() as session:
            units = SyllabusRepository(session).list_units(syllabus_id)
        return units[0].unit_id if units else None

    def _sibling_labels(
        self, scope: ScopeContext, concept_id: str | None, unit_id: str
    ) -> list[str]:
        labels: list[str] = []
        for item in scope.unit_concepts(unit_id):
            if item == concept_id:
                continue
            concept = scope.concept(item)
            if concept is not None:
                labels.append(concept.label)
        return labels

    # -- misconception queries and review ----------------------------------

    def list_misconceptions(self, query: MisconceptionQuery) -> list[MisconceptionRecord]:
        with self.db.session() as session:
            return MisconceptionRepository(session).list(query)

    def get_misconception(self, misconception_id: str) -> MisconceptionRecord:
        with self.db.session() as session:
            record = MisconceptionRepository(session).get(misconception_id)
        if record is None:
            raise AdvancedError(
                "unknown_misconception",
                f"no misconception record {misconception_id!r}",
                detail={"misconception_id": misconception_id},
            )
        return record

    def set_misconception_status(
        self,
        misconception_id: str,
        status: MisconceptionStatus,
        *,
        note: str | None = None,
        reviewer: str = "local-user",
    ) -> MisconceptionRecord:
        with self.db.transaction() as session:
            repo = MisconceptionRepository(session)
            if repo.get(misconception_id) is None:
                raise AdvancedError(
                    "unknown_misconception",
                    f"no misconception record {misconception_id!r}",
                    detail={"misconception_id": misconception_id},
                )
            return repo.set_review_status(misconception_id, status, note=note, reviewer=reviewer)

    def export_misconceptions(
        self, course_id: str, syllabus_id: str, path: Path
    ) -> tuple[Path, int]:
        from exam_engine.models.advanced import MisconceptionQuery as Query

        records = self.list_misconceptions(
            Query(course_id=course_id, syllabus_id=syllabus_id, limit=1000)
        )
        payload = {
            "schema_version": "phase11.misconception.export.v1",
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "exported_at": utcnow().isoformat(),
            "count": len(records),
            "records": [item.model_dump(mode="json") for item in records],
            "note": (
                "Records are candidate readings of stored answers, not diagnoses of students. "
                "Response identifiers are hashes and no student name is stored."
            ),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path, len(records)

    def export_batch(self, batch_id: str, path: Path) -> Path:
        return self.generation.export_batch(batch_id, path)

    def get_batch(self, batch_id: str) -> GenerationBatch:
        return self.generation.get_batch(batch_id)

    def get_question(self, question_id: str) -> GeneratedQuestion:
        return self.generation.get_question(question_id)

    def list_batches(self, course_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.generation.list_batches(course_id, limit=limit)

    def list_variants(
        self,
        course_id: str,
        *,
        origin: AssessmentOrigin,
        strategy: str | None = None,
        limit: int = 50,
    ) -> list[GeneratedQuestion]:
        questions = self.generation.list_questions(GenerationQuery(course_id=course_id, limit=1000))
        rows = [item for item in questions if item.origin == origin.value]
        if strategy:
            rows = [item for item in rows if item.strategy == strategy]
        return rows[:limit]

    def set_review_status(
        self,
        question_id: str,
        status: GenerationReviewStatus,
        *,
        note: str | None = None,
        reviewer: str = "local-user",
        expected_revision: int | None = None,
    ) -> GeneratedQuestion:
        return self.generation.set_review_status(
            question_id,
            status,
            note=note,
            reviewer=reviewer,
            expected_revision=expected_revision,
        )

    # -- shared helpers ----------------------------------------------------

    def _pick_syllabus(
        self, syllabus_id: str | None, syllabi: Sequence[Any], course_id: str
    ) -> Any:
        if syllabus_id:
            match = next((item for item in syllabi if item.syllabus_id == syllabus_id), None)
            if match is None:
                raise AdvancedError(
                    "unknown_syllabus",
                    f"syllabus {syllabus_id!r} does not belong to course {course_id}",
                    detail={"available": [item.syllabus_id for item in syllabi]},
                )
            return match
        if len(syllabi) > 1:
            raise AdvancedError(
                "ambiguous_syllabus",
                (
                    f"course {course_id} has {len(syllabi)} syllabus versions; pass --syllabus "
                    "to choose one instead of guessing"
                ),
                detail={"available": [item.syllabus_id for item in syllabi]},
            )
        return syllabi[0]

    def _duplicate(
        self,
        question: GeneratedQuestion,
        *,
        pool: list[dedupe.PoolItem],
        course_id: str,
        settings: dedupe.DedupeSettings,
        semantic: Any,
    ) -> Any:
        from exam_engine.understanding.similarity import ReasoningProfile

        findings = dedupe.check_candidate(
            question.text,
            candidate_id=question.question_id,
            pool=pool,
            course_id=course_id,
            settings=settings,
            semantic=semantic,
            profile=ReasoningProfile(
                question_type=question.question_type,
                bloom_level=question.requested_bloom.value,
            ),
            concept_ids=tuple(question.concept_ids),
        )
        return findings[0] if findings else None

    def _record_duplicate(
        self,
        finding: Any,
        question: GeneratedQuestion,
        index: int,
        duplicates: list[DuplicateResolution],
        rejections: list[GenerationRejection],
        counts: GenerationCounts,
    ) -> bool:
        duplicates.append(
            DuplicateResolution(
                index=index,
                concept_id=question.concept_ids[0] if question.concept_ids else "",
                kind=finding.kind,
                matched_question_id=finding.matched_question_id,
                score=finding.score,
                reason=f"{finding.reason} (matched {finding.source} question)",
            )
        )
        if finding.is_duplicate:
            counts.duplicates += 1
            counts.rejected += 1
            rejections.append(
                GenerationRejection(
                    index=index,
                    concept_id=question.concept_ids[0] if question.concept_ids else "",
                    concept_label=(question.concept_labels[0] if question.concept_labels else ""),
                    format=question.format,
                    code=f"duplicate_{finding.kind}",
                    message=f"{finding.reason}: {finding.matched_question_id}",
                    text=question.text,
                )
            )
            return True
        question.generation_metadata["similar_to"] = {
            "question_id": finding.matched_question_id,
            "kind": finding.kind,
            "score": finding.score,
        }
        return False

    def _count_validation(self, counts: GenerationCounts, question: GeneratedQuestion) -> None:
        if question.validation.status is GenerationValidationStatus.VALIDATED:
            counts.validated += 1
        elif question.validation.status is GenerationValidationStatus.NEEDS_REVIEW:
            counts.needs_review += 1
        elif question.validation.status is GenerationValidationStatus.VALIDATION_INCOMPLETE:
            counts.validation_incomplete += 1

    def _status(self, counts: GenerationCounts, accepted: int, requested: int) -> GenerationStatus:
        if accepted == 0:
            return GenerationStatus.FAILED
        if accepted < requested or counts.rejected or counts.failed or counts.validation_incomplete:
            return GenerationStatus.PARTIAL
        return GenerationStatus.OK

    def _batch_warnings(self, capability: str, counts: GenerationCounts) -> list[Uncertainty]:
        warnings: list[Uncertainty] = []
        if counts.needs_review:
            warnings.append(
                Uncertainty(
                    code=f"{capability}_review_required",
                    message=(
                        f"{counts.needs_review} question(s) need human review before use; their "
                        "automated checks did not establish every promised property"
                    ),
                    severity=Severity.INFO,
                    detail={"needs_review": counts.needs_review},
                )
            )
        if counts.failed:
            warnings.append(
                Uncertainty(
                    code=f"{capability}_partial_failure",
                    message=(
                        f"{counts.failed} candidate(s) could not be produced; each rejection "
                        "carries its cause"
                    ),
                    severity=Severity.WARNING,
                    detail={"failed": counts.failed},
                )
            )
        return warnings

    def _batch_id(self, capability: str, course_id: str, syllabus_id: str, request: Any) -> str:
        payload = json.dumps(
            {
                "algorithm": ALGORITHM,
                "version": VERSION,
                "capability": capability,
                "course": course_id,
                "syllabus": syllabus_id,
                "request": request.model_dump(mode="json"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return "gb_" + hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _question_id(self, batch_id: str, target_id: str, strategy: str, index: int) -> str:
        payload = f"{batch_id}\x1f{target_id}\x1f{strategy}\x1f{index}"
        return "gq_" + hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _scope(
        self, course_id: str, scope: ScopeContext, source: SourceQuestion
    ) -> GenerationScope:
        documents = sorted({item.document_id for item in source.source_references})
        return GenerationScope(
            document_ids=documents,
            syllabus_id=scope.syllabus_id,
            unit_ids=[source.unit_id],
            concept_ids=list(source.concept_ids),
            statement=(
                f"variants of question {source.question_id} inside syllabus {scope.syllabus_id}, "
                f"concepts {', '.join(source.concept_labels) or 'none resolved'}"
            ),
        )

    def _generation_request(
        self, request: MutationRequest, source: SourceQuestion
    ) -> GenerationRequest:
        return GenerationRequest(
            course_id=request.course_id,
            syllabus_id=source.syllabus_id,
            concept_ids=list(source.concept_ids) or [source.unit_id],
            unit_id=source.unit_id,
            count=request.count,
            format=source.format,
            difficulty=request.difficulty,
            bloom=request.bloom,
            marks=source.marks,
            seed=request.seed,
        )

    def _adversarial_generation_request(
        self, request: AdversarialRequest, targets: Sequence[Any]
    ) -> GenerationRequest:
        return GenerationRequest(
            course_id=request.course_id,
            syllabus_id=request.syllabus_id,
            concept_ids=[item.concept_id for item in targets],
            unit_id=request.unit_id,
            count=request.count,
            format=GenerationFormat.EXPLANATION,
            difficulty=request.difficulty,
            bloom=request.bloom,
            seed=request.seed,
        )

    def _persist(self, batch: GenerationBatch) -> None:
        with self.db.transaction() as session:
            repo = GenerationRepository(session)
            repo.upsert_batch(batch)
            persisted = 0
            for question in batch.questions:
                result = repo.save_question(question, allow_overwrite_approved=False)
                if result in {"created", "updated", "unchanged"}:
                    persisted += 1
                elif result == "approved_locked":
                    batch.warnings.append(
                        Uncertainty(
                            code="approved_locked",
                            message=(
                                f"approved question {question.question_id} was not overwritten"
                            ),
                            severity=Severity.WARNING,
                            detail={"question_id": question.question_id},
                        )
                    )
            batch.counts.persisted = persisted
            repo.upsert_batch(batch)

    def _persist_batch_row(self, batch: GenerationBatch) -> None:
        with self.db.transaction() as session:
            GenerationRepository(session).upsert_batch(batch)


def _temp(text: str, course_id: str) -> QuestionRecord:
    """Minimal structure input; the Phase 3 detectors only need text and identity."""

    return QuestionRecord(
        question_id="temp",
        document_id="phase11",
        course_id=course_id,
        text=text,
    )


__all__ = [
    "ALGORITHM",
    "VERSION",
    "AdvancedAssessmentService",
    "ImportSummary",
    "SourceBundle",
]
