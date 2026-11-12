"""Records for the dataset intelligence and recovery subsystem.

The recovery layer never edits raw CSV or PDF files. It produces a separate,
versioned analysis record: the original values, a normalized view, proposed
repairs, source evidence, answer candidates and a release decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

RECOVERY_VERSION = "recovery.v1.2"
RULE_REGISTRY_VERSION = "rules.v2"

SEVERITIES = ("error", "warning", "info")

#: Transparent component weights. They deliberately give more weight to
#: completeness, answers, options and structure than to metadata.
QUALITY_WEIGHTS = {
    "structural_integrity": 0.17,
    "question_completeness": 0.11,
    "classification_accuracy": 0.07,
    "passage_subquestion_integrity": 0.10,
    "option_integrity": 0.12,
    "answer_completeness": 0.12,
    "answer_correctness": 0.11,
    "source_fidelity": 0.08,
    "mathematical_integrity": 0.07,
    "metadata_consistency": 0.03,
    "referential_integrity": 0.02,
}

ANSWER_STATUSES = (
    "official_verified",
    "official_verified_with_independent_check",
    "multiple_source_corroborated",
    "independently_solved_verified",
    "corroborated_local",
    "local_duplicate",
    "structured_extraction",
    "source_stored_unverified",
    "source_lead_unverified",
    "ai_generated_unverified",
    "conflicting",
    "conflicting_evidence",
    "review_required",
    "not_recoverable",
)

RELEASE_STATUSES = ("released", "review_required", "quarantined")
COURSE_STATUSES = (
    "ready",
    "review_required",
    "blocked",
    "unverifiable_source",
    "insufficient_evidence",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Issue:
    code: str
    category: str
    severity: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    remediation: str = ""
    blocking: bool = False
    field: str | None = None


@dataclass
class Repair:
    repair_type: str
    field: str
    before: Any
    after: Any
    method: str
    confidence: float
    risk: str = "low"
    status: str = "proposed"
    reason: str = ""
    reviewer: str | None = None
    note: str | None = None


@dataclass
class AnswerCandidate:
    answer_text: str | None
    source_type: str
    source_name: str
    verification_status: str
    confidence: float
    method: str
    source_url: str | None = None
    source_reference: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    retrieved_at: str = field(default_factory=now_iso)
    official: bool = False


@dataclass
class SourceDocument:
    source_id: str
    course_id: str
    path: str
    kind: str
    sha256: str
    page_count: int | None = None
    text_chars: int | None = None
    question_fidelity_available: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceCandidate:
    """A discovered source that has not been accepted as answer evidence."""

    source_id: str
    course_id: str
    title: str
    url: str
    document_type: str
    source_tier: int
    reliability: float
    provider: str = "unknown"
    snippet: str = ""
    query: str = ""
    classification_reason: str = ""
    retrieval_status: str = "discovered"
    discovered_at: str = field(default_factory=now_iso)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PassageGroup:
    """A shared passage/case-study context and its associated records."""

    passage_id: str
    course_id: str
    passage_text: str
    child_record_ids: list[str] = field(default_factory=list)
    parent_record_id: str | None = None
    source_reference: str | None = None
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    status: str
    method: str
    confidence: float
    answer_text: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    independent: bool = False


@dataclass
class RecordResult:
    record_id: str
    course_id: str
    source_file: str
    source_row: int
    source_question_id: str
    original: dict[str, Any]
    normalized: dict[str, Any]
    repaired: dict[str, Any]
    structure: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    answer_candidates: list[AnswerCandidate] = field(default_factory=list)
    answer_status: str = "source_stored_unverified"
    source_fidelity: str = "unverifiable"
    source_fidelity_detail: dict[str, Any] = field(default_factory=dict)
    release_status: str = "review_required"
    quality: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    analysis_hash: str = ""

    @property
    def blocking_count(self) -> int:
        return sum(1 for issue in self.issues if issue.blocking)

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blocking_count"] = self.blocking_count
        payload["issue_count"] = self.issue_count
        return payload


@dataclass
class CourseResult:
    course_id: str
    course_code: str
    course_name: str
    category_id: str
    source_file: str
    source_sha256: str
    schema: dict[str, Any]
    row_count: int
    analyzed_count: int
    status: str
    records: list[RecordResult] = field(default_factory=list)
    source_candidates: list[SourceCandidate] = field(default_factory=list)
    passages: list[PassageGroup] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("records", None)
        if include_records:
            payload["records"] = [record.as_dict() for record in self.records]
        payload["source_candidates"] = [asdict(item) for item in self.source_candidates]
        payload["passages"] = [asdict(item) for item in self.passages]
        return payload


@dataclass
class RunResult:
    run_id: str
    started_at: str
    finished_at: str
    mode: str
    dry_run: bool
    root: str
    courses: list[CourseResult]
    totals: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0
    status: str = "ok"

    def as_dict(self, *, include_courses: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if not include_courses:
            payload.pop("courses", None)
        else:
            payload["courses"] = [course.as_dict() for course in self.courses]
        return payload


def make_record_id(course_id: str, source_file: str, source_question_id: str, source_row: int) -> str:
    import hashlib

    raw = f"{course_id}\x1f{source_file}\x1f{source_question_id}\x1f{source_row}"
    return "rec_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "ANSWER_STATUSES",
    "COURSE_STATUSES",
    "QUALITY_WEIGHTS",
    "RECOVERY_VERSION",
    "RELEASE_STATUSES",
    "RULE_REGISTRY_VERSION",
    "SEVERITIES",
    "AnswerCandidate",
    "CourseResult",
    "Issue",
    "PassageGroup",
    "RecordResult",
    "Repair",
    "RunResult",
    "SourceCandidate",
    "SourceDocument",
    "VerificationResult",
    "make_record_id",
    "now_iso",
]
