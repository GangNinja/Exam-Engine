"""Transparent, replaceable question classifiers.

Question type and Bloom level use weighted cue patterns from the configurable
taxonomy plus structural context. Difficulty uses observable features and is
explicitly *provisional*: no student-response data exists in Phase 3, so marks
and wording can only produce an estimate.

Every classifier returns ranked evidence and may return nothing at all. A low
score is reported as UNKNOWN instead of being rounded up to a confident label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    QuestionType,
    ReferenceKind,
    Severity,
)
from exam_engine.models.question import Uncertainty
from exam_engine.nlp.keywords import analysis_tokens
from exam_engine.understanding.structure import QuestionStructure
from exam_engine.understanding.taxonomy import Taxonomy

_TYPE_DEMAND = {
    QuestionType.DEFINITION: 0.10,
    QuestionType.EXPLANATION: 0.30,
    QuestionType.COMPARISON: 0.45,
    QuestionType.APPLICATION: 0.50,
    QuestionType.NUMERICAL: 0.55,
    QuestionType.SCENARIO: 0.50,
    QuestionType.PROOF: 0.70,
    QuestionType.PROGRAMMING: 0.60,
    QuestionType.DESIGN: 0.65,
    QuestionType.EVALUATION: 0.70,
}
_BLOOM_DEMAND = {
    BloomLevel.REMEMBER: 0.10,
    BloomLevel.UNDERSTAND: 0.30,
    BloomLevel.APPLY: 0.45,
    BloomLevel.ANALYZE: 0.60,
    BloomLevel.EVALUATE: 0.75,
    BloomLevel.CREATE: 0.85,
}
_MULTI_STEP_RE = re.compile(
    r"\b(?:then|hence|therefore|first|next|finally|followed by|and then|"
    r"step by step|assumptions? used)\b",
    re.IGNORECASE,
)
_EXPLAIN_WHY_RE = re.compile(
    r"\bexplain why\b|\bwhy .{0,50}\b(?:cannot|can't|does|works|happens|fails)\b",
    re.IGNORECASE,
)
_EXAMPLE_RE = re.compile(
    r"\b(?:give|with|include|state) (?:one|an|two|three|at least one) example",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LabelScore:
    label: str
    score: float
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Classification:
    labels: list[LabelScore] = field(default_factory=list)
    primary: str | None = None
    confidence: float = 0.0
    uncertainties: list[Uncertainty] = field(default_factory=list)


@dataclass(frozen=True)
class DifficultyResult:
    label: DifficultyLabel
    score: float
    confidence: float
    features: dict[str, float] = field(default_factory=dict)
    explanation: list[str] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)


def _quote(value: str, limit: int = 48) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "..."


def _score_definitions(text: str, taxonomy: Taxonomy, section: str) -> list[LabelScore]:
    definitions = taxonomy.question_types if section == "question_types" else taxonomy.bloom_levels
    scores: list[LabelScore] = []
    for label, definition in definitions.items():
        total = 0.0
        evidence: list[str] = []
        for cue in definition.cues:
            match = cue.compile().search(text)
            if match is None:
                continue
            total += cue.weight
            note = cue.note or f"matched '{_quote(match.group(0))}'"
            evidence.append(f"{note}: '{_quote(match.group(0))}'")
        if total > 0.0:
            scores.append(
                LabelScore(
                    label=label,
                    score=max(0.0, min(1.0, round(total, 4))),
                    evidence=evidence,
                )
            )
    scores.sort(key=lambda item: (-item.score, item.label))
    return scores


def classify_question_types(
    text: str,
    structure: QuestionStructure,
    taxonomy: Taxonomy,
    *,
    threshold: float = 0.30,
    max_labels: int = 5,
) -> Classification:
    """Multi-label question-type classification from lexical + structural cues."""
    if not text.strip():
        return Classification(
            uncertainties=[
                Uncertainty(
                    code="empty_text",
                    message="question text is empty; no type was assigned",
                    severity=Severity.WARNING,
                )
            ]
        )

    scores = _score_definitions(text, taxonomy, "question_types")
    by_label = {item.label: item for item in scores}

    def add(label: QuestionType, weight: float, reason: str) -> None:
        if label.value not in taxonomy.question_types or weight <= 0:
            return
        item = by_label.get(label.value)
        if item is None:
            item = LabelScore(label=label.value, score=0.0)
            by_label[label.value] = item
            scores.append(item)
        updated = LabelScore(
            label=item.label,
            score=round(min(1.0, item.score + weight), 4),
            evidence=[*item.evidence, reason],
        )
        by_label[label.value] = updated
        scores[scores.index(item)] = updated

    reference_kinds = {reference.kind for reference in structure.references}
    if ReferenceKind.CODE in reference_kinds:
        add(QuestionType.PROGRAMMING, 0.2, "structure: references a code listing")
    if ReferenceKind.CASE_STUDY in reference_kinds:
        add(QuestionType.SCENARIO, 0.2, "structure: references a case study or passage")
    if ReferenceKind.DIAGRAM in reference_kinds:
        add(QuestionType.DESIGN, 0.1, "structure: references a diagram or figure")
    if structure.subquestion_count >= 2:
        add(QuestionType.APPLICATION, 0.1, "structure: several sub-questions")
    if _MULTI_STEP_RE.search(text):
        add(QuestionType.APPLICATION, 0.1, "wording: multi-step instructions")

    scores.sort(key=lambda item: (-item.score, item.label))
    selected = [item for item in scores if item.score >= threshold][:max_labels]
    if not selected:
        best = scores[0] if scores else None
        return Classification(
            uncertainties=[
                Uncertainty(
                    code="unknown_question_type",
                    message=(
                        "no question-type cue reached the threshold "
                        f"({threshold:.2f}); no type was forced"
                    ),
                    severity=Severity.INFO,
                    detail={"best_score": best.score if best else 0.0},
                )
            ]
        )
    primary = selected[0]
    return Classification(
        labels=selected,
        primary=primary.label,
        confidence=round(primary.score, 4),
    )


def classify_bloom(
    text: str,
    structure: QuestionStructure,
    types: Classification,
    taxonomy: Taxonomy,
    *,
    threshold: float = 0.30,
    max_labels: int = 4,
) -> Classification:
    """Rank Bloom levels using wording, context and question structure."""
    if not text.strip():
        return Classification(
            uncertainties=[
                Uncertainty(
                    code="empty_text",
                    message="question text is empty; no Bloom level was assigned",
                    severity=Severity.WARNING,
                )
            ]
        )

    scores = _score_definitions(text, taxonomy, "bloom_levels")
    by_label = {item.label: item for item in scores}

    def add(level: BloomLevel, weight: float, reason: str) -> None:
        if level.value not in taxonomy.bloom_levels:
            return
        item = by_label.get(level.value)
        if item is None and weight <= 0:
            return
        if item is None:
            item = LabelScore(label=level.value, score=0.0)
            by_label[level.value] = item
            scores.append(item)
        updated = LabelScore(
            label=item.label,
            score=round(max(0.0, min(1.0, item.score + weight)), 4),
            evidence=[*item.evidence, reason],
        )
        by_label[level.value] = updated
        scores[scores.index(item)] = updated

    # Context matters: "explain why" is not the same demand as "explain the
    # definition". These adjustments are evidence-bearing, not hidden tuning.
    if _EXPLAIN_WHY_RE.search(text):
        # “Explain why” is a causal demand, not simple comprehension. The
        # negative adjustment prevents the generic "explain" cue from dominating.
        add(BloomLevel.ANALYZE, 0.65, "context: asks for a cause or reason")
        add(BloomLevel.UNDERSTAND, -0.2, "context: explanation alone understates causality")
    if _EXAMPLE_RE.search(text):
        add(BloomLevel.UNDERSTAND, 0.15, "context: asks for an example")
        add(BloomLevel.APPLY, 0.1, "context: transfers a concept to an example")
    if _MULTI_STEP_RE.search(text):
        add(BloomLevel.ANALYZE, 0.1, "context: multi-step instructions")
    if structure.subquestion_count >= 2:
        add(BloomLevel.ANALYZE, 0.1, "structure: several sub-questions")
    if structure.sentence_count >= 3:
        add(BloomLevel.ANALYZE, 0.05, "structure: extended reasoning text")
    reference_kinds = {reference.kind for reference in structure.references}
    if ReferenceKind.CASE_STUDY in reference_kinds:
        add(BloomLevel.ANALYZE, 0.1, "structure: case study context")
        add(BloomLevel.EVALUATE, 0.05, "structure: case study context")

    type_labels = {item.label for item in types.labels}
    if QuestionType.COMPARISON.value in type_labels:
        add(BloomLevel.ANALYZE, 0.15, "type context: comparison demand")
    if QuestionType.NUMERICAL.value in type_labels:
        add(BloomLevel.APPLY, 0.35, "type context: numerical demand")
    if QuestionType.PROOF.value in type_labels:
        add(BloomLevel.ANALYZE, 0.15, "type context: proof or derivation")
    if QuestionType.EVALUATION.value in type_labels:
        add(BloomLevel.EVALUATE, 0.15, "type context: judgement demand")
    if QuestionType.DESIGN.value in type_labels:
        add(BloomLevel.CREATE, 0.15, "type context: design demand")
    if QuestionType.DEFINITION.value in type_labels:
        add(BloomLevel.REMEMBER, 0.1, "type context: recall demand")
    if QuestionType.EXPLANATION.value in type_labels:
        add(BloomLevel.UNDERSTAND, 0.1, "type context: explanation demand")

    scores.sort(key=lambda item: (-item.score, item.label))
    selected = [item for item in scores if item.score >= threshold][:max_labels]
    if not selected:
        best = scores[0] if scores else None
        return Classification(
            uncertainties=[
                Uncertainty(
                    code="unknown_bloom_level",
                    message=(
                        f"no Bloom cue reached the threshold ({threshold:.2f}); no level was forced"
                    ),
                    severity=Severity.INFO,
                    detail={"best_score": best.score if best else 0.0},
                )
            ]
        )
    primary = selected[0]
    gap = selected[0].score - selected[1].score if len(selected) > 1 else 0.2
    # Confidence reflects both strength and separation; a tie between two levels
    # must not look as certain as one dominant level.
    confidence = primary.score * (0.6 + 0.4 * min(1.0, gap / 0.2))
    return Classification(
        labels=selected,
        primary=primary.label,
        confidence=round(max(0.0, min(1.0, confidence)), 4),
    )


def estimate_difficulty(
    text: str,
    structure: QuestionStructure,
    types: Classification,
    bloom: Classification,
    *,
    marks: float | None = None,
    easy_below: float = 0.32,
    hard_above: float = 0.60,
) -> DifficultyResult:
    """Estimate difficulty from observable features; never empirical difficulty."""
    words = structure.word_count
    if not text.strip() or words == 0:
        return DifficultyResult(
            label=DifficultyLabel.UNKNOWN,
            score=0.0,
            confidence=0.0,
            uncertainties=[
                Uncertainty(
                    code="difficulty_uncertain",
                    message="no question text is available to estimate difficulty",
                    severity=Severity.WARNING,
                )
            ],
        )

    distinct_terms = len(
        {token.casefold() for token in analysis_tokens(text, lemmatize_terms=True)}
    )
    type_scores = {item.label: item.score for item in types.labels}
    type_demand = max(
        (
            type_scores.get(question_type.value, 0.0) * demand
            for question_type, demand in _TYPE_DEMAND.items()
        ),
        default=0.0,
    )
    bloom_demand = _BLOOM_DEMAND.get(BloomLevel(bloom.primary), 0.0) if bloom.primary else 0.0
    reasoning_demand = max(type_demand, bloom_demand)
    wording = min(1.0, words / 70.0)
    structure_load = min(
        1.0,
        0.5 * min(1.0, (structure.sentence_count - 1) / 5.0)
        + 0.5 * min(1.0, structure.subquestion_count / 4.0),
    )
    concept_load = min(1.0, distinct_terms / 22.0)
    quantitative = (
        1.0
        if (
            QuestionType.NUMERICAL.value in type_scores
            or QuestionType.PROOF.value in type_scores
            or ReferenceKind.EQUATION in {reference.kind for reference in structure.references}
        )
        else 0.0
    )
    marks_load = min(1.0, marks / 20.0) if marks is not None else None

    weighted: list[tuple[float, float, str]] = [
        (0.35, reasoning_demand, "reasoning demand"),
        (0.15, wording, "wording length"),
        (0.15, structure_load, "structure and sub-questions"),
        (0.15, concept_load, "distinct content terms"),
        (0.10, quantitative, "quantitative or derivation demand"),
    ]
    if marks_load is not None:
        weighted.append((0.10, marks_load, "stated marks (weak signal)"))
    total_weight = sum(weight for weight, _value, _name in weighted)
    score = sum(weight * value for weight, value, _name in weighted) / total_weight
    score = round(max(0.0, min(1.0, score)), 4)

    coverage = total_weight
    confidence = 0.35 + 0.25 * coverage
    if types.primary:
        confidence += 0.1
    if bloom.primary:
        confidence += 0.1
    if marks is not None:
        confidence += 0.05
    if structure.confidence < 0.6:
        confidence -= 0.15
    if not types.primary and not bloom.primary:
        # Marks and wording alone cannot establish a difficulty band; without a
        # type or Bloom level, UNKNOWN is more honest than "easy".
        confidence = min(confidence, 0.29)
    confidence = round(max(0.0, min(0.9, confidence)), 4)

    marks_features = (
        {"marks": float(marks), "marks_load": round(marks_load, 4)}
        if marks is not None and marks_load is not None
        else {}
    )
    if confidence < 0.30:
        return DifficultyResult(
            label=DifficultyLabel.UNKNOWN,
            score=score,
            confidence=confidence,
            features={
                "reasoning_demand": round(reasoning_demand, 4),
                "wording_length": round(wording, 4),
                "structure_load": round(structure_load, 4),
                "concept_load": round(concept_load, 4),
                "quantitative": quantitative,
                "words": float(words),
                "sentences": float(structure.sentence_count),
                "subquestions": float(structure.subquestion_count),
                "distinct_terms": float(distinct_terms),
                **marks_features,
            },
            explanation=[
                "confidence is too low to choose a difficulty band",
                "estimated from text features; this is not measured student difficulty",
            ],
            uncertainties=[
                Uncertainty(
                    code="difficulty_uncertain",
                    message="too few observable signals to estimate difficulty",
                    severity=Severity.INFO,
                    detail={"confidence": confidence, "score": score},
                )
            ],
        )

    if score < easy_below:
        label = DifficultyLabel.EASY
    elif score > hard_above:
        label = DifficultyLabel.HARD
    else:
        label = DifficultyLabel.MODERATE

    explanation = [
        f"{words} words in {structure.sentence_count} sentence(s)",
        f"{structure.subquestion_count} detected sub-question(s)",
        f"{distinct_terms} distinct content terms",
        f"reasoning demand {reasoning_demand:.2f} from type/Bloom evidence",
        "provisional estimate from wording and structure; not measured student difficulty",
    ]
    if marks is not None:
        explanation.append(
            f"{marks:g} marks treated as a weak signal, not as a difficulty measurement"
        )
    else:
        explanation.append("no marks were stated; marks were not invented")

    return DifficultyResult(
        label=label,
        score=score,
        confidence=confidence,
        features={
            "reasoning_demand": round(reasoning_demand, 4),
            "wording_length": round(wording, 4),
            "structure_load": round(structure_load, 4),
            "concept_load": round(concept_load, 4),
            "quantitative": quantitative,
            "words": float(words),
            "sentences": float(structure.sentence_count),
            "subquestions": float(structure.subquestion_count),
            "distinct_terms": float(distinct_terms),
            **marks_features,
        },
        explanation=explanation,
    )


__all__ = [
    "Classification",
    "DifficultyResult",
    "LabelScore",
    "classify_bloom",
    "classify_question_types",
    "estimate_difficulty",
]
