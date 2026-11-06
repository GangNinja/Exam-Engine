"""Command line interface for the multi-phase exam knowledge engine."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import typer

from exam_engine.config import Settings
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.models.course import CourseRecord
from exam_engine.models.enums import (
    DatasetType,
    DocumentKind,
    DuplicatePolicy,
    IngestionStatus,
    Severity,
)
from exam_engine.models.provenance import DatasetSource
from exam_engine.storage.db import Database, open_database
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    QuestionRepository,
    RunRepository,
    SyllabusRepository,
)

app = typer.Typer(
    add_completion=False,
    help=(
        "Intelligent Exam Knowledge & Assessment Engine: ingestion, NLP, analytics, "
        "graph, prerequisites, coverage, Exam DNA patterns and concept evolution."
    ),
)

_STATE: dict[str, Any] = {"db": None, "ocr": None, "verbose": False}


@app.callback()
def _main(
    db: Path | None = typer.Option(
        None, "--db", help="SQLite database path (default: EXAM_ENGINE_DB or data/exam_engine.db)."
    ),
    ocr: str | None = typer.Option(
        None, "--ocr-provider", help="OCR provider name (none, tesseract, or a registered stub)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    _STATE["db"] = db
    _STATE["ocr"] = ocr
    _STATE["verbose"] = verbose
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _settings() -> Settings:
    settings = Settings.from_env().with_db(_STATE["db"])
    if _STATE["ocr"]:
        from dataclasses import replace

        settings = replace(settings, ocr_provider=_STATE["ocr"])
    return settings


def _open() -> Database:
    settings = _settings()
    try:
        return open_database(settings.db_path)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        typer.echo(f"ERROR: could not open database: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _pipeline(database: Database) -> IngestionPipeline:
    return IngestionPipeline(database, settings=_settings())


# --- commands ---------------------------------------------------------------


@app.command("init-db")
def init_db() -> None:
    """Create or migrate the SQLite schema."""
    settings = _settings()
    database = open_database(settings.db_path)
    try:
        applied = database.applied_migrations
        typer.echo(f"database ready: {settings.db_path}")
        if applied:
            typer.echo("applied migrations: " + ", ".join(applied))
        else:
            typer.echo("schema already up to date")
    finally:
        database.close()


@app.command("import")
def import_manifest(
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
    on_duplicate: DuplicatePolicy = typer.Option(
        DuplicatePolicy.SKIP,
        "--on-duplicate",
        case_sensitive=False,
        help="What to do when identical content was already ingested for the course.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and report without writing."),
    allow_external: bool = typer.Option(
        False, "--allow-external", help="Allow manifest paths outside the manifest directory."
    ),
    with_nlp: bool = typer.Option(
        False,
        "--with-nlp",
        help="Run the Phase 2 analysis for every course in the batch after ingesting.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the full report as JSON."),
) -> None:
    """Ingest every document listed in a dataset manifest."""
    database = _open()
    try:
        batch = _pipeline(database).ingest_manifest(
            manifest,
            duplicate_policy=on_duplicate,
            dry_run=dry_run,
            allow_external=allow_external,
        )
        nlp_lines = _run_nlp_for_batch(database, batch) if with_nlp and not dry_run else []
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        if as_json:
            typer.echo(json.dumps({"error": str(exc), "code": getattr(exc, "code", "error")}))
        else:
            typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        database.close()

    if as_json:
        typer.echo(json.dumps(batch.model_dump(mode="json"), indent=2, default=str))
        for line in nlp_lines:
            typer.echo(line, err=True)
    else:
        _print_batch(batch, dry_run=dry_run)
        for line in nlp_lines:
            typer.echo(line)
    if not batch.ok:
        raise typer.Exit(code=1)


@app.command("ingest")
def ingest_one(
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    course_id: str = typer.Option(..., "--course-id", help="Stable course slug."),
    course_code: str = typer.Option(..., "--course-code"),
    course_title: str = typer.Option(..., "--course-title"),
    institution_code: str = typer.Option(..., "--institution-code"),
    institution_name: str = typer.Option(..., "--institution-name"),
    kind: DocumentKind = typer.Option(DocumentKind.OTHER, "--kind", case_sensitive=False),
    source_id: str = typer.Option(..., "--source-id", help="Dataset provenance slug."),
    dataset_type: DatasetType = typer.Option(
        DatasetType.SYNTHETIC, "--dataset-type", case_sensitive=False
    ),
    dataset_name: str | None = typer.Option(None, "--dataset-name"),
    license_text: str | None = typer.Option(None, "--license"),
    usage_terms: str | None = typer.Option(None, "--usage-terms"),
    source_url: str | None = typer.Option(None, "--source-url"),
    academic_year: str | None = typer.Option(None, "--academic-year"),
    syllabus_version: str | None = typer.Option(None, "--syllabus-version"),
    department: str | None = typer.Option(None, "--department"),
    on_duplicate: DuplicatePolicy = typer.Option(
        DuplicatePolicy.SKIP, "--on-duplicate", case_sensitive=False
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Ingest a single file without writing a manifest."""
    if not license_text and dataset_type is DatasetType.SYNTHETIC:
        license_text = "Synthetic data generated for development testing."
    course = CourseRecord(
        course_id=course_id,
        course_code=course_code,
        title=course_title,
        institution_code=institution_code,
        institution_name=institution_name,
        department=department,
        academic_year=academic_year,
        syllabus_version=syllabus_version,
    )
    source = DatasetSource(
        source_id=source_id,
        name=dataset_name or source_id,
        dataset_type=dataset_type,
        retrieved_at=date.today(),
        license=license_text,
        usage_terms=usage_terms,
        source_url=source_url,
        external_dataset=True,
    )
    database = _open()
    try:
        result = _pipeline(database).ingest_file(
            path,
            course=course,
            source=source,
            kind=kind,
            duplicate_policy=on_duplicate,
            dry_run=dry_run,
        )
    finally:
        database.close()

    typer.echo(f"{result.status.value.upper():<18} {result.source_path}")
    typer.echo(
        f"  course={result.course_id} pages={result.page_count} "
        f"questions={result.question_count} units={result.syllabus_unit_count}"
    )
    for warning in result.warnings:
        typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
    for error in result.errors:
        typer.echo(f"  error: {error.code}: {error.message}", err=True)
    if result.status is IngestionStatus.FAILED:
        raise typer.Exit(code=1)


@app.command("courses")
def list_courses() -> None:
    """List ingested courses."""
    database = _open()
    try:
        with database.session() as session:
            rows = CourseRepository(session).list()
            documents = DocumentRepository(session)
            questions = QuestionRepository(session)
            units = SyllabusRepository(session)
            table = [
                [
                    course.course_id,
                    course.course_code,
                    course.title,
                    course.institution_code,
                    str(documents.count_by_course(course.course_id)),
                    str(questions.count_by_course(course.course_id)),
                    str(units.count_units_by_course(course.course_id)),
                ]
                for course in rows
            ]
    finally:
        database.close()
    _print_table(["course_id", "code", "title", "institution", "docs", "questions", "units"], table)


@app.command("documents")
def list_documents(
    course: str | None = typer.Option(None, "--course", help="Filter by course_id."),
) -> None:
    """List documents with provenance and extraction state."""
    database = _open()
    try:
        with database.session() as session:
            repo = DocumentRepository(session)
            docs = repo.list_by_course(course) if course else repo.list_all()
            table = [
                [
                    doc.document_id[:18],
                    doc.course_id,
                    doc.kind.value,
                    doc.format.value,
                    str(doc.page_count),
                    f"{doc.quality_score:.2f}",
                    doc.extraction_status.value,
                    doc.title[:40],
                ]
                for doc in docs
            ]
    finally:
        database.close()
    _print_table(
        ["document_id", "course", "kind", "format", "pages", "quality", "status", "title"],
        table,
    )


@app.command("questions")
def list_questions(
    course: str | None = typer.Option(None, "--course"),
    document: str | None = typer.Option(None, "--document"),
    needs_review: bool = typer.Option(False, "--needs-review"),
    limit: int = typer.Option(40, "--limit", min=1),
) -> None:
    """Show extracted questions with confidence and provenance."""
    database = _open()
    try:
        with database.session() as session:
            repo = QuestionRepository(session)
            if document:
                questions = repo.list_by_document(document)
            elif needs_review:
                questions = repo.list_needing_review(course, limit=limit)
            elif course:
                questions = repo.list_by_course(course, limit=limit)
            else:
                questions = repo.list_all(limit=limit)
    finally:
        database.close()

    table = [
        [
            q.document_id[:14],
            q.question_number or "-",
            (q.unit or "-")[:14],
            f"{q.marks:g}" if q.marks is not None else "-",
            f"{q.confidence:.2f}",
            "yes" if q.needs_review else "",
            q.source_locator or "-",
            q.text.replace("\n", " ")[:52],
        ]
        for q in questions[:limit]
    ]
    _print_table(["document", "q#", "unit", "marks", "conf", "review", "source", "text"], table)
    typer.echo(f"{len(questions)} question(s) shown (limit {limit})")


@app.command("syllabus")
def show_syllabus(
    course: str | None = typer.Option(None, "--course"),
) -> None:
    """Show parsed syllabi, units and topics."""
    database = _open()
    try:
        with database.session() as session:
            repo = SyllabusRepository(session)
            course_repo = CourseRepository(session)
            course_ids = [course] if course else [c.course_id for c in course_repo.list()]
            syllabi = [s for cid in course_ids for s in repo.get_by_course(cid)]
    finally:
        database.close()

    if not syllabi:
        typer.echo("no syllabi ingested")
        return
    for syllabus in syllabi:
        typer.echo(
            f"\n{syllabus.course_id}  syllabus={syllabus.syllabus_id[:18]} "
            f"confidence={syllabus.confidence:.2f}"
            + ("  NEEDS REVIEW" if syllabus.needs_review else "")
        )
        for unit in syllabus.units:
            hours = f" ({unit.hours:g} h)" if unit.hours is not None else ""
            typer.echo(f"  {unit.unit_label}: {unit.title or '(untitled)'}{hours}")
            for topic in unit.topics:
                typer.echo(f"    - {topic}")


@app.command("runs")
def list_runs(limit: int = typer.Option(10, "--limit", min=1)) -> None:
    """List stored ingestion runs."""
    database = _open()
    try:
        with database.session() as session:
            runs = RunRepository(session).list_runs(limit)
    finally:
        database.close()
    table = [
        [
            run["run_id"],
            run["status"],
            run["started_at"][:19],
            str(run["totals"].get("documents", 0)),
            str(run["totals"].get("questions", 0)),
            str(run["totals"].get("failed", 0)),
        ]
        for run in runs
    ]
    _print_table(["run_id", "status", "started", "docs", "questions", "failed"], table)


@app.command("run")
def show_run(run_id: str = typer.Argument(...)) -> None:
    """Show the per-document report for one run."""
    database = _open()
    try:
        with database.session() as session:
            batch = RunRepository(session).get_run(run_id)
    finally:
        database.close()
    if batch is None:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(code=1)
    _print_batch(batch)


@app.command("stats")
def stats() -> None:
    """Summarise stored data per course."""
    database = _open()
    try:
        with database.session() as session:
            courses = CourseRepository(session).list()
            docs = DocumentRepository(session)
            questions = QuestionRepository(session)
            units = SyllabusRepository(session)
            rows = []
            for course in courses:
                course_questions = questions.list_by_course(course.course_id)
                rows.append(
                    [
                        course.course_id,
                        str(docs.count_by_course(course.course_id)),
                        str(len(course_questions)),
                        str(sum(1 for q in course_questions if q.needs_review)),
                        str(units.count_units_by_course(course.course_id)),
                    ]
                )
    finally:
        database.close()
    _print_table(["course_id", "documents", "questions", "needs_review", "syllabus_units"], rows)


def _run_nlp_for_batch(database: Database, batch) -> list[str]:
    """Optional post-ingest hook: analyse only courses that produced new content."""
    from exam_engine.nlp.service import NlpService

    course_ids = sorted(
        {
            result.course_id
            for result in batch.results
            if result.status in (IngestionStatus.INGESTED, IngestionStatus.REPLACED)
        }
    )
    if not course_ids:
        return ["nlp: nothing new to analyse"]
    service = NlpService(database, _settings().nlp)
    lines: list[str] = []
    for course_id in course_ids:
        report = service.analyze_course(course_id)
        lines.append(
            f"nlp: {course_id} {report.status.value} "
            f"keyphrases={report.totals.get('keyphrases', 0)} "
            f"entities={report.totals.get('entities', 0)} "
            f"topics={report.totals.get('topics', 0)} ({report.duration_ms} ms)"
        )
        for warning in report.warnings:
            if warning.severity.value == "warning":
                lines.append(f"  note: {warning.code}: {warning.message}")
    return lines


# --- rendering --------------------------------------------------------------


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        typer.echo("(no rows)")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    header_line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    typer.echo(header_line)
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _print_batch(batch, *, dry_run: bool = False) -> None:
    typer.echo(
        f"run {batch.run_id}"
        + (" (dry run, nothing written)" if dry_run else "")
        + f"  dataset={batch.source_id}"
    )
    for warning in batch.warnings:
        typer.echo(f"  note: {warning.code}: {warning.message}")
    for result in batch.results:
        marker = {
            "ingested": "OK",
            "replaced": "REPLACED",
            "skipped_duplicate": "SKIP",
            "needs_ocr": "NEEDS_OCR",
            "empty": "EMPTY",
            "failed": "FAILED",
        }.get(result.status.value, result.status.value)
        typer.echo(
            f"  {marker:<13} {result.source_path} "
            f"[{result.course_id}] pages={result.page_count} "
            f"questions={result.question_count} units={result.syllabus_unit_count} "
            f"{result.duration_ms}ms"
        )
        for warning in result.warnings:
            if warning.severity is Severity.INFO:
                continue
            typer.echo(f"      warning: {warning.code}: {warning.message}")
        for error in result.errors:
            typer.echo(f"      error: {error.code}: {error.message}")
    totals = batch.totals
    typer.echo(
        f"totals: documents={totals['documents']} ingested={totals['ingested']} "
        f"skipped={totals['skipped_duplicate']} needs_ocr={totals['needs_ocr']} "
        f"empty={totals['empty']} failed={totals['failed']} "
        f"questions={totals['questions']} units={totals['syllabus_units']} "
        f"({batch.duration_ms()} ms)"
    )


# The NLP sub-app shares this module's database opener instead of importing it
# back (which would create a cycle).
from exam_engine.cli_advanced import register_advanced_commands  # noqa: E402
from exam_engine.cli_analytics import register_analytics_commands  # noqa: E402
from exam_engine.cli_coverage import register_coverage_commands  # noqa: E402
from exam_engine.cli_dashboard import register_dashboard_commands  # noqa: E402
from exam_engine.cli_evolution import register_evolution_commands  # noqa: E402
from exam_engine.cli_exam_dna import register_exam_dna_commands  # noqa: E402
from exam_engine.cli_exams import register_exams_commands  # noqa: E402
from exam_engine.cli_generation import register_generation_commands  # noqa: E402
from exam_engine.cli_graph import register_graph_commands  # noqa: E402
from exam_engine.cli_nlp import register_nlp_commands  # noqa: E402
from exam_engine.cli_prerequisite import register_prerequisite_commands  # noqa: E402
from exam_engine.cli_understanding import register_understanding_commands  # noqa: E402

register_nlp_commands(app, open_db=_open, get_settings=_settings)
register_understanding_commands(app, open_db=_open, get_settings=_settings)
register_analytics_commands(app, open_db=_open, get_settings=_settings)
register_graph_commands(app, open_db=_open, get_settings=_settings)
register_prerequisite_commands(app, open_db=_open, get_settings=_settings)
register_coverage_commands(app, open_db=_open, get_settings=_settings)
register_exam_dna_commands(app, open_db=_open, get_settings=_settings)
register_evolution_commands(app, open_db=_open, get_settings=_settings)
register_generation_commands(app, open_db=_open, get_settings=_settings)
register_advanced_commands(app, open_db=_open, get_settings=_settings)
register_dashboard_commands(app, get_settings=_settings)
register_exams_commands(app, open_db=_open, get_settings=_settings)


if __name__ == "__main__":  # pragma: no cover
    app()
