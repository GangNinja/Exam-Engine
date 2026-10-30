"""``exam-engine nlp`` - inspect and run the Phase 2 analysis.

Registered from :mod:`exam_engine.cli` with the shared database opener so the
sub-app uses the same ``--db`` handling without importing the parent module
(which would create a cycle).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import typer

from exam_engine.config import Settings
from exam_engine.models.enums import ConceptOrigin, NlpStatus, TextOwnerType
from exam_engine.nlp.embeddings import provider_descriptors
from exam_engine.nlp.entities import SpacyEntityExtractor
from exam_engine.nlp.keywords import EXTRACTORS
from exam_engine.nlp.service import NlpService
from exam_engine.storage.db import Database
from exam_engine.storage.nlp_repositories import (
    ConceptRepository,
    EntityRepository,
    KeyphraseRepository,
    ProcessedTextRepository,
    TopicRepository,
)

OpenDatabase = Callable[[], Database]
GetSettings = Callable[[], Settings]


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        typer.echo("(no rows)")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    typer.echo("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _service(database: Database, settings: Settings) -> NlpService:
    return NlpService(database, settings.nlp)


def register_nlp_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    nlp = typer.Typer(help="Phase 2 NLP analysis: preprocessing, keyphrases, embeddings, topics.")

    @nlp.command("analyze")
    def analyze(
        course: str | None = typer.Option(None, "--course", help="Course id (default: all)."),
        force: bool = typer.Option(
            False, "--force", help="Recompute even when cached text is valid."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Run the full analysis for one course or every course."""
        database = open_db()
        try:
            service = _service(database, get_settings())
            reports = service.analyze(course, force=force)
        finally:
            database.close()
        failed = any(report.status is NlpStatus.FAILED for report in reports)
        if as_json:
            typer.echo(
                json.dumps(
                    [report.model_dump(mode="json") for report in reports], indent=2, default=str
                )
            )
            if failed:
                raise typer.Exit(code=1)
            return
        if not reports:
            typer.echo("no courses to analyse")
            return
        for report in reports:
            typer.echo(
                f"{report.course_id or '(all)'}: {report.status.value} "
                f"texts={report.totals.get('texts', 0)} "
                f"keyphrases={report.totals.get('keyphrases', 0)} "
                f"entities={report.totals.get('entities', 0)} "
                f"topics={report.totals.get('topics', 0)} "
                f"embeddings={report.totals.get('embeddings', 0)} "
                f"({report.duration_ms} ms)"
            )
            if report.model_ids:
                typer.echo(f"  models: {', '.join(report.model_ids)}")
            for warning in report.warnings:
                typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
            for error in report.errors:
                typer.echo(f"  error: {error.code}: {error.message}", err=True)
        if failed:
            raise typer.Exit(code=1)

    @nlp.command("status")
    def status(as_json: bool = typer.Option(False, "--json")) -> None:
        """Show derived-data coverage per course."""
        database = open_db()
        try:
            rows = _service(database, get_settings()).status()
        finally:
            database.close()
        if as_json:
            typer.echo(json.dumps(rows, indent=2, default=str))
            return
        _print_table(
            [
                "course_id",
                "docs",
                "texts",
                "keyphrases",
                "entities",
                "concepts",
                "topics",
                "embeddings",
                "last_run",
                "status",
            ],
            [
                [
                    row["course_id"],
                    str(row["documents"]),
                    str(row["texts"]),
                    str(row["keyphrases"]),
                    str(row["entities"]),
                    str(row["concepts"]),
                    str(row["topics"]),
                    str(row["embeddings"]),
                    row["last_run"] or "-",
                    row["last_status"] or "-",
                ]
                for row in rows
            ],
        )

    @nlp.command("providers")
    def providers() -> None:
        """List keyword methods, embedding providers and entity extractors."""
        typer.echo("keyword methods:")
        for name, extractor in sorted(EXTRACTORS.items()):
            typer.echo(
                f"  {name:<10} n-grams up to "
                f"{getattr(extractor, 'max_ngram', '-')} "
                f"(method={extractor.name.value})"
            )
        typer.echo("\nembedding providers:")
        for descriptor in provider_descriptors():
            requires = "download required" if descriptor.get("requires_download") else "offline"
            installed = descriptor.get("installed")
            suffix = "" if installed is None else f", installed={installed}"
            typer.echo(
                f"  {descriptor['name']:<22} {requires}{suffix} - "
                f"{descriptor.get('description', '')}"
            )
        typer.echo("\nentity extractors:")
        typer.echo("  rule_based   offline lexical patterns (default)")
        typer.echo(
            f"  spacy        optional, installed={SpacyEntityExtractor.installed()}, "
            "model must be downloaded"
        )

    @nlp.command("keywords")
    def keywords(
        course: str = typer.Option(..., "--course"),
        limit: int = typer.Option(20, "--limit", min=1),
        method: str | None = typer.Option(None, "--method", help="tfidf, rake or hybrid."),
        scope: str = typer.Option("course", "--scope", help="course or document."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the highest-scoring keyphrases for a course."""
        owner_type = (
            TextOwnerType.DOCUMENT if scope.casefold().startswith("doc") else TextOwnerType.COURSE
        )
        database = open_db()
        try:
            with database.session() as session:
                records = KeyphraseRepository(session).top_for_course(
                    course, limit=limit, method=method, owner_type=owner_type
                )
        finally:
            database.close()
        if as_json:
            typer.echo(
                json.dumps([r.model_dump(mode="json") for r in records], indent=2, default=str)
            )
            return
        _print_table(
            ["rank", "owner", "n", "score", "method", "term"],
            [
                [
                    str(r.rank),
                    r.owner_type.value,
                    str(r.n_gram),
                    f"{r.score:.4f}",
                    r.method.value,
                    r.term[:48],
                ]
                for r in records
            ],
        )
        typer.echo(f"{len(records)} keyphrase(s)")

    @nlp.command("entities")
    def entities(
        course: str = typer.Option(..., "--course"),
        label: str | None = typer.Option(None, "--label", help="Filter by extractor label."),
        limit: int = typer.Option(30, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show named-entity and technical-term candidates."""
        database = open_db()
        try:
            with database.session() as session:
                records = EntityRepository(session).list_by_course(course, limit=limit, label=label)
        finally:
            database.close()
        if as_json:
            typer.echo(
                json.dumps([r.model_dump(mode="json") for r in records], indent=2, default=str)
            )
            return
        _print_table(
            ["label", "conf", "method", "text"],
            [[r.label[:16], f"{r.confidence:.2f}", r.method.value, r.text[:44]] for r in records],
        )
        typer.echo(f"{len(records)} entity candidate(s); these are not verified concepts")

    @nlp.command("concepts")
    def concepts(
        course: str = typer.Option(..., "--course"),
        origin: str = typer.Option("all", "--origin", help="all, syllabus or inferred."),
        limit: int = typer.Option(30, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List verified syllabus concepts and inferred candidates."""
        selected = None
        if origin.casefold() in {"syllabus", "verified"}:
            selected = ConceptOrigin.SYLLABUS
        elif origin.casefold() in {"inferred", "candidate", "candidates"}:
            selected = ConceptOrigin.INFERRED
        database = open_db()
        try:
            with database.session() as session:
                records = ConceptRepository(session).list_by_course(
                    course, origin=selected, limit=limit
                )
        finally:
            database.close()
        if as_json:
            typer.echo(
                json.dumps([r.model_dump(mode="json") for r in records], indent=2, default=str)
            )
            return
        _print_table(
            ["origin", "verified", "conf", "method", "term"],
            [
                [
                    r.origin.value,
                    "yes" if r.verified else "",
                    f"{r.confidence:.2f}",
                    r.method.value,
                    r.term[:46],
                ]
                for r in records
            ],
        )
        typer.echo(f"{len(records)} concept(s); inferred terms still need review")

    @nlp.command("topics")
    def topics(
        course: str = typer.Option(..., "--course"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show discovered topics with their terms and size."""
        database = open_db()
        try:
            with database.session() as session:
                records = TopicRepository(session).list_by_course(course)
        finally:
            database.close()
        if as_json:
            typer.echo(
                json.dumps([r.model_dump(mode="json") for r in records], indent=2, default=str)
            )
            return
        if not records:
            typer.echo(
                "no topics (see nlp analyze warnings: insufficient data is reported, not invented)"
            )
            return
        for topic in records:
            typer.echo(
                f"\n{topic.scope.value} topic {topic.topic_id[-6:]}  size={topic.size}  "
                f"method={topic.method.value}"
            )
            typer.echo("  " + ", ".join(f"{term.term}" for term in topic.terms[:10]))
            for member in topic.members[:8]:
                typer.echo(f"    - {member.owner_id} ({member.weight:.3f})")
            for warning in topic.warnings:
                typer.echo(f"    note: {warning.message}")

    @nlp.command("preprocess")
    def preprocess(
        document: str = typer.Option(..., "--document", help="Document id to inspect."),
        sentences: int = typer.Option(5, "--sentences", min=1, help="How many sentences to show."),
    ) -> None:
        """Show the stored normalised text for a document (raw text is untouched)."""
        database = open_db()
        try:
            with database.session() as session:
                record = ProcessedTextRepository(session).get(TextOwnerType.DOCUMENT, document)
        finally:
            database.close()
        if record is None:
            typer.echo(f"no processed text for {document}; run 'nlp analyze' first", err=True)
            raise typer.Exit(code=1)
        typer.echo(
            f"{document}  chars={record.char_count} tokens={record.token_count} "
            f"sentences={record.sentence_count} language={record.language or 'unknown'} "
            f"config={record.config_id[:14]}"
        )
        for sentence in record.sentences[:sentences]:
            typer.echo(
                f"  [{sentence.index}] ({sentence.start}-{sentence.end}) {sentence.text[:96]}"
            )
        for warning in record.uncertainties:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")

    @nlp.command("reset")
    def reset(
        course: str | None = typer.Option(None, "--course"),
        all_courses: bool = typer.Option(False, "--all", help="Reset every course."),
        yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    ) -> None:
        """Delete derived NLP rows so the next analysis recomputes them."""
        if not course and not all_courses:
            typer.echo("choose --course <id> or --all", err=True)
            raise typer.Exit(code=2)
        if not yes:
            typer.confirm("Delete derived NLP data for this selection?", abort=True)
        database = open_db()
        try:
            _service(database, get_settings()).reset(None if all_courses else course)
        finally:
            database.close()
        typer.echo("derived NLP data removed")

    app.add_typer(nlp, name="nlp")


__all__ = ["register_nlp_commands"]
