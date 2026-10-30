"""``exam-engine exam-dna`` - Phase 8 historical examination patterns.

The command layer is deliberately thin. Scope resolution, pattern detection and
denominators live in :class:`ExamDnaService` so another consumer can reuse it
without the CLI.
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
from exam_engine.exam_dna.service import ExamDnaError, ExamDnaService
from exam_engine.models.enums import PatternCategory, QuestionGroupKind
from exam_engine.models.exam_dna import (
    ExamDnaFilters,
    ExamDnaPattern,
    ExamDnaProfile,
    ExamDnaQuestionGroup,
    ExamDnaRequest,
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
    typer.echo("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


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


def register_exam_dna_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help="Phase 8 historical Exam DNA: distributions, recurrence and repeated questions."
    )

    def _date(value: str | None, option: str) -> date | None:
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ExamDnaError(
                "invalid_exam_dna_filter",
                f"{option} must be an ISO date (YYYY-MM-DD): {value!r}",
                detail={"option": option, "value": value},
            ) from exc

    def _request(
        course: str,
        syllabus: str | None,
        documents: list[str] | None,
        sources: list[str] | None,
        exam_types: list[str] | None,
        periods: list[str] | None,
        date_from: str | None,
        date_to: str | None,
    ) -> ExamDnaRequest:
        try:
            filters = ExamDnaFilters(
                document_ids=documents or [],
                source_ids=sources or [],
                exam_types=exam_types or [],
                exam_periods=periods or [],
                date_from=_date(date_from, "--date-from"),
                date_to=_date(date_to, "--date-to"),
            )
        except ValueError as exc:
            raise ExamDnaError("invalid_exam_dna_filter", str(exc)) from exc
        return ExamDnaRequest(course_id=course, syllabus_id=syllabus, filters=filters)

    def _service(database: Database) -> ExamDnaService:
        settings = get_settings()
        return ExamDnaService(
            database,
            settings.exam_dna,
            coverage_settings=settings.coverage,
        )

    def _fail(exc: Exception, *, as_json: bool) -> None:
        if isinstance(exc, ExamDnaError):
            payload = exc.as_dict()
        elif isinstance(exc, KeyError):
            payload = {"code": "not_found", "message": str(exc).strip("'"), "detail": {}}
        else:
            payload = {"code": "exam_dna_error", "message": str(exc), "detail": {}}
        if as_json:
            _dump({"error": payload})
        else:
            typer.echo(f"ERROR: [{payload['code']}] {payload['message']}", err=True)
            if payload.get("detail"):
                typer.echo("detail: " + json.dumps(payload["detail"], sort_keys=True), err=True)
        raise typer.Exit(code=1) from exc

    def _write_export(profile: ExamDnaProfile, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(profile.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            typer.echo(f"ERROR: [exam_dna_export_failed] could not write {path}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    def _print_profile(profile: ExamDnaProfile) -> None:
        summary = profile.summary
        typer.echo(
            f"course={profile.course.course_id} syllabus={profile.syllabus.syllabus_id} "
            f"status={profile.status.value} profile={profile.profile_id}"
        )
        _print_table(
            [
                "papers",
                "eligible",
                "excluded",
                "questions",
                "with marks",
                "marks",
                "sessions",
                "patterns",
                "recurring",
            ],
            [
                [
                    summary.total_papers,
                    summary.eligible_papers,
                    summary.excluded_papers,
                    summary.questions,
                    summary.questions_with_marks,
                    f"{summary.total_known_marks:g}",
                    summary.known_sessions,
                    summary.pattern_count,
                    summary.recurring_pattern_count,
                ]
            ],
        )
        typer.echo(
            "question types: "
            + ", ".join(
                f"{row.label} {row.question_count}" for row in profile.distributions.question_types
            )
        )
        typer.echo(
            "bloom: "
            + ", ".join(
                f"{row.label} {row.question_count}" for row in profile.distributions.bloom_levels
            )
        )
        typer.echo(
            f"coverage: status={profile.coverage.status.value} "
            f"concepts={profile.coverage.total_concepts} "
            f"covered={profile.coverage.covered_concepts} "
            f"unmapped={profile.coverage.unmapped_concepts} "
            f"uncertain={profile.coverage.uncertain_concepts} "
            f"report={profile.coverage.report_id}"
        )
        typer.echo(f"scope: {profile.scope.scope_statement}")
        if profile.sessions:
            typer.echo(
                f"session series: {len(profile.sessions)} dated session(s), "
                f"{len(profile.trends)} descriptive trend row(s)"
            )
        recurring = [pattern for pattern in profile.patterns if pattern.is_recurring][:10]
        if recurring:
            _print_table(
                ["category", "pattern", "papers", "rate", "sessions"],
                [
                    [
                        pattern.category.value,
                        _short(pattern.label, 40),
                        f"{pattern.papers_observed}/{pattern.eligible_papers}",
                        _percent(pattern.recurrence_rate),
                        f"{pattern.sessions_observed}/{pattern.eligible_sessions}",
                    ]
                    for pattern in recurring
                ],
            )
        for warning in profile.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        typer.echo(
            "note: recurrence is historical observation over the analyzed papers, "
            "not a forecast of future examinations."
        )

    def _print_patterns(patterns: list[ExamDnaPattern], profile: ExamDnaProfile) -> None:
        typer.echo(f"eligible papers={profile.summary.eligible_papers} patterns={len(patterns)}")
        _print_table(
            ["category", "pattern", "papers", "rate", "sessions", "quality", "recurring"],
            [
                [
                    item.category.value,
                    _short(item.label, 38),
                    f"{item.papers_observed}/{item.eligible_papers}",
                    _percent(item.recurrence_rate),
                    f"{item.sessions_observed}/{item.eligible_sessions}",
                    item.evidence_quality.value,
                    "yes" if item.is_recurring else "no",
                ]
                for item in patterns
            ],
        )

    def _print_groups(groups: list[ExamDnaQuestionGroup]) -> None:
        _print_table(
            ["kind", "questions", "papers", "score", "shared concepts", "template"],
            [
                [
                    group.kind.value,
                    group.question_count,
                    group.document_count,
                    "-" if group.score is None else f"{group.score:.2f}",
                    _short(", ".join(group.shared_concepts) or "-", 34),
                    "yes" if group.template_variant else "no",
                ]
                for group in groups
            ],
        )

    @group.command("profile")
    def profile(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(
            None, "--document", help="Paper document id; repeatable."
        ),
        sources: list[str] | None = typer.Option(
            None, "--source", help="Dataset source id; repeatable."
        ),
        exam_types: list[str] | None = typer.Option(
            None, "--exam-type", help="Examination type such as final or midterm."
        ),
        periods: list[str] | None = typer.Option(
            None, "--exam-period", help="Resolved examination period label."
        ),
        date_from: str | None = typer.Option(
            None, "--date-from", help="ISO examination date lower bound."
        ),
        date_to: str | None = typer.Option(
            None, "--date-to", help="ISO examination date upper bound."
        ),
        output: Path | None = typer.Option(
            None, "--output", help="Write the full JSON profile to this path."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Build an evidence-backed historical Exam DNA profile for one course."""
        database = open_db()
        try:
            value = _service(database).analyze(
                _request(
                    course,
                    syllabus,
                    documents,
                    sources,
                    exam_types,
                    periods,
                    date_from,
                    date_to,
                )
            )
        except (ExamDnaError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if output is not None:
            _write_export(value, output)
        if as_json:
            _dump(value.model_dump(mode="json"))
        else:
            _print_profile(value)
            if output is not None:
                typer.echo(f"wrote {output}")

    @group.command("patterns")
    def patterns(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(None, "--document"),
        sources: list[str] | None = typer.Option(None, "--source"),
        exam_types: list[str] | None = typer.Option(None, "--exam-type"),
        periods: list[str] | None = typer.Option(None, "--exam-period"),
        date_from: str | None = typer.Option(None, "--date-from"),
        date_to: str | None = typer.Option(None, "--date-to"),
        category: PatternCategory | None = typer.Option(None, "--category"),
        recurring: bool = typer.Option(
            False, "--recurring", help="Only patterns that clear the recurrence threshold."
        ),
        limit: int = typer.Option(100, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List observed patterns with their paper and session denominators."""
        database = open_db()
        try:
            request = _request(
                course,
                syllabus,
                documents,
                sources,
                exam_types,
                periods,
                date_from,
                date_to,
            )
            service = _service(database)
            value = service.analyze(request)
            rows = [
                item
                for item in value.patterns
                if (category is None or item.category is category)
                and (not recurring or item.is_recurring)
            ][:limit]
        except (ExamDnaError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_patterns(rows, value)

    @group.command("questions")
    def questions(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(None, "--document"),
        sources: list[str] | None = typer.Option(None, "--source"),
        exam_types: list[str] | None = typer.Option(None, "--exam-type"),
        periods: list[str] | None = typer.Option(None, "--exam-period"),
        date_from: str | None = typer.Option(None, "--date-from"),
        date_to: str | None = typer.Option(None, "--date-to"),
        kind: QuestionGroupKind | None = typer.Option(None, "--kind"),
        limit: int = typer.Option(50, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List exact, near-duplicate, semantic and concept-related question groups."""
        database = open_db()
        try:
            request = _request(
                course,
                syllabus,
                documents,
                sources,
                exam_types,
                periods,
                date_from,
                date_to,
            )
            value = _service(database).question_groups(request, kind=kind)
        except (ExamDnaError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        rows = value[:limit]
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_groups(rows)
        typer.echo(
            "note: only 'exact' proves identical wording; other kinds are review "
            "candidates with stored similarity evidence."
        )

    @group.command("papers")
    def papers(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(None, "--document"),
        sources: list[str] | None = typer.Option(None, "--source"),
        exam_types: list[str] | None = typer.Option(None, "--exam-type"),
        periods: list[str] | None = typer.Option(None, "--exam-period"),
        date_from: str | None = typer.Option(None, "--date-from"),
        date_to: str | None = typer.Option(None, "--date-to"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the paper inventory, including duplicate copies and exclusions."""
        database = open_db()
        try:
            value = _service(database).analyze(
                _request(
                    course,
                    syllabus,
                    documents,
                    sources,
                    exam_types,
                    periods,
                    date_from,
                    date_to,
                )
            )
        except (ExamDnaError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if as_json:
            _dump([item.model_dump(mode="json") for item in value.papers])
            return
        _print_table(
            ["document", "period", "session", "type", "questions", "marks", "eligible", "note"],
            [
                [
                    _short(item.title, 30),
                    item.exam_period,
                    item.exam_session or "-",
                    item.exam_type or "-",
                    item.question_count,
                    f"{item.total_known_marks:g}" if item.questions_with_marks else "-",
                    "yes" if item.eligible else "no",
                    item.exclusion_reason.value if item.exclusion_reason else "-",
                ]
                for item in value.papers
            ],
        )

    app.add_typer(group, name="exam-dna")


__all__ = ["register_exam_dna_commands"]
