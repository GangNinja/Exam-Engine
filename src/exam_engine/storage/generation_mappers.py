"""Model <-> row conversion for Phase 10 generated-question tables."""

from __future__ import annotations

from datetime import datetime

from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    GenerationFormat,
    GenerationReviewStatus,
)
from exam_engine.models.generation import (
    DuplicateResolution,
    GeneratedOption,
    GeneratedQuestion,
    GenerationBatch,
    GenerationCounts,
    GenerationRejection,
    GenerationRequest,
    ReviewEvent,
    SourcePassage,
    ValidationResult,
)
from exam_engine.models.question import Uncertainty
from exam_engine.storage.mappers import dumps, iso, loads, parse_dt
from exam_engine.storage.schema import GeneratedQuestionRow, GenerationBatchRow


def _enum(value: type, raw: str | None):
    if not raw:
        return None
    try:
        return value(raw)
    except ValueError:
        return None


def _uncertainties(raw: list[Uncertainty]) -> str:
    return dumps([item.model_dump(mode="json") for item in raw])


def _parse_uncertainties(raw: str | None) -> list[Uncertainty]:
    return [Uncertainty.model_validate(item) for item in loads(raw, []) if isinstance(item, dict)]


def question_to_row(question: GeneratedQuestion) -> GeneratedQuestionRow:
    return GeneratedQuestionRow(
        question_id=question.question_id,
        batch_id=question.batch_id,
        course_id=question.course_id,
        syllabus_id=question.syllabus_id,
        origin=question.origin,
        source_question_id=question.source_question_id,
        strategy=question.strategy,
        unit_id=question.unit_id,
        unit_label=question.unit_label,
        unit_title=question.unit_title,
        concept_ids_json=dumps(question.concept_ids),
        concept_labels_json=dumps(question.concept_labels),
        learning_objective=question.learning_objective,
        format=question.format.value,
        question_type=question.question_type,
        requested_difficulty=question.requested_difficulty.value,
        estimated_difficulty=question.estimated_difficulty.value,
        difficulty_confidence=question.difficulty_confidence,
        difficulty_method=question.difficulty_method,
        requested_bloom=question.requested_bloom.value,
        estimated_bloom=(question.estimated_bloom.value if question.estimated_bloom else None),
        bloom_confidence=question.bloom_confidence,
        marks=question.marks,
        text=question.text,
        options_json=dumps([item.model_dump(mode="json") for item in question.options]),
        correct_option_key=question.correct_option_key,
        answer_key=question.answer_key,
        explanation=question.explanation,
        solution_steps_json=dumps(question.solution_steps),
        distractor_explanations_json=dumps(question.distractor_explanations),
        source_references_json=dumps(
            [item.model_dump(mode="json") for item in question.source_references]
        ),
        generation_metadata_json=dumps(question.generation_metadata),
        validation_status=question.validation.status.value,
        validation_json=dumps(question.validation.model_dump(mode="json")),
        review_status=question.review_status.value,
        review_history_json=dumps(
            [item.model_dump(mode="json") for item in question.review_history]
        ),
        revision=question.revision,
        warnings_json=_uncertainties(question.warnings),
        limitations_json=dumps(question.limitations),
        created_at=iso(question.created_at) or "",
        updated_at=iso(question.updated_at) or "",
    )


def row_to_question(row: GeneratedQuestionRow) -> GeneratedQuestion:
    raw_validation = loads(row.validation_json, {})
    if not isinstance(raw_validation, dict) or "status" not in raw_validation:
        raw_validation = {"status": row.validation_status}
    validation = ValidationResult.model_validate(raw_validation)
    return GeneratedQuestion(
        question_id=row.question_id,
        batch_id=row.batch_id,
        revision=row.revision,
        course_id=row.course_id,
        syllabus_id=row.syllabus_id,
        origin=row.origin,
        source_question_id=row.source_question_id,
        strategy=row.strategy,
        unit_id=row.unit_id,
        unit_label=row.unit_label,
        unit_title=row.unit_title,
        concept_ids=loads(row.concept_ids_json, []),
        concept_labels=loads(row.concept_labels_json, []),
        learning_objective=row.learning_objective,
        format=GenerationFormat(row.format),
        question_type=row.question_type,
        requested_difficulty=DifficultyLabel(row.requested_difficulty),
        estimated_difficulty=_enum(DifficultyLabel, row.estimated_difficulty)
        or DifficultyLabel.UNKNOWN,
        difficulty_confidence=row.difficulty_confidence,
        difficulty_method=row.difficulty_method,
        requested_bloom=BloomLevel(row.requested_bloom),
        estimated_bloom=_enum(BloomLevel, row.estimated_bloom),
        bloom_confidence=row.bloom_confidence,
        marks=row.marks,
        text=row.text,
        options=[GeneratedOption.model_validate(item) for item in loads(row.options_json, [])],
        correct_option_key=row.correct_option_key,
        answer_key=row.answer_key,
        explanation=row.explanation,
        solution_steps=loads(row.solution_steps_json, []),
        distractor_explanations=loads(row.distractor_explanations_json, {}),
        source_references=[
            SourcePassage.model_validate(item) for item in loads(row.source_references_json, [])
        ],
        generation_metadata=loads(row.generation_metadata_json, {}),
        validation=validation,
        review_status=GenerationReviewStatus(row.review_status),
        review_history=[
            ReviewEvent.model_validate(item) for item in loads(row.review_history_json, [])
        ],
        warnings=_parse_uncertainties(row.warnings_json),
        limitations=loads(row.limitations_json, []),
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
        updated_at=parse_dt(row.updated_at) or datetime.now().astimezone(),
    )


def batch_to_row(batch: GenerationBatch) -> GenerationBatchRow:
    payload = batch.model_dump(mode="json")
    # Question bodies live in their own table; the batch row only keeps the report.
    payload["questions"] = [
        {"question_id": item["question_id"]} for item in payload.get("questions", [])
    ]
    payload["request"] = batch.request.model_dump(mode="json")
    return GenerationBatchRow(
        batch_id=batch.batch_id,
        course_id=batch.course_id,
        syllabus_id=batch.scope.syllabus_id,
        status=batch.status.value,
        provider=batch.provider,
        model=batch.model,
        capability=batch.capability,
        request_json=dumps(batch.request.model_dump(mode="json")),
        parameters_json=dumps(batch.parameters),
        counts_json=dumps(batch.counts.model_dump(mode="json")),
        report_json=dumps(payload),
        created_at=iso(batch.generated_at) or "",
        updated_at=iso(batch.generated_at) or "",
    )


def row_to_batch(row: GenerationBatchRow) -> GenerationBatch:
    payload = loads(row.report_json, {})
    if not isinstance(payload, dict) or not payload:
        payload = {}
    raw_request = payload.get("request")
    if not isinstance(raw_request, dict) or not raw_request:
        raw_request = loads(row.request_json, {})
    request = GenerationRequest.model_validate(raw_request)
    counts = GenerationCounts.model_validate(
        payload.get("counts") or loads(row.counts_json, {}) or {}
    )
    scope_payload = payload.get("scope")
    if not isinstance(scope_payload, dict):
        scope_payload = {
            "syllabus_id": row.syllabus_id,
            "statement": "Batch scope was not recorded; only persisted questions are available.",
        }
    payload.update(
        {
            "batch_id": row.batch_id,
            "course_id": row.course_id,
            "status": row.status,
            "provider": row.provider,
            "model": row.model,
            "capability": row.capability,
            "request": request.model_dump(mode="json"),
            "counts": counts.model_dump(mode="json"),
            "scope": scope_payload,
            "parameters": payload.get("parameters") or loads(row.parameters_json, {}),
            "questions": [],
        }
    )
    return GenerationBatch.model_validate(payload)


def rejection_payload(items: list[GenerationRejection]) -> list[dict]:
    return [item.model_dump(mode="json") for item in items]


def duplicate_payload(items: list[DuplicateResolution]) -> list[dict]:
    return [item.model_dump(mode="json") for item in items]


__all__ = [
    "batch_to_row",
    "duplicate_payload",
    "question_to_row",
    "rejection_payload",
    "row_to_batch",
    "row_to_question",
]
