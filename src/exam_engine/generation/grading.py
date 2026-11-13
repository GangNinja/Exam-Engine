"""Local answer checking for stored, generated practice questions.

This is deliberately modest: it checks multiple-choice options exactly and
compares free text with a stored outline using lexical coverage, with a small
set of conservative plural and synonym equivalences. It is useful formative
feedback, not a human grade, and every response says so explicitly.
"""

from __future__ import annotations

import re
from typing import Any

from exam_engine.models.generation import GeneratedQuestion
from exam_engine.nlp.text import STOPWORDS

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-+]*")
_QUOTE_TRANSLATION = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
_GENERIC_TERMS = frozenset(
    {
        "answer",
        "clear",
        "concept",
        "connect",
        "course",
        "course-grounded",
        "define",
        "definition",
        "describe",
        "example",
        "examples",
        "explain",
        "following",
        "given",
        "idea",
        "ideas",
        "introduction",
        "key",
        "material",
        "one",
        "own",
        "question",
        "relate",
        "relates",
        "sentence",
        "state",
        "support",
        "topic",
        "unit",
        "using",
        "words",
        "write",
    }
)
# Deliberately small and explicit: this is not a thesaurus or a semantic model.
_IRREGULAR_PLURALS: dict[str, frozenset[str]] = {
    "analyses": frozenset({"analysis"}),
    "axes": frozenset({"axis"}),
    "bases": frozenset({"base", "basis"}),
    "criteria": frozenset({"criterion"}),
    "foci": frozenset({"focus"}),
    "formulae": frozenset({"formula"}),
    "hypotheses": frozenset({"hypothesis"}),
    "indices": frozenset({"index"}),
    "loci": frozenset({"locus"}),
    "matrices": frozenset({"matrix"}),
    "maxima": frozenset({"maximum"}),
    "minima": frozenset({"minimum"}),
    "parentheses": frozenset({"parenthesis"}),
    "phenomena": frozenset({"phenomenon"}),
    "radii": frozenset({"radius"}),
    "theses": frozenset({"thesis"}),
    "vertices": frozenset({"vertex"}),
}
_SYNONYM_GROUPS = (
    frozenset({"add", "insert"}),
    frozenset({"average", "mean"}),
    frozenset({"begin", "start"}),
    frozenset({"build", "construct"}),
    frozenset({"calculate", "compute"}),
    frozenset({"cartographic", "cartographical", "cartography"}),
    frozenset({"choose", "select"}),
    frozenset({"decrease", "reduce"}),
    frozenset({"demonstrate", "show"}),
    frozenset({"graph", "plot"}),
    frozenset({"grow", "increase"}),
    frozenset({"level", "leveling", "levelling"}),
    frozenset({"method", "technique"}),
    frozenset({"remove", "delete"}),
    frozenset({"result", "outcome"}),
    frozenset({"sum", "total"}),
    frozenset({"topographic", "topographical", "topography"}),
    frozenset({"triangulate", "triangulation"}),
    frozenset({"verify", "check"}),
)
_SYNONYM_VARIANTS: dict[str, frozenset[str]] = {
    word: group for group in _SYNONYM_GROUPS for word in group
}
_STEP_PREFIX_RE = re.compile(
    r"^(?:cover|include|mention|expected point|answer should cover)\s*:\s*",
    re.IGNORECASE,
)
_GENERIC_STEP_PREFIXES = (
    "connect the ideas",
    "connect these points",
    "support the answer",
    "use the expected-answer outline",
    "use the stored outline",
)
_COVERED_RATIO = 0.70
_PARTIAL_RATIO = 0.20
_STRONG_COVERAGE_RATIO = 0.80
MAX_ANSWER_CHARS = 4000
MIN_ANSWER_TOKENS = 3
MAX_OUTLINE_POINTS = 8
LIMITATIONS = [
    "This is a local word/phrase coverage check, not a human or examination grade.",
    "Regular/irregular plurals, conservative word stems, and a small fixed "
    "list of synonyms are matched lexically; other paraphrases and equivalent "
    "mathematical wording can still be missed. Use the stored outline and "
    "source to judge meaning.",
    "No mark is awarded or deducted; coverage counts describe only which "
    "substantive outline ideas appear in your answer.",
]


def _normalise(text: str) -> str:
    return " ".join(text.casefold().translate(_QUOTE_TRANSLATION).split())


def _tokens(text: str) -> list[str]:
    return [item.casefold() for item in _WORD_RE.findall(text.translate(_QUOTE_TRANSLATION))]


def _base_forms(token: str) -> set[str]:
    """Return the token and conservative singular forms."""

    forms = {token}
    irregular = _IRREGULAR_PLURALS.get(token)
    if irregular is not None:
        forms.update(irregular)
        return forms
    if token.endswith("ies") and len(token) > 4:
        forms.add(token[:-3] + "y")
    elif token.endswith("es") and len(token) > 4:
        forms.add(token[:-2])
        forms.add(token[:-1])
    elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        forms.add(token[:-1])
    return forms


def _word_forms(token: str) -> frozenset[str]:
    """Return singular/plural and explicitly listed synonym forms for a token."""

    forms = _base_forms(token)
    variants = set(forms)
    for form in forms:
        variants.update(_SYNONYM_VARIANTS.get(form, frozenset()))
    return frozenset(variants)


def _terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _tokens(text):
        token = raw.strip("-'")
        if len(token) < 4 or token in STOPWORDS or token in _GENERIC_TERMS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _clean_point(raw: str) -> str:
    point = " ".join(raw.split()).strip(" .,;:")
    point = _STEP_PREFIX_RE.sub("", point).strip(" .,;:")
    if not point:
        return ""
    if point.casefold().startswith(_GENERIC_STEP_PREFIXES):
        return ""
    return point


def _topic_points(question: GeneratedQuestion) -> list[str]:
    """Split stored concept labels into syllabus facets without inventing content."""
    text = "; ".join(question.concept_labels)
    points: list[str] = []
    for raw in re.split(r";|,", text):
        point = _clean_point(raw)
        if len(point) >= 3:
            points.append(point)
    return _unique(points)


def _reference_points(reference: str) -> list[str]:
    """Use the stored answer's sentences only when no outline steps exist."""
    points: list[str] = []
    for raw in re.split(r"(?<=[.!?])\s+", reference):
        point = _clean_point(raw)
        if len(point) >= 3:
            points.append(point)
    return _unique(points)


def _outline_points(question: GeneratedQuestion) -> list[tuple[str, str]]:
    """Return (text, origin) pairs from the most specific stored outline available.

    Generated questions normally carry ``solution_steps`` written from the
    question's own scope. If those are unavailable, the concept label's facets
    are used, and the stored answer text is the last fallback. No new content is
    composed here.
    """

    points: list[tuple[str, str]] = []
    for raw in question.solution_steps:
        point = _clean_point(raw)
        if point and _terms(point):
            points.append((point, "stored_solution_step"))
    if not points:
        points.extend(
            (point, "syllabus_facet")
            for point in _topic_points(question)
            if _terms(point)
        )
    if not points:
        points.extend(
            (point, "stored_reference")
            for point in _reference_points(question.answer_key or "")
            if _terms(point)
        )

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for point, origin in points:
        key = _normalise(point)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((point, origin))
    return unique[:MAX_OUTLINE_POINTS]


def _term_matches(term: str, answer_forms: set[str]) -> bool:
    """Match a reference term to an answer term, allowing safe word variants.

    Prefix matching catches common derivations such as survey/surveying,
    measure/measurement and project/projection without treating unrelated
    short words as equivalent.
    """

    forms = _word_forms(term)
    if forms & answer_forms:
        return True
    for form in forms:
        if len(form) < 5:
            continue
        for candidate in answer_forms:
            if len(candidate) >= 5 and (
                form.startswith(candidate) or candidate.startswith(form)
            ):
                return True
    return False


def _check_point(point: str, origin: str, answer_forms: set[str]) -> dict[str, Any] | None:
    terms = _terms(point)
    if not terms:
        return None
    matched: list[str] = []
    missing: list[str] = []
    for term in terms:
        if _term_matches(term, answer_forms):
            matched.append(term)
        else:
            missing.append(term)
    ratio = len(matched) / len(terms)
    if ratio >= _COVERED_RATIO:
        status = "covered"
    elif ratio >= _PARTIAL_RATIO:
        status = "partial"
    else:
        status = "missing"
    return {
        "point": point,
        "origin": origin,
        "status": status,
        "matched_terms": matched,
        "missing_terms": missing,
    }


def _coverage(outline: list[dict[str, Any]]) -> dict[str, Any]:
    covered = sum(1 for item in outline if item["status"] == "covered")
    partial = sum(1 for item in outline if item["status"] == "partial")
    missing = sum(1 for item in outline if item["status"] == "missing")
    matched_terms = sum(len(item["matched_terms"]) for item in outline)
    total_terms = matched_terms + sum(len(item["missing_terms"]) for item in outline)
    ratio = matched_terms / total_terms if total_terms else 0.0
    return {
        "label": "Local coverage check",
        "method": "lexical_coverage",
        "ratio": round(ratio, 4),
        "key_ideas": {
            "covered": covered,
            "partial": partial,
            "missing": missing,
            "total": len(outline),
        },
        "terms": {
            "matched": matched_terms,
            "total": total_terms,
        },
    }


def _response(
    question: GeneratedQuestion,
    *,
    verdict: str,
    feedback: str,
    outline: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
    reference: str | None = None,
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    outline = outline or []
    coverage = coverage or _coverage(outline)
    return {
        "question_id": question.question_id,
        "verdict": verdict,
        "score": coverage["ratio"],
        "feedback": feedback,
        "coverage": coverage,
        "matched_terms": _unique(
            [term for item in outline for term in item.get("matched_terms", [])]
        ),
        "missing_terms": _unique(
            [term for item in outline for term in item.get("missing_terms", [])]
        ),
        "expected_points": [item["point"] for item in outline],
        "covered_points": [item["point"] for item in outline if item["status"] == "covered"],
        "partial_points": [item["point"] for item in outline if item["status"] == "partial"],
        "uncovered_points": [item["point"] for item in outline if item["status"] == "missing"],
        "expected_outline": outline,
        "reference_answer": reference or None,
        "limitations": limitations or LIMITATIONS,
    }


def _option_result(
    question: GeneratedQuestion, answer: str, option_key: str | None
) -> dict[str, Any]:
    chosen = None
    if option_key:
        chosen = next(
            (
                option
                for option in question.options
                if option.key.casefold() == option_key.casefold()
            ),
            None,
        )
    if chosen is None and answer:
        wanted = _normalise(answer)
        chosen = next(
            (
                option
                for option in question.options
                if _normalise(option.text) == wanted or option.key.casefold() == wanted
            ),
            None,
        )
    if chosen is None:
        return _response(
            question,
            verdict="answer_not_recognised",
            feedback="Choose one of the options before checking the answer.",
            coverage={
                "label": "Local key check",
                "method": "stored_option_key",
                "ratio": 0.0,
                "key_ideas": {"covered": 0, "partial": 0, "missing": 0, "total": 0},
                "terms": {"matched": 0, "total": 0},
            },
            limitations=[
                "This checks the stored multiple-choice key; it does not assess your reasoning.",
                "No mark is awarded; choose an option to verify the stored key.",
            ],
        )

    correct = chosen.is_correct
    correct_option = next((option for option in question.options if option.is_correct), None)
    reference = correct_option.text if correct_option else question.answer_key
    point = (
        f"Option {correct_option.key}: {correct_option.text}"
        if correct_option is not None
        else (question.answer_key or "Stored option key")
    )
    outline: list[dict[str, Any]] = [
        {
            "point": point,
            "origin": "stored_option_key",
            "status": "covered" if correct else "missing",
            "matched_terms": [],
            "missing_terms": [],
        }
    ]
    coverage = {
        "label": "Local key check",
        "method": "stored_option_key",
        "ratio": 1.0 if correct else 0.0,
        "key_ideas": {
            "covered": 1 if correct else 0,
            "partial": 0,
            "missing": 0 if correct else 1,
            "total": 1,
        },
        "terms": {"matched": 0, "total": 0},
    }
    return _response(
        question,
        verdict="correct" if correct else "incorrect",
        feedback=(
            "Your selection matches the stored option key. This confirms the key "
            "only; it does not assess your reasoning."
            if correct
            else "Your selection does not match the stored option key. Compare the "
            "outline with the reference; this is not a grade."
        ),
        outline=outline,
        coverage=coverage,
        reference=question.answer_key or reference,
        limitations=[
            "This checks the stored multiple-choice key; it does not assess your "
            "reasoning or working.",
            "No mark is awarded; the outline shows the stored key only.",
        ],
    )


def _coverage_feedback(coverage: dict[str, Any]) -> str:
    ideas = coverage["key_ideas"]
    total = ideas["total"]
    covered = ideas["covered"]
    partial = ideas["partial"]
    if total == 0:
        return (
            "No stored key-idea outline is available for this question. Compare your "
            "response with the stored reference and course material."
        )
    if covered == total:
        return (
            f"You mentioned all {total} key idea(s) in the stored outline. Compare "
            "your explanation with the reference; this is coverage feedback, not a grade."
        )
    if covered or partial:
        pieces = [f"You fully covered {covered} of {total} key idea(s)"]
        if partial:
            pieces.append(f"and partly covered {partial}")
        return (
            f"{' '.join(pieces)}. The outline below shows what is still missing. "
            "This is word/phrase coverage feedback, not a grade."
        )
    return (
        "Your answer does not yet mention the key ideas in the stored outline. Use "
        "the outline below to revise; this is not a grade."
    )


def grade_answer(
    question: GeneratedQuestion,
    *,
    answer: str = "",
    option_key: str | None = None,
) -> dict[str, Any]:
    """Return transparent, local coverage feedback for one answer attempt."""

    cleaned = " ".join((answer or "").split())
    if len(cleaned) > MAX_ANSWER_CHARS:
        cleaned = cleaned[:MAX_ANSWER_CHARS]

    if question.options:
        return _option_result(question, cleaned, option_key)

    reference = question.answer_key or ""
    answer_tokens = {token.strip("'-") for token in _tokens(cleaned)}
    answer_tokens.discard("")
    answer_forms: set[str] = set()
    for token in answer_tokens:
        answer_forms.update(_word_forms(token))
    outline = [
        checked
        for point, origin in _outline_points(question)
        if (checked := _check_point(point, origin, answer_forms)) is not None
    ]
    coverage = _coverage(outline)

    if not outline:
        return _response(
            question,
            verdict="self_review",
            feedback=(
                "No key-idea outline is stored for this question. Use the stored "
                "reference and the course material to check your response."
            ),
            coverage=coverage,
            reference=reference,
        )

    if len(answer_tokens) < MIN_ANSWER_TOKENS:
        return _response(
            question,
            verdict="needs_more",
            feedback=(
                "Write a fuller answer before checking it. Include the key ideas in "
                "the outline; this is not a grade."
            ),
            outline=outline,
            coverage=coverage,
            reference=reference,
        )

    ideas = coverage["key_ideas"]
    strong = ideas["covered"] == ideas["total"] or (
        coverage["ratio"] >= _STRONG_COVERAGE_RATIO
        and ideas["missing"] == 0
        and ideas["partial"] <= 1
    )
    if strong:
        verdict = "strong_match"
    elif ideas["covered"] or ideas["partial"]:
        verdict = "partial"
    else:
        verdict = "needs_more"
    return _response(
        question,
        verdict=verdict,
        feedback=_coverage_feedback(coverage),
        outline=outline,
        coverage=coverage,
        reference=reference,
    )


__all__ = ["LIMITATIONS", "MAX_ANSWER_CHARS", "MIN_ANSWER_TOKENS", "grade_answer"]
