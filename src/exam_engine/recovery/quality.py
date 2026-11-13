"""Calibrated, explainable quality scores and release policy.

A high structural score must not be mistaken for verified educational
correctness. The course score is therefore accompanied by separate structural,
answer-verification, source-verification and release-readiness indicators.
Missing evidence is treated as missing evidence, not as proof of an error.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from exam_engine.models.recovery import (
    QUALITY_WEIGHTS,
    Issue,
    RecordResult,
)

# Answer-point values are only one component. A stored-but-unverified answer is
# deliberately close to a recovered-but-unverified local duplicate; neither is
# educational proof.
_ANSWER_SCORE = {
    "official_verified": 100.0,
    "official_verified_with_independent_check": 100.0,
    "multiple_source_corroborated": 90.0,
    "independently_solved_verified": 88.0,
    "corroborated_local": 88.0,
    "local_duplicate": 66.0,
    "structured_extraction": 74.0,
    "source_stored_unverified": 52.0,
    "source_lead_unverified": 20.0,
    "ai_generated_unverified": 30.0,
    "conflicting": 8.0,
    "conflicting_evidence": 8.0,
    "review_required": 35.0,
    "not_recoverable": 0.0,
}
_SOURCE_SCORE = {
    "exact": 95.0,
    "formatting_normalized": 85.0,
    "partial_or_meaning_change_risk": 62.0,
    "unsupported_or_not_traceable": 15.0,
    "unverifiable": 50.0,
}
VERIFIED_ANSWER_STATUSES = frozenset(
    {
        "official_verified",
        "official_verified_with_independent_check",
        "multiple_source_corroborated",
        "independently_solved_verified",
        "corroborated_local",
    }
)
UNVERIFIED_ANSWER_STATUSES = frozenset(
    {
        "source_stored_unverified",
        "local_duplicate",
        "structured_extraction",
        "source_lead_unverified",
        "ai_generated_unverified",
        "review_required",
    }
)
CONFLICT_STATUSES = frozenset({"conflicting", "conflicting_evidence"})
SOURCE_VERIFIED_FIDELITY = frozenset({"exact", "formatting_normalized"})

#: Explicit release thresholds. They are product policy, not inferred from a
#: single score, because a high score cannot make an unverified answer official.
RELEASE_THRESHOLDS = {
    "minimum_overall": 80.0,
    "minimum_structural_quality": 85.0,
    "minimum_answer_verification_coverage": 0.95,
    "minimum_source_verification_coverage": 0.90,
    "maximum_blocking_issues": 0,
    "maximum_conflicts": 0,
}


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _category_score(
    issues: list[Issue],
    category: str,
    *,
    base: float = 100.0,
    error: float = 30.0,
    warning: float = 10.0,
    info: float = 2.0,
) -> float:
    value = base
    for issue in issues:
        if issue.category != category:
            continue
        value -= error if issue.severity == "error" else warning if issue.severity == "warning" else info
    return _clamp(value)


def _has(issues: list[Issue], *codes: str) -> bool:
    wanted = set(codes)
    return any(issue.code in wanted for issue in issues)


def release_decision(answer_status: str, issues: list[Issue]) -> str:
    """Backward-compatible status-only release decision."""

    return release_assessment_for(answer_status, issues)["status"]


def release_assessment_for(answer_status: str, issues: list[Issue]) -> dict[str, Any]:
    """Explain a record release decision with explicit evidence reasons."""

    reasons: list[str] = []
    blocking = [issue for issue in issues if issue.blocking]
    if answer_status in CONFLICT_STATUSES:
        reasons.append("conflicting_answer_evidence")
    if answer_status == "not_recoverable":
        reasons.append("answer_not_recoverable")
    if blocking:
        reasons.extend(sorted({issue.code for issue in blocking}))
    if reasons:
        return {
            "status": "quarantined",
            "reasons": reasons,
            "blocking_issue_count": len(blocking),
        }
    if answer_status in VERIFIED_ANSWER_STATUSES:
        return {
            "status": "released",
            "reasons": ["verified_answer_evidence"],
            "blocking_issue_count": 0,
        }
    return {
        "status": "review_required",
        "reasons": ["answer_not_independently_verified", "manual_review_required"],
        "blocking_issue_count": 0,
    }


def release_assessment(record: RecordResult) -> dict[str, Any]:
    """Record-level release assessment including source and verification gates."""

    assessment = release_assessment_for(record.answer_status, record.issues)
    reasons = list(assessment["reasons"])
    verification_status = str((record.verification or {}).get("status") or "")
    if record.source_fidelity == "unverifiable":
        reasons.append("source_evidence_unverifiable")
    if verification_status in {"conflicting_evidence"}:
        reasons.append("verification_conflict")
    if verification_status in {"not_recoverable", "ai_generated_unverified"}:
        reasons.append(f"verification_{verification_status}")
    if assessment["status"] == "released" and (
        record.source_fidelity == "unverifiable"
        or verification_status in {"not_recoverable", "ai_generated_unverified"}
        or any(issue.blocking for issue in record.issues)
    ):
        assessment = {
            "status": "review_required",
            "reasons": sorted(set(reasons + ["release_threshold_not_met"])),
            "blocking_issue_count": assessment["blocking_issue_count"],
        }
    else:
        assessment["reasons"] = sorted(set(reasons))
    assessment["source_fidelity"] = record.source_fidelity
    assessment["verification_status"] = verification_status or None
    assessment["thresholds"] = dict(RELEASE_THRESHOLDS)
    return assessment


def score_record(record: RecordResult) -> dict[str, Any]:
    issues = record.issues
    normalized = record.normalized
    repaired = record.repaired
    question_text = str(repaired.get("question_text") or "").strip()
    answer_text = str(repaired.get("answer_text") or "").strip()
    marks = repaired.get("marks")

    structural = _category_score(issues, "structural_integrity", error=40, warning=18)
    if not question_text:
        structural = 0.0
    elif len(question_text.split()) < 4:
        structural = min(structural, 55.0)
    completeness = 100.0
    if not question_text:
        completeness -= 60
    if not marks:
        completeness -= 8
    if not repaired.get("question_id"):
        completeness -= 15
    if not normalized.get("question_type") or normalized.get("question_type") == "unknown":
        completeness -= 8
    completeness = _clamp(completeness)
    classification = 100.0
    if normalized.get("question_type") == "unknown":
        classification = 48.0
    elif _has(issues, "classification.passage_misclassified"):
        classification = 35.0
    passage = _category_score(issues, "passage_subquestion_integrity", warning=20)
    options = _category_score(issues, "option_integrity", warning=15, info=2)
    if _has(issues, "options.missing_for_mcq"):
        options = min(options, 45.0)
    if _has(issues, "options.duplicated_in_question_and_column"):
        options = min(options, 82.0)
    if _has(issues, "answer.key_not_in_options"):
        options = min(options, 30.0)
    answer_completeness = 100.0 if answer_text else 0.0
    if not answer_text and repaired.get("solution_steps"):
        answer_completeness = 30.0
    verification_status = str((record.verification or {}).get("status") or "")
    answer_correctness = _ANSWER_SCORE.get(
        verification_status, _ANSWER_SCORE.get(record.answer_status, 40.0)
    )
    # Verification of an answer does not upgrade an unavailable source.
    source_score = _SOURCE_SCORE.get(record.source_fidelity, 45.0)
    math_score = _category_score(issues, "mathematical_integrity", warning=12, info=2)
    metadata_score = _category_score(issues, "metadata_consistency", warning=7, info=1)
    referential_score = _category_score(issues, "referential_integrity", error=35, warning=12)
    components = {
        "structural_integrity": round(structural, 2),
        "question_completeness": round(completeness, 2),
        "classification_accuracy": round(classification, 2),
        "passage_subquestion_integrity": round(passage, 2),
        "option_integrity": round(options, 2),
        "answer_completeness": round(answer_completeness, 2),
        "answer_correctness": round(answer_correctness, 2),
        "source_fidelity": round(source_score, 2),
        "mathematical_integrity": round(math_score, 2),
        "metadata_consistency": round(metadata_score, 2),
        "referential_integrity": round(referential_score, 2),
    }
    overall = round(
        sum(components[name] * weight for name, weight in QUALITY_WEIGHTS.items()),
        2,
    )
    # These record-level caps are evidence gates, not course-level penalties.
    if record.source_fidelity == "unverifiable":
        overall = min(overall, 80.0)
    if record.answer_status in {"source_stored_unverified", "local_duplicate"}:
        overall = min(overall, 78.0)
    if record.answer_status in CONFLICT_STATUSES or record.answer_status == "not_recoverable":
        overall = min(overall, 35.0)
    overall = round(overall, 2)
    return {
        "components": components,
        "overall": overall,
        "weights": dict(QUALITY_WEIGHTS),
        "verification_status": verification_status or None,
        "evidence": {
            "blocking_issues": sum(1 for issue in issues if issue.blocking),
            "errors": sum(1 for issue in issues if issue.severity == "error"),
            "warnings": sum(1 for issue in issues if issue.severity == "warning"),
            "limitations": "Scores are evidence summaries, not claims of educational correctness.",
        },
    }


def _indicators(records: list[RecordResult], components: dict[str, float]) -> dict[str, Any]:
    total = len(records)
    if not total:
        return {
            "structural_quality": 0.0,
            "answer_verification_coverage": 0.0,
            "source_verification_coverage": 0.0,
            "release_readiness": "blocked",
        }
    structural_names = (
        "structural_integrity",
        "question_completeness",
        "classification_accuracy",
        "passage_subquestion_integrity",
        "option_integrity",
        "mathematical_integrity",
        "metadata_consistency",
        "referential_integrity",
    )
    structural_quality = sum(components.get(name, 0.0) for name in structural_names) / len(
        structural_names
    )
    verified = sum(
        1
        for record in records
        if record.answer_status in VERIFIED_ANSWER_STATUSES
        or (record.verification or {}).get("status") in VERIFIED_ANSWER_STATUSES
    )
    source_verified = sum(
        1 for record in records if record.source_fidelity in SOURCE_VERIFIED_FIDELITY
    )
    conflicts = sum(1 for record in records if record.answer_status in CONFLICT_STATUSES)
    blocking = sum(1 for record in records if record.blocking_count)
    all_unverifiable = all(record.source_fidelity == "unverifiable" for record in records)
    if conflicts:
        readiness = "blocked"
    elif verified == 0 and all_unverifiable:
        readiness = "insufficient_evidence"
    elif blocking or any(record.release_status != "released" for record in records):
        readiness = "review_required"
    else:
        readiness = "ready"
    return {
        "structural_quality": round(structural_quality, 2),
        "answer_verification_coverage": round(verified / total, 4),
        "source_verification_coverage": round(source_verified / total, 4),
        "release_readiness": readiness,
        "release_thresholds": dict(RELEASE_THRESHOLDS),
        "unresolved": sum(
            1 for record in records if not record.repaired.get("answer_text")
        ),
        "conflicts": conflicts,
        "blocking_records": blocking,
        "verified_records": verified,
    }


def course_quality(records: list[RecordResult], course_warnings: list[str]) -> dict[str, Any]:
    if not records:
        return {
            "overall": 0.0,
            "components": {name: 0.0 for name in QUALITY_WEIGHTS},
            "status": "blocked",
            "indicators": _indicators([], {name: 0.0 for name in QUALITY_WEIGHTS}),
            "counts": {},
            "warnings": course_warnings,
            "weights": dict(QUALITY_WEIGHTS),
            "limitations": "No records were available for scoring.",
        }
    components = {name: 0.0 for name in QUALITY_WEIGHTS}
    for record in records:
        for name, value in record.quality.get("components", {}).items():
            components[name] = components.get(name, 0.0) + float(value)
    components = {name: round(value / len(records), 2) for name, value in components.items()}
    overall = round(
        sum(components.get(name, 0.0) * weight for name, weight in QUALITY_WEIGHTS.items()), 2
    )
    # Source evidence is still missing for this corpus. The ``source_fidelity``
    # component and the separate source-verification indicator carry that gap;
    # a blanket course cap would erase genuine differences between courses.
    # Answer coverage is an explicit evidence gate. A structurally neat course
    # with most answers missing must not look nearly as complete as one whose
    # answers are present (though still unverified).
    answer_coverage = sum(
        1 for record in records if record.repaired.get("answer_text")
    ) / len(records)
    overall = min(overall, round(35.0 + 60.0 * answer_coverage, 2))
    # The old implementation capped every course at 69 merely because one
    # record was unrecoverable. Proportional missing-answer evidence is already
    # represented by the averaged components; only actual conflicts block.
    conflicts = sum(1 for record in records if record.answer_status in CONFLICT_STATUSES)
    if conflicts:
        overall = min(overall, 55.0)
    overall = round(overall, 2)
    codes = Counter(issue.code for record in records for issue in record.issues)
    verification_counts = Counter(
        str((record.verification or {}).get("status") or "not_checked") for record in records
    )
    recovered_records = [
        record
        for record in records
        if not str(record.normalized.get("answer_text") or "").strip()
        and str(record.repaired.get("answer_text") or "").strip()
    ]
    counts = {
        "records": len(records),
        "with_answer": sum(1 for record in records if record.repaired.get("answer_text")),
        "missing_answer": sum(1 for record in records if not record.repaired.get("answer_text")),
        "verified": sum(
            1
            for record in records
            if record.answer_status in VERIFIED_ANSWER_STATUSES
            or (record.verification or {}).get("status") in VERIFIED_ANSWER_STATUSES
        ),
        "officially_verified": sum(
            1
            for record in records
            if record.answer_status
            in {"official_verified", "official_verified_with_independent_check"}
            or (record.verification or {}).get("status")
            in {"official_verified", "official_verified_with_independent_check"}
        ),
        "independently_verified": sum(
            1
            for record in records
            if (record.verification or {}).get("status")
            in {"official_verified_with_independent_check", "independently_solved_verified"}
        ),
        "multiple_source_corroborated": sum(
            1
            for record in records
            if (record.verification or {}).get("status") == "multiple_source_corroborated"
        ),
        "unverified_answer": sum(
            1
            for record in records
            if record.repaired.get("answer_text")
            and (
                record.answer_status in UNVERIFIED_ANSWER_STATUSES
                or (record.verification or {}).get("status")
                in {"source_stored_unverified", "review_required", "ai_generated_unverified"}
            )
        ),
        "recovered_answers": len(recovered_records),
        "unverified_recoveries": sum(
            1
            for record in recovered_records
            if not (
                record.answer_status in VERIFIED_ANSWER_STATUSES
                or (record.verification or {}).get("status") in VERIFIED_ANSWER_STATUSES
            )
        ),
        "verified_recoveries": sum(
            1
            for record in recovered_records
            if record.answer_status in VERIFIED_ANSWER_STATUSES
            or (record.verification or {}).get("status") in VERIFIED_ANSWER_STATUSES
        ),
        "officially_verified_recoveries": sum(
            1
            for record in recovered_records
            if record.answer_status
            in {"official_verified", "official_verified_with_independent_check"}
            or (record.verification or {}).get("status")
            in {"official_verified", "official_verified_with_independent_check"}
        ),
        "independently_verified_recoveries": sum(
            1
            for record in recovered_records
            if (record.verification or {}).get("status")
            in {"official_verified_with_independent_check", "independently_solved_verified"}
        ),
        "ai_generated": sum(
            1 for record in records if record.answer_status == "ai_generated_unverified"
        ),
        "repaired": sum(1 for record in records if record.repairs),
        "rejected": 0,
        "conflicting": conflicts,
        "unresolved": sum(1 for record in records if not record.repaired.get("answer_text")),
        "review_required": sum(1 for record in records if record.release_status == "review_required"),
        "quarantined": sum(1 for record in records if record.release_status == "quarantined"),
        "released": sum(1 for record in records if record.release_status == "released"),
        "potentially_fabricated_or_unsupported": sum(
            1
            for record in records
            if any(
                issue.code.startswith(("authenticity.", "source.unsupported"))
                for issue in record.issues
            )
        ),
        "duplicate_records": sum(
            1
            for record in records
            if any(
                issue.code in {"referential.duplicate_question", "referential.duplicate_question_id"}
                for issue in record.issues
            )
        ),
        "structure_errors": sum(
            1
            for record in records
            if any(
                issue.category in {"structural_integrity", "passage_subquestion_integrity"}
                and issue.severity in {"error", "warning"}
                for issue in record.issues
            )
        ),
        "issue_counts": dict(sorted(codes.items())),
        "by_verification_status": dict(sorted(verification_counts.items())),
    }
    blocking = sum(1 for record in records if record.blocking_count)
    all_unverifiable = all(record.source_fidelity == "unverifiable" for record in records)
    if conflicts:
        status = "blocked"
    elif all_unverifiable and counts["verified"] == 0:
        status = "insufficient_evidence"
    elif blocking:
        status = "review_required"
    elif counts["quarantined"] or counts["review_required"]:
        status = "review_required"
    else:
        status = "ready"
    indicators = _indicators(records, components)
    if indicators["release_readiness"] == "insufficient_evidence" and status == "review_required":
        status = "insufficient_evidence"
    release_reasons: list[str] = []
    if conflicts:
        release_reasons.append("conflicting_answer_evidence")
    if counts["unresolved"]:
        release_reasons.append("unresolved_answers")
    if blocking:
        release_reasons.append("blocking_issues")
    if all_unverifiable:
        release_reasons.append("source_evidence_unverifiable")
    if counts["verified"] == 0:
        release_reasons.append("no_verified_answers")
    if counts["quarantined"]:
        release_reasons.append("quarantined_records")
    if counts["review_required"]:
        release_reasons.append("records_require_review")
    release_status = (
        "ready"
        if status == "ready" and not release_reasons
        else "blocked"
        if conflicts
        else "insufficient_evidence"
        if all_unverifiable and counts["verified"] == 0
        else "review_required"
    )
    release = {
        "status": release_status,
        "reasons": sorted(set(release_reasons)),
        "record_counts": {
            "released": counts["released"],
            "review_required": counts["review_required"],
            "quarantined": counts["quarantined"],
        },
        "thresholds": dict(RELEASE_THRESHOLDS),
    }
    return {
        "overall": overall,
        "components": components,
        "status": status,
        "evidence_status": indicators["release_readiness"],
        "indicators": indicators,
        "release": release,
        "counts": counts,
        "warnings": course_warnings,
        "weights": dict(QUALITY_WEIGHTS),
        "limitations": (
            "Source fidelity is unverifiable when no original question paper or official "
            "answer key is present. Missing evidence lowers coverage; it does not prove "
            "that an answer or question is wrong."
        ),
    }


__all__ = [
    "CONFLICT_STATUSES",
    "RELEASE_THRESHOLDS",
    "SOURCE_VERIFIED_FIDELITY",
    "UNVERIFIED_ANSWER_STATUSES",
    "VERIFIED_ANSWER_STATUSES",
    "course_quality",
    "release_decision",
    "score_record",
]
