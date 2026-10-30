"""``exam-engine understanding`` - run and inspect Phase 3 analysis.

Registered from :mod:`exam_engine.cli` with the shared database opener so the
sub-app reuses ``--db`` handling without importing the parent module.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, cast

import typer

from exam_engine.config import Settings
from exam_engine.models.enums import (
    MappingStatus,
    SimilarityKind,
    UnderstandingStatus,
)
from exam_engine.storage.db import Database
from exam_engine.storage.question_repositories import (
    QuestionUnderstandingRepository,
    UnderstandingRunRepository,
)
from exam_engine.storage.repositories import QuestionRepository
from exam_engine.understanding.service import UnderstandingService

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


def _short(value: str, limit: int = 68) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _service(database: Database, settings: Settings) -> UnderstandingService:
    return UnderstandingService(database, settings.understanding)


def _question_map(database: Database, course_id: str) -> dict[str, Any]:
    with database.session() as session:
        return {
            question.question_id: question
            for question in QuestionRepository(session).list_by_course(course_id)
        }


def register_understanding_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help="Phase 3 question understanding: structure, syllabus mapping and classification."
    )

    @group.command("analyze")
    def analyze(
        course: str | None = typer.Option(None, "--course", help="Course id (default: all)."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Analyse stored questions for one course or every course."""
        database = open_db()
        try:
            reports = _service(database, get_settings()).analyze(course)
        finally:
            database.close()
        failed = any(report.status is UnderstandingStatus.FAILED for report in reports)
        if as_json:
            typer.echo(
                json.dumps(
                    [report.model_dump(mode="json") for report in reports],
                    indent=2,
                    default=str,
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
                f"questions={report.totals.get('questions', 0)} "
                f"analysed={report.totals.get('analysed', 0)} "
                f"mapped={report.totals.get('mapped', 0)} "
                f"ambiguous={report.totals.get('ambiguous', 0)} "
                f"unmapped={report.totals.get('unmapped', 0)} "
                f"similarities={report.totals.get('similarities', 0)} "
                f"({report.duration_ms} ms)"
            )
            if report.model_ids:
                typer.echo(f"  embeddings: {', '.join(report.model_ids)}")
            typer.echo(f"  taxonomy: {report.config.get('taxonomy_version', 'unknown')}")
            for warning in report.warnings:
                typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
            for error in report.errors:
                typer.echo(f"  error: {error.code}: {error.message}", err=True)
        if failed:
            raise typer.Exit(code=1)

    @group.command("status")
    def status(as_json: bool = typer.Option(False, "--json")) -> None:
        """Show derived question-understanding coverage per course."""
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
                "questions",
                "analysed",
                "mapped",
                "ambiguous",
                "unmapped",
                "types",
                "bloom",
                "similar",
                "last_run",
                "last_status",
            ],
            [
                [
                    row["course_id"],
                    row["questions"],
                    row["analyzed"],
                    row["mapped"],
                    row["ambiguous"],
                    row["unmapped"],
                    row["types"],
                    row["bloom"],
                    row["similarities"],
                    row["last_run"] or "-",
                    row["last_status"] or "-",
                ]
                for row in rows
            ],
        )

    @group.command("questions")
    def questions(
        course: str = typer.Option(..., "--course"),
        mapping_status: MappingStatus | None = typer.Option(
            None, "--status", help="Filter by mapping outcome."
        ),
        limit: int = typer.Option(50, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List analysed questions with their primary labels."""
        database = open_db()
        try:
            with database.session() as session:
                analysis_repo = QuestionUnderstandingRepository(session)
                analyses = analysis_repo.list_analyses(
                    course, mapping_status=mapping_status, limit=limit
                )
                question_map = {
                    question.question_id: question
                    for question in QuestionRepository(session).list_by_course(course)
                }
                payload: list[dict[str, Any]] = []
                for analysis in analyses:
                    question = question_map.get(analysis.question_id)
                    matches = analysis_repo.list_unit_matches(analysis.question_id)
                    payload.append(
                        {
                            "analysis": analysis.model_dump(mode="json"),
                            "question": (question.model_dump(mode="json") if question else None),
                            "unit_matches": [match.model_dump(mode="json") for match in matches],
                        }
                    )
        finally:
            database.close()
        if as_json:
            typer.echo(json.dumps(payload, indent=2, default=str))
            return
        rows = []
        for item in payload:
            analysis_data = cast(dict[str, Any], item["analysis"])
            raw_question = item.get("question")
            question_data = raw_question if isinstance(raw_question, dict) else {}
            raw_matches = item.get("unit_matches")
            top = (
                cast(list[dict[str, Any]], raw_matches)[0]
                if isinstance(raw_matches, list) and raw_matches
                else {}
            )
            rows.append(
                [
                    _short(question_data.get("question_number") or "-", 8),
                    _short(question_data.get("text") or "", 52),
                    analysis_data["mapping_status"],
                    _short(
                        " / ".join(
                            part for part in (top.get("unit_label"), top.get("topic")) if part
                        )
                        or "-",
                        20,
                    ),
                    analysis_data["primary_type"] or "-",
                    analysis_data["bloom_primary"] or "-",
                    analysis_data["difficulty_label"],
                ]
            )
        _print_table(
            ["no", "question", "mapping", "unit/topic", "type", "bloom", "difficulty"],
            rows,
        )

    @group.command("show")
    def show(
        question_id: str = typer.Argument(...),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show everything recorded for one question, including evidence."""
        database = open_db()
        try:
            with database.session() as session:
                question = QuestionRepository(session).get(question_id)
                if question is None:
                    typer.echo(f"unknown question: {question_id}", err=True)
                    raise typer.Exit(code=1)
                repo = QuestionUnderstandingRepository(session)
                analysis = repo.get_analysis(question_id)
                if analysis is None:
                    typer.echo(
                        f"no Phase 3 analysis for {question_id}; run 'understanding analyze'",
                        err=True,
                    )
                    raise typer.Exit(code=1)
                matches = repo.list_unit_matches(question_id)
                types = repo.list_types(question_id)
                bloom = repo.list_bloom(question_id)
                difficulty = repo.get_difficulty(question_id)
                similarities = repo.list_similarities(question.course_id, question_id=question_id)
                related_ids = {
                    match.matched_question_id
                    if match.question_id == question_id
                    else match.question_id
                    for match in similarities
                }
                related = {
                    related_id: repo_question
                    for related_id in related_ids
                    if (repo_question := QuestionRepository(session).get(related_id)) is not None
                }
                payload: dict[str, Any] = {
                    "question": question.model_dump(mode="json"),
                    "analysis": analysis.model_dump(mode="json"),
                    "unit_matches": [match.model_dump(mode="json") for match in matches],
                    "types": [label.model_dump(mode="json") for label in types],
                    "bloom": [label.model_dump(mode="json") for label in bloom],
                    "difficulty": (difficulty.model_dump(mode="json") if difficulty else None),
                    "similar": [match.model_dump(mode="json") for match in similarities],
                    "related_questions": {
                        related_id: related_question.model_dump(mode="json")
                        for related_id, related_question in related.items()
                    },
                }
        finally:
            database.close()

        if as_json:
            typer.echo(json.dumps(payload, indent=2, default=str))
            return

        question_data: dict[str, Any] = payload["question"]
        analysis_data: dict[str, Any] = payload["analysis"]
        typer.echo(
            f"{question_id}  [{question_data['course_id']}]  "
            f"document={question_data['document_id']}"
        )
        marks = question_data.get("marks")
        typer.echo(
            f"number={question_data.get('question_number') or '-'} "
            f"section={question_data.get('section') or '-'} "
            f"unit={question_data.get('unit') or '-'} "
            f"marks={marks if marks is not None else '-'} "
            f"page={question_data.get('page_number') or '-'} "
            f"extraction_confidence={question_data.get('confidence')}"
        )
        typer.echo(f"text: {_short(question_data['text'], 240)}")
        references = ", ".join(
            reference["kind"] for reference in analysis_data.get("references", [])
        )
        typer.echo(
            f"structure: words={analysis_data['word_count']} "
            f"sentences={analysis_data['sentence_count']} "
            f"subquestions={analysis_data['subquestion_count']} "
            f"references={references or '-'}"
        )
        for subpart in analysis_data.get("subparts", []):
            typer.echo(f"  ({subpart['label']}) {_short(subpart['text'], 100)}")
        typer.echo(
            f"mapping: {analysis_data['mapping_status']} "
            f"confidence={analysis_data['mapping_confidence']:.2f}"
        )
        for match in payload["unit_matches"]:
            topic = f" / {match['topic']}" if match.get("topic") else ""
            typer.echo(
                f"  {match['unit_label']}{topic}: {match['score']:.3f}  "
                f"syllabus={match['syllabus_id']}"
            )
            for evidence in match["evidence"]:
                typer.echo(f"    - {evidence}")
        typer.echo(
            "types: "
            + (
                ", ".join(f"{label['label']} {label['score']:.2f}" for label in payload["types"])
                or "unknown"
            )
        )
        typer.echo(
            "bloom: "
            + (
                ", ".join(f"{label['level']} {label['score']:.2f}" for label in payload["bloom"])
                or "unknown"
            )
        )
        difficulty_data = payload["difficulty"]
        if difficulty_data:
            typer.echo(
                f"difficulty: {difficulty_data['label']} {difficulty_data['score']:.2f} "
                f"(confidence {difficulty_data['confidence']:.2f}, estimated="
                f"{difficulty_data['estimated']})"
            )
            for line in difficulty_data["explanation"]:
                typer.echo(f"  - {line}")
        else:
            typer.echo("difficulty: unknown (no record)")
        if payload["similar"]:
            typer.echo("similar questions:")
            for match in payload["similar"]:
                other = (
                    match["matched_question_id"]
                    if match["question_id"] == question_id
                    else match["question_id"]
                )
                other_question = payload["related_questions"].get(other, {})
                typer.echo(
                    f"  {match['kind']} {match['score']:.3f} {other} "
                    f"{_short(other_question.get('text') or '', 70)}"
                )
        if analysis_data.get("uncertainties"):
            typer.echo("uncertainties:")
            for item in analysis_data["uncertainties"]:
                typer.echo(f"  {item['severity']}: {item['code']}: {item['message']}")

    @group.command("similar")
    def similar(
        course: str = typer.Option(..., "--course"),
        kind: SimilarityKind | None = typer.Option(None, "--kind"),
        threshold: float | None = typer.Option(
            None, "--threshold", min=0.0, max=1.0, help="Minimum stored score."
        ),
        limit: int = typer.Option(50, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List stored question relationships for a course."""
        database = open_db()
        try:
            with database.session() as session:
                matches = QuestionUnderstandingRepository(session).list_similarities(
                    course,
                    kind=kind,
                    min_score=threshold,
                    limit=limit,
                )
            question_map = _question_map(database, course)
        finally:
            database.close()
        if as_json:
            typer.echo(json.dumps([match.model_dump(mode="json") for match in matches], indent=2))
            return
        rows = []
        for match in matches:
            left = question_map.get(match.question_id)
            right = question_map.get(match.matched_question_id)
            rows.append(
                [
                    match.kind.value,
                    f"{match.score:.3f}",
                    f"{match.lexical_score:.3f}",
                    f"{match.semantic_score:.3f}",
                    _short(left.text if left else match.question_id, 38),
                    _short(right.text if right else match.matched_question_id, 38),
                ]
            )
        _print_table(["kind", "score", "lexical", "semantic", "question", "matched"], rows)
        typer.echo("note: similarity is evidence, not proof of duplication; only 'exact' is exact")

    @group.command("runs")
    def runs(
        limit: int = typer.Option(20, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List recent Phase 3 runs, including failures."""
        database = open_db()
        try:
            with database.session() as session:
                rows = UnderstandingRunRepository(session).list_runs(limit)
        finally:
            database.close()
        if as_json:
            typer.echo(json.dumps(rows, indent=2, default=str))
            return
        _print_table(
            ["run_id", "course", "status", "started", "analysed", "mapped", "similar"],
            [
                [
                    _short(row["run_id"], 28),
                    row["course_id"] or "-",
                    row["status"],
                    _short(row["started_at"], 20),
                    row["totals"].get("analysed", 0),
                    row["totals"].get("mapped", 0),
                    row["totals"].get("similarities", 0),
                ]
                for row in rows
            ],
        )

    @group.command("taxonomy")
    def taxonomy(as_json: bool = typer.Option(False, "--json")) -> None:
        """Show the active label definitions and cue weights."""
        database = open_db()
        try:
            taxonomy_record = UnderstandingService(database, get_settings().understanding).taxonomy
        finally:
            database.close()
        if as_json:
            from exam_engine.understanding.taxonomy import taxonomy_as_dict

            typer.echo(json.dumps(taxonomy_as_dict(taxonomy_record), indent=2))
            return
        typer.echo(f"taxonomy {taxonomy_record.version} (source: {taxonomy_record.source})")
        for section, definitions in (
            ("question types", taxonomy_record.question_types),
            ("Bloom levels", taxonomy_record.bloom_levels),
        ):
            typer.echo(f"\n{section}:")
            for label, definition in definitions.items():
                typer.echo(f"  {label}: {definition.description}")
                for cue in definition.cues:
                    note = f"  # {cue.note}" if cue.note else ""
                    typer.echo(f"    {cue.weight:.2f}  {cue.pattern}{note}")

    @group.command("reset")
    def reset(
        course: str | None = typer.Option(None, "--course"),
        all_courses: bool = typer.Option(False, "--all", help="Reset every course."),
        yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    ) -> None:
        """Delete derived Phase 3 rows; Phase 1 and Phase 2 data stay."""
        if not course and not all_courses:
            typer.echo("choose --course <id> or --all", err=True)
            raise typer.Exit(code=2)
        if not yes:
            typer.confirm(
                "Delete derived question-understanding data for this selection?",
                abort=True,
            )
        database = open_db()
        try:
            _service(database, get_settings()).reset(None if all_courses else course)
        finally:
            database.close()
        typer.echo("derived question-understanding data removed")

    app.add_typer(group, name="understanding")


__all__ = ["register_understanding_commands"]
