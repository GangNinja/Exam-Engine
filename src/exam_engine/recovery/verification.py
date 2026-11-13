"""Independent answer verification.

Recovery and verification are separate operations. A candidate is verified only
when an answer key, an independent calculation/solution, or genuinely
independent public evidence supports it. Repeated model output and duplicated
snippets are not independent corroboration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from exam_engine.datasets.parsing import ParsedOption, extract_correct_option
from exam_engine.models.recovery import AnswerCandidate, VerificationResult

VERIFIED_STATUSES = frozenset(
    {
        "official_verified",
        "official_verified_with_independent_check",
        "multiple_source_corroborated",
        "independently_solved_verified",
        "corroborated_local",
    }
)
OFFICIAL_STATUSES = frozenset(
    {"official_verified", "official_verified_with_independent_check"}
)
_NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER_RE = re.compile(_NUMBER_PATTERN)
_RANGE_RE = re.compile(rf"^({_NUMBER_PATTERN})\s+to\s+({_NUMBER_PATTERN})$", re.IGNORECASE)
_OPTION_SET_RE = re.compile(r"^[a-h](?:\s*[;, ]\s*[a-h])+$", re.IGNORECASE)


@dataclass
class VerificationOutcome:
    result: VerificationResult
    conflicts: list[str] = field(default_factory=list)


def _normalise(value: str | None) -> str:
    return " ".join(str(value or "").casefold().replace("−", "-").split())


def _numeric(value: str | None) -> float | None:
    text = _normalise(value).replace(",", "")
    match = _NUMBER_RE.fullmatch(text.strip(" .;:"))
    if match is None:
        return None
    try:
        number = float(match.group())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _canonical_answer(value: str | None) -> str:
    """Normalize only unambiguous serialization differences."""

    text = _normalise(value).strip(" .;:")
    if _OPTION_SET_RE.fullmatch(text):
        return ",".join(sorted(set(re.findall(r"[a-h]", text, re.IGNORECASE))))
    match = _RANGE_RE.fullmatch(text)
    if match:
        low, high = _numeric(match.group(1)), _numeric(match.group(2))
        if low is not None and high is not None:
            if abs(low - high) <= 1e-12:
                return format(low, ".15g")
            return f"{format(low, '.15g')} to {format(high, '.15g')}"
    return text


def answers_equivalent(
    left: str | None,
    right: str | None,
    *,
    options: list[ParsedOption] | None = None,
    tolerance: float = 1e-6,
) -> bool:
    """Compare exact/numeric/MCQ answers without inventing equivalence."""

    if not left or not right:
        return False
    left_text, right_text = _canonical_answer(left), _canonical_answer(right)
    if left_text == right_text:
        return True
    left_num, right_num = _numeric(left), _numeric(right)
    if left_num is not None and right_num is not None:
        return abs(left_num - right_num) <= max(tolerance, tolerance * abs(right_num))
    if options:
        left_key = extract_correct_option(left, "mcq_option", options)
        right_key = extract_correct_option(right, "mcq_option", options)
        if left_key and right_key and left_key == right_key:
            return True
    return False


def _option_keys(options: list[ParsedOption] | None) -> set[str]:
    return {item.key.upper() for item in (options or [])}


def _candidate_key(
    answer: str | None, options: list[ParsedOption] | None, answer_type: str
) -> str | None:
    if not answer:
        return None
    if options:
        return extract_correct_option(answer, "mcq_option", options)
    if answer_type in {"mcq_option", "multiple_options"}:
        return extract_correct_option(answer, answer_type, [])
    return None


def verify_answer(
    *,
    question_text: str,
    options: list[ParsedOption] | None,
    answer_text: str | None,
    answer_type: str,
    candidate: AnswerCandidate | None = None,
    independent_answer: str | None = None,
    conflicting_evidence: bool = False,
) -> VerificationOutcome:
    """Verify one answer candidate and report exactly what was checked."""

    outcome_issue: list[str] = []
    if conflicting_evidence:
        return VerificationOutcome(
            VerificationResult(
                status="conflicting_evidence",
                method="conflicting_sources",
                confidence=0.9,
                answer_text=answer_text,
                evidence={"reason": "independent paths disagree"},
                issues=["conflicting_evidence"],
            ),
            ["independent paths disagree"],
        )

    if not answer_text:
        return VerificationOutcome(
            VerificationResult(
                status="not_recoverable",
                method="no_answer_to_verify",
                confidence=0.0,
                evidence={"question_preview": question_text[:200]},
            )
        )

    keys = _option_keys(options)
    selected_key = _candidate_key(answer_text, options, answer_type)
    if options and answer_type in {"mcq_option", "multiple_options"}:
        if selected_key is None:
            outcome_issue.append("answer_not_mappable_to_one_option")
        elif keys and not set(selected_key.split(",")) <= keys:
            outcome_issue.append("answer_key_not_in_options")

    official = bool(candidate and candidate.official)
    source_type = candidate.source_type if candidate else "unknown"
    independent = bool(independent_answer)

    if independent:
        if answers_equivalent(answer_text, independent_answer, options=options):
            if official:
                return VerificationOutcome(
                    VerificationResult(
                        status="official_verified_with_independent_check",
                        method="official_key_plus_independent_check",
                        confidence=0.99,
                        answer_text=answer_text,
                        evidence={
                            "official_source": candidate.source_name if candidate else None,
                            "independent_answer": independent_answer,
                            "option_key": selected_key,
                        },
                        independent=True,
                    )
                )
            return VerificationOutcome(
                VerificationResult(
                    status="independently_solved_verified",
                    method="independent_answer_agreement",
                    confidence=0.93,
                    answer_text=answer_text,
                    evidence={
                        "independent_answer": independent_answer,
                        "option_key": selected_key,
                    },
                    independent=True,
                )
            )
        return VerificationOutcome(
            VerificationResult(
                status="conflicting_evidence",
                method="independent_answer_disagreement",
                confidence=0.9,
                answer_text=answer_text,
                evidence={
                    "independent_answer": independent_answer,
                    "option_key": selected_key,
                },
                issues=["independent solution disagrees with candidate"],
                independent=True,
            ),
            ["independent solution disagrees with candidate"],
        )

    if official:
        # An official key is accepted only after the answer can be associated
        # with the question's option set. No option reordering is inferred.
        if options and answer_type in {"mcq_option", "multiple_options"} and selected_key is None:
            return VerificationOutcome(
                VerificationResult(
                    status="conflicting_evidence",
                    method="official_key_option_mismatch",
                    confidence=0.8,
                    answer_text=answer_text,
                    evidence={"official_source": candidate.source_name if candidate else None},
                    issues=["official answer cannot be mapped to an option"],
                ),
                ["official answer cannot be mapped to an option"],
            )
        return VerificationOutcome(
            VerificationResult(
                status="official_verified",
                method="official_answer_key_lookup",
                confidence=0.98,
                answer_text=answer_text,
                evidence={
                    "official_source": candidate.source_name if candidate else None,
                    "option_key": selected_key,
                    "limitation": "Official key verification does not prove option order was preserved after extraction.",
                },
            )
        )

    if source_type == "local_duplicate" and candidate is not None:
        courses = candidate.evidence.get("independent_courses") or []
        if len(set(courses)) >= 2:
            return VerificationOutcome(
                VerificationResult(
                    status="multiple_source_corroborated",
                    method="independent_local_courses_agree",
                    confidence=0.86,
                    answer_text=answer_text,
                    evidence={
                        "independent_courses": sorted(set(courses)),
                        "option_key": selected_key,
                    },
                    independent=True,
                )
            )
        return VerificationOutcome(
            VerificationResult(
                status="review_required",
                method="same_course_duplicate_only",
                confidence=0.58,
                answer_text=answer_text,
                evidence={"reason": "A same-course duplicate is not independent evidence."},
            )
        )

    if source_type == "deterministic_solver":
        from exam_engine.recovery.solver import recompute_numeric

        agrees, evidence = recompute_numeric(question_text, answer_text)
        if agrees:
            return VerificationOutcome(
                VerificationResult(
                    status="independently_solved_verified",
                    method="deterministic_recomputation_agrees",
                    confidence=0.78,
                    answer_text=answer_text,
                    evidence={
                        **evidence,
                        "note": "Deterministic recomputation is independent of AI and of the stored row; it is not an official key.",
                    },
                    independent=True,
                )
            )
        return VerificationOutcome(
            VerificationResult(
                status="conflicting_evidence",
                method="deterministic_recomputation_disagrees",
                confidence=0.72,
                answer_text=answer_text,
                evidence=evidence,
                issues=["deterministic recomputation disagrees with candidate"],
                independent=True,
            ),
            ["deterministic recomputation disagrees with candidate"],
        )

    if source_type == "ai_model":
        return VerificationOutcome(
            VerificationResult(
                status="ai_generated_unverified",
                method="model_output_without_independent_check",
                confidence=0.25,
                answer_text=answer_text,
                evidence={"reason": "AI output is a candidate, never an official or verified answer."},
            )
        )

    return VerificationOutcome(
        VerificationResult(
            status="source_stored_unverified",
            method="stored_source_answer_without_key_or_independent_check",
            confidence=0.45,
            answer_text=answer_text,
            evidence={
                "source_type": source_type,
                "option_key": selected_key,
                "limitation": "The answer exists in the dataset but was not checked against a key or independent solution.",
            },
            issues=outcome_issue,
        )
    )


def verification_counts(records: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = str((record.verification or {}).get("status") or "not_checked")
        counts[status] = counts.get(status, 0) + 1
    return counts


__all__ = [
    "OFFICIAL_STATUSES",
    "VERIFIED_STATUSES",
    "VerificationOutcome",
    "answers_equivalent",
    "verification_counts",
    "verify_answer",
]
