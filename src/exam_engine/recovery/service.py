"""End-to-end dataset intelligence and recovery orchestration.

The service is deliberately independent of the 13 phase services. It reads the
same catalog/CSV parsers, stores its own audit tables and can export only
explicitly released records for later consumption.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.config import Settings
from exam_engine.datasets.catalog import (
    DatasetCatalog,
    ExamCategory,
    ExamSubject,
    discover,
    slugify,
)
from exam_engine.datasets.parsing import ParsedRow, parse_row
from exam_engine.ingest.adapters.text_adapter import decode_text
from exam_engine.ingest.normalize import fingerprint_text
from exam_engine.models.recovery import (
    RECOVERY_VERSION,
    RULE_REGISTRY_VERSION,
    AnswerCandidate,
    CourseResult,
    Issue,
    PassageGroup,
    RecordResult,
    Repair,
    RunResult,
    SourceCandidate,
    SourceDocument,
    make_record_id,
    now_iso,
)
from exam_engine.recovery.answers import (
    LocalAnswer,
    LocalAnswerIndex,
    TutorAnswerSolver,
    recover_answer,
)
from exam_engine.recovery.archives import OfficialArchiveDiscovery
from exam_engine.recovery.authenticity import authenticity_issues, source_fidelity
from exam_engine.recovery.crawl4ai_adapter import Crawl4AIFetcher, Crawl4AIHealth
from exam_engine.recovery.papers import (
    extract_paper_questions,
    extract_passage_groups,
    match_paper_questions,
    match_passage_children,
)
from exam_engine.recovery.providers import build_search_provider
from exam_engine.recovery.quality import (
    CONFLICT_STATUSES,
    course_quality,
    release_assessment,
    score_record,
)
from exam_engine.recovery.retrieval import (
    DatabaseRetrievalCache,
    DocumentFetcher,
    FetchedDocument,
    SourceRetriever,
)
from exam_engine.recovery.schema import SchemaProfile, detect_schema
from exam_engine.recovery.solver import DeterministicSolver
from exam_engine.recovery.sources import (
    AnswerKeyIndex,
    SourceDiscovery,
    classify_source,
    extract_cbse_marking_scheme_entries,
    extract_answer_key_entries,
    source_counts,
)
from exam_engine.recovery.structure import analyse_structure
from exam_engine.recovery.verification import (
    VERIFIED_STATUSES,
    answers_equivalent,
    verify_answer,
)
from exam_engine.storage.db import Database
from exam_engine.storage.recovery_repository import RecoveryRepository

logger = logging.getLogger(__name__)
_MAX_PDF_TEXT_PAGES = 60


@dataclass
class _Draft:
    subject: ExamSubject
    schema: SchemaProfile
    source_sha256: str
    rows: int
    records: list[RecordResult]
    source_documents: list[SourceDocument]
    source_texts: list[dict[str, Any]]
    source_candidates: list[SourceCandidate] = field(default_factory=list)
    answer_keys: list[dict[str, Any]] = field(default_factory=list)
    web_answer_keys: list[dict[str, Any]] = field(default_factory=list)
    paper_questions: list[Any] = field(default_factory=list)
    paper_passages: list[Any] = field(default_factory=list)
    source_matches: dict[str, dict[str, Any]] = field(default_factory=dict)
    passage_matches: list[dict[str, Any]] = field(default_factory=list)
    retrieved_documents: list[FetchedDocument] = field(default_factory=list)
    retrievals: list[dict[str, Any]] = field(default_factory=list)
    retrieval_failures: int = 0
    cache_hits: int = 0
    passages: list[PassageGroup] = field(default_factory=list)
    archive_state: str = "not_configured"
    archive_ids: list[str] = field(default_factory=list)
    search_state: str = "not_configured"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DatasetRecoveryService:
    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        web_search: WebSearch | None = None,
        model_solver: Any = None,
    ) -> None:
        self.database = database
        self.settings = settings or Settings.from_env()
        self._web_search = web_search
        self._model_solver = model_solver
        self._deterministic_solver = DeterministicSolver()
        self._model_used = 0
        self._web_used = 0
        self._source_queries = 0
        self._search_provider_name = "auto"
        self._search_provider_status: dict[str, Any] = {}
        self._crawl_engine = "crawl4ai"
        self._crawl_engine_actual = "http"
        self._crawl_health: dict[str, Any] = {}
        self._archive_status: list[dict[str, Any]] = []
        self._retrieval_failures = 0
        self._cache_hits = 0

    # -- inventory ---------------------------------------------------------
    def inventory(self, root: Path | str = "datasets") -> dict[str, Any]:
        catalog = discover(root)
        files: list[dict[str, Any]] = []
        for subject in catalog.subjects:
            headers, row_count, sha, _ = self._read_headers(subject.csv_path)
            profile = detect_schema(headers)
            docs = self._source_documents(subject)
            files.append(
                {
                    "course_id": subject.course_id,
                    "course_code": subject.course_code,
                    "subject": subject.subject_name,
                    "category_id": subject.category_id,
                    "csv": subject.csv_path.as_posix(),
                    "sha256": sha,
                    "rows": row_count,
                    "headers": headers,
                    "schema": profile.as_dict(),
                    "syllabus": [path.as_posix() for path in subject.syllabus_paths],
                    "source_documents": [
                        {
                            "path": doc.path,
                            "kind": doc.kind,
                            "question_fidelity_available": doc.question_fidelity_available,
                        }
                        for doc in docs
                    ],
                }
            )
        return {
            "root": catalog.root.as_posix(),
            "categories": [item.category_id for item in catalog.categories],
            "files": files,
            "totals": {
                "categories": len(catalog.categories),
                "courses": len(files),
                "csv": len(files),
                "rows": sum(item["rows"] for item in files),
                "pdf": sum(len(item["source_documents"]) for item in files),
            },
        }

    # -- analysis ----------------------------------------------------------
    def analyze(
        self,
        root: Path | str = "datasets",
        *,
        course_ids: list[str] | None = None,
        category_ids: list[str] | None = None,
        mode: str = "auto",
        workflow: str = "full",
        dry_run: bool = False,
        use_web: bool = False,
        use_model: bool = False,
        provider: str = "auto",
        fetch_sources: bool = False,
        crawl_engine: str = "crawl4ai",
        retrieval_limit: int = 3,
        retrieval_follow_links: bool = True,
        retry_failed: bool = False,
        web_limit: int = 0,
        source_limit: int = 0,
        model_limit: int = 0,
        run_id: str | None = None,
        resume_run_id: str | None = None,
        incremental: bool = False,
        csv_path: Path | str | None = None,
        course_id: str | None = None,
        course_name: str | None = None,
        course_code: str | None = None,
        category_id: str = "custom",
        output_dir: Path | str | None = None,
    ) -> RunResult:
        if mode not in {"auto", "review", "audit"}:
            raise ValueError("mode must be auto, review or audit")
        workflows = {"inventory", "analyze", "recover", "verify", "reconstruct", "full"}
        if workflow not in workflows:
            raise ValueError("workflow must be one of " + ", ".join(sorted(workflows)))
        if crawl_engine not in {"crawl4ai", "http"}:
            raise ValueError("crawl_engine must be crawl4ai or http")
        self._crawl_engine = crawl_engine
        source_discovery_enabled = workflow in {"recover", "verify", "reconstruct", "full"}
        reconstruction_enabled = workflow in {"reconstruct", "full", "analyze", "recover"}
        started = time.perf_counter()
        if csv_path is not None:
            path = Path(csv_path)
            if not path.is_file():
                raise FileNotFoundError(f"CSV file not found: {path}")
            subject_name = course_name or path.stem
            course_slug = slugify(subject_name) or "uploaded"
            category = ExamCategory(
                category_id=category_id,
                name=category_id,
                institution_code="uploaded",
                institution_name="Uploaded dataset",
                programme="Uploaded dataset",
                root=path.parent,
            )
            subject = ExamSubject(
                category=category,
                subject_name=subject_name,
                subject_slug=course_slug,
                course_id=course_id or f"{category_id}-{course_slug}",
                course_code=course_code or course_slug[:32].upper(),
                csv_path=path,
                syllabus_paths=(),
                syllabus_version=None,
            )
            selected = [subject]
            run_root = path.parent.as_posix()
        else:
            catalog = discover(root)
            selected = self._select_subjects(catalog, course_ids, category_ids)
            run_root = Path(root).as_posix()
        run_id = resume_run_id or run_id or self._new_run_id()
        run = RunResult(
            run_id=run_id,
            started_at=now_iso(),
            finished_at="",
            mode=mode,
            dry_run=dry_run,
            root=run_root,
            courses=[],
            totals={},
            status="running",
        )
        repo: RecoveryRepository | None = None
        if not dry_run:
            with self.database.transaction() as session:
                repo = RecoveryRepository(session)
                repo.start_run(
                    run_id=run_id,
                    mode=mode,
                    dry_run=dry_run,
                    root=run.root,
                    options={
                        "course_ids": course_ids or [],
                        "category_ids": category_ids or [],
                        "workflow": workflow,
                        "use_web": use_web and source_discovery_enabled,
                        "use_model": use_model,
                        "provider": provider,
                        "fetch_sources": fetch_sources,
                        "crawl_engine": crawl_engine,
                        "retrieval_limit": retrieval_limit,
                        "retrieval_follow_links": retrieval_follow_links,
                        "retry_failed": retry_failed,
                        "web_limit": web_limit,
                        "source_limit": source_limit,
                        "model_limit": model_limit,
                    },
                )
        completed = set()
        prior_hashes: dict[str, str] = {}
        if repo is not None and (resume_run_id or incremental):
            with self.database.session() as session:
                prior = RecoveryRepository(session).list_courses(run_id)
            prior_hashes = {
                row["course_id"]: row.get("source_sha256", "") for row in prior
            }
            completed = {row["course_id"] for row in prior}
        if use_web and self._web_search is None:
            self._search_provider_name = provider
            self._web_search = build_search_provider(
                provider, settings=self.settings.tutor, env=__import__("os").environ
            )
            if self._web_search is not None:
                self._search_provider_status = self._web_search.status()
        elif self._web_search is not None:
            status = getattr(self._web_search, "status", None)
            self._search_provider_status = status() if callable(status) else {}
        if use_model and self._model_solver is None:
            self._model_solver = TutorAnswerSolver(self.settings.tutor)

        drafts: list[_Draft] = []
        for subject in selected:
            sha = self._file_sha(subject.csv_path)
            if (
                (resume_run_id or incremental)
                and subject.course_id in completed
                and prior_hashes.get(subject.course_id) == sha
            ):
                run.warnings.append(f"{subject.course_id}: unchanged; resumed from prior run")
                continue
            draft = self._read_and_structure(subject, root, mode)
            drafts.append(draft)

        if (use_web or fetch_sources) and source_discovery_enabled:
            self._discover_sources(
                drafts,
                source_limit=source_limit or len(drafts),
                fetch_sources=fetch_sources,
                retrieval_limit=retrieval_limit,
                follow_links=retrieval_follow_links,
                retry_failed=retry_failed,
                crawl_engine=crawl_engine,
            )

        prior_records: list[dict[str, Any]] = []
        if repo is not None and (resume_run_id or incremental) and completed:
            # A resumed run skips the completed courses, but their local answers
            # are still valid duplicate evidence for the courses being processed.
            with self.database.session() as session:
                prior_records = RecoveryRepository(session).list_record_payloads(
                    run_id, completed
                )

        index = LocalAnswerIndex()

        def index_answer(
            *,
            repaired: dict[str, Any],
            structure: dict[str, Any],
            course_id: str,
            source_file: str,
            source_row: int,
            source_question_id: str,
        ) -> None:
            if not repaired.get("answer_text") or structure.get("child_question"):
                return
            index.add(
                LocalAnswer(
                    question_text=str(repaired.get("question_text") or ""),
                    options=tuple(
                        (item.get("key", ""), item.get("text", ""))
                        for item in repaired.get("options", [])
                    ),
                    answer_text=str(repaired["answer_text"]),
                    course_id=course_id,
                    source_file=source_file,
                    source_row=source_row,
                    source_question_id=source_question_id,
                )
            )

        for payload in prior_records:
            index_answer(
                repaired=json.loads(payload.get("repaired_json") or "{}"),
                structure=json.loads(payload.get("structure_json") or "{}"),
                course_id=str(payload.get("course_id") or ""),
                source_file=str(payload.get("source_file") or ""),
                source_row=int(payload.get("source_row") or 0),
                source_question_id=str(payload.get("source_question_id") or ""),
            )
        for draft in drafts:
            for record in draft.records:
                index_answer(
                    repaired=record.repaired,
                    structure=record.structure,
                    course_id=record.course_id,
                    source_file=record.source_file,
                    source_row=record.source_row,
                    source_question_id=record.source_question_id,
                )

        for draft in drafts:
            course_started = time.perf_counter()
            self._finalize_draft(
                draft,
                index,
                mode=mode,
                use_web=use_web and source_discovery_enabled,
                use_model=use_model,
                web_limit=web_limit,
                model_limit=model_limit,
                reconstruct=reconstruction_enabled,
            )
            course = self._make_course_result(draft, int((time.perf_counter() - course_started) * 1000))
            run.courses.append(course)
            if repo is not None:
                with self.database.transaction() as session:
                    RecoveryRepository(session).save_course(
                        run_id, course, draft.source_documents
                    )
        run.finished_at = now_iso()
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        if dry_run:
            run.totals = self._totals(run.courses)
            run.status = "partial" if any(course.errors for course in run.courses) else "ok"
        else:
            with self.database.transaction() as session:
                repo = RecoveryRepository(session)
                if incremental and not drafts:
                    # The existing run is still the complete snapshot.
                    prior = repo.list_courses(run_id)
                    run.totals = self._totals_from_reports(prior)
                else:
                    run.totals = self._totals(run.courses)
                run.status = "partial" if any(course.errors for course in run.courses) else "ok"
                repo.finish_run(run)
        if output_dir:
            self.write_reports(run, output_dir)
        return run

    def _select_subjects(
        self,
        catalog: DatasetCatalog,
        course_ids: list[str] | None,
        category_ids: list[str] | None,
    ) -> list[ExamSubject]:
        subjects = list(catalog.subjects)
        if category_ids:
            wanted = set(category_ids)
            subjects = [item for item in subjects if item.category_id in wanted]
        if course_ids:
            wanted = set(course_ids)
            subjects = [item for item in subjects if item.course_id in wanted]
        if not subjects:
            raise ValueError("no courses matched the selected filters")
        return subjects

    def _discover_sources(
        self,
        drafts: list[_Draft],
        *,
        source_limit: int,
        fetch_sources: bool,
        retrieval_limit: int,
        follow_links: bool,
        retry_failed: bool,
        crawl_engine: str = "crawl4ai",
    ) -> None:
        """Discover and retrieve official/public documents for each course.

        Official archive discovery runs even without a keyed search provider.
        Crawl4AI is reused for the whole run; PDFs go through the existing
        PyMuPDF extractor.  A snippet or an unparsed PDF never becomes answer
        evidence.
        """

        if source_limit <= 0:
            return
        browser: Crawl4AIFetcher | None = None
        browser_error = ""
        self._crawl_engine = crawl_engine
        if fetch_sources and crawl_engine == "crawl4ai":
            if Crawl4AIFetcher.available():
                browser = Crawl4AIFetcher(headless=True, timeout=25.0)
                self._crawl_engine_actual = "crawl4ai"
            else:
                browser_error = (
                    "Crawl4AI is unavailable; HTML discovery fell back to the HTTP fetcher. "
                    "Run `recovery crawl-health` for diagnostics."
                )
        self._crawl_health = Crawl4AIHealth.quick_status()
        if browser is not None:
            self._crawl_health.update(status="available", browser_launch_ok=None)
        http_fetcher = DocumentFetcher(timeout=20.0)
        archive_discovery = OfficialArchiveDiscovery(
            browser=browser,
            page_fetch=http_fetcher.fetch_document,
        )
        self._archive_status = archive_discovery.status()
        discovery = (
            SourceDiscovery(self._web_search, result_limit=3)
            if self._web_search is not None
            else None
        )
        retriever = (
            SourceRetriever(
                http_fetcher,
                browser_fetcher=browser,
                cache=DatabaseRetrievalCache(self.database),
                retry_failed=retry_failed,
            )
            if fetch_sources
            else None
        )
        try:
            for draft in drafts:
                if self._source_queries >= source_limit:
                    break
                if browser_error:
                    draft.warnings.append(browser_error)
                years = sorted(
                    {
                        year
                        for record in draft.records
                        if (year := self._record_year(record)) is not None
                    }
                )
                labels = sorted(
                    {
                        str(record.repaired.get("paper_label") or "")
                        for record in draft.records
                        if str(record.repaired.get("paper_label") or "").strip()
                    }
                )
                set_counts: Counter[str] = Counter()
                for record in draft.records:
                    identity_text = " ".join(
                        str(value or "")
                        for value in (
                            record.source_question_id,
                            record.original.get("question_id"),
                            record.repaired.get("paper_label"),
                        )
                    )
                    set_match = re.search(
                        r"(?:set|shift)[-_ ]?(\d)", identity_text, re.IGNORECASE
                    ) or re.search(
                        r"_(?:cs|ce|s)(\d)(?:_|$)", identity_text, re.IGNORECASE
                    )
                    if set_match:
                        set_counts[set_match.group(1)] += 1
                sets = [item for item, _count in set_counts.most_common(1)]
                matched_specs = archive_discovery.matching_specs(
                    exam_name=draft.subject.category.name,
                    course_id=draft.subject.course_id,
                    years=years,
                )
                draft.archive_ids = [spec.archive_id for spec in matched_specs]
                if not matched_specs:
                    draft.archive_state = "not_configured"
                elif not any(spec.available for spec in matched_specs):
                    draft.archive_state = "unavailable"
                else:
                    draft.archive_state = "configured"
                candidates: list[SourceCandidate] = []
                archive_candidate_count = 0
                archive_errors_before = len(archive_discovery.last_errors)
                if years and archive_discovery.specs:
                    try:
                        archive_found = archive_discovery.discover(
                            course_id=draft.subject.course_id,
                            exam_name=draft.subject.category.name,
                            subject_name=draft.subject.subject_name,
                            years=years,
                            paper_labels=labels,
                            sets=sets,
                            query_limit=1,
                        )
                        archive_candidate_count += len(archive_found)
                        candidates.extend(archive_found)
                    except Exception as exc:  # noqa: BLE001 - isolate one archive
                        draft.errors.append(
                            f"official archive discovery failed: {type(exc).__name__}: {exc}"[:500]
                        )
                if discovery is not None:
                    try:
                        candidates.extend(
                            discovery.discover(
                                course_id=draft.subject.course_id,
                                exam_name=draft.subject.category.name,
                                subject_name=draft.subject.subject_name,
                                years=years,
                                paper_labels=labels,
                                query_limit=1,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - isolate one provider
                        draft.errors.append(
                            f"source discovery failed: {type(exc).__name__}: {exc}"[:500]
                        )
                search_candidates = sum(
                    1 for item in candidates if item.provider != "official_archive"
                )
                if discovery is None:
                    draft.search_state = "not_configured"
                elif search_candidates:
                    draft.search_state = "documents_found"
                elif self._search_provider_status.get("last_error"):
                    draft.search_state = "provider_failed"
                else:
                    draft.search_state = "no_document"
                deduped: dict[str, SourceCandidate] = {}
                for candidate in candidates:
                    deduped.setdefault(candidate.url, candidate)
                def candidate_order(item: SourceCandidate) -> tuple:
                    variant = str(
                        item.evidence.get("document_variant") or ""
                    ).casefold()
                    if item.document_type == "answer_key" and variant == "final":
                        priority = 0
                    elif item.document_type in {
                        "marking_scheme",
                        "question_paper",
                        "solved_paper",
                    }:
                        priority = 2
                    else:
                        priority = 3
                    year = int(
                        (item.evidence.get("identity") or {}).get("year") or 0
                    )
                    subject_priority = int(
                        item.evidence.get("subject_priority") or 99
                    )
                    return (
                        item.source_tier,
                        priority,
                        subject_priority,
                        -item.reliability,
                        -year,
                        item.title.casefold(),
                        item.url,
                    )

                ordered = sorted(deduped.values(), key=candidate_order)
                candidates = []
                seen_final_keys: set[tuple] = set()
                for item in ordered:
                    variant = str(
                        item.evidence.get("document_variant") or ""
                    ).casefold()
                    if item.document_type == "answer_key" and variant == "final":
                        identity = item.evidence.get("identity") or {}
                        key = (
                            item.evidence.get("archive_id"),
                            identity.get("subject") or identity.get("exam"),
                            identity.get("year"),
                        )
                        if key in seen_final_keys:
                            continue
                        seen_final_keys.add(key)
                    candidates.append(item)
                self._source_queries += 1
                draft.source_candidates = candidates
                retrieval_candidates = self._select_retrieval_candidates(
                    candidates, retrieval_limit
                )
                papers = sum(
                    1
                    for item in candidates
                    if item.document_type in {"question_paper", "solved_paper"}
                )
                keys = 0
                discovered_keys = sum(
                    1 for item in candidates if item.document_type == "answer_key"
                )
                if retriever is not None and retrieval_candidates:
                    try:
                        retrieved = retriever.retrieve(
                            retrieval_candidates,
                            max_documents=max(1, int(retrieval_limit)),
                            follow_links=1 if follow_links else 0,
                        )
                    except Exception as exc:  # noqa: BLE001
                        draft.errors.append(
                            f"source retrieval failed: {type(exc).__name__}: {exc}"[:500]
                        )
                        retrieved = []
                    for item in retrieved:
                        candidate = item.candidate
                        fetched = item.fetched
                        if item.cache_hit:
                            draft.cache_hits += 1
                            self._cache_hits += 1
                        draft.retrieved_documents.append(fetched)
                        kind = candidate.document_type
                        if kind == "other":
                            kind, _tier, _rel, _why = classify_source(
                                title=fetched.title,
                                url=fetched.final_url,
                                snippet=candidate.snippet,
                            )
                        if kind not in {
                            "question_paper",
                            "solved_paper",
                            "answer_key",
                            "official_solution",
                            "marking_scheme",
                        }:
                            continue
                        identity = dict(candidate.evidence.get("identity") or {})
                        zip_set = (fetched.metadata or {}).get("zip_set_resolved")
                        if zip_set:
                            identity["set"] = None if zip_set == "all" else zip_set
                            identity["set_ambiguous"] = bool(
                                (fetched.metadata or {}).get("zip_set_ambiguous")
                            )
                        member_code = (fetched.metadata or {}).get("zip_member_code")
                        if member_code:
                            identity["paper_code"] = member_code
                            identity["subject_code"] = (
                                identity.get("subject_code") or member_code
                            )
                            identity["region"] = (
                                fetched.metadata or {}
                            ).get("zip_member_region")
                        session_ambiguous = bool(
                            candidate.evidence.get("session_ambiguous")
                        ) or bool(identity.get("set_ambiguous"))
                        text = fetched.text
                        entry = {
                            "url": fetched.final_url,
                            "path": fetched.final_url,
                            "title": fetched.title or candidate.title,
                            "text": text,
                            "kind": kind,
                            "tier": int(candidate.source_tier),
                            "provider": candidate.provider,
                            "query": candidate.query,
                            "sha256": fetched.sha256,
                            "retrieved_at": fetched.retrieved_at,
                            "cache_hit": item.cache_hit,
                            "extraction_method": fetched.extraction_method,
                            "crawler": fetched.crawler,
                            "browser_ms": fetched.browser_ms,
                            "source": (
                                "official_archive"
                                if candidate.provider == "official_archive"
                                else "public_web"
                            ),
                            "identity": identity,
                            "session_ambiguous": session_ambiguous,
                            "key_variant": candidate.evidence.get("document_variant"),
                        }
                        draft.retrievals.append(entry)
                        draft.source_texts.append(entry)
                        if kind == "answer_key":
                            entries = extract_answer_key_entries(text)
                            if entries:
                                index = AnswerKeyIndex()
                                index.extend(entries)
                                draft.web_answer_keys.append(
                                    {
                                        "index": index,
                                        "source_name": fetched.title or candidate.title,
                                        "reference": fetched.final_url,
                                        "url": fetched.final_url,
                                        "tier": int(candidate.source_tier),
                                        "entries": len(entries),
                                        "sha256": fetched.sha256,
                                        "retrieved_at": fetched.retrieved_at,
                                        "provider": candidate.provider,
                                        "identity": identity,
                                        "session_ambiguous": session_ambiguous,
                                        "key_variant": candidate.evidence.get(
                                            "document_variant"
                                        ),
                                    }
                                )
                                keys += 1
                        if kind == "marking_scheme":
                            entries = extract_cbse_marking_scheme_entries(text)
                            if entries:
                                index = AnswerKeyIndex()
                                index.extend(entries)
                                draft.web_answer_keys.append(
                                    {
                                        "index": index,
                                        "source_name": fetched.title or candidate.title,
                                        "reference": fetched.final_url,
                                        "url": fetched.final_url,
                                        "tier": int(candidate.source_tier),
                                        "entries": len(entries),
                                        "sha256": fetched.sha256,
                                        "retrieved_at": fetched.retrieved_at,
                                        "provider": candidate.provider,
                                        "identity": identity,
                                        "session_ambiguous": session_ambiguous,
                                        "key_variant": "marking_scheme",
                                    }
                                )
                                keys += 1
                        if kind in {
                            "question_paper",
                            "solved_paper",
                            "official_solution",
                        }:
                            extracted = extract_paper_questions(text)
                            if extracted:
                                for question in extracted:
                                    question.evidence.update(
                                        {
                                            "source_url": fetched.final_url,
                                            "source_sha256": fetched.sha256,
                                            "source_kind": kind,
                                            "source_identity": identity,
                                            "source_tier": int(candidate.source_tier),
                                            "source_provider": candidate.provider,
                                        }
                                    )
                                draft.paper_questions.extend(extracted)
                                papers += 1
                            draft.paper_passages.extend(extract_passage_groups(text))
                    for candidate in candidates:
                        if candidate.retrieval_status == "blocked":
                            draft.retrieval_failures += 1
                            self._retrieval_failures += 1
                        elif candidate.retrieval_status == "failed":
                            draft.retrieval_failures += 1
                            self._retrieval_failures += 1
                archive_retrieved = sum(
                    1
                    for item in draft.retrievals
                    if item.get("source") == "official_archive"
                )
                if draft.archive_state == "configured":
                    if archive_candidate_count == 0:
                        draft.archive_state = "no_document"
                    elif archive_retrieved == 0:
                        draft.archive_state = "extraction_failed"
                    else:
                        draft.archive_state = "retrieved"
                new_errors = archive_discovery.last_errors[archive_errors_before:]
                if new_errors:
                    draft.warnings.extend(new_errors[:3])
                draft.warnings.append(
                    "source discovery: "
                    f"{len(candidates)} discovered candidate(s), "
                    f"{len(retrieval_candidates)} selected for the retrieval budget, "
                    f"{papers} retrieved paper(s), "
                    f"{keys} usable key document(s), {discovered_keys} key candidate(s); "
                    f"archive_state={draft.archive_state}; search_state={draft.search_state}; "
                    "official archive and Crawl4AI health are recorded separately; "
                    "snippets are not answer evidence"
                )
        finally:
            if browser is not None:
                browser.close()

    @staticmethod
    def _select_retrieval_candidates(
        candidates: list[SourceCandidate], retrieval_limit: int
    ) -> list[SourceCandidate]:
        """Allocate a small budget by document role, year and identity.

        A paper-only budget is worse than no budget: it can never yield an
        official answer.  Select the best complete paper/key pair first, using
        the same explicit subject/session/set identity on both sides.  Remaining
        slots are filled deterministically so acquisition, accounting and
        diagnostics are reproducible.
        """

        limit = max(1, int(retrieval_limit))
        unique: list[SourceCandidate] = []
        seen: set[str] = set()
        for item in candidates:
            if item.url in seen:
                continue
            seen.add(item.url)
            unique.append(item)
        if not unique or limit <= 0:
            return []

        def kind(item: SourceCandidate) -> str:
            if item.document_type == "marking_scheme":
                return "marking_scheme"
            if item.document_type == "answer_key":
                variant = str(item.evidence.get("document_variant") or "").casefold()
                return "final_key" if variant == "final" else "provisional_key"
            if item.document_type in {"question_paper", "solved_paper"}:
                return "paper"
            return "other"

        def identity(item: SourceCandidate) -> dict[str, Any]:
            return dict(item.evidence.get("identity") or {})

        def year(item: SourceCandidate) -> int:
            try:
                return int(identity(item).get("year") or 0)
            except (TypeError, ValueError):
                return 0

        def year_order(item: SourceCandidate) -> str:
            return str(item.evidence.get("year_order") or "newest").casefold()

        def year_score(item: SourceCandidate) -> tuple[int, int]:
            value = year(item)
            return (value if year_order(item) == "oldest" else -value, -value)

        def pair_key(item: SourceCandidate) -> tuple[str, str, str, str, str]:
            data = identity(item)
            return (
                str(data.get("year") or ""),
                str(data.get("subject_code") or ""),
                str(data.get("subject_variant") or ""),
                str(data.get("session") or ""),
                str(data.get("set") or ""),
            )

        def subject_priority(item: SourceCandidate) -> int:
            try:
                return int(item.evidence.get("subject_priority") or 99)
            except (TypeError, ValueError):
                return 99

        keys = [item for item in unique if kind(item) in {"final_key", "marking_scheme"}]
        papers = [item for item in unique if kind(item) == "paper"]
        selected: list[SourceCandidate] = []
        selected_urls: set[str] = set()

        def add(item: SourceCandidate) -> None:
            if item.url not in selected_urls and len(selected) < limit:
                selected_urls.add(item.url)
                selected.append(item)

        pairs: list[tuple[tuple[int, int, int, str], SourceCandidate, SourceCandidate]] = []
        for key in keys:
            for paper in papers:
                if pair_key(key) != pair_key(paper):
                    continue
                score = (
                    *year_score(paper),
                    subject_priority(key),
                )
                pairs.append((score, key, paper))
        if pairs:
            pairs.sort(key=lambda row: (row[0], row[1].title.casefold(), row[2].title.casefold()))
            # Add complete pairs in authority/year order.  A lone key has no
            # question-text identity, so it must not consume a slot that would
            # have completed another paper/key pair.
            for _score, key, paper in pairs:
                add(key)
                add(paper)
                if len(selected) >= limit:
                    break
        else:
            if keys:
                add(sorted(keys, key=lambda item: (subject_priority(item), year_score(item), item.url))[0])
            if papers:
                best_year = year(selected[0]) if selected else year(papers[0])
                same_year = [item for item in papers if year(item) == best_year]
                for paper in sorted(same_year or papers, key=lambda item: (subject_priority(item), year_score(item), item.url)):
                    add(paper)
                    if len(selected) >= limit:
                        break

        # Deterministic fill: same year as the selected key/paper first, then
        # explicit identity specificity, then the configured source order.
        anchor_year = year(selected[0]) if selected else (year(unique[0]) if unique else 0)
        fill = sorted(
            unique,
            key=lambda item: (
                0 if year(item) == anchor_year else 1,
                subject_priority(item),
                {"final_key": 0, "marking_scheme": 1, "paper": 2, "provisional_key": 3}.get(kind(item), 4),
                year_score(item),
                item.url,
            ),
        )
        for item in fill:
            if len(selected) >= limit:
                break
            add(item)
        for item in selected:
            item.retrieval_status = "selected"
        return selected

    def _answer_keys(
        self, documents: list[SourceDocument], texts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        by_path = {item["path"]: item for item in texts}
        for document in documents:
            if document.kind != "answer_key":
                continue
            text = str((by_path.get(document.path) or {}).get("text") or "")
            entries = extract_answer_key_entries(text)
            if not entries:
                continue
            index = AnswerKeyIndex()
            index.extend(entries)
            sources.append(
                {
                    "index": index,
                    "source_name": Path(document.path).name,
                    "reference": document.path,
                    "url": None,
                    "tier": int(document.metadata.get("source_tier") or 1),
                    "entries": len(entries),
                }
            )
        return sources

    @staticmethod
    def _record_year(record: RecordResult) -> int | None:
        """Infer an annual identity from the dataset id when metadata is blank."""

        values = [
            record.repaired.get("exam_year"),
            record.normalized.get("exam_year"),
            record.repaired.get("paper_label"),
            record.source_question_id,
            record.source_file,
            record.original.get("question_id"),
        ]
        for value in values:
            text = str(value or "").strip()
            if text.isdigit() and len(text) == 4:
                return int(text)
        combined = " ".join(str(value or "") for value in values)
        match = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", combined)
        return int(match.group(1)) if match else None

    @classmethod
    def _official_answer(cls, record: RecordResult, draft: _Draft) -> dict[str, Any] | None:
        number = cls._question_number(
            record.original.get("original_question_number")
            or record.source_question_id
        )
        source_match = draft.source_matches.get(record.record_id)
        # A question number and a key number are not sufficient identity.  The
        # record must first have matched the actual retrieved paper text.
        if not source_match:
            return None
        matched_number = cls._question_number(source_match.get("question_number"))
        if matched_number != number:
            return None
        year = cls._record_year(record)
        sources = [
            *draft.answer_keys,
            *sorted(
                draft.web_answer_keys,
                key=lambda item: 0 if item.get("key_variant") == "final" else 1,
            ),
        ]
        paper_identity = dict(source_match.get("source_identity") or {})
        paper_code = str(paper_identity.get("subject_code") or "")
        paper_region = str(paper_identity.get("region") or "")
        paper_session = str(paper_identity.get("session") or "")
        record_booklet = cls._record_booklet(record)
        record_set = cls._record_set(record)
        record_region = cls._record_region(record)
        for source in sources:
            if int(source.get("tier") or 0) != 1:
                continue
            identity = dict(source.get("identity") or {})
            key_code = str(identity.get("subject_code") or "")
            key_session = str(identity.get("session") or "")
            key_region = str(identity.get("region") or "")
            # Identity is never optional merely because the archive adapter did
            # not mark a document ambiguous.  A CES/CS paper and key from
            # different sessions share question numbers and must never meet.
            if paper_code and key_code and paper_code.casefold() != key_code.casefold():
                continue
            if paper_session and key_session and paper_session != key_session:
                continue
            # CBSE uses different paper codes for Delhi and Outside Delhi.  A
            # matching question number or even matching text in a shared
            # question cannot bridge those identities.
            if record_region and paper_region and record_region != paper_region:
                continue
            if paper_region and key_region and paper_region != key_region:
                continue
            if source.get("session_ambiguous") or identity.get("set_ambiguous"):
                # Multi-session/multi-set authorities publish different keys for
                # the same question number.  The retrieved paper's explicit
                # subject/session code must equal the key's code.
                if (
                    not paper_code
                    or not key_code
                    or paper_code.casefold() != key_code.casefold()
                ):
                    continue
            key_booklet = str(identity.get("booklet") or "")
            if key_booklet:
                if not record_booklet or key_booklet != record_booklet:
                    continue
            if source.get("key_variant") == "marking_scheme":
                key_set = str(identity.get("set") or "")
                if not key_set or not record_set or key_set != record_set:
                    continue
            source_year = identity.get("year")
            reference = " ".join(
                str(source.get(key) or "")
                for key in ("source_name", "reference", "url")
            )
            if year is not None and source_year is not None and int(source_year) != year:
                continue
            if year is not None and source_year is None and str(year) not in reference:
                continue
            entries = source["index"].match(number)
            answers = {entry.answer for entry in entries}
            if len(answers) != 1:
                # A key with duplicate/contradictory answers for the same number
                # is not usable as official evidence.
                continue
            entry = entries[0]
            answer = str(entry.answer).strip()
            if not answer:
                continue
            metadata = dict(entry.metadata or {})
            if answer.upper().rstrip("*").strip() == "MTA" or metadata.get("marks_to_all"):
                # The final key explicitly marks this question as awarded to
                # everyone.  It is not an answer and must not fall through to a
                # provisional key for a different answer.
                return None
            qtype = str(metadata.get("question_type") or "").upper()
            if qtype == "MSQ":
                letters = sorted(set(re.findall(r"[A-H]", answer.upper())))
                if len(letters) >= 2:
                    answer = ",".join(letters)
            accepted_range: dict[str, float] | None = None
            if qtype == "NAT" and " to " in answer.casefold():
                low, high = re.split(r"(?i)\s+to\s+", answer, maxsplit=1)
                try:
                    accepted_range = {"low": float(low.strip()), "high": float(high.strip())}
                    if accepted_range["low"] == accepted_range["high"]:
                        answer = low.strip()
                except ValueError:
                    accepted_range = None
            return {
                "answer": answer,
                "source_name": source["source_name"],
                "reference": source["reference"],
                "url": source.get("url"),
                "evidence": {
                    "official_tier": source["tier"],
                    "matched_question_number": str(number),
                    "key_entries": source["entries"],
                    "source_sha256": source.get("sha256"),
                    "retrieved_at": source.get("retrieved_at"),
                    "provider": source.get("provider"),
                    "document_reference": source.get("reference"),
                    "key_variant": source.get("key_variant") or metadata.get("key_variant"),
                    "question_type": qtype or None,
                    "session": metadata.get("session") or identity.get("session"),
                    "accepted_range": accepted_range,
                    "source_identity": identity,
                    "paper_match": source_match,
                    "note": (
                        "Exact question-number mapping in a retrieved tier-1 key "
                        "document, after the question text matched the retrieved paper."
                    ),
                },
            }
        return None

    @staticmethod
    def _question_number(value: Any) -> str:
        """Normalize labels such as ``AE Q19`` and ``Q.19`` to ``19``."""

        text = str(value or "").strip()
        match = re.search(r"(\d+)\s*$", text)
        return match.group(1) if match else text

    @staticmethod
    def _record_set(record: RecordResult) -> str:
        text = " ".join(
            str(value or "")
            for value in (
                record.source_question_id,
                record.original.get("question_id"),
                record.repaired.get("paper_label"),
            )
        )
        match = re.search(r"(?:set|shift)[-_ ]?(\d)", text, re.IGNORECASE) or re.search(
            r"_(?:cs|ce|s)(\d)(?:_|$)", text, re.IGNORECASE
        )
        return match.group(1) if match else ""

    @staticmethod
    def _record_region(record: RecordResult) -> str:
        text = " ".join(
            str(value or "")
            for value in (
                record.source_question_id,
                record.original.get("question_id"),
                record.repaired.get("paper_label"),
            )
        )
        compact = re.sub(r"[^a-z]", "", text.casefold())
        if "outsidedelhi" in compact:
            return "2"
        if "foreign" in compact:
            return "3"
        if "delhi" in compact:
            return "1"
        return ""

    @staticmethod
    def _record_booklet(record: RecordResult) -> str:
        text = " ".join(
            str(value or "")
            for value in (
                record.source_question_id,
                record.original.get("question_id"),
                record.repaired.get("paper_label"),
                record.normalized.get("paper_label"),
            )
        )
        match = re.search(r"(?:booklet|code)[-_ ]?(\d{1,3})", text, re.IGNORECASE)
        return match.group(1) if match else ""

    def _build_passages(self, draft: _Draft) -> list[PassageGroup]:
        """Create parent/child evidence groups only where a passage is explicit."""

        grouped: dict[str, list[RecordResult]] = defaultdict(list)
        children_by_parent: dict[str, list[RecordResult]] = defaultdict(list)
        for record in draft.records:
            if record.structure.get("child_question"):
                parent = str(record.structure.get("parent_record_id") or "")
                if parent:
                    children_by_parent[parent].append(record)
                continue
            passage = " ".join(str(record.structure.get("passage") or "").split())
            if passage:
                grouped[passage].append(record)
        groups: list[PassageGroup] = []
        for passage, records in grouped.items():
            inline_children = [
                child for record in records for child in children_by_parent.get(record.record_id, [])
            ]
            if len(records) < 2 and not inline_children:
                continue
            passage_id = "psg_" + hashlib.sha256(
                f"{draft.subject.course_id}\x1f{passage}".encode("utf-8")
            ).hexdigest()[:20]
            parent_id = records[0].record_id if inline_children else None
            child_ids = [child.record_id for child in inline_children] or [
                record.record_id for record in records
            ]
            for record in records:
                record.structure["passage_group_id"] = passage_id
            for child in inline_children:
                child.structure["passage_group_id"] = passage_id
            groups.append(
                PassageGroup(
                    passage_id=passage_id,
                    course_id=draft.subject.course_id,
                    passage_text=passage[:4000],
                    child_record_ids=child_ids,
                    parent_record_id=parent_id,
                    source_reference=f"{records[0].source_file}#row={records[0].source_row}",
                    confidence=0.86 if inline_children else 0.75,
                    evidence={
                        "method": "explicit_inline_children" if inline_children else "shared_explicit_passage_column",
                        "records": len(records),
                        "children": len(child_ids),
                    },
                )
            )
        assigned = {
            record.record_id
            for record in draft.records
            if record.structure.get("passage_group_id")
        }
        for match in draft.passage_matches:
            record_ids = [item for item in match.get("record_ids", []) if item not in assigned]
            if len(record_ids) < 2:
                continue
            passage = " ".join(str(match.get("passage_text") or "").split())
            passage_id = "psg_src_" + hashlib.sha256(
                f"{draft.subject.course_id}\x1f{passage}".encode("utf-8")
            ).hexdigest()[:20]
            for record_id in record_ids:
                assigned.add(record_id)
                record = next(
                    (item for item in draft.records if item.record_id == record_id), None
                )
                if record is not None:
                    record.structure["passage_group_id"] = passage_id
                    record.structure["source_passage_evidence"] = match.get("evidence", {})
            groups.append(
                PassageGroup(
                    passage_id=passage_id,
                    course_id=draft.subject.course_id,
                    passage_text=passage[:4000],
                    child_record_ids=record_ids,
                    parent_record_id=None,
                    source_reference=draft.retrievals[-1].get("url")
                    if draft.retrievals
                    else None,
                    confidence=float(match.get("confidence") or 0.8),
                    evidence={
                        "method": "retrieved_public_paper_passage",
                        **dict(match.get("evidence") or {}),
                        "children": len(record_ids),
                    },
                )
            )
        return groups

    def _read_and_structure(self, subject: ExamSubject, root: Path | str, mode: str) -> _Draft:
        path = subject.csv_path
        headers, raw_rows, sha, decode_warnings = self._read_csv(path)
        profile = detect_schema(headers, raw_rows[:3])
        errors: list[str] = []
        warnings: list[str] = list(decode_warnings)
        if profile.missing_fields:
            errors.append(
                "schema is missing required field(s): " + ", ".join(profile.missing_fields)
            )
        if not profile.standard:
            warnings.append("non-standard CSV schema was detected; canonical mapping was applied")
        source_docs, source_texts = self._source_documents_and_text(subject)
        source_available = any(item.question_fidelity_available for item in source_docs)
        if not source_available:
            warnings.append(
                "no original question-paper/answer-key source is available; "
                "source fidelity will be unverifiable"
            )
        records: list[RecordResult] = []
        for row_index, raw in enumerate(raw_rows, start=2):
            canonical = profile.adapt(raw)
            parsed = parse_row(canonical, index=row_index)
            self._apply_metadata_overrides(parsed, canonical)
            structure = analyse_structure(
                question_text=parsed.question_text,
                options_raw=parsed.options_raw,
                question_type_raw=parsed.question_type_raw,
                answer_type=parsed.answer_type,
                passage_text=canonical.get("passage"),
            )
            normalized = self._normalized(parsed, structure, canonical)
            repaired = dict(normalized)
            repaired["options"] = list(structure.options)
            repaired["question_type"] = structure.question_type
            repaired["question_text"] = structure.question_text
            record_id = make_record_id(
                subject.course_id, path.as_posix(), parsed.source_question_id, row_index
            )
            record = RecordResult(
                record_id=record_id,
                course_id=subject.course_id,
                source_file=path.as_posix(),
                source_row=row_index,
                source_question_id=parsed.source_question_id,
                original=dict(canonical.get("_raw") or raw),
                normalized=normalized,
                repaired=repaired,
                structure=structure.as_dict(),
                answer_status="source_stored_unverified",
            )
            for issue in structure.issues:
                record.issues.append(self._issue_from_dict(issue))
            for repair in structure.repairs:
                record.repairs.append(self._repair_from_dict(repair, mode))
            records.append(record)
        return _Draft(
            subject=subject,
            schema=profile,
            source_sha256=sha,
            rows=len(raw_rows),
            records=records,
            source_documents=source_docs,
            source_texts=source_texts,
            answer_keys=self._answer_keys(source_docs, source_texts),
            errors=errors,
            warnings=warnings,
        )

    def _finalize_draft(
        self,
        draft: _Draft,
        index: LocalAnswerIndex,
        *,
        mode: str,
        use_web: bool,
        use_model: bool,
        web_limit: int,
        model_limit: int,
        reconstruct: bool = True,
    ) -> None:
        source_texts = draft.source_texts
        duplicate_ids = Counter(record.source_question_id for record in draft.records)
        fingerprints = Counter(
            fingerprint_text(str(record.repaired.get("question_text") or ""))
            for record in draft.records
            if str(record.repaired.get("question_text") or "").strip()
        )
        draft.source_matches = match_paper_questions(
            draft.records, draft.paper_questions, threshold=0.94
        )
        draft.passage_matches = match_passage_children(
            draft.paper_passages, draft.records, threshold=0.94
        )
        for record in draft.records:
            parsed_answer = str(record.normalized.get("answer_text") or "").strip() or None
            # Source discovery is grouped per course/paper. A snippet is never
            # promoted to an answer, so per-record web searches are not needed.
            leads: list[AnswerCandidate] = []
            official = self._official_answer(record, draft)
            independent_result = None
            if parsed_answer is not None and official is None:
                try:
                    independent_result = self._deterministic_solver.solve(
                        str(record.repaired.get("question_text") or "")
                    )
                except Exception as exc:  # noqa: BLE001 - a solver failure is not evidence
                    record.warnings.append(
                        f"deterministic verification skipped: {type(exc).__name__}: {exc}"[:300]
                    )
                if independent_result is not None:
                    record.answer_candidates.append(
                        AnswerCandidate(
                            answer_text=independent_result.answer,
                            source_type="deterministic_solver",
                            source_name="local deterministic solver",
                            verification_status="review_required",
                            confidence=independent_result.confidence,
                            method=independent_result.method,
                            source_reference=(
                                f"{record.source_file}#row={record.source_row}"
                            ),
                            evidence={
                                **dict(independent_result.evidence),
                                "solver": "exam_engine.recovery.solver",
                                "solver_version": "deterministic-solver.v1",
                                "computed_at": now_iso(),
                                "independent_of_ai": True,
                                "not_official": True,
                            },
                            official=False,
                        )
                    )
            decision = recover_answer(
                question_text=str(record.repaired.get("question_text") or ""),
                options=self._parsed_options(record),
                answer_text=parsed_answer,
                answer_type=str(record.normalized.get("answer_type") or "unknown"),
                solution_steps=record.normalized.get("solution_steps"),
                course_id=record.course_id,
                source_file=record.source_file,
                source_row=record.source_row,
                source_question_id=record.source_question_id,
                index=index,
                source_leads=leads,
                official_key=official.get("answer") if official else None,
                official_source_name=official.get("source_name") if official else None,
                official_source_reference=official.get("reference") if official else None,
                official_source_url=official.get("url") if official else None,
                official_source_evidence=official.get("evidence") if official else None,
                model_solver=self._model_solver if use_model else None,
                deterministic_solver=(
                    self._deterministic_solver if not parsed_answer and official is None else None
                ),
                model_used=self._model_used,
                model_limit=model_limit,
            )
            if decision.answer_status == "ai_generated_unverified":
                self._model_used += 1
            equivalent_official = bool(
                official
                and decision.answer_text
                and answers_equivalent(
                    decision.answer_text,
                    official.get("answer"),
                    options=self._parsed_options(record),
                )
            )
            if equivalent_official:
                # The stored answer and the official key agree after safe
                # normalization (for example ``A;B;C`` vs ``A,B,C`` or an
                # equal-endpoint NAT range vs a single value). Keep the local
                # text, but attach the official candidate as the provenance
                # that raises the status.
                decision.answer_status = "official_verified"
                decision.candidates.insert(
                    0,
                    AnswerCandidate(
                        answer_text=official.get("answer"),
                        source_type="official_answer_key",
                        source_name=official.get("source_name") or "official answer key",
                        verification_status="official_verified",
                        confidence=0.98,
                        method="official_answer_key_lookup",
                        source_reference=official.get("reference"),
                        source_url=official.get("url"),
                        evidence={
                            "official_key": True,
                            "equivalent_stored_answer": decision.answer_text,
                            **(official.get("evidence") or {}),
                        },
                        official=True,
                    ),
                )
            elif (
                official
                and decision.answer_text
                and not answers_equivalent(
                    decision.answer_text,
                    official.get("answer"),
                    options=self._parsed_options(record),
                )
            ):
                decision.answer_status = "conflicting"
                decision.issues.append(
                    Issue(
                        code="answer.local_and_official_conflict",
                        category="answer_integrity",
                        severity="error",
                        message="A local candidate and an official answer key disagree.",
                        evidence={
                            "local_answer": decision.answer_text,
                            "official_answer": official.get("answer"),
                            "official_reference": official.get("reference"),
                        },
                        confidence=0.98,
                        remediation="Do not choose automatically; inspect the source/version.",
                        blocking=True,
                        field="answer_text",
                    )
                )
                decision.answer_text = None
            record.answer_status = decision.answer_status
            record.answer_candidates.extend(decision.candidates)
            record.issues.extend(decision.issues)
            for repair in decision.repairs:
                if mode == "auto" and repair.risk == "low":
                    repair.status = "applied"
                record.repairs.append(repair)
            if decision.answer_text is not None:
                record.repaired["answer_text"] = decision.answer_text
            if decision.correct_option_key is not None:
                record.repaired["correct_option_key"] = decision.correct_option_key

            candidate = next(
                (item for item in decision.candidates if item.answer_text is not None), None
            )
            answer_type_for_verification = str(
                record.normalized.get("answer_type") or "unknown"
            )
            if decision.correct_option_key:
                answer_type_for_verification = (
                    "multiple_options"
                    if "," in str(decision.correct_option_key)
                    else "mcq_option"
                )
            outcome = verify_answer(
                question_text=str(record.repaired.get("question_text") or ""),
                options=self._parsed_options(record),
                answer_text=(
                    str(record.repaired.get("answer_text") or "").strip() or None
                ),
                answer_type=answer_type_for_verification,
                candidate=candidate,
                conflicting_evidence=decision.answer_status in CONFLICT_STATUSES,
                independent_answer=(
                    independent_result.answer if independent_result is not None else None
                ),
            )
            if independent_result is not None:
                outcome.result.evidence["independent_verification"] = {
                    "method": independent_result.method,
                    "answer": independent_result.answer,
                    "confidence": independent_result.confidence,
                    "solver_version": "deterministic-solver.v1",
                    "computed_at": now_iso(),
                    "inputs": dict(independent_result.evidence),
                }
            if (
                outcome.result.status == "independently_solved_verified"
                and official is None
            ):
                record.answer_status = "independently_solved_verified"
            record.verification = {
                "status": outcome.result.status,
                "method": outcome.result.method,
                "confidence": outcome.result.confidence,
                "answer_text": outcome.result.answer_text,
                "evidence": outcome.result.evidence,
                "issues": outcome.result.issues,
                "independent": outcome.result.independent,
            }
            if outcome.result.status in {"conflicting_evidence"}:
                record.issues.append(
                    Issue(
                        code="answer.verification_conflict",
                        category="answer_integrity",
                        severity="error",
                        message="Independent answer verification paths disagree.",
                        evidence=outcome.result.evidence,
                        confidence=outcome.result.confidence,
                        remediation="Keep the record quarantined until a human resolves the conflict.",
                        blocking=True,
                        field="answer_text",
                    )
                )

            self._record_rules(record, draft, duplicate_ids, fingerprints)
            source_status, source_detail = source_fidelity(
                str(record.repaired.get("question_text") or ""),
                source_texts=source_texts,
            )
            source_match = draft.source_matches.get(record.record_id)
            if source_match is not None:
                similarity = float(source_match.get("similarity") or 0.0)
                source_status = "exact" if similarity >= 0.99 else "formatting_normalized"
                source_detail = {
                    "reason": "retrieved_source_question_match",
                    "similarity": similarity,
                    "source": {
                        "path": source_match.get("source_url")
                        or (
                            draft.retrievals[-1].get("url")
                            if draft.retrievals
                            else None
                        ),
                        "kind": source_match.get("source_kind") or "question_paper",
                        "question_number": source_match.get("question_number"),
                        "sha256": source_match.get("source_sha256"),
                        "identity": source_match.get("source_identity"),
                        "tier": source_match.get("source_tier"),
                        "provider": source_match.get("source_provider"),
                    },
                    "limitations": "Question text matched a retrieved public paper; official authority is claimed only when the provider is a configured official archive.",
                }
                record.structure["source_match"] = source_match
                record.issues.append(
                    Issue(
                        code="source.retrieved_question_match",
                        category="source_fidelity",
                        severity="info",
                        message="The question text was matched to a retrieved public question paper.",
                        evidence=source_detail,
                        confidence=round(similarity, 3),
                        remediation="Check the source identity, year, shift and set before release.",
                        blocking=False,
                        field="source_fidelity",
                    )
                )
            record.source_fidelity = source_status
            record.source_fidelity_detail = source_detail
            if source_match is not None and (
                source_match.get("source_provider") != "official_archive"
                or source_match.get("source_tier") != 1
            ):
                record.issues.append(
                    Issue(
                        code="source.retrieved_not_identity_certified",
                        category="source_fidelity",
                        severity="warning",
                        message="Retrieved paper matching does not by itself certify official provenance.",
                        evidence={
                            "similarity": source_match.get("similarity"),
                            "question_number": source_match.get("question_number"),
                        },
                        confidence=0.9,
                        remediation="Verify examination identity and publication authority before an official claim.",
                        blocking=False,
                        field="source_fidelity",
                    )
                )
            record.issues.extend(
                authenticity_issues(
                    question_text=str(record.repaired.get("question_text") or ""),
                    answer_text=record.repaired.get("answer_text"),
                    solution_steps=record.normalized.get("solution_steps"),
                    options=record.repaired.get("options", []),
                    source_status=source_status,
                    source_detail=source_detail,
                )
            )
            if not record.repaired.get("answer_text"):
                record.issues.append(
                    Issue(
                        code="answer.not_recoverable",
                        category="answer_integrity",
                        severity="error",
                        message="No reliable answer could be recovered from local or configured sources.",
                        evidence={"answer_status": record.answer_status},
                        confidence=0.95,
                        remediation="Supply an official key, a verified source, or review manually; do not invent an answer.",
                        blocking=True,
                        field="answer_text",
                    )
                )
            assessment = release_assessment(record)
            record.release_status = assessment["status"]
            record.quality = score_record(record)
            record.quality["release"] = assessment
            record.analysis_hash = self._analysis_hash(record)

        if any(str(record.repaired.get("question_text") or "").strip() for record in draft.records):
            for record in list(draft.records):
                children = record.structure.get("child_questions") or []
                if len(children) >= 2:
                    record.issues.append(
                        Issue(
                            code="structure.shared_passage_has_child_questions",
                            category="passage_subquestion_integrity",
                            severity="warning",
                            message="A shared passage contains multiple child questions; child proposals were created.",
                            evidence={"children": len(children), "passage_preview": str(record.structure.get("passage") or "")[:300]},
                            confidence=0.86,
                            remediation="Review the child question proposals and their individual options/answers.",
                            blocking=False,
                            field="structure",
                        )
                    )
                    for child_index, child_text in enumerate(children, start=1):
                        child_id = make_record_id(
                            record.course_id,
                            record.source_file,
                            f"{record.source_question_id}#child-{child_index}",
                            record.source_row,
                        )
                        child = RecordResult(
                            record_id=child_id,
                            course_id=record.course_id,
                            source_file=record.source_file,
                            source_row=record.source_row,
                            source_question_id=f"{record.source_question_id}#child-{child_index}",
                            original=record.original,
                            normalized={
                                **record.normalized,
                                "question_text": child_text,
                                "question_type": "passage_based",
                                "answer_text": None,
                            },
                            repaired={
                                **record.repaired,
                                "question_text": child_text,
                                "question_type": "passage_based",
                                "options": [],
                                "answer_text": None,
                                "correct_option_key": None,
                            },
                            structure={
                                "child_question": True,
                                "parent_record_id": record.record_id,
                                "passage": record.structure.get("passage"),
                                "child_index": child_index,
                            },
                            answer_status="not_recoverable",
                            source_fidelity="unverifiable",
                            release_status="quarantined",
                        )
                        child.issues.append(
                            Issue(
                                code="structure.child_question_proposal",
                                category="passage_subquestion_integrity",
                                severity="warning",
                                message="Child question proposed from a multi-question passage; options/answer require review.",
                                evidence={"parent_record_id": record.record_id},
                                confidence=0.86,
                                remediation="Review and attach the child's options, answer and marks from the source.",
                                blocking=True,
                                field="question_text",
                            )
                        )
                        child.quality = score_record(child)
                        draft.records.append(child)
            if reconstruct:
                draft.passages = self._build_passages(draft)

    def _record_rules(
        self,
        record: RecordResult,
        draft: _Draft,
        duplicate_ids: Counter[str],
        fingerprints: Counter[str],
    ) -> None:
        repaired = record.repaired
        text = str(repaired.get("question_text") or "").strip()
        if not text:
            record.issues.append(
                Issue(
                    code="question.empty_text",
                    category="structural_integrity",
                    severity="error",
                    message="The record has no question text.",
                    evidence={"source_row": record.source_row},
                    confidence=1.0,
                    remediation="Recover the question from the source; never invent it.",
                    blocking=True,
                    field="question_text",
                )
            )
        elif len(text.split()) < 4:
            record.issues.append(
                Issue(
                    code="question.too_short",
                    category="structural_integrity",
                    severity="warning",
                    message="The question text is unusually short.",
                    evidence={"text": text},
                    confidence=0.7,
                    remediation="Check whether extraction truncated the question.",
                    blocking=False,
                    field="question_text",
                )
            )
        if not str(record.original.get("question_id") or "").strip():
            record.issues.append(
                Issue(
                    code="referential.blank_source_question_id",
                    category="referential_integrity",
                    severity="warning",
                    message="The source row has no question id; a row-based id was used.",
                    evidence={"source_question_id": record.source_question_id},
                    confidence=0.9,
                    remediation="Preserve the source id when the file provides one.",
                    blocking=False,
                    field="question_id",
                )
            )
        if duplicate_ids[record.source_question_id] > 1:
            record.issues.append(
                Issue(
                    code="referential.duplicate_question_id",
                    category="referential_integrity",
                    severity="error",
                    message="The same source question id occurs more than once in this file.",
                    evidence={"count": duplicate_ids[record.source_question_id]},
                    confidence=0.99,
                    remediation="Check whether this is a duplicated extraction or an intentional repeated occurrence.",
                    blocking=True,
                    field="question_id",
                )
            )
        fp = fingerprint_text(text) if text else ""
        if fp and fingerprints[fp] > 1:
            record.issues.append(
                Issue(
                    code="referential.duplicate_question",
                    category="referential_integrity",
                    severity="warning",
                    message="An exact duplicate question text occurs more than once in this course.",
                    evidence={"count": fingerprints[fp]},
                    confidence=0.98,
                    remediation="Do not delete valid repeated questions automatically; review the duplicate group.",
                    blocking=False,
                    field="question_text",
                )
            )
        if repaired.get("marks") is None:
            record.issues.append(
                Issue(
                    code="metadata.missing_marks",
                    category="metadata_consistency",
                    severity="info",
                    message="Marks are missing from the record.",
                    evidence={},
                    confidence=0.9,
                    remediation="Recover marks from the paper or syllabus metadata.",
                    blocking=False,
                    field="marks",
                )
            )
        if repaired.get("exam_year") is None:
            record.issues.append(
                Issue(
                    code="metadata.missing_exam_year",
                    category="metadata_consistency",
                    severity="info",
                    message="The examination year is not stated or derivable.",
                    evidence={},
                    confidence=0.85,
                    remediation="Add year metadata when the source paper establishes it.",
                    blocking=False,
                    field="exam_year",
                )
            )
        if not str(repaired.get("question_type") or "").strip() or repaired.get(
            "question_type"
        ) == "unknown":
            record.issues.append(
                Issue(
                    code="classification.unknown_question_type",
                    category="classification_accuracy",
                    severity="info",
                    message="Question type remains unknown after structural analysis.",
                    evidence={},
                    confidence=0.8,
                    remediation="Review the question format; do not force it into an unrelated type.",
                    blocking=False,
                    field="question_type",
                )
            )
        if record.structure.get("passage") and record.structure.get("child_questions"):
            record.issues.append(
                Issue(
                    code="passage.shared_passage_detected",
                    category="passage_subquestion_integrity",
                    severity="info",
                    message="A shared passage and dependent questions were detected.",
                    evidence={"children": len(record.structure.get("child_questions") or [])},
                    confidence=0.86,
                    remediation="Use the child proposals for downstream consumption.",
                    blocking=False,
                    field="structure",
                )
            )
        options = repaired.get("options") or []
        qtype = str(repaired.get("question_type") or "")
        if options and str(repaired.get("answer_text") or "").strip():
            key = repaired.get("correct_option_key")
            keys = {str(item.get("key")) for item in options}
            if key and not set(str(key).split(",")) <= keys:
                record.issues.append(
                    Issue(
                        code="answer.key_not_in_options",
                        category="option_integrity",
                        severity="error",
                        message="The recovered correct option key is not present in the options.",
                        evidence={"key": key, "keys": sorted(keys)},
                        confidence=0.98,
                        remediation="Review the option list and answer association.",
                        blocking=True,
                        field="answer_text",
                    )
                )
        if qtype in {"single_correct_mcq", "multiple_correct_mcq"} and len(options) < 2:
            # Already reported by the structure analyser; retain a blocking gate
            # because a choice question without choices is not releasable.
            record.issues.append(
                Issue(
                    code="options.unusable_choice_question",
                    category="option_integrity",
                    severity="error",
                    message="A choice question has no usable option list.",
                    evidence={"question_type": qtype, "options": options},
                    confidence=0.95,
                    remediation="Recover the options from the source; do not fabricate them.",
                    blocking=True,
                    field="options",
                )
            )
        if not draft.schema.standard:
            record.issues.append(
                Issue(
                    code="schema.non_standard_mapping",
                    category="structural_integrity",
                    severity="info",
                    message="This record came from a non-standard schema and was alias-mapped.",
                    evidence={"mapping": draft.schema.mapping},
                    confidence=0.7,
                    remediation="Verify that the alias mapping preserved every meaningful field.",
                    blocking=False,
                    field="schema",
                )
            )

    def _make_course_result(self, draft: _Draft, duration_ms: int) -> CourseResult:
        quality = course_quality(draft.records, draft.warnings)
        counts = dict(quality.get("counts") or {})
        counts["source_documents"] = len(draft.source_documents)
        counts["question_fidelity_sources"] = sum(
            1 for item in draft.source_documents if item.question_fidelity_available
        )
        counts["reconstructed_children"] = sum(
            1 for item in draft.records if item.structure.get("child_question")
        )
        source_types = source_counts(draft.source_candidates)
        counts["source_candidates"] = len(draft.source_candidates)
        counts["papers_discovered"] = source_types.get("question_paper", 0) + source_types.get(
            "solved_paper", 0
        )
        counts["answer_keys_discovered"] = source_types.get("answer_key", 0)
        counts["papers_found"] = sum(
            1 for item in draft.retrievals if item.get("kind") in {"question_paper", "solved_paper"}
        )
        counts["answer_keys_found"] = sum(
            1 for item in draft.retrievals if item.get("kind") == "answer_key"
        )
        counts["documents_retrieved"] = len(draft.retrievals)
        counts["retrieved_source_chars"] = sum(
            len(str(item.get("text") or "")) for item in draft.retrievals
        )
        counts["questions_matched_to_sources"] = len(draft.source_matches)
        counts["source_passage_groups"] = len(draft.passage_matches)
        counts["cache_hits"] = draft.cache_hits
        counts["retrieval_failures"] = draft.retrieval_failures
        counts["passage_groups"] = len(draft.passages)
        counts["answer_key_entries"] = sum(
            int(source.get("entries", 0))
            for source in [*draft.answer_keys, *draft.web_answer_keys]
        )
        counts["source_coverage"] = self._coverage(draft)
        return CourseResult(
            course_id=draft.subject.course_id,
            course_code=draft.subject.course_code,
            course_name=draft.subject.subject_name,
            category_id=draft.subject.category_id,
            source_file=draft.subject.csv_path.as_posix(),
            source_sha256=draft.source_sha256,
            schema=draft.schema.as_dict(),
            row_count=draft.rows,
            analyzed_count=len(draft.records),
            status=quality.get("status", "review_required"),
            records=draft.records,
            source_candidates=list(draft.source_candidates),
            passages=list(draft.passages),
            quality=quality,
            counts=counts,
            warnings=draft.warnings,
            errors=draft.errors,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _coverage(draft: _Draft) -> dict[str, Any]:
        records = draft.records
        official = sum(
            1
            for record in records
            if record.answer_status
            in {"official_verified", "official_verified_with_independent_check"}
        )
        independent = sum(
            1
            for record in records
            if (record.verification or {}).get("status")
            in {"independently_solved_verified", "multiple_source_corroborated"}
        )
        official_with_independent = sum(
            1
            for record in records
            if (record.verification or {}).get("status")
            == "official_verified_with_independent_check"
        )
        released = sum(1 for record in records if record.release_status == "released")
        review = sum(
            1 for record in records if record.release_status == "review_required"
        )
        quarantined = sum(
            1 for record in records if record.release_status == "quarantined"
        )
        matched = len(draft.source_matches)
        papers = sum(
            1 for item in draft.retrievals if item.get("kind") == "question_paper"
        )
        keys = sum(
            1
            for item in draft.retrievals
            if item.get("kind") in {"answer_key", "marking_scheme"}
        )
        key_entries = sum(
            int(source.get("entries", 0))
            for source in [*draft.answer_keys, *draft.web_answer_keys]
        )
        ambiguous = sum(
            1
            for candidate in draft.source_candidates
            if candidate.evidence.get("session_ambiguous")
            or candidate.evidence.get("set_ambiguous")
        )
        archive_state = draft.archive_state
        if archive_state in {"not_configured", "unavailable"}:
            status = archive_state
            blocker = (
                "no official archive configured for this examination/year"
                if archive_state == "not_configured"
                else "configured official archive is unavailable"
            )
        elif archive_state == "no_document":
            status = "no_document"
            blocker = "official archive exposes no document for this year/subject"
        elif archive_state == "extraction_failed":
            status = "extraction_failed"
            blocker = "official document retrieval or extraction failed"
        elif official:
            status = "official_verified"
            blocker = ""
        elif matched and keys:
            status = "matched_unverified"
            blocker = "paper text matched but no usable official key entry was verified"
        elif matched:
            status = "matched_no_key"
            blocker = "paper matched but no question-level official key was retrieved"
        else:
            status = "document_retrieved_no_match"
            blocker = "retrieved document text did not match local questions"
        return {
            "archive_state": archive_state,
            "archive_ids": list(draft.archive_ids),
            "search_state": draft.search_state,
            "papers_retrieved": papers,
            "answer_documents_retrieved": keys,
            "answer_key_entries": key_entries,
            "questions_matched": matched,
            "official_verified": official,
            "independently_verified": independent,
            "official_with_independent_check": official_with_independent,
            "identity_ambiguous_candidates": ambiguous,
            "released_records": released,
            "review_required_records": review,
            "quarantined_records": quarantined,
            "release_readiness": (
                "ready" if records and released == len(records) else "not_ready"
            ),
            "unresolved_records": sum(
                1 for record in records if not record.repaired.get("answer_text")
            ),
            "status": status,
            "blocker": blocker,
        }

    def _web_leads(self, record: RecordResult, subject: ExamSubject) -> list[AnswerCandidate]:
        if self._web_search is None:
            return []
        query = " ".join(
            part
            for part in (
                subject.category.name,
                subject.subject_name,
                str(record.repaired.get("exam_year") or ""),
                str(record.original.get("original_question_number") or ""),
                str(record.repaired.get("question_text") or "")[:240],
            )
            if part
        )
        try:
            sources = self._web_search.search(query, limit=2)
        except Exception:  # noqa: BLE001 - optional external retrieval
            logger.warning("recovery web search failed", exc_info=True)
            return []
        leads: list[AnswerCandidate] = []
        for source in sources:
            official = any(
                suffix in source.url
                for suffix in (".gov.in", ".nic.in", ".ac.in", "nta.ac.in", "gate.iit")
            )
            leads.append(
                AnswerCandidate(
                    answer_text=None,
                    source_type="web_search",
                    source_name=source.title,
                    source_url=source.url,
                    verification_status="source_lead_unverified",
                    confidence=0.15,
                    method=f"{source.provider}_search_snippet",
                    source_reference=query,
                    evidence={
                        "snippet": source.snippet[:1000],
                        "official_domain_heuristic": official,
                        "note": "A search snippet is a lead, not an exact-question answer.",
                    },
                    official=False,
                )
            )
        if leads:
            record.issues.append(
                Issue(
                    code="source.web_leads_found",
                    category="source_fidelity",
                    severity="info",
                    message="Web search found source leads; none were accepted as a verified answer.",
                    evidence={"leads": len(leads)},
                    confidence=0.6,
                    remediation="Open the exact source and verify the question before using an answer.",
                    blocking=False,
                    field="answer_text",
                )
            )
        return leads

    # -- source documents --------------------------------------------------
    def _source_documents(self, subject: ExamSubject) -> list[SourceDocument]:
        docs, _ = self._source_documents_and_text(subject, extract=False)
        return docs

    def _source_documents_and_text(
        self, subject: ExamSubject, *, extract: bool = True
    ) -> tuple[list[SourceDocument], list[dict[str, Any]]]:
        paths: set[Path] = set(subject.syllabus_paths)
        for folder in (subject.csv_path.parent, subject.csv_path.parent / "Syllabus"):
            if folder.is_dir():
                paths.update(folder.glob("*.pdf"))
        documents: list[SourceDocument] = []
        texts: list[dict[str, Any]] = []
        for path in sorted(paths):
            if not path.is_file():
                continue
            kind = self._source_kind(path)
            sha = self._file_sha(path)
            page_count, text = (None, "")
            if extract and kind in {"question_paper", "answer_key"}:
                page_count, text = self._pdf_text(path)
            elif path.suffix.casefold() == ".pdf":
                page_count = self._pdf_pages(path)
            documents.append(
                SourceDocument(
                    source_id="src_" + sha[:20],
                    course_id=subject.course_id,
                    path=path.as_posix(),
                    kind=kind,
                    sha256=sha,
                    page_count=page_count,
                    text_chars=len(text) if text else None,
                    question_fidelity_available=bool(
                        kind in {"question_paper", "answer_key"} and text
                    ),
                    metadata={"filename": path.name},
                )
            )
            documents[-1].metadata["source_tier"] = classify_source(
                filename=path.name
            )[1]
            if text:
                texts.append(
                    {"path": path.as_posix(), "kind": kind, "text": text[:2_000_000]}
                )
        return documents, texts

    @staticmethod
    def _source_kind(path: Path) -> str:
        kind, _tier, _reliability, _reason = classify_source(filename=path.name)
        return kind

    @staticmethod
    def _pdf_pages(path: Path) -> int | None:
        try:
            import fitz  # type: ignore

            document = fitz.open(str(path))
            try:
                return int(document.page_count)
            finally:
                document.close()
        except Exception:  # noqa: BLE001 - page count is optional evidence
            return None

    @staticmethod
    def _pdf_text(path: Path) -> tuple[int | None, str]:
        try:
            import fitz  # type: ignore

            document = fitz.open(str(path))
            try:
                parts = [
                    document.load_page(index).get_text("text")
                    for index in range(min(document.page_count, _MAX_PDF_TEXT_PAGES))
                ]
                return int(document.page_count), "\n".join(parts)
            finally:
                document.close()
        except Exception:  # noqa: BLE001 - source extraction is optional evidence
            return None, ""

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _new_run_id() -> str:
        return "recovery-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def _file_sha(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _read_headers(self, path: Path) -> tuple[list[str], int, str, list[str]]:
        headers, rows, sha, warnings = self._read_csv(path)
        return headers, len(rows), sha, warnings

    @staticmethod
    def _read_csv(path: Path) -> tuple[list[str], list[dict[str, Any]], str, list[str]]:
        data = path.read_bytes()
        text, encoding, had_errors = decode_text(data)
        warnings = [] if not had_errors else [f"decoded with replacement using {encoding}"]
        reader = csv.DictReader(io.StringIO(text))
        headers = [str(item) for item in (reader.fieldnames or []) if item is not None]
        rows: list[dict[str, Any]] = []
        for row in reader:
            rows.append({key: value for key, value in row.items() if key is not None and value not in (None, "")})
        return headers, rows, hashlib.sha256(data).hexdigest(), warnings

    @staticmethod
    def _apply_metadata_overrides(parsed: ParsedRow, canonical: dict[str, Any]) -> None:
        year = canonical.get("exam_year")
        if year not in (None, ""):
            try:
                parsed.exam_year = int(float(str(year)))
            except (TypeError, ValueError):
                pass
        paper = str(canonical.get("paper_label") or "").strip()
        if paper:
            parsed.paper_label = paper

    @staticmethod
    def _normalized(
        parsed: ParsedRow, structure: Any, canonical: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "question_id": canonical.get("question_id") or parsed.source_question_id,
            "source_question_id": parsed.source_question_id,
            "original_question_number": parsed.original_question_number,
            "question_text": structure.question_text,
            "question_type_raw": parsed.question_type_raw,
            "question_type": structure.question_type,
            "options_raw": parsed.options_raw,
            "options": list(structure.options),
            "marks": parsed.marks,
            "answer_id": parsed.answer_id,
            "answer_text": parsed.answer_text,
            "answer_type_raw": parsed.answer_type_raw,
            "answer_type": parsed.answer_type,
            "correct_option_key": parsed.correct_option_key,
            "solution_steps": parsed.solution_steps,
            "passage": canonical.get("passage"),
            "exam_year": parsed.exam_year,
            "paper_label": parsed.paper_label,
            "section": canonical.get("section"),
            "unit": canonical.get("unit"),
            "subject": canonical.get("subject"),
            "category": canonical.get("category"),
            "source_url": canonical.get("source_url"),
            "source_row": parsed.source_row,
        }

    @staticmethod
    def _parsed_options(record: RecordResult) -> list[Any]:
        from exam_engine.datasets.parsing import ParsedOption

        return [
            ParsedOption(key=str(item.get("key", "")), text=str(item.get("text", "")))
            for item in record.repaired.get("options", [])
            if item.get("key") and item.get("text")
        ]

    @staticmethod
    def _issue_from_dict(item: dict[str, Any]) -> Issue:
        return Issue(
            code=str(item.get("code") or "unknown"),
            category=str(item.get("category") or "structural_integrity"),
            severity=str(item.get("severity") or "info"),
            message=str(item.get("message") or ""),
            evidence=item.get("evidence") or {},
            confidence=float(item.get("confidence") or 0.5),
            remediation=str(item.get("remediation") or ""),
            blocking=bool(item.get("blocking")),
            field=item.get("field"),
        )

    @staticmethod
    def _repair_from_dict(item: dict[str, Any], mode: str) -> Repair:
        repair = Repair(
            repair_type=str(item.get("repair_type") or "unknown"),
            field=str(item.get("field") or "structure"),
            before=item.get("before"),
            after=item.get("after"),
            method=str(item.get("method") or "deterministic_rule"),
            confidence=float(item.get("confidence") or 0.5),
            risk=str(item.get("risk") or "low"),
            reason=str(item.get("reason") or ""),
        )
        if mode == "auto" and repair.risk == "low" and repair.confidence >= 0.9:
            repair.status = "applied"
        return repair

    @staticmethod
    def _analysis_hash(record: RecordResult) -> str:
        payload = json.dumps(
            {
                "original": record.original,
                "repaired": record.repaired,
                "issues": [
                    {"code": issue.code, "severity": issue.severity} for issue in record.issues
                ],
                "version": RECOVERY_VERSION,
                "rules": RULE_REGISTRY_VERSION,
            },
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _totals(self, courses: list[CourseResult]) -> dict[str, Any]:
        records = [record for course in courses for record in course.records]
        severity = Counter(issue.severity for record in records for issue in record.issues)
        codes = Counter(issue.code for record in records for issue in record.issues)
        answer_status = Counter(record.answer_status for record in records)
        source_fidelity_counts = Counter(record.source_fidelity for record in records)
        release = Counter(record.release_status for record in records)
        recovered_records = [
            record
            for record in records
            if not str(record.normalized.get("answer_text") or "").strip()
            and str(record.repaired.get("answer_text") or "").strip()
        ]
        recovered = len(recovered_records)
        verified_recoveries = sum(
            1
            for record in recovered_records
            if record.answer_status in VERIFIED_STATUSES
            or (record.verification or {}).get("status") in VERIFIED_STATUSES
        )
        officially_verified_recoveries = sum(
            1
            for record in recovered_records
            if record.answer_status
            in {"official_verified", "official_verified_with_independent_check"}
            or (record.verification or {}).get("status")
            in {"official_verified", "official_verified_with_independent_check"}
        )
        independently_verified_recoveries = sum(
            1
            for record in recovered_records
            if (record.verification or {}).get("status")
            in {"official_verified_with_independent_check", "independently_solved_verified"}
        )
        verified = sum(
            1
            for record in records
            if record.answer_status in VERIFIED_STATUSES
            or (record.verification or {}).get("status") in VERIFIED_STATUSES
        )
        question_types = Counter(
            str(record.repaired.get("question_type") or "unknown") for record in records
        )
        return {
            "courses": len(courses),
            "courses_requiring_repair": sum(
                1
                for course in courses
                if course.status in {"blocked", "review_required", "insufficient_evidence"}
            ),
            "courses_ready": sum(1 for course in courses if course.status == "ready"),
            "courses_unverifiable_source": sum(
                1 for course in courses if course.status == "unverifiable_source"
            ),
            "courses_insufficient_evidence": sum(
                1 for course in courses if course.status == "insufficient_evidence"
            ),
            "source_rows": sum(course.row_count for course in courses),
            "records": len(records),
            "with_answer": sum(1 for record in records if record.repaired.get("answer_text")),
            "missing_answers": sum(
                1 for record in records if not record.repaired.get("answer_text")
            ),
            "recovered_answers": recovered,
            "verified_recoveries": verified_recoveries,
            "unverified_recoveries": recovered - verified_recoveries,
            "officially_verified_recoveries": officially_verified_recoveries,
            "independently_verified_recoveries": independently_verified_recoveries,
            "verified_answers": verified,
            "officially_verified_answers": sum(
                1
                for record in records
                if record.answer_status in {"official_verified", "official_verified_with_independent_check"}
                or (record.verification or {}).get("status")
                in {"official_verified", "official_verified_with_independent_check"}
            ),
            "independently_verified_answers": sum(
                1
                for record in records
                if (record.verification or {}).get("status")
                in {"official_verified_with_independent_check", "independently_solved_verified"}
            ),
            "multiple_source_corroborated_answers": sum(
                1
                for record in records
                if (record.verification or {}).get("status")
                == "multiple_source_corroborated"
            ),
            "unresolved_answers": sum(
                1 for record in records if not record.repaired.get("answer_text")
            ),
            "unverified_answers": sum(
                1
                for record in records
                if record.repaired.get("answer_text")
                and record.answer_status
                in {
                    "source_stored_unverified",
                    "local_duplicate",
                    "structured_extraction",
                    "ai_generated_unverified",
                }
            ),
            "ai_generated_answers": answer_status.get("ai_generated_unverified", 0),
            "conflicting_answers": answer_status.get("conflicting", 0),
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
                    issue.code
                    in {"referential.duplicate_question", "referential.duplicate_question_id"}
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
            "passage_parents": sum(
                1
                for record in records
                if record.structure.get("passage") and record.structure.get("child_questions")
            ),
            "reconstructed_children": sum(
                1 for record in records if record.structure.get("child_question")
            ),
            "passage_groups": sum(len(course.passages) for course in courses),
            "passage_based_records": question_types.get("passage_based", 0),
            "source_candidates": sum(len(course.source_candidates) for course in courses),
            "question_papers_discovered": sum(
                1
                for course in courses
                for candidate in course.source_candidates
                if candidate.document_type in {"question_paper", "solved_paper"}
            ),
            "answer_keys_discovered": sum(
                1
                for course in courses
                for candidate in course.source_candidates
                if candidate.document_type == "answer_key"
            ),
            "question_papers_found": sum(
                int(course.counts.get("papers_found", 0) or 0) for course in courses
            ),
            "answer_keys_found": sum(
                int(course.counts.get("answer_keys_found", 0) or 0) for course in courses
            ),
            "documents_retrieved": sum(
                int(course.counts.get("documents_retrieved", 0) or 0) for course in courses
            ),
            "questions_matched_to_sources": sum(
                int(course.counts.get("questions_matched_to_sources", 0) or 0)
                for course in courses
            ),
            "cache_hits": sum(
                int(course.counts.get("cache_hits", 0) or 0) for course in courses
            ),
            "retrieval_failures": sum(
                int(course.counts.get("retrieval_failures", 0) or 0) for course in courses
            ),
            "retrieved_source_chars": sum(
                int(course.counts.get("retrieved_source_chars", 0) or 0) for course in courses
            ),
            "source_documents": sum(
                int(course.counts.get("source_documents", 0) or 0) for course in courses
            ),
            "archive_configured_courses": sum(
                1
                for course in courses
                if (course.counts.get("source_coverage") or {}).get("archive_state")
                not in {"not_configured", "unavailable", None}
            ),
            "archive_retrieved_courses": sum(
                1
                for course in courses
                if (course.counts.get("source_coverage") or {}).get("archive_state")
                == "retrieved"
            ),
            "courses_with_official_answers": sum(
                1
                for course in courses
                if int((course.counts.get("source_coverage") or {}).get("official_verified", 0))
                > 0
            ),
            "source_coverage_by_status": dict(
                Counter(
                    (course.counts.get("source_coverage") or {}).get("status")
                    or "unknown"
                    for course in courses
                )
            ),
            "source_coverage_by_archive_state": dict(
                Counter(
                    (course.counts.get("source_coverage") or {}).get("archive_state")
                    or "unknown"
                    for course in courses
                )
            ),
            "source_queries": self._source_queries,
            "search_provider": self._search_provider_status,
            "issues": sum(len(record.issues) for record in records),
            "by_severity": dict(severity),
            "by_code": dict(sorted(codes.items())),
            "by_answer_status": dict(answer_status),
            "by_source_fidelity": dict(source_fidelity_counts),
            "by_release_status": dict(release),
            "by_question_type": dict(question_types),
            "by_verification_status": dict(
                Counter(
                    str((record.verification or {}).get("status") or "not_checked")
                    for record in records
                )
            ),
        }

    @staticmethod
    def _totals_from_reports(courses: list[dict[str, Any]]) -> dict[str, Any]:
        counts = [row.get("counts") or {} for row in courses]
        return {
            "courses": len(courses),
            "records": sum(int(item.get("records", 0) or 0) for item in counts),
            "with_answer": sum(int(item.get("with_answer", 0) or 0) for item in counts),
            "missing_answers": sum(int(item.get("missing_answer", 0) or 0) for item in counts),
            "recovered_answers": sum(
                int(item.get("recovered_answers", 0) or 0) for item in counts
            ),
            "verified_recoveries": sum(
                int(item.get("verified_recoveries", 0) or 0) for item in counts
            ),
            "unverified_recoveries": sum(
                max(
                    0,
                    int(item.get("recovered_answers", 0) or 0)
                    - int(item.get("verified_recoveries", 0) or 0),
                )
                for item in counts
            ),
            "officially_verified_recoveries": sum(
                int(item.get("officially_verified_recoveries", 0) or 0)
                for item in counts
            ),
            "independently_verified_recoveries": sum(
                int(item.get("independently_verified_recoveries", 0) or 0)
                for item in counts
            ),
            "verified_answers": sum(int(item.get("verified", 0) or 0) for item in counts),
            "unverified_answers": sum(
                int(item.get("unverified_answer", 0) or 0) for item in counts
            ),
            "conflicting_answers": sum(int(item.get("conflicting", 0) or 0) for item in counts),
            "duplicate_records": sum(
                int(item.get("duplicate_records", 0) or 0) for item in counts
            ),
            "structure_errors": sum(
                int(item.get("structure_errors", 0) or 0) for item in counts
            ),
            "reconstructed_children": sum(
                int(item.get("reconstructed_children", 0) or 0) for item in counts
            ),
            "passage_groups": sum(int(item.get("passage_groups", 0) or 0) for item in counts),
            "courses_insufficient_evidence": sum(
                1 for row in courses if row.get("status") == "insufficient_evidence"
            ),
            "source_candidates": sum(
                int(item.get("source_candidates", 0) or 0) for item in counts
            ),
            "question_papers_found": sum(
                int(item.get("papers_found", 0) or 0) for item in counts
            ),
            "answer_keys_found": sum(
                int(item.get("answer_keys_found", 0) or 0) for item in counts
            ),
            "question_papers_discovered": sum(
                int(item.get("papers_discovered", 0) or 0) for item in counts
            ),
            "answer_keys_discovered": sum(
                int(item.get("answer_keys_discovered", 0) or 0) for item in counts
            ),
            "documents_retrieved": sum(
                int(item.get("documents_retrieved", 0) or 0) for item in counts
            ),
            "questions_matched_to_sources": sum(
                int(item.get("questions_matched_to_sources", 0) or 0) for item in counts
            ),
            "cache_hits": sum(int(item.get("cache_hits", 0) or 0) for item in counts),
            "retrieval_failures": sum(
                int(item.get("retrieval_failures", 0) or 0) for item in counts
            ),
            "source_documents": sum(
                int(item.get("source_documents", 0) or 0) for item in counts
            ),
        }

    # -- reports -----------------------------------------------------------
    def write_reports(self, run: RunResult, output_dir: Path | str) -> dict[str, Path]:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        paths = {
            "summary": target / "dataset_recovery_summary.json",
            "courses": target / "dataset_recovery_courses.csv",
            "coverage": target / "dataset_recovery_coverage.csv",
            "issues": target / "dataset_recovery_issues.csv",
            "answers": target / "dataset_recovery_answer_candidates.csv",
            "records": target / "dataset_recovery_records.csv",
            "sources": target / "dataset_recovery_sources.csv",
            "passages": target / "dataset_recovery_passages.csv",
        }
        summary = run.as_dict(include_courses=True)
        paths["summary"].write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        course_rows = []
        coverage_rows = []
        for course in run.courses:
            quality = course.quality
            row = {
                "course_id": course.course_id,
                "course_code": course.course_code,
                "course_name": course.course_name,
                "category_id": course.category_id,
                "status": course.status,
                "overall": quality.get("overall"),
                "note": quality.get("limitations", ""),
                **{f"score_{key}": value for key, value in quality.get("components", {}).items()},
                **{key: value for key, value in course.counts.items() if not isinstance(value, (dict, list))},
                "warnings": " | ".join(course.warnings),
                "errors": " | ".join(course.errors),
            }
            course_rows.append(row)
            coverage = dict(course.counts.get("source_coverage") or {})
            coverage_rows.append(
                {
                    "course_id": course.course_id,
                    "category_id": course.category_id,
                    "status": course.status,
                    "archive_state": coverage.get("archive_state"),
                    "archive_ids": "|".join(coverage.get("archive_ids") or []),
                    "search_state": coverage.get("search_state"),
                    "papers_retrieved": coverage.get("papers_retrieved"),
                    "answer_documents_retrieved": coverage.get(
                        "answer_documents_retrieved"
                    ),
                    "answer_key_entries": coverage.get("answer_key_entries"),
                    "questions_matched": coverage.get("questions_matched"),
                    "official_verified": coverage.get("official_verified"),
                    "identity_ambiguous_candidates": coverage.get(
                        "identity_ambiguous_candidates"
                    ),
                    "released_records": coverage.get("released_records"),
                    "unresolved_records": coverage.get("unresolved_records"),
                    "coverage_status": coverage.get("status"),
                    "blocker": coverage.get("blocker"),
                }
            )
        issue_rows = []
        candidate_rows = []
        record_rows = []
        source_rows = []
        passage_rows = []
        for course in run.courses:
            for candidate in course.source_candidates:
                source_rows.append(
                    {
                        "course_id": course.course_id,
                        "source_id": candidate.source_id,
                        "title": candidate.title,
                        "url": candidate.url,
                        "document_type": candidate.document_type,
                        "source_tier": candidate.source_tier,
                        "reliability": candidate.reliability,
                        "provider": candidate.provider,
                        "query": candidate.query,
                        "retrieval_status": candidate.retrieval_status,
                        "classification_reason": candidate.classification_reason,
                        "snippet": candidate.snippet,
                    }
                )
            for passage in course.passages:
                passage_rows.append(
                    {
                        "course_id": course.course_id,
                        "passage_id": passage.passage_id,
                        "parent_record_id": passage.parent_record_id,
                        "child_record_ids": "|".join(passage.child_record_ids),
                        "confidence": passage.confidence,
                        "passage_chars": len(passage.passage_text),
                        "passage_preview": passage.passage_text[:300],
                        "source_reference": passage.source_reference,
                    }
                )
            for record in course.records:
                record_rows.append(
                    {
                        "course_id": record.course_id,
                        "record_id": record.record_id,
                        "source_row": record.source_row,
                        "source_question_id": record.source_question_id,
                        "question_type": record.repaired.get("question_type"),
                        "answer_status": record.answer_status,
                        "source_fidelity": record.source_fidelity,
                        "verification_status": (record.verification or {}).get("status"),
                        "release_status": record.release_status,
                        "issue_count": record.issue_count,
                        "blocking_count": record.blocking_count,
                        "overall_quality": record.quality.get("overall"),
                    }
                )
                for issue in record.issues:
                    issue_rows.append(
                        {
                            "course_id": record.course_id,
                            "record_id": record.record_id,
                            "code": issue.code,
                            "category": issue.category,
                            "severity": issue.severity,
                            "blocking": issue.blocking,
                            "confidence": issue.confidence,
                            "message": issue.message,
                            "remediation": issue.remediation,
                            "evidence": json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        }
                    )
                for candidate in record.answer_candidates:
                    candidate_rows.append(
                        {
                            "course_id": record.course_id,
                            "record_id": record.record_id,
                            "answer_text": candidate.answer_text,
                            "source_type": candidate.source_type,
                            "source_name": candidate.source_name,
                            "source_url": candidate.source_url,
                            "verification_status": candidate.verification_status,
                            "confidence": candidate.confidence,
                            "method": candidate.method,
                            "official": candidate.official,
                            "retrieved_at": candidate.retrieved_at,
                            "evidence": json.dumps(
                                candidate.evidence, ensure_ascii=False, default=str
                            ),
                        }
                    )
        self._write_csv(paths["courses"], course_rows)
        self._write_csv(paths["coverage"], coverage_rows)
        self._write_csv(paths["issues"], issue_rows)
        self._write_csv(paths["answers"], candidate_rows)
        self._write_csv(paths["records"], record_rows)
        self._write_csv(paths["sources"], source_rows)
        self._write_csv(paths["passages"], passage_rows)
        return paths

    @staticmethod
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


__all__ = ["DatasetRecoveryService"]
