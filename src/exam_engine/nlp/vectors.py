"""Cache-aware embedding service and small vector helpers.

The cache is a :class:`~exam_engine.storage.nlp_repositories.VectorStoreRepository`
so a different backend (pgvector, FAISS, a file store) can be substituted without
touching the analysis service. Keys are ``(model_id, sha256(text))``: changing the
text or the model invalidates the entry automatically.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from exam_engine.models.course import utcnow
from exam_engine.models.enums import TextOwnerType
from exam_engine.models.nlp import EmbeddingRecord, make_embedding_id
from exam_engine.nlp.embeddings import EmbeddingUnavailable
from exam_engine.storage.nlp_repositories import VectorStoreRepository


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class EmbeddingBatchResult:
    vectors: dict[str, list[float]] = field(default_factory=dict)
    computed: int = 0
    cached: int = 0
    dim: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class EmbeddingService:
    """Embeds texts through a provider while reusing cached vectors."""

    def __init__(self, provider, store: VectorStoreRepository) -> None:
        self.provider = provider
        self.store = store

    @property
    def model_id(self) -> str:
        return str(self.provider.model_id)

    def embed_texts(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        unique: dict[str, str] = {}
        for text in texts:
            if text.strip():
                unique.setdefault(text_sha256(text), text)
        result = EmbeddingBatchResult(dim=int(getattr(self.provider, "dim", 0) or 0))
        if not unique:
            return result

        missing: dict[str, str] = {}
        for digest, text in unique.items():
            cached = self.store.get(self.model_id, digest)
            if cached is not None:
                result.vectors[digest] = cached
                result.cached += 1
            else:
                missing[digest] = text

        if not missing:
            return result

        try:
            vectors = self.provider.embed(list(missing.values()))
        except EmbeddingUnavailable as exc:
            result.error = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 - provider boundary
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        if len(vectors) != len(missing):
            result.error = f"provider returned {len(vectors)} vectors for {len(missing)} texts"
            return result

        now = utcnow()
        for digest, vector in zip(missing, vectors, strict=True):
            values = [float(value) for value in vector]
            self.store.put(self.model_id, digest, values, created_at=now)
            result.vectors[digest] = values
            result.computed += 1
        result.dim = int(getattr(self.provider, "dim", 0) or len(next(iter(vectors))))
        return result

    def record(
        self,
        *,
        course_id: str,
        owner_type: TextOwnerType,
        owner_id: str,
        text: str,
        document_id: str | None = None,
    ) -> EmbeddingRecord:
        digest = text_sha256(text)
        vector = self.store.get(self.model_id, digest) or []
        return EmbeddingRecord(
            embedding_id=make_embedding_id(owner_type, owner_id, self.model_id),
            course_id=course_id,
            owner_type=owner_type,
            owner_id=owner_id,
            document_id=document_id,
            model_id=self.model_id,
            dim=len(vector),
            text_sha256=digest,
        )

    def lookup(self, text: str) -> list[float] | None:
        return self.store.get(self.model_id, text_sha256(text))


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    a = np.asarray(left, dtype=float)
    b = np.asarray(right, dtype=float)
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(a, b) / denominator)


__all__ = ["EmbeddingBatchResult", "EmbeddingService", "cosine", "text_sha256"]
