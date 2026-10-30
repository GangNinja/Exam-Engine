"""Course-scoped syllabus mapping for questions.

Signals, combined in a documented order:

1. An explicit unit/chapter reference (``questions.unit`` or wording such as
   "from Unit 2") is authoritative *within the question's own course*. If the
   reference does not resolve, the question stays unmapped rather than being
   mapped by a guessed lexical neighbour or another course's syllabus.
2. TF-IDF cosine over the course's syllabus units, unit titles and topics.
3. Optional Phase 2 embedding cosine, used only when the question and unit were
   embedded by the same model id.
4. Topic-level similarity, reported alongside the parent unit.

The mapper never forces a question into a unit: below the configured threshold,
or when the top candidates are indistinguishable, the result is UNMAPPED or
AMBIGUOUS with the evidence that led there.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from exam_engine.models.enums import MappingStatus, Severity
from exam_engine.models.question import Uncertainty
from exam_engine.models.syllabus import SyllabusRecord, SyllabusUnitRecord
from exam_engine.models.understanding import UnitMatch, make_unit_match_id
from exam_engine.nlp.keywords import analysis_tokens

METHOD = "tfidf+semantic"
_UNIT_REFERENCE_RE = re.compile(
    r"\b(?:unit|module|chapter|block)\s*(?:no\.?|number)?\s*[-:]?\s*"
    r"(?P<number>\d{1,2}|[ivxIVX]{1,4})\b",
    re.IGNORECASE,
)
# A bare "unit 2" is not always a syllabus reference ("a unit 2 kg mass").
# Text-derived references therefore need a title-cased keyword or a cue before
# them; the explicit ``questions.unit`` field is used without this guard.
_UNIT_REFERENCE_CUE_RE = re.compile(
    r"(?:\bfrom|\bin|\bon|\bof|\bunder|\bwithin|\bper|\bfor|\bsee|\bacross|"
    r"\bthroughout|\baccording to|\bbased on|\brefer(?:ring)? to|\bchapter|"
    r"\bmodule|\bunit|\bblock)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def _roman_to_int(value: str) -> int | None:
    lowered = value.casefold()
    if not lowered or any(char not in _ROMAN_VALUES for char in lowered):
        return None
    total = 0
    previous = 0
    for char in reversed(lowered):
        current = _ROMAN_VALUES[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total or None


def _reference_number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    return _roman_to_int(value)


def _normalise_reference(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class SemanticIndex:
    """Question and unit vectors from one Phase 2 embedding model."""

    model_id: str | None = None
    question_vectors: dict[str, list[float]] = field(default_factory=dict)
    unit_vectors: dict[str, list[float]] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return bool(self.model_id and self.question_vectors and self.unit_vectors)

    @property
    def question_similarity_available(self) -> bool:
        return bool(self.model_id and len(self.question_vectors) >= 2)

    def similarity(self, question_id: str, unit_id: str) -> float | None:
        left = self.question_vectors.get(question_id)
        right = self.unit_vectors.get(unit_id)
        if left is None or right is None or len(left) != len(right):
            return None
        return _cosine(left, right)


@dataclass(frozen=True)
class UnitSource:
    """A unit plus the syllabus version it belongs to."""

    syllabus_id: str
    unit: SyllabusUnitRecord


@dataclass(frozen=True)
class MappingCandidate:
    unit_id: str
    syllabus_id: str
    unit_label: str
    unit_title: str | None
    topic: str | None
    score: float
    matched_terms: list[str]
    evidence: list[str]


@dataclass(frozen=True)
class MappingResult:
    status: MappingStatus
    confidence: float = 0.0
    candidates: list[MappingCandidate] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)

    @property
    def top(self) -> MappingCandidate | None:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True)
class MappingSettings:
    threshold: float = 0.18
    margin: float = 0.04
    max_candidates: int = 3
    semantic_weight: float = 0.6


def _cosine(left: Any, right: Any) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _tfidf_vectors(documents: list[list[str]]) -> list[dict[str, float]]:
    """Small in-house TF-IDF so mapping has no model download or corpus minimum."""
    if not documents:
        return []
    document_frequency: Counter[str] = Counter()
    for tokens in documents:
        document_frequency.update(set(tokens))
    total = len(documents)
    vectors: list[dict[str, float]] = []
    for tokens in documents:
        counts = Counter(tokens)
        vector: dict[str, float] = {}
        for term, count in counts.items():
            idf = math.log((1 + total) / (1 + document_frequency[term])) + 1.0
            weight = (1.0 + math.log(count)) * idf
            vector[term] = weight
        norm = math.sqrt(sum(value * value for value in vector.values()))
        vectors.append({term: value / norm for term, value in vector.items()} if norm else {})
    return vectors


def _cosine_sparse(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return max(0.0, min(1.0, sum(value * right.get(term, 0.0) for term, value in left.items())))


def _tokens(text: str) -> list[str]:
    """Case-folded, lightly lemmatised terms for lexical comparison."""
    return [token.casefold() for token in analysis_tokens(text, lemmatize_terms=True)]


def _unit_tokens(unit: SyllabusUnitRecord) -> list[str]:
    title_tokens = _tokens(unit.title or "")
    topic_tokens = [token for topic in unit.topics for token in _tokens(topic)]
    body_tokens = _tokens(unit.raw_text[:6000])
    # Titles and named topics are curated; weight them by repeating tokens.
    return [*title_tokens, *title_tokens, *topic_tokens, *topic_tokens, *body_tokens]


def _text_unit_references(text: str) -> list[str]:
    """Find reference-like "Unit 2" spans in question wording, not measurements."""
    references: list[str] = []
    seen: set[str] = set()
    for match in _UNIT_REFERENCE_RE.finditer(text):
        if not (
            match.start() == 0
            or text[match.start()].isupper()
            or _UNIT_REFERENCE_CUE_RE.search(text[: match.start()])
        ):
            continue
        value = match.group(0).strip()
        key = _normalise_reference(value)
        if key and key not in seen:
            references.append(value)
            seen.add(key)
    return references


def _resolve_reference(reference: str, sources: list[UnitSource]) -> list[UnitSource]:
    normalised = _normalise_reference(reference)
    matches: list[UnitSource] = []
    for source in sources:
        unit = source.unit
        label = _normalise_reference(unit.unit_label)
        title = _normalise_reference(unit.title or "")
        if normalised and (normalised == label or (title and normalised == title)):
            matches.append(source)
            continue
        number = _reference_number(reference)
        if number is None:
            # Composite metadata such as "Unit 2: Linear Structures" or
            # "Chapter IV - Trees" must resolve to its own number.
            unit_match = _UNIT_REFERENCE_RE.search(reference)
            if unit_match:
                number = _reference_number(unit_match.group("number"))
        if number is None:
            token_match = re.search(r"(?<!\w)(\d{1,2}|[ivxIVX]{1,4})(?!\w)", reference)
            number = _reference_number(token_match.group(1)) if token_match else None
        if number is not None and unit.unit_number == number:
            matches.append(source)
    unique = {source.unit.unit_id: source for source in matches}
    return list(unique.values())


def _explicit_match(
    question_unit: str | None, text: str, sources: list[UnitSource]
) -> tuple[list[UnitSource], str | None]:
    if question_unit and question_unit.strip():
        references = [question_unit.strip()]
    else:
        references = _text_unit_references(text)
    if not references:
        return [], None

    matches: list[UnitSource] = []
    unresolved: list[str] = []
    for reference in references:
        resolved = _resolve_reference(reference, sources)
        if resolved:
            matches.extend(resolved)
        else:
            unresolved.append(reference)
    if unresolved:
        # A question that points at a unit outside this course's syllabus is
        # not silently mapped to whichever other reference happened to resolve.
        return [], ", ".join(unresolved)
    unique = {source.unit.unit_id: source for source in matches}
    return list(unique.values()), ", ".join(references)


def map_question(
    *,
    course_id: str,
    question_id: str,
    text: str,
    question_unit: str | None,
    sources: list[UnitSource],
    semantic: SemanticIndex | None = None,
    settings: MappingSettings | None = None,
) -> MappingResult:
    """Rank this course's syllabus units for one question."""
    settings = settings or MappingSettings()
    if not sources:
        return MappingResult(
            status=MappingStatus.UNMAPPED,
            uncertainties=[
                Uncertainty(
                    code="no_syllabus",
                    message=(
                        "no syllabus units are available for this course; "
                        "the question is left unmapped"
                    ),
                    severity=Severity.WARNING,
                    detail={"course_id": course_id},
                )
            ],
        )

    explicit_sources, explicit_reference = _explicit_match(question_unit, text, sources)
    if explicit_reference and not explicit_sources:
        return MappingResult(
            status=MappingStatus.UNMAPPED,
            uncertainties=[
                Uncertainty(
                    code="unmapped_question",
                    message=(
                        f"question references '{explicit_reference}', which is not in "
                        "this course's syllabus; no other course was used"
                    ),
                    severity=Severity.WARNING,
                    detail={
                        "course_id": course_id,
                        "question_id": question_id,
                        "reference": explicit_reference,
                    },
                )
            ],
        )

    unit_documents = [_unit_tokens(source.unit) for source in sources]
    question_tokens = _tokens(text)
    vectors = _tfidf_vectors([*unit_documents, question_tokens])
    unit_vectors = vectors[:-1]
    question_vector = vectors[-1]

    candidates: list[MappingCandidate] = []
    for source, _unit_doc, unit_vector in zip(sources, unit_documents, unit_vectors, strict=True):
        unit = source.unit
        lexical = _cosine_sparse(question_vector, unit_vector)
        topic, topic_score = _best_topic(question_tokens, unit)
        semantic_score = (
            semantic.similarity(question_id, unit.unit_id)
            if semantic and semantic.available
            else None
        )
        if semantic_score is not None:
            score = (
                settings.semantic_weight * semantic_score
                + (1.0 - settings.semantic_weight) * lexical
            )
        else:
            score = lexical
        if topic_score > score:
            score = topic_score
        score = max(0.0, min(1.0, score))

        evidence: list[str] = []
        if explicit_sources and unit.unit_id in {s.unit.unit_id for s in explicit_sources}:
            score = max(score, 0.9)
            evidence.append(f"explicit unit reference '{explicit_reference}'")
        evidence.append(f"tf-idf cosine {lexical:.3f}")
        if semantic_score is not None and semantic is not None:
            evidence.append(f"semantic cosine {semantic_score:.3f} ({semantic.model_id})")
        if topic_score > 0.0 and topic:
            evidence.append(f"topic '{topic}' lexical {topic_score:.3f}")

        matched_terms = _matched_terms(question_vector, unit_vector)
        if matched_terms:
            evidence.append("shared terms: " + ", ".join(matched_terms[:6]))
        candidates.append(
            MappingCandidate(
                unit_id=unit.unit_id,
                syllabus_id=source.syllabus_id,
                unit_label=unit.unit_label,
                unit_title=unit.title,
                topic=topic,
                score=round(score, 4),
                matched_terms=matched_terms,
                evidence=evidence,
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.unit_id))
    above = [item for item in candidates if item.score >= settings.threshold]
    top_score = candidates[0].score if candidates else 0.0
    selected = above[: max(1, settings.max_candidates)]

    if not selected:
        return MappingResult(
            status=MappingStatus.UNMAPPED,
            confidence=top_score,
            uncertainties=[
                Uncertainty(
                    code="unmapped_question",
                    message=(
                        "no syllabus unit met the mapping threshold "
                        f"({settings.threshold:.2f}); best score {top_score:.2f}"
                    ),
                    severity=Severity.INFO,
                    detail={
                        "course_id": course_id,
                        "question_id": question_id,
                        "best_score": top_score,
                    },
                )
            ],
        )

    explicit_ids = {source.unit.unit_id for source in explicit_sources}
    # Look at every explicit candidate, not just the top-k slice: a low
    # candidate cap must not turn two named units into a confident single match.
    explicit_candidates = [item for item in candidates if item.unit_id in explicit_ids]
    if len(explicit_candidates) == 1:
        selected = explicit_candidates
        status = MappingStatus.MAPPED
        confidence = max(0.9, explicit_candidates[0].score)
    elif len(explicit_candidates) > 1:
        selected = explicit_candidates[: max(1, settings.max_candidates)]
        status = MappingStatus.AMBIGUOUS
        confidence = 0.6
    else:
        # Compare the two best candidates before applying the review cap; a cap
        # of one must not hide a tie with the runner-up.
        gap = above[0].score - above[1].score if len(above) > 1 else 1.0
        status = MappingStatus.MAPPED if gap >= settings.margin else MappingStatus.AMBIGUOUS
        confidence = selected[0].score

    uncertainties: list[Uncertainty] = []
    if status is MappingStatus.AMBIGUOUS:
        uncertainties.append(
            Uncertainty(
                code="ambiguous_mapping",
                message=(
                    "several syllabus units matched within the configured margin; "
                    "a human should review this question"
                ),
                severity=Severity.WARNING,
                detail={
                    "course_id": course_id,
                    "question_id": question_id,
                    "candidates": [
                        {"unit_id": item.unit_id, "score": item.score} for item in selected
                    ],
                },
            )
        )
    return MappingResult(
        status=status,
        confidence=round(max(0.0, min(1.0, confidence)), 4),
        candidates=selected,
        uncertainties=uncertainties,
    )


def _best_topic(question_tokens: list[str], unit: SyllabusUnitRecord) -> tuple[str | None, float]:
    best_topic: str | None = None
    best_score = 0.0
    for topic in unit.topics:
        topic_tokens = _tokens(topic)
        if not topic_tokens or not question_tokens:
            continue
        score = len(set(question_tokens) & set(topic_tokens)) / math.sqrt(
            len(set(question_tokens)) * len(set(topic_tokens))
        )
        if score > best_score:
            best_topic, best_score = topic, score
    return best_topic, min(1.0, best_score)


def _matched_terms(question_vector: dict[str, float], unit_vector: dict[str, float]) -> list[str]:
    shared = [
        (term, question_vector[term] * unit_vector[term])
        for term in question_vector
        if term in unit_vector
    ]
    shared.sort(key=lambda item: (-item[1], item[0]))
    # Only terms present in both vectors are "shared". Topic tokens that the
    # question never uses are represented by the separate topic evidence line.
    return [term for term, _score in shared[:10]]


def to_unit_matches(result: MappingResult, *, course_id: str, question_id: str) -> list[UnitMatch]:
    """Convert mapping candidates into persisted records."""
    matches: list[UnitMatch] = []
    for rank, candidate in enumerate(result.candidates, start=1):
        method = (
            METHOD if any("semantic cosine" in item for item in candidate.evidence) else "tfidf"
        )
        matches.append(
            UnitMatch(
                match_id=make_unit_match_id(
                    course_id,
                    question_id,
                    candidate.unit_id,
                    candidate.topic,
                    method,
                ),
                question_id=question_id,
                course_id=course_id,
                unit_id=candidate.unit_id,
                syllabus_id=candidate.syllabus_id,
                unit_label=candidate.unit_label,
                unit_title=candidate.unit_title,
                topic=candidate.topic,
                score=candidate.score,
                rank=rank,
                method=method,
                matched_terms=candidate.matched_terms,
                evidence=candidate.evidence,
            )
        )
    return matches


def unit_sources(syllabi: list[SyllabusRecord]) -> list[UnitSource]:
    return [
        UnitSource(syllabus_id=syllabus.syllabus_id, unit=unit)
        for syllabus in syllabi
        for unit in syllabus.units
    ]


__all__ = [
    "METHOD",
    "MappingCandidate",
    "MappingResult",
    "MappingSettings",
    "SemanticIndex",
    "UnitSource",
    "map_question",
    "to_unit_matches",
    "unit_sources",
]
