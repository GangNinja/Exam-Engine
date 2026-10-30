"""Deterministic adversarial question generation.

An adversarial question is not a trick question. Every strategy here states the
whole task, quotes the course passage it depends on, and carries a rubric: the
learner is asked to reason about material they have been given, not to guess what
the author had in mind.

Two rules the templates follow:

* the question text never contains the answer, only the material to reason from;
* where an incorrect claim or an incomplete answer is shown, it is *constructed*
  from the real passage and labelled as a claim to evaluate - the course text is
  never presented as a wrong answer.

A question that needs evidence which does not exist is refused with a stable code
instead of being emitted with invented context.
"""

from __future__ import annotations

from dataclasses import dataclass

from exam_engine.advanced.source import AdvancedError, EvidenceContext
from exam_engine.models.advanced import AdversarialCandidate
from exam_engine.models.enums import (
    BloomLevel,
    ChallengeStrategy,
    DifficultyLabel,
    GenerationFormat,
)

NEED_EVIDENCE = "evidence"
NEED_SIBLING = "sibling_concept"
NEED_PREREQUISITE = "in_syllabus_prerequisite"

_OPEN = GenerationFormat.EXPLANATION


@dataclass(frozen=True)
class ChallengeSpec:
    strategy: ChallengeStrategy
    label: str
    description: str
    reasoning: str
    default_bloom: BloomLevel = BloomLevel.ANALYZE
    format: GenerationFormat = _OPEN
    requires: frozenset[str] = frozenset({NEED_EVIDENCE})
    evaluates_claim: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "label": self.label,
            "description": self.description,
            "reasoning_tested": self.reasoning,
            "format": self.format.value,
            "default_bloom": self.default_bloom.value,
            "requires": sorted(self.requires),
            "evaluates_claim": self.evaluates_claim,
        }


_SPECS: tuple[ChallengeSpec, ...] = (
    ChallengeSpec(
        strategy=ChallengeStrategy.UNFAMILIAR_APPLICATION,
        label="Unfamiliar application",
        description=(
            "Apply the concept to a situation the course material describes but does not "
            "work through."
        ),
        reasoning=(
            "Transfer of a definition to an unseen case rather than recall of the definition."
        ),
        default_bloom=BloomLevel.APPLY,
        format=GenerationFormat.APPLICATION,
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.TRANSFER,
        label="Transfer to a related context",
        description="Carry the concept across to a related idea and say what survives the move.",
        reasoning=(
            "Recognising which part of an explanation is general and which is local to one example."
        ),
        default_bloom=BloomLevel.APPLY,
        requires=frozenset({NEED_EVIDENCE, NEED_SIBLING}),
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.MULTI_STEP,
        label="Multi-step reasoning",
        description="Chain state, operation and check so a single recalled sentence is not enough.",
        reasoning="Sequencing the parts of a procedure and checking the result.",
        default_bloom=BloomLevel.APPLY,
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.FALSE_ASSUMPTION,
        label="Find the false assumption",
        description="Expose the assumption under a universal claim and say where it breaks.",
        reasoning="Separating a condition of use from the statement of the concept.",
        evaluates_claim=True,
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.ERROR_DIAGNOSIS,
        label="Diagnose an incomplete answer",
        description="Identify the required step an answer omits and repair it.",
        reasoning="Comparing a partial answer against the material's requirements.",
        evaluates_claim=True,
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.COMPETING_APPROACH,
        label="Compare competing approaches",
        description=(
            "Choose between two ways of handling the concept and state the deciding criterion."
        ),
        reasoning=(
            "Judging alternatives against an explicit criterion instead of preferring a "
            "familiar one."
        ),
        default_bloom=BloomLevel.EVALUATE,
        format=GenerationFormat.EVALUATION,
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.BOUNDARY,
        label="Boundary condition",
        description="Find where the concept stops applying and explain the change.",
        reasoning="Knowing the limits of a rule, not only its statement.",
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.COUNTEREXAMPLE,
        label="Construct a counterexample",
        description="Test a universal claim by building a case against it, or defend the claim.",
        reasoning="Deciding whether a claim is genuinely universal.",
        default_bloom=BloomLevel.EVALUATE,
        evaluates_claim=True,
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.SOLUTION_EVALUATION,
        label="Evaluate a proposed solution",
        description="Judge a solution against the course's own criteria.",
        reasoning="Assessing quality against criteria rather than producing a matching answer.",
        default_bloom=BloomLevel.EVALUATE,
        format=GenerationFormat.EVALUATION,
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.MISSING_PREREQUISITE,
        label="Missing prerequisite",
        description="Show what fails when a prerequisite is skipped.",
        reasoning="Understanding dependency between ideas.",
        requires=frozenset({NEED_EVIDENCE, NEED_PREREQUISITE}),
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.EDGE_CASE,
        label="Edge case",
        description="Reason about the smallest, emptiest or extreme case.",
        reasoning="Testing whether a rule is understood at its limits.",
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.DISTINGUISH_CONCEPTS,
        label="Distinguish concepts",
        description="Separate two closely related concepts with a case that only fits one.",
        reasoning="Discriminating between neighbouring ideas.",
        requires=frozenset({NEED_EVIDENCE, NEED_SIBLING}),
    ),
    ChallengeSpec(
        strategy=ChallengeStrategy.WHY_WRONG,
        label="Why a tempting answer fails",
        description="Explain why a plausible shortcut does not satisfy the material's requirement.",
        reasoning="Understanding the reason behind a procedure, not just its steps.",
        evaluates_claim=True,
    ),
)

SPECS: dict[ChallengeStrategy, ChallengeSpec] = {spec.strategy: spec for spec in _SPECS}

DEFAULT_CHALLENGE_ORDER: tuple[ChallengeStrategy, ...] = (
    ChallengeStrategy.UNFAMILIAR_APPLICATION,
    ChallengeStrategy.MULTI_STEP,
    ChallengeStrategy.ERROR_DIAGNOSIS,
    ChallengeStrategy.EDGE_CASE,
    ChallengeStrategy.DISTINGUISH_CONCEPTS,
    ChallengeStrategy.BOUNDARY,
    ChallengeStrategy.WHY_WRONG,
    ChallengeStrategy.COUNTEREXAMPLE,
)


@dataclass(frozen=True)
class ChallengeTarget:
    """What the challenge is about: one syllabus concept inside one unit."""

    concept_id: str
    concept_label: str
    unit_label: str
    objective: str
    concept_ids: tuple[str, ...] = ()
    unit_id: str = ""


def spec_for(strategy: ChallengeStrategy) -> ChallengeSpec:
    return SPECS[strategy]


def supported_strategies() -> list[ChallengeSpec]:
    return list(_SPECS)


def _quote(text: str) -> str:
    compact = " ".join(text.split())
    return f'"{compact}"'


def _require(context: EvidenceContext, needs: frozenset[str]) -> None:
    if NEED_EVIDENCE in needs and not context.has_evidence():
        raise AdvancedError(
            "insufficient_evidence",
            "no course passage mentions this concept, so a grounded challenge cannot be written",
        )
    if NEED_SIBLING in needs and not context.sibling_labels:
        raise AdvancedError(
            "no_related_concept",
            "this concept has no sibling concept in its unit to reason against",
        )
    if NEED_PREREQUISITE in needs and not (
        context.prerequisite_label and context.prerequisite_in_syllabus
    ):
        raise AdvancedError(
            "no_prerequisite_in_scope",
            (
                "no in-syllabus prerequisite of this concept is known, so a missing-"
                "prerequisite challenge would fall outside the declared scope"
            ),
        )


def _text(
    spec: ChallengeSpec,
    target: ChallengeTarget,
    context: EvidenceContext,
    index: int,
    seed: int,
) -> str:
    concept = target.concept_label
    unit = target.unit_label
    passage = context.first_passage()
    excerpt = _quote(passage.excerpt) if passage is not None else ""
    source = passage.document_title if passage is not None else unit
    sibling = (
        context.sibling_labels[(index + seed) % len(context.sibling_labels)]
        if context.sibling_labels
        else None
    )
    prerequisite = context.prerequisite_label

    strategy = spec.strategy
    if strategy is ChallengeStrategy.UNFAMILIAR_APPLICATION:
        settings = (
            f"A team working through {source} meets this situation: {excerpt}",
            f"During a {unit} practical, this case comes up: {excerpt}",
            f"A colleague quotes this from {source}: {excerpt}",
        )
        return (
            f"{settings[(index + seed) % len(settings)]}\n\n"
            f"Using {concept} as the course defines it, decide what should be done and "
            "explain why. State the one assumption your decision depends on."
        )

    if strategy is ChallengeStrategy.TRANSFER:
        return (
            f"{source} explains {concept} as follows: {excerpt}\n\n"
            f"The same idea also appears in {sibling}. Explain what carries over unchanged "
            f"from {concept} to {sibling} and what has to be adapted, using one detail from "
            "the passage as evidence."
        )

    if strategy is ChallengeStrategy.MULTI_STEP:
        return (
            f"Consider this extract from {source}: {excerpt}\n\n"
            f"(a) State what {concept} requires in this case.\n"
            "(b) Carry out that step.\n"
            "(c) Describe a check that would show your result is consistent with the passage."
        )

    if strategy is ChallengeStrategy.FALSE_ASSUMPTION:
        return (
            f"A student claims: {excerpt} means that {concept} always holds, in every case, "
            "with no further condition.\n\n"
            "Identify the assumption this claim makes, state the condition under which the "
            "claim fails, and explain what the passage actually supports."
        )

    if strategy is ChallengeStrategy.ERROR_DIAGNOSIS:
        return (
            f"{source} requires the following for {concept}: {excerpt}\n\n"
            'An answer to a question on this topic says only: "It follows the rule described '
            'in the notes."\n\n'
            "Name the specific step this answer omits, explain what goes wrong without it, and "
            "give the complete answer the passage requires."
        )

    if strategy is ChallengeStrategy.COMPETING_APPROACH:
        return (
            f"{source} describes one way of handling {concept}: {excerpt}\n\n"
            "Compare that approach with one alternative you know. State the criterion that "
            "decides which approach to use here, choose one, and justify the choice with "
            "evidence from the passage."
        )

    if strategy is ChallengeStrategy.BOUNDARY:
        return (
            f"According to {source}, {concept} behaves as follows: {excerpt}\n\n"
            f"Describe the boundary case in which {concept} stops applying. Explain what "
            "changes at that boundary and why the usual answer no longer holds."
        )

    if strategy is ChallengeStrategy.COUNTEREXAMPLE:
        return (
            f"A claim is made about {concept} on the basis of this extract: {excerpt}\n\n"
            "Either construct a specific case in which the claim fails, or explain why no such "
            "case exists. Justify your answer against the passage."
        )

    if strategy is ChallengeStrategy.SOLUTION_EVALUATION:
        return (
            f"A proposed solution to a {concept} problem is: {excerpt}\n\n"
            "Evaluate it against the criteria the course applies to this topic. Name one "
            "strength, one weakness, and state whether you would accept it, with reasons."
        )

    if strategy is ChallengeStrategy.MISSING_PREREQUISITE:
        return (
            f"{source} covers {concept} as follows: {excerpt}\n\n"
            f"A learner attempts this after skipping {prerequisite}. Identify what breaks, name "
            f"the step from {prerequisite} that must come first, and explain the consequence of "
            "leaving it out."
        )

    if strategy is ChallengeStrategy.EDGE_CASE:
        return (
            f"{source} states: {excerpt}\n\n"
            f"Consider the smallest or emptiest case in which {concept} applies. What does the "
            "passage imply there, and does the usual answer still hold? Explain the difference."
        )

    if strategy is ChallengeStrategy.DISTINGUISH_CONCEPTS:
        return (
            f"{source} describes {concept} as: {excerpt}\n\n"
            f"Distinguish it from {sibling}: give one case in which only one of the two applies, "
            "and state the difference in a single sentence."
        )

    # WHY_WRONG
    return (
        f"{source} describes the requirement for {concept}: {excerpt}\n\n"
        "A common shortcut is to answer from memory without checking the requirement. Explain "
        "why that shortcut fails here, and state what a complete answer must contain."
    )


def _rubric(target: ChallengeTarget, spec: ChallengeSpec) -> list[str]:
    concept = target.concept_label
    base = [
        f"States what {concept} requires, consistent with the cited source passage.",
        "Applies that requirement to the specific case in the question rather than "
        "restating a definition.",
        "Justifies the answer with evidence from the passage or an explicitly stated assumption.",
    ]
    if spec.evaluates_claim:
        base.append("Names the assumption or omitted step explicitly and says why it matters.")
    if spec.strategy is ChallengeStrategy.COMPETING_APPROACH:
        base.append("States the criterion used to compare the approaches before choosing.")
    if spec.strategy is ChallengeStrategy.DISTINGUISH_CONCEPTS:
        base.append("Gives a case that separates the two concepts rather than describing both.")
    return base


def challenge(
    target: ChallengeTarget,
    strategy: ChallengeStrategy,
    *,
    index: int,
    seed: int,
    context: EvidenceContext,
    difficulty: DifficultyLabel = DifficultyLabel.HARD,
    bloom: BloomLevel | None = None,
) -> AdversarialCandidate:
    """Build one fair challenge, or raise :class:`AdvancedError` with a stable code."""

    spec = SPECS[strategy]
    _require(context, spec.requires)
    passage = context.first_passage()
    references = [passage] if passage is not None else []
    text = _text(spec, target, context, index, seed)
    rubric = _rubric(target, spec)
    limitations = [
        "The rubric states the reasoning a complete answer must contain; no automatic model "
        "answer was produced, so marking remains a human judgement.",
    ]
    if passage is None:
        limitations.append(
            "No document passage was available; the question relies on the syllabus concept "
            "statement alone."
        )
    return AdversarialCandidate(
        index=index,
        strategy=strategy,
        text=text,
        answer_key=None,
        explanation=(
            f"Reference material ({passage.document_title}): {passage.excerpt}"
            if passage is not None
            else None
        ),
        rubric=rubric,
        bloom=bloom or spec.default_bloom,
        difficulty=difficulty,
        format=spec.format,
        objective=target.objective,
        rationale=spec.reasoning,
        concept_ids=list(target.concept_ids or (target.concept_id,)),
        concept_labels=[target.concept_label],
        source_references=references,
        limitations=limitations,
    )


__all__ = [
    "DEFAULT_CHALLENGE_ORDER",
    "NEED_EVIDENCE",
    "NEED_PREREQUISITE",
    "NEED_SIBLING",
    "SPECS",
    "ChallengeSpec",
    "ChallengeTarget",
    "challenge",
    "spec_for",
    "supported_strategies",
]
