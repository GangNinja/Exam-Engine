"""Automated quality validation for generated questions.

Generation success is not validity. This module runs independent, named checks
over a fully assembled question and reports every finding with a stable code.
A ``VALIDATED`` result means the observable, checkable properties held - it is
never a claim that the question is educationally correct or that its answer is
the only defensible one. Anything that needs human judgement is downgraded to
``NEEDS_REVIEW``.

Checks are grouped the way the phase specification describes them: structure,
syllabus alignment, answerability, multiple-choice quality, clarity, and
difficulty/Bloom alignment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from exam_engine.config import GenerationSettings
from exam_engine.models.enums import (
    DifficultyLabel,
    GenerationFormat,
    GenerationValidationStatus,
    Severity,
)
from exam_engine.models.generation import (
    GeneratedQuestion,
    SourcePassage,
    ValidationIssue,
    ValidationResult,
)
from exam_engine.models.question import Uncertainty

_QUOTED_RE = re.compile(r'["“”](?P<quote>[^"“”]{12,400})["“”]')
_PLACEHOLDER_RE = re.compile(r"[{}]|\bTBD\b|\bTODO\b|\bNone\b")
_TERMINAL_RE = re.compile(r"[?.!:]\s*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_VAGUE_RE = re.compile(r"\b(etc\.|and/or|somehow|various things)\b", re.IGNORECASE)
_META_OPTION_RE = re.compile(r"^(all|none) of the above\.?$", re.IGNORECASE)
_BOUND_REFERENCE_RE = re.compile(
    r"\bthe (case|passage|text|diagram) (above|below)\b", re.IGNORECASE
)
_LONG_FORMATS = frozenset(
    {
        GenerationFormat.LONG_ANSWER,
        GenerationFormat.DESIGN,
        GenerationFormat.CASE_BASED,
        GenerationFormat.EVALUATION,
        GenerationFormat.ANALYSIS,
    }
)
_KEY_REQUIRED_FORMATS = frozenset({GenerationFormat.MULTIPLE_CHOICE, GenerationFormat.SHORT_ANSWER})


@dataclass(frozen=True)
class ValidationContext:
    """Everything validation may inspect; no hidden database access."""

    settings: GenerationSettings
    allowed_concept_ids: frozenset[str] = frozenset()
    concept_labels: dict[str, str] = field(default_factory=dict)
    allowed_unit_ids: frozenset[str] = frozenset()
    forbidden_labels: frozenset[str] = frozenset()
    evidence: tuple[SourcePassage, ...] = ()
    thin_evidence: bool = False
    phase3_available: bool = True


@dataclass
class _Findings:
    """One validation pass: the subject, its scope and the accrued findings."""

    question: GeneratedQuestion
    context: ValidationContext
    issues: list[ValidationIssue] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    def add(
        self,
        code: str,
        message: str,
        rule: str,
        *,
        severity: Severity = Severity.WARNING,
        detail: dict | None = None,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                code=code,
                message=message,
                rule=rule,
                severity=severity,
                detail=detail or {},
            )
        )


def _words(text: str) -> list[str]:
    return text.split()


def _normalise(text: str) -> str:
    return " ".join(text.casefold().split())


def _evidence_text(passages: tuple[SourcePassage, ...]) -> str:
    return _normalise(" ".join(passage.excerpt for passage in passages))


def _check_structure(findings: _Findings) -> None:
    question = findings.question
    findings.checks.append("structure.required_fields")
    words = _words(question.text)
    if not question.text.strip():
        findings.add(
            "missing_question_text",
            "the question text is empty",
            "structure.required_fields",
            severity=Severity.ERROR,
        )
    elif len(words) < 3:
        findings.add(
            "question_too_short",
            "the question text is too short to state a task",
            "structure.required_fields",
            severity=Severity.ERROR,
            detail={"words": len(words)},
        )
    elif len(words) < 6:
        findings.add(
            "question_brief",
            "the question is very short; check that the task is unambiguous",
            "structure.clarity",
            detail={"words": len(words)},
        )

    findings.checks.append("structure.format_match")
    if question.format is GenerationFormat.MULTIPLE_CHOICE:
        if not question.options or not question.correct_option_key:
            findings.add(
                "format_mismatch",
                "a multiple-choice question needs options and a correct option key",
                "structure.format_match",
                severity=Severity.ERROR,
            )
    elif question.options:
        findings.add(
            "format_mismatch",
            f"a {question.format.value} question must not carry answer options",
            "structure.format_match",
            severity=Severity.ERROR,
        )

    findings.checks.append("structure.marks")
    if question.marks <= 0.0 or question.marks > 100.0:
        findings.add(
            "invalid_marks",
            "marks must be greater than zero and at most 100",
            "structure.marks",
            severity=Severity.ERROR,
            detail={"marks": question.marks},
        )
    elif question.format is GenerationFormat.MULTIPLE_CHOICE and question.marks > 5:
        findings.add(
            "unusual_marks",
            "a multiple-choice question carrying more than 5 marks is unusual",
            "structure.marks",
            detail={"marks": question.marks},
        )
    if question.marks < 3.0 and question.format in _LONG_FORMATS:
        findings.add(
            "marks_task_mismatch",
            f"{question.format.value} tasks normally carry more than 2 marks",
            "structure.marks",
            detail={"marks": question.marks},
        )

    findings.checks.append("structure.rendering")
    if _PLACEHOLDER_RE.search(question.text):
        findings.add(
            "unrendered_placeholder",
            "the question text still contains a template placeholder or missing value",
            "structure.rendering",
            severity=Severity.ERROR,
        )
    if not _TERMINAL_RE.search(question.text):
        findings.add(
            "missing_terminal_punctuation",
            "the question text does not end with punctuation",
            "structure.rendering",
        )


def _check_alignment(findings: _Findings) -> None:
    question = findings.question
    context = findings.context
    findings.checks.append("alignment.concept_scope")
    if not question.concept_ids:
        findings.add(
            "missing_concepts",
            "the question has no target concept",
            "alignment.concept_scope",
            severity=Severity.ERROR,
        )
    unknown = [item for item in question.concept_ids if item not in context.allowed_concept_ids]
    if unknown:
        findings.add(
            "concept_out_of_scope",
            "a target concept is not part of the selected course and syllabus",
            "alignment.concept_scope",
            severity=Severity.ERROR,
            detail={"concept_ids": unknown},
        )

    findings.checks.append("alignment.unit_scope")
    if question.unit_id not in context.allowed_unit_ids:
        findings.add(
            "unit_out_of_scope",
            "the question unit is not part of the selected syllabus",
            "alignment.unit_scope",
            severity=Severity.ERROR,
            detail={"unit_id": question.unit_id},
        )

    findings.checks.append("alignment.concept_named")
    visible = _normalise(" ".join([question.text, *(option.text for option in question.options)]))
    labels = [context.concept_labels.get(item, "") for item in question.concept_ids]
    if labels and not any(_normalise(label) in visible for label in labels if label):
        findings.add(
            "concept_absent_from_question",
            "the question never names or quotes its target concept",
            "alignment.concept_named",
        )

    findings.checks.append("alignment.source_evidence")
    if not question.source_references and not context.evidence:
        findings.add(
            "missing_source_evidence",
            "no source passage supports this question",
            "alignment.source_evidence",
            severity=Severity.ERROR,
        )
    elif context.thin_evidence:
        findings.add(
            "thin_evidence",
            (
                "the only available evidence is the syllabus concept label; the task is "
                "grounded but the source material is thin"
            ),
            "alignment.source_evidence",
        )

    findings.checks.append("alignment.course_isolation")
    foreign = [
        label
        for label in context.forbidden_labels
        if len(label) >= 6 and _normalise(label) in visible
    ]
    if foreign:
        findings.add(
            "foreign_material_leak",
            "the question mentions a concept that belongs to another course",
            "alignment.course_isolation",
            detail={"labels": sorted(foreign)[:5]},
        )


def _check_answerability(findings: _Findings) -> None:
    question = findings.question
    context = findings.context
    findings.checks.append("answerability.context")
    if question.format is GenerationFormat.CASE_BASED and not _QUOTED_RE.search(question.text):
        findings.add(
            "case_context_missing",
            "a case-based question must quote the case it refers to",
            "answerability.context",
            severity=Severity.ERROR,
        )
    if _BOUND_REFERENCE_RE.search(question.text):
        findings.add(
            "unbound_reference",
            "the question refers to material that is not included",
            "answerability.context",
        )

    findings.checks.append("answerability.answer_key")
    if question.answer_key is None:
        if question.format in _KEY_REQUIRED_FORMATS:
            findings.add(
                "missing_answer_key",
                f"a {question.format.value} question must include an answer key",
                "answerability.answer_key",
                severity=Severity.ERROR,
            )
        else:
            findings.add(
                "answer_key_not_derived",
                (
                    "no reference answer was derived for this open task; a reviewer must "
                    "confirm what a complete answer requires"
                ),
                "answerability.answer_key",
            )
    else:
        findings.checks.append("answerability.answer_key_grounded")
        quoted = [match.group("quote") for match in _QUOTED_RE.finditer(question.answer_key)]
        if quoted:
            # A comparison key quotes the second concept's passage too, so every
            # citation attached to the question counts, not only the target evidence.
            evidence = _evidence_text(tuple(context.evidence) + tuple(question.source_references))
            unsupported = [quote for quote in quoted if _normalise(quote)[:80] not in evidence]
            if unsupported:
                findings.add(
                    "answer_key_unverified",
                    "the answer key quotes material that is not in the cited evidence",
                    "answerability.answer_key_grounded",
                    severity=(
                        Severity.ERROR
                        if context.settings.require_evidence_for_answer
                        else Severity.WARNING
                    ),
                    detail={"quotes": [item[:80] for item in unsupported[:2]]},
                )
        else:
            findings.add(
                "answer_key_unverified",
                (
                    "the answer key is not a quoted source passage, so automated checks "
                    "cannot confirm it against the evidence"
                ),
                "answerability.answer_key_grounded",
            )

    findings.checks.append("answerability.solution_steps")
    if (
        question.format is GenerationFormat.NUMERICAL
        and not question.solution_steps
        and question.answer_key is not None
    ):
        findings.add(
            "missing_solution_steps",
            "a numerical question with an answer should show its solution steps",
            "answerability.solution_steps",
        )


def _check_mcq(findings: _Findings) -> None:
    question = findings.question
    settings = findings.context.settings
    if question.format is not GenerationFormat.MULTIPLE_CHOICE:
        return
    options = question.options
    findings.checks.append("mcq.option_count")
    if not settings.min_mcq_options <= len(options) <= settings.max_mcq_options:
        findings.add(
            "mcq_option_count",
            (
                f"multiple-choice questions need between {settings.min_mcq_options} and "
                f"{settings.max_mcq_options} options; found {len(options)}"
            ),
            "mcq.option_count",
            severity=Severity.ERROR,
            detail={"options": len(options)},
        )

    findings.checks.append("mcq.single_correct")
    correct = [option for option in options if option.is_correct]
    if len(correct) != 1:
        findings.add(
            "mcq_correct_count",
            f"exactly one option must be marked correct; found {len(correct)}",
            "mcq.single_correct",
            severity=Severity.ERROR,
        )
    if correct and question.correct_option_key != correct[0].key:
        findings.add(
            "mcq_key_mismatch",
            "the stored correct option key does not match the correct option",
            "mcq.single_correct",
            severity=Severity.ERROR,
            detail={"key": question.correct_option_key, "option": correct[0].key},
        )

    findings.checks.append("mcq.options_distinct")
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for option in options:
        normalised = _normalise(option.text)
        if normalised in seen:
            duplicates.append(option.key)
        seen[normalised] = option.key
        if len(option.text.strip()) < 2:
            findings.add(
                "mcq_empty_option",
                f"option {option.key} is empty or too short",
                "mcq.options_distinct",
                severity=Severity.ERROR,
            )
        if _META_OPTION_RE.match(option.text.strip()):
            findings.add(
                "mcq_meta_option",
                (f"option {option.key} is a meta option that tests reading rather than knowledge"),
                "mcq.options_distinct",
            )
    if duplicates:
        findings.add(
            "mcq_duplicate_options",
            "two options share the same text",
            "mcq.options_distinct",
            severity=Severity.ERROR,
            detail={"keys": duplicates},
        )

    findings.checks.append("mcq.answer_leakage")
    stem = _normalise(question.text)
    if correct and _normalise(correct[0].text) in stem:
        findings.add(
            "answer_leakage",
            "the correct option is repeated verbatim in the question stem",
            "mcq.answer_leakage",
            severity=Severity.ERROR,
        )

    findings.checks.append("mcq.stem_unambiguous")
    competing = [
        option.key
        for option in options
        if not option.is_correct
        and len(option.text.strip()) >= 3
        and _normalise(option.text) in stem
    ]
    if competing:
        findings.add(
            "mcq_ambiguous_stem",
            ("the stem also contains another option, so more than one answer appears to fit"),
            "mcq.stem_unambiguous",
            severity=Severity.ERROR,
            detail={"keys": competing},
        )

    findings.checks.append("mcq.answer_key")
    if (
        question.answer_key
        and correct
        and _normalise(question.answer_key) != _normalise(correct[0].text)
    ):
        findings.add(
            "mcq_answer_key_mismatch",
            "the answer key does not repeat the correct option",
            "mcq.answer_key",
            severity=Severity.ERROR,
        )
    missing_notes = [
        option.key
        for option in options
        if not option.is_correct and option.key not in question.distractor_explanations
    ]
    if missing_notes:
        findings.add(
            "mcq_missing_distractor_notes",
            "some distractors have no explanation of why they are wrong",
            "mcq.answer_key",
            detail={"keys": missing_notes},
        )


def _check_clarity(findings: _Findings) -> None:
    question = findings.question
    findings.checks.append("quality.repetition")
    sentences = [item.strip() for item in _SENTENCE_SPLIT_RE.split(question.text) if item.strip()]
    if len(sentences) != len({_normalise(item) for item in sentences}):
        findings.add(
            "repeated_sentence",
            "the question repeats the same sentence",
            "quality.repetition",
        )

    findings.checks.append("quality.vague_wording")
    if _VAGUE_RE.search(question.text):
        findings.add(
            "vague_wording",
            "the wording leaves the required scope open",
            "quality.vague_wording",
        )

    findings.checks.append("quality.single_task")
    if question.format is not GenerationFormat.CASE_BASED and question.text.count("?") > 1:
        findings.add(
            "multiple_questions",
            "the item contains more than one question mark; check that it asks one task",
            "quality.single_task",
        )

    findings.checks.append("quality.assumptions")
    if question.format is GenerationFormat.NUMERICAL and not question.source_references:
        findings.add(
            "unsupported_numeric_task",
            "a numerical task without cited evidence may rely on unstated assumptions",
            "quality.assumptions",
            severity=Severity.ERROR,
        )


def _check_cognitive(findings: _Findings) -> None:
    question = findings.question
    findings.checks.append("cognitive.bloom_alignment")
    if not findings.context.phase3_available or question.estimated_bloom is None:
        findings.add(
            "bloom_not_estimated",
            (
                "no Bloom classification is available for this text, so alignment with "
                "the requested level could not be checked"
            ),
            "cognitive.bloom_alignment",
            severity=Severity.INFO,
        )
    elif question.generation_metadata.get("bloom_explicit") is False:
        pass
    else:
        if question.estimated_bloom is not question.requested_bloom:
            findings.add(
                "bloom_mismatch",
                (
                    f"the requested level is {question.requested_bloom.value} but the "
                    f"observed cue profile is {question.estimated_bloom.value}"
                ),
                "cognitive.bloom_alignment",
                detail={
                    "requested": question.requested_bloom.value,
                    "estimated": question.estimated_bloom.value,
                    "confidence": question.bloom_confidence,
                },
            )
        if question.bloom_confidence < 0.25:
            findings.add(
                "bloom_low_confidence",
                "the Bloom classification is weak, so the level needs human review",
                "cognitive.bloom_alignment",
                severity=Severity.INFO,
                detail={"confidence": question.bloom_confidence},
            )

    findings.checks.append("cognitive.difficulty_alignment")
    if question.estimated_difficulty is DifficultyLabel.UNKNOWN:
        findings.add(
            "difficulty_not_estimated",
            "difficulty could not be estimated from observable features",
            "cognitive.difficulty_alignment",
            severity=Severity.INFO,
        )
    elif (
        question.estimated_difficulty is not question.requested_difficulty
        and question.generation_metadata.get("difficulty_explicit") is not False
    ):
        findings.add(
            "difficulty_mismatch",
            (
                f"requested difficulty is {question.requested_difficulty.value} but "
                f"observable features suggest {question.estimated_difficulty.value}"
            ),
            "cognitive.difficulty_alignment",
            detail={"confidence": question.difficulty_confidence},
        )


def validate_question(question: GeneratedQuestion, context: ValidationContext) -> ValidationResult:
    """Run every check and reduce the findings to one status."""

    findings = _Findings(question=question, context=context)
    _check_structure(findings)
    _check_alignment(findings)
    _check_answerability(findings)
    _check_mcq(findings)
    _check_clarity(findings)
    _check_cognitive(findings)

    errors = [item for item in findings.issues if item.severity is Severity.ERROR]
    warnings = [item for item in findings.issues if item.severity is Severity.WARNING]
    informational = [item for item in findings.issues if item.severity is Severity.INFO]
    if errors:
        status = GenerationValidationStatus.REJECTED
    elif warnings:
        status = GenerationValidationStatus.NEEDS_REVIEW
    elif informational:
        status = GenerationValidationStatus.VALIDATION_INCOMPLETE
    else:
        status = GenerationValidationStatus.VALIDATED

    penalty = 0.18 * len(errors) + 0.06 * len(warnings)
    confidence = round(max(0.0, min(1.0, 0.9 - penalty)), 4)
    return ValidationResult(
        status=status,
        issues=findings.issues,
        checks=findings.checks,
        confidence=confidence,
    )


def warnings_from_validation(result: ValidationResult) -> list[Uncertainty]:
    """Promote findings to record-level uncertainties for later consumers."""

    converted: list[Uncertainty] = []
    for issue in result.issues:
        severity = issue.severity
        if severity is Severity.ERROR:
            severity = Severity.WARNING
        converted.append(
            Uncertainty(
                code=f"generation_{issue.code}",
                message=issue.message,
                severity=severity,
                detail={"rule": issue.rule, **issue.detail},
            )
        )
    return converted


__all__ = [
    "ValidationContext",
    "validate_question",
    "warnings_from_validation",
]
