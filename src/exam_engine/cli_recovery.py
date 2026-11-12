"""Phase 14 CLI: dataset intelligence, repair and answer recovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from exam_engine.recovery.service import DatasetRecoveryService
from exam_engine.storage.recovery_repository import RecoveryRepository


def _echo(message: str = "", *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe = str(message).encode(encoding, "backslashreplace").decode(encoding)
    typer.echo(safe, err=err)


def _table(headers: list[str], rows: list[list[Any]]) -> None:
    if not rows:
        _echo("(no rows)")
        return
    widths = [len(item) for item in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    _echo("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    _echo("  ".join("-" * width for width in widths))
    for row in rows:
        _echo("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))


def register_recovery_commands(app: typer.Typer, *, open_db: Any, get_settings: Any) -> None:
    recovery = typer.Typer(
        add_completion=False,
        help="Phase 14 dataset intelligence, repair, answer recovery and quality scoring.",
    )

    @recovery.command("inventory")
    def inventory_command(
        root: Path = typer.Option(Path("datasets"), "--root"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Inventory courses, schemas, CSV files and source documents."""
        database = open_db()
        try:
            payload = DatasetRecoveryService(database, get_settings()).inventory(root)
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        totals = payload["totals"]
        _echo(
            f"categories={totals['categories']} courses={totals['courses']} "
            f"csv={totals['csv']} rows={totals['rows']} pdf={totals['pdf']}"
        )
        _table(
            ["course", "category", "rows", "schema", "question-source"],
            [
                [
                    item["course_id"],
                    item["category_id"],
                    item["rows"],
                    item["schema"]["confidence"],
                    "yes"
                    if any(
                        doc["question_fidelity_available"]
                        for doc in item["source_documents"]
                    )
                    else "no",
                ]
                for item in payload["files"]
            ],
        )

    @recovery.command("analyze")
    def analyze_command(
        root: Path = typer.Option(Path("datasets"), "--root"),
        course: list[str] | None = typer.Option(None, "--course", help="Course id; repeatable."),
        category: list[str] | None = typer.Option(
            None, "--category", help="Category id; repeatable."
        ),
        mode: str = typer.Option("auto", "--mode", help="auto, review or audit"),
        workflow: str = typer.Option(
            "full", "--workflow", help="inventory, analyze, recover, verify, reconstruct or full"
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Analyze without database writes."),
        use_web: bool = typer.Option(False, "--use-web", help="Discover public sources."),
        provider: str = typer.Option(
            "auto", "--provider", help="auto, exa, duckduckgo, wikipedia or off"
        ),
        fetch_sources: bool = typer.Option(
            False, "--fetch-sources", help="Retrieve permitted public documents, not only snippets."
        ),
        crawl_engine: str = typer.Option(
            "crawl4ai", "--crawl-engine", help="crawl4ai for rendered HTML, or http fallback."
        ),
        retrieval_limit: int = typer.Option(3, "--retrieval-limit", min=0, max=8),
        retrieval_follow_links: bool = typer.Option(
            True, "--retrieval-follow-links/--no-retrieval-follow-links"
        ),
        retry_failed: bool = typer.Option(False, "--retry-failed"),
        web_limit: int = typer.Option(0, "--web-limit", min=0),
        source_limit: int = typer.Option(
            0, "--source-limit", min=0, help="Maximum grouped source-discovery queries."
        ),
        use_model: bool = typer.Option(
            False, "--use-model", help="Allow AI answer candidates (never official)."
        ),
        model_limit: int = typer.Option(0, "--model-limit", min=0),
        run_id: str | None = typer.Option(None, "--run-id"),
        resume: bool = typer.Option(False, "--resume", help="Resume an interrupted run."),
        incremental: bool = typer.Option(
            False, "--incremental", help="Re-analyse only changed files in the latest run."
        ),
        report_dir: Path | None = typer.Option(Path("reports/recovery"), "--report-dir"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Analyze and repair the selected datasets without altering raw files."""
        if resume and not run_id:
            _echo("--resume requires --run-id", err=True)
            raise typer.Exit(code=1)
        database = open_db()
        try:
            result = DatasetRecoveryService(database, get_settings()).analyze(
                root,
                course_ids=course or None,
                category_ids=category or None,
                mode=mode,
                workflow=workflow,
                dry_run=dry_run,
                use_web=use_web,
                use_model=use_model,
                provider=provider,
                fetch_sources=fetch_sources,
                crawl_engine=crawl_engine,
                retrieval_limit=retrieval_limit,
                retrieval_follow_links=retrieval_follow_links,
                retry_failed=retry_failed,
                web_limit=web_limit,
                source_limit=source_limit,
                model_limit=model_limit,
                run_id=run_id,
                resume_run_id=run_id if resume else None,
                incremental=incremental,
                output_dir=report_dir,
            )
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(result.as_dict(include_courses=True), indent=2, ensure_ascii=False))
            return
        _echo(f"run={result.run_id} status={result.status} dry_run={result.dry_run}")
        totals = result.totals
        _echo(
            f"courses={totals.get('courses', 0)} records={totals.get('records', 0)} "
            f"missing_answers={totals.get('missing_answers', 0)} "
            f"recovered={totals.get('recovered_answers', 0)} "
            f"verified={totals.get('verified_answers', 0)} "
            f"sources={totals.get('source_candidates', 0)} "
            f"retrieved_docs={totals.get('documents_retrieved', 0)} "
            f"matched_questions={totals.get('questions_matched_to_sources', 0)} "
            f"papers={totals.get('question_papers_found', 0)} "
            f"answer_keys={totals.get('answer_keys_found', 0)} "
            f"passage_groups={totals.get('passage_groups', 0)} "
            f"quarantined={totals.get('by_release_status', {}).get('quarantined', 0)} "
            f"issues={totals.get('issues', 0)}"
        )
        _table(
            ["course", "status", "score", "records", "missing", "recovered", "review"],
            [
                [
                    item.course_id,
                    item.status,
                    item.quality.get("overall", 0),
                    item.analyzed_count,
                    item.counts.get("missing_answer", 0),
                    item.counts.get("recovered_answers", 0),
                    item.counts.get("review_required", 0),
                ]
                for item in result.courses
            ],
        )
        if report_dir:
            _echo(f"reports written to {report_dir}")
        if result.warnings:
            for warning in result.warnings[:10]:
                _echo(f"note: {warning}", err=True)

    @recovery.command("file")
    def file_command(
        csv_path: Path = typer.Argument(..., exists=True, dir_okay=False),
        course_id: str = typer.Option(..., "--course-id"),
        course_name: str | None = typer.Option(None, "--course-name"),
        course_code: str | None = typer.Option(None, "--course-code"),
        category: str = typer.Option("custom", "--category"),
        mode: str = typer.Option("auto", "--mode"),
        workflow: str = typer.Option("full", "--workflow"),
        dry_run: bool = typer.Option(False, "--dry-run"),
        use_web: bool = typer.Option(False, "--use-web"),
        provider: str = typer.Option("auto", "--provider"),
        fetch_sources: bool = typer.Option(False, "--fetch-sources"),
        crawl_engine: str = typer.Option("crawl4ai", "--crawl-engine"),
        source_limit: int = typer.Option(1, "--source-limit", min=0),
        retrieval_limit: int = typer.Option(2, "--retrieval-limit", min=0, max=8),
        report_dir: Path | None = typer.Option(Path("reports/recovery"), "--report-dir"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Analyze one arbitrary CSV without requiring a predefined exam folder."""
        database = open_db()
        try:
            result = DatasetRecoveryService(database, get_settings()).analyze(
                csv_path.parent,
                mode=mode,
                dry_run=dry_run,
                csv_path=csv_path,
                course_id=course_id,
                course_name=course_name,
                course_code=course_code,
                category_id=category,
                workflow=workflow,
                use_web=use_web,
                provider=provider,
                fetch_sources=fetch_sources,
                crawl_engine=crawl_engine,
                source_limit=source_limit,
                retrieval_limit=retrieval_limit,
                output_dir=report_dir,
            )
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(result.as_dict(include_courses=True), indent=2, ensure_ascii=False))
            return
        course = result.courses[0]
        _echo(
            f"run={result.run_id} course={course.course_id} status={course.status} "
            f"records={course.analyzed_count} missing={course.counts.get('missing_answer', 0)} "
            f"recovered={course.counts.get('recovered_answers', 0)}"
        )
        if report_dir:
            _echo(f"reports written to {report_dir}")

    @recovery.command("providers")
    def providers_command(
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show real search-provider availability without making a query."""
        from exam_engine.recovery.providers import provider_descriptions

        rows = provider_descriptions(get_settings().tutor)
        if as_json:
            _echo(json.dumps({"providers": rows}, indent=2, ensure_ascii=False))
            return
        _table(
            ["provider", "available", "configured", "notes"],
            [
                [
                    item.get("name"),
                    "yes" if item.get("available") else "no",
                    "yes" if item.get("configured") else "no",
                    str(item.get("detail") or item.get("reason") or "")[:90],
                ]
                for item in rows
            ],
        )

    @recovery.command("crawl-health")
    def crawl_health_command(
        force: bool = typer.Option(False, "--force", help="Ignore the five-minute health cache."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Check Crawl4AI, Playwright and the Chromium browser runtime."""
        from exam_engine.recovery.crawl4ai_adapter import Crawl4AIHealth

        payload = Crawl4AIHealth.check(force=force)
        if as_json:
            _echo(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        _echo(
            f"status={payload.get('status')} installed={payload.get('installed')} "
            f"version={payload.get('version')} browser={payload.get('browser_launch_ok')} "
            f"headless={payload.get('headless')}"
        )
        if payload.get("error"):
            _echo(f"diagnostic: {payload['error']}", err=True)

    @recovery.command("crawl")
    def crawl_command(
        url: str = typer.Argument(..., help="Public HTTP(S) page to render."),
        as_json: bool = typer.Option(False, "--json"),
        timeout: float = typer.Option(30.0, "--timeout", min=5.0, max=120.0),
    ) -> None:
        """Render one public page with Crawl4AI and inspect extracted content."""
        from exam_engine.recovery.crawl4ai_adapter import Crawl4AIFetcher

        fetcher = Crawl4AIFetcher(timeout=timeout)
        try:
            page = fetcher.crawl(url)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _echo(f"crawl failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            fetcher.close()
        payload = {
            "url": page.url,
            "final_url": page.final_url,
            "status_code": page.status_code,
            "title": page.title,
            "canonical_url": page.canonical_url,
            "markdown_chars": len(page.markdown),
            "links": page.links[:50],
            "tables": page.tables[:5],
            "metadata": page.metadata,
            "elapsed_ms": page.elapsed_ms,
        }
        if as_json:
            _echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return
        _echo(
            f"url={page.final_url} status={page.status_code} title={page.title!r} "
            f"markdown={len(page.markdown)} links={len(page.links)} "
            f"tables={len(page.tables)} elapsed_ms={page.elapsed_ms}"
        )
        _echo(page.markdown[:1000])

    @recovery.command("sources")
    def sources_command(
        run_id: str | None = typer.Option(None, "--run-id"),
        course: str | None = typer.Option(None, "--course"),
        limit: int = typer.Option(100, "--limit", min=1, max=2000),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List discovered public sources and their retrieval evidence."""
        database = open_db()
        try:
            with database.session() as session:
                rows = RecoveryRepository(session).list_sources(
                    run_id, course_id=course, limit=limit
                )
        finally:
            database.close()
        if as_json:
            _echo(json.dumps({"sources": rows, "total": len(rows)}, indent=2, ensure_ascii=False))
            return
        _table(
            ["tier", "retrieval", "type", "course", "provider", "title"],
            [
                [
                    item["source_tier"],
                    item["retrieval_status"],
                    item["document_type"],
                    item["course_id"],
                    item["provider"],
                    str(item["title"])[:70],
                ]
                for item in rows
            ],
        )

    @recovery.command("status")
    def status_command(
        run_id: str | None = typer.Option(None, "--run-id"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the latest recovery run."""
        database = open_db()
        try:
            with database.session() as session:
                payload = RecoveryRepository(session).overview(run_id)
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        run = payload.get("run") or {}
        _echo(
            f"run={payload.get('run_id')} status={run.get('status')} "
            f"courses={payload.get('courses', 0)} records={payload.get('records', 0)}"
        )
        totals = payload.get("totals") or {}
        _echo(
            f"missing_answers={totals.get('missing_answers', 0)} "
            f"recovered={totals.get('recovered_answers', 0)} "
            f"verified={totals.get('verified_answers', 0)} "
            f"quarantined={totals.get('by_release_status', {}).get('quarantined', 0)}"
        )
        cache = payload.get("retrieval_cache") or {}
        _echo(
            f"retrieval_cache={cache.get('total', 0)} ok={cache.get('ok', 0)} "
            f"failed={cache.get('error', 0)}"
        )

    @recovery.command("courses")
    def courses_command(
        run_id: str | None = typer.Option(None, "--run-id"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List course-level quality results."""
        database = open_db()
        try:
            with database.session() as session:
                rows = RecoveryRepository(session).list_courses(run_id)
        finally:
            database.close()
        if as_json:
            _echo(json.dumps({"courses": rows}, indent=2, ensure_ascii=False))
            return
        _table(
            ["course", "quality", "status", "records", "missing", "verified", "quarantined"],
            [
                [
                    row["course_id"],
                    row["overall"],
                    row["quality_status"],
                    row["analyzed_count"],
                    row["counts"].get("missing_answer", 0),
                    row["counts"].get("verified", 0),
                    row["counts"].get("quarantined", 0),
                ]
                for row in rows
            ],
        )

    @recovery.command("issues")
    def issues_command(
        course: str | None = typer.Option(None, "--course"),
        severity: str | None = typer.Option(None, "--severity"),
        limit: int = typer.Option(100, "--limit", min=1, max=1000),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List detected issues across a run."""
        database = open_db()
        try:
            with database.session() as session:
                rows = RecoveryRepository(session).list_issues(
                    course_id=course, severity=severity, limit=limit
                )
        finally:
            database.close()
        if as_json:
            _echo(json.dumps({"issues": rows}, indent=2, ensure_ascii=False))
            return
        _table(
            ["course", "record", "severity", "code", "message"],
            [
                [
                    row["course_id"],
                    row["record_id"][:16],
                    row["severity"],
                    row["code"],
                    row["message"][:70],
                ]
                for row in rows
            ],
        )

    @recovery.command("show")
    def show_command(
        record_id: str = typer.Argument(...),
        run_id: str | None = typer.Option(None, "--run-id"),
    ) -> None:
        """Show the original, repaired, evidence and candidates for one record."""
        database = open_db()
        try:
            with database.session() as session:
                payload = RecoveryRepository(session).record_detail(record_id, run_id=run_id)
        finally:
            database.close()
        if payload is None:
            _echo(f"unknown recovery record: {record_id}", err=True)
            raise typer.Exit(code=1)
        _echo(json.dumps(payload, indent=2, ensure_ascii=False))

    @recovery.command("review")
    def review_command(
        repair_id: int = typer.Argument(...),
        approve: bool = typer.Option(False, "--approve"),
        reject: bool = typer.Option(False, "--reject"),
        note: str | None = typer.Option(None, "--note"),
        reviewer: str = typer.Option("cli-user", "--reviewer"),
        edited_after: str | None = typer.Option(None, "--edited-after-json"),
    ) -> None:
        """Approve, reject or edit a proposed repair."""
        if approve == reject:
            _echo("choose exactly one of --approve or --reject", err=True)
            raise typer.Exit(code=1)
        edited: Any = None
        if edited_after:
            try:
                edited = json.loads(edited_after)
            except json.JSONDecodeError as exc:
                _echo(f"invalid edited JSON: {exc}", err=True)
                raise typer.Exit(code=1) from exc
        database = open_db()
        try:
            with database.transaction() as session:
                payload = RecoveryRepository(session).review_repair(
                    repair_id,
                    status="approved" if approve else "rejected",
                    reviewer=reviewer,
                    note=note,
                    edited_after=edited,
                )
        finally:
            database.close()
        if payload is None:
            _echo(f"unknown repair: {repair_id}", err=True)
            raise typer.Exit(code=1)
        _echo(f"repair {repair_id} {'approved' if approve else 'rejected'}")

    @recovery.command("rollback")
    def rollback_command(
        run_id: str = typer.Argument(...),
        yes: bool = typer.Option(False, "--yes", help="Do not prompt for confirmation."),
    ) -> None:
        """Quarantine all releases from a recovery run without deleting audit evidence."""
        if not yes and not typer.confirm("Quarantine every release from this run?"):
            raise typer.Exit(code=1)
        database = open_db()
        try:
            with database.transaction() as session:
                ok = RecoveryRepository(session).rollback(run_id)
        finally:
            database.close()
        if not ok:
            _echo(f"unknown run: {run_id}", err=True)
            raise typer.Exit(code=1)
        _echo(f"run {run_id} rolled back; raw datasets were not modified")

    @recovery.command("export")
    def export_command(
        course: str = typer.Option(..., "--course"),
        output: Path = typer.Option(Path("reports/recovery/released.json"), "--output"),
        run_id: str | None = typer.Option(None, "--run-id"),
    ) -> None:
        """Export only records explicitly released by recovery."""
        database = open_db()
        try:
            with database.session() as session:
                rows = RecoveryRepository(session).release_records(course, run_id=run_id)
        finally:
            database.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        _echo(f"exported {len(rows)} released record(s) to {output}")

    app.add_typer(recovery, name="recovery")


__all__ = ["register_recovery_commands"]
