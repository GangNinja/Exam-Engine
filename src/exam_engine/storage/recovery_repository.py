"""Persistence and read models for dataset recovery.

Writes are append-only at the dataset level: source tables are never touched.
A rerun for the same course replaces only that course's recovery detail rows.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select

from exam_engine.models.recovery import (
    RECOVERY_VERSION,
    RULE_REGISTRY_VERSION,
    CourseResult,
    RunResult,
    SourceDocument,
    now_iso,
)
from exam_engine.storage.schema import (
    recovery_answer_candidates,
    recovery_course_reports,
    recovery_issues,
    recovery_passages,
    recovery_records,
    recovery_repairs,
    recovery_runs,
    recovery_source_candidates,
    recovery_retrieval_cache,
    recovery_source_documents,
    recovery_verifications,
)

_MAX_RECORD_PAGE = 200
_VERIFIED_RECOVERY_STATUSES = frozenset(
    {
        "official_verified",
        "official_verified_with_independent_check",
        "multiple_source_corroborated",
        "independently_solved_verified",
        "corroborated_local",
    }
)
_OFFICIAL_RECOVERY_STATUSES = frozenset(
    {"official_verified", "official_verified_with_independent_check"}
)
_INDEPENDENT_RECOVERY_STATUSES = frozenset(
    {"official_verified_with_independent_check", "independently_solved_verified"}
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class RecoveryRepository:
    def __init__(self, session: Any) -> None:
        self.session = session

    # -- run lifecycle -----------------------------------------------------
    def start_run(
        self,
        *,
        run_id: str,
        mode: str,
        dry_run: bool,
        root: str,
        options: dict[str, Any] | None = None,
    ) -> None:
        existing = self.session.execute(
            select(recovery_runs.c.run_id).where(recovery_runs.c.run_id == run_id)
        ).scalar_one_or_none()
        if existing:
            self.session.execute(
                recovery_runs.update()
                .where(recovery_runs.c.run_id == run_id)
                .values(
                    status="running",
                    mode=mode,
                    dry_run=1 if dry_run else 0,
                    root=root,
                    options_json=_json(options or {}),
                )
            )
            return
        self.session.execute(
            recovery_runs.insert().values(
                run_id=run_id,
                started_at=now_iso(),
                finished_at=None,
                status="running",
                mode=mode,
                dry_run=1 if dry_run else 0,
                root=root,
                recovery_version=RECOVERY_VERSION,
                rule_version=RULE_REGISTRY_VERSION,
                options_json=_json(options or {}),
                totals_json="{}",
                warnings_json="[]",
                errors_json="[]",
                duration_ms=0,
                created_at=now_iso(),
            )
        )

    def completed_courses(self, run_id: str) -> set[str]:
        rows = self.session.execute(
            select(recovery_course_reports.c.course_id).where(
                recovery_course_reports.c.run_id == run_id
            )
        ).all()
        return {str(row[0]) for row in rows}

    def save_course(
        self,
        run_id: str,
        course: CourseResult,
        source_documents: list[SourceDocument],
    ) -> None:
        course_id = course.course_id
        for table in (
            recovery_records,
            recovery_issues,
            recovery_repairs,
            recovery_answer_candidates,
            recovery_source_documents,
            recovery_source_candidates,
            recovery_verifications,
            recovery_passages,
            recovery_course_reports,
        ):
            self.session.execute(
                delete(table).where(table.c.run_id == run_id, table.c.course_id == course_id)
            )
        self.session.execute(
            recovery_course_reports.insert().values(
                run_id=run_id,
                course_id=course_id,
                course_code=course.course_code,
                course_name=course.course_name,
                category_id=course.category_id,
                source_file=course.source_file,
                source_sha256=course.source_sha256,
                schema_json=_json(course.schema),
                row_count=course.row_count,
                analyzed_count=course.analyzed_count,
                status=course.status,
                quality_json=_json(course.quality),
                counts_json=_json(course.counts),
                warnings_json=_json(course.warnings),
                errors_json=_json(course.errors),
                duration_ms=course.duration_ms,
                created_at=now_iso(),
            )
        )
        record_rows: list[dict[str, Any]] = []
        issue_rows: list[dict[str, Any]] = []
        repair_rows: list[dict[str, Any]] = []
        candidate_rows: list[dict[str, Any]] = []
        for record in course.records:
            record_rows.append(
                {
                    "run_id": run_id,
                    "record_id": record.record_id,
                    "course_id": record.course_id,
                    "source_file": record.source_file,
                    "source_row": record.source_row,
                    "source_question_id": record.source_question_id,
                    "source_sha256": course.source_sha256,
                    "analysis_hash": record.analysis_hash,
                    "original_json": _json(record.original),
                    "normalized_json": _json(record.normalized),
                    "repaired_json": _json(record.repaired),
                    "structure_json": _json(record.structure),
                    "quality_json": _json(record.quality),
                    "verification_json": _json(record.verification),
                    "answer_status": record.answer_status,
                    "source_fidelity": record.source_fidelity,
                    "release_status": record.release_status,
                    "issue_count": record.issue_count,
                    "blocking_count": record.blocking_count,
                    "created_at": now_iso(),
                }
            )
            for issue in record.issues:
                issue_rows.append(
                    {
                        "run_id": run_id,
                        "course_id": record.course_id,
                        "record_id": record.record_id,
                        "code": issue.code,
                        "category": issue.category,
                        "severity": issue.severity,
                        "message": issue.message,
                        "evidence_json": _json(issue.evidence),
                        "confidence": issue.confidence,
                        "remediation": issue.remediation,
                        "blocking": 1 if issue.blocking else 0,
                        "field": issue.field,
                        "created_at": now_iso(),
                    }
                )
            for repair in record.repairs:
                repair_rows.append(
                    {
                        "run_id": run_id,
                        "course_id": record.course_id,
                        "record_id": record.record_id,
                        "repair_type": repair.repair_type,
                        "field": repair.field,
                        "before_json": _json(repair.before),
                        "after_json": _json(repair.after),
                        "method": repair.method,
                        "confidence": repair.confidence,
                        "risk": repair.risk,
                        "status": repair.status,
                        "reason": repair.reason,
                        "reviewer": repair.reviewer,
                        "note": repair.note,
                        "created_at": now_iso(),
                    }
                )
            for candidate in record.answer_candidates:
                candidate_rows.append(
                    {
                        "run_id": run_id,
                        "course_id": record.course_id,
                        "record_id": record.record_id,
                        "answer_text": candidate.answer_text,
                        "source_type": candidate.source_type,
                        "source_name": candidate.source_name,
                        "source_url": candidate.source_url,
                        "source_reference": candidate.source_reference,
                        "verification_status": candidate.verification_status,
                        "confidence": candidate.confidence,
                        "method": candidate.method,
                        "evidence_json": _json(candidate.evidence),
                        "retrieved_at": candidate.retrieved_at,
                        "official": 1 if candidate.official else 0,
                    }
                )
        if record_rows:
            self.session.execute(recovery_records.insert(), record_rows)
        if issue_rows:
            self.session.execute(recovery_issues.insert(), issue_rows)
        if repair_rows:
            self.session.execute(recovery_repairs.insert(), repair_rows)
        verification_rows: list[dict[str, Any]] = []
        for record in course.records:
            if not record.verification:
                continue
            verification_rows.append(
                {
                    "run_id": run_id,
                    "course_id": record.course_id,
                    "record_id": record.record_id,
                    "status": str(record.verification.get("status") or "not_checked"),
                    "method": str(record.verification.get("method") or "unknown"),
                    "confidence": float(record.verification.get("confidence") or 0.0),
                    "answer_text": record.verification.get("answer_text"),
                    "evidence_json": _json(record.verification.get("evidence") or {}),
                    "issues_json": _json(record.verification.get("issues") or []),
                    "independent": 1 if record.verification.get("independent") else 0,
                    "created_at": now_iso(),
                }
            )
        if candidate_rows:
            self.session.execute(recovery_answer_candidates.insert(), candidate_rows)
        if verification_rows:
            self.session.execute(recovery_verifications.insert(), verification_rows)
        if course.source_candidates:
            self.session.execute(
                recovery_source_candidates.insert(),
                [
                    {
                        "run_id": run_id,
                        "course_id": course.course_id,
                        "source_id": item.source_id,
                        "title": item.title,
                        "url": item.url,
                        "document_type": item.document_type,
                        "source_tier": item.source_tier,
                        "reliability": item.reliability,
                        "provider": item.provider,
                        "snippet": item.snippet,
                        "query": item.query,
                        "classification_reason": item.classification_reason,
                        "retrieval_status": item.retrieval_status,
                        "discovered_at": item.discovered_at,
                        "evidence_json": _json(item.evidence),
                    }
                    for item in course.source_candidates
                ],
            )
        if course.passages:
            self.session.execute(
                recovery_passages.insert(),
                [
                    {
                        "passage_id": item.passage_id,
                        "run_id": run_id,
                        "course_id": course.course_id,
                        "passage_text": item.passage_text,
                        "parent_record_id": item.parent_record_id,
                        "child_record_ids_json": _json(item.child_record_ids),
                        "source_reference": item.source_reference,
                        "confidence": item.confidence,
                        "evidence_json": _json(item.evidence),
                    }
                    for item in course.passages
                ],
            )
        if source_documents:
            self.session.execute(
                recovery_source_documents.insert(),
                [
                    {
                        "run_id": run_id,
                        "course_id": item.course_id,
                        "path": item.path,
                        "kind": item.kind,
                        "sha256": item.sha256,
                        "page_count": item.page_count,
                        "text_chars": item.text_chars,
                        "question_fidelity_available": 1 if item.question_fidelity_available else 0,
                        "metadata_json": _json(item.metadata),
                    }
                    for item in source_documents
                ],
            )

    def finish_run(self, run: RunResult) -> None:
        self.session.execute(
            recovery_runs.update()
            .where(recovery_runs.c.run_id == run.run_id)
            .values(
                finished_at=run.finished_at,
                status=run.status,
                totals_json=_json(run.totals),
                warnings_json=_json(run.warnings),
                errors_json=_json(run.errors),
                duration_ms=run.duration_ms,
            )
        )

    def latest_run_id(self) -> str | None:
        return self.session.execute(
            select(recovery_runs.c.run_id).order_by(recovery_runs.c.started_at.desc()).limit(1)
        ).scalar_one_or_none()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.session.execute(
            select(recovery_runs).where(recovery_runs.c.run_id == run_id)
        ).mappings().first()
        if row is None:
            return None
        payload = dict(row)
        for key in ("options_json", "totals_json", "warnings_json", "errors_json"):
            payload[key.removesuffix("_json")] = _load(payload.pop(key), {})
        return payload

    # -- public-document retrieval cache ----------------------------------
    def cached_document(self, url: str) -> dict[str, Any] | None:
        row = self.session.execute(
            select(recovery_retrieval_cache).where(recovery_retrieval_cache.c.url == url)
        ).mappings().first()
        if row is None:
            return None
        payload = dict(row)
        payload["warnings"] = _load(payload.pop("warnings_json", "[]"), [])
        payload["links"] = _load(payload.pop("links_json", "[]"), [])
        payload["tables"] = _load(payload.pop("tables_json", "[]"), [])
        payload["metadata"] = _load(payload.pop("metadata_json", "{}"), {})
        return payload

    def save_cached_document(self, payload: dict[str, Any]) -> None:
        values = {
            "url": str(payload.get("url") or ""),
            "final_url": str(payload.get("final_url") or ""),
            "title": str(payload.get("title") or ""),
            "content_type": str(payload.get("content_type") or ""),
            "text": str(payload.get("text") or ""),
            "text_chars": int(payload.get("text_chars") or len(str(payload.get("text") or ""))),
            "sha256": str(payload.get("sha256") or ""),
            "bytes_read": int(payload.get("bytes_read") or 0),
            "page_count": payload.get("page_count"),
            "extraction_method": str(payload.get("extraction_method") or ""),
            "http_status": payload.get("http_status"),
            "warnings_json": _json(payload.get("warnings") or []),
            "fetched_at": str(payload.get("fetched_at") or now_iso()),
            "status": str(payload.get("status") or "ok"),
            "error": str(payload.get("error") or ""),
            "markdown": str(payload.get("markdown") or ""),
            "links_json": _json(payload.get("links") or []),
            "tables_json": _json(payload.get("tables") or []),
            "metadata_json": _json(payload.get("metadata") or {}),
            "canonical_url": str(payload.get("canonical_url") or ""),
            "crawler": str(payload.get("crawler") or "http"),
            "browser_ms": payload.get("browser_ms"),
        }
        if not values["url"]:
            return
        self.session.execute(
            delete(recovery_retrieval_cache).where(
                recovery_retrieval_cache.c.url == values["url"]
            )
        )
        self.session.execute(recovery_retrieval_cache.insert().values(**values))

    def cache_stats(self) -> dict[str, int]:
        rows = self.session.execute(
            select(recovery_retrieval_cache.c.status, func.count())
            .group_by(recovery_retrieval_cache.c.status)
        ).all()
        stats = {str(row[0]): int(row[1]) for row in rows}
        stats["total"] = sum(stats.values())
        return stats

    # -- read models -------------------------------------------------------
    def _recovery_counts(self, run_id: str) -> dict[str, dict[str, int]]:
        """Derive recovery-subset counts instead of reusing corpus-wide verified totals.

        Older persisted runs only stored the corpus-wide verified count, which can
        exceed the number of recovered answers because answers that were already
        present are verified too.  Rebuilding the subset from the stored records
        keeps the overview accurate without rewriting historical reports.
        """

        rows = self.session.execute(
            select(
                recovery_records.c.course_id,
                recovery_records.c.normalized_json,
                recovery_records.c.repaired_json,
                recovery_records.c.answer_status,
                recovery_records.c.verification_json,
            ).where(recovery_records.c.run_id == run_id)
        ).all()
        counts: dict[str, dict[str, int]] = {}
        for course_id, normalized_json, repaired_json, answer_status, verification_json in rows:
            normalized = _load(normalized_json, {})
            repaired = _load(repaired_json, {})
            if str((normalized or {}).get("answer_text") or "").strip():
                continue
            if not str((repaired or {}).get("answer_text") or "").strip():
                continue
            verification_status = str(
                (_load(verification_json, {}) or {}).get("status") or ""
            )
            entry = counts.setdefault(
                str(course_id),
                {
                    "recovered_answers": 0,
                    "verified_recoveries": 0,
                    "unverified_recoveries": 0,
                    "officially_verified_recoveries": 0,
                    "independently_verified_recoveries": 0,
                },
            )
            entry["recovered_answers"] += 1
            if (
                answer_status in _VERIFIED_RECOVERY_STATUSES
                or verification_status in _VERIFIED_RECOVERY_STATUSES
            ):
                entry["verified_recoveries"] += 1
            if (
                answer_status in _OFFICIAL_RECOVERY_STATUSES
                or verification_status in _OFFICIAL_RECOVERY_STATUSES
            ):
                entry["officially_verified_recoveries"] += 1
            if verification_status in _INDEPENDENT_RECOVERY_STATUSES:
                entry["independently_verified_recoveries"] += 1
        for entry in counts.values():
            entry["unverified_recoveries"] = (
                entry["recovered_answers"] - entry["verified_recoveries"]
            )
        return counts

    def overview(self, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return {
                "run": None,
                "courses": 0,
                "courses_analyzed": 0,
                "courses_requiring_repair": 0,
                "records": 0,
                "totals": {},
            }
        run = self.get_run(run_id) or {}
        course_rows = self.session.execute(
            select(recovery_course_reports).where(recovery_course_reports.c.run_id == run_id)
        ).mappings().all()
        statuses = [_load(row["quality_json"], {}).get("status") for row in course_rows]
        totals = dict(run.get("totals") or {})
        recovery_counts = self._recovery_counts(run_id)
        recovered = sum(item["recovered_answers"] for item in recovery_counts.values())
        verified_recoveries = sum(
            item["verified_recoveries"] for item in recovery_counts.values()
        )
        totals.update(
            recovered_answers=recovered,
            verified_recoveries=verified_recoveries,
            unverified_recoveries=recovered - verified_recoveries,
            officially_verified_recoveries=sum(
                item["officially_verified_recoveries"]
                for item in recovery_counts.values()
            ),
            independently_verified_recoveries=sum(
                item["independently_verified_recoveries"]
                for item in recovery_counts.values()
            ),
        )
        return {
            "run_id": run_id,
            "run": run,
            "courses": len(course_rows),
            "courses_analyzed": len(course_rows),
            "courses_requiring_repair": sum(
                1
                for status in statuses
                if status in {"blocked", "review_required", "insufficient_evidence"}
            ),
            "records": sum(int(row["analyzed_count"] or 0) for row in course_rows),
            "totals": totals,
            "retrieval_cache": self.cache_stats(),
        }

    def list_sources(
        self,
        run_id: str | None = None,
        *,
        course_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return []
        query = select(recovery_source_candidates).where(
            recovery_source_candidates.c.run_id == run_id
        )
        if course_id:
            query = query.where(recovery_source_candidates.c.course_id == course_id)
        rows = self.session.execute(
            query.order_by(
                recovery_source_candidates.c.source_tier,
                recovery_source_candidates.c.course_id,
                recovery_source_candidates.c.reliability.desc(),
            ).limit(max(1, min(int(limit), 2000)))
        ).mappings().all()
        return [
            {
                "course_id": row["course_id"],
                "source_id": row["source_id"],
                "title": row["title"],
                "url": row["url"],
                "document_type": row["document_type"],
                "source_tier": row["source_tier"],
                "reliability": row["reliability"],
                "provider": row["provider"],
                "query": row["query"],
                "retrieval_status": row["retrieval_status"],
                "evidence": _load(row["evidence_json"], {}),
            }
            for row in rows
        ]

    def list_courses(self, run_id: str | None = None) -> list[dict[str, Any]]:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return []
        rows = self.session.execute(
            select(recovery_course_reports)
            .where(recovery_course_reports.c.run_id == run_id)
            .order_by(recovery_course_reports.c.course_name, recovery_course_reports.c.course_id)
        ).mappings().all()
        recovery_counts = self._recovery_counts(run_id)
        payload: list[dict[str, Any]] = []
        for row in rows:
            quality = _load(row["quality_json"], {})
            counts = _load(row["counts_json"], {})
            counts.update(recovery_counts.get(str(row["course_id"]), {}))
            payload.append(
                {
                    "run_id": row["run_id"],
                    "course_id": row["course_id"],
                    "course_code": row["course_code"],
                    "course_name": row["course_name"],
                    "category_id": row["category_id"],
                    "source_file": row["source_file"],
                    "source_sha256": row["source_sha256"],
                    "status": row["status"],
                    "overall": quality.get("overall"),
                    "components": quality.get("components", {}),
                    "quality_status": quality.get("status", row["status"]),
                    "counts": counts,
                    "warnings": _load(row["warnings_json"], []),
                    "errors": _load(row["errors_json"], []),
                    "row_count": row["row_count"],
                    "analyzed_count": row["analyzed_count"],
                }
            )
        return payload

    def list_record_payloads(
        self, run_id: str, course_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """Load the minimum record payload needed to rebuild a resume index."""

        statement = select(
            recovery_records.c.course_id,
            recovery_records.c.source_file,
            recovery_records.c.source_row,
            recovery_records.c.source_question_id,
            recovery_records.c.repaired_json,
            recovery_records.c.structure_json,
        ).where(recovery_records.c.run_id == run_id)
        if course_ids:
            statement = statement.where(
                recovery_records.c.course_id.in_(sorted(course_ids))
            )
        rows = self.session.execute(
            statement.order_by(
                recovery_records.c.course_id, recovery_records.c.source_row
            )
        ).mappings().all()
        return [dict(row) for row in rows]

    def course_detail(
        self,
        course_id: str,
        *,
        run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        issue_limit: int = 100,
    ) -> dict[str, Any] | None:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return None
        row = self.session.execute(
            select(recovery_course_reports).where(
                recovery_course_reports.c.run_id == run_id,
                recovery_course_reports.c.course_id == course_id,
            )
        ).mappings().first()
        if row is None:
            return None
        limit = max(1, min(limit, _MAX_RECORD_PAGE))
        offset = max(0, offset)
        records = self.session.execute(
            select(recovery_records)
            .where(
                recovery_records.c.run_id == run_id,
                recovery_records.c.course_id == course_id,
            )
            .order_by(recovery_records.c.source_row)
            .limit(limit)
            .offset(offset)
        ).mappings().all()
        issues = self.session.execute(
            select(recovery_issues)
            .where(
                recovery_issues.c.run_id == run_id,
                recovery_issues.c.course_id == course_id,
            )
            .order_by(
                recovery_issues.c.blocking.desc(),
                recovery_issues.c.severity,
                recovery_issues.c.issue_id,
            )
            .limit(issue_limit)
        ).mappings().all()
        total = int(
            self.session.execute(
                select(func.count())
                .select_from(recovery_records)
                .where(
                    recovery_records.c.run_id == run_id,
                    recovery_records.c.course_id == course_id,
                )
            ).scalar_one()
            or 0
        )
        return {
            "run_id": run_id,
            "course_id": row["course_id"],
            "course_code": row["course_code"],
            "course_name": row["course_name"],
            "category_id": row["category_id"],
            "source_file": row["source_file"],
            "source_sha256": row["source_sha256"],
            "schema": _load(row["schema_json"], {}),
            "row_count": row["row_count"],
            "analyzed_count": row["analyzed_count"],
            "status": row["status"],
            "quality": _load(row["quality_json"], {}),
            "counts": _load(row["counts_json"], {}),
            "warnings": _load(row["warnings_json"], []),
            "errors": _load(row["errors_json"], []),
            "duration_ms": row["duration_ms"],
            "records": [self._record_summary(item) for item in records],
            "records_total": total,
            "records_offset": offset,
            "records_limit": limit,
            "issues": [self._issue_payload(item) for item in issues],
            "source_documents": self._source_documents(run_id, course_id),
            "source_candidates": self._source_candidates(run_id, course_id),
            "passages": self._passages(run_id, course_id),
        }

    def _source_documents(self, run_id: str, course_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(recovery_source_documents).where(
                recovery_source_documents.c.run_id == run_id,
                recovery_source_documents.c.course_id == course_id,
            )
        ).mappings().all()
        return [
            {
                "path": row["path"],
                "kind": row["kind"],
                "sha256": row["sha256"],
                "page_count": row["page_count"],
                "text_chars": row["text_chars"],
                "question_fidelity_available": bool(row["question_fidelity_available"]),
                "metadata": _load(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def _source_candidates(self, run_id: str, course_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(recovery_source_candidates).where(
                recovery_source_candidates.c.run_id == run_id,
                recovery_source_candidates.c.course_id == course_id,
            )
        ).mappings().all()
        return [
            {
                "source_id": row["source_id"],
                "title": row["title"],
                "url": row["url"],
                "document_type": row["document_type"],
                "source_tier": row["source_tier"],
                "reliability": row["reliability"],
                "provider": row["provider"],
                "snippet": row["snippet"],
                "query": row["query"],
                "classification_reason": row["classification_reason"],
                "retrieval_status": row["retrieval_status"],
                "discovered_at": row["discovered_at"],
                "evidence": _load(row["evidence_json"], {}),
            }
            for row in rows
        ]

    def _passages(self, run_id: str, course_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(recovery_passages).where(
                recovery_passages.c.run_id == run_id,
                recovery_passages.c.course_id == course_id,
            )
        ).mappings().all()
        return [
            {
                "passage_id": row["passage_id"],
                "passage_text": row["passage_text"],
                "parent_record_id": row["parent_record_id"],
                "child_record_ids": _load(row["child_record_ids_json"], []),
                "source_reference": row["source_reference"],
                "confidence": row["confidence"],
                "evidence": _load(row["evidence_json"], {}),
            }
            for row in rows
        ]

    def _record_summary(self, row: Any) -> dict[str, Any]:
        repaired = _load(row["repaired_json"], {})
        return {
            "record_id": row["record_id"],
            "source_row": row["source_row"],
            "source_question_id": row["source_question_id"],
            "question_text": str(repaired.get("question_text") or "")[:600],
            "question_type": repaired.get("question_type"),
            "answer_text": str(repaired.get("answer_text") or "")[:400] or None,
            "answer_status": row["answer_status"],
            "source_fidelity": row["source_fidelity"],
            "release_status": row["release_status"],
            "issue_count": row["issue_count"],
            "blocking_count": row["blocking_count"],                    "quality": _load(row["quality_json"], {}).get("overall"),
            "verification": _load(row["verification_json"], {}),
            "verification_status": _load(row["verification_json"], {}).get("status"),
        }

    def _issue_payload(self, row: Any) -> dict[str, Any]:
        return {
            "issue_id": row["issue_id"],
            "record_id": row["record_id"],
            "code": row["code"],
            "category": row["category"],
            "severity": row["severity"],
            "message": row["message"],
            "evidence": _load(row["evidence_json"], {}),
            "confidence": row["confidence"],
            "remediation": row["remediation"],
            "blocking": bool(row["blocking"]),
            "field": row["field"],
        }

    def record_detail(
        self, record_id: str, *, run_id: str | None = None
    ) -> dict[str, Any] | None:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return None
        row = self.session.execute(
            select(recovery_records).where(
                recovery_records.c.run_id == run_id,
                recovery_records.c.record_id == record_id,
            )
        ).mappings().first()
        if row is None:
            return None
        issues = self.session.execute(
            select(recovery_issues)
            .where(recovery_issues.c.run_id == run_id, recovery_issues.c.record_id == record_id)
            .order_by(recovery_issues.c.issue_id)
        ).mappings().all()
        repairs = self.session.execute(
            select(recovery_repairs)
            .where(recovery_repairs.c.run_id == run_id, recovery_repairs.c.record_id == record_id)
            .order_by(recovery_repairs.c.repair_id)
        ).mappings().all()
        candidates = self.session.execute(
            select(recovery_answer_candidates)
            .where(
                recovery_answer_candidates.c.run_id == run_id,
                recovery_answer_candidates.c.record_id == record_id,
            )
            .order_by(recovery_answer_candidates.c.candidate_id)
        ).mappings().all()
        return {
            "record_id": row["record_id"],
            "run_id": row["run_id"],
            "course_id": row["course_id"],
            "source_file": row["source_file"],
            "source_row": row["source_row"],
            "source_question_id": row["source_question_id"],
            "source_sha256": row["source_sha256"],
            "original": _load(row["original_json"], {}),
            "normalized": _load(row["normalized_json"], {}),
            "repaired": _load(row["repaired_json"], {}),
            "structure": _load(row["structure_json"], {}),
            "quality": _load(row["quality_json"], {}),
            "verification": _load(row["verification_json"], {}),
            "answer_status": row["answer_status"],
            "source_fidelity": row["source_fidelity"],
            "release_status": row["release_status"],
            "issues": [self._issue_payload(item) for item in issues],
            "repairs": [
                {
                    "repair_id": item["repair_id"],
                    "repair_type": item["repair_type"],
                    "field": item["field"],
                    "before": _load(item["before_json"], None),
                    "after": _load(item["after_json"], None),
                    "method": item["method"],
                    "confidence": item["confidence"],
                    "risk": item["risk"],
                    "status": item["status"],
                    "reason": item["reason"],
                    "reviewer": item["reviewer"],
                    "note": item["note"],
                }
                for item in repairs
            ],
            "answer_candidates": [
                {
                    "candidate_id": item["candidate_id"],
                    "answer_text": item["answer_text"],
                    "source_type": item["source_type"],
                    "source_name": item["source_name"],
                    "source_url": item["source_url"],
                    "source_reference": item["source_reference"],
                    "verification_status": item["verification_status"],
                    "confidence": item["confidence"],
                    "method": item["method"],
                    "evidence": _load(item["evidence_json"], {}),
                    "retrieved_at": item["retrieved_at"],
                    "official": bool(item["official"]),
                }
                for item in candidates
            ],
        }

    def list_issues(
        self,
        *,
        run_id: str | None = None,
        course_id: str | None = None,
        severity: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return []
        stmt = select(recovery_issues).where(recovery_issues.c.run_id == run_id)
        if course_id:
            stmt = stmt.where(recovery_issues.c.course_id == course_id)
        if severity:
            stmt = stmt.where(recovery_issues.c.severity == severity)
        rows = self.session.execute(
            stmt.order_by(recovery_issues.c.issue_id).limit(max(1, min(limit, 1000))).offset(offset)
        ).mappings().all()
        return [self._issue_payload(item) | {"course_id": item["course_id"]} for item in rows]

    def review_repair(
        self,
        repair_id: int,
        *,
        status: str,
        reviewer: str,
        note: str | None = None,
        edited_after: Any = None,
    ) -> dict[str, Any] | None:
        row = self.session.execute(
            select(recovery_repairs).where(recovery_repairs.c.repair_id == repair_id)
        ).mappings().first()
        if row is None:
            return None
        values: dict[str, Any] = {"status": status, "reviewer": reviewer, "note": note}
        if edited_after is not None:
            values["after_json"] = _json(edited_after)
        self.session.execute(
            recovery_repairs.update().where(recovery_repairs.c.repair_id == repair_id).values(**values)
        )
        return self.record_detail(row["record_id"], run_id=row["run_id"])

    def rollback(self, run_id: str) -> bool:
        row = self.get_run(run_id)
        if row is None:
            return False
        self.session.execute(
            recovery_runs.update()
            .where(recovery_runs.c.run_id == run_id)
            .values(status="rolled_back")
        )
        self.session.execute(
            recovery_records.update()
            .where(recovery_records.c.run_id == run_id)
            .values(release_status="quarantined")
        )
        self.session.execute(
            recovery_repairs.update()
            .where(recovery_repairs.c.run_id == run_id)
            .values(status="rolled_back")
        )
        return True

    def release_records(self, course_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return []
        rows = self.session.execute(
            select(recovery_records)
            .where(
                recovery_records.c.run_id == run_id,
                recovery_records.c.course_id == course_id,
                recovery_records.c.release_status == "released",
            )
            .order_by(recovery_records.c.source_row)
        ).mappings().all()
        return [
            {
                "record_id": row["record_id"],
                "course_id": row["course_id"],
                "source_question_id": row["source_question_id"],
                "repaired": _load(row["repaired_json"], {}),
                "quality": _load(row["quality_json"], {}),
                "answer_status": row["answer_status"],
                "release_status": row["release_status"],
            }
            for row in rows
        ]

    def save_documents(self, run_id: str, documents: list[SourceDocument]) -> None:
        if not documents:
            return
        self.session.execute(
            recovery_source_documents.insert(),
            [
                {
                    "run_id": run_id,
                    "course_id": item.course_id,
                    "path": item.path,
                    "kind": item.kind,
                    "sha256": item.sha256,
                    "page_count": item.page_count,
                    "text_chars": item.text_chars,
                    "question_fidelity_available": 1
                    if item.question_fidelity_available
                    else 0,
                    "metadata_json": _json(item.metadata),
                }
                for item in documents
            ],
        )


__all__ = ["RecoveryRepository"]
