"""Local answer checking for stored, generated practice questions.

This is deliberately modest: it checks multiple-choice options exactly and
compares free text with the stored reference and the syllabus topic's key
aspects using lexical coverage. It is useful formative feedback, not a human
grade, and the response says so every time.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from exam_engine.models.generation import GeneratedQuestion
from exam_engine.nlp.text import STOPWORDS

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-+]*")
_GENERIC_TERMS = frozenset(
    {
        "answer",
        "concept",
        "course",
        "define",
        "definition",
        "describe",
        "explain",
        "following",
        "given",
        "material",
        "question",
        "state",
        "topic",
        "unit",
        "using",
        "words",
    }
)
MAX_ANSWER_CHARS = 4000


def _normalise(text: str) -> str:
    return " ".join(text.casefold().split())


def _tokens(text: str) -> list[str]:
    return [item.casefold() for item in _WORD_RE.findall(text)]


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


def _aspects(question: GeneratedQuestion) -> list[str]:
    text = "; ".join(question.concept_labels)
    parts: list[str] = []
    for raw in re.split(r";|,", text):
        part = " ".join(raw.split()).strip(" .,;:")
        if len(part) < 3:
            continue
        if parts and len(part.split()) < 3:
            parts[-1] = f"{parts[-1]}, {part}"
        elif part.casefold() not in {item.casefold() for item in parts}:
            parts.append(part)
    return parts[:8]


def _covered(text_tokens: set[str], terms: list[str]) -> bool:
    return any(term in text_tokens for term in terms)


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
        return {
            "verdict": "answer_not_recognised",
            "score": 0.0,
            "max_score": question.marks,
            "awarded_score": 0.0,
            "feedback": "Choose one of the options before checking the answer.",
            "matched_terms": [],
            "missing_terms": [],
            "expected_points": [option.text for option in question.options],
            "reference_answer": question.answer_key,
            "limitations": [],
        }
    correct = chosen.is_correct
    reference = next(
        (option.text for option in question.options if option.is_correct), question.answer_key
    )
    return {
        "verdict": "correct" if correct else "incorrect",
        "score": 1.0 if correct else 0.0,
        "max_score": question.marks,
        "awarded_score": question.marks if correct else 0.0,
        "feedback": (
            f"Correct. Option {chosen.key} matches the stored answer."
            if correct
            else (
                f"Option {chosen.key} is not the stored answer. Compare it with "
                "the reference and try again."
            )
        ),
        "matched_terms": [chosen.text],
        "missing_terms": [reference] if reference else [],
        "expected_points": [reference] if reference else [],
        "reference_answer": question.answer_key or reference,
        "limitations": [
            "This checks the stored multiple-choice key; it does not explain "
            "the reasoning behind the option."
        ],
    }


def grade_answer(
    question: GeneratedQuestion,
    *,
    answer: str = "",
    option_key: str | None = None,
) -> dict[str, Any]:
    """Return transparent, local feedback for one answer attempt."""

    cleaned = " ".join((answer or "").split())
    if len(cleaned) > MAX_ANSWER_CHARS:
        cleaned = cleaned[:MAX_ANSWER_CHARS]

    if question.options:
        return _option_result(question, cleaned, option_key)

    reference = question.answer_key or ""
    aspects = _aspects(question)
    expected_terms = _terms(" ".join(question.concept_labels))
    answer_tokens = set(_tokens(cleaned))
    matched = [term for term in expected_terms if term in answer_tokens]
    missing = [term for term in expected_terms if term not in answer_tokens]
    matched_points = [aspect for aspect in aspects if _covered(answer_tokens, _terms(aspect))]
    missing_points = [aspect for aspect in aspects if aspect not in matched_points]

    limitations = [
        "This is a local lexical check, not a human or examination grade.",
        "It compares your wording with the stored reference and syllabus topic; "
        "an equivalent answer may still need human review.",
    ]

    if not expected_terms:
        return {
            "verdict": "self_review",
            "score": 0.0,
            "max_score": question.marks,
            "awarded_score": 0.0,
            "feedback": (
                "No reference points are stored for this question. Use the expected-answer "
                "outline to check your response with the course material."
            ),
            "matched_terms": [],
            "missing_terms": [],
            "expected_points": aspects,
            "reference_answer": reference or None,
            "limitations": limitations,
        }

    if len(answer_tokens) < 3:
        return {
            "verdict": "needs_more",
            "score": 0.0,
            "max_score": question.marks,
            "awarded_score": 0.0,
            "feedback": (
                "Write a fuller answer before checking it. Include the key ideas in the outline."
            ),
            "matched_terms": matched,
            "missing_terms": missing[:8],
            "expected_points": aspects,
            "reference_answer": reference or None,
            "limitations": limitations,
        }

    coverage = len(matched) / len(expected_terms)
    similarity = SequenceMatcher(
        None,
        _normalise(cleaned),
        _normalise(" ".join(question.concept_labels)),
    ).ratio()
    score = round(min(1.0, 0.82 * coverage + 0.18 * similarity), 4)
    if score >= 0.72:
        verdict = "strong_match"
        feedback = (
            "Your answer covers most of the stored key ideas. Compare the nuance "
            "with the reference."
        )
    elif score >= 0.38:
        verdict = "partial"
        feedback = "Your answer covers some key ideas. Use the missing points below to complete it."
    else:
        verdict = "needs_more"
        feedback = (
            "Your answer does not yet cover enough of the stored key ideas. "
            "Use the outline and source."
        )

    return {
        "verdict": verdict,
        "score": score,
        "max_score": question.marks,
        "awarded_score": round(score * question.marks, 2),
        "feedback": feedback,
        "matched_terms": matched[:12],
        "missing_terms": missing[:12],
        "expected_points": aspects,
        "covered_points": matched_points,
        "uncovered_points": missing_points,
        "reference_answer": reference or None,
        "limitations": limitations,
    }


__all__ = ["MAX_ANSWER_CHARS", "grade_answer"]
