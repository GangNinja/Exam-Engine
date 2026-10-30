"""Phase 2 NLP foundation: preprocessing, keyphrases, embeddings, concepts, topics.

Everything here is derived data over the Phase 1 tables. The entry point for
orchestration is :class:`exam_engine.nlp.service.NlpService`; individual
components are importable on their own and can be swapped out.
"""

from exam_engine.nlp.embeddings import (
    EmbeddingUnavailable,
    HashingTfidfProvider,
    LsaProvider,
    SentenceTransformerProvider,
    provider_descriptors,
    resolve_provider,
)
from exam_engine.nlp.entities import (
    RawEntity,
    RuleBasedEntityExtractor,
    SpacyEntityExtractor,
    build_extractor,
)
from exam_engine.nlp.keywords import (
    HybridKeywordExtractor,
    KeywordDocument,
    RakeKeywordExtractor,
    TfidfKeywordExtractor,
    get_extractor,
)
from exam_engine.nlp.preprocess import (
    Preprocessor,
    SectionChunk,
    split_section_chunks,
    split_sections,
)
from exam_engine.nlp.service import NlpService
from exam_engine.nlp.text import (
    detect_language,
    lemmatize,
    normalize_for_analysis,
    segment_sentences,
    tokenize,
)
from exam_engine.nlp.topics import LsaKMeansTopicModeler, TopicDocument, get_topic_modeler
from exam_engine.nlp.vectors import EmbeddingService, cosine

__all__ = [
    "EmbeddingService",
    "EmbeddingUnavailable",
    "HashingTfidfProvider",
    "HybridKeywordExtractor",
    "KeywordDocument",
    "LsaKMeansTopicModeler",
    "LsaProvider",
    "NlpService",
    "Preprocessor",
    "RakeKeywordExtractor",
    "RawEntity",
    "RuleBasedEntityExtractor",
    "SectionChunk",
    "SentenceTransformerProvider",
    "SpacyEntityExtractor",
    "TfidfKeywordExtractor",
    "TopicDocument",
    "build_extractor",
    "cosine",
    "detect_language",
    "get_extractor",
    "get_topic_modeler",
    "lemmatize",
    "normalize_for_analysis",
    "provider_descriptors",
    "resolve_provider",
    "segment_sentences",
    "split_section_chunks",
    "split_sections",
    "tokenize",
]
