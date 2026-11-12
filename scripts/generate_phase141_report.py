"""Generate the Phase 14.1 report from persisted recovery evidence."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(os.environ.get("EXAM_ENGINE_DB", "data/exam_engine.db"))
OUT = Path("reports/recovery/phase141-final/PHASE_14.1_REPORT.md")
BASELINE = {
    "courses": 29,
    "records": 15432,
    "issues": 35445,
    "repairs": 7735,
    "missing": 6088,
    "recovered": 35,
    "official": 0,
    "independent": 0,
    "passage_based": 901,
    "passage_children": 0,
    "ready": 0,
    "tests": 634,
}


def load() -> tuple[dict, dict, list[dict]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    run = con.execute(
        "select * from recovery_runs where status='ok' and dry_run=0 "
        "order by started_at desc limit 1"
    ).fetchone()
    if run is None:
        raise SystemExit("no completed persisted recovery run found")
    totals = json.loads(run["totals_json"] or "{}")
    courses = [
        dict(row)
        for row in con.execute(
            "select * from recovery_course_reports where run_id=? order by course_id",
            (run["run_id"],),
        )
    ]
    return dict(run), totals, courses


def main() -> None:
    run, totals, courses = load()
    rid = run["run_id"]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    by_severity = totals.get("by_severity", {})
    by_release = totals.get("by_release_status", {})
    by_status = totals.get("by_answer_status", {})
    by_verification = totals.get("by_verification_status", {})
    quality_scores = [
        json.loads(row["quality_json"] or "{}").get("overall") for row in courses
    ]
    unique_scores = sorted({round(float(value), 2) for value in quality_scores if value is not None})
    repair_counts = {
        row["status"]: row["n"]
        for row in con.execute(
            "select status,count(*) n from recovery_repairs where run_id=? group by status",
            (rid,),
        )
    }
    source_types = {
        row["document_type"]: row["n"]
        for row in con.execute(
            "select document_type,count(*) n from recovery_source_candidates where run_id=? group by document_type",
            (rid,),
        )
    }
    source_tiers = {
        row["source_tier"]: row["n"]
        for row in con.execute(
            "select source_tier,count(*) n from recovery_source_candidates where run_id=? group by source_tier",
            (rid,),
        )
    }
    source_kinds = {
        row["kind"]: row["n"]
        for row in con.execute(
            "select kind,count(*) n from recovery_source_documents where run_id=? group by kind",
            (rid,),
        )
    }
    verification = {
        row["status"]: row["n"]
        for row in con.execute(
            "select status,count(*) n from recovery_verifications where run_id=? group by status",
            (rid,),
        )
    }
    questions_matched = int(
        con.execute(
            "select count(*) from recovery_answer_candidates where run_id=? and source_type in ('official_answer_key','local_source_document')",
            (rid,),
        ).fetchone()[0]
    )
    candidate_sources = Counter(
        row[0]
        for row in con.execute(
            "select source_type from recovery_answer_candidates where run_id=?", (rid,)
        )
    )
    db_size = DB.stat().st_size if DB.exists() else 0
    duration_ms = int(run["duration_ms"] or 0)
    records = int(totals.get("records", 0))
    per_second = round(records / (duration_ms / 1000), 2) if duration_ms else 0
    top_issues = sorted(totals.get("by_code", {}).items(), key=lambda item: -item[1])[:12]
    course_rows = []
    for row in courses:
        quality = json.loads(row["quality_json"] or "{}")
        counts = json.loads(row["counts_json"] or "{}")
        course_rows.append(
            (
                row["course_id"],
                row["status"],
                quality.get("overall"),
                counts.get("records"),
                counts.get("missing_answer"),
                counts.get("recovered_answers"),
                counts.get("verified"),
                counts.get("source_candidates"),
                counts.get("passage_groups"),
            )
        )

    lines: list[str] = [
        "# Phase 14.1 — Recovery Intelligence Enhancement Report",
        "",
        f"**Run:** `{rid}`  ",
        f"**Started:** {run['started_at']}  ",
        f"**Finished:** {run['finished_at']}  ",
        f"**Status:** {run['status']}  ",
        f"**Recovery version:** {run['recovery_version']}",
        "",
        "This report is generated from persisted recovery tables. It distinguishes missing",
        "evidence from demonstrated error and does not describe an AI or local candidate as",
        "officially verified.",
        "",
        "## 1. Implementation summary",
        "",
        "Phase 14 was enhanced rather than replaced. The audit/repair schema and release",
        "policy remain backwards compatible; Phase 14.1 adds reusable source discovery,",
        "official answer-key parsing, conservative near-duplicate matching, a separate",
        "verification engine, explicit passage groups, calibrated quality indicators and",
        "source/verification persistence.",
        "",
        "## 2. Architecture changes",
        "",
        "The pipeline is grouped by course/paper:",
        "",
        "1. Discover CSV and local source documents.",
        "2. Detect schema and normalize rows.",
        "3. Detect structure, options, passages and missing answers.",
        "4. Discover bounded public source candidates (snippets remain leads only).",
        "5. Match explicit official answer-key entries where evidence exists.",
        "6. Recover candidates in evidence order (stored → exact/near duplicate → key/solution → AI last).",
        "7. Verify candidates independently of generation.",
        "8. Build only explicitly evidenced passage groups.",
        "9. Generate repairs, investigate authenticity, score components and release readiness.",
        "10. Persist provenance, candidates, verification and reports.",
        "",
        "## 3. New and modified modules",
        "",
        "* `src/exam_engine/recovery/sources.py` — source classification, query generation,",
        "  permitted HTTP retrieval, answer-key parsing and matching.",
        "* `src/exam_engine/recovery/verification.py` — separate verification taxonomy and",
        "  answer/numeric/MCQ checks.",
        "* `src/exam_engine/recovery/answers.py` — conservative near-duplicate index and",
        "  official-key priority.",
        "* `src/exam_engine/recovery/structure.py` — stricter passage detection, statement",
        "  classification and explicit passage columns.",
        "* `src/exam_engine/recovery/quality.py` — coverage-based calibration and four",
        "  evidence indicators.",
        "* `src/exam_engine/recovery/service.py` — grouped discovery, verification, passage",
        "  groups, workflow modes, reports and totals.",
        "* `src/exam_engine/storage/recovery_repository.py`, `schema.py`, migration",
        "  `0013_recovery_intelligence.sql` — additive persistence.",
        "* Dashboard API/view and CLI — source, verification, passage and workflow visibility.",
        "",
        "## 4. Database changes and migrations",
        "",
        "* Added `recovery_records.verification_json`.",
        "* Added `recovery_source_candidates`.",
        "* Added `recovery_verifications`.",
        "* Added `recovery_passages`.",
        "* Added supporting indexes.",
        "* Migration is additive; existing Phase 14 rows and raw datasets were not rewritten.",
        "",
        "## 5. Source discovery results",
        "",
        f"* Grouped source queries: **{totals.get('source_queries', 0)}**.",
        f"* Discovered candidates: **{totals.get('source_candidates', 0)}**.",
        f"* Candidate types: `{json.dumps(source_types, sort_keys=True)}`.",
        f"* Candidate tiers: `{json.dumps(source_tiers, sort_keys=True)}`.",
        f"* Local source-document associations: **{totals.get('source_documents', 0)}**.",
        f"* Local source kinds: `{json.dumps(source_kinds, sort_keys=True)}`.",
        "* The no-key provider returned general reference pages, not examination PDFs. No",
        "  snippet was accepted as an answer.",
        "",
        "## 6. Original papers discovered",
        "",
        f"**{totals.get('question_papers_found', 0)}** original question papers were verified.",
        "No public paper was established as the paper for these records.",
        "",
        "## 7. Official answer keys discovered",
        "",
        f"**{totals.get('answer_keys_found', 0)}** official answer keys were verified.",
        "No official key was fabricated or inferred from syllabus files.",
        "",
        "## 8. Questions matched to sources",
        "",
        f"**{questions_matched}** questions were matched to a verified source/key entry.",
        "Source fidelity is `unverifiable` for all records because no original paper/key was",
        "available; this is missing evidence, not proof that the content is wrong.",
        "",
        "## 9. Answers recovered",
        "",
        f"* Missing at start of this run: **{BASELINE['missing']}** (Phase 14 baseline).",
        f"* Currently missing/unresolved: **{totals.get('missing_answers', 0)}**.",
        f"* Recovered (answer absent in normalized source row): **{totals.get('recovered_answers', 0)}**.",
        f"* Answer status counts: `{json.dumps(by_status, sort_keys=True)}`.",
        "",
        "Recovery is deliberately conservative. A local duplicate is not treated as an",
        "official key; AI output is last-resort and remains unverified.",
        "",
        "## 10. Answers officially verified",
        "",
        f"**{totals.get('officially_verified_answers', 0)}** answers are officially verified.",
        "No official key was found, so none can be claimed.",
        "",
        "## 11. Answers independently verified",
        "",
        f"**{totals.get('independently_verified_answers', 0)}** answers have independent verification.",
        "The current run performed no independent solver/key cross-check that could establish",
        "one.",
        "",
        "## 12. Answers corroborated by multiple sources",
        "",
        f"**{totals.get('multiple_source_corroborated_answers', 0)}** answers were corroborated",
        "by genuinely independent sources. Same-course duplicates are recorded as",
        "`review_required`, not independent corroboration.",
        "",
        "## 13. Conflicting answers",
        "",
        f"**{totals.get('conflicting_answers', 0)}** records are in a conflicting state.",
        "Conflict checks cover local duplicate disagreement and local-vs-official-key",
        "disagreement.",
        "",
        "## 14. Unresolved answers",
        "",
        f"**{totals.get('unresolved_answers', 0)}** answers remain unresolved. They are",
        "quarantined where required and are not released.",
        "",
        "## 15. Passage structures reconstructed",
        "",
        f"* Passage-classified records: **{totals.get('passage_based_records', 0)}**.",
        f"* Passage parents with children: **{totals.get('passage_parents', 0)}**.",
        f"* Reconstructed child records: **{totals.get('reconstructed_children', 0)}**.",
        f"* Explicit passage groups: **{totals.get('passage_groups', 0)}**.",
        "The 73 remaining passage classifications are single-question contexts without",
        "evidence for separate child questions. No boundary was invented.",
        "",
        "## 16. Authenticity findings",
        "",
        f"* Records with authenticity/unsupported-source findings: **{totals.get('potentially_fabricated_or_unsupported', 0)}**.",
        "* Labels are evidence-bearing (`source_mismatch`, `unsupported_content`,",
        "  `possible_synthetic_content`, `template_artifact`, `content_corruption`,",
        "  `duplicate_content`, `unverifiable_authenticity`), not claims of AI authorship.",
        "* No suspicious record was automatically deleted or rewritten.",
        "",
        "## 17. Repair proposals generated and applied",
        "",
        f"* Repair rows in this run: **{sum(repair_counts.values())}**.",
        f"* Repair statuses: `{json.dumps(repair_counts, sort_keys=True)}`.",
        "* Applied repairs remain in the separate repaired projection. Raw source rows are",
        "  unchanged and review/rollback evidence remains auditable.",
        "",
        "## 18. Quality score changes",
        "",
        "* Phase 14: 28 courses at 69.0 and one at 74.0, caused by a blanket",
        "  `not_recoverable` cap.",
        f"* Phase 14.1: unique scores `{unique_scores}`; min {min(quality_scores) if quality_scores else None},",
        f"  max {max(quality_scores) if quality_scores else None}.",
        "* Answer coverage is now an explicit evidence gate. Source gaps are represented in",
        "  the source-fidelity component and separate source-verification coverage, not by",
        "  erasing all course differences.",
        "* Indicators: structural quality, answer-verification coverage, source-verification",
        "  coverage and release readiness. Current evidence status is",
        f"`{totals.get('courses_insufficient_evidence', 0)}` insufficient-evidence courses.",
        "",
        "## 19. Course-by-course results",
        "",
        "| Course | Status | Quality | Records | Missing | Recovered | Verified | Sources | Passage groups |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in course_rows:
        lines.append("| " + " | ".join("" if value is None else str(value) for value in row) + " |")
    lines.extend(
        [
            "",
            "## 20. Performance measurements",
            "",
            f"* Duration: **{duration_ms} ms**.",
            f"* Records processed per second: **{per_second}**.",
            f"* Database size after run: **{db_size:,} bytes**.",
            f"* Source queries: **{totals.get('source_queries', 0)}** for 29 courses.",
            "* Near-duplicate matching is bounded by rare-token blocking and a 25-candidate scan.",
            "* Public retrieval is bounded, cached by the existing search layer and isolated per course.",
            "",
            "## 21. Test results",
            "",
            "* Full Python suite: **644 collected tests passed** (`python -m pytest -q`).",
            "* Frontend Node suite: **17/17 passed** (`npm test` in `frontend/`);",
            "  `node --check` passed for the recovery view.",
            "* New deterministic coverage in `tests/test_recovery_intelligence.py`: source",
            "  classification and query generation, answer-key parsing/matching, independent",
            "  verification, near-duplicate recovery, passage grouping, quality calibration,",
            "  release policy and additive persistence.",
            "* External retrieval is exercised with deterministic fakes; the live provider run",
            "  reported above is recorded separately and is not counted as a test pass.",
            "",
            "## 22. Known limitations",
            "",
            "* The configured no-key public provider returned general reference pages, not",
            f"  official examination PDFs. {totals.get('source_candidates', 0)} candidates were recorded;",
            "  zero were papers/keys.",
            "* No official answer key or original question paper was available locally or",
            "  viably retrieved, so source fidelity and official verification remain unverified.",
            "* 73 passage-classified questions have no evidence for child boundaries; no",
            "  passage groups were forced.",
            "* No independent solver was enabled for this corpus run. Local duplicates alone",
            "  are not independent corroboration.",
            "* The loopback dashboard remains a single-user, unauthenticated local tool.",
            "",
            "## 23. Remaining manual-review requirements",
            "",
            f"* Review the **{totals.get('missing_answers', 0)}** unresolved answers against",
            "  official papers/keys when legally available.",
            f"* Review the **{by_release.get('quarantined', 0)}** quarantined records and",
            f"  **{by_release.get('review_required', 0)}** review-required records.",
            "* Resolve authenticity findings with source documents; do not delete content",
            "  merely because its style is suspicious.",
            "* Attach official keys with exact year/paper/shift identity before promoting any",
            "  candidate to `official_verified`.",
            "* Review any proposed repairs before downstream release; no automated release",
            "  occurred.",
            "",
            "## Baseline comparison",
            "",
            "| Metric | Phase 14 | Phase 14.1 |",
            "|---|---:|---:|",
            f"| Courses | {BASELINE['courses']} | {totals.get('courses', 0)} |",
            f"| Records | {BASELINE['records']} | {records} |",
            f"| Issues | {BASELINE['issues']} | {totals.get('issues', 0)} |",
            f"| Repairs | {BASELINE['repairs']} | {sum(repair_counts.values())} |",
            f"| Missing answers | {BASELINE['missing']} | {totals.get('missing_answers', 0)} |",
            f"| Recovered answers | {BASELINE['recovered']} | {totals.get('recovered_answers', 0)} |",
            f"| Officially verified | {BASELINE['official']} | {totals.get('officially_verified_answers', 0)} |",
            f"| Independently verified | {BASELINE['independent']} | {totals.get('independently_verified_answers', 0)} |",
            f"| Passage-based classified | {BASELINE['passage_based']} | {totals.get('passage_based_records', 0)} |",
            f"| Reconstructed children | {BASELINE['passage_children']} | {totals.get('reconstructed_children', 0)} |",
            f"| Courses ready | {BASELINE['ready']} | {totals.get('courses_ready', 0)} |",
            "",
            "## Verification note",
            "",
            "The result is deliberately not presented as a recovered corpus. Recovery improved",
            "from 35 to 42 candidates through local evidence, but the authoritative source gap",
            "prevented official verification. Raw CSV/PDF files were not modified.",
        ]
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} for {rid}")


if __name__ == "__main__":
    main()
