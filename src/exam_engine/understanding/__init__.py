"""Phase 3 question understanding: structure, mapping and classification."""

from exam_engine.understanding.mapping import (
    MappingResult,
    MappingSettings,
    SemanticIndex,
    map_question,
    unit_sources,
)
from exam_engine.understanding.service import UnderstandingService
from exam_engine.understanding.similarity import (
    ReasoningProfile,
    SimilaritySettings,
    find_similar_questions,
)
from exam_engine.understanding.taxonomy import DEFAULT_TAXONOMY, Taxonomy, load_taxonomy

__all__ = [
    "DEFAULT_TAXONOMY",
    "MappingResult",
    "MappingSettings",
    "ReasoningProfile",
    "SemanticIndex",
    "SimilaritySettings",
    "Taxonomy",
    "UnderstandingService",
    "find_similar_questions",
    "load_taxonomy",
    "map_question",
    "unit_sources",
]
