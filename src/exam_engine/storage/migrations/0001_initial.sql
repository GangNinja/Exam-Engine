-- Phase 1 initial schema: courses, provenance, documents, pages, questions,
-- syllabi, syllabus units and ingestion run reporting.

CREATE TABLE IF NOT EXISTS courses (
    course_id         TEXT PRIMARY KEY,
    course_code       TEXT NOT NULL,
    title             TEXT NOT NULL,
    institution_code  TEXT NOT NULL,
    institution_name  TEXT NOT NULL,
    department        TEXT,
    programme         TEXT,
    academic_year     TEXT,
    syllabus_version  TEXT,
    description       TEXT,
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_courses_offering
    ON courses (institution_code, course_code, academic_year, syllabus_version);

CREATE TABLE IF NOT EXISTS dataset_sources (
    source_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    dataset_type    TEXT NOT NULL,
    retrieved_at    TEXT NOT NULL,
    license         TEXT,
    license_url     TEXT,
    source_url      TEXT,
    publisher       TEXT,
    usage_terms     TEXT,
    notes           TEXT,
    is_synthetic    INTEGER NOT NULL DEFAULT 0,
    external_dataset INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    document_id        TEXT PRIMARY KEY,
    course_id          TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    source_id          TEXT NOT NULL REFERENCES dataset_sources (source_id) ON DELETE RESTRICT,
    kind               TEXT NOT NULL,
    format             TEXT NOT NULL,
    title              TEXT NOT NULL,
    original_filename  TEXT NOT NULL,
    relative_path      TEXT,
    content_sha256     TEXT NOT NULL,
    text_fingerprint   TEXT NOT NULL DEFAULT '',
    byte_size          INTEGER NOT NULL,
    page_count         INTEGER NOT NULL DEFAULT 0,
    char_count         INTEGER NOT NULL DEFAULT 0,
    extraction_status  TEXT NOT NULL,
    quality_score      REAL NOT NULL DEFAULT 1.0,
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    language           TEXT,
    academic_year      TEXT,
    publication_date   TEXT,
    ingested_at        TEXT NOT NULL,
    pipeline_version   TEXT NOT NULL,
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    warnings_json      TEXT NOT NULL DEFAULT '[]',
    UNIQUE (course_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_documents_course ON documents (course_id);
CREATE INDEX IF NOT EXISTS idx_documents_fingerprint
    ON documents (course_id, text_fingerprint);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents (source_id);

CREATE TABLE IF NOT EXISTS pages (
    page_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    page_number  INTEGER NOT NULL,
    text         TEXT NOT NULL DEFAULT '',
    char_count   INTEGER NOT NULL DEFAULT 0,
    method       TEXT NOT NULL,
    has_images   INTEGER NOT NULL DEFAULT 0,
    ocr_attempted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (document_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_pages_document ON pages (document_id, page_number);

CREATE TABLE IF NOT EXISTS questions (
    question_id       TEXT PRIMARY KEY,
    document_id       TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    course_id         TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    question_number   TEXT,
    number_sort       INTEGER,
    section           TEXT,
    text              TEXT NOT NULL,
    marks             REAL,
    marks_source      TEXT,
    page_number       INTEGER,
    source_locator    TEXT,
    confidence        REAL NOT NULL DEFAULT 0.5,
    needs_review      INTEGER NOT NULL DEFAULT 0,
    fingerprint       TEXT NOT NULL DEFAULT '',
    duplicate_of      TEXT,
    uncertainties_json TEXT NOT NULL DEFAULT '[]',
    metadata_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_questions_course ON questions (course_id);
CREATE INDEX IF NOT EXISTS idx_questions_document ON questions (document_id, number_sort);
CREATE INDEX IF NOT EXISTS idx_questions_fingerprint ON questions (course_id, fingerprint);

CREATE TABLE IF NOT EXISTS syllabi (
    syllabus_id       TEXT PRIMARY KEY,
    course_id         TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    document_id       TEXT NOT NULL UNIQUE REFERENCES documents (document_id) ON DELETE CASCADE,
    source_id         TEXT NOT NULL REFERENCES dataset_sources (source_id) ON DELETE RESTRICT,
    title             TEXT,
    confidence        REAL NOT NULL DEFAULT 0.0,
    needs_review      INTEGER NOT NULL DEFAULT 0,
    uncertainties_json TEXT NOT NULL DEFAULT '[]',
    ingested_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_syllabi_course ON syllabi (course_id);

CREATE TABLE IF NOT EXISTS syllabus_units (
    unit_id           TEXT PRIMARY KEY,
    syllabus_id       TEXT NOT NULL REFERENCES syllabi (syllabus_id) ON DELETE CASCADE,
    unit_label        TEXT NOT NULL,
    unit_number       INTEGER,
    title             TEXT,
    hours             REAL,
    topics_json       TEXT NOT NULL DEFAULT '[]',
    raw_text          TEXT NOT NULL DEFAULT '',
    page_number       INTEGER,
    confidence        REAL NOT NULL DEFAULT 0.5,
    uncertainties_json TEXT NOT NULL DEFAULT '[]',
    metadata_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_syllabus_units_syllabus ON syllabus_units (syllabus_id);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    manifest_path   TEXT,
    source_id       TEXT,
    status          TEXT NOT NULL,
    totals_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS ingestion_results (
    result_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL REFERENCES ingestion_runs (run_id) ON DELETE CASCADE,
    source_path          TEXT NOT NULL,
    course_id            TEXT NOT NULL,
    status               TEXT NOT NULL,
    document_id          TEXT,
    page_count           INTEGER NOT NULL DEFAULT 0,
    question_count       INTEGER NOT NULL DEFAULT 0,
    syllabus_unit_count  INTEGER NOT NULL DEFAULT 0,
    duplicate_of         TEXT,
    errors_json          TEXT NOT NULL DEFAULT '[]',
    warnings_json        TEXT NOT NULL DEFAULT '[]',
    duration_ms          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ingestion_results_run ON ingestion_results (run_id);
