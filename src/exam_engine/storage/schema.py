"""SQLAlchemy ORM rows mirroring ``storage/migrations``.

Timestamps and JSON payloads are stored as TEXT so SQLite round-trips them
without dialect-specific coercion.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CourseRow(Base):
    __tablename__ = "courses"

    course_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    institution_code: Mapped[str] = mapped_column(Text, nullable=False)
    institution_name: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(Text)
    programme: Mapped[str | None] = mapped_column(Text)
    academic_year: Mapped[str | None] = mapped_column(Text)
    syllabus_version: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "idx_courses_offering",
            "institution_code",
            "course_code",
            "academic_year",
            "syllabus_version",
        ),
    )


class DatasetSourceRow(Base):
    __tablename__ = "dataset_sources"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_type: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[str] = mapped_column(Text, nullable=False)
    license: Mapped[str | None] = mapped_column(Text)
    license_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    usage_terms: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_dataset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DocumentRow(Base):
    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("dataset_sources.source_id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extraction_status: Mapped[str] = mapped_column(Text, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    quality_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    language: Mapped[str | None] = mapped_column(Text)
    academic_year: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    __table_args__ = (
        UniqueConstraint("course_id", "content_sha256", name="ux_documents_course_sha"),
        Index("idx_documents_course", "course_id"),
        Index("idx_documents_fingerprint", "course_id", "text_fingerprint"),
        Index("idx_documents_source", "source_id"),
    )


class PageRow(Base):
    __tablename__ = "pages"

    page_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    has_images: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ocr_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="ux_pages_document_page"),
        Index("idx_pages_document", "document_id", "page_number"),
    )


class QuestionRow(Base):
    __tablename__ = "questions"

    question_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    question_number: Mapped[str | None] = mapped_column(Text)
    number_sort: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[float | None] = mapped_column(Float)
    marks_source: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer)
    source_locator: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    needs_review: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duplicate_of: Mapped[str | None] = mapped_column(Text)
    uncertainties_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        Index("idx_questions_course", "course_id"),
        Index("idx_questions_unit", "course_id", "unit"),
        Index("idx_questions_document", "document_id", "number_sort"),
        Index("idx_questions_fingerprint", "course_id", "fingerprint"),
    )


class SyllabusRow(Base):
    __tablename__ = "syllabi"

    syllabus_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("dataset_sources.source_id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    needs_review: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uncertainties_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    ingested_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_syllabi_course", "course_id"),)


class SyllabusUnitRow(Base):
    __tablename__ = "syllabus_units"

    unit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    syllabus_id: Mapped[str] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE"), nullable=False
    )
    unit_label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    hours: Mapped[float | None] = mapped_column(Float)
    topics_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    page_number: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    uncertainties_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (Index("idx_syllabus_units_syllabus", "syllabus_id"),)


class IngestionRunRow(Base):
    __tablename__ = "ingestion_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    totals_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class IngestionResultRow(Base):
    __tablename__ = "ingestion_results"

    result_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingestion_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    course_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    syllabus_unit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_of: Mapped[str | None] = mapped_column(Text)
    errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("idx_ingestion_results_run", "run_id"),)


# --- Phase 2: NLP foundation -------------------------------------------------


class ProcessedTextRow(Base):
    __tablename__ = "processed_texts"

    text_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    raw_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    config_id: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    sentence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sentences_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    uncertainties_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", name="ux_processed_texts_owner"),
        Index("idx_processed_texts_course", "course_id", "owner_type"),
        Index("idx_processed_texts_document", "document_id"),
    )


class DocumentSectionRow(Base):
    __tablename__ = "document_sections"

    section_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("document_id", "section_index", name="ux_document_sections_index"),
        Index("idx_document_sections_document", "document_id", "section_index"),
    )


class EmbeddingCacheRow(Base):
    __tablename__ = "embedding_cache"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("model_id", "text_sha256", name="ux_embedding_cache_model_text"),
    )


class EmbeddingRow(Base):
    __tablename__ = "embeddings"

    embedding_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_key: Mapped[str] = mapped_column(
        Text, ForeignKey("embedding_cache.cache_key", ondelete="CASCADE"), nullable=False
    )
    text_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "model_id", name="ux_embeddings_owner_model"),
        Index("idx_embeddings_course", "course_id", "owner_type"),
    )


class KeyphraseRow(Base):
    __tablename__ = "keyphrases"

    keyphrase_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    term: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    n_gram: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        Index("idx_keyphrases_owner", "course_id", "owner_type", "owner_id", "method", "rank"),
    )


class EntityRow(Base):
    __tablename__ = "entities"

    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        Index("idx_entities_course", "course_id", "label"),
        Index("idx_entities_owner", "owner_type", "owner_id"),
    )


class ConceptRow(Base):
    __tablename__ = "concept_candidates"

    concept_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    verified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit_id: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("course_id", "origin", "normalized", name="ux_concepts_course_origin"),
        Index("idx_concepts_course", "course_id", "origin"),
    )


class TopicRow(Base):
    __tablename__ = "nlp_topics"

    topic_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_nlp_topics_course", "course_id", "scope"),)


class TopicTermRow(Base):
    __tablename__ = "nlp_topic_terms"

    # Migration 0003 keys topic terms by (topic_id, normalized); keep the ORM
    # mapping identical so merge() looks rows up by the database's key.
    topic_id: Mapped[str] = mapped_column(
        Text, ForeignKey("nlp_topics.topic_id", ondelete="CASCADE"), primary_key=True
    )
    normalized: Mapped[str] = mapped_column(Text, primary_key=True)
    term: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)


class TopicMemberRow(Base):
    __tablename__ = "nlp_topic_members"

    topic_id: Mapped[str] = mapped_column(
        Text, ForeignKey("nlp_topics.topic_id", ondelete="CASCADE"), primary_key=True
    )
    owner_type: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class NlpRunRow(Base):
    __tablename__ = "nlp_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    model_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    methods_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    totals_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("idx_nlp_runs_course", "course_id", "started_at"),)


# --- question understanding (Phase 3) -------------------------------------


class QuestionAnalysisRow(Base):
    __tablename__ = "question_analysis"

    analysis_id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    text_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sentence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subquestion_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subparts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    references_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    mapping_status: Mapped[str] = mapped_column(Text, nullable=False)
    mapping_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    top_unit_id: Mapped[str | None] = mapped_column(Text)
    primary_type: Mapped[str | None] = mapped_column(Text)
    primary_type_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    difficulty_label: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    difficulty_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    difficulty_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bloom_primary: Mapped[str | None] = mapped_column(Text)
    bloom_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    structure_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    uncertainties_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_question_analysis_course", "course_id", "mapping_status"),
        Index("idx_question_analysis_document", "document_id"),
    )


class QuestionUnitMatchRow(Base):
    __tablename__ = "question_unit_matches"

    match_id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(
        Text, ForeignKey("syllabus_units.unit_id", ondelete="CASCADE"), nullable=False
    )
    syllabus_id: Mapped[str] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE"), nullable=False
    )
    unit_label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_title: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    matched_terms_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    __table_args__ = (
        Index("idx_question_unit_matches_course", "course_id", "question_id", "rank"),
        Index("idx_question_unit_matches_unit", "unit_id"),
    )


class QuestionTypeRow(Base):
    __tablename__ = "question_types"

    type_id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    __table_args__ = (Index("idx_question_types_course", "course_id", "label", "question_id"),)


class QuestionBloomRow(Base):
    __tablename__ = "question_bloom"

    bloom_id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    __table_args__ = (Index("idx_question_bloom_course", "course_id", "level", "question_id"),)


class QuestionDifficultyRow(Base):
    __tablename__ = "question_difficulty"

    difficulty_id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    estimated: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    features_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    explanation_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    __table_args__ = (
        UniqueConstraint("question_id", "method", name="ux_question_difficulty_method"),
        Index("idx_question_difficulty_course", "course_id", "label"),
    )


class QuestionSimilarityRow(Base):
    __tablename__ = "question_similarity"

    pair_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False
    )
    matched_question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    lexical_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    semantic_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "course_id",
            "question_id",
            "matched_question_id",
            "kind",
            name="ux_question_similarity_pair",
        ),
        Index("idx_question_similarity_course", "course_id", "question_id"),
        Index("idx_question_similarity_match", "matched_question_id"),
    )


# --- Phase 5: persistent knowledge graph -------------------------------------
class GraphNodeRow(Base):
    __tablename__ = "graph_nodes"

    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    node_type: Mapped[str] = mapped_column(Text, nullable=False)
    node_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_label: Mapped[str | None] = mapped_column(Text)
    course_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE")
    )
    syllabus_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE")
    )
    document_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    question_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE")
    )
    entity_id: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("node_type", "node_key", name="ux_graph_nodes_type_key"),
        Index("idx_graph_nodes_course_type", "course_id", "node_type"),
        Index("idx_graph_nodes_syllabus", "syllabus_id", "node_type"),
        Index("idx_graph_nodes_document", "document_id"),
        Index("idx_graph_nodes_question", "question_id"),
        Index("idx_graph_nodes_normalized", "course_id", "normalized_label"),
    )


class GraphEdgeRow(Base):
    __tablename__ = "graph_edges"

    edge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE")
    )
    syllabus_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE")
    )
    source_node_id: Mapped[str] = mapped_column(
        Text, ForeignKey("graph_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    target_node_id: Mapped[str] = mapped_column(
        Text, ForeignKey("graph_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_document_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    source_page: Mapped[int | None] = mapped_column(Integer)
    model_version: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relationship",
            name="ux_graph_edges_source_target_rel",
        ),
        Index("idx_graph_edges_course_rel", "course_id", "relationship"),
        Index("idx_graph_edges_syllabus_rel", "syllabus_id", "relationship"),
        Index("idx_graph_edges_source", "source_node_id", "relationship"),
        Index("idx_graph_edges_target", "target_node_id", "relationship"),
        Index("idx_graph_edges_document", "source_document_id"),
    )


class UnderstandingRunRow(Base):
    __tablename__ = "understanding_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    methods_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    model_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    totals_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("idx_understanding_runs_course", "course_id", "started_at"),)


class GenerationBatchRow(Base):
    __tablename__ = "generation_batches"

    batch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    syllabus_id: Mapped[str] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(Text, nullable=False, default="generation")
    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    counts_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_generation_batches_course", "course_id", "created_at"),
        Index("idx_generation_batches_syllabus", "syllabus_id"),
    )


class GeneratedQuestionRow(Base):
    __tablename__ = "generated_questions"

    question_id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_batches.batch_id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    syllabus_id: Mapped[str] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(Text, nullable=False)
    unit_label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_title: Mapped[str | None] = mapped_column(Text)
    concept_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    concept_labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    learning_objective: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str | None] = mapped_column(Text)
    requested_difficulty: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_difficulty: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    difficulty_method: Mapped[str] = mapped_column(Text, nullable=False)
    requested_bloom: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_bloom: Mapped[str | None] = mapped_column(Text)
    bloom_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    marks: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    correct_option_key: Mapped[str | None] = mapped_column(Text)
    answer_key: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    solution_steps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    distractor_explanations_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_references_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    generation_metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    validation_status: Mapped[str] = mapped_column(Text, nullable=False)
    validation_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    review_status: Mapped[str] = mapped_column(Text, nullable=False)
    review_history_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    limitations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="generation")
    source_question_id: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_generated_questions_course", "course_id", "syllabus_id", "review_status"),
        Index("idx_generated_questions_batch", "batch_id"),
        Index("idx_generated_questions_unit", "unit_id"),
        Index("idx_generated_questions_source", "source_question_id"),
        Index("idx_generated_questions_origin", "course_id", "origin", "strategy"),
    )


class StudentResponseRow(Base):
    __tablename__ = "student_responses"

    response_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    syllabus_id: Mapped[str] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    correctness: Mapped[str] = mapped_column(Text, nullable=False)
    marks_awarded: Mapped[float | None] = mapped_column(Float)
    marks_available: Mapped[float | None] = mapped_column(Float)
    group_ref: Mapped[str | None] = mapped_column(Text)
    identified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual_import")
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    imported_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_student_responses_course", "course_id", "syllabus_id", "question_id"),
        Index("idx_student_responses_fingerprint", "fingerprint"),
    )


class ExamPaperRow(Base):
    __tablename__ = "exam_papers"

    paper_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_code: Mapped[str] = mapped_column(Text, nullable=False)
    paper_label: Mapped[str] = mapped_column(Text, nullable=False)
    exam_year: Mapped[int | None] = mapped_column(Integer)
    source_question_prefix: Mapped[str | None] = mapped_column(Text)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("course_id", "paper_label", name="ux_exam_papers_course_label"),
        Index("idx_exam_papers_course_idx", "course_id", "exam_year"),
        Index("idx_exam_papers_category_idx", "category_id", "subject_code"),
    )


class ExamPassageRow(Base):
    __tablename__ = "exam_passages"

    passage_id: Mapped[str] = mapped_column(Text, primary_key=True)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    passage_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)
    child_question_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_exam_passages_course_idx", "course_id"),)


class ExamQuestionDetailRow(Base):
    __tablename__ = "exam_question_details"

    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), primary_key=True
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        Text, ForeignKey("exam_papers.paper_id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_code: Mapped[str] = mapped_column(Text, nullable=False)
    source_question_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_row: Mapped[int | None] = mapped_column(Integer)
    original_question_number: Mapped[str | None] = mapped_column(Text)
    question_type: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    question_type_raw: Mapped[str | None] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    has_options: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subparts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    exam_year: Mapped[int | None] = mapped_column(Integer)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    passage_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("exam_passages.passage_id", ondelete="SET NULL")
    )
    passage_order: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("course_id", "source_question_id", name="ux_exam_details_course_source"),
        Index("idx_exam_details_course_idx", "course_id", "exam_year", "question_type"),
        Index("idx_exam_details_paper_idx", "paper_id"),
        Index("idx_exam_details_passage_idx", "passage_id", "passage_order"),
    )


class ExamAnswerRow(Base):
    __tablename__ = "exam_answers"

    answer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    source_answer_id: Mapped[str | None] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_type: Mapped[str] = mapped_column(Text, nullable=False)
    answer_type_raw: Mapped[str | None] = mapped_column(Text)
    correct_option_key: Mapped[str | None] = mapped_column(Text)
    solution_steps: Mapped[str | None] = mapped_column(Text)
    match_method: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_exam_answers_course_idx", "course_id", "answer_type"),)


class ExamProvenanceRow(Base):
    """One evidence-bound provenance fact per document/paper/question/answer.

    Nullable columns are deliberate: a corpus that never stated an authority,
    paper code, booklet, language or retrieval date must store NULL rather than
    an inferred value.
    """

    __tablename__ = "exam_provenance"

    provenance_id: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    paper_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("exam_papers.paper_id", ondelete="CASCADE")
    )
    question_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("questions.question_id", ondelete="CASCADE")
    )
    answer_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("exam_answers.answer_id", ondelete="CASCADE")
    )
    source_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("dataset_sources.source_id", ondelete="SET NULL")
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    source_publisher: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[str | None] = mapped_column(Text)
    source_document_sha256: Mapped[str | None] = mapped_column(Text)
    examining_authority: Mapped[str | None] = mapped_column(Text)
    examination_name: Mapped[str | None] = mapped_column(Text)
    exam_year: Mapped[int | None] = mapped_column(Integer)
    exam_session: Mapped[str | None] = mapped_column(Text)
    subject_code: Mapped[str | None] = mapped_column(Text)
    paper_code: Mapped[str | None] = mapped_column(Text)
    set_code: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    original_question_id: Mapped[str | None] = mapped_column(Text)
    original_option_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    original_question_number: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    source_classification: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    identity_status: Mapped[str] = mapped_column(Text, nullable=False, default="unresolved")
    paper_identity_status: Mapped[str] = mapped_column(Text, nullable=False, default="unresolved")
    key_identity_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="not_applicable"
    )
    notes: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_exam_provenance_course", "course_id", "scope"),
        Index("idx_exam_provenance_document", "document_id"),
        Index("idx_exam_provenance_paper", "paper_id"),
        Index("idx_exam_provenance_question", "question_id"),
        Index("idx_exam_provenance_answer", "answer_id"),
        Index(
            "idx_exam_provenance_status",
            "source_classification",
            "identity_status",
            "paper_identity_status",
        ),
    )


class ExamImportRunRow(Base):
    __tablename__ = "exam_import_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    totals_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExamImportFileRow(Base):
    __tablename__ = "exam_import_files"

    file_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("exam_import_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_code: Mapped[str] = mapped_column(Text, nullable=False)
    course_id: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_questions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_answers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        Index("idx_exam_import_files_run_idx", "run_id"),
        Index("idx_exam_import_files_course_idx", "course_id", "kind"),
    )


class MisconceptionRow(Base):
    __tablename__ = "misconceptions"

    misconception_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    course_id: Mapped[str] = mapped_column(
        Text, ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False
    )
    syllabus_id: Mapped[str] = mapped_column(
        Text, ForeignKey("syllabi.syllabus_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    concept_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    concept_labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    question_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    support_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_questions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_groups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detection_method: Mapped[str] = mapped_column(Text, nullable=False)
    support_level: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    suggested_correction: Mapped[str | None] = mapped_column(Text)
    follow_up_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    review_status: Mapped[str] = mapped_column(Text, nullable=False)
    review_history_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    limitations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "idx_misconceptions_course",
            "course_id",
            "syllabus_id",
            "review_status",
            "support_level",
        ),
        Index("idx_misconceptions_kind", "course_id", "kind"),
    )


# Dataset Intelligence & Recovery tables. They are intentionally separate from
# the source question/answer tables: recovery records are evidence and
# proposals, not replacements for the immutable imported corpus.
recovery_runs = Table(
    "recovery_runs",
    Base.metadata,
    Column("run_id", Text, primary_key=True),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("status", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("dry_run", Integer, nullable=False, default=0),
    Column("root", Text, nullable=False),
    Column("recovery_version", Text, nullable=False),
    Column("rule_version", Text, nullable=False),
    Column("options_json", Text, nullable=False, default="{}"),
    Column("totals_json", Text, nullable=False, default="{}"),
    Column("warnings_json", Text, nullable=False, default="[]"),
    Column("errors_json", Text, nullable=False, default="[]"),
    Column("duration_ms", Integer, nullable=False, default=0),
    Column("created_at", Text, nullable=False),
)

recovery_course_reports = Table(
    "recovery_course_reports",
    Base.metadata,
    Column("run_id", Text, primary_key=True),
    Column("course_id", Text, primary_key=True),
    Column("course_code", Text, nullable=False),
    Column("course_name", Text, nullable=False),
    Column("category_id", Text, nullable=False),
    Column("source_file", Text, nullable=False),
    Column("source_sha256", Text, nullable=False),
    Column("schema_json", Text, nullable=False, default="{}"),
    Column("row_count", Integer, nullable=False, default=0),
    Column("analyzed_count", Integer, nullable=False, default=0),
    Column("status", Text, nullable=False),
    Column("quality_json", Text, nullable=False, default="{}"),
    Column("counts_json", Text, nullable=False, default="{}"),
    Column("warnings_json", Text, nullable=False, default="[]"),
    Column("errors_json", Text, nullable=False, default="[]"),
    Column("duration_ms", Integer, nullable=False, default=0),
    Column("created_at", Text, nullable=False),
)

recovery_records = Table(
    "recovery_records",
    Base.metadata,
    Column("run_id", Text, primary_key=True),
    Column("record_id", Text, primary_key=True),
    Column("course_id", Text, nullable=False),
    Column("source_file", Text, nullable=False),
    Column("source_row", Integer, nullable=False),
    Column("source_question_id", Text, nullable=False),
    Column("source_sha256", Text, nullable=False),
    Column("analysis_hash", Text, nullable=False),
    Column("original_json", Text, nullable=False),
    Column("normalized_json", Text, nullable=False),
    Column("repaired_json", Text, nullable=False),
    Column("structure_json", Text, nullable=False),
    Column("quality_json", Text, nullable=False),
    Column("verification_json", Text, nullable=False, default="{}"),
    Column("answer_status", Text, nullable=False),
    Column("source_fidelity", Text, nullable=False),
    Column("release_status", Text, nullable=False),
    Column("issue_count", Integer, nullable=False, default=0),
    Column("blocking_count", Integer, nullable=False, default=0),
    Column("created_at", Text, nullable=False),
)

recovery_issues = Table(
    "recovery_issues",
    Base.metadata,
    Column("issue_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("course_id", Text, nullable=False),
    Column("record_id", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("evidence_json", Text, nullable=False, default="{}"),
    Column("confidence", Float, nullable=False, default=0.5),
    Column("remediation", Text, nullable=False, default=""),
    Column("blocking", Integer, nullable=False, default=0),
    Column("field", Text),
    Column("created_at", Text, nullable=False),
)

recovery_repairs = Table(
    "recovery_repairs",
    Base.metadata,
    Column("repair_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("course_id", Text, nullable=False),
    Column("record_id", Text, nullable=False),
    Column("repair_type", Text, nullable=False),
    Column("field", Text, nullable=False),
    Column("before_json", Text, nullable=False, default="null"),
    Column("after_json", Text, nullable=False, default="null"),
    Column("method", Text, nullable=False),
    Column("confidence", Float, nullable=False, default=0.5),
    Column("risk", Text, nullable=False, default="low"),
    Column("status", Text, nullable=False, default="proposed"),
    Column("reason", Text, nullable=False, default=""),
    Column("reviewer", Text),
    Column("note", Text),
    Column("created_at", Text, nullable=False),
)

recovery_answer_candidates = Table(
    "recovery_answer_candidates",
    Base.metadata,
    Column("candidate_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("course_id", Text, nullable=False),
    Column("record_id", Text, nullable=False),
    Column("answer_text", Text),
    Column("source_type", Text, nullable=False),
    Column("source_name", Text, nullable=False),
    Column("source_url", Text),
    Column("source_reference", Text),
    Column("verification_status", Text, nullable=False),
    Column("confidence", Float, nullable=False, default=0.0),
    Column("method", Text, nullable=False),
    Column("evidence_json", Text, nullable=False, default="{}"),
    Column("retrieved_at", Text, nullable=False),
    Column("official", Integer, nullable=False, default=0),
)

recovery_source_documents = Table(
    "recovery_source_documents",
    Base.metadata,
    Column("source_document_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("course_id", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("page_count", Integer),
    Column("text_chars", Integer),
    Column("question_fidelity_available", Integer, nullable=False, default=0),
    Column("metadata_json", Text, nullable=False, default="{}"),
)

recovery_source_candidates = Table(
    "recovery_source_candidates",
    Base.metadata,
    Column("candidate_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("course_id", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("document_type", Text, nullable=False),
    Column("source_tier", Integer, nullable=False),
    Column("reliability", Float, nullable=False, default=0.0),
    Column("provider", Text, nullable=False, default="unknown"),
    Column("snippet", Text, nullable=False, default=""),
    Column("query", Text, nullable=False, default=""),
    Column("classification_reason", Text, nullable=False, default=""),
    Column("retrieval_status", Text, nullable=False, default="discovered"),
    Column("discovered_at", Text, nullable=False),
    Column("evidence_json", Text, nullable=False, default="{}"),
)

recovery_verifications = Table(
    "recovery_verifications",
    Base.metadata,
    Column("verification_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("course_id", Text, nullable=False),
    Column("record_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("method", Text, nullable=False),
    Column("confidence", Float, nullable=False, default=0.0),
    Column("answer_text", Text),
    Column("evidence_json", Text, nullable=False, default="{}"),
    Column("issues_json", Text, nullable=False, default="[]"),
    Column("independent", Integer, nullable=False, default=0),
    Column("created_at", Text, nullable=False),
)

recovery_passages = Table(
    "recovery_passages",
    Base.metadata,
    Column("passage_id", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("course_id", Text, primary_key=True),
    Column("passage_text", Text, nullable=False),
    Column("parent_record_id", Text),
    Column("child_record_ids_json", Text, nullable=False, default="[]"),
    Column("source_reference", Text),
    Column("confidence", Float, nullable=False, default=0.0),
    Column("evidence_json", Text, nullable=False, default="{}"),
)

recovery_retrieval_cache = Table(
    "recovery_retrieval_cache",
    Base.metadata,
    Column("url", Text, primary_key=True),
    Column("final_url", Text, nullable=False, default=""),
    Column("title", Text, nullable=False, default=""),
    Column("content_type", Text, nullable=False, default=""),
    Column("text", Text, nullable=False, default=""),
    Column("text_chars", Integer, nullable=False, default=0),
    Column("sha256", Text, nullable=False, default=""),
    Column("bytes_read", Integer, nullable=False, default=0),
    Column("page_count", Integer),
    Column("extraction_method", Text, nullable=False, default=""),
    Column("http_status", Integer),
    Column("warnings_json", Text, nullable=False, default="[]"),
    Column("fetched_at", Text, nullable=False),
    Column("status", Text, nullable=False, default="ok"),
    Column("error", Text, nullable=False, default=""),
    Column("markdown", Text, nullable=False, default=""),
    Column("links_json", Text, nullable=False, default="[]"),
    Column("tables_json", Text, nullable=False, default="[]"),
    Column("metadata_json", Text, nullable=False, default="{}"),
    Column("canonical_url", Text, nullable=False, default=""),
    Column("crawler", Text, nullable=False, default="http"),
    Column("browser_ms", Integer),
)

RECOVERY_TABLES = (
    recovery_runs,
    recovery_course_reports,
    recovery_records,
    recovery_issues,
    recovery_repairs,
    recovery_answer_candidates,
    recovery_source_documents,
    recovery_source_candidates,
    recovery_verifications,
    recovery_passages,
    recovery_retrieval_cache,
)

DOMAIN_TABLES = (
    CourseRow.__table__,
    DatasetSourceRow.__table__,
    DocumentRow.__table__,
    PageRow.__table__,
    QuestionRow.__table__,
    SyllabusRow.__table__,
    SyllabusUnitRow.__table__,
    IngestionRunRow.__table__,
    IngestionResultRow.__table__,
    ProcessedTextRow.__table__,
    DocumentSectionRow.__table__,
    EmbeddingCacheRow.__table__,
    EmbeddingRow.__table__,
    KeyphraseRow.__table__,
    EntityRow.__table__,
    ConceptRow.__table__,
    TopicRow.__table__,
    TopicTermRow.__table__,
    TopicMemberRow.__table__,
    NlpRunRow.__table__,
    QuestionAnalysisRow.__table__,
    QuestionUnitMatchRow.__table__,
    QuestionTypeRow.__table__,
    QuestionBloomRow.__table__,
    QuestionDifficultyRow.__table__,
    QuestionSimilarityRow.__table__,
    UnderstandingRunRow.__table__,
    GraphNodeRow.__table__,
    GraphEdgeRow.__table__,
    GenerationBatchRow.__table__,
    GeneratedQuestionRow.__table__,
    StudentResponseRow.__table__,
    MisconceptionRow.__table__,
    ExamPaperRow.__table__,
    ExamPassageRow.__table__,
    ExamQuestionDetailRow.__table__,
    ExamAnswerRow.__table__,
    ExamProvenanceRow.__table__,
    ExamImportRunRow.__table__,
    ExamImportFileRow.__table__,
    *RECOVERY_TABLES,
)
