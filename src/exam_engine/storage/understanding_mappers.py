"""Model <-> row conversion for Phase 3 question-understanding tables."""

from __future__ import annotations

from datetime import datetime

from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    MappingStatus,
    QuestionType,
    SimilarityKind,
    UnderstandingStatus,
)
from exam_engine.models.question import Uncertainty
from exam_engine.models.understanding import (
    AssetReference,
    BloomLabel,
    DifficultyAssessment,
    QuestionAnalysis,
    SimilarityMatch,
    Subpart,
    TypeLabel,
    UnderstandingRunReport,
    UnitMatch,
)
from exam_engine.storage.mappers import dumps, iso, loads, parse_dt
from exam_engine.storage.schema import (
    QuestionAnalysisRow,
    QuestionBloomRow,
    QuestionDifficultyRow,
    QuestionSimilarityRow,
    QuestionTypeRow,
    QuestionUnitMatchRow,
    UnderstandingRunRow,
)


def _uncertainties(raw: list[Uncertainty]) -> str:
    return dumps([item.model_dump(mode="json") for item in raw])


def _parse_uncertainties(raw: str | None) -> list[Uncertainty]:
    return [Uncertainty.model_validate(item) for item in loads(raw, []) if isinstance(item, dict)]


def analysis_to_row(analysis: QuestionAnalysis) -> QuestionAnalysisRow:
    return QuestionAnalysisRow(
        analysis_id=analysis.analysis_id,
        question_id=analysis.question_id,
        course_id=analysis.course_id,
        document_id=analysis.document_id,
        text_sha256=analysis.text_sha256,
        word_count=analysis.word_count,
        sentence_count=analysis.sentence_count,
        subquestion_count=analysis.subquestion_count,
        subparts_json=dumps([item.model_dump(mode="json") for item in analysis.subparts]),
        references_json=dumps([item.model_dump(mode="json") for item in analysis.references]),
        mapping_status=analysis.mapping_status.value,
        mapping_confidence=analysis.mapping_confidence,
        top_unit_id=analysis.top_unit_id,
        primary_type=analysis.primary_type.value if analysis.primary_type else None,
        primary_type_confidence=analysis.primary_type_confidence,
        difficulty_label=analysis.difficulty_label.value,
        difficulty_score=analysis.difficulty_score,
        difficulty_confidence=analysis.difficulty_confidence,
        bloom_primary=analysis.bloom_primary.value if analysis.bloom_primary else None,
        bloom_confidence=analysis.bloom_confidence,
        structure_confidence=analysis.structure_confidence,
        uncertainties_json=_uncertainties(analysis.uncertainties),
        created_at=iso(analysis.created_at) or "",
    )


def _optional_enum(value: type, raw: str | None):
    if not raw:
        return None
    try:
        return value(raw)
    except ValueError:
        return None


def row_to_analysis(row: QuestionAnalysisRow, *, question_text: str = "") -> QuestionAnalysis:
    return QuestionAnalysis(
        analysis_id=row.analysis_id,
        question_id=row.question_id,
        course_id=row.course_id,
        document_id=row.document_id,
        text_sha256=row.text_sha256,
        word_count=row.word_count,
        sentence_count=row.sentence_count,
        subquestion_count=row.subquestion_count,
        subparts=[Subpart.model_validate(item) for item in loads(row.subparts_json, [])],
        references=[AssetReference.model_validate(item) for item in loads(row.references_json, [])],
        mapping_status=MappingStatus(row.mapping_status),
        mapping_confidence=row.mapping_confidence,
        top_unit_id=row.top_unit_id,
        primary_type=_optional_enum(QuestionType, row.primary_type),
        primary_type_confidence=row.primary_type_confidence,
        difficulty_label=DifficultyLabel(row.difficulty_label),
        difficulty_score=row.difficulty_score,
        difficulty_confidence=row.difficulty_confidence,
        bloom_primary=_optional_enum(BloomLevel, row.bloom_primary),
        bloom_confidence=row.bloom_confidence,
        structure_confidence=row.structure_confidence,
        uncertainties=_parse_uncertainties(row.uncertainties_json),
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
    )


def unit_match_to_row(match: UnitMatch) -> QuestionUnitMatchRow:
    return QuestionUnitMatchRow(
        match_id=match.match_id,
        question_id=match.question_id,
        course_id=match.course_id,
        unit_id=match.unit_id,
        syllabus_id=match.syllabus_id,
        unit_label=match.unit_label,
        unit_title=match.unit_title,
        topic=match.topic,
        score=match.score,
        rank=match.rank,
        method=match.method,
        matched_terms_json=dumps(match.matched_terms),
        evidence_json=dumps(match.evidence),
    )


def row_to_unit_match(row: QuestionUnitMatchRow) -> UnitMatch:
    return UnitMatch(
        match_id=row.match_id,
        question_id=row.question_id,
        course_id=row.course_id,
        unit_id=row.unit_id,
        syllabus_id=row.syllabus_id,
        unit_label=row.unit_label,
        unit_title=row.unit_title,
        topic=row.topic,
        score=row.score,
        rank=row.rank,
        method=row.method,
        matched_terms=loads(row.matched_terms_json, []),
        evidence=loads(row.evidence_json, []),
    )


def type_to_row(label: TypeLabel) -> QuestionTypeRow:
    return QuestionTypeRow(
        type_id=label.type_id,
        question_id=label.question_id,
        course_id=label.course_id,
        label=label.label.value,
        score=label.score,
        rank=label.rank,
        confidence=label.confidence,
        method=label.method,
        evidence_json=dumps(label.evidence),
    )


def row_to_type(row: QuestionTypeRow) -> TypeLabel:
    return TypeLabel(
        type_id=row.type_id,
        question_id=row.question_id,
        course_id=row.course_id,
        label=QuestionType(row.label),
        score=row.score,
        rank=row.rank,
        confidence=row.confidence,
        method=row.method,
        evidence=loads(row.evidence_json, []),
    )


def bloom_to_row(label: BloomLabel) -> QuestionBloomRow:
    return QuestionBloomRow(
        bloom_id=label.bloom_id,
        question_id=label.question_id,
        course_id=label.course_id,
        level=label.level.value,
        score=label.score,
        rank=label.rank,
        confidence=label.confidence,
        method=label.method,
        evidence_json=dumps(label.evidence),
    )


def row_to_bloom(row: QuestionBloomRow) -> BloomLabel:
    return BloomLabel(
        bloom_id=row.bloom_id,
        question_id=row.question_id,
        course_id=row.course_id,
        level=BloomLevel(row.level),
        score=row.score,
        rank=row.rank,
        confidence=row.confidence,
        method=row.method,
        evidence=loads(row.evidence_json, []),
    )


def difficulty_to_row(assessment: DifficultyAssessment) -> QuestionDifficultyRow:
    return QuestionDifficultyRow(
        difficulty_id=assessment.difficulty_id,
        question_id=assessment.question_id,
        course_id=assessment.course_id,
        label=assessment.label.value,
        score=assessment.score,
        confidence=assessment.confidence,
        method=assessment.method,
        estimated=1 if assessment.estimated else 0,
        features_json=dumps(assessment.features),
        explanation_json=dumps(assessment.explanation),
    )


def row_to_difficulty(row: QuestionDifficultyRow) -> DifficultyAssessment:
    return DifficultyAssessment(
        difficulty_id=row.difficulty_id,
        question_id=row.question_id,
        course_id=row.course_id,
        label=DifficultyLabel(row.label),
        score=row.score,
        confidence=row.confidence,
        method=row.method,
        estimated=bool(row.estimated),
        features=loads(row.features_json, {}),
        explanation=loads(row.explanation_json, []),
    )


def similarity_to_row(match: SimilarityMatch) -> QuestionSimilarityRow:
    return QuestionSimilarityRow(
        pair_id=match.pair_id,
        course_id=match.course_id,
        question_id=match.question_id,
        matched_question_id=match.matched_question_id,
        kind=match.kind.value,
        score=match.score,
        lexical_score=match.lexical_score,
        semantic_score=match.semantic_score,
        method=match.method,
        evidence_json=dumps(match.evidence),
        created_at=iso(match.created_at) or "",
    )


def row_to_similarity(row: QuestionSimilarityRow) -> SimilarityMatch:
    return SimilarityMatch(
        pair_id=row.pair_id,
        course_id=row.course_id,
        question_id=row.question_id,
        matched_question_id=row.matched_question_id,
        kind=SimilarityKind(row.kind),
        score=row.score,
        lexical_score=row.lexical_score,
        semantic_score=row.semantic_score,
        method=row.method,
        evidence=loads(row.evidence_json, []),
        created_at=parse_dt(row.created_at) or datetime.now().astimezone(),
    )


def run_to_row(report: UnderstandingRunReport) -> UnderstandingRunRow:
    return UnderstandingRunRow(
        run_id=report.run_id,
        course_id=report.course_id,
        status=report.status.value,
        started_at=iso(report.started_at) or "",
        finished_at=iso(report.finished_at),
        methods_json=dumps(report.methods),
        model_ids_json=dumps(report.model_ids),
        totals_json=dumps(report.totals),
        config_json=dumps(report.config),
        warnings_json=_uncertainties(report.warnings),
        errors_json=_uncertainties(report.errors),
        duration_ms=report.duration_ms,
    )


def row_to_run(row: UnderstandingRunRow) -> UnderstandingRunReport:
    return UnderstandingRunReport(
        run_id=row.run_id,
        course_id=row.course_id,
        status=UnderstandingStatus(row.status),
        started_at=parse_dt(row.started_at) or datetime.now().astimezone(),
        finished_at=parse_dt(row.finished_at),
        methods=loads(row.methods_json, []),
        model_ids=loads(row.model_ids_json, []),
        totals=loads(row.totals_json, {}),
        config=loads(row.config_json, {}),
        warnings=_parse_uncertainties(row.warnings_json),
        errors=_parse_uncertainties(row.errors_json),
        duration_ms=row.duration_ms,
    )


__all__ = [
    "analysis_to_row",
    "bloom_to_row",
    "difficulty_to_row",
    "row_to_analysis",
    "row_to_bloom",
    "row_to_difficulty",
    "row_to_run",
    "row_to_similarity",
    "row_to_type",
    "row_to_unit_match",
    "run_to_row",
    "similarity_to_row",
    "type_to_row",
    "unit_match_to_row",
]
