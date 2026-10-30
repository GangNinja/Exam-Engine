"""Named-entity and concept-candidate extraction.

Two extractors implement the same protocol:

``rule_based`` — lexical patterns (acronyms, notation, method names, dated
references, titled people, institutions). Deterministic, offline, and honest
about being lexical: it does not understand academic concepts, it only finds
phrases shaped like them. Every record keeps the method and a confidence so
downstream phases can weigh it.

``spacy`` — optional (``pip install -e ".[nlp-spacy]"`` plus a downloaded spaCy
model) and used only when explicitly requested. General-purpose NER labels people,
organisations and dates reasonably well but does not know that "binary search"
or "Amdahl's law" are course concepts, so its output stays a candidate.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from typing import Any, Protocol

from exam_engine.models.enums import NlpMethod, Severity, TextOwnerType
from exam_engine.models.nlp import EntityRecord, make_entity_id
from exam_engine.models.question import Uncertainty

DEFAULT_SPACY_MODEL = "en_core_web_sm"


class EntityUnavailable(RuntimeError):
    """Raised when an optional entity extractor cannot run here."""


@dataclass(frozen=True)
class RawEntity:
    """Extractor-neutral finding before it is tied to a course and owner."""

    text: str
    label: str
    start: int
    end: int
    confidence: float


class EntityExtractor(Protocol):
    name: NlpMethod

    def extract(self, text: str) -> list[RawEntity]: ...

    def metadata(self) -> dict[str, Any]: ...


# --- rule-based --------------------------------------------------------------

_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}s?\b")
_METHOD_RE = re.compile(
    r"\b(?:Algorithm|Theorem|Lemma|Corollary|Principle|Law|Rule|Model|Equation|Test|"
    r"Transform|Distribution|Protocol|Diagram)[ \t]+[A-Z0-9][\w'\-]*"
)
_NOTATION_RE = re.compile(r"\b[A-Za-z]\s*\(\s*[^()\n]{1,40}\)")
_YEAR_RE = re.compile(r"\b(?:1[6-9]|20)\d{2}\b")
_PERSON_RE = re.compile(
    r"\b(?:Dr|Prof|Professor|Mr|Ms|Mrs|Sir)\.?[ \t]+[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+)?"
)
# Entities stay on one line: a newline usually separates a heading from the body
# and joining across it produced phrases such as "Algorithms\nDemo Institute".
_ORG_RE = re.compile(
    r"\b(?:[A-Z][\w&.\-]*[ \t]+){1,4}"
    r"(?:University|Institute|College|Board|Council|Ministry|Department|School|Society|"
    r"Association|Organisation|Organization|Commission)\b"
)
_TECH_TERM_RE = re.compile(
    r"\b[a-z]+(?:[- ][a-z]+)?[ \t]+(?:algorithm|tree|graph|sort|search|queue|stack|"
    r"network|protocol|database|complexity|theorem|lemma|matrix|vector|distribution)\b"
)
_PROPER_RE = re.compile(
    r"\b[A-Z][a-z]{2,}(?:[ \t]+(?:of|for|and|the)[ \t]+)?(?:[ \t]+[A-Z][a-z]{2,}){0,2}\b"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (_NOTATION_RE, "NOTATION", 0.8),
    (_METHOD_RE, "METHOD", 0.7),
    (_PERSON_RE, "PERSON", 0.6),
    (_ORG_RE, "ORG", 0.65),
    (_YEAR_RE, "DATE", 0.8),
    (_ACRONYM_RE, "ACRONYM", 0.6),
    (_TECH_TERM_RE, "TECHNIQUE", 0.45),
    (_PROPER_RE, "PROPER_NOUN", 0.4),
)

# Overlapping matches: a span is dropped when an already-kept span of equal or
# higher priority covers it. Priority is by label, then by length.
_LABEL_PRIORITY = {
    "NOTATION": 5,
    "METHOD": 4,
    "ORG": 3,
    "PERSON": 3,
    "DATE": 2,
    "ACRONYM": 2,
    "TECHNIQUE": 1,
    "PROPER_NOUN": 0,
}


class RuleBasedEntityExtractor:
    """Lexical pattern extractor; never claims semantic understanding."""

    name = NlpMethod.RULE_BASED

    def __init__(self, *, max_entities: int = 200) -> None:
        self.max_entities = max(1, max_entities)

    def extract(self, text: str) -> list[RawEntity]:
        candidates: list[tuple[int, int, str, str, float]] = []
        for pattern, label, confidence in _PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(0).strip()
                if value:
                    candidates.append((match.start(), match.end(), value, label, confidence))
        # Highest-priority label first, then the longer span, then the earlier
        # start. Greedy acceptance then guarantees no overlaps: a later, longer
        # same-priority match can no longer sit on top of an already-kept span.
        candidates.sort(
            key=lambda item: (
                -_LABEL_PRIORITY.get(item[3], 0),
                -(item[1] - item[0]),
                item[0],
            )
        )

        kept: list[RawEntity] = []
        for start, end, value, label, confidence in candidates:
            if any(not (end <= other.start or start >= other.end) for other in kept):
                continue
            kept.append(
                RawEntity(
                    text=value,
                    label=label,
                    start=start,
                    end=end,
                    confidence=confidence,
                )
            )
            if len(kept) >= self.max_entities:
                break
        return sorted(kept, key=lambda entity: (entity.start, entity.end))

    def metadata(self) -> dict[str, Any]:
        return {
            "provider": "rule_based",
            "labels": sorted({label for _pattern, label, _c in _PATTERNS}),
            "requires_download": False,
            "description": "lexical patterns; not semantic concept recognition",
        }


# --- optional spaCy ----------------------------------------------------------


class SpacyEntityExtractor:
    """Optional spaCy NER. Requires the ``nlp`` extra and a downloaded model."""

    name = NlpMethod.SPACY

    def __init__(self, model_name: str = DEFAULT_SPACY_MODEL) -> None:
        self.model_name = model_name
        self._nlp: Any = None

    @staticmethod
    def installed() -> bool:
        return importlib.util.find_spec("spacy") is not None

    def available(self) -> bool:
        if not self.installed():
            return False
        try:
            import spacy.util

            return bool(spacy.util.is_package(self.model_name))
        except Exception:  # noqa: BLE001 - optional dependency boundary
            return False

    def _load(self) -> Any:
        if self._nlp is not None:
            return self._nlp
        if not self.installed():
            raise EntityUnavailable(
                "spaCy is not installed; install the 'nlp-spacy' extra to use this provider"
            )
        try:
            import spacy

            self._nlp = spacy.load(self.model_name)
        except Exception as exc:  # noqa: BLE001 - optional dependency boundary
            raise EntityUnavailable(
                f"could not load spaCy model '{self.model_name}': {exc}"
            ) from exc
        return self._nlp

    def extract(self, text: str) -> list[RawEntity]:
        nlp = self._load()
        try:
            doc = nlp(text[:100_000])
        except Exception as exc:  # noqa: BLE001 - optional dependency boundary
            raise EntityUnavailable(f"spaCy inference failed: {exc}") from exc
        return [
            RawEntity(
                text=entity.text,
                label=entity.label_,
                start=int(entity.start_char),
                end=int(entity.end_char),
                confidence=0.7,
            )
            for entity in doc.ents
        ]

    def metadata(self) -> dict[str, Any]:
        return {
            "provider": "spacy",
            "model": self.model_name,
            "requires_download": True,
            "description": "general-purpose NER; not syllabus-aware",
        }


# --- records -----------------------------------------------------------------


def normalize_entity(value: str) -> str:
    return " ".join(value.casefold().split())


def entity_records(
    extractor: EntityExtractor,
    text: str,
    *,
    course_id: str,
    owner_type: TextOwnerType,
    owner_id: str,
    document_id: str | None = None,
) -> list[EntityRecord]:
    """Attach extractor findings to a course/owner with provenance."""
    records: list[EntityRecord] = []
    for raw in extractor.extract(text):
        if not raw.text.strip():
            continue
        records.append(
            EntityRecord(
                entity_id=make_entity_id(
                    owner_type, owner_id, raw.label, raw.start, raw.text.strip()
                ),
                course_id=course_id,
                document_id=document_id,
                owner_type=owner_type,
                owner_id=owner_id,
                text=raw.text.strip(),
                normalized=normalize_entity(raw.text),
                label=raw.label,
                method=extractor.name,
                confidence=max(0.0, min(1.0, raw.confidence)),
                start_char=max(0, raw.start),
                end_char=max(raw.start, raw.end),
            )
        )
    return records


def get_extractor(name: str, *, spacy_model: str = DEFAULT_SPACY_MODEL) -> EntityExtractor:
    lowered = (name or "rule_based").casefold()
    if lowered in {"rule_based", "rules", "heuristic"}:
        return RuleBasedEntityExtractor()
    if lowered in {"spacy", "ner"}:
        return SpacyEntityExtractor(spacy_model)
    raise KeyError(f"unknown entity extractor '{name}'; available: rule_based, spacy")


def build_extractor(
    name: str, *, spacy_model: str = DEFAULT_SPACY_MODEL
) -> tuple[EntityExtractor, list[Uncertainty]]:
    """Resolve an extractor, falling back to lexical rules when spaCy is unavailable."""
    warnings: list[Uncertainty] = []
    requested = (name or "rule_based").casefold()
    if requested in {"spacy", "ner"}:
        candidate = SpacyEntityExtractor(spacy_model)
        if candidate.available():
            return candidate, warnings
        warnings.append(
            Uncertainty(
                code="model_unavailable",
                message=(
                    f"spaCy model '{spacy_model}' is not available; "
                    "entity extraction fell back to lexical rules"
                ),
                severity=Severity.WARNING,
            )
        )
        return RuleBasedEntityExtractor(), warnings
    return get_extractor(requested, spacy_model=spacy_model), warnings


def lexical_extraction_note(extractor: EntityExtractor) -> Uncertainty | None:
    """Record that a lexical extractor ran, so results are not over-trusted."""
    if extractor.name is NlpMethod.RULE_BASED:
        return Uncertainty(
            code="lexical_entities",
            message=(
                "entity extraction used lexical rules, not a semantic model; "
                "candidates still need review"
            ),
            severity=Severity.INFO,
        )
    return None


__all__ = [
    "DEFAULT_SPACY_MODEL",
    "EntityExtractor",
    "EntityUnavailable",
    "RawEntity",
    "RuleBasedEntityExtractor",
    "SpacyEntityExtractor",
    "build_extractor",
    "entity_records",
    "get_extractor",
    "lexical_extraction_note",
    "normalize_entity",
]
