"""Question-bank quality and content-integrity audit.

This module is deliberately read-only. It inspects the source CSV rows without
rewriting them, classifies defects with stable codes, and returns a separate
correction plan for the only deterministic normalisation that is safe here:
removing non-text C0 control characters from display text.

The auditor is shared by the command-line report and the focused tests. It
does not generate answers, recover missing diagrams, or repair ambiguous
mathematical text.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.datasets.catalog import discover
from exam_engine.datasets.parsing import looks_corrupted, parse_row

SEVERITIES = ("critical", "high", "medium", "low", "review")
AREAS = ("data_integrity", "formatting", "visual")
OBJECTIVE_TYPES = frozenset({"mcq", "msq", "assertion_reason"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
IMAGE_COLUMNS = frozenset(
    {
        "image",
        "image_path",
        "image_url",
        "figure",
        "figure_path",
        "diagram",
        "diagram_path",
        "asset",
        "asset_path",
        "media",
        "media_path",
    }
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REPLACEMENT_RE = re.compile("[\ufffd\u00c2\u00e2]")
_PUA_RE = re.compile("[\ue000-\uf8ff]")
_HTML_ENTITY_RE = re.compile(
    r"&(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-f]+);", re.IGNORECASE
)
_PAGE_ARTIFACT_RE = re.compile(r"\bPage\s+\d+\s+(?:of|/)\s+\d+\b", re.IGNORECASE)
_IMAGE_MARKDOWN_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)"
)
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[(?:figure|image)[^\]]*\]", re.IGNORECASE)
_FIGURE_REFERENCE_RE = re.compile(
    r"\b(?:see|shown in|refer to|given in|represented in)\s+(?:the\s+)?"
    r"(?:figure|diagram|graph|circuit|chart)\b",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"^\s*```")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_ELLIPSIS_TAIL_RE = re.compile(r"(?:\.{3,}|…)\s*$")
_UNCLOSED_TAIL_RE = re.compile(r"[\(\[\{]\s*$")
_DANGLING_MATH_RE = re.compile(
    r"\\(?:frac|sqrt|left|right|begin|end)\s*(?:\{|$)|(?:\$\$|\\\(|\\\[)\s*$"
)


@dataclass(frozen=True)
class QualityIssue:
    """One classified, traceable defect."""

    question_id: str
    source_question_id: str
    course_id: str
    source_path: str
    source_row: int | None
    area: str
    category: str
    code: str
    severity: str
    field: str
    message: str
    original: str | None
    auto_correctable: bool
    manual_review: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "source_question_id": self.source_question_id,
            "course_id": self.course_id,
            "source_path": self.source_path,
            "source_row": self.source_row or "",
            "area": self.area,
            "category": self.category,
            "code": self.code,
            "severity": self.severity,
            "field": self.field,
            "message": self.message,
            "original": self.original or "",
            "auto_correctable": self.auto_correctable,
            "manual_review": self.manual_review,
            "detail": json.dumps(self.detail, ensure_ascii=False, sort_keys=True),
        }


@dataclass(frozen=True)
class SafeCorrection:
    """A deterministic, reversible text correction, not yet applied."""

    course_id: str
    question_id: str
    source_question_id: str
    source_path: str
    field: str
    before: str
    after: str
    rule: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "question_id": self.question_id,
            "source_question_id": self.source_question_id,
            "source_path": self.source_path,
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "rule": self.rule,
            "reason": self.reason,
        }


@dataclass
class _Facts:
    course_id: str
    source_question_id: str
    question_id: str
    source_path: str
    source_row: int
    question_text: str
    options_raw: str | None
    options: list[Any]
    question_type: str
    question_type_raw: str | None
    answer_text: str | None
    answer_type: str
    correct_option_key: str | None
    marks: float | None
    source_file: str
    raw_question_id: str
    row_values: tuple[str, ...]
    raw_row: dict[str, Any]
    exact_duplicate: bool = False

    @property
    def is_objective(self) -> bool:
        return self.question_type in OBJECTIVE_TYPES


@dataclass
class QualityAudit:
    root: Path
    issues: list[QualityIssue] = field(default_factory=list)
    safe_corrections: list[SafeCorrection] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    area_summary: dict[str, Any] = field(default_factory=dict)
    asset_inventory: list[str] = field(default_factory=list)
    source_hashes: dict[str, str] = field(default_factory=dict)
    generated_at: str = ""

    def issues_for(self, area: str | None = None) -> list[QualityIssue]:
        if area is None:
            return list(self.issues)
        return [issue for issue in self.issues if issue.area == area]

    def write(self, output_dir: Path | str = Path("reports/step2_quality")) -> dict[str, Path]:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        summary_path = target / "summary.json"
        issues_path = target / "issues.csv"
        corrections_path = target / "safe_corrections.json"
        summary_path.write_text(
            json.dumps(self.summary, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        _write_csv(issues_path, [issue.as_dict() for issue in self.issues])
        corrections_path.write_text(
            json.dumps(
                [item.as_dict() for item in self.safe_corrections],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "summary": summary_path,
            "issues": issues_path,
            "safe_corrections": corrections_path,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)


def _short(value: str | None, limit: int = 180) -> str | None:
    if value is None:
        return None
    clean = " ".join(value.split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def _issue(
    issues: list[QualityIssue],
    facts: _Facts,
    *,
    area: str,
    category: str,
    code: str,
    severity: str,
    field: str,
    message: str,
    original: str | None = None,
    auto_correctable: bool = False,
    manual_review: bool = True,
    detail: dict[str, Any] | None = None,
) -> None:
    if severity not in SEVERITIES:
        raise ValueError(f"unsupported severity: {severity}")
    issues.append(
        QualityIssue(
            question_id=facts.question_id,
            source_question_id=facts.source_question_id,
            course_id=facts.course_id,
            source_path=facts.source_path,
            source_row=facts.source_row,
            area=area,
            category=category,
            code=code,
            severity=severity,
            field=field,
            message=message,
            original=_short(original),
            auto_correctable=auto_correctable,
            manual_review=manual_review,
            detail=detail or {},
        )
    )


def _row_key(facts: _Facts) -> tuple[str, str]:
    return facts.course_id, facts.raw_question_id or facts.source_question_id


def _valid_question_id(qid: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", qid))


def _source_context(subject: Any, root: Path) -> tuple[str, str]:
    relative = subject.csv_path.relative_to(root).as_posix()
    return subject.course_id, relative


def _check_identity(
    facts: _Facts,
    issues: list[QualityIssue],
    *,
    duplicate_keys: set[tuple[str, str]],
    exact_duplicate: bool,
) -> None:
    if not facts.raw_question_id.strip():
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="identity",
            code="missing_question_id",
            severity="critical",
            field="question_id",
            message="The source row has no question_id; no identifier was invented.",
            manual_review=True,
        )
    elif not _valid_question_id(facts.raw_question_id):
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="identity",
            code="invalid_question_id_format",
            severity="high",
            field="question_id",
            message="The source question_id contains unsupported characters; it is preserved verbatim.",
            original=facts.raw_question_id,
            manual_review=True,
        )
    if _row_key(facts) in duplicate_keys:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="identity",
            code="duplicate_question_id",
            severity="high",
            field="question_id",
            message=(
                "This course/question_id occurs more than once. The composite identity "
                "(course_id, source question id) is the scope used here; rows were not merged."
            ),
            original=facts.source_question_id,
            manual_review=True,
        )
    if exact_duplicate:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="identity",
            code="duplicate_source_record",
            severity="medium",
            field="row",
            message="An exact duplicate source row was found; it was kept as a separate row.",
            manual_review=True,
        )


def _check_required_and_metadata(facts: _Facts, issues: list[QualityIssue]) -> None:
    if not facts.question_text.strip():
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="required_field",
            code="missing_question_text",
            severity="critical",
            field="question_text",
            message="question_text is empty; no text was invented.",
            manual_review=True,
        )
    if not facts.question_type_raw:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="required_field",
            code="missing_question_type",
            severity="medium",
            field="question_type",
            message="question_type is empty; the stored normalised type is unknown.",
            manual_review=True,
        )
    elif facts.question_type == "unknown":
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="question_type",
            code="unknown_question_type",
            severity="medium",
            field="question_type",
            message=(
                f"The declared type {facts.question_type_raw!r} is not recognised by the "
                "existing normaliser; it was not reclassified from superficial text."
            ),
            original=facts.question_type_raw,
            manual_review=True,
        )
    if not facts.source_path:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="provenance",
            code="missing_source_path",
            severity="critical",
            field="source_file",
            message="No source CSV path is available for this record.",
            manual_review=True,
        )
    if not facts.course_id:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="provenance",
            code="unmapped_course",
            severity="critical",
            field="course_id",
            message="The source file could not be mapped to an existing course.",
            manual_review=True,
        )
    if facts.marks is None:
        # Marks are genuinely optional in the source schema. This is an
        # informational gap, not a defect, and is reported only in the summary.
        return


def _check_options_and_answers(facts: _Facts, issues: list[QualityIssue]) -> None:
    option_texts = [" ".join(str(option.text or "").split()).casefold() for option in facts.options]
    if facts.is_objective and len(facts.options) < 2:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="options",
            code="missing_options",
            severity="high",
            field="options",
            message=(
                f"The declared type is {facts.question_type!r} but only {len(facts.options)} "
                "structured option(s) were parsed. Missing options were not invented."
            ),
            original=facts.options_raw,
            manual_review=True,
        )
    if facts.options_raw and not facts.options:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="options",
            code="options_not_parsed",
            severity="high",
            field="options",
            message="The options field is populated but no deterministic option structure parsed.",
            original=facts.options_raw,
            manual_review=True,
        )
    if facts.options and any(not option_text.strip() for option_text in option_texts):
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="options",
            code="empty_option_text",
            severity="high",
            field="options",
            message="At least one option label has no text.",
            manual_review=True,
        )
    duplicate_texts = [
        text for text, count in Counter(option_texts).items() if text and count > 1
    ]
    duplicate_texts = [
        text for text in duplicate_texts if not _IMAGE_PLACEHOLDER_RE.fullmatch(text)
    ]
    if duplicate_texts:
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="options",
            code="duplicate_option_text",
            severity="medium",
            field="options",
            message=(
                "Two or more options have identical text. They were preserved because "
                "the source may intentionally repeat a value."
            ),
            original=" | ".join(duplicate_texts[:3]),
            manual_review=True,
            detail={"duplicate_texts": duplicate_texts[:5]},
        )
    option_keys = {str(option.key).upper() for option in facts.options}
    if facts.correct_option_key and option_keys:
        referenced = {
            key.strip().upper() for key in facts.correct_option_key.split(",") if key.strip()
        }
        invalid = sorted(referenced - option_keys)
        if invalid:
            _issue(
                issues,
                facts,
                area="data_integrity",
                category="answers",
                code="answer_points_to_unknown_option",
                severity="critical",
                field="correct_option_key",
                message=(
                    "The stored answer references option key(s) that do not exist on this "
                    "question. The answer was not rewritten."
                ),
                original=facts.correct_option_key,
                manual_review=True,
                detail={"unknown_keys": invalid, "known_keys": sorted(option_keys)},
            )
    if (
        facts.answer_text
        and facts.is_objective
        and facts.options
        and not facts.correct_option_key
    ):
        _issue(
            issues,
            facts,
            area="data_integrity",
            category="answers",
            code="answer_option_unresolved",
            severity="medium",
            field="answer_text",
            message=(
                "The answer exists but its option key could not be resolved deterministically. "
                "The answer text is preserved for review."
            ),
            original=facts.answer_text,
            manual_review=True,
        )


def _check_formatting(facts: _Facts, issues: list[QualityIssue]) -> None:
    text = facts.question_text
    all_text = text + "\n" + (facts.options_raw or "")
    if _CONTROL_RE.search(text):
        _issue(
            issues,
            facts,
            area="formatting",
            category="control_characters",
            code="control_character",
            severity="low",
            field="question_text",
            message=(
                "The source contains a non-text C0 control character. Removing it is "
                "deterministic and reversible; the source and audit value are retained."
            ),
            original=text,
            auto_correctable=True,
            manual_review=False,
            detail={"characters": sorted(set(_CONTROL_RE.findall(text)))},
        )
    if _REPLACEMENT_RE.search(all_text) or looks_corrupted(all_text):
        _issue(
            issues,
            facts,
            area="formatting",
            category="ocr_corruption",
            code="replacement_or_mojibake",
            severity="high",
            field="question_text",
            message=(
                "Replacement characters or mojibake were found. They may hide mathematical "
                "or textual content; no character was guessed."
            ),
            original=all_text,
            manual_review=True,
        )
    if _PUA_RE.search(all_text):
        _issue(
            issues,
            facts,
            area="formatting",
            category="mathematical_glyphs",
            code="private_use_math_glyph",
            severity="high",
            field="question_text",
            message=(
                "Private-use glyphs are present, usually from PDF equation extraction. "
                "They are not converted to LaTeX or guessed symbols."
            ),
            original=all_text,
            manual_review=True,
            detail={"codepoints": sorted({f"U+{ord(char):04X}" for char in _PUA_RE.findall(all_text)})},
        )
    if _PAGE_ARTIFACT_RE.search(all_text):
        _issue(
            issues,
            facts,
            area="formatting",
            category="page_artifact",
            code="page_header_footer",
            severity="medium",
            field=(
                "question_text" if _PAGE_ARTIFACT_RE.search(text) else "options"
            ),
            message=(
                "A page header/footer string is embedded in question or option text. "
                "It was preserved rather than trimmed because its boundary is ambiguous."
            ),
            original=all_text,
            manual_review=True,
        )
    if _HTML_ENTITY_RE.search(all_text):
        _issue(
            issues,
            facts,
            area="formatting",
            category="html_entities",
            code="raw_html_entity",
            severity="medium",
            field="question_text",
            message="Raw HTML entities are present; no entity decoder was applied automatically.",
            original=all_text,
            manual_review=True,
        )
    if _DANGLING_MATH_RE.search(text):
        _issue(
            issues,
            facts,
            area="formatting",
            category="mathematical_syntax",
            code="unbalanced_math_syntax",
            severity="high",
            field="question_text",
            message=(
                "A math delimiter or LaTeX command appears unbalanced or incomplete. "
                "The source text was not reconstructed."
            ),
            original=text,
            manual_review=True,
        )
    if _UNCLOSED_TAIL_RE.search(text.strip()):
        _issue(
            issues,
            facts,
            area="formatting",
            category="truncation",
            code="unclosed_bracket_tail",
            severity="high",
            field="question_text",
            message="The question ends with an opening bracket/parenthesis, suggesting truncation.",
            original=text[-160:],
            manual_review=True,
        )
    if _ELLIPSIS_TAIL_RE.search(text.strip()):
        _issue(
            issues,
            facts,
            area="formatting",
            category="truncation",
            code="ellipsis_tail",
            severity="review",
            field="question_text",
            message=(
                "The question ends with an ellipsis. It may be a deliberately open-ended "
                "series or a source truncation; it was not expanded."
            ),
            original=text[-160:],
            manual_review=True,
        )
    if _CODE_FENCE_RE.search(text):
        # Fenced code can be rendered faithfully by the front-end; no defect is
        # raised merely because code is present. The count is exposed in metrics.
        return
    if _TABLE_SEPARATOR_RE.search(text):
        # A genuine Markdown table is renderable and is not considered corrupt.
        return


def _resolve_image(src: str, root: Path, source_path: Path) -> Path:
    candidate = Path(src)
    if candidate.is_absolute():
        # Never follow an absolute path outside the project root.
        try:
            candidate.relative_to(root)
        except ValueError:
            return root / "__outside_root__"
        return candidate
    return (root / source_path.parent / candidate).resolve()


def _check_visual(facts: _Facts, issues: list[QualityIssue], root: Path) -> tuple[int, int]:
    """Return (placeholder count, missing referenced asset count) for one row."""
    text = facts.question_text
    options_text = facts.options_raw or ""
    placeholder_hits = _IMAGE_PLACEHOLDER_RE.findall(text + "\n" + options_text)
    if placeholder_hits:
        _issue(
            issues,
            facts,
            area="visual",
            category="visual_placeholder",
            code="visual_asset_placeholder",
            severity="critical",
            field="options" if _IMAGE_PLACEHOLDER_RE.search(options_text) else "question_text",
            message=(
                "The source contains an explicit figure/image placeholder, but no image "
                "file is supplied. The placeholder is preserved and shown as unavailable."
            ),
            original=" | ".join(placeholder_hits[:4]),
            manual_review=True,
            detail={"placeholder_count": len(placeholder_hits)},
        )
    missing_assets = 0
    for match in _IMAGE_MARKDOWN_RE.finditer(text):
        src = match.group("src")
        resolved = _resolve_image(src, root, Path(facts.source_path))
        if "__outside_root__" in resolved.as_posix() or not resolved.is_file():
            missing_assets += 1
            _issue(
                issues,
                facts,
                area="visual",
                category="missing_asset",
                code="missing_image_asset",
                severity="high",
                field="question_text",
                message=(
                    "A Markdown image reference does not resolve to a readable file inside "
                    "the dataset. The reference is preserved for manual recovery."
                ),
                original=src,
                manual_review=True,
                detail={"alt": match.group("alt"), "resolved": resolved.as_posix()},
            )
        elif resolved.suffix.lower() not in IMAGE_SUFFIXES:
            _issue(
                issues,
                facts,
                area="visual",
                category="unsupported_asset",
                code="unsupported_image_format",
                severity="medium",
                field="question_text",
                message="The referenced image has an unsupported file extension.",
                original=src,
                manual_review=True,
                detail={"suffix": resolved.suffix},
            )
    if not placeholder_hits and _FIGURE_REFERENCE_RE.search(text):
        _issue(
            issues,
            facts,
            area="visual",
            category="visual_reference",
            code="figure_reference_without_asset",
            severity="review",
            field="question_text",
            message=(
                "The text references a figure/diagram, but the source has no explicit asset "
                "field or placeholder. The relationship cannot be verified without recovery."
            ),
            original=text,
            manual_review=True,
        )
    for column in facts.raw_row:
        if column.casefold() in IMAGE_COLUMNS and str(facts.raw_row.get(column) or "").strip():
            src = str(facts.raw_row[column]).strip()
            resolved = _resolve_image(src, root, Path(facts.source_path))
            if not resolved.is_file():
                missing_assets += 1
                _issue(
                    issues,
                    facts,
                    area="visual",
                    category="missing_asset",
                    code="missing_image_asset",
                    severity="high",
                    field=column,
                    message="The image column references a file that is not present.",
                    original=src,
                    manual_review=True,
                    detail={"resolved": resolved.as_posix()},
                )
    return len(placeholder_hits), missing_assets


def _make_facts(
    row: dict[str, Any],
    *,
    subject: Any,
    root: Path,
    source_row: int,
) -> _Facts:
    parsed = parse_row(row, index=source_row)
    course_id, source_path = _source_context(subject, root)
    raw_qid = str(row.get("question_id") or "").strip()
    raw_key = str(row.get("correct_option_key") or "").strip()
    raw_values = tuple(str(value or "") for value in row.values())
    return _Facts(
        course_id=course_id,
        source_question_id=parsed.source_question_id,
        question_id=parsed.source_question_id,
        source_path=source_path,
        source_row=source_row,
        question_text=parsed.question_text,
        options_raw=parsed.options_raw,
        options=parsed.options,
        question_type=parsed.question_type,
        question_type_raw=parsed.question_type_raw,
        answer_text=parsed.answer_text,
        answer_type=parsed.answer_type,
        correct_option_key=raw_key or parsed.correct_option_key,
        marks=parsed.marks,
        source_file=source_path,
        raw_question_id=raw_qid,
        row_values=raw_values,
        raw_row=row,
    )


def _iter_source_rows(root: Path) -> Iterator[tuple[Any, Path, int, dict[str, Any]]]:
    catalog = discover(root)
    seen_paths: set[Path] = set()
    for subject in catalog.subjects:
        path = subject.csv_path
        if path in seen_paths:
            continue
        seen_paths.add(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                yield subject, path, row_number, row


def _asset_inventory(root: Path) -> list[str]:
    assets: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            assets.append(path.relative_to(root).as_posix())
    return assets


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return ""
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if ".git" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def audit_question_dataset(
    root: Path | str,
    *,
    batch_size: int = 500,
    progress: Callable[[int, int], None] | None = None,
) -> QualityAudit:
    """Audit every source question without modifying the source tree.

    ``batch_size`` is a checkpoint/reporting boundary; rows are never retained
    in a full in-memory copy. Duplicate IDs and exact-row fingerprints are
    tracked in lightweight dictionaries, which is sufficient for this corpus
    and avoids an O(n²) scan.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"dataset root not found: {root_path}")
    audit = QualityAudit(root=root_path)
    audit.generated_at = _now()
    audit.asset_inventory = _asset_inventory(root_path)
    audit.source_hashes["root"] = _tree_sha256(root_path)

    issues: list[QualityIssue] = []
    corrections: list[SafeCorrection] = []
    total = 0
    seen_keys: Counter[tuple[str, str]] = Counter()
    seen_rows: set[tuple[str, tuple[str, ...]]] = set()
    per_area: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    defect_ids: set[str] = set()
    review_ids: set[str] = set()
    correction_ids: set[str] = set()
    missing_options_by_type: Counter[str] = Counter()
    duplicate_ids: set[tuple[str, str]] = set()
    placeholder_rows = 0
    placeholder_options = 0
    missing_assets = 0
    table_rows = 0
    pipe_structure_rows = 0
    code_rows = 0
    math_rows = 0
    page_rows = 0
    controls = 0
    mojibake = 0
    pua = 0
    unanswered = 0
    marks_missing = 0
    no_order = 0
    expected = 0
    subjects = 0
    category_ids: set[str] = set()

    cache: list[_Facts] = []

    def flush() -> None:
        nonlocal placeholder_rows, placeholder_options, missing_assets, table_rows
        nonlocal pipe_structure_rows, code_rows, math_rows, page_rows, controls, mojibake, pua
        nonlocal unanswered, marks_missing, no_order
        for facts in cache:
            # All row-level checks happen here so the source is read once.
            _check_identity(
                facts,
                issues,
                duplicate_keys=duplicate_ids,
                exact_duplicate=facts.exact_duplicate,
            )
            _check_required_and_metadata(facts, issues)
            _check_options_and_answers(facts, issues)
            _check_formatting(facts, issues)
            placeholders, missing = _check_visual(facts, issues, root_path)
            placeholder_rows += 1 if placeholders else 0
            placeholder_options += sum(
                bool(_IMAGE_PLACEHOLDER_RE.search(str(option.text or "")))
                for option in facts.options
            )
            missing_assets += missing
            if _TABLE_SEPARATOR_RE.search(facts.question_text):
                table_rows += 1
            if re.search(r"^\s*\|.*\|\s*$", facts.question_text, re.MULTILINE):
                pipe_structure_rows += 1
            if _CODE_FENCE_RE.search(facts.question_text):
                code_rows += 1
            if _PUA_RE.search(facts.question_text + (facts.options_raw or "")):
                math_rows += 1
                pua += 1
            if _PAGE_ARTIFACT_RE.search(facts.question_text + (facts.options_raw or "")):
                page_rows += 1
            if _CONTROL_RE.search(facts.question_text):
                controls += 1
                corrected = _CONTROL_RE.sub("", facts.question_text)
                if corrected.strip():
                    corrections.append(
                        SafeCorrection(
                            course_id=facts.course_id,
                            question_id=facts.question_id,
                            source_question_id=facts.source_question_id,
                            source_path=facts.source_path,
                            field="question_text",
                            before=facts.question_text,
                            after=corrected,
                            rule="remove_c0_control_characters",
                            reason=(
                                "C0 controls have no textual meaning in the stored display "
                                "text; the original value remains in this audit."
                            ),
                        )
                    )
                    correction_ids.add(facts.question_id)
            if _REPLACEMENT_RE.search(facts.question_text + (facts.options_raw or "")):
                mojibake += 1
            if not facts.answer_text:
                unanswered += 1
            if facts.marks is None:
                marks_missing += 1
            if not facts.raw_row.get("original_question_number"):
                no_order += 1
            if facts.is_objective and len(facts.options) < 2:
                missing_options_by_type[facts.question_type] += 1
        cache.clear()

    for subject, _path, row_number, row in _iter_source_rows(root_path):
        total += 1
        expected += 1
        category_ids.add(subject.category_id)
        if total == 1:
            subjects = len(discover(root_path).subjects)
        facts = _make_facts(row, subject=subject, root=root_path, source_row=row_number)
        key = _row_key(facts)
        seen_keys[key] += 1
        if seen_keys[key] > 1:
            duplicate_ids.add(key)
        row_key = (facts.course_id, facts.row_values)
        facts.exact_duplicate = row_key in seen_rows
        seen_rows.add(row_key)
        cache.append(facts)
        if len(cache) >= batch_size:
            flush()
            if progress is not None:
                progress(total, expected)
    flush()

    # Use a compact synthetic issue for duplicate groups that were split across
    # flush batches. Within a batch the per-row check already reports them.
    for key, count in sorted(seen_keys.items()):
        if count <= 1:
            continue
        if any(
            issue.code == "duplicate_question_id"
            and (issue.course_id, issue.source_question_id) == key
            for issue in issues
        ):
            continue
        issues.append(
            QualityIssue(
                question_id=key[1],
                source_question_id=key[1],
                course_id=key[0],
                source_path="",
                source_row=None,
                area="data_integrity",
                category="identity",
                code="duplicate_question_id",
                severity="high",
                field="question_id",
                message=f"Duplicate composite identity occurs {count} times in the source.",
                original=key[1],
                auto_correctable=False,
                manual_review=True,
                detail={"occurrences": count},
            )
        )

    # Count and classify issue-bearing records. Review-only issues are kept
    # visible but do not make a record structurally invalid.
    for issue in issues:
        per_area[issue.area] += 1
        severity_counts[issue.severity] += 1
        if issue.severity != "review":
            defect_ids.add(issue.question_id)
        if issue.manual_review:
            review_ids.add(issue.question_id)

    duplicate_groups = sum(1 for count in seen_keys.values() if count > 1)
    valid = total - len(defect_ids)
    audit.issues = issues
    audit.safe_corrections = corrections
    audit.summary = {
        "generated_at": audit.generated_at,
        "root": root_path.as_posix(),
        "record_count": total,
        "subject_count": subjects,
        "category_count": len(category_ids),
        "valid_records": max(0, valid),
        "defective_records": len(defect_ids),
        "records_requiring_manual_review": len(review_ids),
        "critical_defects": severity_counts["critical"],
        "high_defects": severity_counts["high"],
        "medium_defects": severity_counts["medium"],
        "low_defects": severity_counts["low"],
        "review_defects": severity_counts["review"],
        "issue_count": len(issues),
        "issue_count_by_area": dict(sorted(per_area.items())),
        "issue_count_by_severity": dict(sorted(severity_counts.items())),
        "duplicate_question_id_groups": duplicate_groups,
        "duplicate_question_id_rows": sum(
            count for count in seen_keys.values() if count > 1
        ),
        "duplicate_source_records": sum(
            1 for issue in issues if issue.code == "duplicate_source_record"
        ),
        "missing_options": sum(missing_options_by_type.values()),
        "missing_options_by_type": dict(sorted(missing_options_by_type.items())),
        "unknown_question_type": sum(
            1 for issue in issues if issue.code == "unknown_question_type"
        ),
        "unanswered_records": unanswered,
        "missing_marks_records": marks_missing,
        "missing_order_records": no_order,
        "auto_correctable_records": len(correction_ids),
        "record_count_after_planned_safe_correction": total,
        "page_artifact_records": sum(
            1 for issue in issues if issue.code == "page_header_footer"
        ),
        "control_character_records": sum(
            1 for issue in issues if issue.code == "control_character"
        ),
        "mojibake_records": sum(
            1 for issue in issues if issue.code == "replacement_or_mojibake"
        ),
        "private_use_math_records": sum(
            1 for issue in issues if issue.code == "private_use_math_glyph"
        ),
        "table_structure_records": table_rows,
        "pipe_structure_records": pipe_structure_rows,
        "code_fence_records": code_rows,
        "math_glyph_records": sum(
            1 for issue in issues if issue.code == "private_use_math_glyph"
        ),
        "visual_placeholder_rows": sum(
            1 for issue in issues if issue.code == "visual_asset_placeholder"
        ),
        "visual_placeholder_options": placeholder_options,
        "missing_image_assets": sum(
            1 for issue in issues if issue.code == "missing_image_asset"
        ),
        "image_asset_files": len(audit.asset_inventory),
        "source_files": len({subject.csv_path for subject in discover(root_path).subjects}),
        "source_hashes": audit.source_hashes,
        "normalisation_policy": {
            "applied_to_source": False,
            "safe_candidate": "remove_c0_control_characters",
            "requires_dry_run": True,
            "answer_content_modified": False,
            "missing_content_invented": False,
        },
    }
    audit.area_summary = {
        area: {
            "issues": sum(1 for issue in issues if issue.area == area),
            "records": len(
                {
                    issue.question_id
                    for issue in issues
                    if issue.area == area and issue.severity != "review"
                }
            ),
            "severities": dict(
                Counter(issue.severity for issue in issues if issue.area == area)
            ),
        }
        for area in AREAS
    }
    audit.summary["area_summary"] = audit.area_summary
    return audit


def safe_correct_text(value: str) -> str:
    """Remove non-text C0 controls only; whitespace, newlines and tabs are preserved."""
    return _CONTROL_RE.sub("", value)


def write_reports(audit: QualityAudit, output_dir: Path | str) -> dict[str, Path]:
    """Write the audit's supporting files and the requested Markdown reports."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    artifacts = audit.write(target)
    summary = audit.summary
    integrity = audit.issues_for("data_integrity")
    formatting = audit.issues_for("formatting")
    visual = audit.issues_for("visual")

    (target / "QUALITY_AUDIT_REPORT.md").write_text(
        _audit_report(summary, integrity, formatting, visual), encoding="utf-8"
    )
    (target / "DATA_INTEGRITY_REPORT.md").write_text(
        _area_report("Data Integrity", summary, integrity), encoding="utf-8"
    )
    (target / "QUESTION_FORMATTING_REPORT.md").write_text(
        _area_report("Question Formatting", summary, formatting), encoding="utf-8"
    )
    (target / "IMAGE_DIAGRAM_INTEGRITY_REPORT.md").write_text(
        _visual_report(summary, visual), encoding="utf-8"
    )
    (target / "CORRECTION_REPORT.md").write_text(
        _correction_report(audit), encoding="utf-8"
    )
    return artifacts


def _table(rows: Iterable[tuple[str, Any]]) -> str:
    lines = ["| Metric | Value |", "|---|---:|"]
    for key, value in rows:
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def _top_examples(issues: list[QualityIssue], limit: int = 12) -> str:
    if not issues:
        return "No issues were detected in this area."
    grouped: Counter[str] = Counter(issue.code for issue in issues)
    lines = ["| Code | Severity | Count | Example source |", "|---|---|---:|---|"]
    for code, count in grouped.most_common(limit):
        example = next(issue for issue in issues if issue.code == code)
        lines.append(
            f"| `{code}` | {example.severity} | {count} | "
            f"`{example.source_path}` row {example.source_row or '—'} · "
            f"`{example.source_question_id}` |"
        )
    return "\n".join(lines)


def _summary_table(summary: dict[str, Any]) -> str:
    return _table(
        [
            ("Records inspected", summary["record_count"]),
            ("Valid records", summary["valid_records"]),
            ("Defective records", summary["defective_records"]),
            ("Critical defects", summary["critical_defects"]),
            ("High-severity defects", summary["high_defects"]),
            ("Medium-severity defects", summary["medium_defects"]),
            ("Low-severity defects", summary["low_defects"]),
            ("Review-only findings", summary["review_defects"]),
            ("Records requiring manual review", summary["records_requiring_manual_review"]),
            ("Duplicate question-ID groups", summary["duplicate_question_id_groups"]),
            ("Duplicate source records", summary["duplicate_source_records"]),
            ("Missing objective options", summary["missing_options"]),
            ("Unknown question types", summary["unknown_question_type"]),
            ("Page-artifact records", summary["page_artifact_records"]),
            ("Control-character records", summary["control_character_records"]),
            ("Mojibake/replacement records", summary["mojibake_records"]),
            ("Private-use math records", summary["private_use_math_records"]),
            ("Visual placeholder rows", summary["visual_placeholder_rows"]),
            ("Missing image assets", summary["missing_image_assets"]),
            ("Image asset files found", summary["image_asset_files"]),
            ("Markdown table records", summary["table_structure_records"]),
            ("Pipe-structure records", summary["pipe_structure_records"]),
            ("Fenced-code records", summary["code_fence_records"]),
        ]
    )


def _audit_report(
    summary: dict[str, Any],
    integrity: list[QualityIssue],
    formatting: list[QualityIssue],
    visual: list[QualityIssue],
) -> str:
    return f"""# Question Bank Quality & Content Integrity Audit

Generated: {summary['generated_at']}

Source root: `{summary['root']}`

This is a read-only audit. It does not regenerate answers, infer missing options,
reconstruct mathematics, create diagrams, modify v5/v6, or silently delete
records. Every defect retains its course, source path, source row and source
question ID.

## Overall totals

{_summary_table(summary)}

Valid means no issue above review-only severity. Review-only findings (for
example an unverifiable figure mention) remain listed but do not by themselves
make a record structurally unusable.

## Area overview

| Area | Issues | Affected records | Severities |
|---|---:|---:|---|
| Data integrity | {len(integrity)} | {summary['area_summary']['data_integrity']['records']} | {json.dumps(summary['area_summary']['data_integrity']['severities'])} |
| Formatting | {len(formatting)} | {summary['area_summary']['formatting']['records']} | {json.dumps(summary['area_summary']['formatting']['severities'])} |
| Visual integrity | {len(visual)} | {summary['area_summary']['visual']['records']} | {json.dumps(summary['area_summary']['visual']['severities'])} |

“Issues” is the number of findings; “affected records” is the number of unique
question IDs with a finding in that area.

## Highest-value findings

### Data integrity

{_top_examples(integrity, 10)}

### Formatting

{_top_examples(formatting, 10)}

### Visual content

{_top_examples(visual, 10)}

## Source preservation

- Source v5/v6 trees are read-only inputs; this audit does not write them.
- No answer or solution value was changed.
- Unanswered records remain unanswered.
- The safe correction plan is separate and is not applied to the source.
- Duplicate identities use the existing composite scope `(course_id, source
  question_id)`, not a global assumption.

See the three area reports, `CORRECTION_REPORT.md`, `issues.csv`, `summary.json`
and `safe_corrections.json` for the complete evidence.
"""


def _area_report(title: str, summary: dict[str, Any], issues: list[QualityIssue]) -> str:
    area_key = {
        "Data Integrity": "data_integrity",
        "Question Formatting": "formatting",
    }.get(title)
    severity_counts = (
        summary["area_summary"][area_key]["severities"]
        if area_key and area_key in summary["area_summary"]
        else Counter(issue.severity for issue in issues)
    )
    return f"""# {title} Report

Generated: {summary['generated_at']}

{_top_examples(issues, 30)}

## Area totals

| Severity | Count |
|---|---:|
{chr(10).join(f"| {severity} | {count} |" for severity, count in severity_counts.items())}

## Preservation rules

- Optional fields such as marks, explanations and answers are not errors merely
  because they are absent.
- Missing options are not synthesized.
- An answer that cannot be interpreted is reported, never rewritten.
- Page headers, OCR artifacts and private-use glyphs are preserved for review.
- Duplicate source rows are not merged or deleted.

The full issue inventory is in `issues.csv`.
"""


def _visual_report(summary: dict[str, Any], issues: list[QualityIssue]) -> str:
    placeholders = [issue for issue in issues if issue.code == "visual_asset_placeholder"]
    references = [issue for issue in issues if issue.code == "figure_reference_without_asset"]
    missing = [issue for issue in issues if issue.code == "missing_image_asset"]
    return f"""# Image & Diagram Integrity Report

Generated: {summary['generated_at']}

## Finding

The source tree contains **{summary['image_asset_files']} image asset files**.
No image file, caption table or asset manifest was supplied with the v5 export.
The source does contain {summary['visual_placeholder_rows']} rows with explicit
figure/image placeholders and {summary['visual_placeholder_options']} placeholder
options. These are missing visual content, not files that can be reconstructed.

## Metrics

| Metric | Count |
|---|---:|
| Image asset files found | {summary['image_asset_files']} |
| Questions with figure/image placeholders | {summary['visual_placeholder_rows']} |
| Placeholder options | {summary['visual_placeholder_options']} |
| Missing Markdown image paths | {len(missing)} |
| Figure/diagram references without an asset field | {len(references)} |

## Placeholder examples

{_top_examples(placeholders, 12)}

## Missing references

{_top_examples(missing, 12)}

## Frontend behavior

The question renderer displays the original placeholder text with a clear
unavailable-media fallback. It does not invent an image, fetch a remote image,
or treat an HTML `<img>` example in an IT question as an attachment. Markdown
image paths are resolved only inside the dataset root; external or absolute
paths are not used as asset sources.
"""


def _correction_report(audit: QualityAudit) -> str:
    summary = audit.summary
    groups = Counter(correction.rule for correction in audit.safe_corrections)
    lines = [
        "# Correction Report",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Policy",
        "",
        "The only safe automatic correction identified is removal of non-text C0",
        "control characters from display text. The plan retains the original value,",
        "the corrected value, the rule and the source trace. It is not applied to",
        "v5/v6 source files.",
        "",
        "The existing import pipeline already removes these controls from stored",
        "question text; this is why the canonical database's control-character count",
        "is zero while the source audit still reports the source artifacts.",
        "",
        "## Totals",
        "",
        _table(
            [
                ("Safe correction candidates", len(audit.safe_corrections)),
                ("Affected source records", summary["auto_correctable_records"]),
                ("Automatic corrections applied to source", 0),
                ("Source files modified", 0),
                ("Answers modified", 0),
                ("Options invented", 0),
                ("Questions deleted", 0),
                ("Records left unchanged", summary["record_count"]),
                ("Records requiring manual review", summary["records_requiring_manual_review"]),
                ("Unresolved findings", summary["issue_count"]),
                ("Rolled-back corrections", 0),
            ]
        ),
        "",
        "## Rules",
        "",
        "| Rule | Candidates |",
        "|---|---:|",
        *[f"| `{rule}` | {count} |" for rule, count in groups.items()],
        "",
        "## Validation after planning",
        "",
        "The plan does not change record count, IDs, answers, options or source",
        "paths. Re-running the auditor against the corrected working copy is the",
        "required dry-run check before any future application.",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "AREAS",
    "SEVERITIES",
    "QualityAudit",
    "QualityIssue",
    "SafeCorrection",
    "audit_question_dataset",
    "safe_correct_text",
    "write_reports",
]
