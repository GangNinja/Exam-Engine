from __future__ import annotations

from pathlib import Path

from exam_engine.config import (
    CoverageSettings,
    ExamDnaSettings,
    GraphSettings,
    IngestionLimits,
    PrerequisiteSettings,
    Settings,
    UnderstandingSettings,
)


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.db_path == Path("data") / "exam_engine.db"
    assert settings.ocr_provider is None
    assert settings.limits == IngestionLimits()
    assert settings.limits.min_question_confidence == 0.6
    assert settings.analytics.small_sample_questions == 5
    assert settings.analytics.priority_weights()["frequency"] == 0.30


def test_env_overrides() -> None:
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_DB": "tmp/custom.db",
            "EXAM_ENGINE_OCR_PROVIDER": "tesseract",
            "EXAM_ENGINE_MAX_FILE_MB": "2",
            "EXAM_ENGINE_MAX_PAGES": "10",
            "EXAM_ENGINE_MAX_DOC_CHARS": "5000",
            "EXAM_ENGINE_MAX_QUESTIONS": "50",
            "EXAM_ENGINE_MIN_QUESTION_CONFIDENCE": "0.75",
        }
    )
    assert settings.db_path == Path("tmp/custom.db")
    assert settings.ocr_provider == "tesseract"
    assert settings.limits.max_file_bytes == 2 * 1024 * 1024
    assert settings.limits.max_pages == 10
    assert settings.limits.max_document_chars == 5000
    assert settings.limits.max_questions_per_document == 50
    assert settings.limits.min_question_confidence == 0.75


def test_analytics_env_overrides() -> None:
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_Q4_MAX_QUESTIONS": "40",
            "EXAM_ENGINE_Q4_SMALL_SAMPLE": "3",
            "EXAM_ENGINE_Q4_LOW_CONFIDENCE": "0.65",
            "EXAM_ENGINE_Q4_MIN_TREND_PERIODS": "2",
            "EXAM_ENGINE_Q4_MIN_PERIOD_QUESTIONS": "2",
            "EXAM_ENGINE_Q4_SIMILARITY_THRESHOLD": "0",
            "EXAM_ENGINE_Q4_PRIORITY_FREQUENCY": "0.4",
        }
    )
    assert settings.analytics.max_questions == 40
    assert settings.analytics.small_sample_questions == 3
    assert settings.analytics.low_confidence_threshold == 0.65
    assert settings.analytics.similarity_threshold == 0.0
    assert settings.analytics.priority_frequency_weight == 0.4

    zero_weight = Settings.from_env({"EXAM_ENGINE_Q4_PRIORITY_RECENCY": "0"})
    assert zero_weight.analytics.priority_recency_weight == 0.0


def test_invalid_env_values_fall_back_to_defaults() -> None:
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_MAX_PAGES": "-4",
            "EXAM_ENGINE_MAX_FILE_MB": "lots",
            "EXAM_ENGINE_MIN_QUESTION_CONFIDENCE": "2",
            "EXAM_ENGINE_OCR_PROVIDER": "",
        }
    )
    assert settings.limits.max_pages == IngestionLimits().max_pages
    assert settings.limits.max_file_bytes == IngestionLimits().max_file_bytes
    assert settings.limits.min_question_confidence == 0.6
    assert settings.ocr_provider is None


def test_understanding_thresholds_are_configurable() -> None:
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_Q3_MAX_QUESTIONS": "100",
            "EXAM_ENGINE_Q3_MAX_SIMILARITY_QUESTIONS": "50",
            "EXAM_ENGINE_Q3_TAXONOMY": "custom.json",
            "EXAM_ENGINE_Q3_MAPPING_THRESHOLD": "0.30",
            "EXAM_ENGINE_Q3_MAPPING_MARGIN": "0.10",
            "EXAM_ENGINE_Q3_MAPPING_CANDIDATES": "4",
            "EXAM_ENGINE_Q3_MAPPING_SEMANTIC_WEIGHT": "0.50",
            "EXAM_ENGINE_Q3_TYPE_THRESHOLD": "0.40",
            "EXAM_ENGINE_Q3_BLOOM_THRESHOLD": "0.35",
            "EXAM_ENGINE_Q3_DIFFICULTY_EASY_BELOW": "0.25",
            "EXAM_ENGINE_Q3_DIFFICULTY_HARD_ABOVE": "0.70",
            "EXAM_ENGINE_Q3_SIMILARITY_NEAR": "0.80",
            "EXAM_ENGINE_Q3_SIMILARITY_SEMANTIC": "0.70",
            "EXAM_ENGINE_Q3_SIMILARITY_CONCEPT": "0.30",
            "EXAM_ENGINE_Q3_SIMILARITY_TOP_K": "3",
            "EXAM_ENGINE_Q3_SIMILARITY_CANDIDATES": "15",
        }
    )
    understanding = settings.understanding
    assert understanding.max_questions == 100
    assert understanding.max_similarity_questions == 50
    assert understanding.taxonomy_path == "custom.json"
    assert understanding.mapping_threshold == 0.30
    assert understanding.mapping_margin == 0.10
    assert understanding.mapping_max_candidates == 4
    assert understanding.mapping_semantic_weight == 0.50
    assert understanding.type_threshold == 0.40
    assert understanding.bloom_threshold == 0.35
    assert understanding.difficulty_easy_below == 0.25
    assert understanding.difficulty_hard_above == 0.70
    assert understanding.similarity_near_threshold == 0.80
    assert understanding.similarity_semantic_threshold == 0.70
    assert understanding.similarity_concept_threshold == 0.30
    assert understanding.similarity_top_k == 3
    assert understanding.similarity_max_candidates == 15

    invalid = Settings.from_env({"EXAM_ENGINE_Q3_MAPPING_THRESHOLD": "2"})
    assert invalid.understanding == UnderstandingSettings()


def test_graph_settings_are_configurable_and_conservative() -> None:
    assert Settings.from_env({}).graph == GraphSettings()
    assert Settings.from_env({}).graph.infer_prerequisite_candidates is False
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_Q5_MAX_QUESTIONS": "25",
            "EXAM_ENGINE_Q5_MAX_SIMILARITIES": "40",
            "EXAM_ENGINE_Q5_INCLUDE_INFERRED_CONCEPTS": "false",
            "EXAM_ENGINE_Q5_INCLUDE_SIMILARITIES": "0",
            "EXAM_ENGINE_Q5_INFER_PREREQUISITES": "1",
            "EXAM_ENGINE_Q5_CONCEPT_DUPLICATE_THRESHOLD": "0.9",
        }
    )
    assert settings.graph.max_questions == 25
    assert settings.graph.max_similarities == 40
    assert settings.graph.include_inferred_concepts is False
    assert settings.graph.include_similarities is False
    assert settings.graph.infer_prerequisite_candidates is True
    assert settings.graph.concept_duplicate_threshold == 0.9
    invalid = Settings.from_env({"EXAM_ENGINE_Q5_CONCEPT_DUPLICATE_THRESHOLD": "2"})
    assert invalid.graph.concept_duplicate_threshold == 0.80
    moved = Settings.from_env({}).with_graph(include_similarities=False)
    assert moved.graph.include_similarities is False


def test_prerequisite_settings_are_conservative_and_configurable() -> None:
    defaults = Settings.from_env({}).prerequisite
    assert defaults == PrerequisiteSettings()
    assert defaults.order_window == 2
    assert defaults.include_cross_unit_order is False
    assert defaults.cooccurrence_min_questions == 2
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_Q6_ORDER_WINDOW": "1",
            "EXAM_ENGINE_Q6_CROSS_UNIT_ORDER": "true",
            "EXAM_ENGINE_Q6_MAX_CANDIDATES": "3",
            "EXAM_ENGINE_Q6_COOCCURRENCE_MIN": "4",
            "EXAM_ENGINE_Q6_COOCCURRENCE_JACCARD": "0.5",
            "EXAM_ENGINE_Q6_PATH_MAX_DEPTH": "5",
        }
    )
    assert settings.prerequisite.order_window == 1
    assert settings.prerequisite.include_cross_unit_order is True
    assert settings.prerequisite.max_candidates_per_concept == 3
    assert settings.prerequisite.cooccurrence_min_questions == 4
    assert settings.prerequisite.cooccurrence_min_jaccard == 0.5
    assert settings.prerequisite.path_max_depth == 5
    invalid = Settings.from_env({"EXAM_ENGINE_Q6_COOCCURRENCE_JACCARD": "2"})
    assert invalid.prerequisite.cooccurrence_min_jaccard == 0.34
    moved = Settings.from_env({}).with_prerequisite(order_window=1)
    assert moved.prerequisite.order_window == 1
    assert Settings.from_env({}).prerequisite.order_window == 2


def test_coverage_settings_are_configurable_and_bounded() -> None:
    defaults = Settings.from_env({}).coverage
    assert defaults == CoverageSettings()
    assert defaults.covered_min_questions == 2
    assert defaults.mapping_confidence_floor == 0.18
    assert defaults.small_corpus_questions == 5
    assert defaults.include_graph_explicit_mappings is True
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_Q7_MAX_QUESTIONS": "40",
            "EXAM_ENGINE_Q7_COVERED_MIN_QUESTIONS": "3",
            "EXAM_ENGINE_Q7_LOW_CONFIDENCE": "0.6",
            "EXAM_ENGINE_Q7_MAPPING_CONFIDENCE_FLOOR": "0.25",
            "EXAM_ENGINE_Q7_SMALL_CORPUS": "8",
            "EXAM_ENGINE_Q7_MIN_ANALYSIS_COVERAGE": "0.9",
            "EXAM_ENGINE_Q7_GRAPH_EXPLICIT_MAPPINGS": "false",
        }
    )
    assert settings.coverage.max_questions == 40
    assert settings.coverage.covered_min_questions == 3
    assert settings.coverage.low_confidence_threshold == 0.6
    assert settings.coverage.mapping_confidence_floor == 0.25
    assert settings.coverage.small_corpus_questions == 8
    assert settings.coverage.min_analysis_coverage == 0.9
    assert settings.coverage.include_graph_explicit_mappings is False
    invalid = Settings.from_env({"EXAM_ENGINE_Q7_LOW_CONFIDENCE": "2"})
    assert invalid.coverage.low_confidence_threshold == 0.5
    moved = Settings.from_env({}).with_coverage(covered_min_questions=4)
    assert moved.coverage.covered_min_questions == 4
    assert Settings.from_env({}).coverage.covered_min_questions == 2


def test_exam_dna_settings_are_configurable() -> None:
    defaults = Settings.from_env({}).exam_dna
    assert defaults == ExamDnaSettings()
    assert defaults.min_papers_for_recurrence == 3
    assert defaults.duplicate_signature_min_questions == 3
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_Q8_MAX_QUESTIONS": "30",
            "EXAM_ENGINE_Q8_MIN_PAPERS_FOR_RECURRENCE": "4",
            "EXAM_ENGINE_Q8_RECURRENCE_THRESHOLD": "0.7",
            "EXAM_ENGINE_Q8_DOMINANT_UNIT_SHARE": "0.4",
            "EXAM_ENGINE_Q8_MIN_CONCEPT_PAPERS": "3",
            "EXAM_ENGINE_Q8_DUPLICATE_MIN_QUESTIONS": "5",
            "EXAM_ENGINE_Q8_SIMILARITY_MIN_SCORE": "0.6",
            "EXAM_ENGINE_Q8_MAX_PATTERNS": "40",
            "EXAM_ENGINE_Q8_MAX_GROUPS": "25",
            "EXAM_ENGINE_Q8_LOW_CONFIDENCE": "0.7",
        }
    )
    assert settings.exam_dna.max_questions == 30
    assert settings.exam_dna.min_papers_for_recurrence == 4
    assert settings.exam_dna.recurrence_threshold == 0.7
    assert settings.exam_dna.dominant_unit_marks_share == 0.4
    assert settings.exam_dna.min_concept_paper_occurrences == 3
    assert settings.exam_dna.duplicate_signature_min_questions == 5
    assert settings.exam_dna.similarity_min_score == 0.6
    assert settings.exam_dna.max_patterns == 40
    assert settings.exam_dna.max_question_groups == 25
    assert settings.exam_dna.low_confidence_threshold == 0.7
    invalid = Settings.from_env({"EXAM_ENGINE_Q8_RECURRENCE_THRESHOLD": "2"})
    assert invalid.exam_dna.recurrence_threshold == 0.5
    moved = Settings.from_env({}).with_exam_dna(min_papers_for_recurrence=2)
    assert moved.exam_dna.min_papers_for_recurrence == 2
    assert Settings.from_env({}).exam_dna.min_papers_for_recurrence == 3


def test_with_db_and_limits_helpers() -> None:
    settings = Settings.from_env({})
    moved = settings.with_db("other.db")
    assert moved.db_path == Path("other.db")
    assert settings.db_path == Path("data") / "exam_engine.db"
    tightened = settings.with_limits(max_pages=3)
    assert tightened.limits.max_pages == 3
    assert settings.limits.max_pages == 500

    analytics = settings.with_analytics(max_questions=7)
    assert analytics.analytics.max_questions == 7
    assert settings.analytics.max_questions == 20000
