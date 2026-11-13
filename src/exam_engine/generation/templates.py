"""Grounded question-construction templates for the deterministic provider.

Every template is a *task statement* wrapped around real evidence: a syllabus
concept, its unit, and a quoted passage from the course's own non-examination
material. Templates never state a fact the evidence does not contain, so the
deterministic path cannot invent definitions, numbers or formulae.

Only the formats and cognitive levels listed in :data:`SUPPORTED` are produced
by templates. Anything else is reported to the caller as unsupported rather than
being approximated at a different level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

from exam_engine.models.enums import BloomLevel, DifficultyLabel, GenerationFormat

#: Where a template's answer material comes from.
#: ``excerpt`` - the quoted source passage is the reference answer.
#: ``pair`` - two quoted passages, one per concept, are the reference material.
#: ``none`` - the task is open-ended; the provider must not fabricate a key.
ANSWER_MODES = frozenset({"excerpt", "pair", "none"})

#: Extra evidence a template needs before it can be used.
NEEDS = frozenset({"passage", "second_concept", "case_context", "numeric"})


@dataclass(frozen=True)
class Template:
    """One (format, Bloom level) construction recipe."""

    template_id: str
    format: GenerationFormat
    bloom: BloomLevel
    stem: str
    needs: tuple[str, ...] = ("passage",)
    answer_mode: str = "none"
    notes: str = ""

    def render(self, context: dict[str, str]) -> str:
        missing = [name for name in _placeholders(self.stem) if not context.get(name)]
        if missing:
            raise KeyError(", ".join(sorted(missing)))
        return " ".join(self.stem.format(**context).split())


def _placeholders(text: str) -> set[str]:
    return {
        chunk.split("}", 1)[0]
        for chunk in text.split("{")[1:]
        if "}" in chunk and chunk.split("}", 1)[0].isidentifier()
    }


def _template(
    template_id: str,
    fmt: GenerationFormat,
    bloom: BloomLevel,
    stem: str,
    *,
    needs: tuple[str, ...] = ("passage",),
    answer_mode: str = "none",
    notes: str = "",
) -> Template:
    return Template(
        template_id=template_id,
        format=fmt,
        bloom=bloom,
        stem=stem,
        needs=needs,
        answer_mode=answer_mode,
        notes=notes,
    )


def _build() -> tuple[
    dict[tuple[GenerationFormat, BloomLevel], Template],
    dict[tuple[GenerationFormat, BloomLevel], tuple[Template, ...]],
]:
    entries = [
        # -- multiple choice ------------------------------------------------
        # The excerpt is masked at the concept label, otherwise the correct option
        # would be repeated in the stem and the item would test reading only.
        _template(
            "mcq_def_match_recall",
            GenerationFormat.MULTIPLE_CHOICE,
            BloomLevel.REMEMBER,
            'This statement from {source} has one term removed: "{masked}" '
            "Which concept from {unit} completes it?",
            answer_mode="excerpt",
        ),
        _template(
            "mcq_def_match_understand",
            GenerationFormat.MULTIPLE_CHOICE,
            BloomLevel.UNDERSTAND,
            'Which concept from {unit} completes this statement from {source}: "{masked}"?',
            answer_mode="excerpt",
        ),
        # -- short answer ---------------------------------------------------
        _template(
            "short_define",
            GenerationFormat.SHORT_ANSWER,
            BloomLevel.REMEMBER,
            "Using the course material for {unit}, define the following topic in one "
            "or two sentences: {concept}.",
            answer_mode="excerpt",
        ),
        _template(
            "short_explain",
            GenerationFormat.SHORT_ANSWER,
            BloomLevel.UNDERSTAND,
            "Explain the following topic in your own words, using the treatment in "
            "{unit}: {concept}.",
            answer_mode="excerpt",
        ),
        _template(
            "short_explain_example",
            GenerationFormat.SHORT_ANSWER,
            BloomLevel.UNDERSTAND,
            "Using the course material for {unit}, explain {concept} in your own words "
            "and give one example of how it is used.",
            answer_mode="excerpt",
        ),
        _template(
            "short_explain_why",
            GenerationFormat.SHORT_ANSWER,
            BloomLevel.UNDERSTAND,
            "Explain what {concept} means in the context of {unit}, and state why it "
            "matters in this course.",
            answer_mode="excerpt",
        ),
        _template(
            "short_explain_apply",
            GenerationFormat.SHORT_ANSWER,
            BloomLevel.UNDERSTAND,
            "Describe {concept} from {unit} and explain how you would use it in a "
            "problem or worked example.",
            answer_mode="excerpt",
        ),
        # -- explanation ----------------------------------------------------
        _template(
            "explain_state",
            GenerationFormat.EXPLANATION,
            BloomLevel.REMEMBER,
            "Using the definition given in {unit}, state what the following topic "
            "means: {concept}.",
            answer_mode="excerpt",
        ),
        _template(
            "explain_own_words",
            GenerationFormat.EXPLANATION,
            BloomLevel.UNDERSTAND,
            "Explain the following topic and why it matters in {unit}: {concept}. "
            "Use your own words and refer to the course material where it helps.",
            answer_mode="excerpt",
        ),
        _template(
            "explain_apply",
            GenerationFormat.EXPLANATION,
            BloomLevel.APPLY,
            "Explain how {concept} would be used when working on a problem in "
            "{unit}. Ground your example in the course material.",
            needs=("passage",),
        ),
        _template(
            "explain_analyse",
            GenerationFormat.EXPLANATION,
            BloomLevel.ANALYZE,
            "Explain how {concept} relates to the other ideas in {unit}. Identify "
            "the relationships you rely on.",
        ),
        _template(
            "explain_evaluate",
            GenerationFormat.EXPLANATION,
            BloomLevel.EVALUATE,
            "Evaluate how {concept} is presented in {unit}: what does the material "
            "explain well, and what does it leave unclear? Justify each judgement.",
        ),
        _template(
            "explain_create",
            GenerationFormat.EXPLANATION,
            BloomLevel.CREATE,
            "Develop an explanation of {concept} for a student who has not studied "
            "{unit}, including one example of your own construction.",
        ),
        # -- long answer ----------------------------------------------------
        _template(
            "long_discuss",
            GenerationFormat.LONG_ANSWER,
            BloomLevel.UNDERSTAND,
            "Write a structured answer on the following topic and its place in {unit}: "
            "{concept}. Use the course terminology correctly.",
            answer_mode="excerpt",
        ),
        _template(
            "long_apply",
            GenerationFormat.LONG_ANSWER,
            BloomLevel.APPLY,
            "In a structured answer, show how {concept} is applied within {unit}. "
            "State your assumptions and the steps you take.",
        ),
        _template(
            "long_analyse",
            GenerationFormat.LONG_ANSWER,
            BloomLevel.ANALYZE,
            "Analyse the role of {concept} in {unit}. Separate it into its parts and "
            "explain how those parts relate.",
        ),
        _template(
            "long_evaluate",
            GenerationFormat.LONG_ANSWER,
            BloomLevel.EVALUATE,
            "Evaluate the treatment of {concept} in {unit}. State the criteria you "
            "use and support each judgement with the course material.",
        ),
        _template(
            "long_design",
            GenerationFormat.LONG_ANSWER,
            BloomLevel.CREATE,
            "Propose and justify an approach in {unit} that makes use of {concept}. "
            "Describe the approach, its assumptions and its limits.",
        ),
        # -- comparison -----------------------------------------------------
        _template(
            "compare_understand",
            GenerationFormat.COMPARISON,
            BloomLevel.UNDERSTAND,
            "Compare {concept} and {other} as they are presented in {unit}.",
            needs=("passage", "second_concept"),
            answer_mode="pair",
        ),
        _template(
            "compare_analyse",
            GenerationFormat.COMPARISON,
            BloomLevel.ANALYZE,
            "Analyse the differences between {concept} and {other} in {unit}. "
            "Explain which differences matter and why.",
            needs=("passage", "second_concept"),
            answer_mode="pair",
        ),
        _template(
            "compare_evaluate",
            GenerationFormat.COMPARISON,
            BloomLevel.EVALUATE,
            "Compare {concept} and {other} in {unit}, then judge which is more "
            "suitable for the purposes the material states. Justify your judgement.",
            needs=("passage", "second_concept"),
            answer_mode="pair",
        ),
        # -- application ----------------------------------------------------
        _template(
            "apply_new_situation",
            GenerationFormat.APPLICATION,
            BloomLevel.APPLY,
            "Apply {concept} to a new situation drawn from {unit}. State your "
            "assumptions and the steps you take.",
        ),
        _template(
            "apply_to_passage",
            GenerationFormat.APPLICATION,
            BloomLevel.ANALYZE,
            'Apply {concept} to the following material from {source}: "{excerpt}" '
            "Explain what the application shows and what it does not.",
        ),
        # -- case based -----------------------------------------------------
        _template(
            "case_apply",
            GenerationFormat.CASE_BASED,
            BloomLevel.APPLY,
            'Case: "{excerpt}" (from {source}). Using {concept}, explain what is '
            "happening in this case and what should be done next.",
            needs=("passage", "case_context"),
        ),
        _template(
            "case_analyse",
            GenerationFormat.CASE_BASED,
            BloomLevel.ANALYZE,
            'Case: "{excerpt}" (from {source}). Analyse the case using {concept}. '
            "Identify the assumptions your analysis depends on.",
            needs=("passage", "case_context"),
        ),
        _template(
            "case_evaluate",
            GenerationFormat.CASE_BASED,
            BloomLevel.EVALUATE,
            'Case: "{excerpt}" (from {source}). Evaluate the situation in the case '
            "using {concept}, and justify your judgement.",
            needs=("passage", "case_context"),
        ),
        # -- analysis -------------------------------------------------------
        _template(
            "analyse_parts",
            GenerationFormat.ANALYSIS,
            BloomLevel.ANALYZE,
            "Analyse {concept} as it appears in {unit}: break it into its parts and "
            "explain how those parts relate.",
        ),
        _template(
            "analyse_evaluate",
            GenerationFormat.ANALYSIS,
            BloomLevel.EVALUATE,
            "Analyse and evaluate the role of {concept} in {unit}, using evidence from {source}.",
        ),
        # -- design ---------------------------------------------------------
        _template(
            "design_create",
            GenerationFormat.DESIGN,
            BloomLevel.CREATE,
            "Design an approach in {unit} that uses {concept} to meet a requirement "
            "you state. Describe the design, its assumptions and its limits.",
        ),
        _template(
            "design_evaluate",
            GenerationFormat.DESIGN,
            BloomLevel.EVALUATE,
            "Evaluate a design in {unit} that relies on {concept}: what would make it "
            "succeed or fail? Support your evaluation with the course material.",
        ),
        # -- evaluation -----------------------------------------------------
        _template(
            "evaluate_analyse",
            GenerationFormat.EVALUATION,
            BloomLevel.ANALYZE,
            "Analyse the strengths and weaknesses of {concept} as presented in "
            "{unit}, separating description from judgement.",
        ),
        _template(
            "evaluate_criteria",
            GenerationFormat.EVALUATION,
            BloomLevel.EVALUATE,
            "Evaluate {concept} using explicit criteria. State the criteria first and "
            "support each judgement with the course material for {unit}.",
        ),
        # -- numerical (model-backed only) ----------------------------------
        _template(
            "numeric_apply",
            GenerationFormat.NUMERICAL,
            BloomLevel.APPLY,
            "Using the relationship involving {concept} stated in {source}, solve a "
            "numerical problem of your own construction and show every step.",
            needs=("passage", "numeric"),
        ),
        _template(
            "numeric_analyse",
            GenerationFormat.NUMERICAL,
            BloomLevel.ANALYZE,
            "Analyse the quantities and relationships behind {concept} in {source}. "
            "Derive a worked example and state its assumptions and units.",
            needs=("passage", "numeric"),
        ),
    ]
    canonical: dict[tuple[GenerationFormat, BloomLevel], Template] = {}
    variants: dict[tuple[GenerationFormat, BloomLevel], list[Template]] = {}
    for item in entries:
        key = (item.format, item.bloom)
        canonical.setdefault(key, item)
        variants.setdefault(key, []).append(item)
    return canonical, {key: tuple(items) for key, items in variants.items()}


_TEMPLATE_INDEX, _VARIANT_INDEX = _build()
TEMPLATES: MappingProxyType[tuple[GenerationFormat, BloomLevel], Template] = MappingProxyType(
    _TEMPLATE_INDEX
)
TEMPLATE_VARIANTS: MappingProxyType[tuple[GenerationFormat, BloomLevel], tuple[Template, ...]] = (
    MappingProxyType(_VARIANT_INDEX)
)

#: Formats a template can produce at all. Numerical problems need a language model
#: because a template cannot invent a solvable problem statement.
TEMPLATE_FORMATS = frozenset(
    item.format for item in TEMPLATES.values() if item.format is not GenerationFormat.NUMERICAL
)

#: Formats that need a capable language model regardless of provider.
MODEL_ONLY_FORMATS = frozenset({GenerationFormat.NUMERICAL})

#: Default marks when the request does not state an allocation. These are
#: conventions of the templates, not calibrated measurements.
DEFAULT_MARKS: MappingProxyType[GenerationFormat, float] = MappingProxyType(
    {
        GenerationFormat.MULTIPLE_CHOICE: 1.0,
        GenerationFormat.SHORT_ANSWER: 3.0,
        GenerationFormat.EXPLANATION: 4.0,
        GenerationFormat.LONG_ANSWER: 8.0,
        GenerationFormat.COMPARISON: 6.0,
        GenerationFormat.APPLICATION: 6.0,
        GenerationFormat.NUMERICAL: 5.0,
        GenerationFormat.CASE_BASED: 8.0,
        GenerationFormat.ANALYSIS: 6.0,
        GenerationFormat.DESIGN: 10.0,
        GenerationFormat.EVALUATION: 8.0,
    }
)

#: Bloom level a request falls back to, per format, when none is supplied.
DEFAULT_BLOOM: MappingProxyType[GenerationFormat, BloomLevel] = MappingProxyType(
    {
        GenerationFormat.MULTIPLE_CHOICE: BloomLevel.UNDERSTAND,
        GenerationFormat.SHORT_ANSWER: BloomLevel.UNDERSTAND,
        GenerationFormat.LONG_ANSWER: BloomLevel.ANALYZE,
        GenerationFormat.EXPLANATION: BloomLevel.UNDERSTAND,
        GenerationFormat.COMPARISON: BloomLevel.ANALYZE,
        GenerationFormat.APPLICATION: BloomLevel.APPLY,
        GenerationFormat.NUMERICAL: BloomLevel.APPLY,
        GenerationFormat.CASE_BASED: BloomLevel.APPLY,
        GenerationFormat.ANALYSIS: BloomLevel.ANALYZE,
        GenerationFormat.DESIGN: BloomLevel.CREATE,
        GenerationFormat.EVALUATION: BloomLevel.EVALUATE,
    }
)

#: Difficulty a request falls back to when none is supplied.
DEFAULT_DIFFICULTY = DifficultyLabel.MODERATE

#: Formats whose requested Bloom level cannot be supported by this phase at all.
FORMAT_BLOOM_LIMITS: MappingProxyType[GenerationFormat, frozenset[BloomLevel]] = MappingProxyType(
    {
        GenerationFormat.MULTIPLE_CHOICE: frozenset({BloomLevel.REMEMBER, BloomLevel.UNDERSTAND}),
        GenerationFormat.SHORT_ANSWER: frozenset({BloomLevel.REMEMBER, BloomLevel.UNDERSTAND}),
    }
)

#: Cognitive verb used to phrase a learning objective for a Bloom level.
OBJECTIVE_VERBS: MappingProxyType[BloomLevel, str] = MappingProxyType(
    {
        BloomLevel.REMEMBER: "Recall",
        BloomLevel.UNDERSTAND: "Explain",
        BloomLevel.APPLY: "Apply",
        BloomLevel.ANALYZE: "Analyse",
        BloomLevel.EVALUATE: "Evaluate",
        BloomLevel.CREATE: "Create with",
    }
)


def template_for(fmt: GenerationFormat, bloom: BloomLevel) -> Template | None:
    return TEMPLATES.get((fmt, bloom))


def templates_for(fmt: GenerationFormat, bloom: BloomLevel) -> tuple[Template, ...]:
    """Return every compatible wording for a format/Bloom pair."""

    return TEMPLATE_VARIANTS.get((fmt, bloom), ())


def supported_pairs() -> list[tuple[GenerationFormat, BloomLevel]]:
    return sorted(TEMPLATES, key=lambda item: (item[0].value, item[1].value))


def needs_of(template: Template) -> frozenset[str]:
    return frozenset(template.needs)


@dataclass(frozen=True)
class FormatSupport:
    """Human-readable capability summary used by the CLI and docs."""

    format: GenerationFormat
    blooms: tuple[BloomLevel, ...]
    requires_model: bool
    templates: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "format": self.format.value,
            "blooms": [level.value for level in self.blooms],
            "requires_model": self.requires_model,
            "templates": list(self.templates),
        }


def format_support() -> list[FormatSupport]:
    by_format: dict[GenerationFormat, list[Template]] = {}
    for (fmt, _bloom), template in TEMPLATES.items():
        by_format.setdefault(fmt, []).append(template)
    support: list[FormatSupport] = []
    for fmt in sorted(by_format, key=lambda item: item.value):
        templates = sorted(by_format[fmt], key=lambda item: item.bloom.value)
        support.append(
            FormatSupport(
                format=fmt,
                blooms=tuple(item.bloom for item in templates),
                requires_model=fmt in MODEL_ONLY_FORMATS,
                templates=tuple(item.template_id for item in templates),
            )
        )
    return support


__all__ = [
    "ANSWER_MODES",
    "DEFAULT_BLOOM",
    "DEFAULT_DIFFICULTY",
    "DEFAULT_MARKS",
    "FORMAT_BLOOM_LIMITS",
    "MODEL_ONLY_FORMATS",
    "NEEDS",
    "OBJECTIVE_VERBS",
    "TEMPLATES",
    "TEMPLATE_FORMATS",
    "TEMPLATE_VARIANTS",
    "FormatSupport",
    "Template",
    "format_support",
    "needs_of",
    "supported_pairs",
    "template_for",
    "templates_for",
]
