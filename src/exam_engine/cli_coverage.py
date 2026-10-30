"""``exam-engine coverage`` - Phase 7 exam coverage gap reporting.

The command layer is deliberately thin. Filtering, classification and
provenance live in :class:`CoverageService` so a dashboard or another agent can
reuse it without the CLI.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import typer

from exam_engine.config import Settings
from exam_engine.coverage.service import CoverageError, CoverageService
from exam_engine.models.coverage import (
    ConceptCoverageFinding,
    CoverageFilters,
    CoverageReport,
    CoverageRequest,
)
from exam_engine.models.enums import CoverageStatus, QuestionType
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


def _short(value: str, limit: int = 48) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _dump(value: Any) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n"
    # Machine-readable coverage output is UTF-8 regardless of the terminal's
    # legacy code page (Windows redirects often default to cp1252).
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        typer.echo(text, nl=False)
        return
    stream.write(text.encode("utf-8"))
    stream.flush()


def register_coverage_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help="Phase 7 evidence-backed syllabus coverage, unit summaries and unmapped questions."
    )

    def _request(
        course: str,
        syllabus: str | None,
        documents: list[str] | None,
        sources: list[str] | None,
        units: list[str] | None,
        question_types: list[QuestionType] | None,
        periods: list[str] | None,
    ) -> CoverageRequest:
        return CoverageRequest(
            course_id=course,
            syllabus_id=syllabus,
            filters=CoverageFilters(
                document_ids=documents or [],
                source_ids=sources or [],
                unit_ids=units or [],
                question_types=question_types or [],
                exam_periods=periods or [],
            ),
        )

    def _service(database: Database) -> CoverageService:
        settings = get_settings()
        return CoverageService(database, settings.coverage)

    def _fail(exc: Exception, *, as_json: bool) -> None:
        if isinstance(exc, CoverageError):
            payload = exc.as_dict()
        elif isinstance(exc, KeyError):
            payload = {"code": "not_found", "message": str(exc).strip("'"), "detail": {}}
        else:
            payload = {"code": "coverage_error", "message": str(exc), "detail": {}}
        if as_json:
            _dump({"error": payload})
        else:
            typer.echo(f"ERROR: [{payload['code']}] {payload['message']}", err=True)
            if payload.get("detail"):
                typer.echo(
                    "detail: " + json.dumps(payload["detail"], sort_keys=True),
                    err=True,
                )
        raise typer.Exit(code=1) from exc

    def _write_export(report: CoverageReport, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            typer.echo(
                f"ERROR: [coverage_export_failed] could not write {path}: {exc}",
                err=True,
            )
            raise typer.Exit(code=1) from exc

    def _print_summary(report: CoverageReport) -> None:
        summary = report.summary
        typer.echo(
            f"course={report.course.course_id} syllabus={report.syllabus.syllabus_id} "
            f"status={report.status.value} report={report.report_id}"
        )
        typer.echo(
            f"concepts={summary.total_concepts} covered={summary.covered_concepts} "
            f"low={summary.low_coverage_concepts} unmapped={summary.unmapped_concepts} "
            f"uncertain={summary.uncertain_concepts} "
            f"insufficient={summary.insufficient_data_concepts}"
        )
        _print_table(
            [
                "questions",
                "analysed",
                "mapped",
                "unmapped",
                "analysis",
                "mapped rate",
                "concept coverage",
                "mapped marks",
            ],
            [
                [
                    summary.total_questions,
                    summary.analysed_questions,
                    summary.mapped_questions,
                    summary.unmapped_questions,
                    _percent(summary.analysis_coverage_rate),
                    _percent(summary.mapped_question_rate),
                    _percent(summary.concept_coverage_rate),
                    (
                        f"{summary.mapped_marks:g}/{summary.total_known_marks:g}"
                        if summary.questions_with_marks
                        else "no marks"
                    ),
                ]
            ],
        )
        for warning in report.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")

    @group.command("report")
    def report(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(
            None, "--document", help="Document id; repeatable."
        ),
        sources: list[str] | None = typer.Option(
            None, "--source", help="Dataset source id; repeatable."
        ),
        units: list[str] | None = typer.Option(
            None, "--unit", help="Syllabus unit id; repeatable."
        ),
        question_types: list[QuestionType] | None = typer.Option(None, "--question-type"),
        periods: list[str] | None = typer.Option(
            None, "--exam-period", help="Resolved exam period label."
        ),
        output: Path | None = typer.Option(
            None, "--output", help="Write the full JSON report to this path."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Generate an evidence-backed coverage report for one course and syllabus."""
        database = open_db()
        try:
            value = _service(database).analyze(
                _request(course, syllabus, documents, sources, units, question_types, periods)
            )
        except (CoverageError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if output is not None:
            _write_export(value, output)
        if as_json:
            _dump(value.model_dump(mode="json"))
        else:
            _print_summary(value)
            if output is not None:
                typer.echo(f"wrote {output}")

    @group.command("concepts")
    def concepts(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(None, "--document"),
        sources: list[str] | None = typer.Option(None, "--source"),
        units: list[str] | None = typer.Option(None, "--unit"),
        question_types: list[QuestionType] | None = typer.Option(None, "--question-type"),
        periods: list[str] | None = typer.Option(None, "--exam-period"),
        status: CoverageStatus | None = typer.Option(None, "--status"),
        limit: int = typer.Option(50, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List concept-level coverage findings with reasons and candidates."""
        database = open_db()
        try:
            value = _service(database).analyze(
                _request(course, syllabus, documents, sources, units, question_types, periods)
            )
        except (CoverageError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        rows = [item for item in value.concepts if status is None or item.status is status]
        rows = rows[:limit]
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_table(
            ["status", "concept", "units", "qs", "docs", "sessions", "marks", "conf", "reason"],
            [
                [
                    item.status.value,
                    _short(item.label, 42),
                    ",".join(item.unit_labels) or "-",
                    item.distinct_question_count,
                    item.document_count,
                    item.session_count,
                    f"{item.mapped_marks:g}" if item.questions_with_marks else "-",
                    _percent(item.average_mapping_confidence),
                    _short(item.reasons[0] if item.reasons else "-", 56),
                ]
                for item in rows
            ],
        )
        typer.echo(
            f"note: coverage describes the available corpus only; candidates are not "
            f"treated as coverage (report status={value.status.value})"
        )

    @group.command("concept")
    def concept(
        concept: str = typer.Option(..., "--concept", help="Concept id or normalized label."),
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(None, "--document"),
        sources: list[str] | None = typer.Option(None, "--source"),
        units: list[str] | None = typer.Option(None, "--unit"),
        question_types: list[QuestionType] | None = typer.Option(None, "--question-type"),
        periods: list[str] | None = typer.Option(None, "--exam-period"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show one concept's evidence, reasons and rejected mapping candidates."""
        database = open_db()
        try:
            value = _service(database).concept(
                _request(course, syllabus, documents, sources, units, question_types, periods),
                concept,
            )
        except (CoverageError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if as_json:
            _dump(value.model_dump(mode="json"))
            return
        _print_concept(value)

    @group.command("units")
    def units(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(None, "--document"),
        sources: list[str] | None = typer.Option(None, "--source"),
        unit: list[str] | None = typer.Option(None, "--unit"),
        question_types: list[QuestionType] | None = typer.Option(None, "--question-type"),
        periods: list[str] | None = typer.Option(None, "--exam-period"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show unit-level coverage and the concepts needing attention."""
        database = open_db()
        try:
            value = _service(database).analyze(
                _request(course, syllabus, documents, sources, unit, question_types, periods)
            )
        except (CoverageError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        if as_json:
            _dump([item.model_dump(mode="json") for item in value.units])
            return
        _print_table(
            ["unit", "concepts", "covered", "low", "unmapped", "uncertain", "qs", "coverage"],
            [
                [
                    _short(item.unit_label, 28),
                    item.concept_count,
                    item.status_counts.get(CoverageStatus.COVERED.value, 0),
                    item.status_counts.get(CoverageStatus.LOW_COVERAGE.value, 0),
                    item.status_counts.get(CoverageStatus.UNMAPPED.value, 0),
                    item.status_counts.get(CoverageStatus.UNCERTAIN.value, 0),
                    item.distinct_question_count,
                    _percent(item.concept_coverage_rate),
                ]
                for item in value.units
            ],
        )
        for item in value.units:
            if item.needs_attention:
                typer.echo(f"{_short(item.unit_label, 28)}: " + ", ".join(item.needs_attention))

    @group.command("unmapped")
    def unmapped(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        documents: list[str] | None = typer.Option(None, "--document"),
        sources: list[str] | None = typer.Option(None, "--source"),
        units: list[str] | None = typer.Option(None, "--unit"),
        question_types: list[QuestionType] | None = typer.Option(None, "--question-type"),
        periods: list[str] | None = typer.Option(None, "--exam-period"),
        limit: int = typer.Option(50, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List questions without a confident mapping; they are not labelled out-of-syllabus."""
        database = open_db()
        try:
            value = _service(database).unmapped(
                _request(course, syllabus, documents, sources, units, question_types, periods)
            )
        except (CoverageError, KeyError, ValueError) as exc:
            _fail(exc, as_json=as_json)
        finally:
            database.close()
        rows = value[:limit]
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_table(
            ["question", "document", "status", "reason", "mappings", "excerpt"],
            [
                [
                    item.question_number or item.question_id[-8:],
                    _short(item.document_title, 30),
                    item.analysis_status.value if item.analysis_status else "not_analysed",
                    item.reason,
                    len(item.existing_mappings),
                    _short(item.text_excerpt, 56),
                ]
                for item in rows
            ],
        )
        typer.echo(
            "note: an unmapped question is unresolved relative to this syllabus version; "
            "it is not automatically out-of-syllabus."
        )

    app.add_typer(group, name="coverage")


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _print_concept(item: ConceptCoverageFinding) -> None:
    typer.echo(f"{item.status.value}  {item.label}  [{item.concept_id}]")
    typer.echo(
        f"units={', '.join(item.unit_labels) or '-'} "
        f"questions={item.distinct_question_count} documents={item.document_count} "
        f"sessions={item.session_count} marks={item.mapped_marks:g} "
        f"confidence={_percent(item.average_mapping_confidence)}"
    )
    for reason in item.reasons:
        typer.echo(f"why: {reason}")
    for question in item.supporting_questions:
        typer.echo(
            f"  mapped: {question.question_id} {question.document_title} "
            f"[{question.mapping_method} {question.mapping_score}]"
        )
        for evidence in question.evidence:
            typer.echo(f"    evidence: {evidence}")
    for candidate in item.candidate_mappings:
        typer.echo(
            f"  candidate ({candidate.reason}): {candidate.question_id} "
            f"{_short(candidate.topic or '-', 40)} score={candidate.score} "
            f"unit={candidate.unit_label or '-'}"
        )
    for warning in item.warnings:
        typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
    for limitation in item.limitations:
        typer.echo(f"  limitation: {limitation}")


__all__ = ["register_coverage_commands"]
