"""Phase 11 advanced assessment intelligence.

Three independent capabilities that share Phase 10's storage and validation:

* :mod:`exam_engine.advanced.mutation` - derive variants of an existing question
  while declaring which properties the strategy can keep invariant;
* :mod:`exam_engine.advanced.adversarial` - write challenging but fair questions
  from real course passages, each with a rubric;
* :mod:`exam_engine.advanced.misconception` - read stored answers against their
  reference material and report candidate patterns with their evidence.

:class:`AdvancedAssessmentService` wires them to the database, Phase 10 validation
and the review workflow. Nothing here approves a question or a misconception on
its own, and no capability depends on another succeeding.
"""

from __future__ import annotations

from exam_engine.advanced.service import (
    ALGORITHM,
    VERSION,
    AdvancedAssessmentService,
    ImportSummary,
    SourceBundle,
)

__all__ = [
    "ALGORITHM",
    "VERSION",
    "AdvancedAssessmentService",
    "ImportSummary",
    "SourceBundle",
]
