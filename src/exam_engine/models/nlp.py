"""Validated records produced by the Phase 2 NLP layer.

These models are deliberately independent of the algorithms that produce them:
a preprocessing run, a keyword extractor, an embedding model and a topic model
all speak in these records, so providers can be replaced without touching
storage or the CLI.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from exam_engine.models.course import utcnow
from exam_engine.models.enums import (
    ConceptOrigin,
    NlpMethod,
    NlpStatus,
    TextOwnerType,
)
from exam_engine.models.question import Uncertainty


def _digest(payload: str, prefix: str) -> str:
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# --- preprocessing -----------------------------------------------------------


class Token(BaseModel):
    """One token with its character span in the normalised text."""

    model_config = ConfigDict(extra="forbid")

    text: str
    lemma: str | None = None
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    is_word: bool = True
    is_technical: bool = Field(
        default=False, description="Contains digits, notation or symbols worth keeping."
    )

    @property
    def normalized(self) -> str:
        return self.text.casefold()


class Sentence(BaseModel):
    """A sentence with its span so callers can map back to the source text."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=0)
    text: str
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    token_count: int = Field(default=0, ge=0)


class PreprocessConfig(BaseModel):
    """Normalisation switches used to build :class:`ProcessedText`.

    Term-level choices such as lemmatisation and stop-word removal live in
    ``NlpSettings`` and are applied by the extractors, so changing them does not
    invalidate the stored normalised text.
    """

    model_config = ConfigDict(extra="forbid")

    unicode_form: str = Field(default="NFKC", pattern="^(NFC|NFD|NFKC|NFKD|none)$")
    preserve_notation: bool = True
    min_sentence_chars: int = Field(default=2, ge=0)
    language: str = "en"

    def config_id(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return _digest(payload, "cfg_")


class ProcessedText(BaseModel):
    """Normalised representation of one source text.

    The original text is never replaced: this is a parallel representation with
    its own id, config hash and provenance.
    """

    model_config = ConfigDict(extra="forbid")

    text_id: str
    course_id: str
    owner_type: TextOwnerType
    owner_id: str
    document_id: str | None = None
    raw_sha256: str = Field(..., min_length=64, max_length=64)
    config_id: str
    text: str
    sentences: list[Sentence] = Field(default_factory=list)
    language: str | None = None
    token_count: int = Field(default=0, ge=0)
    char_count: int = Field(default=0, ge=0)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def sentence_count(self) -> int:
        return len(self.sentences)


def make_text_id(owner_type: TextOwnerType, owner_id: str) -> str:
    return _digest(f"{owner_type.value}\x1f{owner_id}", "txt_")


# --- keyphrases --------------------------------------------------------------


class KeyphraseRecord(BaseModel):
    """A ranked term or multiword phrase with its scoring method."""

    model_config = ConfigDict(extra="forbid")

    keyphrase_id: str
    course_id: str
    owner_type: TextOwnerType
    owner_id: str
    document_id: str | None = None
    term: str
    normalized: str
    score: float = Field(..., ge=0.0)
    method: NlpMethod
    rank: int = Field(..., ge=1)
    n_gram: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


def make_keyphrase_id(
    owner_type: TextOwnerType, owner_id: str, method: NlpMethod, normalized: str
) -> str:
    return _digest(f"{owner_type.value}\x1f{owner_id}\x1f{method.value}\x1f{normalized}", "kp_")


# --- entities and concepts ---------------------------------------------------


class EntityRecord(BaseModel):
    """A named entity or technical term candidate found inside a text."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    course_id: str
    document_id: str | None = None
    owner_type: TextOwnerType
    owner_id: str
    text: str = Field(..., min_length=1)
    normalized: str
    label: str = Field(..., description="Extractor label, e.g. ACRONYM, METHOD, PERSON.")
    method: NlpMethod
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    start_char: int = Field(default=0, ge=0)
    end_char: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


def make_entity_id(
    owner_type: TextOwnerType, owner_id: str, label: str, start: int, text: str
) -> str:
    return _digest(f"{owner_type.value}\x1f{owner_id}\x1f{label}\x1f{start}\x1f{text}", "ent_")


class ConceptRecord(BaseModel):
    """A concept candidate or a syllabus-stated concept.

    ``origin=SYLLABUS`` means the term came from a parsed syllabus and is
    treated as verified; ``origin=INFERRED`` means a statistical or heuristic
    extractor suggested it and a human still has to confirm it.
    """

    model_config = ConfigDict(extra="forbid")

    concept_id: str
    course_id: str
    term: str = Field(..., min_length=1)
    normalized: str
    origin: ConceptOrigin
    method: NlpMethod
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified: bool = False
    unit_id: str | None = None
    evidence: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


def make_concept_id(course_id: str, origin: ConceptOrigin, normalized: str) -> str:
    return _digest(f"{course_id}\x1f{origin.value}\x1f{normalized}", "con_")


# --- topics ------------------------------------------------------------------


class TopicTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    weight: float = Field(..., ge=0.0)
    rank: int = Field(..., ge=1)


class TopicMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_type: TextOwnerType
    owner_id: str
    document_id: str | None = None
    weight: float = Field(default=0.0, ge=0.0)


class TopicRecord(BaseModel):
    """One discovered topic: interpretable terms plus its member documents."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    course_id: str
    scope: TextOwnerType
    scope_id: str | None = None
    method: NlpMethod
    k: int = Field(..., ge=1)
    label: str
    terms: list[TopicTerm] = Field(default_factory=list)
    members: list[TopicMember] = Field(default_factory=list)
    size: int = Field(default=0, ge=0)
    warnings: list[Uncertainty] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


def make_topic_id(course_id: str, scope: str, scope_id: str | None, index: int) -> str:
    return _digest(f"{course_id}\x1f{scope}\x1f{scope_id or ''}\x1f{index}", "top_")


# --- embeddings --------------------------------------------------------------


class EmbeddingRecord(BaseModel):
    """Reference to a cached vector; the bytes live in the vector store."""

    model_config = ConfigDict(extra="forbid")

    embedding_id: str
    course_id: str
    owner_type: TextOwnerType
    owner_id: str
    document_id: str | None = None
    model_id: str
    dim: int = Field(..., ge=1)
    text_sha256: str = Field(..., min_length=64, max_length=64)
    created_at: datetime = Field(default_factory=utcnow)


def make_embedding_id(owner_type: TextOwnerType, owner_id: str, model_id: str) -> str:
    return _digest(f"{owner_type.value}\x1f{owner_id}\x1f{model_id}", "emb_")


# --- runs --------------------------------------------------------------------


class NlpRunReport(BaseModel):
    """Accounting for one analysis run so results stay reproducible."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    course_id: str | None = None
    status: NlpStatus = NlpStatus.OK
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    model_ids: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Uncertainty] = Field(default_factory=list)
    errors: list[Uncertainty] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)

    @property
    def ok(self) -> bool:
        return self.status is not NlpStatus.FAILED

    @property
    def insufficient_data(self) -> bool:
        return self.status is NlpStatus.INSUFFICIENT_DATA
