"""``exam-engine evolution`` - Phase 9 concept evolution reports.

The command layer stays thin: date resolution, change detection and every
denominator live in :class:`ConceptEvolutionService` so another consumer can
reuse them without the CLI.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import typer

from exam_engine.config import Settings
from exam_engine.evolution.service import ConceptEvolutionService, EvolutionError
from exam_engine.models.enums import ConceptChangeCategory, DocumentKind
from exam_engine.models.evolution import (
    ConceptChange,
    ConceptTimeline,
    EvolutionFilters,
    EvolutionReport,
    EvolutionRequest,
    SyllabusComparison,
)
from exam_engine.storage.db import Database

OpenDatabase = Callable[[], Database]
GetSettings = Callable[[], Settings]


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        typer.echo("(no rows)")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    typer.echo("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))


def _short(value: str, limit: int = 44) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _dump(value: Any) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        typer.echo(text, nl=False)
        return
    stream.write(text.encode("utf-8"))
    stream.flush()


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def register_evolution_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help="Phase 9 concept evolution: dated timelines, change events and syllabus diffs."
    )

    def _date(value: str | None, option: str) -> date | None:
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise EvolutionError(
                "invalid_evolution_filter",
                f"{option} must be an ISO date (YYYY-MM-DD): {value!r}",
                detail={"option": option, "value": value},
            ) from exc

    def _request(
        course: str,
        concepts: list[str] | None,
        documents: list[str] | None,
        kinds: list[DocumentKind] | None,
        syllabi: list[str] | None,
        date_from: str | None,
        date_to: str | None,
        *,
        include_assessment: bool = True,
        include_exam_dna: bool = True,
        include_relationships: bool = True,
        categories: list[ConceptChangeCategory] | None = None,
    ) -> EvolutionRequest:
        try:
            filters = EvolutionFilters(
                concept_ids=concepts or [],
                document_ids=documents or [],
                document_kinds=kinds or [],
                syllabus_ids=syllabi or [],
                categories=categories or [],
                date_from=_date(date_from, "--date-from"),
                date_to=_date(date_to, "--date-to"),
            )
        except ValueError as exc:
            raise EvolutionError("invalid_evolution_filter", str(exc)) from exc
        return EvolutionRequest(
            course_id=course,
            filters=filters,
            include_assessment=include_assessment,
            include_exam_dna=include_exam_dna,
            include_relationships=include_relationships,
        )

    def _service(database: Database) -> ConceptEvolutionService:
        settings = get_settings()
        return ConceptEvolutionService(
            database,
            settings.evolution,
            coverage_settings=settings.coverage,
            exam_dna_settings=settings.exam_dna,
        )

    def _fail(exc: Exception, *, as_json: bool) -> None:
        if isinstance(exc, EvolutionError):
            payload = exc.as_dict()
        elif isinstance(exc, KeyError):
            payload = {"code": "not_found", "message": str(exc).strip("'"), "detail": {}}
        else:
            payload = {"code": "evolution_error", "message": str(exc), "detail": {}}
        if as_json:
            _dump({"error": payload})
        else:
            typer.echo(f"ERROR: [{payload['code']}] {payload['message']}", err=True)
            if payload.get("detail"):
                typer.echo("detail: " + json.dumps(payload["detail"], sort_keys=True), err=True)
        raise typer.Exit(code=1) from exc

    def _write_export(report: EvolutionReport, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            raise EvolutionError(
                "evolution_export_failed",
                f"could not write {path}: {exc}",
                detail={"path": str(path)},
            ) from exc

    def _print_report(report: EvolutionReport) -> None:
        summary = report.summary
        typer.echo(
            f"course={report.course.course_id} status={report.status.value} "
            f"report={report.report_id}"
        )
        _print_table(
            [
                "documents",
                "dated",
                "concepts",
                "observations",
                "dated obs",
                "changes",
                "review",
                "comparisons",
            ],
            [
                [
                    summary.documents,
                    summary.dated_documents,
                    summary.concepts,
                    summary.observations,
                    summary.dated_observations,
                    summary.changes,
                    summary.review_required_changes,
                    summary.syllabus_comparisons,
                ]
            ],
        )
        if summary.changes_by_category:
            typer.echo(
                "changes: "
                + ", ".join(
                    f"{name} {count}" for name, count in sorted(summary.changes_by_category.items())
                )
            )
        for comparison in report.syllabus_comparisons:
            typer.echo(
                f"syllabus {comparison.earlier.syllabus_id} -> "
                f"{comparison.later.syllabus_id} "
                f"(order_known={str(comparison.order_known).lower()}, "
                f"introduced={len(comparison.introduced)}, "
                f"not_observed_later={len(comparison.no_longer_observed)}, "
                f"moved={len(comparison.moved_units)})"
            )
        typer.echo(f"scope: {report.scope.scope_statement}")
        top = report.changes[:10]
        if top:
            _print_table(
                ["category", "concept", "earlier", "later", "confidence", "review"],
                [
                    [
                        change.category.value,
                        _short(", ".join(change.labels) or "-", 34),
                        str(change.earlier.date) if change.earlier else "-",
                        str(change.later.date) if change.later else "-",
                        f"{change.confidence:.2f}",
                        "yes" if change.review_required else "no",
                    ]
                    for change in top
                ],
            )
        for warning in report.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        typer.echo(
            "note: these are observations inside the analyzed corpus, not the complete history "
            "of any concept, and no change is applied automatically."
        )

    def _print_timeline(timeline: ConceptTimeline) -> None:
        typer.echo(
            f"concept={timeline.concept_id} label={timeline.canonical_label} "
            f"observations={timeline.observation_count} "
            f"dated={timeline.dated_observation_count} "
            f"first={timeline.first_observed} last={timeline.last_observed}"
        )
        _print_table(
            ["date", "type", "document", "term", "unit", "excerpt"],
            [
                [
                    item.date or "-",
                    item.date_type.value,
                    _short(item.document_title, 26),
                    _short(item.observed_term, 24),
                    item.unit_label or "-",
                    _short(item.excerpt, 46),
                ]
                for item in timeline.observations
            ],
        )
        for limitation in timeline.limitations:
            typer.echo(f"  limitation: {limitation}")

    def _print_changes(changes: list[ConceptChange], report: EvolutionReport) -> None:
        typer.echo(
            f"course={report.course.course_id} concepts={report.summary.concepts} "
            f"changes={len(changes)}"
        )
        _print_table(
            ["category", "concept", "earlier", "later", "confidence", "quality", "explanation"],
            [
                [
                    change.category.value,
                    _short(", ".join(change.labels) or "-", 30),
                    str(change.earlier.date) if change.earlier else "-",
                    str(change.later.date) if change.later else "-",
                    f"{change.confidence:.2f}",
                    change.evidence_quality.value,
                    _short(change.explanation, 56),
                ]
                for change in changes
            ],
        )

    def _print_comparison(comparison: SyllabusComparison) -> None:
        typer.echo(
            f"earlier={comparison.earlier.syllabus_id} "
            f"({comparison.earlier.date or 'undated'}) "
            f"later={comparison.later.syllabus_id} "
            f"({comparison.later.date or 'undated'}) "
            f"order_known={str(comparison.order_known).lower()}"
        )
        _print_table(
            ["shared", "introduced", "not observed later", "moved", "description changes"],
            [
                [
                    len(comparison.shared_concepts),
                    len(comparison.introduced),
                    len(comparison.no_longer_observed),
                    len(comparison.moved_units),
                    len(comparison.description_changes),
                ]
            ],
        )
        for move in comparison.moved_units:
            typer.echo(f"  moved: {move.label} {move.earlier_unit!r} -> {move.later_unit!r}")
        for description in comparison.description_changes:
            typer.echo(
                f"  description: {description.unit_label} "
                f"added={description.added_topics} removed={description.removed_topics}"
            )
        for warning in comparison.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")

    @group.command("report")
    def report(
        course: str = typer.Option(..., "--course"),
        concepts: list[str] | None = typer.Option(
            None, "--concept", help="Concept id or label; repeatable."
        ),
        documents: list[str] | None = typer.Option(None, "--document"),
        kinds: list[DocumentKind] | None = typer.Option(None, "--kind"),
        syllabi: list[str] | None = typer.Option(None, "--syllabus"),
        date_from: str | None = typer.Option(None, "--date-from"),
        date_to: str | None = typer.Option(None, "--date-to"),
        no_assessment: bool = typer.Option(
            False, "--no-assessment", help="Skip Phase 7/8 assessment evolution."
        ),
        no_exam_dna: bool = typer.Option(
            False, "--no-exam-dna", help="Skip Phase 8 profile cross-references."
        ),
        no_relationships: bool = typer.Option(
            False, "--no-relationships", help="Skip graph relationship comparison."
        ),
        output: Path | None = typer.Option(None, "--output"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Build a dated, evidence-backed concept evolution report."""
        database = open_db()
        try:
            request = _request(
                course,
                concepts,
                documents,
                kinds,
                syllabi,
                date_from,
                date_to,
                include_assessment=not no_assessment,
                include_exam_dna=not no_exam_dna,
                include_relationships=not no_relationships,
            )
            value = _service(database).analyze(request)
        except (EvolutionError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if output is not None:
            try:
                _write_export(value, output)
            except EvolutionError as exc:
                _fail(exc, as_json=as_json)
        if as_json:
            _dump(value.model_dump(mode="json"))
        else:
            _print_report(value)
            if output is not None:
                typer.echo(f"wrote {output}")

    @group.command("timeline")
    def timeline(
        course: str = typer.Option(..., "--course"),
        concept: str = typer.Option(..., "--concept", help="Concept id or canonical label."),
        documents: list[str] | None = typer.Option(None, "--document"),
        kinds: list[DocumentKind] | None = typer.Option(None, "--kind"),
        syllabi: list[str] | None = typer.Option(None, "--syllabus"),
        date_from: str | None = typer.Option(None, "--date-from"),
        date_to: str | None = typer.Option(None, "--date-to"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show one concept's chronological observations and their date provenance."""
        database = open_db()
        try:
            request = _request(course, None, documents, kinds, syllabi, date_from, date_to)
            value = _service(database).timeline(request, concept)
        except (EvolutionError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if as_json:
            _dump(value.model_dump(mode="json"))
            return
        _print_timeline(value)

    @group.command("changes")
    def changes(
        course: str = typer.Option(..., "--course"),
        concepts: list[str] | None = typer.Option(None, "--concept"),
        documents: list[str] | None = typer.Option(None, "--document"),
        kinds: list[DocumentKind] | None = typer.Option(None, "--kind"),
        syllabi: list[str] | None = typer.Option(None, "--syllabus"),
        date_from: str | None = typer.Option(None, "--date-from"),
        date_to: str | None = typer.Option(None, "--date-to"),
        category: ConceptChangeCategory | None = typer.Option(None, "--category"),
        min_confidence: float | None = typer.Option(None, "--min-confidence", min=0.0, max=1.0),
        review_only: bool = typer.Option(False, "--review-only"),
        limit: int = typer.Option(100, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List detected changes, filtered by category, confidence or review state."""
        database = open_db()
        try:
            request = _request(
                course,
                concepts,
                documents,
                kinds,
                syllabi,
                date_from,
                date_to,
                categories=[category] if category else None,
            )
            service = _service(database)
            value = service.analyze(request)
            rows = service.changes(
                request,
                category=category,
                min_confidence=min_confidence,
                review_only=review_only,
                report=value,
            )[:limit]
        except (EvolutionError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_changes(rows, value)

    @group.command("compare")
    def compare(
        course: str = typer.Option(..., "--course"),
        earlier: str = typer.Option(..., "--earlier", help="Earlier syllabus id."),
        later: str = typer.Option(..., "--later", help="Later syllabus id."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Compare two syllabus versions of one course."""
        database = open_db()
        try:
            request = _request(course, None, None, None, [earlier, later], None, None)
            value = _service(database).compare_syllabi(request, earlier, later)
        except (EvolutionError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if as_json:
            _dump(value.model_dump(mode="json"))
            return
        _print_comparison(value)

    app.add_typer(group, name="evolution")
