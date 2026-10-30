"""Model <-> row conversion for Phase 11 response and misconception tables."""

from __future__ import annotations

from datetime import datetime

from exam_engine.models.advanced import (
    MisconceptionEvidence,
    MisconceptionRecord,
    ResponseRecord,
)
from exam_engine.models.enums import (
    MisconceptionKind,
    MisconceptionStatus,
    ResponseCorrectness,
    SupportLevel,
)
from exam_engine.models.generation import ReviewEvent
from exam_engine.storage.mappers import dumps, iso, loads, parse_dt
from exam_engine.storage.schema import MisconceptionRow, StudentResponseRow


def response_to_row(record: ResponseRecord) -> StudentResponseRow:
    return StudentResponseRow(
        response_id=record.response_id,
        course_id=record.course_id,
        syllabus_id=record.syllabus_id,
        question_id=record.question_id,
        answer_text=record.answer_text,
        correctness=record.correctness.value,
        marks_awarded=record.marks_awarded,
        marks_available=record.marks_available,
        group_ref=record.group_ref,
        identified=record.identified,
        source=record.source,
        fingerprint=record.fingerprint,
        imported_at=iso(record.imported_at) or "",
    )


def row_to_response(row: StudentResponseRow) -> ResponseRecord:
    return ResponseRecord(
        response_id=row.response_id,
        course_id=row.course_id,
        syllabus_id=row.syllabus_id,
        question_id=row.question_id,
        answer_text=row.answer_text,
        correctness=ResponseCorrectness(row.correctness),
        marks_awarded=row.marks_awarded,
        marks_available=row.marks_available,
        group_ref=row.group_ref,
        identified=bool(row.identified),
        source=row.source,
        fingerprint=row.fingerprint,
        imported_at=parse_dt(row.imported_at) or datetime.now().astimezone(),
    )


def misconception_to_row(record: MisconceptionRecord) -> MisconceptionRow:
    return MisconceptionRow(
        misconception_id=record.misconception_id,
        schema_version=record.schema_version,
        course_id=record.course_id,
        syllabus_id=record.syllabus_id,
        kind=record.kind.value,
        label=record.label,
        description=record.description,
        concept_ids_json=dumps(record.concept_ids),
        concept_labels_json=dumps(record.concept_labels),
        question_ids_json=dumps(record.question_ids),
        evidence_json=dumps([item.model_dump(mode="json") for item in record.evidence]),
        support_count=record.support_count,
        distinct_questions=record.distinct_questions,
        distinct_groups=record.distinct_groups,
        detection_method=record.detection_method,
        support_level=record.support_level.value,
        confidence=record.confidence,
        suggested_correction=record.suggested_correction,
        follow_up_json=dumps(record.follow_up_questions),
        review_status=record.review_status.value,
        review_history_json=dumps([item.model_dump(mode="json") for item in record.review_history]),
        limitations_json=dumps(record.limitations),
        created_at=iso(record.created_at) or "",
        updated_at=iso(record.updated_at) or "",
    )


def row_to_misconception(row: MisconceptionRow) -> MisconceptionRecord:
    return MisconceptionRecord(
        schema_version=row.schema_version,
        misconception_id=row.misconception_id,
        course_id=row.course_id,
        syllabus_id=row.syllabus_id,
        kind=MisconceptionKind(row.kind),
        label=row.label,
        description=row.description,
        concept_ids=loads(row.concept_ids_json, []),
        concept_labels=loads(row.concept_labels_json, []),
        question_ids=loads(row.question_ids_json, []),
        evidence=[
            MisconceptionEvidence.model_validate(item)
            for item in loads(row.evidence_json, [])
            if isinstance(item, dict)
        ],
        support_count=row.support_count,
        distinct_questions=row.distinct_questions,
        distinct_groups=row.distinct_groups,
        detection_method=row.detection_method,
        support_level=SupportLevel(row.support_level),
        confidence=row.confidence,
        suggested_correction=row.suggested_correction,
        follow_up_questions=loads(row.follow_up_json, []),
        review_status=MisconceptionStatus(row.review_status),
        review_history=[
            ReviewEvent.model_validate(item)
            for item in loads(row.review_history_json, [])
            if isinstance(item, dict)
        ],
        limitations=loads(row.limitations_json, []),
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
        updated_at=parse_dt(row.updated_at) or datetime.now().astimezone(),
    )


__all__ = [
    "misconception_to_row",
    "response_to_row",
    "row_to_misconception",
    "row_to_response",
]
