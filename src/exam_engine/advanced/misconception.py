"""Deterministic, evidence-bounded misconception mining.

The analyser reads stored student answers against the reference material that
already exists for the question and reports *candidate* patterns. Three rules
shape the design:

1. an incorrect answer is not automatically a misconception. Answers that are
   empty, too short to read, unreadable, unrelated to the question, or correct
   are discarded with a stated reason;
2. a pattern needs independent responses. Duplicate imports collapse to one
   response, and support is counted per distinct response id and per distinct
   question, so two copies of one answer never look like a class-wide pattern;
3. uncertainty is reported, not hidden. A single observation is labelled as one,
   a small cluster is a candidate, and only enough independent evidence across
   enough questions is called recurring. Nothing here measures a student.

Every record carries the excerpt it was inferred from, the rule that fired, the
method version and its limitations. Synthetic fixtures validate the pipeline;
they are not evidence about real learners.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from exam_engine.config import AdvancedSettings
from exam_engine.models.advanced import (
    EXCERPT_CHARS,
    MisconceptionDiscard,
    MisconceptionEvidence,
    MisconceptionRecord,
    MisconceptionReport,
    ResponseRecord,
    make_misconception_id,
)
from exam_engine.models.enums import (
    MisconceptionKind,
    ResponseCorrectness,
    Severity,
    SupportLevel,
)
from exam_engine.models.question import Uncertainty
from exam_engine.nlp.text import STOPWORDS

METHOD = "rule_based_response_analysis"
METHOD_VERSION = "phase11.v1"

#: Scaffold words that appear in answer keys and document titles but carry no
#: subject meaning; keeping them would dilute every coverage score.
_NOISE_TERMS = frozenset(
    {
        "reference",
        "source",
        "material",
        "passage",
        "lecture",
        "notes",
        "synthetic",
        "according",
        "above",
        "below",
        "course",
        "module",
        "unit",
        "chapter",
        "question",
        "answer",
        "extract",
        "document",
    }
)

_MAX_KEY_TERMS = 12
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_DEFINITION_RE = re.compile(
    r"\b(is|are|means|refers to|is defined as|is known as|is called)\b", re.IGNORECASE
)
_REASONING_RE = re.compile(
    r"\b(because|therefore|since|so that|hence|thus|which means|as a result|"
    r"this implies|given that)\b",
    re.IGNORECASE,
)
_ABSOLUTE_RE = re.compile(
    r"\b(always|never|every|all|any|none|cannot|impossible|regardless)\b", re.IGNORECASE
)
_CONDITION_RE = re.compile(
    r"\b(if|when|only|provided|unless|assuming|as long as|depends on)\b", re.IGNORECASE
)
#: Explicit step markers only. A bare "first" would also match the phrase
#: "first in, first out", which is a definition rather than a procedure.
_STEP_RE = re.compile(
    r"\b(step\s*\d|step\s+(one|two|three)|firstly|secondly|thirdly|next,"
    r"|then\b|finally|after that)\b",
    re.IGNORECASE,
)
_FORMULA_CUE_RE = re.compile(r"[=+\-*/^%]|\bformula\b|\bequation\b", re.IGNORECASE)

#: Kinds this analyser can infer from an answer, with the rule that decides it.
KIND_RULES: Mapping[MisconceptionKind, str] = {
    MisconceptionKind.INCORRECT_DEFINITION: "definition_frame_without_reference_terms",
    MisconceptionKind.CONCEPT_CONFUSION: "sibling_concept_dominates_answer",
    MisconceptionKind.FORMULA_MISAPPLICATION: "numeric_result_differs_from_reference",
    MisconceptionKind.MISSING_REASONING_STEP: "terms_without_justification",
    MisconceptionKind.INVALID_ASSUMPTION: "unconditional_claim_against_conditional_reference",
    MisconceptionKind.PROCEDURAL_ERROR: "step_sequence_omits_required_term",
    MisconceptionKind.TERMINOLOGY_MISUSE: "term_variant_instead_of_reference_term",
    MisconceptionKind.PREREQUISITE_GAP: "prerequisite_absent_from_answer_but_required",
    MisconceptionKind.PARTIAL_UNDERSTANDING: "partial_term_coverage",
}

#: Kinds deliberately not inferred from text alone, with the reason.
UNSUPPORTED_KINDS: Mapping[MisconceptionKind, str] = {
    MisconceptionKind.UNCLEAR_OR_UNRELATED: (
        "responses with no defensible reading are discarded with a reason, not recorded as "
        "a misconception"
    ),
}

_KIND_LABELS: Mapping[MisconceptionKind, str] = {
    MisconceptionKind.INCORRECT_DEFINITION: "Definition stated without the required terms",
    MisconceptionKind.CONCEPT_CONFUSION: "Confusion with a related concept",
    MisconceptionKind.FORMULA_MISAPPLICATION: "Formula applied to produce a different result",
    MisconceptionKind.MISSING_REASONING_STEP: "Reasoning given without justification",
    MisconceptionKind.INVALID_ASSUMPTION: "Rule applied without its stated condition",
    MisconceptionKind.PROCEDURAL_ERROR: "Procedure described with a required step missing",
    MisconceptionKind.TERMINOLOGY_MISUSE: "Term used in a different form than the material",
    MisconceptionKind.PREREQUISITE_GAP: "Required prerequisite not addressed",
    MisconceptionKind.PARTIAL_UNDERSTANDING: "Part of the required answer produced",
}

_KIND_WEIGHT: Mapping[MisconceptionKind, float] = {
    MisconceptionKind.CONCEPT_CONFUSION: 0.72,
    MisconceptionKind.INVALID_ASSUMPTION: 0.68,
    MisconceptionKind.FORMULA_MISAPPLICATION: 0.66,
    MisconceptionKind.INCORRECT_DEFINITION: 0.6,
    MisconceptionKind.PROCEDURAL_ERROR: 0.6,
    MisconceptionKind.PREREQUISITE_GAP: 0.56,
    MisconceptionKind.MISSING_REASONING_STEP: 0.52,
    MisconceptionKind.TERMINOLOGY_MISUSE: 0.5,
    MisconceptionKind.PARTIAL_UNDERSTANDING: 0.44,
}

_CORRECTIVE: Mapping[MisconceptionKind, str] = {
    MisconceptionKind.INCORRECT_DEFINITION: (
        "Re-state the definition using the terms the reference answer requires, then compare "
        "the two statements side by side."
    ),
    MisconceptionKind.CONCEPT_CONFUSION: (
        "Write the two concepts next to each other with one example each, and mark the feature "
        "that separates them."
    ),
    MisconceptionKind.FORMULA_MISAPPLICATION: (
        "Recompute the worked example step by step and check each substitution against the "
        "reference answer."
    ),
    MisconceptionKind.MISSING_REASONING_STEP: (
        "Add the missing 'because' or 'therefore' link, stating which fact justifies the step."
    ),
    MisconceptionKind.INVALID_ASSUMPTION: (
        "Name the condition the rule depends on and re-answer the question stating that "
        "condition explicitly."
    ),
    MisconceptionKind.PROCEDURAL_ERROR: (
        "List the required steps in order from the reference material, then redo the task "
        "against that list."
    ),
    MisconceptionKind.TERMINOLOGY_MISUSE: (
        "Check the exact term and its form in the course material, and rewrite the answer using "
        "that form."
    ),
    MisconceptionKind.PREREQUISITE_GAP: (
        "Revisit the prerequisite concept before re-attempting this question."
    ),
    MisconceptionKind.PARTIAL_UNDERSTANDING: (
        "Complete the missing part of the answer, then compare it with the reference answer."
    ),
}


@dataclass(frozen=True)
class QuestionContext:
    """Everything known about one question that its answers are read against."""

    question_id: str
    text: str
    reference_answer: str | None = None
    concept_ids: tuple[str, ...] = ()
    concept_labels: tuple[str, ...] = ()
    sibling_labels: tuple[str, ...] = ()
    prerequisite_labels: tuple[str, ...] = ()
    marks_available: float | None = None
    reference_source: str = "answer_key"

    def has_concept_mapping(self) -> bool:
        return bool(self.concept_ids or self.concept_labels)


@dataclass(frozen=True)
class _Signal:
    kind: MisconceptionKind
    marker: str
    weight: float
    detail: str


@dataclass
class _Bucket:
    kind: MisconceptionKind
    question: QuestionContext
    evidence: list[MisconceptionEvidence] = field(default_factory=list)
    markers: set[str] = field(default_factory=set)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _same_term(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 5 and longer.startswith(shorter)


def _key_terms(reference: str, limit: int = _MAX_KEY_TERMS) -> list[str]:
    terms: list[str] = []
    for token in _tokens(reference):
        if len(token) < 4 or token in STOPWORDS or token in _NOISE_TERMS or token.isdigit():
            continue
        if any(_same_term(token, existing) for existing in terms):
            continue
        terms.append(token)
        if len(terms) == limit:
            break
    return terms


def _coverage(answer_tokens: list[str], terms: Sequence[str]) -> float:
    if not terms:
        return 0.0
    hits = sum(1 for term in terms if any(_same_term(token, term) for token in answer_tokens))
    return hits / len(terms)


def _mentions(answer_tokens: list[str], label: str) -> int:
    label_tokens = [token for token in _tokens(label) if len(token) >= 4]
    if not label_tokens:
        return 0
    return sum(
        1 for token in answer_tokens if any(_same_term(token, item) for item in label_tokens)
    )


def _numbers(text: str) -> list[float]:
    values: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        try:
            values.append(float(match.group(0)))
        except ValueError:  # pragma: no cover - the pattern only matches numbers
            continue
    return values


def _is_variant(token: str, term: str) -> bool:
    """A term written in a different form: same stem, different ending."""

    if token == term or len(term) < 6 or len(token) < 6:
        return False
    stem = term[: max(5, len(term) - 3)]
    return token.startswith(stem) and token != term


def _detect(
    response: ResponseRecord,
    question: QuestionContext,
    settings: AdvancedSettings,
) -> tuple[list[_Signal], str | None, str | None]:
    """Return ``(signals, discard_reason, discard_detail)``."""

    answer = " ".join(response.answer_text.split())
    tokens = _tokens(answer)
    if not answer:
        return [], "empty_answer", "the stored answer is empty"
    if len(tokens) < settings.min_answer_words:
        return (
            [],
            "answer_too_short",
            f"the answer has {len(tokens)} word(s); at least {settings.min_answer_words} are "
            "needed to read an attempt",
        )
    if response.correctness is ResponseCorrectness.CORRECT:
        return [], "response_correct", "the answer is recorded as correct"
    reference = question.reference_answer
    if not reference:
        return (
            [],
            "missing_reference_answer",
            "no reference answer or rubric is stored for this question, so an incorrect "
            "reading cannot be established",
        )
    if not question.has_concept_mapping():
        return (
            [],
            "missing_concept_mapping",
            "the question is not mapped to a syllabus concept, so a misconception cannot be "
            "attributed to one",
        )

    terms = _key_terms(reference)
    coverage = _coverage(tokens, terms)
    wrong = response.correctness is ResponseCorrectness.INCORRECT
    partial = response.correctness is ResponseCorrectness.PARTIALLY_CORRECT

    if coverage < settings.min_reference_overlap and not wrong and not partial:
        return (
            [],
            "no_detectable_overlap",
            f"the answer shares under {settings.min_reference_overlap:.0%} of the reference "
            "terms and is not labelled incorrect, so it cannot be classified",
        )

    signals: list[_Signal] = []

    target_mentions = max(
        (_mentions(tokens, label) for label in question.concept_labels), default=0
    )
    sibling_hits: list[str] = []
    for label in question.sibling_labels:
        if label.casefold() in {item.casefold() for item in question.concept_labels}:
            continue
        if _mentions(tokens, label) > target_mentions:
            sibling_hits.append(label)
    if sibling_hits and target_mentions == 0 and (wrong or partial):
        signals.append(
            _Signal(
                MisconceptionKind.CONCEPT_CONFUSION,
                "sibling_concept_dominates_answer",
                0.8,
                f"the answer discusses {', '.join(sibling_hits[:2])} but never the concept the "
                "question asks about",
            )
        )

    if _DEFINITION_RE.search(answer) and wrong and coverage < 0.25:
        signals.append(
            _Signal(
                MisconceptionKind.INCORRECT_DEFINITION,
                "definition_frame_without_reference_terms",
                0.6,
                "a definition is offered, but none of the terms the reference answer requires "
                "appear in it",
            )
        )

    reference_numbers = _numbers(reference)
    if reference_numbers and _FORMULA_CUE_RE.search(reference):
        answer_numbers = _numbers(answer)
        if answer_numbers and not any(
            abs(item - expected) <= max(1e-9, abs(expected) * 1e-6)
            for item in answer_numbers
            for expected in reference_numbers
        ):
            signals.append(
                _Signal(
                    MisconceptionKind.FORMULA_MISAPPLICATION,
                    "numeric_result_differs_from_reference",
                    0.7,
                    f"the answer states {answer_numbers[0]:g} where the reference gives "
                    f"{reference_numbers[0]:g}",
                )
            )

    if partial and coverage > 0.0:
        # Partial credit plus any coverage of the reference terms is the strongest
        # reading available; it is still a candidate, never a verdict on the learner.
        signals.append(
            _Signal(
                MisconceptionKind.PARTIAL_UNDERSTANDING,
                "partial_term_coverage",
                0.45,
                f"the answer covers {coverage:.0%} of the reference terms and is marked "
                "partially correct",
            )
        )
    elif coverage > 0.0 and not _REASONING_RE.search(answer) and wrong:
        signals.append(
            _Signal(
                MisconceptionKind.MISSING_REASONING_STEP,
                "terms_without_justification",
                0.5,
                "the answer uses the required terms but gives no reason connecting them to the "
                "conclusion",
            )
        )

    if _ABSOLUTE_RE.search(answer) and _CONDITION_RE.search(reference) and wrong:
        signals.append(
            _Signal(
                MisconceptionKind.INVALID_ASSUMPTION,
                "unconditional_claim_against_conditional_reference",
                0.65,
                "the answer states an unconditional rule where the reference states a condition",
            )
        )

    if _STEP_RE.search(answer) and coverage < 0.5 and (wrong or partial):
        missing = [term for term in terms if not any(_same_term(token, term) for token in tokens)]
        if missing:
            signals.append(
                _Signal(
                    MisconceptionKind.PROCEDURAL_ERROR,
                    "step_sequence_omits_required_term",
                    0.55,
                    f"the answer walks through steps but omits the required term {missing[0]!r}",
                )
            )

    variants = [(token, term) for token in tokens for term in terms if _is_variant(token, term)]
    if variants and wrong:
        token, term = variants[0]
        signals.append(
            _Signal(
                MisconceptionKind.TERMINOLOGY_MISUSE,
                "term_variant_instead_of_reference_term",
                0.5,
                f"the answer writes {token!r} where the material uses {term!r}",
            )
        )

    prerequisites = [label for label in question.prerequisite_labels if label]
    if prerequisites and coverage == 0.0:
        required = [label for label in prerequisites if _mentions(_tokens(reference), label)]
        if required:
            signals.append(
                _Signal(
                    MisconceptionKind.PREREQUISITE_GAP,
                    "prerequisite_absent_from_answer_but_required",
                    0.5,
                    f"the reference answer relies on {required[0]} and the answer never "
                    "mentions it",
                )
            )

    if not signals:
        return (
            [],
            "no_supported_signal",
            "the answer does not match any rule this analyser can support; no misconception "
            "was inferred",
        )
    return signals, None, None


def _support_level(
    support: int, distinct_questions: int, settings: AdvancedSettings
) -> SupportLevel:
    if support <= 1:
        return SupportLevel.SINGLE_OBSERVATION
    if (
        support >= settings.min_support_for_recurring
        and distinct_questions >= settings.min_distinct_questions_for_recurring
    ):
        return SupportLevel.RECURRING
    return SupportLevel.CANDIDATE


def _confidence(kind: MisconceptionKind, support: int, distinct_questions: int) -> float:
    base = _KIND_WEIGHT.get(kind, 0.4)
    bonus = 0.06 * max(0, support - 1) + 0.06 * max(0, distinct_questions - 1)
    return round(min(0.95, base + bonus), 4)


def analyse(
    responses: Sequence[ResponseRecord],
    questions: Mapping[str, QuestionContext],
    *,
    settings: AdvancedSettings | None = None,
    course_id: str,
    syllabus_id: str,
    min_support: int | None = None,
    max_responses: int | None = None,
) -> MisconceptionReport:
    """Analyse responses for one course and syllabus; never mutates anything."""

    settings = settings or AdvancedSettings()
    limit = max_responses or settings.max_responses
    excerpt_chars = min(settings.excerpt_chars, EXCERPT_CHARS)
    warnings: list[Uncertainty] = []
    discarded: list[MisconceptionDiscard] = []

    unique: dict[str, ResponseRecord] = {}
    duplicates = 0
    for record in responses[:limit]:
        if record.fingerprint in unique:
            duplicates += 1
            continue
        unique[record.fingerprint] = record
    if len(responses) > limit:
        warnings.append(
            Uncertainty(
                code="response_limit_applied",
                message=(
                    f"{len(responses)} responses were supplied but only the first {limit} were "
                    "read; increase the configured limit to analyse all of them"
                ),
                severity=Severity.INFO,
                detail={"supplied": len(responses), "analysed": limit},
            )
        )

    identified = sum(1 for record in unique.values() if record.identified)
    if unique and not identified:
        warnings.append(
            Uncertainty(
                code="independent_responses_unverifiable",
                message=(
                    "no response carried a student or group reference, so independent-response "
                    "counts cannot be established: identical answers were collapsed by "
                    "fingerprint, but repeated submissions by one learner cannot be detected"
                ),
                severity=Severity.WARNING,
                detail={"analysed": len(unique), "identified": 0},
            )
        )

    buckets: dict[tuple[MisconceptionKind, tuple[str, ...]], _Bucket] = {}
    questions_seen: set[str] = set()
    groups: dict[tuple[MisconceptionKind, tuple[str, ...]], set[str]] = defaultdict(set)

    for record in sorted(unique.values(), key=lambda item: (item.question_id, item.response_id)):
        question = questions.get(record.question_id)
        if question is None:
            discarded.append(
                MisconceptionDiscard(
                    response_id=record.response_id,
                    question_id=record.question_id,
                    reason="unknown_question",
                    detail="the response refers to a question that is not available in this course",
                )
            )
            continue
        questions_seen.add(question.question_id)
        signals, reason, detail = _detect(record, question, settings)
        if reason is not None:
            discarded.append(
                MisconceptionDiscard(
                    response_id=record.response_id,
                    question_id=record.question_id,
                    reason=reason,
                    detail=detail or "",
                )
            )
            continue
        excerpt = record.excerpt(excerpt_chars)
        for signal in signals:
            scope = tuple(sorted(question.concept_ids or question.concept_labels))
            key = (signal.kind, scope)
            bucket = buckets.setdefault(key, _Bucket(kind=signal.kind, question=question))
            bucket.evidence.append(
                MisconceptionEvidence(
                    response_id=record.response_id,
                    question_id=record.question_id,
                    excerpt=excerpt,
                    marker=signal.marker,
                    method=f"{METHOD}:{METHOD_VERSION}",
                    weight=signal.weight,
                )
            )
            bucket.markers.add(signal.marker)
            if record.group_ref:
                groups[key].add(record.group_ref)

    threshold = settings.min_support_for_candidate if min_support is None else min_support
    records: list[MisconceptionRecord] = []
    for key, bucket in sorted(buckets.items(), key=lambda item: (item[0][0].value, item[0][1])):
        kind, scope = key
        support = len(bucket.evidence)
        distinct_questions = len({item.question_id for item in bucket.evidence})
        level = _support_level(support, distinct_questions, settings)
        if support < threshold:
            warnings.append(
                Uncertainty(
                    code="misconception_below_support_threshold",
                    message=(
                        f"{support} response(s) support the {kind.value} pattern; the configured "
                        f"minimum is {threshold}, so it is reported but must not be treated as a "
                        "recurring pattern"
                    ),
                    severity=Severity.INFO,
                    detail={"misconception_kind": kind.value, "support": support},
                )
            )
        context = bucket.question
        evidence = bucket.evidence[: settings.max_evidence_per_record]
        entry = MisconceptionRecord(
            misconception_id=make_misconception_id(course_id, syllabus_id, kind, list(scope)),
            course_id=course_id,
            syllabus_id=syllabus_id,
            kind=kind,
            label=_KIND_LABELS.get(kind, kind.value),
            description=_describe(kind, bucket.markers, support, distinct_questions),
            concept_ids=list(context.concept_ids),
            concept_labels=list(context.concept_labels),
            question_ids=sorted({item.question_id for item in bucket.evidence}),
            evidence=evidence,
            support_count=support,
            distinct_questions=distinct_questions,
            distinct_groups=len(groups.get(key, ())),
            detection_method=f"{METHOD}:{METHOD_VERSION}",
            support_level=level,
            confidence=_confidence(kind, support, distinct_questions),
            suggested_correction=_CORRECTIVE.get(kind),
            follow_up_questions=[
                f"Re-attempt question {context.question_id} and compare your answer with "
                "reference answer.",
            ],
            limitations=_limitations(level, distinct_questions, context),
        )
        records.append(entry)

    if not records:
        warnings.append(
            Uncertainty(
                code="no_supported_misconceptions",
                message=(
                    "no response matched a supported rule; this is not evidence that no "
                    "misconceptions exist, only that this analyser found none"
                ),
                severity=Severity.INFO,
            )
        )

    report = MisconceptionReport(
        course_id=course_id,
        syllabus_id=syllabus_id,
        responses_supplied=len(responses),
        responses_stored=len(unique),
        distinct_responses=len(unique),
        duplicate_responses=duplicates,
        questions_analysed=len(questions_seen),
        records=records,
        discarded=discarded,
        parameters={
            "method": METHOD,
            "method_version": METHOD_VERSION,
            "min_support": threshold,
            "min_support_for_recurring": settings.min_support_for_recurring,
            "min_distinct_questions_for_recurring": (settings.min_distinct_questions_for_recurring),
            "min_answer_words": settings.min_answer_words,
            "min_reference_overlap": settings.min_reference_overlap,
            "excerpt_chars": excerpt_chars,
            "max_evidence_per_record": settings.max_evidence_per_record,
            "max_responses": limit,
            "identified_responses": identified,
        },
        warnings=warnings,
        limitations=_report_limitations(len(unique), len(questions_seen), identified),
    )
    return report


def _describe(
    kind: MisconceptionKind, markers: set[str], support: int, distinct_questions: int
) -> str:
    return (
        f"{_KIND_LABELS.get(kind, kind.value)}: {support} independent response(s) across "
        f"{distinct_questions} question(s) matched the rule "
        f"{', '.join(sorted(markers))}. This is a candidate reading of the answers, not a "
        "diagnosis of any learner."
    )


def _limitations(
    level: SupportLevel, distinct_questions: int, question: QuestionContext
) -> list[str]:
    limitations = [
        "Detection is rule-based over answer wording; an answer can match a rule without "
        "sharing the misunderstanding the rule describes.",
        "Only answers to questions with a stored reference answer and a concept mapping were read.",
    ]
    if level is SupportLevel.SINGLE_OBSERVATION:
        limitations.append(
            "A single response cannot establish a pattern; this record needs more independent "
            "evidence before it is treated as recurring."
        )
    if distinct_questions < 2:
        limitations.append(
            "All supporting responses answer the same question, so the finding may reflect that "
            "question's wording rather than the concept."
        )
    if question.reference_source == "rubric":
        limitations.append(
            "The reference is a grading rubric rather than a model answer, which limits how "
            "precisely an incorrect reading can be established."
        )
    return limitations


def _report_limitations(responses: int, questions: int, identified: int) -> list[str]:
    limitations = [
        "These records describe answer patterns in the imported data only; no population-level "
        "claim about student ability is made or implied.",
        "Duplicate imports were collapsed by answer fingerprint before counting.",
        "Synthetic fixtures validate the pipeline; they are not evidence about real learners.",
    ]
    if responses and not identified:
        limitations.append(
            "No response carried a student or group reference, so the reported support counts "
            "are answer patterns rather than verified independent responses."
        )
    if responses and responses < 10:
        limitations.append(
            f"The sample holds {responses} response(s) across {questions} question(s), which is "
            "too small for any general claim."
        )
    return limitations


__all__ = [
    "KIND_RULES",
    "METHOD",
    "METHOD_VERSION",
    "UNSUPPORTED_KINDS",
    "QuestionContext",
    "analyse",
]
