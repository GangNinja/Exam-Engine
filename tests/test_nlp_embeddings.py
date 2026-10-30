from __future__ import annotations

import numpy as np

from exam_engine.models.enums import EmbeddingScope
from exam_engine.nlp.embeddings import (
    EmbeddingUnavailable,
    HashingTfidfProvider,
    LsaProvider,
    SentenceTransformerProvider,
    provider_descriptors,
    resolve_provider,
)
from exam_engine.nlp.vectors import EmbeddingService, cosine, text_sha256
from exam_engine.storage.nlp_repositories import VectorStoreRepository

CORPUS = [
    "Binary search trees keep keys ordered and support logarithmic lookup.",
    "Graph traversal visits vertices and edges using breadth first search.",
    "Sorting algorithms include quicksort, merge sort and heapsort variants.",
    "Dynamic programming solves overlapping subproblems with memoisation.",
    "Hash tables map keys to buckets and handle collisions by chaining.",
    "Operating systems schedule processes and manage virtual memory pages.",
    "Relational algebra defines selection, projection and join operators.",
    "Finite automata recognise regular languages with states and transitions.",
]


def test_hashing_provider_shape_and_normalisation() -> None:
    provider = HashingTfidfProvider(dim=64)
    vectors = provider.embed(["Binary search tree", "Graph traversal algorithm"])
    assert len(vectors) == 2
    assert all(len(vector) == 64 for vector in vectors)
    for vector in vectors:
        assert np.isclose(np.linalg.norm(vector), 1.0, atol=1e-6)


def test_hashing_provider_is_deterministic() -> None:
    provider = HashingTfidfProvider(dim=32)
    first = provider.embed(["Quick sort partitions the array."])[0]
    second = HashingTfidfProvider(dim=32).embed(["Quick sort partitions the array."])[0]
    assert first == second
    other = provider.embed(["Hash tables chain collisions."])[0]
    assert first != other


def test_hashing_provider_handles_empty_text() -> None:
    vectors = HashingTfidfProvider(dim=16).embed([""])
    assert len(vectors) == 1
    assert all(value == 0.0 for value in vectors[0])


def test_lsa_provider_requires_enough_documents() -> None:
    try:
        LsaProvider.fit(["one document only", "second document here"], dim=8)
    except EmbeddingUnavailable as exc:
        assert "at least" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("LSA should refuse a two-document corpus")


def test_lsa_provider_fits_a_course_corpus() -> None:
    provider = LsaProvider.fit(CORPUS, dim=8)
    assert provider.dim >= 3
    assert provider.dim <= 8
    assert "lsa-tfidf-v1-" in provider.model_id
    vectors = provider.embed(CORPUS[:2])
    assert len(vectors[0]) == provider.dim
    assert np.isclose(np.linalg.norm(vectors[0]), 1.0, atol=1e-6)


def test_lsa_model_id_tracks_the_corpus() -> None:
    first = LsaProvider.fit(CORPUS, dim=6)
    second = LsaProvider.fit(CORPUS + ["New document about compilers and parsing."], dim=6)
    assert first.model_id != second.model_id


def test_sentence_transformer_provider_is_optional_and_offline_safe() -> None:
    provider = SentenceTransformerProvider(allow_download=False)
    assert provider.scope is EmbeddingScope.PRETRAINED
    assert isinstance(provider.available(), bool)
    if not provider.available():
        try:
            provider.embed(["text"])
        except EmbeddingUnavailable as exc:
            assert "install" in str(exc) or "load" in str(exc)
        else:  # pragma: no cover - environment dependent
            raise AssertionError("unavailable provider must not silently work")


def test_resolve_auto_falls_back_to_hashing_for_tiny_corpora() -> None:
    choice = resolve_provider("auto", corpus=["only one text"], allow_download=False)
    assert choice.provider.model_id.startswith("hashing-tfidf")
    assert choice.provider.scope is EmbeddingScope.STATELESS


def test_resolve_lsa_with_small_corpus_warns_and_falls_back() -> None:
    choice = resolve_provider("lsa", corpus=["a", "b"], lsa_dim=8)
    assert choice.provider.model_id.startswith("hashing-tfidf")
    assert any("lsa unavailable" in warning for warning in choice.warnings)


def test_resolve_unknown_provider_raises() -> None:
    try:
        resolve_provider("magic", corpus=CORPUS)
    except KeyError as exc:
        assert "magic" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown provider must raise")


def test_provider_descriptors_are_machine_readable() -> None:
    names = {descriptor["name"] for descriptor in provider_descriptors()}
    assert {"auto", "hashing", "lsa", "sentence-transformers"} <= names


class CountingProvider:
    """Deterministic stub used to observe cache behaviour."""

    model_id = "stub-v1"
    dim = 4
    scope = EmbeddingScope.STATELESS

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(len(text) % 7), 1.0, 0.0, 0.5] for text in texts]

    def metadata(self):
        return {"provider": "stub"}


class FailingProvider(CountingProvider):
    model_id = "failing-v1"

    def embed(self, texts):
        raise EmbeddingUnavailable("simulated model failure")


def test_embedding_cache_avoids_recomputation(database) -> None:
    provider = CountingProvider()
    with database.transaction() as session:
        service = EmbeddingService(provider, VectorStoreRepository(session))
        first = service.embed_texts(["alpha text", "beta text"])
        assert first.computed == 2
        assert first.cached == 0
        assert len(provider.calls) == 1

        second = service.embed_texts(["alpha text", "beta text"])
        assert second.computed == 0
        assert second.cached == 2
        assert len(provider.calls) == 1


def test_embedding_cache_invalidates_when_text_changes(database) -> None:
    provider = CountingProvider()
    with database.transaction() as session:
        service = EmbeddingService(provider, VectorStoreRepository(session))
        service.embed_texts(["alpha text"])
        result = service.embed_texts(["alpha text", "gamma text"])
        assert result.cached == 1
        assert result.computed == 1
        assert text_sha256("gamma text") in result.vectors


def test_provider_failure_is_reported_not_raised(database) -> None:
    with database.transaction() as session:
        service = EmbeddingService(FailingProvider(), VectorStoreRepository(session))
        result = service.embed_texts(["alpha text"])
    assert result.ok is False
    assert result.error and "simulated model failure" in result.error
    assert result.vectors == {}


def test_cosine_similarity_behaviour() -> None:
    assert np.isclose(cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
    assert np.isclose(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine([1.0], [1.0, 0.0]) == 0.0
