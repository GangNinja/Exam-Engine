"""Configurable cue definitions for question understanding.

The taxonomy is deliberately data rather than hard-coded conditionals: labels,
descriptions, cue regexes and their weights live in :class:`Taxonomy`, so a
department can tune wording without editing classifier code. The built-in
taxonomy below is the documented default and stays inspectable from the CLI.

Cues are *evidence*, never a verdict. A question is labelled only when the
accumulated cue weight reaches the configured threshold, and every label stores
the patterns that fired. The taxonomy cannot invent a label outside
:class:`~exam_engine.models.enums.QuestionType` or
:class:`~exam_engine.models.enums.BloomLevel`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exam_engine.models.enums import BloomLevel, QuestionType


class TaxonomyError(ValueError):
    """Raised when a taxonomy file is malformed or uses unknown labels."""


@dataclass(frozen=True)
class Cue:
    """One weighted regex signal with the rationale shown as evidence."""

    pattern: str
    weight: float = 1.0
    note: str = ""

    def compile(self) -> re.Pattern[str]:
        try:
            return re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:  # pragma: no cover - validated on construction
            raise TaxonomyError(f"invalid cue pattern {self.pattern!r}: {exc}") from exc


@dataclass(frozen=True)
class LabelDefinition:
    """A label, its definition and the cues that support it."""

    label: str
    description: str
    cues: tuple[Cue, ...] = field(default_factory=tuple)

    def validated(self) -> LabelDefinition:
        if not self.label or not self.description.strip():
            raise TaxonomyError("every label needs a name and a description")
        for cue in self.cues:
            if cue.weight <= 0 or cue.weight > 3:
                raise TaxonomyError(
                    f"cue weight for {self.label} must be in (0, 3], got {cue.weight}"
                )
            cue.compile()
        return self


@dataclass(frozen=True)
class Taxonomy:
    """Question-type and Bloom definitions plus a version for auditability."""

    version: str
    question_types: dict[str, LabelDefinition]
    bloom_levels: dict[str, LabelDefinition]
    source: str = "builtin"

    def validated(self) -> Taxonomy:
        if not self.version.strip():
            raise TaxonomyError("taxonomy version must not be empty")
        allowed_types = {level.value for level in QuestionType}
        allowed_bloom = {level.value for level in BloomLevel}
        for label, definition in self.question_types.items():
            if label not in allowed_types:
                raise TaxonomyError(f"unknown question type label: {label}")
            if definition.label != label:
                raise TaxonomyError(f"definition label mismatch for {label}")
            definition.validated()
        for label, definition in self.bloom_levels.items():
            if label not in allowed_bloom:
                raise TaxonomyError(f"unknown Bloom level label: {label}")
            if definition.label != label:
                raise TaxonomyError(f"definition label mismatch for {label}")
            definition.validated()
        if not self.question_types:
            raise TaxonomyError("taxonomy must define at least one question type")
        return self


def _cue(pattern: str, weight: float = 1.0, note: str = "") -> Cue:
    return Cue(pattern=pattern, weight=weight, note=note)


DEFAULT_TAXONOMY = Taxonomy(
    version="builtin-1",
    source="builtin",
    question_types={
        QuestionType.DEFINITION.value: LabelDefinition(
            label=QuestionType.DEFINITION.value,
            description=(
                "Recall or state a definition, term, list of facts, or named result "
                "without asking for reasoning beyond the stated content."
            ),
            cues=(
                _cue(r"\bdefine\b", 1.0, "asks for a definition"),
                _cue(r"\bdefinition of\b", 0.9, "names a definition"),
                _cue(r"\bwhat (?:is|are) (?:a|an)\b", 0.55, "definition question form"),
                _cue(
                    r"\bwhat (?:is|are) the (?:definition|meaning|purpose|function|role|"
                    r"difference|significance)\b",
                    0.5,
                    "asks for a term's meaning or role",
                ),
                _cue(
                    r"\bstate (?:the )?(?:laws?|definition|properties|characteristics)\b",
                    0.8,
                    "asks to state established content",
                ),
                _cue(r"^\s*(?:\d+[.)]\s*)?state\b", 0.45, "imperative recall"),
                _cue(
                    r"(?:^|[.!?;]\s*|\band\s+|\bthen\s+)(?:list|name)\b",
                    0.55,
                    "asks for a list or names",
                ),
                _cue(r"\bwrite short notes on\b", 0.7, "asks for brief recall notes"),
            ),
        ),
        QuestionType.EXPLANATION.value: LabelDefinition(
            label=QuestionType.EXPLANATION.value,
            description=(
                "Explain, describe, discuss or interpret a mechanism, reason or "
                "relationship in the student's own words."
            ),
            cues=(
                _cue(r"\bexplain\b", 1.0, "asks for explanation"),
                _cue(r"\bdescribe\b", 0.9, "asks for a description"),
                _cue(r"\bdiscuss\b", 0.65, "asks for discussion"),
                _cue(r"\billustrate\b", 0.6, "asks for illustration"),
                _cue(r"\bsummar(?:y|ise|ize)\b", 0.7, "asks for a summary"),
                _cue(r"\binterpret\b", 0.7, "asks for interpretation"),
                _cue(r"\bwhy\b", 0.45, "asks for a reason"),
                _cue(r"\bhow (?:does|do|is|are|can)\b", 0.35, "asks how something works"),
            ),
        ),
        QuestionType.COMPARISON.value: LabelDefinition(
            label=QuestionType.COMPARISON.value,
            description="Ask for similarities, differences or a contrast between items.",
            cues=(
                _cue(r"\bcompare\b", 1.0, "asks for comparison"),
                _cue(r"\bcontrast\b", 0.95, "asks for contrast"),
                _cue(r"\bdifferences? between\b", 0.9, "asks for differences"),
                _cue(r"\bdistinguish between\b", 0.85, "asks to distinguish"),
                _cue(r"\badvantages? (?:and|vs\.?) disadvantages?\b", 0.7, "asks for trade-offs"),
                _cue(r"\bversus\b|\bvs\.?\b", 0.45, "names a contrast"),
            ),
        ),
        QuestionType.APPLICATION.value: LabelDefinition(
            label=QuestionType.APPLICATION.value,
            description=(
                "Apply a known method, rule or procedure to a new case, including "
                "tracing and solving."
            ),
            cues=(
                _cue(r"\bapply\b", 1.0, "asks to apply knowledge"),
                _cue(r"\busing\b", 0.35, "asks for use of a method"),
                _cue(r"\bsolve\b", 0.75, "asks for a solution"),
                _cue(r"\bfind\b", 0.5, "asks to find a value"),
                _cue(r"\bdetermine\b", 0.45, "asks to determine a value"),
                _cue(r"\btrace\b", 0.8, "asks to trace an execution"),
                _cue(r"\bdemonstrate\b", 0.5, "asks to demonstrate"),
                _cue(r"\bshow how\b", 0.6, "asks to show a procedure"),
            ),
        ),
        QuestionType.NUMERICAL.value: LabelDefinition(
            label=QuestionType.NUMERICAL.value,
            description=(
                "Require calculation, measurement or manipulation of numerical or "
                "symbolic quantities."
            ),
            cues=(
                _cue(r"\bcalculate\b", 1.0, "asks for a calculation"),
                _cue(r"\bcompute\b", 1.0, "asks for computation"),
                _cue(
                    r"\bevaluate (?:the )?(?:value|expression|integral|limit)\b",
                    0.8,
                    "asks for a value",
                ),
                _cue(
                    r"\bfind (?:the )?(?:value|magnitude|acceleration|velocity|period|"
                    r"frequency|current|voltage|force|energy|mass|height|area|volume|"
                    r"probability|number)\b",
                    0.7,
                    "asks for a numeric quantity",
                ),
                _cue(
                    r"\bwhat is the (?:maximum|minimum|largest|smallest|number of|value of)\b",
                    0.6,
                    "asks for a computed quantity",
                ),
                _cue(r"\b(?:array|list|matrix)\s*\[", 0.5, "contains an explicit data set"),
                _cue(
                    r"\b\d+(?:\.\d+)?\s*(?:kg|m/s|m/s\^?2|n/m|hz|j|w|v|a|mol|%)\b",
                    0.5,
                    "contains a quantity with units",
                ),
                _cue(r"\d\s*[+\-*/=]\s*\d", 0.5, "contains arithmetic"),
            ),
        ),
        QuestionType.SCENARIO.value: LabelDefinition(
            label=QuestionType.SCENARIO.value,
            description=(
                "Present a case, situation or passage and ask the student to reason "
                "within that context."
            ),
            cues=(
                _cue(r"\bcase study\b", 1.0, "references a case study"),
                _cue(r"\bscenario\b", 0.95, "references a scenario"),
                _cue(r"\bsuppose\b", 0.6, "sets up a hypothetical case"),
                _cue(r"\bconsider (?:a|an|the following|the case)\b", 0.55, "sets up a case"),
                _cue(
                    r"\ba (?:company|student|team|hospital|bank|school)\b",
                    0.4,
                    "describes a setting",
                ),
                _cue(r"\byou are (?:given|asked|required)\b", 0.55, "addresses a case role"),
                _cue(r"\bgiven the following\b", 0.45, "introduces given context"),
            ),
        ),
        QuestionType.PROOF.value: LabelDefinition(
            label=QuestionType.PROOF.value,
            description=(
                "Ask for a proof, derivation or formal verification of a claim or expression."
            ),
            cues=(
                _cue(r"\bprove\b", 1.0, "asks for a proof"),
                _cue(r"\bproof\b", 0.9, "names a proof"),
                _cue(r"\bderive\b", 0.9, "asks for a derivation"),
                _cue(r"\bshow that\b", 0.85, "asks to show a claim"),
                _cue(r"\bverify that\b", 0.65, "asks for verification"),
                _cue(r"\bhence show\b", 0.7, "asks for a chained proof"),
                _cue(r"\bformally (?:prove|show)\b", 0.9, "asks for formal reasoning"),
            ),
        ),
        QuestionType.PROGRAMMING.value: LabelDefinition(
            label=QuestionType.PROGRAMMING.value,
            description=(
                "Ask for code, pseudocode, algorithm design, debugging or analysis "
                "of program behaviour and complexity."
            ),
            cues=(
                _cue(
                    r"\bwrite (?:a |an )?(?:program|code|function|procedure|algorithm|"
                    r"pseudocode)\b",
                    1.0,
                    "asks for code or pseudocode",
                ),
                _cue(
                    r"\bimplement (?:a |an |the )?(?:program|code|algorithm|function|"
                    r"procedure|method|class|interface|module|data structure|routine|"
                    r"feature)\b",
                    0.8,
                    "asks for a code or algorithm implementation",
                ),
                # "Implement a policy" is not a programming question in a
                # management course; bare "implement" only supports other cues.
                _cue(r"\bimplement\b", 0.25, "mentions an implementation"),
                _cue(r"\bpseudocode\b", 0.8, "mentions pseudocode"),
                _cue(r"\bcode (?:snippet|fragment|below)\b", 0.7, "references code"),
                # A language name alone is weak evidence across courses ("rust"
                # also names corrosion, "go" is a verb). It supports other
                # programming cues but cannot reach the type threshold alone.
                _cue(
                    r"\b(?:python|java|javascript|typescript|c\+\+|c#|sql|rust|golang)\b",
                    0.25,
                    "names a programming language",
                ),
                _cue(r"\b(?:time|space) complexity\b", 0.6, "asks about complexity"),
                _cue(r"\bdebug\b|\bunit test\b|\bcompile\b", 0.55, "programming activity"),
            ),
        ),
        QuestionType.DESIGN.value: LabelDefinition(
            label=QuestionType.DESIGN.value,
            description=(
                "Ask the student to design, construct or formulate a solution, "
                "diagram, model or plan."
            ),
            cues=(
                _cue(r"\bdesign\b", 1.0, "asks for design"),
                _cue(r"\barchitecture\b", 0.75, "asks about architecture"),
                _cue(r"\bdraw (?:a |an |the )?", 0.65, "asks for a diagram or drawing"),
                _cue(r"\bconstruct\b", 0.6, "asks to construct"),
                _cue(r"\bformulate\b", 0.6, "asks to formulate"),
                _cue(r"\bdevelop (?:a |an )", 0.5, "asks to develop an artefact"),
                _cue(r"\bpropose\b", 0.5, "asks for a proposal"),
                _cue(r"\bmodel the\b", 0.5, "asks for a model"),
            ),
        ),
        QuestionType.EVALUATION.value: LabelDefinition(
            label=QuestionType.EVALUATION.value,
            description=(
                "Ask for a judgement, critique, recommendation or justified assessment "
                "of alternatives."
            ),
            cues=(
                _cue(r"\bevaluate\b", 1.0, "asks for evaluation"),
                _cue(r"\bassess\b", 0.9, "asks for assessment"),
                _cue(r"\bcritique\b|\bcritically\b", 0.85, "asks for critique"),
                _cue(r"\bjustify\b", 0.75, "asks for justification"),
                _cue(r"\bjudge\b|\bappraise\b", 0.8, "asks for judgement"),
                _cue(r"\brecommend\b", 0.7, "asks for a recommendation"),
                _cue(r"\bcomment on\b", 0.55, "asks for commentary"),
                _cue(
                    r"\badvantages? (?:and|vs\.?) disadvantages?\b",
                    0.5,
                    "asks for a trade-off judgement",
                ),
                _cue(r"\bmerits?\b", 0.5, "asks about merits"),
                _cue(
                    r"\bis it (?:appropriate|justified|better)\b",
                    0.6,
                    "asks for a justified judgement",
                ),
                _cue(r"\bvalidate\b", 0.55, "asks to validate a claim"),
            ),
        ),
    },
    bloom_levels={
        BloomLevel.REMEMBER.value: LabelDefinition(
            label=BloomLevel.REMEMBER.value,
            description=(
                "Retrieve facts, definitions, names or lists that were taught. "
                "Memory is necessary but not sufficient evidence for this level."
            ),
            cues=(
                _cue(r"\bdefine\b", 0.7, "retrieve a definition"),
                _cue(
                    r"(?:^|[.!?;]\s*|\band\s+|\bthen\s+)(?:list|name)\b",
                    0.6,
                    "retrieve a list or names",
                ),
                _cue(r"^\s*(?:\d+[.)]\s*)?state\b", 0.45, "state established content"),
                _cue(r"\bstate\b", 0.25, "mentions established content"),
                _cue(
                    r"\brecall\b|\bidentify\b|\blabel\b|\bmatch\b|\breproduce\b",
                    0.6,
                    "retrieval verb",
                ),
                _cue(r"\bwhat (?:is|are) (?:a|an)\b", 0.4, "recall form"),
                _cue(
                    r"\bwhat (?:is|are) the (?:definition|meaning|purpose|function|role)\b",
                    0.35,
                    "recall a definition or role",
                ),
            ),
        ),
        BloomLevel.UNDERSTAND.value: LabelDefinition(
            label=BloomLevel.UNDERSTAND.value,
            description=(
                "Explain, describe, summarise or interpret meaning and relationships "
                "without necessarily applying them."
            ),
            cues=(
                _cue(r"\bexplain\b", 0.7, "explain meaning"),
                _cue(r"\bdescribe\b", 0.6, "describe meaning"),
                _cue(r"\bsummar(?:y|ise|ize)\b|\binterpret\b", 0.6, "interpret or summarise"),
                _cue(r"\billustrate\b", 0.5, "illustrate meaning"),
                _cue(r"\bdiscuss\b", 0.45, "discuss meaning"),
                _cue(
                    r"\bclassify\b|\bcompare\b|\bcontrast\b",
                    0.35,
                    "relate categories (may also be analyse)",
                ),
                _cue(r"\bgive (?:an|one) example\b", 0.35, "exemplify a concept"),
                _cue(r"\bwhy\b", 0.3, "asks for a reason"),
            ),
        ),
        BloomLevel.APPLY.value: LabelDefinition(
            label=BloomLevel.APPLY.value,
            description=(
                "Use a procedure, method or concept in a concrete new situation, "
                "including calculation, tracing and implementation."
            ),
            cues=(
                _cue(r"\bapply\b", 0.75, "apply a method"),
                _cue(r"\bsolve\b|\bcalculate\b|\bcompute\b", 0.65, "execute a procedure"),
                _cue(r"\bfind\b|\bdetermine\b", 0.5, "derive a requested value"),
                _cue(r"\btrace\b|\bexecute\b|\bdemonstrate\b", 0.55, "execute a procedure"),
                _cue(r"\bimplement\b", 0.6, "implement a method"),
                _cue(r"\busing\b", 0.3, "use a given method"),
                _cue(r"\bshow how\b", 0.45, "demonstrate a procedure"),
            ),
        ),
        BloomLevel.ANALYZE.value: LabelDefinition(
            label=BloomLevel.ANALYZE.value,
            description=(
                "Break a problem into parts, examine relationships, compare "
                "alternatives or explain causes and structure."
            ),
            cues=(
                _cue(r"\banaly(?:se|ze)\b", 0.75, "analyse a structure"),
                _cue(r"\bcompare\b|\bcontrast\b", 0.6, "compare parts"),
                _cue(r"\bdifferentiate\b|\bdistinguish\b", 0.6, "distinguish parts"),
                _cue(r"\bexamine\b|\bbreak down\b", 0.55, "examine components"),
                _cue(r"\brelationship\b|\binfer\b", 0.45, "infer relationships"),
                _cue(r"\bderive\b|\bshow that\b", 0.5, "derive a structure"),
                _cue(r"\bwhy\b", 0.25, "explain causes"),
            ),
        ),
        BloomLevel.EVALUATE.value: LabelDefinition(
            label=BloomLevel.EVALUATE.value,
            description=(
                "Judge, critique, justify, recommend or appraise using criteria and evidence."
            ),
            cues=(
                _cue(r"\bevaluate\b", 0.8, "evaluate against criteria"),
                _cue(r"\bassess\b|\bcritique\b|\bcritically\b", 0.75, "assess quality"),
                _cue(r"\bjustify\b|\bargue\b", 0.65, "justify a position"),
                _cue(r"\bjudge\b|\bappraise\b|\brecommend\b", 0.65, "judge or recommend"),
                _cue(r"\bcomment on\b|\bvalidate\b", 0.5, "assess a claim"),
                _cue(r"\badvantages? (?:and|vs\.?) disadvantages?\b", 0.55, "weigh trade-offs"),
                _cue(r"\bis it (?:appropriate|justified|better)\b", 0.6, "judge a choice"),
            ),
        ),
        BloomLevel.CREATE.value: LabelDefinition(
            label=BloomLevel.CREATE.value,
            description=(
                "Produce a new artefact, plan, design, model or solution that did not "
                "exist in the taught material."
            ),
            cues=(
                _cue(r"\bdesign\b", 0.8, "design a new artefact"),
                _cue(r"\bcreate\b|\bcompose\b", 0.75, "create an artefact"),
                _cue(r"\bconstruct\b|\bformulate\b", 0.65, "construct a solution"),
                _cue(r"\bdevelop (?:a |an )|\bbuild\b", 0.55, "develop an artefact"),
                _cue(r"\bpropose\b|\bplan\b", 0.55, "propose a plan"),
                _cue(r"\bwrite (?:a |an )?(?:program|pseudocode)\b", 0.5, "write a new program"),
                _cue(r"\bgenerate\b", 0.5, "generate new material"),
            ),
        ),
    },
)


def load_taxonomy(path: Path | str) -> Taxonomy:
    """Load a JSON taxonomy override and validate every label and regex.

    Shape::

        {"version": "dept-2026", "question_types": {"definition": {
            "description": "...", "cues": [{"pattern": "\\bdefine\\b",
            "weight": 1.0, "note": "asks for a definition"}]}},
         "bloom_levels": {...}}
    """
    taxonomy_path = Path(path)
    try:
        payload = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaxonomyError(f"cannot read taxonomy {taxonomy_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaxonomyError("taxonomy root must be a JSON object")

    def definitions(section: str) -> dict[str, LabelDefinition]:
        raw_section = payload.get(section, {})
        if not isinstance(raw_section, dict) or not raw_section:
            raise TaxonomyError(f"taxonomy needs a non-empty '{section}' object")
        built: dict[str, LabelDefinition] = {}
        for label, raw in raw_section.items():
            if not isinstance(raw, dict):
                raise TaxonomyError(f"{section}.{label} must be an object")
            description = str(raw.get("description") or "").strip()
            cues_raw = raw.get("cues") or []
            if not isinstance(cues_raw, list):
                raise TaxonomyError(f"{section}.{label}.cues must be a list")
            cues: list[Cue] = []
            for index, item in enumerate(cues_raw):
                if isinstance(item, str):
                    cues.append(Cue(pattern=item))
                elif isinstance(item, dict):
                    try:
                        weight = float(item.get("weight", 1.0))
                    except (TypeError, ValueError) as exc:
                        raise TaxonomyError(
                            f"{section}.{label}.cues[{index}].weight must be numeric"
                        ) from exc
                    cues.append(
                        Cue(
                            pattern=str(item.get("pattern") or ""),
                            weight=weight,
                            note=str(item.get("note") or ""),
                        )
                    )
                else:
                    raise TaxonomyError(f"{section}.{label}.cues[{index}] is invalid")
            built[label] = LabelDefinition(label=label, description=description, cues=tuple(cues))
        return built

    taxonomy = Taxonomy(
        version=str(payload.get("version") or "custom"),
        question_types=definitions("question_types"),
        bloom_levels=definitions("bloom_levels"),
        source=str(taxonomy_path),
    )
    return taxonomy.validated()


def taxonomy_as_dict(taxonomy: Taxonomy) -> dict[str, Any]:
    """Serialise the taxonomy for the CLI and run reports."""

    def dump(definitions: dict[str, LabelDefinition]) -> dict[str, Any]:
        return {
            label: {
                "description": definition.description,
                "cues": [
                    {"pattern": cue.pattern, "weight": cue.weight, "note": cue.note}
                    for cue in definition.cues
                ],
            }
            for label, definition in definitions.items()
        }

    return {
        "version": taxonomy.version,
        "source": taxonomy.source,
        "question_types": dump(taxonomy.question_types),
        "bloom_levels": dump(taxonomy.bloom_levels),
    }


__all__ = [
    "DEFAULT_TAXONOMY",
    "Cue",
    "LabelDefinition",
    "Taxonomy",
    "TaxonomyError",
    "load_taxonomy",
    "taxonomy_as_dict",
]
