"""Configuration and ingestion guardrails.

Values come from the environment so nothing depends on hard-coded machine paths.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IngestionLimits:
    """Safety limits applied to every ingested file."""

    max_file_bytes: int = 50 * 1024 * 1024
    max_pages: int = 500
    max_document_chars: int = 2_000_000
    max_questions_per_document: int = 2_000
    min_page_chars: int = 40
    min_question_chars: int = 8
    min_question_confidence: float = 0.6
    ocr_dpi: int = 200


DEFAULT_DB_RELPATH = Path("data") / "exam_engine.db"
DEFAULT_ST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@dataclass(frozen=True)
class NlpSettings:
    """Phase 2 analysis settings.

    Defaults are chosen so a fresh checkout works offline with no model
    downloads; the neural embedding provider is opt-in.
    """

    embedding_provider: str = "auto"
    sentence_transformer_model: str = DEFAULT_ST_MODEL
    hashing_dim: int = 256
    lsa_dim: int = 128
    top_k: int = 20
    topics_k: int = 5
    keyword_method: str = "tfidf"
    entity_extractor: str = "rule_based"
    spacy_model: str = "en_core_web_sm"
    max_documents: int = 200
    max_sections: int = 200
    language: str = "en"
    allow_model_download: bool = False
    lemmatize: bool = False
    strip_stopwords: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> NlpSettings:
        env = os.environ if env is None else env
        return cls(
            embedding_provider=_str_env(env, "EXAM_ENGINE_EMBEDDING_PROVIDER", "auto"),
            sentence_transformer_model=_str_env(
                env, "EXAM_ENGINE_EMBEDDING_MODEL", DEFAULT_ST_MODEL
            ),
            hashing_dim=_int_env(env, "EXAM_ENGINE_EMBEDDING_DIM", 256),
            lsa_dim=_int_env(env, "EXAM_ENGINE_LSA_DIM", 128),
            top_k=_int_env(env, "EXAM_ENGINE_NLP_TOP_K", 20),
            topics_k=_int_env(env, "EXAM_ENGINE_NLP_TOPICS_K", 5),
            keyword_method=_str_env(env, "EXAM_ENGINE_NLP_KEYWORDS", "tfidf"),
            entity_extractor=_str_env(env, "EXAM_ENGINE_NLP_ENTITIES", "rule_based"),
            spacy_model=_str_env(env, "EXAM_ENGINE_SPACY_MODEL", "en_core_web_sm"),
            max_documents=_int_env(env, "EXAM_ENGINE_NLP_MAX_DOCUMENTS", 200),
            max_sections=_int_env(env, "EXAM_ENGINE_NLP_MAX_SECTIONS", 200),
            language=_str_env(env, "EXAM_ENGINE_NLP_LANGUAGE", "en"),
            allow_model_download=_bool_env(env, "EXAM_ENGINE_ALLOW_MODEL_DOWNLOAD", False),
            lemmatize=_bool_env(env, "EXAM_ENGINE_NLP_LEMMATIZE", False),
            strip_stopwords=_bool_env(env, "EXAM_ENGINE_NLP_STRIP_STOPWORDS", True),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class UnderstandingSettings:
    """Phase 3 question-understanding thresholds and limits.

    All thresholds are deliberately explicit: a candidate below the threshold
    stays unmapped or unclassified instead of being forced into a label.
    """

    max_questions: int = 2000
    max_similarity_questions: int = 2000
    taxonomy_path: str | None = None
    mapping_threshold: float = 0.18
    mapping_margin: float = 0.04
    mapping_max_candidates: int = 3
    mapping_semantic_weight: float = 0.6
    type_threshold: float = 0.30
    bloom_threshold: float = 0.30
    difficulty_easy_below: float = 0.32
    difficulty_hard_above: float = 0.60
    similarity_near_threshold: float = 0.78
    similarity_semantic_threshold: float = 0.68
    similarity_concept_threshold: float = 0.35
    similarity_top_k: int = 5
    similarity_max_candidates: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> UnderstandingSettings:
        env = os.environ if env is None else env
        return cls(
            max_questions=_int_env(env, "EXAM_ENGINE_Q3_MAX_QUESTIONS", 2000),
            max_similarity_questions=_int_env(env, "EXAM_ENGINE_Q3_MAX_SIMILARITY_QUESTIONS", 2000),
            taxonomy_path=(env.get("EXAM_ENGINE_Q3_TAXONOMY") or None),
            mapping_threshold=_float_env(env, "EXAM_ENGINE_Q3_MAPPING_THRESHOLD", 0.18),
            mapping_margin=_float_env(env, "EXAM_ENGINE_Q3_MAPPING_MARGIN", 0.04),
            mapping_max_candidates=_int_env(env, "EXAM_ENGINE_Q3_MAPPING_CANDIDATES", 3),
            mapping_semantic_weight=_float_env(env, "EXAM_ENGINE_Q3_MAPPING_SEMANTIC_WEIGHT", 0.6),
            type_threshold=_float_env(env, "EXAM_ENGINE_Q3_TYPE_THRESHOLD", 0.30),
            bloom_threshold=_float_env(env, "EXAM_ENGINE_Q3_BLOOM_THRESHOLD", 0.30),
            difficulty_easy_below=_float_env(env, "EXAM_ENGINE_Q3_DIFFICULTY_EASY_BELOW", 0.32),
            difficulty_hard_above=_float_env(env, "EXAM_ENGINE_Q3_DIFFICULTY_HARD_ABOVE", 0.60),
            similarity_near_threshold=_float_env(env, "EXAM_ENGINE_Q3_SIMILARITY_NEAR", 0.78),
            similarity_semantic_threshold=_float_env(
                env, "EXAM_ENGINE_Q3_SIMILARITY_SEMANTIC", 0.68
            ),
            similarity_concept_threshold=_float_env(env, "EXAM_ENGINE_Q3_SIMILARITY_CONCEPT", 0.35),
            similarity_top_k=_int_env(env, "EXAM_ENGINE_Q3_SIMILARITY_TOP_K", 5),
            similarity_max_candidates=_int_env(env, "EXAM_ENGINE_Q3_SIMILARITY_CANDIDATES", 30),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class AnalyticsSettings:
    """Phase 4 analytics limits, confidence thresholds and ranking weights.

    Weights are relative and are renormalised over the factors that are actually
    available. No factor is silently replaced with an invented value.
    """

    max_questions: int = 20000
    small_sample_questions: int = 5
    low_confidence_threshold: float = 0.5
    min_trend_periods: int = 2
    min_period_questions: int = 3
    similarity_threshold: float = 0.0
    priority_frequency_weight: float = 0.30
    priority_recency_weight: float = 0.20
    priority_marks_weight: float = 0.20
    priority_coverage_weight: float = 0.20
    priority_confidence_weight: float = 0.10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AnalyticsSettings:
        env = os.environ if env is None else env
        return cls(
            max_questions=_int_env(env, "EXAM_ENGINE_Q4_MAX_QUESTIONS", 20000),
            small_sample_questions=_int_env(env, "EXAM_ENGINE_Q4_SMALL_SAMPLE", 5),
            low_confidence_threshold=_float_env(env, "EXAM_ENGINE_Q4_LOW_CONFIDENCE", 0.5),
            min_trend_periods=_int_env(env, "EXAM_ENGINE_Q4_MIN_TREND_PERIODS", 2),
            min_period_questions=_int_env(env, "EXAM_ENGINE_Q4_MIN_PERIOD_QUESTIONS", 3),
            similarity_threshold=_zero_to_one_env(env, "EXAM_ENGINE_Q4_SIMILARITY_THRESHOLD", 0.0),
            priority_frequency_weight=_zero_to_one_env(
                env, "EXAM_ENGINE_Q4_PRIORITY_FREQUENCY", 0.30
            ),
            priority_recency_weight=_zero_to_one_env(env, "EXAM_ENGINE_Q4_PRIORITY_RECENCY", 0.20),
            priority_marks_weight=_zero_to_one_env(env, "EXAM_ENGINE_Q4_PRIORITY_MARKS", 0.20),
            priority_coverage_weight=_zero_to_one_env(
                env, "EXAM_ENGINE_Q4_PRIORITY_COVERAGE", 0.20
            ),
            priority_confidence_weight=_zero_to_one_env(
                env, "EXAM_ENGINE_Q4_PRIORITY_CONFIDENCE", 0.10
            ),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def priority_weights(self) -> dict[str, float]:
        return {
            "frequency": self.priority_frequency_weight,
            "recency": self.priority_recency_weight,
            "marks": self.priority_marks_weight,
            "coverage": self.priority_coverage_weight,
            "confidence": self.priority_confidence_weight,
        }


@dataclass(frozen=True)
class GraphSettings:
    """Phase 5 graph construction limits and conservative inference switches."""

    max_questions: int = 20000
    max_similarities: int = 50000
    include_inferred_concepts: bool = True
    include_similarities: bool = True
    infer_prerequisite_candidates: bool = False
    concept_duplicate_threshold: float = 0.80

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GraphSettings:
        env = os.environ if env is None else env
        return cls(
            max_questions=_int_env(env, "EXAM_ENGINE_Q5_MAX_QUESTIONS", 20000),
            max_similarities=_int_env(env, "EXAM_ENGINE_Q5_MAX_SIMILARITIES", 50000),
            include_inferred_concepts=_bool_env(
                env, "EXAM_ENGINE_Q5_INCLUDE_INFERRED_CONCEPTS", True
            ),
            include_similarities=_bool_env(env, "EXAM_ENGINE_Q5_INCLUDE_SIMILARITIES", True),
            infer_prerequisite_candidates=_bool_env(
                env, "EXAM_ENGINE_Q5_INFER_PREREQUISITES", False
            ),
            concept_duplicate_threshold=_zero_to_one_env(
                env, "EXAM_ENGINE_Q5_CONCEPT_DUPLICATE_THRESHOLD", 0.80
            ),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class PrerequisiteSettings:
    """Phase 6 discovery limits and evidence switches.

    Values are deliberately conservative: missing evidence produces no edge,
    while weak evidence produces a reviewable candidate rather than a fact.
    """

    order_window: int = 2
    include_cross_unit_order: bool = False
    max_candidates_per_concept: int = 6
    cooccurrence_min_questions: int = 2
    cooccurrence_min_jaccard: float = 0.34
    path_max_depth: int = 8

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PrerequisiteSettings:
        env = os.environ if env is None else env
        return cls(
            order_window=_int_env(env, "EXAM_ENGINE_Q6_ORDER_WINDOW", 2),
            include_cross_unit_order=_bool_env(env, "EXAM_ENGINE_Q6_CROSS_UNIT_ORDER", False),
            max_candidates_per_concept=_int_env(env, "EXAM_ENGINE_Q6_MAX_CANDIDATES", 6),
            cooccurrence_min_questions=_int_env(env, "EXAM_ENGINE_Q6_COOCCURRENCE_MIN", 2),
            cooccurrence_min_jaccard=_zero_to_one_env(
                env, "EXAM_ENGINE_Q6_COOCCURRENCE_JACCARD", 0.34
            ),
            path_max_depth=_int_env(env, "EXAM_ENGINE_Q6_PATH_MAX_DEPTH", 8),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class CoverageSettings:
    """Phase 7 coverage thresholds.

    A single question is observed evidence but not broad coverage; concepts below
    the configured minimum are reported as ``low_coverage`` rather than unmapped.
    The confidence floor is applied to already persisted Phase 3 candidates, so
    Phase 7 never invents a mapping the earlier phase did not propose.
    """

    max_questions: int = 20000
    covered_min_questions: int = 2
    low_confidence_threshold: float = 0.5
    mapping_confidence_floor: float = 0.18
    small_corpus_questions: int = 5
    min_analysis_coverage: float = 0.8
    include_graph_explicit_mappings: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CoverageSettings:
        env = os.environ if env is None else env
        return cls(
            max_questions=_int_env(env, "EXAM_ENGINE_Q7_MAX_QUESTIONS", 20000),
            covered_min_questions=_int_env(env, "EXAM_ENGINE_Q7_COVERED_MIN_QUESTIONS", 2),
            low_confidence_threshold=_float_env(env, "EXAM_ENGINE_Q7_LOW_CONFIDENCE", 0.5),
            mapping_confidence_floor=_float_env(
                env, "EXAM_ENGINE_Q7_MAPPING_CONFIDENCE_FLOOR", 0.18
            ),
            small_corpus_questions=_int_env(env, "EXAM_ENGINE_Q7_SMALL_CORPUS", 5),
            min_analysis_coverage=_float_env(env, "EXAM_ENGINE_Q7_MIN_ANALYSIS_COVERAGE", 0.8),
            include_graph_explicit_mappings=_bool_env(
                env, "EXAM_ENGINE_Q7_GRAPH_EXPLICIT_MAPPINGS", True
            ),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ExamDnaSettings:
    """Phase 8 historical pattern thresholds.

    A pattern is only called recurring when it clears both the paper minimum and
    the recurrence-rate threshold. Similarity reuse never promotes a stored
    candidate pair into a claim of duplicated wording.
    """

    max_questions: int = 20000
    min_papers_for_recurrence: int = 3
    recurrence_threshold: float = 0.5
    dominant_unit_marks_share: float = 0.25
    min_concept_paper_occurrences: int = 2
    duplicate_signature_min_questions: int = 3
    similarity_min_score: float = 0.0
    max_patterns: int = 500
    max_question_groups: int = 200
    low_confidence_threshold: float = 0.5

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ExamDnaSettings:
        env = os.environ if env is None else env
        return cls(
            max_questions=_int_env(env, "EXAM_ENGINE_Q8_MAX_QUESTIONS", 20000),
            min_papers_for_recurrence=_int_env(env, "EXAM_ENGINE_Q8_MIN_PAPERS_FOR_RECURRENCE", 3),
            recurrence_threshold=_zero_to_one_env(env, "EXAM_ENGINE_Q8_RECURRENCE_THRESHOLD", 0.5),
            dominant_unit_marks_share=_zero_to_one_env(
                env, "EXAM_ENGINE_Q8_DOMINANT_UNIT_SHARE", 0.25
            ),
            min_concept_paper_occurrences=_int_env(env, "EXAM_ENGINE_Q8_MIN_CONCEPT_PAPERS", 2),
            duplicate_signature_min_questions=_int_env(
                env, "EXAM_ENGINE_Q8_DUPLICATE_MIN_QUESTIONS", 3
            ),
            similarity_min_score=_zero_to_one_env(env, "EXAM_ENGINE_Q8_SIMILARITY_MIN_SCORE", 0.0),
            max_patterns=_int_env(env, "EXAM_ENGINE_Q8_MAX_PATTERNS", 500),
            max_question_groups=_int_env(env, "EXAM_ENGINE_Q8_MAX_GROUPS", 200),
            low_confidence_threshold=_float_env(env, "EXAM_ENGINE_Q8_LOW_CONFIDENCE", 0.5),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class EvolutionSettings:
    """Phase 9 concept-evolution thresholds and safety limits.

    Context comparison is lexical and threshold-based; it is a review signal, not
    a semantic verdict. Assessment change detection keeps raw counts and shares
    separate and only fires when both windows clear the sample minimum.
    """

    max_documents: int = 2000
    max_concepts: int = 500
    max_observations_per_concept: int = 200
    max_changes: int = 1000
    context_similarity_threshold: float = 0.55
    min_change_confidence: float = 0.5
    min_dated_observations: int = 2
    min_syllabus_confidence: float = 0.6
    min_assessment_questions: int = 2
    assessment_share_delta: float = 0.25
    max_excerpt_chars: int = 240
    include_exam_dna: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EvolutionSettings:
        env = os.environ if env is None else env
        return cls(
            max_documents=_int_env(env, "EXAM_ENGINE_Q9_MAX_DOCUMENTS", 2000),
            max_concepts=_int_env(env, "EXAM_ENGINE_Q9_MAX_CONCEPTS", 500),
            max_observations_per_concept=_int_env(env, "EXAM_ENGINE_Q9_MAX_OBSERVATIONS", 200),
            max_changes=_int_env(env, "EXAM_ENGINE_Q9_MAX_CHANGES", 1000),
            context_similarity_threshold=_zero_to_one_env(
                env, "EXAM_ENGINE_Q9_CONTEXT_SIMILARITY", 0.55
            ),
            min_change_confidence=_zero_to_one_env(
                env, "EXAM_ENGINE_Q9_MIN_CHANGE_CONFIDENCE", 0.5
            ),
            min_dated_observations=_int_env(env, "EXAM_ENGINE_Q9_MIN_DATED_OBSERVATIONS", 2),
            min_syllabus_confidence=_zero_to_one_env(
                env, "EXAM_ENGINE_Q9_MIN_SYLLABUS_CONFIDENCE", 0.6
            ),
            min_assessment_questions=_int_env(env, "EXAM_ENGINE_Q9_MIN_ASSESSMENT_QUESTIONS", 2),
            assessment_share_delta=_zero_to_one_env(
                env, "EXAM_ENGINE_Q9_ASSESSMENT_SHARE_DELTA", 0.25
            ),
            max_excerpt_chars=_int_env(env, "EXAM_ENGINE_Q9_MAX_EXCERPT_CHARS", 240),
            include_exam_dna=_bool_env(env, "EXAM_ENGINE_Q9_INCLUDE_EXAM_DNA", True),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class GenerationSettings:
    """Phase 10 generation limits, validation thresholds and provider settings.

    The deterministic template provider is the default, so no paid API is required.
    An OpenAI-compatible provider is opt-in and reads its credential from the
    environment; the key is never stored in configuration or reports.
    """

    max_questions: int = 20
    max_mcq_options: int = 6
    min_mcq_options: int = 3
    max_evidence_passages: int = 4
    max_evidence_chars: int = 240
    max_related_labels: int = 4
    duplicate_near_threshold: float = 0.78
    duplicate_semantic_threshold: float = 0.68
    provider_timeout_seconds: float = 20.0
    provider_max_retries: int = 2
    provider_retry_backoff_seconds: float = 0.5
    require_evidence_for_answer: bool = True
    llm_base_url: str | None = None
    llm_model: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GenerationSettings:
        env = os.environ if env is None else env
        return cls(
            max_questions=_int_env(env, "EXAM_ENGINE_Q10_MAX_QUESTIONS", 20),
            max_mcq_options=_int_env(env, "EXAM_ENGINE_Q10_MAX_MCQ_OPTIONS", 6),
            min_mcq_options=_int_env(env, "EXAM_ENGINE_Q10_MIN_MCQ_OPTIONS", 3),
            max_evidence_passages=_int_env(env, "EXAM_ENGINE_Q10_MAX_EVIDENCE", 4),
            max_evidence_chars=_int_env(env, "EXAM_ENGINE_Q10_MAX_EVIDENCE_CHARS", 240),
            max_related_labels=_int_env(env, "EXAM_ENGINE_Q10_MAX_RELATED", 4),
            duplicate_near_threshold=_zero_to_one_env(env, "EXAM_ENGINE_Q10_DUPLICATE_NEAR", 0.78),
            duplicate_semantic_threshold=_zero_to_one_env(
                env, "EXAM_ENGINE_Q10_DUPLICATE_SEMANTIC", 0.68
            ),
            provider_timeout_seconds=_positive_float_env(
                env, "EXAM_ENGINE_Q10_PROVIDER_TIMEOUT", 20.0
            ),
            provider_max_retries=_non_negative_int_env(env, "EXAM_ENGINE_Q10_PROVIDER_RETRIES", 2),
            provider_retry_backoff_seconds=_non_negative_float_env(
                env, "EXAM_ENGINE_Q10_RETRY_BACKOFF", 0.5
            ),
            require_evidence_for_answer=_bool_env(
                env, "EXAM_ENGINE_Q10_REQUIRE_ANSWER_EVIDENCE", True
            ),
            llm_base_url=(env.get("EXAM_ENGINE_Q10_LLM_BASE_URL") or None),
            llm_model=(env.get("EXAM_ENGINE_Q10_LLM_MODEL") or None),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class AdvancedSettings:
    """Phase 11 limits and evidence thresholds.

    Mutation and adversarial generation reuse Phase 10 validation, so these limits
    describe how many candidates are attempted and how much response evidence a
    misconception pattern needs before it may be called recurring.
    """

    max_variants: int = 10
    max_responses: int = 5000
    min_support_for_candidate: int = 2
    min_support_for_recurring: int = 3
    min_distinct_questions_for_recurring: int = 2
    min_answer_words: int = 3
    excerpt_chars: int = 240
    min_reference_overlap: float = 0.08
    max_evidence_per_record: int = 6

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AdvancedSettings:
        env = os.environ if env is None else env
        return cls(
            max_variants=_int_env(env, "EXAM_ENGINE_Q11_MAX_VARIANTS", 10),
            max_responses=_int_env(env, "EXAM_ENGINE_Q11_MAX_RESPONSES", 5000),
            min_support_for_candidate=_int_env(env, "EXAM_ENGINE_Q11_MIN_SUPPORT", 2),
            min_support_for_recurring=_int_env(env, "EXAM_ENGINE_Q11_RECURRING_SUPPORT", 3),
            min_distinct_questions_for_recurring=_int_env(
                env, "EXAM_ENGINE_Q11_RECURRING_QUESTIONS", 2
            ),
            min_answer_words=_int_env(env, "EXAM_ENGINE_Q11_MIN_ANSWER_WORDS", 3),
            excerpt_chars=_int_env(env, "EXAM_ENGINE_Q11_EXCERPT_CHARS", 240),
            min_reference_overlap=_zero_to_one_env(
                env, "EXAM_ENGINE_Q11_MIN_REFERENCE_OVERLAP", 0.08
            ),
            max_evidence_per_record=_int_env(env, "EXAM_ENGINE_Q11_MAX_EVIDENCE", 6),
        )

    def as_config(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from explicit arguments or environment variables."""

    db_path: Path = DEFAULT_DB_RELPATH
    ocr_provider: str | None = None
    limits: IngestionLimits = IngestionLimits()
    nlp: NlpSettings = NlpSettings()
    understanding: UnderstandingSettings = UnderstandingSettings()
    analytics: AnalyticsSettings = AnalyticsSettings()
    graph: GraphSettings = GraphSettings()
    prerequisite: PrerequisiteSettings = PrerequisiteSettings()
    coverage: CoverageSettings = CoverageSettings()
    exam_dna: ExamDnaSettings = ExamDnaSettings()
    evolution: EvolutionSettings = EvolutionSettings()
    generation: GenerationSettings = GenerationSettings()
    advanced: AdvancedSettings = AdvancedSettings()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        limits = IngestionLimits(
            max_file_bytes=_int_env(env, "EXAM_ENGINE_MAX_FILE_MB", 50) * 1024 * 1024,
            max_pages=_int_env(env, "EXAM_ENGINE_MAX_PAGES", 500),
            max_document_chars=_int_env(env, "EXAM_ENGINE_MAX_DOC_CHARS", 2_000_000),
            max_questions_per_document=_int_env(env, "EXAM_ENGINE_MAX_QUESTIONS", 2_000),
            min_question_confidence=_float_env(env, "EXAM_ENGINE_MIN_QUESTION_CONFIDENCE", 0.6),
        )
        return cls(
            db_path=Path(env.get("EXAM_ENGINE_DB", DEFAULT_DB_RELPATH)),
            ocr_provider=env.get("EXAM_ENGINE_OCR_PROVIDER") or None,
            limits=limits,
            nlp=NlpSettings.from_env(env),
            understanding=UnderstandingSettings.from_env(env),
            analytics=AnalyticsSettings.from_env(env),
            graph=GraphSettings.from_env(env),
            prerequisite=PrerequisiteSettings.from_env(env),
            coverage=CoverageSettings.from_env(env),
            exam_dna=ExamDnaSettings.from_env(env),
            evolution=EvolutionSettings.from_env(env),
            generation=GenerationSettings.from_env(env),
            advanced=AdvancedSettings.from_env(env),
        )

    def with_db(self, db_path: Path | str | None) -> Settings:
        if db_path is None:
            return self
        return replace(self, db_path=Path(db_path))

    def with_limits(self, **changes: Any) -> Settings:
        return replace(self, limits=replace(self.limits, **changes))

    def with_nlp(self, **changes: Any) -> Settings:
        return replace(self, nlp=replace(self.nlp, **changes))

    def with_understanding(self, **changes: Any) -> Settings:
        return replace(self, understanding=replace(self.understanding, **changes))

    def with_analytics(self, **changes: Any) -> Settings:
        return replace(self, analytics=replace(self.analytics, **changes))

    def with_graph(self, **changes: Any) -> Settings:
        return replace(self, graph=replace(self.graph, **changes))

    def with_prerequisite(self, **changes: Any) -> Settings:
        return replace(self, prerequisite=replace(self.prerequisite, **changes))

    def with_coverage(self, **changes: Any) -> Settings:
        return replace(self, coverage=replace(self.coverage, **changes))

    def with_evolution(self, **changes: Any) -> Settings:
        return replace(self, evolution=replace(self.evolution, **changes))

    def with_exam_dna(self, **changes: Any) -> Settings:
        return replace(self, exam_dna=replace(self.exam_dna, **changes))

    def with_generation(self, **changes: Any) -> Settings:
        return replace(self, generation=replace(self.generation, **changes))

    def with_advanced(self, **changes: Any) -> Settings:
        return replace(self, advanced=replace(self.advanced, **changes))


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if 0.0 < value <= 1.0 else default


def _zero_to_one_env(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if 0.0 <= value <= 1.0 else default


def _str_env(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip()


def _bool_env(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def _positive_float_env(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(str(env.get(key, "")).strip())
    except ValueError:
        return default
    return value if value > 0.0 else default


def _non_negative_float_env(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(str(env.get(key, "")).strip())
    except ValueError:
        return default
    return value if value >= 0.0 else default


def _non_negative_int_env(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(str(env.get(key, "")).strip())
    except ValueError:
        return default
    return value if value >= 0 else default
