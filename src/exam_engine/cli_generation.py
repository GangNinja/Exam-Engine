"""``exam-engine generation`` - Phase 10 question generation and review.

The command layer stays thin: grounding, validation, dedupe and persistence all
live in :class:`QuestionGenerationService`, so another consumer can reuse them
without the CLI. Commands never edit historical examination questions.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import typer

from exam_engine.config import Settings
from exam_engine.generation.service import GenerationError, QuestionGenerationService
from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    GenerationFormat,
    GenerationReviewStatus,
    GenerationValidationStatus,
)
from exam_engine.models.generation import (
    GeneratedQuestion,
    GenerationBatch,
    GenerationQuery,
    GenerationRequest,
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


def _request_payload(batch: GenerationBatch) -> dict[str, Any]:
    payload = batch.model_dump(mode="json")
    return payload


def register_generation_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help=(
            "Phase 10 question generation: grounded, validated, reviewable practice "
            "questions built from stored syllabus and study material."
        )
    )
    app.add_typer(group, name="generation")

    def _service(database: Database) -> QuestionGenerationService:
        settings = get_settings()
        return QuestionGenerationService(
            database,
            settings.generation,
            understanding_settings=settings.understanding,
        )

    def _fail(exc: Exception, *, as_json: bool) -> None:
        if isinstance(exc, GenerationError):
            payload = exc.as_dict()
        elif isinstance(exc, KeyError):
            payload = {"code": "not_found", "message": str(exc).strip("'"), "detail": {}}
        else:  # pragma: no cover - unexpected boundary failure
            payload = {"code": "generation_error", "message": str(exc), "detail": {}}
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
            f"batch={batch.batch_id} status={batch.status.value} "
            f"course={batch.course_id} syllabus={batch.scope.syllabus_id}"
        )
        typer.echo(
            f"provider={batch.provider}"
            + (f" model={batch.model}" if batch.model else "")
            + f" bloom={batch.resolved_bloom.value} difficulty={batch.resolved_difficulty.value}"
            + f" marks={batch.request.marks or '-'}"
        )
        typer.echo(
            f"requested={counts.requested} generated={counts.generated} "
            f"validated={counts.validated} needs_review={counts.needs_review} "
            f"incomplete={counts.validation_incomplete} rejected={counts.rejected} "
            f"duplicates={counts.duplicates} failed={counts.failed} "
            f"persisted={counts.persisted} reused_approved={counts.reused_approved}"
        )
        _print_table(
            ["id", "unit", "concept", "format", "bloom", "validation", "review", "question"],
            [
                [
                    question.question_id,
                    question.unit_label,
                    _short(", ".join(question.concept_labels), 22),
                    question.format.value,
                    question.requested_bloom.value,
                    question.validation.status.value,
                    question.review_status.value,
                    _short(question.text, 58),
                ]
                for question in batch.questions
            ],
        )
        if batch.rejections:
            typer.echo("rejections:")
            _print_table(
                ["index", "concept", "code", "reason"],
                [
                    [
                        item.index,
                        _short(item.concept_label, 24),
                        item.code,
                        _short(item.message, 72),
                    ]
                    for item in batch.rejections
                ],
            )
        if batch.duplicates:
            typer.echo("similarity decisions:")
            _print_table(
                ["index", "kind", "matched", "score", "reason"],
                [
                    [
                        item.index,
                        item.kind,
                        item.matched_question_id,
                        f"{item.score:.3f}",
                        _short(item.reason, 62),
                    ]
                    for item in batch.duplicates
                ],
            )
        for warning in batch.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        if counts.requested and len(batch.questions) < counts.requested:
            typer.echo(
                f"  partial: {len(batch.questions)} of {counts.requested} requested questions "
                "were produced; see rejections above"
            )
        typer.echo(f"scope: {batch.scope.statement}")
        typer.echo(
            "note: generated questions are proposed practice material. Automated validation "
            "does not prove educational correctness, and nothing here predicts real "
            "examination content."
        )

    def _print_question(question: GeneratedQuestion) -> None:
        typer.echo(f"question={question.question_id} revision={question.revision}")
        typer.echo(
            f"batch={question.batch_id} course={question.course_id} "
            f"syllabus={question.syllabus_id} unit={question.unit_label}"
        )
        typer.echo(
            "concepts=" + ", ".join(question.concept_labels) + f"  format={question.format.value}"
        )
        typer.echo(
            f"bloom requested={question.requested_bloom.value} "
            "estimated="
            + (question.estimated_bloom.value if question.estimated_bloom else "unknown")
            + " "
            f"confidence={question.bloom_confidence:.2f}"
        )
        typer.echo(
            f"difficulty requested={question.requested_difficulty.value} "
            f"estimated={question.estimated_difficulty.value} "
            f"confidence={question.difficulty_confidence:.2f} marks={question.marks:g}"
        )
        typer.echo(f"objective={question.learning_objective}")
        typer.echo("")
        typer.echo(question.text)
        if question.options:
            typer.echo("")
            for option in question.options:
                marker = "*" if option.is_correct else " "
                typer.echo(f"  {marker} {option.key}) {option.text}")
                note = question.distractor_explanations.get(option.key)
                if note:
                    typer.echo(f"      {note}")
        if question.answer_key:
            typer.echo("")
            typer.echo(f"answer key: {question.answer_key}")
        if question.explanation:
            typer.echo(f"explanation: {question.explanation}")
        for step in question.solution_steps:
            typer.echo(f"step: {step}")
        typer.echo("")
        typer.echo(
            f"validation={question.validation.status.value} "
            "confidence="
            + (
                str(question.validation.confidence)
                if question.validation.confidence is not None
                else "-"
            )
        )
        for issue in question.validation.issues:
            typer.echo(f"  {issue.severity.value}: {issue.code} ({issue.rule}): {issue.message}")
        typer.echo(f"review={question.review_status.value}")
        for event in question.review_history:
            typer.echo(
                f"  {event.at.isoformat(timespec='seconds')} {event.status.value} "
                f"by {event.reviewer}" + (f": {event.note}" if event.note else "")
            )
        typer.echo("sources:")
        for source in question.source_references:
            typer.echo(
                f"  {source.document_kind.value}: {source.document_title}"
                + (f" p.{source.page_number}" if source.page_number else "")
                + f' [{source.method}] "{_short(source.excerpt, 68)}"'
            )
        for warning in question.warnings:
            typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
        for limitation in question.limitations:
            typer.echo(f"  limitation: {limitation}")

    @group.command("generate")
    def generate(
        course: str = typer.Option(..., "--course", help="Course id."),
        syllabus: str | None = typer.Option(
            None, "--syllabus", help="Syllabus version id (required when the course has several)."
        ),
        concept: list[str] = typer.Option(
            [], "--concept", help="Target concept id or label; repeat for several."
        ),
        unit: str | None = typer.Option(None, "--unit", help="Syllabus unit id."),
        count: int = typer.Option(5, "--count", min=1, help="Questions to request."),
        fmt: GenerationFormat = typer.Option(
            GenerationFormat.EXPLANATION, "--format", help="Requested question format."
        ),
        difficulty: DifficultyLabel | None = typer.Option(
            None, "--difficulty", help="Requested difficulty (default: documentation default)."
        ),
        bloom: BloomLevel | None = typer.Option(
            None, "--bloom", help="Requested Bloom level (default: format default)."
        ),
        marks: float | None = typer.Option(None, "--marks", help="Marks per question."),
        document: list[str] = typer.Option(
            [], "--document", help="Restrict grounding to these document ids; repeat as needed."
        ),
        reference: list[str] = typer.Option(
            [], "--reference", help="Reference question id(s) for style conventions."
        ),
        provider: str = typer.Option(
            "auto", "--provider", help="auto, template or openai (opt-in model provider)."
        ),
        seed: int = typer.Option(0, "--seed", min=0, help="Deterministic option shuffle seed."),
        language: str = typer.Option("en", "--language", help="Content language (en only)."),
        include_answer_key: bool = typer.Option(
            True, "--answer-key/--no-answer-key", help="Include an answer key where derivable."
        ),
        include_explanation: bool = typer.Option(
            True, "--explanation/--no-explanation", help="Include an explanation where supported."
        ),
        allow_overwrite_approved: bool = typer.Option(
            False,
            "--allow-overwrite-approved",
            help="Replace an already approved question with this regeneration.",
        ),
        persist: bool = typer.Option(
            True, "--persist/--no-persist", help="Store the batch and its questions."
        ),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Generate grounded questions for concepts or a syllabus unit."""

        if not concept and not unit:
            _fail(
                GenerationError(
                    "missing_target",
                    "provide at least one --concept or a --unit; concepts are never guessed",
                ),
                as_json=as_json,
            )
        try:
            request = GenerationRequest(
                course_id=course,
                syllabus_id=syllabus,
                concept_ids=list(concept),
                unit_id=unit,
                count=count,
                format=fmt,
                difficulty=difficulty,
                bloom=bloom,
                marks=marks,
                document_ids=list(document),
                reference_question_ids=list(reference),
                language=language,
                provider=provider,
                seed=seed,
                include_answer_key=include_answer_key,
                include_explanation=include_explanation,
                allow_overwrite_approved=allow_overwrite_approved,
            )
        except ValueError as exc:
            _fail(GenerationError("invalid_request", str(exc)), as_json=as_json)
            return
        try:
            with open_db() as database:
                batch = _service(database).generate(request, persist=persist)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(_request_payload(batch))
            return
        _print_batch(batch)

    @group.command("formats")
    def formats(
        provider: str = typer.Option(
            "auto", "--provider", help="Provider to check capabilities for."
        ),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """List supported formats, Bloom levels and provider requirements."""

        try:
            with open_db() as database:
                service = _service(database)
                rows = service.capabilities()
                described = service.describe_provider(provider)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump({"provider": described, "formats": rows})
            return
        typer.echo(
            f"provider={described['name']} model={described.get('model') or '-'} "
            f"network={described['requires_network']} "
            f"credentials={described['requires_credentials']}"
        )
        _print_table(
            ["format", "supported blooms", "blocked by provider", "needs model"],
            [
                [
                    row["format"],
                    ", ".join(row["supported_by_provider"]) or "-",
                    ", ".join(row["blocked_by_provider"]) or "-",
                    "yes" if row["requires_model"] else "no",
                ]
                for row in rows
            ],
        )
        typer.echo(
            "note: the deterministic template provider needs no key and no network; numerical "
            "problems require a configured language-model provider."
        )

    @group.command("list")
    def list_questions(
        course: str = typer.Option(..., "--course", help="Course id."),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus version id."),
        batch: str | None = typer.Option(None, "--batch", help="Only this batch."),
        concept: str | None = typer.Option(None, "--concept", help="Only this concept id."),
        fmt: GenerationFormat | None = typer.Option(None, "--format", help="Only this format."),
        review_status: GenerationReviewStatus | None = typer.Option(
            None, "--review-status", help="Only this review status."
        ),
        validation_status: GenerationValidationStatus | None = typer.Option(
            None, "--validation-status", help="Only this validation status."
        ),
        limit: int = typer.Option(50, "--limit", min=1, max=1000, help="Maximum rows."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """List stored generated questions with their validation and review state."""

        try:
            with open_db() as database:
                questions = _service(database).list_questions(
                    GenerationQuery(
                        course_id=course,
                        syllabus_id=syllabus,
                        batch_id=batch,
                        concept_id=concept,
                        format=fmt,
                        review_status=review_status,
                        validation_status=validation_status,
                        limit=limit,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump([item.model_dump(mode="json") for item in questions])
            return
        _print_table(
            ["id", "batch", "unit", "format", "validation", "review", "rev", "question"],
            [
                [
                    item.question_id,
                    item.batch_id,
                    item.unit_label,
                    item.format.value,
                    item.validation.status.value,
                    item.review_status.value,
                    item.revision,
                    _short(item.text, 60),
                ]
                for item in questions
            ],
        )
        typer.echo(f"total={len(questions)}")

    @group.command("show")
    def show(
        batch: str | None = typer.Option(None, "--batch", help="Batch id to print in full."),
        question_id: str | None = typer.Option(
            None, "--question", help="Question id to print in full."
        ),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Show one generated question or one full batch report."""

        if not batch and not question_id:
            _fail(
                GenerationError("missing_target", "pass --batch or --question"),
                as_json=as_json,
            )
            return
        try:
            with open_db() as database:
                service = _service(database)
                if question_id:
                    question = service.get_question(question_id)
                    if as_json:
                        _dump(question.model_dump(mode="json"))
                    else:
                        _print_question(question)
                    return
                payload = service.get_batch(batch or "")
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(payload.model_dump(mode="json"))
            return
        _print_batch(payload)

    @group.command("batches")
    def batches(
        course: str = typer.Option(..., "--course", help="Course id."),
        limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum batches."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """List stored generation batches for a course."""

        try:
            with open_db() as database:
                rows = _service(database).list_batches(course, limit=limit)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(rows)
            return
        _print_table(
            ["batch", "status", "provider", "model", "requested", "validated", "created"],
            [
                [
                    row["batch_id"],
                    row["status"],
                    row["provider"],
                    row["model"] or "-",
                    row["counts"].get("requested", 0),
                    row["counts"].get("validated", 0),
                    row["created_at"],
                ]
                for row in rows
            ],
        )

    @group.command("failures")
    def failures(
        batch: str = typer.Option(..., "--batch", help="Batch id."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Inspect why a batch did not meet its requested count."""

        try:
            with open_db() as database:
                payload = _service(database).get_batch(batch)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        report = {
            "batch_id": payload.batch_id,
            "status": payload.status.value,
            "counts": payload.counts.model_dump(mode="json"),
            "rejections": [item.model_dump(mode="json") for item in payload.rejections],
            "duplicates": [item.model_dump(mode="json") for item in payload.duplicates],
            "review_required": [
                {
                    "question_id": item.question_id,
                    "validation": item.validation.status.value,
                    "issues": [issue.model_dump(mode="json") for issue in item.validation.issues],
                }
                for item in payload.questions
                if item.validation.status is not GenerationValidationStatus.VALIDATED
            ],
        }
        if as_json:
            _dump(report)
            return
        counts = payload.counts
        typer.echo(
            f"batch={payload.batch_id} status={payload.status.value} "
            f"requested={counts.requested} accepted={len(payload.questions)} "
            f"rejected={counts.rejected} failed={counts.failed} duplicates={counts.duplicates}"
        )
        if payload.rejections:
            _print_table(
                ["index", "concept", "code", "reason"],
                [
                    [
                        item.index,
                        _short(item.concept_label, 24),
                        item.code,
                        _short(item.message, 78),
                    ]
                    for item in payload.rejections
                ],
            )
            for item in payload.rejections:
                for issue in item.issues:
                    typer.echo(f"  [{item.index}] {issue.code} ({issue.rule}): {issue.message}")
        else:
            typer.echo("no rejections")
        reviewable = [
            question
            for question in payload.questions
            if question.validation.status is not GenerationValidationStatus.VALIDATED
        ]
        if reviewable:
            typer.echo("questions that need review:")
            for question in reviewable:
                codes = (
                    ", ".join(f"{issue.code}" for issue in question.validation.issues)
                    or "no findings"
                )
                typer.echo(f"  {question.question_id} [{question.validation.status.value}] {codes}")

    @group.command("review")
    def review(
        question_id: str = typer.Option(..., "--question", help="Generated question id."),
        status: GenerationReviewStatus = typer.Option(..., "--status", help="New review status."),
        note: str | None = typer.Option(None, "--note", help="Reviewer note kept in the history."),
        reviewer: str = typer.Option("local-user", "--reviewer", help="Reviewer name."),
        expected_revision: int | None = typer.Option(
            None, "--expected-revision", help="Refuse the change if the row moved on."
        ),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Approve, reject or reopen one generated question."""

        try:
            with open_db() as database:
                question = _service(database).set_review_status(
                    question_id,
                    status,
                    note=note,
                    reviewer=reviewer,
                    expected_revision=expected_revision,
                )
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(question.model_dump(mode="json"))
            return
        _print_question(question)

    @group.command("review-batch")
    def review_batch(
        batch: str = typer.Option(..., "--batch", help="Batch id."),
        status: GenerationReviewStatus = typer.Option(..., "--status", help="New review status."),
        note: str | None = typer.Option(None, "--note", help="Reviewer note."),
        reviewer: str = typer.Option("local-user", "--reviewer", help="Reviewer name."),
        all_questions: bool = typer.Option(
            False, "--all", help="Include questions that already left pending review."
        ),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Apply one review decision to a batch."""

        try:
            with open_db() as database:
                updated = _service(database).review_batch(
                    batch,
                    status,
                    note=note,
                    reviewer=reviewer,
                    only_pending=not all_questions,
                )
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump([item.model_dump(mode="json") for item in updated])
            return
        _print_table(
            ["question", "review", "revision"],
            [[item.question_id, item.review_status.value, item.revision] for item in updated],
        )
        typer.echo(f"updated={len(updated)}")

    @group.command("export")
    def export(
        batch: str = typer.Option(..., "--batch", help="Batch id."),
        out: Path = typer.Option(..., "--out", help="Destination JSON path."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Export a batch report and its questions as JSON."""

        try:
            with open_db() as database:
                path = _service(database).export_batch(batch, out)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump({"path": str(path), "batch_id": batch})
            return
        typer.echo(f"wrote {path}")

    @group.command("review-summary")
    def review_summary(
        course: str = typer.Option(..., "--course", help="Course id."),
        syllabus: str = typer.Option(..., "--syllabus", help="Syllabus version id."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Count stored generated questions by review status."""

        try:
            with open_db() as database:
                counts = _service(database).review_summary(course, syllabus)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            _fail(exc, as_json=as_json)
            return
        if as_json:
            _dump(counts)
            return
        _print_table(["review status", "questions"], sorted(counts.items()))


__all__ = ["register_generation_commands"]
