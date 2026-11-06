"""Deterministic question mutation.

A mutation is a *derived* question, never a new canonical one. Each strategy
declares which properties it can keep invariant, so the service can refuse a
request that asks for something the strategy cannot promise:

``learning_objective``
    The same intended learning outcome.
``concepts``
    The same assessed syllabus concept(s).
``answer_key``
    The stored answer key remains a valid answer to the variant.
``task``
    The same work is asked for.
``single_correct_answer``
    Exactly one option stays defensible.

Where a strategy cannot promise a property, the candidate records what changed and
the service rejects it if the request insisted on that property. Semantic
equivalence is never asserted from an embedding score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from exam_engine.advanced.source import (
    AdvancedError,
    EvidenceContext,
    SourceOption,
    SourceQuestion,
)
from exam_engine.generation.templates import OBJECTIVE_VERBS
from exam_engine.models.advanced import MutationCandidate
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    GenerationFormat,
    MutationStrategy,
)

INVARIANT_OBJECTIVE = "learning_objective"
INVARIANT_CONCEPT = "concepts"
INVARIANT_ANSWER = "answer_key"
INVARIANT_TASK = "task"
INVARIANT_SINGLE_CORRECT = "single_correct_answer"

ALL_INVARIANTS = frozenset(
    {
        INVARIANT_OBJECTIVE,
        INVARIANT_CONCEPT,
        INVARIANT_ANSWER,
        INVARIANT_TASK,
        INVARIANT_SINGLE_CORRECT,
    }
)

#: Extra input a strategy needs before it can build anything.
NEED_EVIDENCE = "evidence"
NEED_NUMBERS = "numbers"
NEED_RELATED = "related_concept"
NEED_MCQ_OPTIONS = "mcq_options"
NEED_PARTS = "reorderable_parts"

_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
_MARKER_RE = re.compile(r"(?<![\w])(?:\(([a-d])\)|([1-9])[.)])\s+", re.IGNORECASE)
_LEADING_VERB_RE = re.compile(
    r"^(explain|describe|define|analyse|analyze|apply|evaluate|design|compare|discuss|state|give)\b",
    re.IGNORECASE,
)
_MAX_TEXT = 2000


@dataclass(frozen=True)
class MutationSpec:
    strategy: MutationStrategy
    label: str
    description: str
    invariants: frozenset[str]
    formats: frozenset[GenerationFormat] | None = None
    requires: frozenset[str] = frozenset()
    changes_answer: bool = False
    default_bloom: BloomLevel | None = None

    def supports(self, fmt: GenerationFormat) -> bool:
        return self.formats is None or fmt in self.formats

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "label": self.label,
            "description": self.description,
            "invariants": sorted(self.invariants),
            "formats": sorted(item.value for item in self.formats) if self.formats else None,
            "requires": sorted(self.requires),
            "changes_answer": self.changes_answer,
        }


_MCQ = frozenset({GenerationFormat.MULTIPLE_CHOICE})
_LONG_FORMATS = frozenset(
    {
        GenerationFormat.LONG_ANSWER,
        GenerationFormat.EXPLANATION,
        GenerationFormat.SHORT_ANSWER,
        GenerationFormat.ANALYSIS,
        GenerationFormat.APPLICATION,
        GenerationFormat.CASE_BASED,
        GenerationFormat.DESIGN,
        GenerationFormat.EVALUATION,
    }
)

_SPECS: tuple[MutationSpec, ...] = (
    MutationSpec(
        strategy=MutationStrategy.REPHRASE,
        label="Rephrase",
        description="Ask the same task in different words; the expected answer is unchanged.",
        invariants=frozenset(
            {
                INVARIANT_OBJECTIVE,
                INVARIANT_CONCEPT,
                INVARIANT_ANSWER,
                INVARIANT_TASK,
                INVARIANT_SINGLE_CORRECT,
            }
        ),
    ),
    MutationSpec(
        strategy=MutationStrategy.SCENARIO,
        label="Change the scenario",
        description="Move the same task into a concrete study setting.",
        invariants=frozenset(
            {
                INVARIANT_OBJECTIVE,
                INVARIANT_CONCEPT,
                INVARIANT_ANSWER,
                INVARIANT_TASK,
                INVARIANT_SINGLE_CORRECT,
            }
        ),
    ),
    MutationSpec(
        strategy=MutationStrategy.REORDER,
        label="Reorder the parts",
        description="Reorder enumerated sub-tasks without adding or dropping one.",
        invariants=frozenset(
            {INVARIANT_OBJECTIVE, INVARIANT_CONCEPT, INVARIANT_ANSWER, INVARIANT_TASK}
        ),
        requires=frozenset({NEED_PARTS}),
        changes_answer=True,
    ),
    MutationSpec(
        strategy=MutationStrategy.APPLICATION,
        label="New application context",
        description="Apply the same concept to a documented situation from the course material.",
        invariants=frozenset({INVARIANT_OBJECTIVE, INVARIANT_CONCEPT}),
        formats=_LONG_FORMATS,
        requires=frozenset({NEED_EVIDENCE}),
        changes_answer=True,
        default_bloom=BloomLevel.APPLY,
    ),
    MutationSpec(
        strategy=MutationStrategy.NUMERICAL,
        label="Change numerical parameters",
        description=(
            "Substitute the numeric parameters of a numerical problem. The answer must "
            "be recomputed, so no answer key is carried over."
        ),
        invariants=frozenset({INVARIANT_OBJECTIVE, INVARIANT_CONCEPT}),
        formats=frozenset({GenerationFormat.NUMERICAL}),
        requires=frozenset({NEED_NUMBERS}),
        changes_answer=True,
        default_bloom=BloomLevel.APPLY,
    ),
    MutationSpec(
        strategy=MutationStrategy.DIFFICULTY,
        label="Adjust difficulty",
        description="Raise or lower the reasoning demand without changing the assessed concept.",
        invariants=frozenset({INVARIANT_OBJECTIVE, INVARIANT_CONCEPT}),
        changes_answer=True,
    ),
    MutationSpec(
        strategy=MutationStrategy.COGNITIVE,
        label="Change the cognitive operation",
        description="Ask for a different Bloom-level operation on the same concept.",
        invariants=frozenset({INVARIANT_CONCEPT}),
        formats=_LONG_FORMATS,
        changes_answer=True,
    ),
    MutationSpec(
        strategy=MutationStrategy.RELATED_CONCEPT,
        label="Use a related concept",
        description=(
            "Ask about a related or prerequired concept instead. The assessed concept and "
            "learning objective change by design."
        ),
        invariants=frozenset(),
        requires=frozenset({NEED_RELATED}),
        changes_answer=True,
    ),
    MutationSpec(
        strategy=MutationStrategy.DISTRACTOR,
        label="Rewrite the distractors",
        description="Rebuild multiple-choice options while keeping exactly one correct answer.",
        invariants=frozenset(
            {INVARIANT_OBJECTIVE, INVARIANT_CONCEPT, INVARIANT_ANSWER, INVARIANT_SINGLE_CORRECT}
        ),
        formats=_MCQ,
        requires=frozenset({NEED_MCQ_OPTIONS, NEED_RELATED}),
    ),
)

SPECS: dict[MutationStrategy, MutationSpec] = {spec.strategy: spec for spec in _SPECS}


def spec_for(strategy: MutationStrategy) -> MutationSpec:
    return SPECS[strategy]


def supported_strategies(fmt: GenerationFormat | None = None) -> list[MutationSpec]:
    specs = list(_SPECS)
    if fmt is not None:
        specs = [spec for spec in specs if spec.supports(fmt)]
    return specs


def _clean(text: str) -> str:
    return " ".join(text.split())


def _lower_first(text: str) -> str:
    if not text:
        return text
    first_word = text.split()[0]
    if first_word.isupper() or len(first_word) <= 2:
        return text
    return text[0].lower() + text[1:]


def _terminal(text: str) -> str:
    text = text.rstrip()
    if not text:
        return text
    if text[-1] in ".?!":
        return text
    return text + "."


def _bounded(text: str) -> str:
    text = _clean(text)
    if len(text) > _MAX_TEXT:
        raise AdvancedError(
            "mutation_too_long",
            "the mutated text exceeds the supported length",
            detail={"limit": _MAX_TEXT},
        )
    return text


def _enumerated_parts(text: str) -> tuple[str, list[str]] | None:
    """Split enumerated sub-tasks into ``(instruction, parts)``, or ``None``."""

    markers = list(_MARKER_RE.finditer(text))
    if len(markers) >= 2:
        instruction = text[: markers[0].start()].strip()
        parts: list[str] = []
        for position, marker in enumerate(markers):
            end = markers[position + 1].start() if position + 1 < len(markers) else len(text)
            body = text[marker.end() : end].strip(" ;\n")
            if len(body.split()) < 2:
                return None
            parts.append(body)
        return instruction, parts

    segments = [segment.strip(" .;") for segment in text.split(";")]
    if len(segments) >= 2 and all(len(segment.split()) >= 3 for segment in segments):
        return "", segments
    return None


def _relabel(instruction: str, parts: list[str]) -> str:
    if len(parts) == 1:
        return _clean(f"{instruction} {parts[0]}".strip())
    letters = "abcdefgh"
    labelled = [f"({letters[index]}) {part}" for index, part in enumerate(parts)]
    prefix = f"{instruction} " if instruction else ""
    return _clean(prefix + " ".join(labelled))


def _replace_numbers(text: str, index: int, seed: int) -> tuple[str, list[str]]:
    matches = list(_NUMBER_RE.finditer(text))
    usable = [match for match in matches if not re.fullmatch(r"(19|20)\d{2}", match.group(1))]
    if not usable:
        raise AdvancedError(
            "no_numeric_parameters",
            "the question states no numeric parameter that can be varied",
        )
    target = usable[index % len(usable)]
    original = float(target.group(1))
    step = 1 + (seed + index) % 3
    replacement = int(original + step) if original.is_integer() else round(original + step, 2)
    if replacement <= 0:
        replacement = original + step
    changed = text[: target.start()] + str(replacement) + text[target.end() :]
    return changed, [target.group(1), str(replacement)]


def _difficulty_suffix(difficulty: DifficultyLabel) -> str:
    if difficulty is DifficultyLabel.EASY:
        return " Give a short, direct answer and define any term you use."
    if difficulty is DifficultyLabel.HARD:
        return " Justify each step, state one assumption, and give one limitation."
    return " Answer in a short paragraph with one supporting reason."


def _cognitive_text(source: SourceQuestion, bloom: BloomLevel) -> str:
    concept = source.concept_label
    if bloom is BloomLevel.REMEMBER:
        return f"State the key facts about {concept} that the course material establishes."
    if bloom is BloomLevel.UNDERSTAND:
        return f"Explain {concept} in your own words and give one example from the material."
    if bloom is BloomLevel.APPLY:
        return f"Apply {concept} to a new case and show each step of your reasoning."
    if bloom is BloomLevel.ANALYZE:
        return f"Analyse {concept}: separate its parts and explain how they relate."
    if bloom is BloomLevel.EVALUATE:
        return f"Evaluate {concept} against explicit criteria and justify your judgement."
    return f"Design an approach that uses {concept}, and justify the choices you make."


def _rephrase_text(source: SourceQuestion, index: int) -> str:
    text = source.text
    variants: tuple[str, ...]
    if source.is_multiple_choice:
        variants = (
            f"From the course material, {_lower_first(text)}",
            f"Choose the best answer. {text}",
            f"Answer without referring to your notes: {text}",
        )
    else:
        variants = (
            f"In your own words, {_lower_first(text)}",
            f"Answer precisely and show your reasoning. {text}",
            f"A tutor asks: {text}",
            f"Restate the task in your own words, then answer it. {text}",
        )
        if "in your own words" in text.casefold():
            # A rephrase must not repeat a phrase the source question already uses;
            # every remaining variant embeds the text without that framing.
            variants = (
                f"Answer precisely and show your reasoning. {text}",
                f"A tutor asks: {text}",
            )
    return variants[index % len(variants)]


def _scenario_text(source: SourceQuestion, index: int) -> str:
    settings = (
        f"A classmate preparing for the {source.unit_label} tutorial asks you this:",
        f"During a {source.unit_label} revision session, the group is asked:",
        f"In a {source.unit_label} study group, one student poses this question:",
    )
    return f"{settings[index % len(settings)]}\n\n{source.text}"


def _application_text(source: SourceQuestion, context: EvidenceContext) -> str:
    passage = context.first_passage()
    if passage is None:
        raise AdvancedError(
            "insufficient_mutation_context",
            "application mutation needs a grounding passage from the course material",
        )
    return (
        f"Apply {source.concept_label} to this situation from {passage.document_title}: "
        f'"{passage.excerpt}"\n\n{source.text}'
    )


def _related_text(source: SourceQuestion, context: EvidenceContext, index: int) -> tuple[str, str]:
    labels = [
        label
        for label in context.sibling_labels
        if label.casefold() != source.concept_label.casefold()
    ]
    if not labels:
        raise AdvancedError(
            "no_related_concept",
            "this question has no related syllabus concept to mutate towards",
        )
    sibling = labels[index % len(labels)]
    text = source.text
    pattern = re.compile(re.escape(source.concept_label), re.IGNORECASE)
    if pattern.search(text):
        text = pattern.sub(sibling, text)
    else:
        text = (
            f"Explain {sibling} in {source.unit_label}, and state one difference from "
            f"{source.concept_label}."
        )
    return text, sibling


def _distractor_options(
    source: SourceQuestion, context: EvidenceContext, index: int, seed: int
) -> tuple[list[SourceOption], str]:
    correct = next((item for item in source.options if item.is_correct), None)
    if correct is None:
        raise AdvancedError(
            "invalid_multiple_choice_source",
            "the source multiple-choice question has no correct option",
        )
    pool = [
        label for label in context.related_labels if label.casefold() != correct.text.casefold()
    ]
    if len(pool) < 2:
        raise AdvancedError(
            "insufficient_distractors",
            "rewriting distractors needs at least two other syllabus concepts in the unit",
            detail={"available": len(pool)},
        )
    chosen: list[str] = []
    for offset in range(len(pool)):
        candidate = pool[(seed + index + offset) % len(pool)]
        if candidate.casefold() not in {item.casefold() for item in chosen}:
            chosen.append(candidate)
        if len(chosen) == 3:
            break
    texts = [correct.text, *chosen]
    rotation = (seed + index) % len(texts)
    texts = texts[rotation:] + texts[:rotation]
    options = [
        SourceOption(key="ABCDEF"[position], text=label, is_correct=label == correct.text)
        for position, label in enumerate(texts)
    ]
    key = next(item.key for item in options if item.is_correct)
    return options, key


def has_numeric_parameters(text: str) -> bool:
    """True when the question states a number that a mutation could substitute."""

    return bool(
        [
            match
            for match in _NUMBER_RE.finditer(text)
            if not re.fullmatch(r"(19|20)\d{2}", match.group(1))
        ]
    )


def has_reorderable_parts(text: str) -> bool:
    """True when the question has enumerated sub-tasks that could be reordered."""

    return _enumerated_parts(text) is not None


def mutate(
    source: SourceQuestion,
    strategy: MutationStrategy,
    *,
    index: int,
    seed: int,
    context: EvidenceContext,
    difficulty: DifficultyLabel | None = None,
    bloom: BloomLevel | None = None,
) -> MutationCandidate:
    """Build one variant, or raise :class:`AdvancedError` with a stable code."""

    spec = SPECS[strategy]
    if not spec.supports(source.format):
        raise AdvancedError(
            "unsupported_mutation_format",
            f"{strategy.value} mutation does not support {source.format.value} questions",
            detail={
                "strategy": strategy.value,
                "format": source.format.value,
                "supported": sorted(item.value for item in spec.formats or ()),
            },
        )

    text = source.text
    answer_key = source.answer_key
    explanation = source.explanation
    solution_steps = list(source.solution_steps)
    options = [SourceOption(item.key, item.text, item.is_correct) for item in source.options]
    correct_key = source.correct_option_key
    new_bloom = bloom or spec.default_bloom or source.bloom
    new_difficulty = difficulty or source.difficulty
    objective = source.objective
    concept_ids = list(source.concept_ids)
    concept_labels = list(source.concept_labels)
    references = list(source.source_references)
    changed: list[str] = []
    limitations: list[str] = []

    if strategy is MutationStrategy.REPHRASE:
        text = _rephrase_text(source, index)
        changed.append("wording")

    elif strategy is MutationStrategy.SCENARIO:
        text = _scenario_text(source, index)
        changed.append("scenario")

    elif strategy is MutationStrategy.REORDER:
        parts = _enumerated_parts(source.text)
        if parts is None:
            raise AdvancedError(
                "no_reorderable_parts",
                "the question has no enumerated sub-tasks that can be reordered",
            )
        instruction, items = parts
        rotation = (index + seed) % len(items)
        rotated = items[rotation:] + items[:rotation]
        text = _relabel(instruction, rotated)
        changed.append("part_order")
        limitations.append(
            "sub-task order changed; the answer key still lists the same requirements"
        )

    elif strategy is MutationStrategy.APPLICATION:
        text = _application_text(source, context)
        passage = context.first_passage()
        changed.extend(["application_context", "reasoning_demand", "answer_key_removed"])
        # The passage is the material the learner reasons from; it is not an answer, so
        # no answer key is attached to an application the system did not solve.
        answer_key = None
        explanation = (
            f"Reference material ({passage.document_title}): {passage.excerpt}"
            if passage is not None
            else None
        )
        solution_steps = []
        new_bloom = bloom or BloomLevel.APPLY
        limitations.append(
            "the application answer is not derived automatically; a reviewer must confirm "
            "what a complete answer contains"
        )

    elif strategy is MutationStrategy.NUMERICAL:
        text, numbers = _replace_numbers(source.text, index, seed)
        changed.extend(["numeric_parameters", "answer_key_removed"])
        answer_key = None
        explanation = None
        solution_steps = []
        limitations.append(
            "numeric parameters changed from "
            f"{numbers[0]} to {numbers[1]}; the answer key was removed and must be recomputed"
        )

    elif strategy is MutationStrategy.DIFFICULTY:
        text = _terminal(source.text) + _difficulty_suffix(new_difficulty)
        changed.extend(["difficulty_demand", "answer_key_removed"])
        # The original key answers the original task only, so it is not carried over.
        answer_key = None
        explanation = None
        solution_steps = []
        limitations.append(
            "the added demand is not reflected in the stored answer key, so the key was "
            "dropped and must be rewritten for the harder or easier task"
        )

    elif strategy is MutationStrategy.COGNITIVE:
        text = _cognitive_text(source, new_bloom)
        objective = f"{OBJECTIVE_VERBS[new_bloom]} {source.concept_label} in {source.unit_label}"
        changed.extend(["cognitive_operation", "learning_objective", "answer_key_removed"])
        answer_key = None
        explanation = None
        solution_steps = []
        limitations.append(
            "the question was rewritten for a different Bloom level; the original answer key "
            "does not apply"
        )

    elif strategy is MutationStrategy.RELATED_CONCEPT:
        text, sibling = _related_text(source, context, index)
        sibling_passage = context.sibling_passage()
        sibling_id = None
        if sibling_passage is not None and sibling_passage.concept_id:
            sibling_id = sibling_passage.concept_id
        concept_ids = [sibling_id] if sibling_id else []
        concept_labels = [sibling]
        objective = f"{OBJECTIVE_VERBS[new_bloom]} {sibling} in {source.unit_label}"
        changed.extend(["concept", "learning_objective", "answer_key_removed"])
        answer_key = None
        explanation = None
        solution_steps = []
        if sibling_passage is not None:
            references.append(sibling_passage)
        limitations.append(
            "the assessed concept changed by design; the source answer key no longer applies"
        )

    elif strategy is MutationStrategy.DISTRACTOR:
        options, correct_key = _distractor_options(source, context, index, seed)
        if answer_key is None and correct_key is not None:
            answer_key = next(item.text for item in options if item.is_correct)
        changed.append("distractors")

    text = _bounded(_terminal(text))
    if strategy is not MutationStrategy.RELATED_CONCEPT:
        concept_ids = list(source.concept_ids)
        concept_labels = list(source.concept_labels)

    return MutationCandidate(
        index=index,
        strategy=strategy,
        text=text,
        concept_ids=concept_ids,
        concept_labels=concept_labels,
        answer_key=answer_key,
        explanation=explanation,
        solution_steps=solution_steps,
        options=[item.text for item in options],
        correct_option_key=correct_key,
        bloom=new_bloom,
        difficulty=new_difficulty,
        objective=objective,
        invariants=sorted(spec.invariants),
        changed=changed,
        limitations=limitations,
        source_references=references,
    )


def option_records(candidate: MutationCandidate) -> list[SourceOption]:
    """Option records for a candidate, with the correct key resolved by position."""

    if not candidate.options:
        return []
    keys = "ABCDEF"
    correct_key = candidate.correct_option_key or ""
    correct_index = keys.index(correct_key) if correct_key in keys else -1
    return [
        SourceOption(key=keys[position], text=text, is_correct=position == correct_index)
        for position, text in enumerate(candidate.options[: len(keys)])
    ]


__all__ = [
    "ALL_INVARIANTS",
    "INVARIANT_ANSWER",
    "INVARIANT_CONCEPT",
    "INVARIANT_OBJECTIVE",
    "INVARIANT_SINGLE_CORRECT",
    "INVARIANT_TASK",
    "NEED_EVIDENCE",
    "NEED_MCQ_OPTIONS",
    "NEED_NUMBERS",
    "NEED_PARTS",
    "NEED_RELATED",
    "SPECS",
    "MutationSpec",
    "has_numeric_parameters",
    "has_reorderable_parts",
    "mutate",
    "option_records",
    "spec_for",
    "supported_strategies",
]
