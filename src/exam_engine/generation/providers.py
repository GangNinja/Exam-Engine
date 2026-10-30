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
    needs_of,
    template_for,
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
    model: str | None = "phase10-templates-v1"

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
        return prompt.passages[0] if prompt.passages else None

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
            foreign = sum(1 for label in prompt.related_labels if _mentions(masked, label))
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
        masked = self._masked_statement(prompt, passage.excerpt)
        return {
            "concept": prompt.concept_label,
            "unit": prompt.unit_label,
            "source": passage.document_title,
            "excerpt": passage.excerpt,
            "masked": masked if masked is not None else passage.excerpt,
            "other": prompt.related_labels[0] if prompt.related_labels else "",
            "objective": prompt.learning_objective,
        }

    def _mcq_options(self, prompt: GenerationPrompt) -> list[GeneratedOption]:
        labels = [prompt.concept_label]
        for label in prompt.related_labels:
            normalised = label.casefold()
            if normalised not in {item.casefold() for item in labels}:
                labels.append(label)
        maximum = min(self.settings.max_mcq_options, len(labels))
        if maximum < self.settings.min_mcq_options:
            raise ProviderError(
                "insufficient_distractors",
                (
                    f"a {prompt.format.value} question needs at least "
                    f"{self.settings.min_mcq_options - 1} other concepts in the unit to build "
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
        template = template_for(prompt.format, prompt.bloom)
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
            options = self._mcq_options(prompt)
            draft.options = [option.text for option in options]
            draft.correct_index = next(
                position for position, option in enumerate(options) if option.is_correct
            )
            draft.answer_key = prompt.concept_label if prompt.include_answer_key else None
            draft.distractor_explanations = {
                option.key: (
                    f"{option.text} is a syllabus concept from {prompt.unit_label}, "
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
            if prompt.include_answer_key:
                draft.answer_key = f'Reference from {passage.document_title}: "{passage.excerpt}"'
            if prompt.include_explanation:
                draft.explanation = (
                    f'The course material for {prompt.unit_label} states: "{passage.excerpt}"'
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
            if prompt.include_answer_key:
                draft.answer_key = (
                    f'Reference material: {passage.document_title} - "{passage.excerpt}"; '
                    f'{other.document_title} - "{other.excerpt}"'
                )
            if prompt.include_explanation:
                draft.explanation = (
                    "Both reference excerpts are quoted from the course material; the "
                    "comparison itself is the student's task."
                )
            return draft

        # Open-ended task: no reference answer can be derived from the source
        # without reasoning, so none is invented.
        draft.metadata["answer_key_status"] = "not_provided_by_template_provider"
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
