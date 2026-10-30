"""``exam-engine advanced`` - Phase 11 mutation, adversarial and misconception tools.

The command layer only parses input and prints results: every decision lives in
:class:`AdvancedAssessmentService`, so the same operations are available to other
callers. Review commands never approve anything automatically, and exported reports
carry the same "candidate, not diagnosis" wording as the records themselves.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import typer

from exam_engine.advanced.service import AdvancedAssessmentService
from exam_engine.advanced.source import AdvancedError
from exam_engine.config import Settings
from exam_engine.models.advanced import (
    AdversarialRequest,
    MisconceptionQuery,
    MutationRequest,
    ResponseImport,
    ResponseSubmission,
)
from exam_engine.models.enums import (
    AssessmentOrigin,
    BloomLevel,
    ChallengeStrategy,
    DifficultyLabel,
    GenerationFormat,
    GenerationReviewStatus,
    MisconceptionKind,
    MisconceptionStatus,
    MutationStrategy,
)
from exam_engine.models.generation import GenerationBatch
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


def _short(value: str | None, limit: int = 52) -> str:
    if not value:
        return "-"
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


def register_advanced_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help=(
            "Phase 11 advanced assessment: question mutation, adversarial questions and "
            "evidence-bounded misconception mining."
        )
    )
    app.add_typer(group, name="advanced")

    def _service(database: Database) -> AdvancedAssessmentService:
        settings = get_settings()
        return AdvancedAssessmentService(database, settings)

    def _fail(exc: Exception, *, as_json: bool) -> None:
        if isinstance(exc, AdvancedError):
            payload = {"code": exc.code, "message": exc.message, "detail": exc.detail}
        elif isinstance(exc, KeyError):
            payload = {"code": "not_found", "message": str(exc).strip("'"), "detail": {}}
        else:  # pragma: no cover - unexpected boundary failure
            payload = {"code": "advanced_error", "message": str(exc), "detail": {}}
        if as_json:
            _dump({"error": payload})
        else:
            typer.echo(f"ERROR: [{payload['code']}] {payload['message']}", err=True)
            if payload.get("detail"):
                typer.echo("detail: " + json.dumps(payload["detail"], sort_keys=True), err=True)
        raise typer.Exit(code=1) from exc

    def _print_batch(batch: GenerationBatch) -> None:
        counts = batch.counts
        typer.echo(
            f"batch={batch.batch_id} capability={batch.capability} status={batch.status.value} "
            f"course={batch.course_id} syllabus={batch.scope.syllabus_id}"
        )
        typer.echo(
            f"algorithm={batch.algorithm}/{batch.algorithm_version} provider={batch.provider}"
        )
        typer.echo(
            f"requested={counts.requested} generated={counts.generated} "
            f"validated={counts.validated} needs_review={counts.needs_review} "
            f"incomplete={counts.validation_incomplete} rejected={counts.rejected} "
            f"duplicates={counts.duplicates} failed={counts.failed} "
            f"persisted={counts.persisted}"
        )
        _print_table(
            ["id", "strategy", "format", "bloom", "validation", "review", "question"],
            [
                [
                    question.question_id,
                    question.strategy or "-",
                    question.format.value,
                    question.requested_bloom.value,
                    question.validation.status.value,
                    question.review_status.value,
                    _short(question.text, 58),
                ]
                for question in batch.questions
            ],
        )
        for rejection in batch.rejections:
            typer.echo(
                f"  rejection[{rejection.index}] {rejection.code}: {_short(rejection.message, 90)}"
            )
            for issue in rejection.issues:
                typer.echo(f"      {issue.severity.value}: {issue.code} - {issue.message}")
        for warning in batch.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        typer.echo(f"scope: {batch.scope.statement}")
        for limitation in batch.limitations:
            typer.echo(f"  limitation: {limitation}")

    def _print_question(question: Any) -> None:
        typer.echo(
            f"question={question.question_id} origin={question.origin} "
            f"strategy={question.strategy or '-'} revision={question.revision}"
        )
        typer.echo(
            f"course={question.course_id} syllabus={question.syllabus_id} "
            f"unit={question.unit_label} format={question.format.value}"
        )
        typer.echo("concepts=" + ", ".join(question.concept_labels))
        typer.echo(
            f"bloom requested={question.requested_bloom.value} estimated="
            f"{question.estimated_bloom.value if question.estimated_bloom else 'unknown'}"
        )
        typer.echo(f"objective={question.learning_objective}")
        typer.echo("")
        typer.echo(question.text)
        if question.answer_key:
            typer.echo("")
            typer.echo(f"answer key: {question.answer_key}")
        if question.explanation:
            typer.echo(f"reference: {question.explanation}")
        for step in question.solution_steps:
            typer.echo(f"rubric: {step}")
        typer.echo("")
        typer.echo(f"validation={question.validation.status.value}")
        for issue in question.validation.issues:
            typer.echo(f"  {issue.severity.value}: {issue.code} ({issue.rule}): {issue.message}")
        typer.echo(f"review={question.review_status.value}")
        for source in question.source_references:
            typer.echo(
                f"  source: {source.document_title}"
                f' [{source.method}] "{_short(source.excerpt, 68)}"'
            )
        for limitation in question.limitations:
            typer.echo(f"  limitation: {limitation}")

    # -- capabilities ------------------------------------------------------

    @group.command("capabilities")
    def capabilities(
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """List supported mutation and challenge strategies and analysis rules."""

        with open_db() as database:
            payload = _service(database).capabilities()
        if as_json:
            _dump(payload)
            return
        typer.echo(f"phase={payload['phase']} algorithm={payload['algorithm']}")
        typer.echo("mutation strategies:")
        _print_table(
            ["strategy", "formats", "invariants", "answer changes"],
            [
                [
                    item["strategy"],
                    ",".join(item["formats"]) if item["formats"] else "any",
                    ",".join(item["invariants"]) or "none",
                    "yes" if item["changes_answer"] else "no",
                ]
                for item in payload["mutation_strategies"]
            ],
        )
        typer.echo("challenge strategies:")
        _print_table(
            ["strategy", "format", "requires", "claim"],
            [
                [
                    item["strategy"],
                    item["format"],
                    ",".join(item["requires"]) or "-",
                    "yes" if item["evaluates_claim"] else "no",
                ]
                for item in payload["challenge_strategies"]
            ],
        )
        typer.echo("misconception rules:")
        _print_table(
            ["kind", "rule"],
            [[kind, rule] for kind, rule in payload["misconception_kinds"].items()],
        )

    # -- mutation ----------------------------------------------------------

    @group.command("mutate")
    def mutate(
        course: str = typer.Option(..., "--course", help="Course id."),
        question: str = typer.Option(..., "--question", help="Source question id."),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus version id."),
        strategy: list[MutationStrategy] = typer.Option(
            [], "--strategy", help="Mutation strategy; repeat for several."
        ),
        count: int = typer.Option(3, "--count", min=1, help="Number of variants."),
        difficulty: DifficultyLabel | None = typer.Option(
            None, "--difficulty", help="Requested difficulty for the variants."
        ),
        bloom: BloomLevel | None = typer.Option(None, "--bloom", help="Requested Bloom level."),
        fmt: GenerationFormat | None = typer.Option(
            None,
            "--format",
            help="Constrain the response format; it must match the source question.",
        ),
        seed: int = typer.Option(0, "--seed", min=0, help="Deterministic variation seed."),
        preserve_objective: bool = typer.Option(
            True, "--preserve-objective/--allow-objective-change"
        ),
        preserve_concept: bool = typer.Option(True, "--preserve-concept/--allow-concept-change"),
        preserve_answer: bool = typer.Option(True, "--preserve-answer/--allow-answer-change"),
        allow_format_change: bool = typer.Option(False, "--allow-format-change"),
        persist: bool = typer.Option(True, "--persist/--no-persist"),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Derive variants of an existing question, preserving what each strategy promises."""

        try:
            request = MutationRequest(
                course_id=course,
                syllabus_id=syllabus,
                question_id=question,
                strategies=list(strategy),
                count=count,
                difficulty=difficulty,
                bloom=bloom,
                format=fmt,
                preserve_objective=preserve_objective,
                preserve_concept=preserve_concept,
                preserve_answer=preserve_answer,
                allow_format_change=allow_format_change,
                seed=seed,
                persist=persist,
            )
        except ValueError as exc:
            _fail(AdvancedError("invalid_request", str(exc)), as_json=as_json)
            return
        try:
            with open_db() as database:
                batch = _service(database).mutate(request)
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(batch.model_dump(mode="json"))
            return
        _print_batch(batch)
        typer.echo(
            "note: variants are derived practice material. A VALIDATED status means the "
            "promised properties held, not that the question is educationally correct."
        )

    # -- adversarial -------------------------------------------------------

    @group.command("adversarial")
    def adversarial(
        course: str = typer.Option(..., "--course", help="Course id."),
        concept: list[str] = typer.Option([], "--concept", help="Concept id or label; repeatable."),
        unit: str | None = typer.Option(None, "--unit", help="Syllabus unit id."),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus version id."),
        strategy: list[ChallengeStrategy] = typer.Option(
            [], "--strategy", help="Challenge strategy; repeat for several."
        ),
        count: int = typer.Option(3, "--count", min=1, help="Number of questions."),
        difficulty: DifficultyLabel | None = typer.Option(None, "--difficulty"),
        bloom: BloomLevel | None = typer.Option(None, "--bloom"),
        seed: int = typer.Option(0, "--seed", min=0),
        allow_out_of_scope: bool = typer.Option(False, "--allow-out-of-scope"),
        include_rubric: bool = typer.Option(True, "--rubric/--no-rubric"),
        persist: bool = typer.Option(True, "--persist/--no-persist"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Generate challenging but fair questions grounded in course material."""

        if not concept and not unit:
            _fail(
                AdvancedError(
                    "missing_target",
                    "provide at least one --concept or a --unit; targets are never guessed",
                ),
                as_json=as_json,
            )
        try:
            request = AdversarialRequest(
                course_id=course,
                syllabus_id=syllabus,
                concept_ids=list(concept),
                unit_id=unit,
                strategies=list(strategy),
                count=count,
                difficulty=difficulty,
                bloom=bloom,
                allow_out_of_scope=allow_out_of_scope,
                include_rubric=include_rubric,
                seed=seed,
                persist=persist,
            )
        except ValueError as exc:
            _fail(AdvancedError("invalid_request", str(exc)), as_json=as_json)
            return
        try:
            with open_db() as database:
                batch = _service(database).generate_adversarial(request)
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(batch.model_dump(mode="json"))
            return
        _print_batch(batch)
        typer.echo(
            "note: challenges are review material. No model answer is invented; each question "
            "carries a rubric and must be reviewed before use."
        )

    @group.command("variants")
    def variants(
        course: str = typer.Option(..., "--course", help="Course id."),
        origin: AssessmentOrigin = typer.Option(
            AssessmentOrigin.MUTATION, "--origin", help="mutation or adversarial."
        ),
        strategy: str | None = typer.Option(None, "--strategy"),
        limit: int = typer.Option(20, "--limit", min=1, max=200),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List stored variants by origin and strategy."""

        try:
            with open_db() as database:
                rows = _service(database).list_variants(
                    course, origin=origin, strategy=strategy, limit=limit
                )
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_table(
            ["id", "origin", "strategy", "validation", "review", "question"],
            [
                [
                    item.question_id,
                    item.origin,
                    item.strategy or "-",
                    item.validation.status.value,
                    item.review_status.value,
                    _short(item.text, 54),
                ]
                for item in rows
            ],
        )

    @group.command("show")
    def show(
        question: str = typer.Argument(..., help="Generated, mutated or adversarial question id."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show one stored question with its rubric, validation findings and provenance."""

        try:
            with open_db() as database:
                row = _service(database).get_question(question)
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(row.model_dump(mode="json"))
            return
        _print_question(row)

    @group.command("review")
    def review(
        question: str = typer.Argument(..., help="Question id."),
        status: GenerationReviewStatus = typer.Option(..., "--status", help="New review status."),
        note: str | None = typer.Option(None, "--note", help="Reviewer note."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Record a human review decision on a stored variant."""

        try:
            with open_db() as database:
                row = _service(database).set_review_status(question, status, note=note)
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(row.model_dump(mode="json"))
            return
        typer.echo(f"question={row.question_id} review={row.review_status.value}")
        for event in row.review_history:
            typer.echo(
                f"  {event.at.isoformat(timespec='seconds')} {event.status.value} by "
                f"{event.reviewer}" + (f": {event.note}" if event.note else "")
            )

    # -- responses ---------------------------------------------------------

    responses = typer.Typer(help="Import and inspect stored student responses.")
    group.add_typer(responses, name="responses")

    @responses.command("import")
    def import_responses(
        course: str = typer.Option(..., "--course", help="Course id."),
        file: Path = typer.Option(..., "--file", help="JSON file of responses."),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        source: str = typer.Option("manual_import", "--source", help="Where the data came from."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Import anonymised responses from a JSON file.

        Each entry needs a question id and an answer; ``student_ref`` and ``group_ref``
        are optional and are hashed on import. The plain values are never stored.
        """

        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _fail(
                AdvancedError("invalid_file", f"could not read {file}: {exc}"),
                as_json=as_json,
            )
            return
        entries = raw.get("responses") if isinstance(raw, dict) else raw
        if not isinstance(entries, list) or not entries:
            _fail(
                AdvancedError("invalid_file", "the file must hold a non-empty list of responses"),
                as_json=as_json,
            )
            return
        try:
            submissions = [ResponseSubmission.model_validate(item) for item in entries]
            payload = ResponseImport(
                course_id=course,
                syllabus_id=syllabus,
                source=source,
                responses=submissions,
            )
        except ValueError as exc:
            _fail(AdvancedError("invalid_request", str(exc)), as_json=as_json)
            return
        try:
            with open_db() as database:
                summary = _service(database).import_responses(payload)
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(summary.as_dict())
            return
        typer.echo(
            f"course={summary.course_id} syllabus={summary.syllabus_id} "
            f"supplied={summary.supplied} inserted={summary.inserted} "
            f"duplicates={summary.duplicates} "
            f"correctness_inferred={summary.inferred_correctness}"
        )
        for warning in summary.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        typer.echo("note: response ids are hashes; no student name is stored, logged or exported.")

    @responses.command("list")
    def list_responses(
        course: str = typer.Option(..., "--course", help="Course id."),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        question: list[str] = typer.Option([], "--question", help="Restrict to these questions."),
        limit: int = typer.Option(50, "--limit", min=1, max=1000),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List stored responses, showing only anonymised identifiers."""

        from exam_engine.storage.advanced_repository import ResponseRepository
        from exam_engine.storage.repositories import CourseRepository

        try:
            with open_db() as database, database.session() as session:
                if CourseRepository(session).get(course) is None:
                    raise AdvancedError("unknown_course", f"no course {course!r}")
                rows = ResponseRepository(session).list(
                    course,
                    syllabus_id=syllabus,
                    question_ids=list(question) if question else None,
                    limit=limit,
                )
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_table(
            ["response", "question", "correctness", "marks", "words", "answer"],
            [
                [
                    item.response_id,
                    item.question_id,
                    item.correctness.value,
                    f"{item.marks_awarded:g}/{item.marks_available:g}"
                    if item.marks_awarded is not None and item.marks_available is not None
                    else "-",
                    item.word_count,
                    _short(item.answer_text, 48),
                ]
                for item in rows
            ],
        )

    # -- misconceptions ----------------------------------------------------

    misconceptions = typer.Typer(help="Mine and review candidate misconception patterns.")
    group.add_typer(misconceptions, name="misconceptions")

    @misconceptions.command("analyse")
    def analyse(
        course: str = typer.Option(..., "--course", help="Course id."),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        question: list[str] = typer.Option([], "--question", help="Restrict to these questions."),
        min_support: int | None = typer.Option(
            None, "--min-support", min=1, help="Minimum independent responses for a candidate."
        ),
        persist: bool = typer.Option(True, "--persist/--no-persist"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Analyse stored responses for candidate misconception patterns."""

        try:
            with open_db() as database:
                report = _service(database).analyse_misconceptions(
                    course,
                    syllabus_id=syllabus,
                    question_ids=list(question),
                    min_support=min_support,
                    persist=persist,
                )
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(report.model_dump(mode="json"))
            return
        typer.echo(
            f"course={report.course_id} syllabus={report.syllabus_id} "
            f"responses={report.distinct_responses} duplicates={report.duplicate_responses} "
            f"questions={report.questions_analysed} records={len(report.records)} "
            f"discarded={len(report.discarded)}"
        )
        _print_table(
            ["id", "kind", "support", "questions", "level", "confidence", "review", "label"],
            [
                [
                    item.misconception_id,
                    item.kind.value,
                    item.support_count,
                    item.distinct_questions,
                    item.support_level.value,
                    f"{item.confidence:.2f}",
                    item.review_status.value,
                    _short(item.label, 44),
                ]
                for item in report.records
            ],
        )
        if report.discarded:
            reasons: dict[str, int] = {}
            for item in report.discarded:
                reasons[item.reason] = reasons.get(item.reason, 0) + 1
            typer.echo("discarded responses by reason:")
            _print_table(
                ["reason", "count"],
                [[key, value] for key, value in sorted(reasons.items())],
            )
        for warning in report.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        for limitation in report.limitations:
            typer.echo(f"  limitation: {limitation}")
        typer.echo(
            "note: records are candidate readings of stored answers, not diagnoses, and no "
            "population-level claim is implied."
        )

    @misconceptions.command("list")
    def list_misconceptions(
        course: str = typer.Option(..., "--course", help="Course id."),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        kind: MisconceptionKind | None = typer.Option(None, "--kind"),
        status: MisconceptionStatus | None = typer.Option(None, "--status"),
        concept: str | None = typer.Option(None, "--concept", help="Concept id."),
        min_support: int = typer.Option(0, "--min-support", min=0),
        limit: int = typer.Option(50, "--limit", min=1, max=1000),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List stored misconception records."""

        try:
            with open_db() as database:
                rows = _service(database).list_misconceptions(
                    MisconceptionQuery(
                        course_id=course,
                        syllabus_id=syllabus,
                        kind=kind,
                        status=status,
                        concept_id=concept,
                        min_support=min_support,
                        limit=limit,
                    )
                )
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump([item.model_dump(mode="json") for item in rows])
            return
        _print_table(
            ["id", "kind", "support", "level", "confidence", "review", "label"],
            [
                [
                    item.misconception_id,
                    item.kind.value,
                    item.support_count,
                    item.support_level.value,
                    f"{item.confidence:.2f}",
                    item.review_status.value,
                    _short(item.label, 44),
                ]
                for item in rows
            ],
        )

    @misconceptions.command("show")
    def show_misconception(
        misconception_id: str = typer.Argument(..., help="Misconception id."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show one record with its supporting evidence excerpts."""

        try:
            with open_db() as database:
                record = _service(database).get_misconception(misconception_id)
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(record.model_dump(mode="json"))
            return
        typer.echo(
            f"misconception={record.misconception_id} kind={record.kind.value} "
            f"support={record.support_count} questions={record.distinct_questions} "
            f"level={record.support_level.value} confidence={record.confidence:.2f} "
            f"review={record.review_status.value}"
        )
        typer.echo(f"course={record.course_id} syllabus={record.syllabus_id}")
        typer.echo("concepts=" + (", ".join(record.concept_labels) or "-"))
        typer.echo(f"label={record.label}")
        typer.echo(f"description={record.description}")
        if record.suggested_correction:
            typer.echo(f"suggested correction: {record.suggested_correction}")
        typer.echo("evidence:")
        for item in record.evidence:
            typer.echo(
                f"  {item.response_id} [{item.marker} {item.method}] q={item.question_id}: "
                f'"{item.excerpt}"'
            )
        for limitation in record.limitations:
            typer.echo(f"  limitation: {limitation}")

    @misconceptions.command("review")
    def review_misconception(
        misconception_id: str = typer.Argument(..., help="Misconception id."),
        status: MisconceptionStatus = typer.Option(
            ..., "--status", help="candidate, approved or rejected."
        ),
        note: str | None = typer.Option(None, "--note"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Record a human decision on one candidate misconception."""

        try:
            with open_db() as database:
                record = _service(database).set_misconception_status(
                    misconception_id, status, note=note
                )
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(record.model_dump(mode="json"))
            return
        typer.echo(f"misconception={record.misconception_id} review={record.review_status.value}")
        for event in record.review_history:
            typer.echo(
                f"  {event.at.isoformat(timespec='seconds')} {event.status.value} by "
                f"{event.reviewer}" + (f": {event.note}" if event.note else "")
            )

    @misconceptions.command("export")
    def export_misconceptions(
        course: str = typer.Option(..., "--course", help="Course id."),
        syllabus: str = typer.Option(..., "--syllabus", help="Syllabus version id."),
        out: Path = typer.Option(..., "--out", help="Destination JSON file."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Export stored misconception records as a structured report."""

        try:
            with open_db() as database:
                path, count = _service(database).export_misconceptions(course, syllabus, out)
        except AdvancedError as exc:
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump({"path": str(path), "count": count})
            return
        typer.echo(f"wrote {count} record(s) to {path}")


__all__ = ["register_advanced_commands"]
