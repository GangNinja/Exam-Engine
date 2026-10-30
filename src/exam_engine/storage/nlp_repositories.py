"""Repositories for Phase 2 NLP tables.

Same rule as Phase 1: this module is the only place that touches ORM rows for
derived NLP data. Everything here is replaceable derived state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from exam_engine.models.enums import ConceptOrigin, TextOwnerType
from exam_engine.models.nlp import (
    ConceptRecord,
    EmbeddingRecord,
    EntityRecord,
    KeyphraseRecord,
    NlpRunReport,
    ProcessedText,
    TopicRecord,
)
from exam_engine.storage import nlp_mappers as mappers
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


class ProcessedTextRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, owner_type: TextOwnerType, owner_id: str) -> ProcessedText | None:
        row = self.session.scalars(
            select(ProcessedTextRow).where(
                ProcessedTextRow.owner_type == owner_type.value,
                ProcessedTextRow.owner_id == owner_id,
            )
        ).first()
        return mappers.row_to_processed_text(row) if row else None

    def upsert(self, text: ProcessedText) -> None:
        existing = self.session.get(ProcessedTextRow, text.text_id)
        row = mappers.processed_text_to_row(text)
        if existing is not None:
            self.session.delete(existing)
            self.session.flush()
        self.session.add(row)
        self.session.flush()

    def list_by_course(self, course_id: str) -> list[ProcessedText]:
        rows = self.session.scalars(
            select(ProcessedTextRow)
            .where(ProcessedTextRow.course_id == course_id)
            .order_by(ProcessedTextRow.owner_type, ProcessedTextRow.owner_id)
        ).all()
        return [mappers.row_to_processed_text(r) for r in rows]

    def count_by_course(self, course_id: str, owner_type: TextOwnerType | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(ProcessedTextRow)
            .where(ProcessedTextRow.course_id == course_id)
        )
        if owner_type is not None:
            stmt = stmt.where(ProcessedTextRow.owner_type == owner_type.value)
        return int(self.session.scalar(stmt) or 0)

    def delete_for_course(self, course_id: str) -> None:
        self.session.execute(
            delete(ProcessedTextRow).where(ProcessedTextRow.course_id == course_id)
        )
        self.session.flush()

    def delete_missing(self, course_id: str, owners: set[tuple[TextOwnerType, str]]) -> None:
        """Drop cached texts for owners this run no longer produces."""
        keep = {(owner_type.value, owner_id) for owner_type, owner_id in owners}
        rows = self.session.scalars(
            select(ProcessedTextRow).where(ProcessedTextRow.course_id == course_id)
        ).all()
        for row in rows:
            if (row.owner_type, row.owner_id) not in keep:
                self.session.delete(row)
        self.session.flush()


class SectionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_document(self, document_id: str, sections: list[DocumentSectionRow]) -> None:
        self.session.execute(
            delete(DocumentSectionRow).where(DocumentSectionRow.document_id == document_id)
        )
        for section in sections:
            self.session.add(section)
        self.session.flush()

    def list_for_document(self, document_id: str) -> list[DocumentSectionRow]:
        return list(
            self.session.scalars(
                select(DocumentSectionRow)
                .where(DocumentSectionRow.document_id == document_id)
                .order_by(DocumentSectionRow.section_index)
            ).all()
        )

    def count_by_course(self, course_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(DocumentSectionRow)
                .where(DocumentSectionRow.course_id == course_id)
            )
            or 0
        )

    def delete_for_course(self, course_id: str) -> None:
        self.session.execute(
            delete(DocumentSectionRow).where(DocumentSectionRow.course_id == course_id)
        )
        self.session.flush()


class VectorStoreRepository:
    """SQLite-backed vector cache; swap this class out for another backend."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, model_id: str, text_sha256: str) -> list[float] | None:
        row = self.session.get(EmbeddingCacheRow, mappers.vector_cache_key(model_id, text_sha256))
        return mappers.decode_vector(row.vector) if row else None

    def put(
        self,
        model_id: str,
        text_sha256: str,
        values: list[float],
        *,
        created_at: datetime,
    ) -> None:
        row = mappers.embedding_cache_row(
            model_id=model_id, text_sha256=text_sha256, values=values, created_at=created_at
        )
        existing = self.session.get(EmbeddingCacheRow, row.cache_key)
        if existing is not None:
            existing.dim = row.dim
            existing.vector = row.vector
            existing.created_at = row.created_at
        else:
            self.session.add(row)
        self.session.flush()

    def count(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(EmbeddingCacheRow)) or 0)


class EmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, embedding: EmbeddingRecord) -> None:
        row = self.session.get(EmbeddingRow, embedding.embedding_id)
        new_row = mappers.embedding_to_row(embedding)
        if row is not None:
            self.session.delete(row)
            self.session.flush()
        self.session.add(new_row)
        self.session.flush()

    def list_by_course(self, course_id: str) -> list[EmbeddingRecord]:
        rows = self.session.scalars(
            select(EmbeddingRow)
            .where(EmbeddingRow.course_id == course_id)
            .order_by(EmbeddingRow.owner_type, EmbeddingRow.owner_id)
        ).all()
        return [mappers.row_to_embedding(r) for r in rows]

    def count_by_course(self, course_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(EmbeddingRow)
                .where(EmbeddingRow.course_id == course_id)
            )
            or 0
        )

    def delete_for_course(self, course_id: str) -> None:
        self.session.execute(delete(EmbeddingRow).where(EmbeddingRow.course_id == course_id))
        self.session.flush()


class KeyphraseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_owner(self, course_id: str, owner_type: TextOwnerType, owner_id: str) -> None:
        self.session.execute(
            delete(KeyphraseRow).where(
                KeyphraseRow.course_id == course_id,
                KeyphraseRow.owner_type == owner_type.value,
                KeyphraseRow.owner_id == owner_id,
            )
        )
        self.session.flush()

    def add_many(self, keyphrases: list[KeyphraseRecord]) -> None:
        for keyphrase in keyphrases:
            self.session.merge(mappers.keyphrase_to_row(keyphrase))
        self.session.flush()

    def list_for_owner(
        self, owner_type: TextOwnerType, owner_id: str, method: str | None = None
    ) -> list[KeyphraseRecord]:
        stmt = select(KeyphraseRow).where(
            KeyphraseRow.owner_type == owner_type.value,
            KeyphraseRow.owner_id == owner_id,
        )
        if method:
            stmt = stmt.where(KeyphraseRow.method == method)
        rows = self.session.scalars(stmt.order_by(KeyphraseRow.rank)).all()
        return [mappers.row_to_keyphrase(r) for r in rows]

    def top_for_course(
        self,
        course_id: str,
        limit: int = 20,
        method: str | None = None,
        owner_type: TextOwnerType | None = None,
    ) -> list[KeyphraseRecord]:
        stmt = select(KeyphraseRow).where(KeyphraseRow.course_id == course_id)
        if method:
            stmt = stmt.where(KeyphraseRow.method == method)
        if owner_type is not None:
            stmt = stmt.where(KeyphraseRow.owner_type == owner_type.value)
        rows = self.session.scalars(
            stmt.order_by(KeyphraseRow.rank, KeyphraseRow.score.desc(), KeyphraseRow.term).limit(
                limit
            )
        ).all()
        return [mappers.row_to_keyphrase(r) for r in rows]

    def count_by_course(self, course_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(KeyphraseRow)
                .where(KeyphraseRow.course_id == course_id)
            )
            or 0
        )

    def delete_for_course(self, course_id: str) -> None:
        self.session.execute(delete(KeyphraseRow).where(KeyphraseRow.course_id == course_id))
        self.session.flush()


class EntityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_owner(self, course_id: str, owner_type: TextOwnerType, owner_id: str) -> None:
        self.session.execute(
            delete(EntityRow).where(
                EntityRow.course_id == course_id,
                EntityRow.owner_type == owner_type.value,
                EntityRow.owner_id == owner_id,
            )
        )
        self.session.flush()

    def add_many(self, entities: list[EntityRecord]) -> None:
        for entity in entities:
            self.session.merge(mappers.entity_to_row(entity))
        self.session.flush()

    def list_by_course(
        self, course_id: str, limit: int | None = None, label: str | None = None
    ) -> list[EntityRecord]:
        stmt = select(EntityRow).where(EntityRow.course_id == course_id)
        if label:
            stmt = stmt.where(EntityRow.label == label)
        stmt = stmt.order_by(EntityRow.owner_id, EntityRow.start_char)
        if limit:
            stmt = stmt.limit(limit)
        rows = self.session.scalars(stmt).all()
        return [mappers.row_to_entity(r) for r in rows]

    def count_by_course(self, course_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(EntityRow).where(EntityRow.course_id == course_id)
            )
            or 0
        )

    def delete_for_course(self, course_id: str) -> None:
        self.session.execute(delete(EntityRow).where(EntityRow.course_id == course_id))
        self.session.flush()


class ConceptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, concepts: list[ConceptRecord]) -> None:
        for concept in concepts:
            self.session.merge(mappers.concept_to_row(concept))
        self.session.flush()

    def replace_origin(self, course_id: str, origin: ConceptOrigin) -> None:
        self.session.execute(
            delete(ConceptRow).where(
                ConceptRow.course_id == course_id, ConceptRow.origin == origin.value
            )
        )
        self.session.flush()

    def list_by_course(
        self, course_id: str, origin: ConceptOrigin | None = None, limit: int | None = None
    ) -> list[ConceptRecord]:
        stmt = select(ConceptRow).where(ConceptRow.course_id == course_id)
        if origin is not None:
            stmt = stmt.where(ConceptRow.origin == origin.value)
        # Verified syllabus concepts lead: they are the only ground truth in
        # this table, so mixed listings must not bury them under candidates.
        stmt = stmt.order_by(ConceptRow.verified.desc(), ConceptRow.origin, ConceptRow.term)
        if limit:
            stmt = stmt.limit(limit)
        rows = self.session.scalars(stmt).all()
        return [mappers.row_to_concept(r) for r in rows]

    def delete_for_course(self, course_id: str) -> None:
        self.session.execute(delete(ConceptRow).where(ConceptRow.course_id == course_id))
        self.session.flush()

    def list_foreign_terms(self, course_id: str, limit: int = 500) -> list[str]:
        """Distinct concept terms stored for courses other than ``course_id``.

        Phase 10 uses this only to detect accidental cross-course material in a
        generated question; nothing from the other courses is read further.
        """
        stmt = (
            select(ConceptRow.term)
            .where(ConceptRow.course_id != course_id)
            .order_by(ConceptRow.term)
            .limit(limit)
        )
        return [str(term) for term in self.session.scalars(stmt).all()]

    def count_by_course(self, course_id: str, origin: ConceptOrigin | None = None) -> int:
        stmt = select(func.count()).select_from(ConceptRow).where(ConceptRow.course_id == course_id)
        if origin is not None:
            stmt = stmt.where(ConceptRow.origin == origin.value)
        return int(self.session.scalar(stmt) or 0)


class TopicRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_course(self, course_id: str, scope: TextOwnerType) -> None:
        self.session.execute(
            delete(TopicRow).where(TopicRow.course_id == course_id, TopicRow.scope == scope.value)
        )
        self.session.flush()

    def add_many(self, topics: list[TopicRecord], *, created_at: datetime) -> None:
        for topic in topics:
            self.session.add(mappers.topic_to_row(topic, created_at))
            self.session.flush()
            for term in topic.terms:
                self.session.merge(mappers.topic_term_to_row(topic.topic_id, term))
            for member in topic.members:
                self.session.merge(mappers.topic_member_to_row(topic.topic_id, member))
        self.session.flush()

    def list_by_course(
        self, course_id: str, scope: TextOwnerType | None = None
    ) -> list[TopicRecord]:
        stmt = select(TopicRow).where(TopicRow.course_id == course_id)
        if scope is not None:
            stmt = stmt.where(TopicRow.scope == scope.value)
        rows = self.session.scalars(stmt.order_by(TopicRow.topic_id)).all()
        topics: list[TopicRecord] = []
        for row in rows:
            terms = list(
                self.session.scalars(
                    select(TopicTermRow).where(TopicTermRow.topic_id == row.topic_id)
                ).all()
            )
            members = list(
                self.session.scalars(
                    select(TopicMemberRow).where(TopicMemberRow.topic_id == row.topic_id)
                ).all()
            )
            created = mappers.parse_dt(row.created_at) or datetime.now().astimezone()
            topics.append(mappers.row_to_topic(row, terms, members, created))
        return topics

    def count_by_course(self, course_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(TopicRow).where(TopicRow.course_id == course_id)
            )
            or 0
        )

    def delete_for_course(self, course_id: str) -> None:
        self.session.execute(delete(TopicRow).where(TopicRow.course_id == course_id))
        self.session.flush()


class NlpRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, report: NlpRunReport) -> None:
        row = self.session.get(NlpRunRow, report.run_id)
        new_row = mappers.run_to_row(report)
        if row is not None:
            self.session.delete(row)
            self.session.flush()
        self.session.add(new_row)
        self.session.flush()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(NlpRunRow).order_by(NlpRunRow.started_at.desc()).limit(limit)
        ).all()
        return [
            {
                "run_id": r.run_id,
                "course_id": r.course_id,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "totals": mappers.loads(r.totals_json, {}),
                "model_ids": mappers.loads(r.model_ids_json, []),
                "duration_ms": r.duration_ms,
            }
            for r in rows
        ]

    def get(self, run_id: str) -> NlpRunReport | None:
        row = self.session.get(NlpRunRow, run_id)
        return mappers.row_to_run(row) if row else None

    def latest_for_course(self, course_id: str) -> NlpRunReport | None:
        """Latest attempt, including failures, so status never hides a bad run."""
        row = self.session.scalars(
            select(NlpRunRow)
            .where(NlpRunRow.course_id == course_id)
            .order_by(NlpRunRow.started_at.desc())
        ).first()
        return mappers.row_to_run(row) if row else None


__all__ = [
    "ConceptRepository",
    "EmbeddingRepository",
    "EntityRepository",
    "KeyphraseRepository",
    "NlpRunRepository",
    "ProcessedTextRepository",
    "SectionRepository",
    "TopicRepository",
    "VectorStoreRepository",
]
