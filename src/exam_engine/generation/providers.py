"""Question-generation providers.

Two providers ship with Phase 10:

``template``
    Deterministic and offline. It wraps syllabus concepts, units and quoted
    source passages in a fixed task skeleton. It never invents facts and only
    produces question formats it can ground, so some formats (numerical
    problems) and some cognitive levels are reported as unsupported.

``openai``
    An OpenAI-compatible chat-completions endpoint, opt-in through environment
    variables. Responses are parsed into a strict :class:`QuestionDraft` schema;
    malformed output, timeouts, rate limits and provider errors become typed
    failures instead of silent partial questions.

Credentials are read from the environment and never stored in a report, log,
question record or batch configuration.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from exam_engine.config import GenerationSettings
from exam_engine.generation.templates import (
    DEFAULT_MARKS,
    MODEL_ONLY_FORMATS,
    Template,
    needs_of,
    template_for,
    templates_for,
)
from exam_engine.models.enums import BloomLevel, GenerationFormat
from exam_engine.models.generation import (
    GeneratedOption,
    GenerationPrompt,
    QuestionDraft,
    SourcePassage,
)

#: A case-based task needs a paragraph, not a fragment, before it is answerable.
CASE_MIN_EXCERPT_CHARS = 160

#: Default endpoint when a key and model are configured but no base URL is given.
DEFAULT_BASE_URL = "https://api.openai.com/v1"

_JSON_BLOCK_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_OPTION_KEYS = "ABCDEFGH"


class ProviderError(RuntimeError):
    """Typed provider or capability failure with a stable machine code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class GenerationProvider(Protocol):
    """The interface the generation service depends on."""

    @property
    def name(self) -> str:
        """Stable provider key recorded on every batch."""

    @property
    def model(self) -> str | None:
        """Model identifier, or ``None`` for the offline template provider."""

    def supports(self, fmt: GenerationFormat, bloom: BloomLevel) -> bool:
        """Whether this provider can attempt the requested format and level."""

    def capability_note(self, fmt: GenerationFormat, bloom: BloomLevel) -> str:
        """Explain why an unsupported combination was refused."""

    def describe(self) -> dict[str, Any]:
        """Provider metadata safe to persist; never includes credentials."""

    def generate(self, prompt: GenerationPrompt) -> QuestionDraft:
        """Produce one draft or raise :class:`ProviderError`."""


def _mentions(text: str, label: str) -> bool:
    """Word-boundary, case-insensitive check for a concept label inside a statement."""

    phrase = " ".join(label.split())
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None


def _short(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _normalised_label(text: str) -> str:
    return " ".join(text.casefold().strip(" .,;:").split())


def _is_label_only(excerpt: str, label: str) -> bool:
    """Whether a passage is just the syllabus topic rather than an explanation."""

    actual = _normalised_label(excerpt)
    expected = _normalised_label(label)
    if not actual or not expected:
        return False
    return actual == expected or (expected in actual and len(actual) <= len(expected) * 1.35 + 20)


def _answer_aspects(label: str) -> list[str]:
    """Split a syllabus topic into checkable parts for a reference outline."""

    parts: list[str] = []
    for raw in re.split(r";|,", label):
        part = " ".join(raw.split()).strip(" .,;:")
        if len(part) < 3:
            continue
        if parts and len(part.split()) < 3:
            parts[-1] = f"{parts[-1]}, {part}"
        elif part.casefold() not in {item.casefold() for item in parts}:
            parts.append(part)
    return parts or [label.strip(" .,;:")]


_UNIT_REFERENCE_RE = re.compile(
    r"\bUnit\s+(?:Marks\s+)?(?:[0-9]+|[IVXLCDM]+)\b(?:\s*\([^)]*\))?\s*[:.\-\u2013\u2014]?\s*",
    re.IGNORECASE,
)


def _clean_source_text(text: str) -> str:
    """Clean extraction artefacts and learner-irrelevant unit headings."""
    cleaned = "".join(
        " "
        if ord(character) == 0xFFFD
        or 0xE000 <= ord(character) <= 0xF8FF
        or ord(character) < 32
        else character
        for character in text
    )
    cleaned = _UNIT_REFERENCE_RE.sub(" ", cleaned)
    return " ".join(cleaned.split()).strip(" .,;:…").strip(" .,;:…")


def _answer_points(label: str, excerpt: str) -> list[str]:
    """Extract concrete neighbouring topics when the source is a topic list."""
    # A single explanatory sentence should be quoted or paraphrased, not split into
    # a pseudo-list of unrelated fragments.  Semicolon-separated syllabus rows are
    # the case where neighbouring topics genuinely help a learner.
    if excerpt.count(";") < 2:
        return []
    pieces = re.split(r";|\n", excerpt)
    points: list[str] = []
    expected = _normalised_label(label)
    for raw in pieces:
        point = _clean_source_text(raw)
        point = re.sub(r"^(?:and|or)\s+", "", point, flags=re.IGNORECASE)
        if len(point) < 4:
            continue
        lowered = point.casefold()
        if any(
            marker in lowered
            for marker in ("page ", "marks", "cbseacademic", "pdf", "total:")
        ):
            continue
        normalised_point = _normalised_label(point)
        if normalised_point == expected:
            continue
        # Drop a repeated label at the start while keeping its explanatory tail:
        # "Aerothermodynamics ...: thrust, efficiency" becomes "thrust, efficiency".
        if expected and normalised_point.startswith(expected):
            point = point[len(label) :].lstrip(" :.\u2013\u2014(\"'-").strip()
            normalised_point = _normalised_label(point)
            if len(point) < 4 or normalised_point == expected:
                continue
        elif expected and (
            expected in normalised_point
            or (normalised_point in expected and len(normalised_point.split()) >= 3)
        ):
            # A phrase that merely repeats part of the topic label adds no detail.
            continue
        if re.search(r"\b\d+\b", point) and len(point.split()) <= 5:
            # Marks/page/unit rows from a syllabus table are not answer ideas.
            continue
        if normalised_point in {_normalised_label(item) for item in points}:
            continue
        points.append(point)
    return points[:5]


def _aspect_summary(aspects: Sequence[str]) -> str:
    """A readable, bounded phrase for the evidence points in a topic label."""
    unique = [item for item in aspects if item]
    shown = unique[:6]
    summary = "; ".join(shown)
    if len(unique) > len(shown):
        summary += "; …"
    return summary


def _answer_steps(label: str, aspects: Sequence[str]) -> list[str]:
    """A short, useful answer outline instead of one word per label fragment."""
    summary = _aspect_summary(aspects)
    if not summary or _normalised_label(summary) == _normalised_label(label):
        middle = "Explain the key ideas behind it and how they relate."
    else:
        middle = f"Explain the key ideas: {summary}."
    return [
        f"Define {label} in one clear sentence.",
        middle,
        "Connect the ideas and support the answer with one course-grounded example.",
    ]


def _answer_quote(label: str, excerpt: str) -> str | None:
    """A short, relevant verbatim anchor for the answer key, if one exists."""
    cleaned = _clean_source_text(excerpt)
    cleaned_label = _clean_source_text(label)
    if len(cleaned_label) >= 12 and re.search(
        rf"(?<!\w){re.escape(cleaned_label)}(?!\w)", cleaned, re.IGNORECASE
    ):
        return f'"{cleaned_label}"'
    # A topic-list passage is still relevant even when the label is inflected.
    # A single unrelated sentence is not: saying "Syllabus entry: ..." next to a
    # different sentence makes the answer look wrong, so omit the anchor instead.
    if excerpt.count(";") >= 2:
        anchor = cleaned[:120].rsplit(" ", 1)[0].rstrip() if len(cleaned) > 120 else cleaned
        if len(anchor) >= 12:
            return f'"{anchor}"'
    return None


def _excerpt_answer(
    label: str,
    excerpt: str,
    aspects: Sequence[str],
    *,
    label_only: bool,
) -> str:
    """A readable answer built only from the stored source passage."""
    points = _answer_points(label, excerpt)
    summary = _aspect_summary(points)
    quote = _answer_quote(label, excerpt)
    source_note = f" Syllabus entry: {quote}." if quote else ""
    if label_only:
        coverage = f" A complete answer should cover: {summary}." if summary else (
            " A complete answer should define the topic and add detail from the course notes."
        )
        return (
            f"The course material names {label} as a syllabus topic but does not "
            f"provide a definition.{coverage}{source_note}"
        )
    if points and summary:
        return (
            f"The course material groups {label} with these syllabus ideas: {summary}. "
            "A complete answer should define the topic, explain how these ideas relate, "
            f"and support the explanation with one example.{source_note}"
        )
    return (
        f"The course material lists {label} as a syllabus topic. A complete answer "
        "should define the topic and explain its role using the course notes."
        f"{source_note}"
    )


def _outline_answer(label: str, excerpt: str, aspects: Sequence[str]) -> str:
    """A transparent outline for open-ended tasks, with its grounding evidence."""
    points = _answer_points(label, excerpt)
    summary = _aspect_summary(points)
    focus = summary or "the ideas shown in the course material"
    quote = _answer_quote(label, excerpt)
    source_note = f" The syllabus entry is {quote}." if quote else ""
    return (
        f"Reference outline for {label}.{source_note} Start with a clear definition, "
        f"then explain {focus}. Connect the ideas and close with one course-grounded "
        "example. This is a review outline, not a single model answer."
    )


#: Placeholder put where the tested concept's name was removed from a stem.
MASK = "_____"

#: A masked passage must leave at least this many words for the item to be answerable.
MASK_MIN_REMAINDER_WORDS = 4

#: A list marker plus a section heading that masking itself consumed.
_HEADING_PREFIX_RE = re.compile(
    rf"^\s*(?:\d+[.)]|[-*\u2022])\s*{re.escape(MASK)}[\s:;,.-]*(?=[A-Za-z0-9])"
)


def _strip_masked_heading(text: str) -> str | None:
    """Remove a leading list marker and heading when masking consumed the heading.

    Segmentation often glues a section heading onto its sentence ("2. Queue A queue
    is first-in-first-out."). Masking then leaves two blanks, one of which is the
    heading. Dropping that prefix leaves a single, answerable blank.
    """
    cleaned = _HEADING_PREFIX_RE.sub("", text, count=1).strip()
    if cleaned == text.strip():
        return None
    return cleaned if len(cleaned.split()) >= MASK_MIN_REMAINDER_WORDS else None


def _term_patterns(terms: Sequence[str]) -> list[re.Pattern[str]]:
    cleaned = {" ".join(term.split()) for term in terms if term and len(term.strip()) >= 3}
    return [
        re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        for term in sorted(cleaned, key=len, reverse=True)
    ]


def masked_excerpt(excerpt: str, terms: Sequence[str]) -> str | None:
    """Remove the tested concept's names from a passage, or ``None`` if that empties it.

    A cloze item can only be grounded when something remains once the answer is
    hidden; a syllabus entry that is merely the concept name cannot support one.
    Every term passed in (label and known aliases) is masked so a passage never
    gives the answer away through a synonym.
    """
    patterns = _term_patterns(terms)
    remainder = excerpt
    matched = False
    for pattern in patterns:
        if pattern.search(remainder):
            remainder = pattern.sub(" ", remainder)
            matched = True
    if not matched:
        return None
    remainder = " ".join(remainder.split())
    for character in (" ", ".", ",", ";", ":", '"', "'"):
        remainder = remainder.strip(character)
    if len(remainder.split()) < MASK_MIN_REMAINDER_WORDS:
        return None
    masked = excerpt
    for pattern in patterns:
        masked = pattern.sub(MASK, masked)
    return " ".join(masked.split())


@dataclass
class TemplateProvider:
    """Deterministic, offline construction from syllabus evidence."""

    settings: GenerationSettings
    name: str = "template"
    model: str | None = "templates-v1"

    def supports(self, fmt: GenerationFormat, bloom: BloomLevel) -> bool:
        if fmt in MODEL_ONLY_FORMATS:
            return False
        return template_for(fmt, bloom) is not None

    def capability_note(self, fmt: GenerationFormat, bloom: BloomLevel) -> str:
        if fmt in MODEL_ONLY_FORMATS:
            return (
                f"{fmt.value} questions need a configured language-model provider; "
                "the deterministic template provider does not invent problem statements"
            )
        return (
            f"the deterministic template provider has no grounded construction for "
            f"{fmt.value} at Bloom level {bloom.value}; choose another level or configure "
            "a language-model provider"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "kind": "deterministic",
            "requires_network": False,
            "requires_credentials": False,
            "min_mcq_options": self.settings.min_mcq_options,
            "max_mcq_options": self.settings.max_mcq_options,
        }

    # -- helpers -----------------------------------------------------------

    def _passage(self, prompt: GenerationPrompt) -> SourcePassage | None:
        if not prompt.passages:
            return None
        if prompt.format is GenerationFormat.CASE_BASED:
            # Case tasks need context, not the shortest sentence that happens to
            # mention the label.  Prefer the richest grounded excerpt available.
            candidates = [*prompt.passages, *prompt.related_passages]
            return max(candidates, key=lambda passage: len(passage.excerpt))
        return prompt.passages[0]

    def _mcq_passage(self, prompt: GenerationPrompt) -> SourcePassage | None:
        """Prefer the statement that names the fewest of the offered sibling concepts.

        Syllabus lines often enumerate a whole unit, so the first passage can name
        several concepts that are also options. That makes a cloze stem look as if
        more than one option fits, so a passage naming fewer siblings is preferred.
        If every passage enumerates them, validation still rejects the item rather
        than shipping an ambiguous stem.
        """
        if not prompt.passages:
            return None
        best: tuple[int, int, SourcePassage] | None = None
        for position, passage in enumerate(prompt.passages):
            masked = self._masked_statement(prompt, passage.excerpt)
            if masked is None:
                continue
            # Count siblings in the original excerpt.  The masked statement hides
            # them on purpose, so counting the masked form would make every passage
            # look equally clean and defeat this preference.
            foreign = sum(
                1 for label in prompt.related_labels if _mentions(passage.excerpt, label)
            )
            if best is None or (foreign, position) < best[:2]:
                best = (foreign, position, passage)
        return best[2] if best is not None else prompt.passages[0]

    def _mask_terms(self, prompt: GenerationPrompt, excerpt: str) -> list[str]:
        """The single most specific name for the concept that this statement uses.

        Masking every known name at once can leave more than one blank in a phrase
        like "Stacks and queues: ...", which is not answerable as a single-choice
        item. The longest name present is masked, and the concept's own multi-word
        label outranks a word taken from it.
        """
        present = [term for term in prompt.concept_terms if _mentions(excerpt, term)]
        if not present:
            return []
        present.sort(key=lambda item: (len(item.split()) > 1, len(item)), reverse=True)
        return [present[0]]

    def _masked_statement(self, prompt: GenerationPrompt, excerpt: str) -> str | None:
        """The excerpt with every name of the tested concept hidden, one blank only."""
        masked = masked_excerpt(excerpt, self._mask_terms(prompt, excerpt))
        if masked is None:
            return None
        return _strip_masked_heading(masked) or masked

    def _context(self, prompt: GenerationPrompt, passage: SourcePassage) -> dict[str, str]:
        excerpt = _clean_source_text(passage.excerpt)
        masked = self._masked_statement(prompt, excerpt)
        return {
            "concept": prompt.concept_label,
            # The unit is stored on the question and in provenance.  It is not
            # repeated in the learner-facing task statement: unit names made the
            # generated questions read like syllabus navigation rather than questions.
            "unit": "this syllabus",
            "source": _clean_source_text(passage.document_title) or "the course syllabus",
            "excerpt": excerpt,
            "masked": masked if masked is not None else excerpt,
            "other": prompt.related_labels[0] if prompt.related_labels else "",
            "objective": prompt.learning_objective,
        }

    def _mcq_options(
        self, prompt: GenerationPrompt, passage: SourcePassage | None = None
    ) -> list[GeneratedOption]:
        labels = [prompt.concept_label]
        related: list[str] = []
        for label in prompt.related_labels:
            normalised = label.casefold()
            if normalised not in {item.casefold() for item in labels}:
                related.append(label)
        if passage is not None:
            # Prefer alternatives that do not already appear in the stem.  This
            # keeps a topic-list passage usable as a recognition item without
            # offering the same topics both in the wording and in the options.
            related.sort(key=lambda label: _mentions(passage.excerpt, label))
        labels.extend(related)
        maximum = min(self.settings.max_mcq_options, len(labels))
        if maximum < self.settings.min_mcq_options:
            raise ProviderError(
                "insufficient_distractors",
                (
                    f"a {prompt.format.value} question needs at least "
                    f"{self.settings.min_mcq_options - 1} other concepts in the course to build "
                    f"plausible options; found {len(labels) - 1}"
                ),
                detail={"available": len(labels) - 1},
            )
        options = labels[:maximum]
        rng = random.Random(prompt.seed * 1009 + prompt.index)
        rng.shuffle(options)
        return [
            GeneratedOption(
                key=_OPTION_KEYS[position],
                text=label,
                is_correct=label == prompt.concept_label,
            )
            for position, label in enumerate(options)
        ]

    def generate(self, prompt: GenerationPrompt) -> QuestionDraft:
        candidates = templates_for(prompt.format, prompt.bloom)
        template: Template | None
        if len(candidates) > 1 and prompt.seed:
            rng = random.Random(prompt.seed * 1009 + prompt.index)
            template = candidates[rng.randrange(len(candidates))]
        else:
            template = candidates[0] if candidates else template_for(prompt.format, prompt.bloom)
        if template is None or prompt.format in MODEL_ONLY_FORMATS:
            raise ProviderError(
                "provider_capability", self.capability_note(prompt.format, prompt.bloom)
            )
        passage = (
            self._mcq_passage(prompt)
            if prompt.format is GenerationFormat.MULTIPLE_CHOICE
            else self._passage(prompt)
        )
        needs = needs_of(template)
        if "passage" in needs and passage is None:
            raise ProviderError(
                "insufficient_evidence",
                "no source passage mentions this concept, so a grounded question cannot be built",
            )
        if "second_concept" in needs and not prompt.related_labels:
            raise ProviderError(
                "insufficient_related_concepts",
                "this format needs a second syllabus concept from the same unit to compare with",
            )
        if "case_context" in needs and (
            passage is None or len(passage.excerpt) < CASE_MIN_EXCERPT_CHARS
        ):
            raise ProviderError(
                "insufficient_case_context",
                (
                    "a case-based question needs a longer source passage "
                    f"(at least {CASE_MIN_EXCERPT_CHARS} characters of context)"
                ),
            )
        if passage is None:
            # Every template needs at least one evidence reference; the service
            # always supplies a syllabus-topic passage when document text is thin.
            raise ProviderError("insufficient_evidence", "no evidence passage is available")
        if prompt.format is GenerationFormat.MULTIPLE_CHOICE and (
            self._masked_statement(prompt, passage.excerpt) is None
        ):
            raise ProviderError(
                "insufficient_evidence",
                (
                    "a multiple-choice item needs a passage that describes the concept in "
                    "more than its own name; the only available evidence is the concept "
                    "label, so the item could not be grounded"
                ),
                detail={"concept_id": prompt.concept_id},
            )

        try:
            text = template.render(self._context(prompt, passage))
        except KeyError as exc:
            raise ProviderError(
                "template_context_missing",
                f"template {template.template_id} needs context {exc}",
            ) from exc
        draft = QuestionDraft(
            text=text,
            template_id=template.template_id,
            metadata={
                "provider": self.name,
                "model": self.model,
                "format": prompt.format.value,
                "bloom": prompt.bloom.value,
                "difficulty": prompt.difficulty.value,
                "template_id": template.template_id,
                "answer_mode": template.answer_mode,
            },
        )
        if prompt.format is GenerationFormat.MULTIPLE_CHOICE:
            options = self._mcq_options(prompt, passage)
            draft.options = [option.text for option in options]
            draft.correct_index = next(
                position for position, option in enumerate(options) if option.is_correct
            )
            draft.answer_key = prompt.concept_label if prompt.include_answer_key else None
            draft.distractor_explanations = {
                option.key: (
                    f"{option.text} is a syllabus concept from this course, "
                    "but the quoted statement does not describe it."
                )
                for option in options
                if not option.is_correct
            }
            if prompt.include_explanation:
                draft.explanation = (
                    f"The statement from {passage.document_title} describes {prompt.concept_label}."
                )
            return draft

        if template.answer_mode == "excerpt":
            aspects = _answer_points(prompt.concept_label, passage.excerpt)
            label_only = _is_label_only(passage.excerpt, prompt.concept_label)
            draft.solution_steps = _answer_steps(prompt.concept_label, aspects)
            if prompt.include_answer_key:
                draft.answer_key = _excerpt_answer(
                    prompt.concept_label,
                    passage.excerpt,
                    aspects,
                    label_only=label_only,
                )
                draft.metadata["answer_key_status"] = (
                    "rubric_only"
                    if label_only or _answer_quote(prompt.concept_label, passage.excerpt) is None
                    else "source_excerpt"
                )
            if prompt.include_explanation:
                if label_only:
                    draft.explanation = (
                        "The stored source only names this topic, so this reference "
                        "outline lists what a complete answer should cover. Add course "
                        "notes for a fuller model answer."
                    )
                else:
                    draft.explanation = (
                        "This reference answer paraphrases the stored course material. "
                        "It is a grounding outline, not a complete model answer."
                    )
            return draft

        if template.answer_mode == "pair":
            other = next(
                (
                    item
                    for item in prompt.related_passages
                    if item.concept_id and item.concept_id != prompt.concept_id
                ),
                None,
            )
            if other is None:
                raise ProviderError(
                    "insufficient_evidence",
                    "comparison answers need a grounding passage for the second concept too",
                )
            other_label = prompt.related_labels[0] if prompt.related_labels else "the related concept"
            if prompt.include_answer_key:
                draft.answer_key = (
                    f"Reference material: Compare {prompt.concept_label} with {other_label}. "
                    f'Course material for {prompt.concept_label}: '
                    f'"{_clean_source_text(passage.excerpt)}" '
                    f'Course material for {other_label}: '
                    f'"{_clean_source_text(other.excerpt)}" '
                    "State at least one similarity, one difference, and why the "
                    "difference matters for the task."
                )
            if prompt.include_explanation:
                draft.explanation = (
                    "Both excerpts are quoted from the course material. The comparison "
                    "itself is the student's task; the answer key identifies the evidence "
                    "to compare."
                )
            return draft

        # Open-ended task: provide a reviewable reference outline rather than a
        # single invented model answer.  Every outline point comes from the concept
        # label and the quoted evidence, so the student can see what a complete
        # answer should cover.
        aspects = _answer_points(prompt.concept_label, passage.excerpt)
        draft.solution_steps = _answer_steps(prompt.concept_label, aspects)
        if prompt.include_answer_key:
            draft.answer_key = _outline_answer(
                prompt.concept_label,
                passage.excerpt,
                aspects,
            )
        draft.metadata["answer_key_status"] = "reference_outline"
        if prompt.include_explanation:
            draft.explanation = (
                "This is a reference outline grounded in the stored course material. "
                "The task is open-ended, so the outline identifies what a complete "
                "answer should cover rather than prescribing one answer."
            )
        return draft


class OpenAICompatibleProvider:
    """Opt-in provider for an OpenAI-compatible chat-completions endpoint."""

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        settings: GenerationSettings,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.settings = settings
        self._opener = opener or _default_opener
        self._sleep = sleep
        if not self._api_key:
            raise ProviderError(
                "provider_not_configured",
                "the language-model provider needs an API key in the environment",
            )

    # -- configuration -----------------------------------------------------

    @classmethod
    def from_env(
        cls,
        settings: GenerationSettings,
        env: Mapping[str, str] | None = None,
        *,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> OpenAICompatibleProvider:
        env = os.environ if env is None else env
        base_url = (settings.llm_base_url or env.get("EXAM_ENGINE_Q10_LLM_BASE_URL") or "").strip()
        model = (settings.llm_model or env.get("EXAM_ENGINE_Q10_LLM_MODEL") or "").strip()
        api_key = (
            env.get("EXAM_ENGINE_Q10_LLM_API_KEY") or env.get("OPENAI_API_KEY") or ""
        ).strip()
        if not model:
            raise ProviderError(
                "provider_not_configured",
                (
                    "set EXAM_ENGINE_Q10_LLM_MODEL to use the model provider; "
                    "the API key is read from the environment, never from config"
                ),
            )
        return cls(
            base_url=base_url or DEFAULT_BASE_URL,
            model=model,
            api_key=api_key,
            settings=settings,
            opener=opener,
            sleep=sleep,
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self.model)

    def supports(self, fmt: GenerationFormat, bloom: BloomLevel) -> bool:
        return True

    def capability_note(self, fmt: GenerationFormat, bloom: BloomLevel) -> str:
        return "the model provider supports every format; output is still validated"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "kind": "language_model",
            "base_url": self.base_url,
            "requires_network": True,
            "requires_credentials": True,
            "max_retries": self.settings.provider_max_retries,
        }

    # -- request building --------------------------------------------------

    def _schema_hint(self, prompt: GenerationPrompt) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "text": "string, the complete question text",
            "answer_key": "string or null",
            "explanation": "string or null",
            "solution_steps": ["string"],
        }
        if prompt.format is GenerationFormat.MULTIPLE_CHOICE:
            schema["options"] = [
                f"exactly {self.settings.min_mcq_options} to "
                f"{self.settings.max_mcq_options} option strings"
            ]
            schema["correct_index"] = "zero-based index of the single correct option"
            schema["distractor_explanations"] = {"A": "why this option is wrong"}
        return schema

    def _messages(self, prompt: GenerationPrompt) -> list[dict[str, str]]:
        evidence = "\n".join(
            f"- [{item.document_title}"
            + (f", page {item.page_number}" if item.page_number else "")
            + f'] "{item.excerpt}"'
            for item in prompt.passages
        )
        related = "\n".join(f"- {label}" for label in prompt.related_labels) or "- (none)"
        rules = [
            "You write practice questions for a university course.",
            "Ground every factual claim in the supplied evidence; never invent citations,",
            "formulas, numbers or textbook pages. If the evidence is insufficient, say so in",
            "the explanation instead of guessing.",
            f"Target Bloom level: {prompt.bloom.value} ({prompt.difficulty.value} difficulty).",
            "The task must genuinely require that cognitive level.",
            "Respond with one JSON object only, no prose and no code fences.",
        ]
        user = "\n".join(
            [
                f"Course: {prompt.course_id}",
                f"Unit: {prompt.unit_label}"
                + (f" - {prompt.unit_title}" if prompt.unit_title else ""),
                f"Concept: {prompt.concept_label}",
                f"Learning objective: {prompt.learning_objective}",
                f"Question format: {prompt.format.value}",
                f"Marks: {prompt.marks:g}",
                f"Language: {prompt.language}",
                f"Related syllabus concepts:\n{related}",
                f"Evidence passages:\n{evidence or '- (none)'}",
                f"Required JSON fields: {json.dumps(self._schema_hint(prompt))}",
                "Do not state that the question will appear in an examination.",
            ]
        )
        return [
            {"role": "system", "content": "\n".join(rules)},
            {"role": "user", "content": user},
        ]

    def _payload(self, prompt: GenerationPrompt) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": 0.2,
            "messages": self._messages(prompt),
            "response_format": {"type": "json_object"},
        }

    # -- transport ---------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            response = self._opener(request, self.settings.provider_timeout_seconds)
            body = response.read() if hasattr(response, "read") else response
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = _retry_after_seconds(exc)
                raise ProviderError(
                    "provider_rate_limited",
                    "the model provider rejected the request with HTTP 429 (rate limit)",
                    retryable=True,
                    detail={"retry_after_seconds": retry_after},
                ) from exc
            if exc.code >= 500:
                raise ProviderError(
                    "provider_error",
                    f"the model provider returned HTTP {exc.code}",
                    retryable=True,
                    detail={"status": exc.code},
                ) from exc
            raise ProviderError(
                "provider_error",
                f"the model provider rejected the request with HTTP {exc.code}",
                detail={"status": exc.code},
            ) from exc
        except TimeoutError as exc:
            raise ProviderError(
                "provider_timeout",
                "the model provider did not answer before the configured timeout",
                retryable=True,
            ) from exc
        except OSError as exc:  # URLError is an OSError
            raise ProviderError(
                "provider_unavailable",
                "the model provider could not be reached",
                retryable=True,
                detail={"error": type(exc).__name__},
            ) from exc
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return str(body)

    # -- response parsing --------------------------------------------------

    def _parse(self, raw: str) -> QuestionDraft:
        if not raw or not raw.strip():
            raise ProviderError(
                "provider_empty_response", "the model provider returned an empty body"
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "provider_invalid_response",
                "the model provider did not return valid JSON",
                detail={"error": str(exc)[:120]},
            ) from exc
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                "provider_invalid_response",
                "the provider response has no choices",
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                "provider_empty_response",
                "the provider choice contains no message content",
            )
        cleaned = _JSON_BLOCK_RE.sub("", content.strip())
        try:
            draft_payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "provider_invalid_response",
                "the question payload is not valid JSON",
                detail={"error": str(exc)[:120]},
            ) from exc
        if not isinstance(draft_payload, dict):
            raise ProviderError(
                "provider_invalid_response",
                "the question payload must be a JSON object",
            )
        try:
            draft = QuestionDraft.model_validate(draft_payload)
        except ValueError as exc:
            raise ProviderError(
                "provider_invalid_response",
                "the question payload does not match the required schema",
                detail={"error": str(exc)[:400]},
            ) from exc
        return draft

    def generate(self, prompt: GenerationPrompt) -> QuestionDraft:
        payload = self._payload(prompt)
        attempts = 0
        last: ProviderError | None = None
        for attempt in range(self.settings.provider_max_retries + 1):
            attempts = attempt + 1
            try:
                draft = self._parse(self._post(payload))
            except ProviderError as exc:
                last = exc
                if not exc.retryable or attempt >= self.settings.provider_max_retries:
                    exc.detail.setdefault("attempts", attempts)
                    raise
                delay = self.settings.provider_retry_backoff_seconds * (2**attempt)
                retry_after = exc.detail.get("retry_after_seconds")
                if isinstance(retry_after, (int, float)):
                    delay = min(max(float(retry_after), 0.0), 5.0)
                if delay > 0:
                    self._sleep(delay)
                continue
            draft.metadata.update(
                {
                    "provider": self.name,
                    "model": self.model,
                    "attempts": attempts,
                    "format": prompt.format.value,
                    "bloom": prompt.bloom.value,
                    "difficulty": prompt.difficulty.value,
                }
            )
            if not draft.answer_key:
                draft.metadata.setdefault("answer_key_status", "not_provided_by_model")
            return draft
        raise last or ProviderError("provider_error", "the provider produced no question")


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    header = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        return None


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - explicit https endpoint


def build_provider(
    name: str,
    settings: GenerationSettings,
    *,
    env: Mapping[str, str] | None = None,
    opener: Callable[[urllib.request.Request, float], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> GenerationProvider:
    """Resolve a provider name; ``auto`` prefers a configured model provider."""

    env = os.environ if env is None else env
    key = name.strip().casefold()
    if key in {"", "auto"}:
        try:
            return OpenAICompatibleProvider.from_env(settings, env, opener=opener, sleep=sleep)
        except ProviderError:
            return TemplateProvider(settings)
    if key in {"template", "deterministic", "offline"}:
        return TemplateProvider(settings)
    if key in {"openai", "llm", "model"}:
        return OpenAICompatibleProvider.from_env(settings, env, opener=opener, sleep=sleep)
    raise ProviderError(
        "invalid_provider",
        f"unknown provider {name!r}; expected auto, template or openai",
        detail={"provider": name},
    )


def default_marks(fmt: GenerationFormat) -> float:
    return DEFAULT_MARKS[fmt]


def passage_excerpt(passage: SourcePassage) -> str:
    return _short(passage.excerpt)


__all__ = [
    "CASE_MIN_EXCERPT_CHARS",
    "DEFAULT_BASE_URL",
    "MASK",
    "GenerationProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "TemplateProvider",
    "build_provider",
    "default_marks",
    "masked_excerpt",
    "passage_excerpt",
]
