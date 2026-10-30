"""Phase 10 question generation.

The package keeps four seams separate so each can be tested and replaced:

``templates``
    Deterministic, grounded task construction.
``providers``
    The provider interface, the offline template provider and an opt-in
    OpenAI-compatible provider.
``validation``
    Independent automated checks over an assembled question.
``dedupe``
    Duplicate/variant detection built on Phase 3 similarity.

:class:`~exam_engine.generation.service.QuestionGenerationService` wires them to
stored Phase 1-9 data.
"""

from exam_engine.generation.providers import (
    GenerationProvider,
    OpenAICompatibleProvider,
    ProviderError,
    TemplateProvider,
    build_provider,
)
from exam_engine.generation.service import GenerationError, QuestionGenerationService

__all__ = [
    "GenerationError",
    "GenerationProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "QuestionGenerationService",
    "TemplateProvider",
    "build_provider",
]
