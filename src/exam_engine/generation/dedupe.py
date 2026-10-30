"""Duplicate and variant detection for generated questions.

Phase 3 already defines what a duplicate means and how to score it, so this
module reuses :func:`find_similar_questions` and its taxonomoy of ``exact``,
``near``, ``semantic`` and ``concept_related`` pairs instead of inventing a
second similarity scale.

Candidate retrieval stays cheap: a generated question is only compared with the
existing questions that share the most analysis tokens with it, so a large
historical corpus does not turn every request into a quadratic comparison.
Historical questions are read, never written.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from exam_engine.models.enums import SimilarityKind
from exam_engine.models.question import QuestionRecord
from exam_engine.models.understanding import SimilarityMatch
from exam_engine.nlp.embeddings import HashingTfidfProvider
from exam_engine.nlp.keywords import analysis_tokens
from exam_engine.understanding.mapping import SemanticIndex
from exam_engine.understanding.similarity import (
    ReasoningProfile,
    SimilaritySettings,
    find_similar_questions,
    normalise_question_text,
)

#: Sources a candidate can collide with.
SOURCE_GENERATED = "generated"
SOURCE_HISTORICAL = "historical"

_HASHING_MODEL_RE = re.compile(r"^hashing-tfidf-v1-d(?P<dim>\d+)$")


@dataclass(frozen=True)
class DedupeSettings:
    """Thresholds; defaults mirror the Phase 3 similarity configuration."""

    near_threshold: float = 0.78
    semantic_threshold: float = 0.68
    concept_threshold: float = 0.35
    max_candidates: int = 25


@dataclass(frozen=True)
class PoolItem:
    """One comparison target: an existing generated or historical question."""

    question_id: str
    text: str
    source: str
    concept_ids: tuple[str, ...] = ()
    profile: ReasoningProfile = field(default_factory=ReasoningProfile)


@dataclass(frozen=True)
class DuplicateFinding:
    """The strongest relationship found between a candidate and the pool."""

    kind: str
    matched_question_id: str
    source: str
    score: float
    reason: str

    @property
    def is_duplicate(self) -> bool:
        return self.kind in {
            SimilarityKind.EXACT.value,
            SimilarityKind.NEAR.value,
            SimilarityKind.SEMANTIC.value,
        }


def _token_dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2.0 * len(left & right) / (len(left) + len(right))


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in analysis_tokens(text, lemmatize_terms=True)}


def candidate_vector(text: str, semantic: SemanticIndex | None) -> list[float] | None:
    """Embed a generated draft with the same model Phase 2 used, when possible.

    Only the deterministic hashing model can be reproduced outside a fitted
    corpus; LSA and neural vectors are corpus-dependent and are therefore not
    fabricated for new text.
    """
    if semantic is None or not semantic.model_id:
        return None
    match = _HASHING_MODEL_RE.match(semantic.model_id)
    if match is None:
        return None
    provider = HashingTfidfProvider(dim=int(match.group("dim")))
    return provider.embed([text])[0]


def _records(
    candidate_id: str,
    text: str,
    profile: ReasoningProfile,
    pool: list[PoolItem],
    course_id: str,
) -> tuple[list[QuestionRecord], dict[str, ReasoningProfile]]:
    records = [
        QuestionRecord(
            question_id=candidate_id,
            document_id="generated",
            course_id=course_id,
            text=text,
        )
    ]
    profiles: dict[str, ReasoningProfile] = {candidate_id: profile}
    for item in pool:
        records.append(
            QuestionRecord(
                question_id=item.question_id,
                document_id="existing" if item.source == SOURCE_HISTORICAL else "generated",
                course_id=course_id,
                text=item.text,
            )
        )
        profiles[item.question_id] = item.profile
    return records, profiles


def check_candidate(
    text: str,
    *,
    candidate_id: str,
    pool: list[PoolItem],
    course_id: str,
    settings: DedupeSettings,
    semantic: SemanticIndex | None = None,
    profile: ReasoningProfile | None = None,
    semantic_available: bool = True,
    concept_ids: tuple[str, ...] = (),
) -> list[DuplicateFinding]:
    """Return similarity findings for one candidate, strongest first.

    Two items that ask the same wording pattern about *different* syllabus
    concepts are variants, not duplicates: they assess different material. Only
    an identical text is rejected across concepts.
    """

    if not pool:
        return []
    profile = profile or ReasoningProfile()
    normalised = normalise_question_text(text)
    exact = [item for item in pool if normalise_question_text(item.text) == normalised]
    if exact:
        item = exact[0]
        return [
            DuplicateFinding(
                kind=SimilarityKind.EXACT.value,
                matched_question_id=item.question_id,
                source=item.source,
                score=1.0,
                reason="normalised text is identical to an existing question",
            )
        ]

    candidate_tokens = _tokens(text)
    ranked: list[tuple[float, PoolItem]] = []
    for item in pool:
        score = _token_dice(candidate_tokens, _tokens(item.text))
        if score > 0.0:
            ranked.append((score, item))
    if not ranked:
        return []
    ranked.sort(key=lambda entry: (-entry[0], entry[1].question_id))
    shortlist = [
        item
        for score, item in ranked[: settings.max_candidates]
        if score >= settings.concept_threshold
    ]
    if not shortlist:
        return []

    use_semantic = semantic_available and semantic is not None and bool(semantic.model_id)
    index: SemanticIndex | None = None
    if use_semantic and semantic is not None:
        vector = candidate_vector(text, semantic)
        if vector is None:
            index = None
        else:
            vectors = {candidate_id: vector}
            for item in shortlist:
                existing = semantic.question_vectors.get(item.question_id)
                if existing is not None:
                    vectors[item.question_id] = existing
            index = SemanticIndex(
                model_id=semantic.model_id,
                question_vectors=vectors,
                unit_vectors=semantic.unit_vectors,
            )

    records, profiles = _records(candidate_id, text, profile, shortlist, course_id)
    result = find_similar_questions(
        course_id,
        records,
        semantic=index,
        profiles=profiles,
        settings=SimilaritySettings(
            near_threshold=settings.near_threshold,
            semantic_threshold=settings.semantic_threshold,
            concept_threshold=settings.concept_threshold,
            top_k=max(1, len(records)),
            max_candidates=max(2, len(records) - 1),
        ),
    )
    by_id = {item.question_id: item for item in shortlist}
    findings: list[DuplicateFinding] = []
    for match in result.matches:
        other_id = _other_side(match, candidate_id)
        if other_id is None:
            continue
        matched = by_id.get(other_id)
        if matched is None:
            continue
        findings.append(_finding(match, matched))
    findings = [_scoped(finding, concept_ids, by_id) for finding in findings]
    findings.sort(key=lambda item: (_rank(item.kind), -item.score, item.matched_question_id))
    return findings


def _scoped(
    finding: DuplicateFinding,
    concept_ids: tuple[str, ...],
    pool: dict[str, PoolItem],
) -> DuplicateFinding:
    item = pool.get(finding.matched_question_id)
    if item is None or not concept_ids or not item.concept_ids:
        return finding
    if set(concept_ids) & set(item.concept_ids):
        return finding
    if finding.kind == SimilarityKind.EXACT.value:
        return finding
    return DuplicateFinding(
        kind=SimilarityKind.CONCEPT_RELATED.value,
        matched_question_id=finding.matched_question_id,
        source=finding.source,
        score=finding.score,
        reason=(
            "shares wording with a question on a different concept; kept as a separate "
            "item because it assesses other material"
        ),
    )


def _other_side(match: SimilarityMatch, candidate_id: str) -> str | None:
    if match.question_id == candidate_id:
        return match.matched_question_id
    if match.matched_question_id == candidate_id:
        return match.question_id
    return None


def _rank(kind: str) -> int:
    order = {
        SimilarityKind.EXACT.value: 0,
        SimilarityKind.NEAR.value: 1,
        SimilarityKind.SEMANTIC.value: 2,
        SimilarityKind.CONCEPT_RELATED.value: 3,
    }
    return order.get(kind, 4)


def _finding(match: SimilarityMatch, item: PoolItem) -> DuplicateFinding:
    if match.kind is SimilarityKind.CONCEPT_RELATED:
        reason = (
            "shares concepts or wording with an existing question but asks for "
            "materially different reasoning; kept as a variant"
        )
    elif match.kind is SimilarityKind.SEMANTIC:
        reason = "semantically equivalent to an existing question with the same reasoning profile"
    elif match.kind is SimilarityKind.NEAR:
        reason = "lexically near-identical to an existing question"
    else:
        reason = "exact duplicate of an existing question"
    return DuplicateFinding(
        kind=match.kind.value,
        matched_question_id=item.question_id,
        source=item.source,
        score=round(match.score, 4),
        reason=reason,
    )


def candidate_key(text: str) -> str:
    return "cand_" + hashlib.sha256(normalise_question_text(text).encode()).hexdigest()[:12]


__all__ = [
    "SOURCE_GENERATED",
    "SOURCE_HISTORICAL",
    "DedupeSettings",
    "DuplicateFinding",
    "PoolItem",
    "candidate_key",
    "candidate_vector",
    "check_candidate",
]
