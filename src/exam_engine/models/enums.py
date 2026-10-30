"""Enumerations shared across models, storage and the ingestion pipeline."""

from __future__ import annotations

from enum import StrEnum


class DatasetType(StrEnum):
    """Provenance class of a dataset.

    ``synthetic`` data must never be presented as an official exam paper.
    """

    OFFICIAL = "official"
    PUBLIC = "public"
    OPENLY_LICENSED = "openly_licensed"
    PERMISSIONED = "permissioned"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"


class DocumentKind(StrEnum):
    QUESTION_PAPER = "question_paper"
    SYLLABUS = "syllabus"
    NOTES = "notes"
    QUESTION_BANK = "question_bank"
    OTHER = "other"


class SourceFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    CSV = "csv"
    JSON = "json"
    IMAGE = "image"


class ExtractionMethod(StrEnum):
    EMBEDDED_TEXT = "embedded_text"
    PLAIN_TEXT = "plain_text"
    STRUCTURED = "structured"
    OCR = "ocr"
    NONE = "none"


class ExtractionStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    NEEDS_OCR = "needs_ocr"
    EMPTY = "empty"
    FAILED = "failed"


class IngestionStatus(StrEnum):
    INGESTED = "ingested"
    REPLACED = "replaced"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    NEEDS_OCR = "needs_ocr"
    EMPTY = "empty"
    FAILED = "failed"


class DuplicatePolicy(StrEnum):
    SKIP = "skip"
    REPLACE = "replace"
    FAIL = "fail"


class SegmentationStatus(StrEnum):
    SEGMENTED = "segmented"
    PARTIAL = "partial"
    UNSEGMENTED = "unsegmented"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class FileVerdict(StrEnum):
    OK = "ok"
    REJECTED = "rejected"


# --- NLP (Phase 2) -----------------------------------------------------------


class TextOwnerType(StrEnum):
    """What a derived text representation belongs to."""

    COURSE = "course"
    DOCUMENT = "document"
    SECTION = "section"
    QUESTION = "question"
    SYLLABUS_UNIT = "syllabus_unit"


class NlpMethod(StrEnum):
    """Which algorithm produced a record; kept so results stay auditable."""

    TFIDF = "tfidf"
    RAKE = "rake"
    HYBRID = "hybrid"
    HASHING_TFIDF = "hashing_tfidf"
    LSA = "lsa"
    SENTENCE_TRANSFORMERS = "sentence_transformers"
    RULE_BASED = "rule_based"
    SPACY = "spacy"
    KMEANS = "kmeans"


class ConceptOrigin(StrEnum):
    """Inferred concepts must never be confused with syllabus-stated ones."""

    SYLLABUS = "syllabus"
    INFERRED = "inferred"


class NlpStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"
    FAILED = "failed"


class EmbeddingScope(StrEnum):
    """How an embedding provider was fitted."""

    STATELESS = "stateless"
    CORPUS = "corpus"
    PRETRAINED = "pretrained"


# --- Question understanding (Phase 3) ---------------------------------------


class QuestionType(StrEnum):
    """Observable question demands; a question may carry several labels."""

    DEFINITION = "definition"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    APPLICATION = "application"
    NUMERICAL = "numerical"
    SCENARIO = "scenario"
    PROOF = "proof"
    PROGRAMMING = "programming"
    DESIGN = "design"
    EVALUATION = "evaluation"


class BloomLevel(StrEnum):
    """Cognitive demand levels, inferred from wording *and* structure."""

    REMEMBER = "remember"
    UNDERSTAND = "understand"
    APPLY = "apply"
    ANALYZE = "analyze"
    EVALUATE = "evaluate"
    CREATE = "create"


class DifficultyLabel(StrEnum):
    """Provisional difficulty bands; not empirically measured student difficulty."""

    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"
    UNKNOWN = "unknown"


class MappingStatus(StrEnum):
    """Outcome of course-scoped syllabus mapping."""

    MAPPED = "mapped"
    AMBIGUOUS = "ambiguous"
    UNMAPPED = "unmapped"


class ReferenceKind(StrEnum):
    """Material a question explicitly points to."""

    DIAGRAM = "diagram"
    CODE = "code"
    TABLE = "table"
    EQUATION = "equation"
    CASE_STUDY = "case_study"


class SimilarityKind(StrEnum):
    """Why two questions were matched; only ``exact`` is proof of duplication."""

    EXACT = "exact"
    NEAR = "near"
    SEMANTIC = "semantic"
    CONCEPT_RELATED = "concept_related"


class UnderstandingStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"
    FAILED = "failed"


class TrendStatus(StrEnum):
    """Whether the available periods support a descriptive trend comparison."""

    OK = "ok"
    LOW_SAMPLE = "low_sample"
    INSUFFICIENT_DATA = "insufficient_data"
    NO_PERIOD_METADATA = "no_period_metadata"


# --- Knowledge graph (Phase 5) -----------------------------------------------


class GraphNodeType(StrEnum):
    """Entity kinds stored as graph nodes."""

    INSTITUTION = "institution"
    SUBJECT = "subject"
    COURSE = "course"
    SYLLABUS = "syllabus"
    UNIT = "unit"
    TOPIC = "topic"
    CONCEPT = "concept"
    QUESTION = "question"
    SOURCE_DOCUMENT = "source_document"
    EXAM_SESSION = "exam_session"
    EVIDENCE = "evidence"


class GraphRelationship(StrEnum):
    """Documented, extensible graph edge taxonomy."""

    CONTAINS = "CONTAINS"
    COVERS = "COVERS"
    PREREQUISITE_OF = "PREREQUISITE_OF"
    RELATED_TO = "RELATED_TO"
    SIMILAR_TO = "SIMILAR_TO"
    APPEARS_IN = "APPEARS_IN"
    DERIVED_FROM = "DERIVED_FROM"
    BELONGS_TO = "BELONGS_TO"
    ASSESSES = "ASSESSES"


class GraphEdgeStatus(StrEnum):
    """How much review an edge has received; inferred is not verified."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"
    CANDIDATE = "candidate"
    REVIEWED = "reviewed"
    VERIFIED = "verified"


class GraphBuildStatus(StrEnum):
    """Outcome of one course graph build."""

    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    FAILED = "failed"


# --- Coverage gap detection (Phase 7) ---------------------------------------


class CoverageStatus(StrEnum):
    """Evidence-bound coverage class for one syllabus concept.

    ``UNMAPPED`` describes the analysed corpus only; it never claims that the
    concept has never appeared or will not appear in an examination.
    """

    COVERED = "covered"
    LOW_COVERAGE = "low_coverage"
    UNMAPPED = "unmapped"
    UNCERTAIN = "uncertain"
    INSUFFICIENT_DATA = "insufficient_data"


class CoverageReportStatus(StrEnum):
    """Reliability of one coverage analysis, not its educational conclusion."""

    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"


# --- Exam DNA pattern detection (Phase 8) -----------------------------------


class ExamDnaStatus(StrEnum):
    """Reliability of one historical pattern profile, not its predictive value."""

    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"


class PatternCategory(StrEnum):
    """Observable question-setting characteristics tracked across papers."""

    QUESTION_TYPE = "question_type"
    BLOOM_LEVEL = "bloom_level"
    UNIT_PRESENCE = "unit_presence"
    UNIT_MARKS_SHARE = "unit_marks_share"
    COMMAND_VERB = "command_verb"
    SECTION_STRUCTURE = "section_structure"
    QUESTION_TYPE_MIX = "question_type_mix"
    MARKS_VALUE = "marks_value"
    CONCEPT_TYPE_PAIR = "concept_type_pair"
    CHOICE_STRUCTURE = "choice_structure"


class QuestionGroupKind(StrEnum):
    """Why a repeated/similar question group exists; only exact proves duplication."""

    EXACT = "exact"
    NEAR_DUPLICATE = "near_duplicate"
    SEMANTIC = "semantic"
    CONCEPT_RELATED = "concept_related"


class PaperExclusionReason(StrEnum):
    """Why a course document was not treated as independent paper evidence."""

    QUESTION_BANK = "question_bank"
    DUPLICATE_COPY = "duplicate_copy"
    NO_QUESTIONS = "no_questions"
    INCOMPLETE_EXTRACTION = "incomplete_extraction"
    FILTERED = "filtered"


class EvidenceQuality(StrEnum):
    """Strength of the evidence behind one observed pattern."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ChoiceRuleKind(StrEnum):
    """Explicit optional/compulsory instruction found in a paper's text."""

    ALL_COMPULSORY = "all_compulsory"
    CHOOSE_ANY = "choose_any"


ERROR_CODES = {
    # validation / IO
    "file_not_found": "Source file does not exist.",
    "not_a_file": "Source path is not a regular file.",
    "path_outside_root": "Source path escapes the permitted dataset root.",
    "unsupported_format": "File extension is not supported by any adapter.",
    "format_mismatch": "File content does not match its declared format.",
    "binary_content": "Text format contains binary (NUL) content.",
    "file_too_large": "File exceeds the configured size limit.",
    "empty_file": "File contains no bytes.",
    "unreadable_file": "File could not be read.",
    # parsing
    "parse_error": "Document parser raised an error.",
    "pdf_encrypted": "PDF is encrypted and cannot be read.",
    "pdf_page_limit": "PDF exceeds the configured page limit.",
    "page_limit_exceeded": "Document exceeds the configured page limit.",
    "docx_invalid": "DOCX container is invalid or unreadable.",
    "json_invalid": "JSON document is malformed.",
    "csv_invalid": "CSV document is malformed.",
    "ocr_unavailable": "Document needs OCR but no OCR provider is available.",
    "ocr_failed": "OCR provider failed for a page.",
    # content
    "empty_document": "Document produced no extractable text.",
    "no_questions_detected": "No numbered questions could be detected.",
    "no_syllabus_units_detected": "No syllabus units could be detected.",
    "question_limit_exceeded": "Question count exceeds the configured limit.",
    "low_quality_text": "Extracted text looks degraded.",
    # storage / batch
    "duplicate_document": "Document content already ingested for this course.",
    "course_conflict": "Course exists with different immutable metadata.",
    "unexpected_error": "Unexpected failure while ingesting this document.",
    "invalid_manifest": "Ingestion manifest failed validation.",
    "missing_manifest_field": "Manifest is missing a required field.",
    "manifest_document_missing": "Manifest references a missing file.",
    "database_error": "Database operation failed.",
    # nlp / phase 2
    "empty_text": "Text is empty after normalisation; nothing to analyse.",
    "language_unsupported": "Text language is not supported by the configured analysers.",
    "insufficient_data": "Too little data for a statistically meaningful result.",
    "model_unavailable": "The configured NLP model or library is not available.",
    "model_failure": "The NLP model failed while processing; a fallback was used.",
    "embedding_dim_mismatch": "Stored embedding dimension does not match the model.",
    "analysis_truncated": "Input exceeded the configured analysis limit.",
    "lexical_entities": (
        "Entities were found with lexical rules; they are candidates, not verified concepts."
    ),
    "degraded_fallback": "A requested component was unavailable and a documented fallback ran.",
    # question understanding / phase 3
    "no_syllabus": "No syllabus units are available for this course; mapping is unknown.",
    "unmapped_question": "No syllabus unit or topic met the mapping threshold.",
    "ambiguous_mapping": "Several syllabus units matched with indistinguishable scores.",
    "unknown_question_type": "No question type reached the classification threshold.",
    "unknown_bloom_level": "No Bloom level reached the classification threshold.",
    "difficulty_uncertain": "Difficulty is low-confidence or cannot be estimated from the text.",
    "implausible_subpart_marks": (
        "A sub-question's stated marks exceeded the per-question cap and were not stored."
    ),
    "low_extraction_confidence": "The Phase 1 extraction of this question needs review.",
    "embedding_signal_unavailable": (
        "Phase 2 question embeddings are unavailable; classification fell back to lexical signals."
    ),
    "similarity_fallback": "Similarity used lexical signals because embeddings were unavailable.",
    "question_analysis_failed": "One question could not be analysed; it was skipped with an error.",
    # analytics / phase 4
    "missing_analysis": (
        "Questions have no Phase 3 analysis; classification metrics are incomplete."
    ),
    "missing_period_metadata": (
        "Documents lack examination-session or academic-period metadata; temporal analysis "
        "is unavailable for them."
    ),
    "mixed_period_sources": (
        "Examination periods were derived from different metadata kinds; comparisons are "
        "descriptive and may not be equivalent."
    ),
    "small_sample": "The available sample is too small for a stable descriptive conclusion.",
    "insufficient_periods": "Fewer than two usable periods are available for trend comparison.",
    "subpart_marks_mismatch": (
        "Stated sub-part marks do not add up to the parent question's stated marks."
    ),
    "duplicate_document_text": (
        "Several documents share the same extracted-text fingerprint; check for repeated imports."
    ),
    "exact_repetition": (
        "Identical normalised question text appears more than once; appearances are retained."
    ),
    "cross_course_report": (
        "Reports for multiple courses are kept separate; no cross-syllabus totals are pooled."
    ),
    "analytics_truncated": "The configured analytics question limit truncated the input.",
    # knowledge graph / phase 5
    "graph_build_failed": "A course graph build failed; the previous graph was kept.",
    "graph_truncated": "A configured graph limit truncated questions or similarity pairs.",
    "no_concepts": (
        "No syllabus topics or Phase 2 concepts were available; concept queries are empty."
    ),
    "no_questions": "The course has no stored questions; assessment edges are empty.",
    "no_question_mappings": (
        "Questions were analysed but no supported topic/concept edges could be created."
    ),
    "no_exam_sessions": (
        "No document has explicit examination-session metadata; session queries are empty."
    ),
    "ambiguous_prerequisites": (
        "A syllabus prerequisite list did not identify target topics; no edges were guessed."
    ),
    "similarity_disabled": (
        "Stored Phase 3 similarities were not used because graph similarity is disabled."
    ),
    "incomplete_question_mapping": (
        "Some Phase 3 matches could not be linked to a syllabus topic and remain unmapped."
    ),
    "unmapped_questions": ("Some analysed questions have no supported topic/concept mapping."),
    "ambiguous_question_mappings": (
        "Some questions have ambiguous Phase 3 mappings; those edges are candidates."
    ),
    # coverage / phase 7
    "unknown_course": "The requested course does not exist.",
    "unknown_syllabus": "The requested syllabus does not belong to the course.",
    "ambiguous_syllabus": (
        "The course has several syllabus versions; an explicit syllabus selection is required."
    ),
    "invalid_coverage_filter": (
        "A coverage filter names data outside the selected course or syllabus."
    ),
    "coverage_graph_disabled": (
        "Phase 5 graph mappings are disabled by configuration; only Phase 3 mappings were used."
    ),
    "coverage_missing_marks": (
        "Some selected questions have no stated marks; mark totals and shares exclude them."
    ),
    "coverage_unit_no_concepts": "No syllabus topics were parsed for the unit.",
    "coverage_unit_insufficient_data": (
        "Every concept in the unit lacks sufficient evidence for classification."
    ),
    "coverage_unit_no_mapped_questions": (
        "No question was confidently mapped to a concept in the unit."
    ),
    "coverage_truncated": "The configured coverage question limit truncated the corpus.",
    "coverage_small_corpus": (
        "The available question corpus is too small to conclude that an unmapped concept "
        "is unassessed."
    ),
    "coverage_partial_analysis": (
        "Some questions have no Phase 3 analysis; concept coverage may change after analysis."
    ),
    "coverage_ambiguous_evidence": (
        "Ambiguous or secondary mapping evidence was retained but not treated as coverage."
    ),
    "coverage_low_confidence": (
        "Accepted mappings are below the configured confidence threshold; coverage is uncertain."
    ),
    "coverage_shared_concept_units": (
        "A concept occurs in several selected units; unit counts overlap and are not additive."
    ),
    "coverage_unattributed_questions": (
        "Questions without mappings to any syllabus version cannot be attributed to the "
        "selected version and were excluded from its denominator."
    ),
    "coverage_graph_unavailable": (
        "No matching Phase 5 concept nodes were found; syllabus topic identity was used without "
        "graph aliases or explicit graph assessment edges."
    ),
    "coverage_export_failed": "The coverage report could not be written to the requested path.",
    # exam dna / phase 8
    "exam_dna_insufficient_papers": (
        "Fewer eligible examination papers than the configured recurrence minimum are "
        "available; recurring patterns cannot be established."
    ),
    "exam_dna_small_sample": (
        "The available paper collection is too small for a stable cross-paper pattern."
    ),
    "exam_dna_duplicate_paper": (
        "A document duplicates another paper's questions or extracted text; it is not "
        "independent evidence."
    ),
    "exam_dna_missing_dates": (
        "Examination session or date metadata is missing; session-level recurrence is "
        "unavailable for those papers."
    ),
    "exam_dna_mixed_exam_types": (
        "Papers of different examination types are included; comparisons may not be equivalent."
    ),
    "exam_dna_different_marks_totals": (
        "Papers state different total marks; raw marks are reported but not normalised "
        "across papers."
    ),
    "exam_dna_missing_marks": (
        "Some questions have no stated marks; distributions and shares exclude them."
    ),
    "exam_dna_missing_analysis": (
        "Some questions have no Phase 3 analysis; type, Bloom and mapping patterns are incomplete."
    ),
    "exam_dna_low_confidence": (
        "Some question classifications are below the configured confidence threshold."
    ),
    "exam_dna_missing_classification": (
        "Some analysed questions have no primary question-type label."
    ),
    "exam_dna_uncertain_segmentation": (
        "Some questions have low-confidence structure; sub-question links may be incomplete."
    ),
    "exam_dna_uncertain_mapping": (
        "Some concepts have uncertain Phase 7 coverage evidence; concept patterns inherit it."
    ),
    "exam_dna_partial_extraction": (
        "A paper's extraction was incomplete; its questions may not represent the whole paper."
    ),
    "exam_dna_no_sections": (
        "No reliable section metadata is available; section and internal-choice patterns "
        "cannot be derived for those papers."
    ),
    "exam_dna_choice_uncertain": (
        "An optional/compulsory instruction was ambiguous and was not treated as a rule."
    ),
    "exam_dna_truncated": "The configured Exam DNA question limit truncated the corpus.",
    "exam_dna_no_similarity": (
        "No stored Phase 3 similarity pairs meet the configured threshold; only exact "
        "repetition is available."
    ),
    "exam_dna_no_questions": "The selected papers contain no stored questions.",
    "exam_dna_graph_unavailable": (
        "Phase 5 graph evidence is unavailable; concept identity comes from the Phase 7 "
        "coverage report only."
    ),
    "exam_dna_forecast_out_of_scope": (
        "Historical recurrence is descriptive only; no future-examination probability is "
        "calculated by this phase."
    ),
    "exam_dna_export_failed": "The Exam DNA profile could not be written to the requested path.",
    "invalid_exam_dna_filter": (
        "An Exam DNA filter is invalid or names data outside the selected course."
    ),
}


# --- Concept evolution tracking (Phase 9) ------------------------------------


class EvolutionStatus(StrEnum):
    """Reliability of one evolution report, not a claim about real history."""

    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"


class EvolutionDateType(StrEnum):
    """Which source date an observation was ordered by.

    These are deliberately distinct: an ingestion timestamp is never a
    publication date, and an academic year is not an exact date.
    """

    PUBLICATION = "publication"
    EDITION = "edition"
    SYLLABUS_EFFECTIVE = "syllabus_effective"
    EXAMINATION = "examination"
    UNKNOWN = "unknown"


class ConceptChangeCategory(StrEnum):
    """Observable, reviewable categories of change inside the analyzed corpus."""

    TERMINOLOGY_CHANGED = "terminology_changed"
    ALIAS_INTRODUCED = "alias_introduced"
    CONCEPT_INTRODUCED = "concept_introduced"
    CONCEPT_NO_LONGER_OBSERVED = "concept_no_longer_observed"
    CONTEXT_CHANGED = "context_changed"
    SYLLABUS_UNIT_CHANGED = "syllabus_unit_changed"
    DESCRIPTION_CHANGED = "description_changed"
    ASSESSMENT_PATTERN_CHANGED = "assessment_pattern_changed"
    RELATIONSHIP_CHANGED = "relationship_changed"
    POSSIBLE_CONCEPT_MERGE = "possible_concept_merge"
    POSSIBLE_CONCEPT_SPLIT = "possible_concept_split"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EvolutionReviewStatus(StrEnum):
    """Review state of one detected change; nothing is auto-applied."""

    NOT_REQUIRED = "not_required"
    PENDING_REVIEW = "pending_review"


EVOLUTION_WARNING_MESSAGES: dict[str, str] = {
    "evolution_missing_date": (
        "A document has no usable source date; it is reported but cannot be placed on the "
        "timeline by date."
    ),
    "evolution_conflicting_dates": (
        "A document states more than one source date; the selected date is reported and the "
        "conflict is flagged instead of silently resolved."
    ),
    "evolution_invalid_date": (
        "A source date could not be parsed and was not replaced with a guessed value."
    ),
    "evolution_ingestion_time_not_used": (
        "Ingestion timestamps are never used as publication or effective dates."
    ),
    "evolution_graph_unavailable": (
        "Phase 5 concept nodes are unavailable; concept identity falls back to syllabus topics."
    ),
    "evolution_unknown_version_order": (
        "Syllabus versions lack reliable effective dates; their order is not assumed."
    ),
    "evolution_incomplete_extraction": (
        "A document's extraction was incomplete; absence of a concept is not proof of removal."
    ),
    "evolution_undated_excluded": (
        "Undated records were excluded from a date filter rather than silently included."
    ),
    "evolution_insufficient_observations": (
        "A concept has too few dated observations to support a change claim."
    ),
    "evolution_context_similarity": (
        "Contextual comparison uses lexical similarity between excerpts; it is a review "
        "signal, not proof that the concept's meaning changed."
    ),
    "evolution_low_confidence": (
        "A detected observation or change is below the configured confidence threshold."
    ),
    "evolution_truncated": "A configured Phase 9 limit truncated the analyzed corpus.",
    "evolution_mixed_formats": (
        "Documents of different formats or examination types were compared descriptively; "
        "their metrics may not be equivalent."
    ),
    "evolution_no_coverage": (
        "Phase 7 coverage is unavailable; assessment evolution is limited to raw records."
    ),
    "evolution_export_failed": "The concept evolution report could not be written.",
    "invalid_evolution_filter": (
        "A Phase 9 filter is invalid or names data outside the selected course."
    ),
}


# --- Question generation (Phase 10) ------------------------------------------


class GenerationFormat(StrEnum):
    """Requested response format, kept separate from the cognitive task label."""

    MULTIPLE_CHOICE = "multiple_choice"
    SHORT_ANSWER = "short_answer"
    LONG_ANSWER = "long_answer"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    APPLICATION = "application"
    NUMERICAL = "numerical"
    CASE_BASED = "case_based"
    ANALYSIS = "analysis"
    DESIGN = "design"
    EVALUATION = "evaluation"


class GenerationStatus(StrEnum):
    """Completeness of one generation batch, never its educational quality."""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class GenerationValidationStatus(StrEnum):
    """Outcome of automated validation; VALIDATED is not a correctness proof."""

    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    VALIDATION_INCOMPLETE = "validation_incomplete"


class GenerationReviewStatus(StrEnum):
    """Human review state of one generated question."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssessmentOrigin(StrEnum):
    """Where one stored question came from; only ``generation`` is Phase 10."""

    GENERATION = "generation"
    MUTATION = "mutation"
    ADVERSARIAL = "adversarial"


class MutationStrategy(StrEnum):
    """A permitted way to derive a variant from an existing question."""

    REPHRASE = "rephrase"
    SCENARIO = "scenario"
    REORDER = "reorder"
    APPLICATION = "application"
    NUMERICAL = "numerical"
    DIFFICULTY = "difficulty"
    COGNITIVE = "cognitive"
    RELATED_CONCEPT = "related_concept"
    DISTRACTOR = "distractor"


class ChallengeStrategy(StrEnum):
    """A fair way to test understanding beyond recall."""

    UNFAMILIAR_APPLICATION = "unfamiliar_application"
    TRANSFER = "transfer"
    MULTI_STEP = "multi_step"
    FALSE_ASSUMPTION = "false_assumption"
    ERROR_DIAGNOSIS = "error_diagnosis"
    COMPETING_APPROACH = "competing_approach"
    BOUNDARY = "boundary"
    COUNTEREXAMPLE = "counterexample"
    SOLUTION_EVALUATION = "solution_evaluation"
    MISSING_PREREQUISITE = "missing_prerequisite"
    EDGE_CASE = "edge_case"
    DISTINGUISH_CONCEPTS = "distinguish_concepts"
    WHY_WRONG = "why_wrong"


class MisconceptionKind(StrEnum):
    """A suspected misunderstanding, never a verdict on a student."""

    INCORRECT_DEFINITION = "incorrect_definition"
    CONCEPT_CONFUSION = "concept_confusion"
    FORMULA_MISAPPLICATION = "formula_misapplication"
    MISSING_REASONING_STEP = "missing_reasoning_step"
    INVALID_ASSUMPTION = "invalid_assumption"
    PROCEDURAL_ERROR = "procedural_error"
    TERMINOLOGY_MISUSE = "terminology_misuse"
    PREREQUISITE_GAP = "prerequisite_gap"
    PARTIAL_UNDERSTANDING = "partial_understanding"
    UNCLEAR_OR_UNRELATED = "unclear_or_unrelated"


class MisconceptionStatus(StrEnum):
    """Human review state of one mined pattern."""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"


class SupportLevel(StrEnum):
    """How much independent response evidence a pattern has."""

    SINGLE_OBSERVATION = "single_observation"
    CANDIDATE = "candidate"
    RECURRING = "recurring"


class ResponseCorrectness(StrEnum):
    """Given or inferred correctness of one student response."""

    CORRECT = "correct"
    PARTIALLY_CORRECT = "partially_correct"
    INCORRECT = "incorrect"
    UNCLEAR = "unclear"
