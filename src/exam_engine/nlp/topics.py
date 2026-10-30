"""Initial topic discovery.

The default baseline is TF-IDF → truncated SVD → k-means over the reduced
space, with topic terms read back from the cluster centroid in the TF-IDF
space. It is deterministic (fixed random seed), needs no downloads and runs fast
on a course-sized corpus.

Small corpora are the common case for this project and are handled explicitly:
below three documents, or with a vocabulary too small for clustering, the modeler
refuses to emit topics and says why, because a "topic" fitted on two documents is
not a discovery.
"""

from __future__ import annotations

import contextlib
import io
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import joblib
import numpy as np

from exam_engine.models.enums import NlpMethod, NlpStatus, Severity, TextOwnerType
from exam_engine.models.nlp import (
    TopicMember,
    TopicRecord,
    TopicTerm,
    make_topic_id,
)
from exam_engine.models.question import Uncertainty
from exam_engine.nlp.keywords import analysis_tokens
from exam_engine.nlp.vectors import cosine

MIN_DOCUMENTS = 3
MIN_TERMS = 5

_PROBE_CACHED = False


def _quietly_cache_physical_cores() -> None:
    """Cache joblib's physical-core probe without its Windows noise.

    ``KMeans`` asks sklearn for an OpenMP thread count, which asks joblib for
    the number of physical cores. Where ``wmic`` is unavailable (Windows 11
    24H2 and later) that probe fails and joblib prints the exception traceback
    to stderr, ignoring warning filters. One quiet call fills the process-wide
    cache, so later fits never probe again.
    """
    global _PROBE_CACHED
    if _PROBE_CACHED:
        return
    _PROBE_CACHED = True
    try:
        with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
            warnings.simplefilter("ignore")
            joblib.cpu_count(only_physical_cores=True)
    except Exception:  # pragma: no cover - the probe only affects thread counts
        pass


@dataclass(frozen=True)
class TopicDocument:
    owner_type: TextOwnerType
    owner_id: str
    text: str
    document_id: str | None = None


@dataclass
class TopicModelResult:
    topics: list[TopicRecord] = field(default_factory=list)
    status: NlpStatus = NlpStatus.OK
    params: dict[str, Any] = field(default_factory=dict)
    warnings: list[Uncertainty] = field(default_factory=list)


class TopicModeler(Protocol):
    name: NlpMethod

    def fit(
        self,
        documents: Sequence[TopicDocument],
        *,
        course_id: str,
        scope: TextOwnerType,
        scope_id: str | None = None,
        k: int = 5,
    ) -> TopicModelResult: ...


def _insufficient(message: str, detail: dict[str, Any]) -> TopicModelResult:
    return TopicModelResult(
        topics=[],
        status=NlpStatus.INSUFFICIENT_DATA,
        params=detail,
        warnings=[
            Uncertainty(
                code="insufficient_data",
                message=message,
                severity=Severity.WARNING,
                detail=detail,
            )
        ],
    )


class LsaKMeansTopicModeler:
    """TF-IDF + truncated SVD + k-means over documents."""

    name = NlpMethod.KMEANS

    def __init__(self, *, terms_per_topic: int = 8, max_components: int = 50) -> None:
        self.terms_per_topic = max(1, terms_per_topic)
        self.max_components = max(2, max_components)

    def fit(
        self,
        documents: Sequence[TopicDocument],
        *,
        course_id: str,
        scope: TextOwnerType,
        scope_id: str | None = None,
        k: int = 5,
    ) -> TopicModelResult:
        texts = [doc for doc in documents if doc.text.strip()]
        requested_k = max(1, k)
        params: dict[str, Any] = {
            "k_requested": requested_k,
            "n_documents": len(texts),
            "min_documents": MIN_DOCUMENTS,
            "random_state": 42,
        }
        if len(texts) < MIN_DOCUMENTS:
            return _insufficient(
                f"topic discovery needs at least {MIN_DOCUMENTS} non-empty documents; "
                f"found {len(texts)}",
                params,
            )

        try:
            from sklearn.cluster import KMeans
            from sklearn.decomposition import TruncatedSVD
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError as exc:  # pragma: no cover - hard dependency
            return _insufficient(f"scikit-learn is unavailable: {exc}", params)

        token_lists = [analysis_tokens(doc.text, lemmatize_terms=True) for doc in texts]
        unique_texts = len({doc.text.strip() for doc in texts})
        params["unique_documents"] = unique_texts
        if unique_texts < 2:
            return _insufficient(
                "every document has identical text; there is no structure to discover",
                params,
            )
        vectorizer = TfidfVectorizer(
            analyzer=lambda raw: raw,
            preprocessor=None,
            tokenizer=None,
            lowercase=False,
            min_df=1,
            sublinear_tf=True,
        )
        matrix = vectorizer.fit_transform(token_lists)
        terms = list(vectorizer.get_feature_names_out())
        params["n_features"] = len(terms)
        if len(terms) < MIN_TERMS:
            return _insufficient(
                f"topic discovery needs at least {MIN_TERMS} distinct terms; found {len(terms)}",
                params,
            )

        # Two documents per cluster is the minimum that keeps a cluster from
        # being a single outlier; below that the guard reports insufficient data.
        effective_k = min(requested_k, unique_texts, max(1, len(texts) // 2))
        params["k_used"] = effective_k
        notes: list[Uncertainty] = []
        if effective_k < requested_k:
            notes.append(
                Uncertainty(
                    code="insufficient_data",
                    message=(
                        f"requested {requested_k} topics but only {len(texts)} documents; "
                        f"used {effective_k} to avoid empty or unstable clusters"
                    ),
                    severity=Severity.WARNING,
                    detail={"requested": requested_k, "used": effective_k},
                )
            )

        components = min(self.max_components, len(terms) - 1, len(texts) - 1)
        if components < 2:
            return _insufficient("corpus cannot support a two-dimensional projection", params)
        svd = TruncatedSVD(n_components=components, random_state=42)
        reduced = svd.fit_transform(matrix)
        params["components"] = components
        params["variance_explained"] = round(float(svd.explained_variance_ratio_.sum()), 6)

        _quietly_cache_physical_cores()
        model = KMeans(n_clusters=effective_k, n_init=10, random_state=42)
        labels = model.fit_predict(reduced)

        dense = matrix.toarray()
        topics: list[TopicRecord] = []
        sizes = np.bincount(labels, minlength=effective_k)
        for cluster_index in range(effective_k):
            members_index = np.where(labels == cluster_index)[0]
            if members_index.size == 0:
                notes.append(
                    Uncertainty(
                        code="insufficient_data",
                        message=f"cluster {cluster_index} received no documents and was dropped",
                        severity=Severity.WARNING,
                    )
                )
                continue
            centroid = dense[members_index].mean(axis=0)
            top_indices = np.argsort(-centroid)[: self.terms_per_topic]
            term_records = [
                TopicTerm(
                    term=terms[int(index)], weight=round(float(centroid[index]), 6), rank=rank
                )
                for rank, index in enumerate(top_indices, start=1)
                if centroid[index] > 0
            ]
            if not term_records:
                continue
            members = [
                TopicMember(
                    owner_type=texts[int(position)].owner_type,
                    owner_id=texts[int(position)].owner_id,
                    document_id=texts[int(position)].document_id,
                    weight=round(cosine(dense[int(position)], centroid), 6),
                )
                for position in members_index
            ]
            label = " / ".join(term.term for term in term_records[:3])
            topics.append(
                TopicRecord(
                    topic_id=make_topic_id(course_id, scope.value, scope_id, cluster_index),
                    course_id=course_id,
                    scope=scope,
                    scope_id=scope_id,
                    method=self.name,
                    k=effective_k,
                    label=label,
                    terms=term_records,
                    members=members,
                    size=int(members_index.size),
                    metadata={"cluster_index": cluster_index, "params": params},
                )
            )

        if len(topics) <= 1:
            notes.append(
                Uncertainty(
                    code="insufficient_data",
                    message=(
                        "the corpus collapsed into a single topic; treat this as "
                        "'no structure found', not as a discovery"
                    ),
                    severity=Severity.WARNING,
                )
            )
        unused = [index for index in range(effective_k) if sizes[index] == 0]
        params["empty_clusters"] = len(unused)

        return TopicModelResult(
            topics=topics,
            status=NlpStatus.OK if topics else NlpStatus.INSUFFICIENT_DATA,
            params=params,
            warnings=notes,
        )


TOPIC_MODELERS: dict[str, TopicModeler] = {"kmeans": LsaKMeansTopicModeler()}


def get_topic_modeler(name: str = "kmeans") -> TopicModeler:
    try:
        return TOPIC_MODELERS[name.casefold()]
    except KeyError as exc:
        raise KeyError(
            f"unknown topic modeler '{name}'; available: {', '.join(sorted(TOPIC_MODELERS))}"
        ) from exc


__all__ = [
    "LsaKMeansTopicModeler",
    "MIN_DOCUMENTS",
    "MIN_TERMS",
    "TOPIC_MODELERS",
    "TopicDocument",
    "TopicModelResult",
    "TopicModeler",
    "get_topic_modeler",
]
