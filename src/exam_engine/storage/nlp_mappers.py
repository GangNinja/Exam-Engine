"""Model <-> row conversion for Phase 2 NLP tables."""

from __future__ import annotations

import hashlib
from datetime import datetime

import numpy as np

from exam_engine.models.enums import (
    ConceptOrigin,
    NlpMethod,
    NlpStatus,
    TextOwnerType,
)
from exam_engine.models.nlp import (
    ConceptRecord,
    EmbeddingRecord,
    EntityRecord,
    KeyphraseRecord,
    NlpRunReport,
    ProcessedText,
    Sentence,
    TopicMember,
    TopicRecord,
    TopicTerm,
)
from exam_engine.models.question import Uncertainty
from exam_engine.storage.mappers import dumps, iso, loads, parse_dt
from exam_engine.storage.schema import (
    ConceptRow,
    DocumentSectionRow,
    EmbeddingCacheRow,
    EmbeddingRow,
    EntityRow,
    KeyphraseRow,
    NlpRunRow,
    ProcessedTextRow,
    TopicMemberRow,
    TopicRow,
    TopicTermRow,
)


def vector_cache_key(model_id: str, text_sha256: str) -> str:
    return "vec_" + hashlib.sha256(f"{model_id}\x1f{text_sha256}".encode()).hexdigest()[:32]


def encode_vector(values: list[float]) -> bytes:
    """Store float32 little-endian so vectors round-trip across platforms."""
    return np.asarray(values, dtype="<f4").tobytes()


def decode_vector(raw: bytes) -> list[float]:
    return [float(v) for v in np.frombuffer(raw, dtype="<f4")]


# --- texts -------------------------------------------------------------------


def processed_text_to_row(text: ProcessedText) -> ProcessedTextRow:
    return ProcessedTextRow(
        text_id=text.text_id,
        course_id=text.course_id,
        owner_type=text.owner_type.value,
        owner_id=text.owner_id,
        document_id=text.document_id,
        raw_sha256=text.raw_sha256,
        config_id=text.config_id,
        language=text.language,
        text=text.text,
        sentence_count=len(text.sentences),
        token_count=text.token_count,
        char_count=text.char_count,
        sentences_json=dumps([s.model_dump(mode="json") for s in text.sentences]),
        uncertainties_json=dumps([u.model_dump(mode="json") for u in text.uncertainties]),
        created_at=iso(text.created_at) or "",
    )


def row_to_processed_text(row: ProcessedTextRow) -> ProcessedText:
    return ProcessedText(
        text_id=row.text_id,
        course_id=row.course_id,
        owner_type=TextOwnerType(row.owner_type),
        owner_id=row.owner_id,
        document_id=row.document_id,
        raw_sha256=row.raw_sha256,
        config_id=row.config_id,
        language=row.language,
        text=row.text,
        sentences=[Sentence.model_validate(item) for item in loads(row.sentences_json, [])],
        token_count=row.token_count,
        char_count=row.char_count,
        uncertainties=[
            Uncertainty.model_validate(item) for item in loads(row.uncertainties_json, [])
        ],
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
    )


def section_to_row(
    *,
    section_id: str,
    document_id: str,
    course_id: str,
    section_index: int,
    heading: str | None,
    text: str,
    char_start: int,
    char_end: int,
    page_start: int | None,
    page_end: int | None,
) -> DocumentSectionRow:
    return DocumentSectionRow(
        section_id=section_id,
        document_id=document_id,
        course_id=course_id,
        section_index=section_index,
        heading=heading,
        text=text,
        char_start=char_start,
        char_end=char_end,
        page_start=page_start,
        page_end=page_end,
    )


# --- embeddings --------------------------------------------------------------


def embedding_cache_row(
    *, model_id: str, text_sha256: str, values: list[float], created_at: datetime
) -> EmbeddingCacheRow:
    return EmbeddingCacheRow(
        cache_key=vector_cache_key(model_id, text_sha256),
        model_id=model_id,
        text_sha256=text_sha256,
        dim=len(values),
        vector=encode_vector(values),
        created_at=iso(created_at) or "",
    )


def embedding_to_row(embedding: EmbeddingRecord) -> EmbeddingRow:
    return EmbeddingRow(
        embedding_id=embedding.embedding_id,
        course_id=embedding.course_id,
        owner_type=embedding.owner_type.value,
        owner_id=embedding.owner_id,
        document_id=embedding.document_id,
        model_id=embedding.model_id,
        dim=embedding.dim,
        cache_key=vector_cache_key(embedding.model_id, embedding.text_sha256),
        text_sha256=embedding.text_sha256,
        created_at=iso(embedding.created_at) or "",
    )


def row_to_embedding(row: EmbeddingRow) -> EmbeddingRecord:
    return EmbeddingRecord(
        embedding_id=row.embedding_id,
        course_id=row.course_id,
        owner_type=TextOwnerType(row.owner_type),
        owner_id=row.owner_id,
        document_id=row.document_id,
        model_id=row.model_id,
        dim=row.dim,
        text_sha256=row.text_sha256,
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
    )


# --- keyphrases / entities / concepts ----------------------------------------


def keyphrase_to_row(keyphrase: KeyphraseRecord) -> KeyphraseRow:
    return KeyphraseRow(
        keyphrase_id=keyphrase.keyphrase_id,
        course_id=keyphrase.course_id,
        owner_type=keyphrase.owner_type.value,
        owner_id=keyphrase.owner_id,
        document_id=keyphrase.document_id,
        term=keyphrase.term,
        normalized=keyphrase.normalized,
        score=keyphrase.score,
        method=keyphrase.method.value,
        rank=keyphrase.rank,
        n_gram=keyphrase.n_gram,
        metadata_json=dumps(keyphrase.metadata),
    )


def row_to_keyphrase(row: KeyphraseRow) -> KeyphraseRecord:
    return KeyphraseRecord(
        keyphrase_id=row.keyphrase_id,
        course_id=row.course_id,
        owner_type=TextOwnerType(row.owner_type),
        owner_id=row.owner_id,
        document_id=row.document_id,
        term=row.term,
        normalized=row.normalized,
        score=row.score,
        method=NlpMethod(row.method),
        rank=row.rank,
        n_gram=row.n_gram,
        metadata=loads(row.metadata_json, {}),
    )


def entity_to_row(entity: EntityRecord) -> EntityRow:
    return EntityRow(
        entity_id=entity.entity_id,
        course_id=entity.course_id,
        document_id=entity.document_id,
        owner_type=entity.owner_type.value,
        owner_id=entity.owner_id,
        text=entity.text,
        normalized=entity.normalized,
        label=entity.label,
        method=entity.method.value,
        confidence=entity.confidence,
        start_char=entity.start_char,
        end_char=entity.end_char,
        metadata_json=dumps(entity.metadata),
    )


def row_to_entity(row: EntityRow) -> EntityRecord:
    return EntityRecord(
        entity_id=row.entity_id,
        course_id=row.course_id,
        document_id=row.document_id,
        owner_type=TextOwnerType(row.owner_type),
        owner_id=row.owner_id,
        text=row.text,
        normalized=row.normalized,
        label=row.label,
        method=NlpMethod(row.method),
        confidence=row.confidence,
        start_char=row.start_char,
        end_char=row.end_char,
        metadata=loads(row.metadata_json, {}),
    )


def concept_to_row(concept: ConceptRecord) -> ConceptRow:
    return ConceptRow(
        concept_id=concept.concept_id,
        course_id=concept.course_id,
        term=concept.term,
        normalized=concept.normalized,
        origin=concept.origin.value,
        method=concept.method.value,
        confidence=concept.confidence,
        verified=1 if concept.verified else 0,
        unit_id=concept.unit_id,
        evidence_json=dumps(concept.evidence),
        created_at=iso(concept.created_at) or "",
    )


def row_to_concept(row: ConceptRow) -> ConceptRecord:
    return ConceptRecord(
        concept_id=row.concept_id,
        course_id=row.course_id,
        term=row.term,
        normalized=row.normalized,
        origin=ConceptOrigin(row.origin),
        method=NlpMethod(row.method),
        confidence=row.confidence,
        verified=bool(row.verified),
        unit_id=row.unit_id,
        evidence=loads(row.evidence_json, []),
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
    )


# --- topics ------------------------------------------------------------------


def topic_to_row(topic: TopicRecord, created_at: datetime) -> TopicRow:
    return TopicRow(
        topic_id=topic.topic_id,
        course_id=topic.course_id,
        scope=topic.scope.value,
        scope_id=topic.scope_id,
        method=topic.method.value,
        k=topic.k,
        label=topic.label,
        size=topic.size,
        params_json=dumps(topic.metadata),
        warnings_json=dumps([w.model_dump(mode="json") for w in topic.warnings]),
        created_at=iso(created_at) or "",
    )


def topic_term_to_row(topic_id: str, term: TopicTerm) -> TopicTermRow:
    return TopicTermRow(
        topic_id=topic_id,
        term=term.term,
        normalized=term.term.casefold(),
        weight=term.weight,
        rank=term.rank,
    )


def topic_member_to_row(topic_id: str, member: TopicMember) -> TopicMemberRow:
    return TopicMemberRow(
        topic_id=topic_id,
        owner_type=member.owner_type.value,
        owner_id=member.owner_id,
        document_id=member.document_id,
        weight=member.weight,
    )


def row_to_topic(
    row: TopicRow,
    terms: list[TopicTermRow],
    members: list[TopicMemberRow],
    created_at: datetime,
) -> TopicRecord:
    return TopicRecord(
        topic_id=row.topic_id,
        course_id=row.course_id,
        scope=TextOwnerType(row.scope),
        scope_id=row.scope_id,
        method=NlpMethod(row.method),
        k=row.k,
        label=row.label,
        terms=[
            TopicTerm(term=t.term, weight=t.weight, rank=t.rank)
            for t in sorted(terms, key=lambda t: t.rank)
        ],
        members=[
            TopicMember(
                owner_type=TextOwnerType(m.owner_type),
                owner_id=m.owner_id,
                document_id=m.document_id,
                weight=m.weight,
            )
            for m in members
        ],
        size=row.size,
        warnings=[Uncertainty.model_validate(item) for item in loads(row.warnings_json, [])],
        metadata=loads(row.params_json, {}),
        created_at=created_at,
    )


# --- runs --------------------------------------------------------------------


def run_to_row(report: NlpRunReport) -> NlpRunRow:
    return NlpRunRow(
        run_id=report.run_id,
        course_id=report.course_id,
        status=report.status.value,
        started_at=iso(report.started_at) or "",
        finished_at=iso(report.finished_at),
        model_ids_json=dumps(report.model_ids),
        methods_json=dumps(report.methods),
        totals_json=dumps(report.totals),
        config_json=dumps(report.config),
        warnings_json=dumps([w.model_dump(mode="json") for w in report.warnings]),
        errors_json=dumps([e.model_dump(mode="json") for e in report.errors]),
        duration_ms=report.duration_ms,
    )


def row_to_run(row: NlpRunRow) -> NlpRunReport:
    return NlpRunReport(
        run_id=row.run_id,
        course_id=row.course_id,
        status=NlpStatus(row.status),
        started_at=parse_dt(row.started_at) or datetime.now().astimezone(),
        finished_at=parse_dt(row.finished_at),
        model_ids=loads(row.model_ids_json, []),
        methods=loads(row.methods_json, []),
        totals=loads(row.totals_json, {}),
        config=loads(row.config_json, {}),
        warnings=[Uncertainty.model_validate(item) for item in loads(row.warnings_json, [])],
        errors=[Uncertainty.model_validate(item) for item in loads(row.errors_json, [])],
        duration_ms=row.duration_ms,
    )


__all__ = [
    "concept_to_row",
    "decode_vector",
    "embedding_cache_row",
    "embedding_to_row",
    "encode_vector",
    "entity_to_row",
    "keyphrase_to_row",
    "processed_text_to_row",
    "row_to_concept",
    "row_to_embedding",
    "row_to_entity",
    "row_to_keyphrase",
    "row_to_processed_text",
    "row_to_run",
    "row_to_topic",
    "run_to_row",
    "section_to_row",
    "topic_member_to_row",
    "topic_term_to_row",
    "topic_to_row",
    "vector_cache_key",
]
