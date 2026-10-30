"""Phase 11 error type and source-question view.

One error type keeps CLI exit codes and report codes consistent across the three
capabilities, and one immutable source view lets mutation and validation share the
same facts without either reaching into storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    GenerationFormat,
)
from exam_engine.models.generation import SourcePassage


class AdvancedError(RuntimeError):
    """A request that cannot be honoured, with a stable machine code."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class SourceOption:
    key: str
    text: str
    is_correct: bool


@dataclass(frozen=True)
class SourceQuestion:
    """Everything a mutation may read about the question it derives from."""

    question_id: str
    course_id: str
    syllabus_id: str
    unit_id: str
    unit_label: str
    unit_title: str | None
    concept_ids: tuple[str, ...]
    concept_labels: tuple[str, ...]
    text: str
    format: GenerationFormat
    bloom: BloomLevel
    difficulty: DifficultyLabel
    objective: str
    marks: float
    answer_key: str | None = None
    explanation: str | None = None
    solution_steps: tuple[str, ...] = ()
    options: tuple[SourceOption, ...] = ()
    correct_option_key: str | None = None
    source_references: tuple[SourcePassage, ...] = ()
    question_type: str | None = None

    @property
    def is_multiple_choice(self) -> bool:
        return self.format is GenerationFormat.MULTIPLE_CHOICE

    @property
    def concept_label(self) -> str:
        return self.concept_labels[0] if self.concept_labels else "this concept"


@dataclass(frozen=True)
class EvidenceContext:
    """The grounding a mutation or challenge may use beyond the question itself.

    Every entry is real course material; nothing here is invented for a prompt.
    """

    passages: tuple[SourcePassage, ...] = ()
    sibling_labels: tuple[str, ...] = ()
    sibling_passages: tuple[SourcePassage, ...] = ()
    prerequisite_label: str | None = None
    prerequisite_in_syllabus: bool = False
    related_labels: tuple[str, ...] = ()
    concept_ids: frozenset[str] = frozenset()
    missing: tuple[str, ...] = field(default_factory=tuple)

    def first_passage(self) -> SourcePassage | None:
        return self.passages[0] if self.passages else None

    def sibling_passage(self) -> SourcePassage | None:
        return self.sibling_passages[0] if self.sibling_passages else None

    def has_evidence(self) -> bool:
        return bool(self.passages)
