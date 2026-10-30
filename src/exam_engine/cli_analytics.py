"""``exam-engine analytics`` - Phase 4 question-paper analytics.

The CLI is a thin inspector over :class:`AnalyticsService`; all calculations and
denominators live in the service/model layer so later interfaces can reuse them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import typer

from exam_engine.analytics.service import AnalyticsService
from exam_engine.config import AnalyticsSettings, Settings
from exam_engine.models.analytics import AnalyticsFilters, AnalyticsReport
from exam_engine.models.enums import DifficultyLabel, QuestionType, SimilarityKind
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
    typer.echo("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _short(value: str, limit: int = 80) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _settings_with_overrides(settings: Settings, threshold: float | None) -> AnalyticsSettings:
    if threshold is None:
        return settings.analytics
    return replace(settings.analytics, similarity_threshold=threshold)


def _service(
    database: Database, settings: Settings, threshold: float | None = None
) -> AnalyticsService:
    return AnalyticsService(database, _settings_with_overrides(settings, threshold))


def _filters(
    *,
    institution: str | None,
    syllabus_version: str | None,
    source: list[str] | None,
    document: list[str] | None,
    unit: list[str] | None,
    topic: list[str] | None,
    exam_type: list[str] | None,
    period: list[str] | None,
    question_type: list[QuestionType] | None,
    difficulty: list[DifficultyLabel] | None,
    marks_min: float | None,
    marks_max: float | None,
) -> AnalyticsFilters:
    return AnalyticsFilters(
        institution_code=institution,
        syllabus_version=syllabus_version,
        source_ids=source or [],
        document_ids=document or [],
        unit_ids=unit or [],
        topics=topic or [],
        exam_types=exam_type or [],
        periods=period or [],
        question_types=question_type or [],
        difficulties=difficulty or [],
        marks_min=marks_min,
        marks_max=marks_max,
    )


def _reports(
    database: Database,
    settings: Settings,
    courses: list[str] | None,
    filters: AnalyticsFilters,
    threshold: float | None = None,
) -> list[AnalyticsReport]:
    service = _service(database, settings, threshold)
    if courses:
        return [service.analyze(course, filters)[0] for course in courses]
    return service.analyze(None, filters)


def _print_report(report: AnalyticsReport) -> None:
    quality = report.data_quality
    mapping = report.mapping
    marks = report.marks
    repetition = report.repetition
    trends = report.trends
    types = report.question_types
    difficulty = report.difficulty
    typer.echo(f"{report.course.course_id}  {report.course.course_code}  {report.course.title}")
    typer.echo(
        f"questions={quality.questions} distinct={quality.distinct_question_texts} "
        f"documents={quality.documents} papers={quality.question_papers} "
        f"banks={quality.question_banks} sessions={quality.known_sessions} "
        f"periods={quality.known_periods}"
    )
    typer.echo(
        f"coverage: analysis={quality.analysis_coverage:.0%} "
        f"mapping={quality.mapping_coverage:.0%} marks={quality.marks_coverage:.0%} "
        f"period={quality.questions_with_period}/{quality.questions}"
    )
    typer.echo(
        f"mapping: mapped={mapping.mapped_questions} ambiguous={mapping.ambiguous_questions} "
        f"unmapped={mapping.unmapped_questions} "
        f"not_analysed={mapping.not_analysed_questions} "
        f"unit_assignments={mapping.unit_assignments} multi_unit={mapping.multi_unit_questions}"
    )
    typer.echo(
        "types: "
        + (
            ", ".join(f"{row.label}={row.question_count}" for row in types.primary_labels)
            or "(none)"
        )
        + f" unclassified={types.unclassified_questions}"
    )
    typer.echo(
        "difficulty: "
        + (", ".join(f"{row.label}={row.question_count}" for row in difficulty.labels) or "(none)")
        + f" unknown={difficulty.unknown_questions}"
        + f" not_analysed={difficulty.not_analysed_questions}"
    )
    typer.echo(
        f"marks: stated={marks.questions_with_marks}/{marks.total_questions} "
        f"missing={marks.missing_marks} total={marks.total_marks:g} "
        f"mean={marks.mean_marks if marks.mean_marks is not None else '-'}"
    )
    typer.echo(
        f"repetition: exact_groups={repetition.exact_group_count} "
        f"exact_in_groups={repetition.exact_questions} "
        f"exact_extra={repetition.exact_repeated_appearances} "
        f"near={repetition.near_pairs} semantic={repetition.semantic_pairs} "
        f"concept_related={repetition.concept_related_pairs}"
    )
    typer.echo(
        f"trends: {trends.status.value} periods={len(trends.periods)} "
        f"changes={len(trends.changes)} "
        f"range={trends.first_period or '-'}..{trends.last_period or '-'}"
    )
    if report.topic_priority.topics:
        typer.echo("topic priority (historical prominence in this dataset):")
        for row in report.topic_priority.topics[:10]:
            typer.echo(
                f"  {row.rank:>2}. {row.topic} score={row.score:.3f} "
                f"questions={row.question_count} sessions={row.session_count} "
                f"papers={row.paper_count}"
            )
    if report.warnings:
        typer.echo("warnings:")
        for warning in report.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
    typer.echo("note: analytics describe the available dataset; they are not forecasts.")


def register_analytics_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help="Phase 4 question-paper analytics: frequency, marks, trends and topic priority."
    )

    @group.command("report")
    def report(
        course: list[str] | None = typer.Option(
            None, "--course", help="Course id; repeat for multiple separate reports."
        ),
        institution: str | None = typer.Option(None, "--institution"),
        syllabus_version: str | None = typer.Option(None, "--syllabus-version"),
        source: list[str] | None = typer.Option(
            None, "--source", help="Dataset source id; repeatable."
        ),
        document: list[str] | None = typer.Option(
            None, "--document", help="Document id; repeatable."
        ),
        unit: list[str] | None = typer.Option(None, "--unit", help="Syllabus unit id; repeatable."),
        topic: list[str] | None = typer.Option(None, "--topic", help="Syllabus topic; repeatable."),
        exam_type: list[str] | None = typer.Option(
            None, "--exam-type", help="Explicit examination type; repeatable."
        ),
        period: list[str] | None = typer.Option(
            None, "--period", help="Examination period/session; repeatable."
        ),
        question_type: list[QuestionType] | None = typer.Option(
            None, "--type", help="Primary question type; repeatable."
        ),
        difficulty: list[DifficultyLabel] | None = typer.Option(
            None, "--difficulty", help="Estimated difficulty; repeatable."
        ),
        marks_min: float | None = typer.Option(None, "--marks-min", min=0.0),
        marks_max: float | None = typer.Option(None, "--marks-max", min=0.0),
        similarity_threshold: float | None = typer.Option(
            None, "--similarity-threshold", min=0.0, max=1.0
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Build the full analytics report for one or more courses."""
        settings = get_settings()
        database = open_db()
        try:
            filters = _filters(
                institution=institution,
                syllabus_version=syllabus_version,
                source=source,
                document=document,
                unit=unit,
                topic=topic,
                exam_type=exam_type,
                period=period,
                question_type=question_type,
                difficulty=difficulty,
                marks_min=marks_min,
                marks_max=marks_max,
            )
            reports = _reports(database, settings, course, filters, similarity_threshold)
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            typer.echo(
                json.dumps(
                    [item.model_dump(mode="json") for item in reports],
                    indent=2,
                    default=str,
                )
            )
            return
        if not reports:
            typer.echo("no courses matched the analytics filters")
            return
        if len(reports) > 1:
            typer.echo(
                "note: multiple courses are reported separately; "
                "no cross-syllabus totals are pooled"
            )
        for index, item in enumerate(reports):
            if index:
                typer.echo("")
            _print_report(item)

    @group.command("repetitions")
    def repetitions(
        course: str = typer.Option(..., "--course"),
        kind: SimilarityKind | None = typer.Option(None, "--kind"),
        threshold: float | None = typer.Option(None, "--threshold", min=0.0, max=1.0),
        limit: int = typer.Option(50, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Inspect repeated text and stored similar-question evidence."""
        settings = get_settings()
        database = open_db()
        try:
            report_value = _reports(database, settings, [course], AnalyticsFilters(), threshold)[0]
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if kind is SimilarityKind.EXACT:
            groups = report_value.repetition.exact_groups[:limit]
            if as_json:
                typer.echo(
                    json.dumps([group.model_dump(mode="json") for group in groups], indent=2)
                )
                return
            _print_table(
                ["questions", "documents", "sessions", "text"],
                [
                    [
                        group.question_count,
                        group.document_count,
                        group.known_session_count,
                        _short(group.appearances[0].text, 70),
                    ]
                    for group in groups
                ],
            )
        else:
            pairs = [
                pair
                for pair in report_value.repetition.similarity_pairs
                if kind is None or pair.kind is kind
            ][:limit]
            if as_json:
                typer.echo(json.dumps([pair.model_dump(mode="json") for pair in pairs], indent=2))
                return
            _print_table(
                ["kind", "score", "scope", "session", "question", "matched"],
                [
                    [
                        pair.kind.value,
                        f"{pair.score:.3f}",
                        "same-doc" if pair.same_document else "cross-doc",
                        (
                            "same-session"
                            if pair.same_session is True
                            else "different-session"
                            if pair.same_session is False
                            else "-"
                        ),
                        _short(pair.left.text, 42),
                        _short(pair.right.text, 42),
                    ]
                    for pair in pairs
                ],
            )
        typer.echo("note: only exact repeated text is proof of identical wording.")

    @group.command("topics")
    def topics(
        course: str = typer.Option(..., "--course"),
        limit: int = typer.Option(20, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the evidence-based historical topic ranking."""
        database = open_db()
        try:
            report_value = _reports(database, get_settings(), [course], AnalyticsFilters())[0]
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        rows = report_value.topic_priority.topics[:limit]
        if as_json:
            typer.echo(json.dumps([row.model_dump(mode="json") for row in rows], indent=2))
            return
        _print_table(
            ["rank", "topic", "score", "questions", "papers", "sessions", "avg marks"],
            [
                [
                    row.rank,
                    _short(row.topic, 40),
                    f"{row.score:.3f}",
                    row.question_count,
                    row.paper_count,
                    row.session_count,
                    row.average_marks if row.average_marks is not None else "-",
                ]
                for row in rows
            ],
        )
        typer.echo(
            "descriptive only: historical prominence in the available dataset, not a forecast."
        )

    @group.command("trends")
    def trends(
        course: str = typer.Option(..., "--course"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show observed examination-period summaries and descriptive changes."""
        database = open_db()
        try:
            report_value = _reports(database, get_settings(), [course], AnalyticsFilters())[0]
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        value = report_value.trends
        if as_json:
            typer.echo(json.dumps(value.model_dump(mode="json"), indent=2, default=str))
            return
        typer.echo(f"status={value.status.value} available={value.available}")
        if value.reason:
            typer.echo(f"reason: {value.reason}")
        _print_table(
            ["period", "source", "questions", "docs", "sessions", "marks", "low sample"],
            [
                [
                    row.period,
                    row.source,
                    row.question_count,
                    row.document_count,
                    row.session_count,
                    f"{row.total_marks:g}",
                    "yes" if row.low_sample else "no",
                ]
                for row in value.periods
            ],
        )
        if value.changes:
            typer.echo("changes:")
            for change in value.changes[:40]:
                typer.echo(
                    f"  {change.metric} {change.label}: {change.previous_value:g} -> "
                    f"{change.current_value:g} ({change.delta:+g}) "
                    f"{'reliable' if change.reliable else 'low-sample'}"
                )
        if value.new_topics:
            typer.echo(
                "newly observed topics: "
                + ", ".join(f"{item.topic} ({item.period})" for item in value.new_topics[:20])
            )
        for warning in value.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        for note in value.notes:
            typer.echo(f"note: {note}")

    @group.command("status")
    def status(as_json: bool = typer.Option(False, "--json")) -> None:
        """Show whether questions have Phase 3 analysis before analytics."""
        database = open_db()
        try:
            rows = AnalyticsService(database, get_settings().analytics).status()
        finally:
            database.close()
        if as_json:
            typer.echo(json.dumps(rows, indent=2, default=str))
            return
        _print_table(
            ["course_id", "questions", "analysed", "mapped", "ambiguous", "unmapped", "similar"],
            [
                [
                    row["course_id"],
                    row["questions"],
                    row["analysed"],
                    row["mapped"],
                    row["ambiguous"],
                    row["unmapped"],
                    row["similarities"],
                ]
                for row in rows
            ],
        )

    app.add_typer(group, name="analytics")


__all__ = ["register_analytics_commands"]
