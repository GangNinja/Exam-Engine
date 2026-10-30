"""Provider-independent embeddings.

Three providers ship with the project:

``hashing`` — stateless signed feature hashing over word uni/bi-grams. Always
available, no download, deterministic across machines. Lexical, not deep
semantics; it exists so the pipeline works offline and in tests.

``lsa`` — TF-IDF + truncated SVD fitted on the course corpus. Captures
co-occurrence structure, still no download. The model id embeds a corpus
fingerprint, so adding or editing a document produces a new model id and stale
vectors cannot be reused.

``sentence-transformers`` — optional extra (``pip install -e ".[neural]"``) plus
a model download (all-MiniLM-L6-v2 is ≈90 MB). Never downloaded automatically
unless ``EXAM_ENGINE_ALLOW_MODEL_DOWNLOAD=1`` is set.

Every provider is resolved lazily and failures degrade explicitly instead of
crashing an analysis run.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from exam_engine.models.enums import EmbeddingScope
from exam_engine.nlp.keywords import analysis_tokens

DEFAULT_ST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# LSA needs enough documents and vocabulary for the projection to mean anything.
MIN_LSA_DOCUMENTS = 5
MIN_LSA_FEATURES = 20


class EmbeddingUnavailable(RuntimeError):
    """Raised when a provider cannot run (missing dependency, model or corpus)."""


class EmbeddingProvider(Protocol):
    model_id: str
    dim: int
    scope: EmbeddingScope

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts; must return one vector per input, in the same order."""
        ...

    def metadata(self) -> dict[str, Any]:
        """Model identity and requirements, persisted with the analysis run."""
        ...


def _l2_normalize(values: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return values / norms


def corpus_fingerprint(texts: Sequence[str]) -> str:
    digests = sorted(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts)
    return hashlib.sha256("\x1f".join(digests).encode()).hexdigest()


# --- hashing provider --------------------------------------------------------


class HashingTfidfProvider:
    """Signed hashing of sublinear term frequencies (word 1–2 grams).

    Deterministic, dependency-free and corpus-independent: the same text always
    maps to the same vector, which also makes it safe as a cache key.
    """

    scope = EmbeddingScope.STATELESS

    def __init__(self, *, dim: int = 256) -> None:
        if dim < 8:
            raise ValueError("embedding dimension must be at least 8")
        self.dim = dim
        self.model_id = f"hashing-tfidf-v1-d{dim}"

    def _features(self, text: str) -> Counter[str]:
        tokens = [token.casefold() for token in analysis_tokens(text, lemmatize_terms=True)]
        features: Counter[str] = Counter(tokens)
        features.update(f"{left} {right}" for left, right in zip(tokens, tokens[1:], strict=False))
        return features

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = np.zeros((len(texts), self.dim), dtype=np.float64)
        for row, text in enumerate(texts):
            for feature, count in self._features(text).items():
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vectors[row, index] += sign * (1.0 + math.log(count))
        return _l2_normalize(vectors).astype(np.float64).tolist()

    def metadata(self) -> dict[str, Any]:
        return {
            "provider": "hashing",
            "model_id": self.model_id,
            "dim": self.dim,
            "scope": self.scope.value,
            "requires_download": False,
            "description": "signed feature hashing over word uni/bi-grams (lexical)",
        }


# --- LSA provider ------------------------------------------------------------


class LsaProvider:
    """TF-IDF + truncated SVD fitted on the supplied corpus."""

    scope = EmbeddingScope.CORPUS

    def __init__(self, dim: int, model_id: str, vectorizer: Any, svd: Any) -> None:
        self.dim = dim
        self.model_id = model_id
        self._vectorizer = vectorizer
        self._svd = svd

    @classmethod
    def fit(cls, corpus: Sequence[str], *, dim: int = 128) -> LsaProvider:
        texts = [text for text in corpus if text.strip()]
        if len(texts) < MIN_LSA_DOCUMENTS:
            raise EmbeddingUnavailable(
                f"LSA needs at least {MIN_LSA_DOCUMENTS} non-empty texts to fit; got {len(texts)}"
            )
        try:
            from sklearn.decomposition import TruncatedSVD
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError as exc:  # pragma: no cover - scikit-learn is a hard dependency
            raise EmbeddingUnavailable("scikit-learn is not installed") from exc

        token_lists = [analysis_tokens(text, lemmatize_terms=True) for text in texts]
        vectorizer = TfidfVectorizer(
            analyzer=lambda raw: raw,
            preprocessor=None,
            tokenizer=None,
            lowercase=False,
            min_df=1,
            sublinear_tf=True,
        )
        matrix = vectorizer.fit_transform(token_lists)
        if matrix.shape[1] < MIN_LSA_FEATURES:
            raise EmbeddingUnavailable(
                f"LSA needs at least {MIN_LSA_FEATURES} distinct terms; got {matrix.shape[1]}"
            )
        components = min(dim, matrix.shape[1] - 1, matrix.shape[0] - 1)
        if components < 3:
            raise EmbeddingUnavailable("corpus cannot support a 3-dimensional LSA projection")
        svd = TruncatedSVD(n_components=components, random_state=42)
        svd.fit(matrix)
        fingerprint = corpus_fingerprint(texts)[:12]
        return cls(components, f"lsa-tfidf-v1-{fingerprint}-d{components}", vectorizer, svd)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        token_lists = [analysis_tokens(text, lemmatize_terms=True) for text in texts]
        matrix = self._vectorizer.transform(token_lists)
        projected = self._svd.transform(matrix)
        return _l2_normalize(projected).astype(np.float64).tolist()

    def metadata(self) -> dict[str, Any]:
        return {
            "provider": "lsa",
            "model_id": self.model_id,
            "dim": self.dim,
            "scope": self.scope.value,
            "requires_download": False,
            "description": "TF-IDF + truncated SVD fitted on this course corpus",
        }


# --- optional neural provider ------------------------------------------------


class SentenceTransformerProvider:
    """Optional sentence-transformers provider (local inference, no API calls).

    The model is loaded on first use and is never downloaded unless the caller
    explicitly allows it. Failures surface as :class:`EmbeddingUnavailable` so the
    service can fall back without losing the run.
    """

    scope = EmbeddingScope.PRETRAINED

    def __init__(self, model_name: str = DEFAULT_ST_MODEL, *, allow_download: bool = False) -> None:
        self.model_name = model_name
        self.allow_download = allow_download
        self.model_id = f"st:{model_name}"
        self.dim = 0
        self._model: Any = None

    @staticmethod
    def installed() -> bool:
        return importlib.util.find_spec("sentence_transformers") is not None

    def available(self) -> bool:
        if not self.installed():
            return False
        if self.allow_download:
            return True
        # Without downloads, the model must already be in the local HF cache.
        try:
            from pathlib import Path

            cache_root = Path.home() / ".cache" / "huggingface" / "hub"
            slug = "models--" + self.model_name.replace("/", "--")
            return (cache_root / slug).exists()
        except OSError:  # pragma: no cover - unreadable home directory
            return False

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.installed():
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed; "
                "install the 'neural' extra to use this provider"
            )
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.model_name, local_files_only=not self.allow_download
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise EmbeddingUnavailable(
                f"could not load sentence-transformers model '{self.model_name}': {exc}"
            ) from exc
        self.dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        try:
            vectors = model.encode(
                list(texts),
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise EmbeddingUnavailable(f"sentence-transformers inference failed: {exc}") from exc
        return np.asarray(vectors, dtype=np.float64).tolist()

    def metadata(self) -> dict[str, Any]:
        return {
            "provider": "sentence-transformers",
            "model_id": self.model_id,
            "dim": self.dim,
            "scope": self.scope.value,
            "requires_download": not self.available(),
            "model_size_mb": 90,
            "description": "pretrained sentence embeddings (local inference only)",
        }


# --- resolution --------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingChoice:
    provider: EmbeddingProvider
    warnings: list[str]


def resolve_provider(
    name: str,
    *,
    corpus: Sequence[str],
    hashing_dim: int = 256,
    lsa_dim: int = 128,
    st_model: str = DEFAULT_ST_MODEL,
    allow_download: bool = False,
) -> EmbeddingChoice:
    """Resolve a provider by name, degrading explicitly for ``auto``.

    ``auto`` prefers the neural model only when it is already available, then the
    corpus-fitted LSA projection, then stateless hashing. Each downgrade is
    recorded as a warning so the run report shows which model actually ran.
    """
    requested = (name or "auto").casefold()
    warnings: list[str] = []

    if requested in {"hashing", "hashing-tfidf", "hash"}:
        return EmbeddingChoice(HashingTfidfProvider(dim=hashing_dim), warnings)
    if requested in {"lsa", "svd"}:
        try:
            return EmbeddingChoice(LsaProvider.fit(corpus, dim=lsa_dim), warnings)
        except EmbeddingUnavailable as exc:
            warnings.append(f"lsa unavailable ({exc}); used hashing instead")
            return EmbeddingChoice(HashingTfidfProvider(dim=hashing_dim), warnings)
    if requested in {"sentence-transformers", "st", "neural", "minilm"}:
        provider = SentenceTransformerProvider(st_model, allow_download=allow_download)
        if provider.available():
            return EmbeddingChoice(provider, warnings)
        warnings.append(
            f"sentence-transformers model '{st_model}' is unavailable "
            "(package or cached model missing); used LSA/hashing instead"
        )
        try:
            return EmbeddingChoice(LsaProvider.fit(corpus, dim=lsa_dim), warnings)
        except EmbeddingUnavailable:
            return EmbeddingChoice(HashingTfidfProvider(dim=hashing_dim), warnings)
    if requested != "auto":
        raise KeyError(
            f"unknown embedding provider '{name}'; "
            "available: auto, hashing, lsa, sentence-transformers"
        )

    provider = SentenceTransformerProvider(st_model, allow_download=allow_download)
    if provider.available():
        return EmbeddingChoice(provider, warnings)
    try:
        return EmbeddingChoice(LsaProvider.fit(corpus, dim=lsa_dim), warnings)
    except EmbeddingUnavailable:
        return EmbeddingChoice(HashingTfidfProvider(dim=hashing_dim), warnings)


def provider_descriptors() -> list[dict[str, Any]]:
    """Machine-readable list for ``exam-engine nlp providers``."""
    return [
        {
            "name": "auto",
            "description": "sentence-transformers if cached, else LSA, else hashing",
            "requires_download": False,
        },
        HashingTfidfProvider().metadata() | {"name": "hashing"},
        {
            "name": "lsa",
            "description": "TF-IDF + truncated SVD fitted on the course corpus",
            "requires_download": False,
            "min_corpus": MIN_LSA_DOCUMENTS,
            "min_terms": MIN_LSA_FEATURES,
        },
        {
            "name": "sentence-transformers",
            "description": "optional local neural embeddings",
            "requires_download": True,
            "installed": SentenceTransformerProvider.installed(),
            "model": DEFAULT_ST_MODEL,
            "model_size_mb": 90,
        },
    ]


__all__ = [
    "DEFAULT_ST_MODEL",
    "EmbeddingChoice",
    "EmbeddingProvider",
    "EmbeddingUnavailable",
    "HashingTfidfProvider",
    "LsaProvider",
    "SentenceTransformerProvider",
    "corpus_fingerprint",
    "provider_descriptors",
    "resolve_provider",
]
