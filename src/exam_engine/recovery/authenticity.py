"""Evidence-based content authenticity and source-fidelity checks.

This module does not claim to detect authorship. It reports suspicious content
patterns and, when a source is available, whether the record is traceable to it.
An unavailable source is recorded as ``unverifiable``, never as fabricated.
"""

from __future__ import annotations

import re
from typing import Any

from exam_engine.models.recovery import Issue

_GENERIC_PATTERNS = (
    r"\bit\s+depends\s+on\s+(?:various|many|several)\s+factors\b",
    r"\bthis\s+is\s+a\s+(?:good|great|important)\s+question\b",
    r"\bthe\s+answer\s+is\s+not\s+(?:clear|known)\b",
    r"\bas\s+per\s+(?:the\s+)?(?:passage|text)\b",
    r"\bvarious\s+(?:factors|reasons|things)\b",
    r"\bneed\s+more\s+(?:information|context)\b",
    r"\bcan(?:not|'t)\s+be\s+determined\b",
    r"\bthe\s+correct\s+answer\s+is\s+option\b",
)
_GENERIC_RES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _GENERIC_PATTERNS)
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it of on or that the this to was were will with".split()
)


def _tokens(value: str | None) -> set[str]:
    return {
        token
        for token in _WORD_RE.findall((value or "").casefold())
        if len(token) > 2 and token not in _STOP
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def answer_grounding(question_text: str, answer_text: str | None) -> float:
    """Lexical overlap is only a warning signal, not proof of correctness."""

    return _jaccard(_tokens(question_text), _tokens(answer_text))


def source_fidelity(
    question_text: str,
    *,
    source_texts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Classify traceability against available source documents.

    ``source_texts`` contains documents whose ``kind`` is a question paper or an
    answer key. The comparison is intentionally lexical and disclosed; a human
    must inspect meaning-changing edits.
    """

    if not source_texts:
        return "unverifiable", {
            "reason": "no_question_paper_or_answer_key_source",
            "detail": "No original question-paper or answer-key source is available for this course.",
        }
    wanted = _tokens(question_text)
    best_score = 0.0
    best: dict[str, Any] | None = None
    for source in source_texts:
        score = _jaccard(wanted, _tokens(str(source.get("text") or "")))
        if score > best_score:
            best_score = score
            best = source
    if best_score >= 0.95:
        status = "exact"
    elif best_score >= 0.78:
        status = "formatting_normalized"
    elif best_score >= 0.45:
        status = "partial_or_meaning_change_risk"
    else:
        status = "unsupported_or_not_traceable"
    return status, {
        "reason": "source_comparison",
        "similarity": round(best_score, 3),
        "source": {
            "path": best.get("path") if best else None,
            "kind": best.get("kind") if best else None,
        }
        if best
        else None,
        "limitations": "Lexical comparison cannot establish semantic equivalence by itself.",
    }


def authenticity_issues(
    *,
    question_text: str,
    answer_text: str | None,
    solution_steps: str | None,
    options: list[dict[str, str]],
    source_status: str,
    source_detail: dict[str, Any],
) -> list[Issue]:
    issues: list[Issue] = []
    answer = (answer_text or "").strip()
    if answer:
        for pattern in _GENERIC_RES:
            match = pattern.search(answer)
            if match:
                issues.append(
                    Issue(
                        code="authenticity.generic_answer_language",
                        category="authenticity",
                        severity="warning",
                        message="The answer uses generic language that may not address the question.",
                        evidence={"matched": match.group(0), "answer": answer[:240]},
                        confidence=0.7,
                        remediation="Check the answer against the question and, where available, the source.",
                        field="answer_text",
                    )
                )
                break
        overlap = answer_grounding(question_text, answer)
        if len(answer) >= 20 and overlap < 0.035 and not re.fullmatch(r"[A-H]", answer):
            issues.append(
                Issue(
                    code="authenticity.low_answer_grounding",
                    category="authenticity",
                    severity="info",
                    message="The answer has little lexical overlap with the question; semantic review is advised.",
                    evidence={"overlap": round(overlap, 3), "answer": answer[:240]},
                    confidence=0.55,
                    remediation="Do not treat this as proof of fabrication; verify against the source.",
                    field="answer_text",
                )
            )
        if (
            solution_steps
            and len(solution_steps) > 40
            and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", answer.strip())
            and not _tokens(solution_steps) & _tokens(answer)
        ):
            issues.append(
                Issue(
                    code="authenticity.explanation_answer_mismatch",
                    category="authenticity",
                    severity="info",
                    message="The worked explanation and final answer share little terminology.",
                    evidence={"answer": answer[:160], "solution_excerpt": solution_steps[:240]},
                    confidence=0.5,
                    remediation="Review whether the explanation supports the stated answer.",
                    field="solution_steps",
                )
            )
    if source_status == "unsupported_or_not_traceable":
        issues.append(
            Issue(
                code="source.not_traceable_to_available_paper",
                category="source_fidelity",
                severity="warning",
                message="The record is not traceable to the available source text.",
                evidence=source_detail,
                confidence=0.65,
                remediation="Check the source document and the conversion; do not label the content fabricated without review.",
                blocking=False,
                field="question_text",
            )
        )
    elif source_status == "partial_or_meaning_change_risk":
        issues.append(
            Issue(
                code="source.meaning_change_risk",
                category="source_fidelity",
                severity="warning",
                message="The record only partially matches the available source text.",
                evidence=source_detail,
                confidence=0.7,
                remediation="Compare the question, options and answer with the original before release.",
                blocking=False,
                field="question_text",
            )
        )
    return issues


__all__ = [
    "answer_grounding",
    "authenticity_issues",
    "source_fidelity",
]
