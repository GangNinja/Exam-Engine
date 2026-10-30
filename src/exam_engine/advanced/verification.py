"""Phase 11 verification: strategy invariants and adversarial fairness.

Phase 10 already validates structure, syllabus alignment, answerability, clarity and
cognitive alignment. This module adds the two checks Phase 10 cannot make:

* did the mutation keep the properties its strategy promised (objective, concepts,
  answer key, format), and
* is an adversarial question fair — supplied context, a defensible answer or rubric,
  no answer revealed in the prompt, and no concept outside the declared scope.

A finding is never a claim that a question is educationally sound.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from exam_engine.advanced.mutation import (
    INVARIANT_ANSWER,
    INVARIANT_CONCEPT,
    INVARIANT_OBJECTIVE,
    MutationSpec,
)
from exam_engine.advanced.source import SourceQuestion
from exam_engine.generation.validation import ValidationContext, validate_question
from exam_engine.models.enums import (
    BloomLevel,
    ChallengeStrategy,
    DifficultyLabel,
    GenerationValidationStatus,
    Severity,
)
from exam_engine.models.generation import GeneratedQuestion, ValidationIssue, ValidationResult

#: Formats whose answers are open-ended enough to need a model answer or rubric.
_OPEN_FORMATS = {
    "explanation",
    "long_answer",
    "case_based",
    "design",
    "evaluation",
    "analysis",
    "application",
}

#: Words that signal the question itself is unfair rather than merely hard.
_UNFAIR_WORDS = ("gotcha", "trick question", "obviously", "as you should already know")

#: Strategies that legitimately state a claim the learner must test.
_CLAIM_STRATEGIES = {
    ChallengeStrategy.COUNTEREXAMPLE,
    ChallengeStrategy.FALSE_ASSUMPTION,
    ChallengeStrategy.WHY_WRONG,
    ChallengeStrategy.ERROR_DIAGNOSIS,
}


def _normalise(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def _status_for(issues: list[ValidationIssue]) -> GenerationValidationStatus:
    if any(item.severity is Severity.ERROR for item in issues):
        return GenerationValidationStatus.REJECTED
    if any(item.severity is Severity.WARNING for item in issues):
        return GenerationValidationStatus.NEEDS_REVIEW
    if any(item.severity is Severity.INFO for item in issues):
        return GenerationValidationStatus.VALIDATION_INCOMPLETE
    return GenerationValidationStatus.VALIDATED


def _downgrade(issues: list[ValidationIssue], codes: frozenset[str]) -> list[ValidationIssue]:
    """Turn a Phase 10 error into a review warning for a derived variant.

    A strategy that changes what a complete answer must contain legitimately leaves
    the question without a key until a reviewer recomputes one, so the Phase 10 rule
    "a short-answer question must carry a key" becomes a review item rather than a
    rejection. Nothing is dropped: the finding keeps its code and gains a note.
    """

    if not codes:
        return issues
    result: list[ValidationIssue] = []
    for issue in issues:
        if issue.code in codes and issue.severity is Severity.ERROR:
            result.append(
                issue.model_copy(
                    update={
                        "severity": Severity.WARNING,
                        "message": (
                            f"{issue.message} (derived variant: the answer must be recomputed "
                            "and reviewed)"
                        ),
                    }
                )
            )
        else:
            result.append(issue)
    return result


def _merge(
    base: ValidationResult,
    extra: list[ValidationIssue],
    checks: list[str],
    downgrade: frozenset[str] = frozenset(),
) -> ValidationResult:
    issues = [*_downgrade(base.issues, downgrade), *extra]
    errors = sum(1 for item in issues if item.severity is Severity.ERROR)
    warnings = sum(1 for item in issues if item.severity is Severity.WARNING)
    confidence = round(max(0.0, min(1.0, 0.9 - 0.18 * errors - 0.06 * warnings)), 4)
    return ValidationResult(
        status=_status_for(issues),
        issues=issues,
        checks=[*base.checks, *checks],
        confidence=confidence,
    )


@dataclass
class VerificationRequest:
    """What the caller promised to preserve (or to change)."""

    preserve_objective: bool = True
    preserve_concept: bool = True
    preserve_answer: bool = True
    allow_format_change: bool = False
    allow_out_of_scope: bool = False
    rubric_required: bool = False
    allowed_concept_ids: frozenset[str] = frozenset()
    source: SourceQuestion | None = None
    spec: MutationSpec | None = None
    challenge: ChallengeStrategy | None = None
    extra_checks: list[str] = field(default_factory=list)


def verify(
    question: GeneratedQuestion,
    context: ValidationContext,
    request: VerificationRequest,
    *,
    downgrade: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Run Phase 10 checks plus the Phase 11 invariant or fairness checks."""

    base = validate_question(question, context)
    extra: list[ValidationIssue] = []
    checks: list[str] = []
    if request.source is not None and request.spec is not None:
        extra, checks = _mutation_findings(question, request)
    if request.challenge is not None:
        extra, checks = _adversarial_findings(question, request, extra, checks)
    return _merge(base, extra, checks, downgrade)


def _mutation_findings(
    question: GeneratedQuestion, request: VerificationRequest
) -> tuple[list[ValidationIssue], list[str]]:
    source = request.source
    spec = request.spec
    assert source is not None and spec is not None
    issues: list[ValidationIssue] = []
    checks: list[str] = []

    checks.append("mutation.distinct_from_source")
    if _normalise(question.text) == _normalise(source.text):
        issues.append(
            ValidationIssue(
                code="mutation_identical_to_source",
                message="the variant is word-for-word the question it was derived from",
                severity=Severity.ERROR,
                rule="mutation.distinct_from_source",
            )
        )

    checks.append("mutation.objective_preserved")
    if (
        request.preserve_objective
        and invariant_required(spec, INVARIANT_OBJECTIVE, request)
        and _normalise(question.learning_objective) != _normalise(source.objective)
    ):
        issues.append(
            ValidationIssue(
                code="learning_objective_changed",
                message=(
                    "the request required the learning objective to stay the same, but the "
                    "mutation rewrote it"
                ),
                severity=Severity.ERROR,
                rule="mutation.objective_preserved",
                detail={"before": source.objective, "after": question.learning_objective},
            )
        )

    checks.append("mutation.concept_preserved")
    if (
        request.preserve_concept
        and invariant_required(spec, INVARIANT_CONCEPT, request)
        and set(question.concept_ids) != set(source.concept_ids)
    ):
        issues.append(
            ValidationIssue(
                code="concept_changed",
                message=("the assessed concept changed although the request required it unchanged"),
                severity=Severity.ERROR,
                rule="mutation.concept_preserved",
                detail={
                    "source": sorted(source.concept_ids),
                    "variant": sorted(question.concept_ids),
                },
            )
        )

    checks.append("mutation.answer_consistency")
    if request.preserve_answer and invariant_required(spec, INVARIANT_ANSWER, request):
        if source.answer_key is None:
            issues.append(
                ValidationIssue(
                    code="answer_unverifiable",
                    message=(
                        "the source question has no answer key, so answer preservation could not "
                        "be checked and must be confirmed by a reviewer"
                    ),
                    severity=Severity.WARNING,
                    rule="mutation.answer_consistency",
                )
            )
        elif _normalise(question.answer_key) != _normalise(source.answer_key):
            issues.append(
                ValidationIssue(
                    code="answer_key_changed",
                    message=(
                        "the request required the answer to stay equivalent, but the answer key "
                        "was changed"
                    ),
                    severity=Severity.ERROR,
                    rule="mutation.answer_consistency",
                )
            )
    elif (
        spec.changes_answer
        and source.answer_key is not None
        and _normalise(question.answer_key) == _normalise(source.answer_key)
    ):
        issues.append(
            ValidationIssue(
                code="stale_answer_key",
                message=(
                    "this strategy changes what a complete answer must contain, but the original "
                    "answer key was carried over unchanged"
                ),
                severity=Severity.ERROR,
                rule="mutation.answer_consistency",
                detail={"strategy": spec.strategy.value},
            )
        )
    elif spec.changes_answer:
        issues.append(
            ValidationIssue(
                code="answer_equivalence_unverified",
                message=(
                    "the answer changed; equivalence with the source answer is not established and "
                    "needs human review"
                ),
                severity=Severity.WARNING,
                rule="mutation.answer_consistency",
            )
        )

    checks.append("mutation.format_preserved")
    if not request.allow_format_change and question.format is not source.format:
        issues.append(
            ValidationIssue(
                code="format_changed",
                message="the mutation changed the response format without being allowed to",
                severity=Severity.ERROR,
                rule="mutation.format_preserved",
                detail={"before": source.format.value, "after": question.format.value},
            )
        )

    checks.append("mutation.numeric_validity")
    if (
        question.format.value == "numerical"
        and question.answer_key is None
        and not question.solution_steps
    ):
        issues.append(
            ValidationIssue(
                code="numerical_answer_removed",
                message=(
                    "numeric parameters changed and the answer key was removed; the solution "
                    "must be recomputed before use"
                ),
                severity=Severity.WARNING,
                rule="mutation.numeric_validity",
            )
        )

    return issues, checks


def _adversarial_findings(
    question: GeneratedQuestion,
    request: VerificationRequest,
    extra: list[ValidationIssue],
    checks: list[str],
) -> tuple[list[ValidationIssue], list[str]]:
    strategy = request.challenge
    issues = list(extra)
    checks = list(checks)

    checks.append("adversarial.context_supplied")
    quoted = '"' in question.text or "“" in question.text
    if not quoted and not question.source_references:
        issues.append(
            ValidationIssue(
                code="missing_essential_context",
                message=(
                    "the question supplies no source context, so the learner cannot see what "
                    "information the task depends on"
                ),
                severity=Severity.ERROR,
                rule="adversarial.context_supplied",
            )
        )

    checks.append("adversarial.grading_material")
    if request.rubric_required and question.answer_key is None and not question.solution_steps:
        issues.append(
            ValidationIssue(
                code="missing_grading_rubric",
                message=(
                    "an open-ended challenge needs a model answer or a grading rubric before it "
                    "can be marked"
                ),
                severity=Severity.ERROR,
                rule="adversarial.grading_material",
            )
        )

    checks.append("adversarial.answer_not_revealed")
    key = (question.answer_key or "").strip()
    if len(key) >= 8 and _normalise(key) in _normalise(question.text):
        issues.append(
            ValidationIssue(
                code="answer_revealed_in_question",
                message="the student-facing text contains the answer key",
                severity=Severity.ERROR,
                rule="adversarial.answer_not_revealed",
            )
        )

    checks.append("adversarial.scope")
    outside = sorted(set(question.concept_ids) - request.allowed_concept_ids)
    if outside and not request.allow_out_of_scope:
        issues.append(
            ValidationIssue(
                code="out_of_scope_concept",
                message=(
                    "the question depends on concepts outside the declared syllabus scope and the "
                    "request did not allow that"
                ),
                severity=Severity.ERROR,
                rule="adversarial.scope",
                detail={"concept_ids": outside},
            )
        )

    checks.append("adversarial.single_task")
    if question.text.count("?") > 2:
        issues.append(
            ValidationIssue(
                code="ambiguous_task",
                message="the text asks several questions at once, which makes the task unclear",
                severity=Severity.WARNING,
                rule="adversarial.single_task",
            )
        )

    checks.append("adversarial.fair_wording")
    lowered = question.text.casefold()
    if strategy not in _CLAIM_STRATEGIES:
        hits = [word for word in _UNFAIR_WORDS if word in lowered]
        if hits:
            issues.append(
                ValidationIssue(
                    code="possible_misleading_wording",
                    message=(
                        "the wording hints that the task is a trick rather than a test of reasoning"
                    ),
                    severity=Severity.WARNING,
                    rule="adversarial.fair_wording",
                    detail={"phrases": hits},
                )
            )

    checks.append("adversarial.rubric_depth")
    if question.format.value in _OPEN_FORMATS and len(question.solution_steps) < 2:
        issues.append(
            ValidationIssue(
                code="thin_grading_rubric",
                message=(
                    "an open-ended challenge should list at least two rubric criteria so marking "
                    "is consistent"
                ),
                severity=Severity.WARNING,
                rule="adversarial.rubric_depth",
            )
        )

    checks.append("adversarial.difficulty_justification")
    if question.requested_difficulty is DifficultyLabel.EASY and question.requested_bloom in {
        BloomLevel.CREATE,
        BloomLevel.EVALUATE,
    }:
        issues.append(
            ValidationIssue(
                code="difficulty_bloom_mismatch",
                message=(
                    f"a {question.requested_bloom.value}-level task is not an easy task; state a "
                    "difficulty consistent with the reasoning required"
                ),
                severity=Severity.WARNING,
                rule="adversarial.difficulty_justification",
            )
        )
    return issues, checks


def invariant_required(spec: MutationSpec, invariant: str, request: VerificationRequest) -> bool:
    """True when the request asks for a property this strategy claims to preserve."""

    return invariant in spec.invariants


__all__ = ["VerificationRequest", "invariant_required", "verify"]
