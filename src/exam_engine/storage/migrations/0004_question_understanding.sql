-- Phase 3: question understanding and classification.
--
-- Everything here is derived from Phase 1 questions plus Phase 2 signals. Rows
-- are replaced per course on every run; the original question text is never
-- modified. UNKNOWN/UNMAPPED outcomes are first-class results, not errors.

CREATE TABLE IF NOT EXISTS question_analysis (
    analysis_id            TEXT PRIMARY KEY,
    question_id            TEXT NOT NULL UNIQUE
                               REFERENCES questions (question_id) ON DELETE CASCADE,
    course_id              TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    document_id            TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    text_sha256            TEXT NOT NULL,
    word_count             INTEGER NOT NULL DEFAULT 0,
    sentence_count         INTEGER NOT NULL DEFAULT 0,
    subquestion_count      INTEGER NOT NULL DEFAULT 0,
    subparts_json          TEXT NOT NULL DEFAULT '[]',
    references_json        TEXT NOT NULL DEFAULT '[]',
    mapping_status         TEXT NOT NULL,
    mapping_confidence     REAL NOT NULL DEFAULT 0.0,
    top_unit_id            TEXT,
    primary_type           TEXT,
    primary_type_confidence REAL NOT NULL DEFAULT 0.0,
    difficulty_label       TEXT NOT NULL DEFAULT 'unknown',
    difficulty_score       REAL NOT NULL DEFAULT 0.0,
    difficulty_confidence  REAL NOT NULL DEFAULT 0.0,
    bloom_primary          TEXT,
    bloom_confidence       REAL NOT NULL DEFAULT 0.0,
    structure_confidence   REAL NOT NULL DEFAULT 0.0,
    uncertainties_json     TEXT NOT NULL DEFAULT '[]',
    created_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_question_analysis_course
    ON question_analysis (course_id, mapping_status);
CREATE INDEX IF NOT EXISTS idx_question_analysis_document
    ON question_analysis (document_id);

CREATE TABLE IF NOT EXISTS question_unit_matches (
    match_id        TEXT PRIMARY KEY,
    question_id     TEXT NOT NULL REFERENCES questions (question_id) ON DELETE CASCADE,
    course_id       TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    unit_id         TEXT NOT NULL REFERENCES syllabus_units (unit_id) ON DELETE CASCADE,
    syllabus_id     TEXT NOT NULL REFERENCES syllabi (syllabus_id) ON DELETE CASCADE,
    unit_label      TEXT NOT NULL,
    unit_title      TEXT,
    topic           TEXT,
    score           REAL NOT NULL DEFAULT 0.0,
    rank            INTEGER NOT NULL,
    method          TEXT NOT NULL,
    matched_terms_json TEXT NOT NULL DEFAULT '[]',
    evidence_json   TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_question_unit_matches_course
    ON question_unit_matches (course_id, question_id, rank);
CREATE INDEX IF NOT EXISTS idx_question_unit_matches_unit
    ON question_unit_matches (unit_id);

CREATE TABLE IF NOT EXISTS question_types (
    type_id      TEXT PRIMARY KEY,
    question_id  TEXT NOT NULL REFERENCES questions (question_id) ON DELETE CASCADE,
    course_id    TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    score        REAL NOT NULL DEFAULT 0.0,
    rank         INTEGER NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0.0,
    method       TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_question_types_course
    ON question_types (course_id, label, question_id);

CREATE TABLE IF NOT EXISTS question_bloom (
    bloom_id     TEXT PRIMARY KEY,
    question_id  TEXT NOT NULL REFERENCES questions (question_id) ON DELETE CASCADE,
    course_id    TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    level        TEXT NOT NULL,
    score        REAL NOT NULL DEFAULT 0.0,
    rank         INTEGER NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0.0,
    method       TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_question_bloom_course
    ON question_bloom (course_id, level, question_id);

CREATE TABLE IF NOT EXISTS question_difficulty (
    difficulty_id TEXT PRIMARY KEY,
    question_id   TEXT NOT NULL REFERENCES questions (question_id) ON DELETE CASCADE,
    course_id     TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    label         TEXT NOT NULL DEFAULT 'unknown',
    score         REAL NOT NULL DEFAULT 0.0,
    confidence    REAL NOT NULL DEFAULT 0.0,
    method        TEXT NOT NULL,
    estimated     INTEGER NOT NULL DEFAULT 1,
    features_json TEXT NOT NULL DEFAULT '{}',
    explanation_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (question_id, method)
);

CREATE INDEX IF NOT EXISTS idx_question_difficulty_course
    ON question_difficulty (course_id, label);

CREATE TABLE IF NOT EXISTS question_similarity (
    pair_id             TEXT PRIMARY KEY,
    course_id           TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    question_id         TEXT NOT NULL REFERENCES questions (question_id) ON DELETE CASCADE,
    matched_question_id TEXT NOT NULL REFERENCES questions (question_id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    score               REAL NOT NULL DEFAULT 0.0,
    lexical_score       REAL NOT NULL DEFAULT 0.0,
    semantic_score      REAL NOT NULL DEFAULT 0.0,
    method              TEXT NOT NULL,
    evidence_json       TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL,
    UNIQUE (course_id, question_id, matched_question_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_question_similarity_course
    ON question_similarity (course_id, question_id);
CREATE INDEX IF NOT EXISTS idx_question_similarity_match
    ON question_similarity (matched_question_id);

CREATE TABLE IF NOT EXISTS understanding_runs (
    run_id        TEXT PRIMARY KEY,
    course_id     TEXT,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    methods_json  TEXT NOT NULL DEFAULT '[]',
    model_ids_json TEXT NOT NULL DEFAULT '[]',
    totals_json   TEXT NOT NULL DEFAULT '{}',
    config_json   TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json   TEXT NOT NULL DEFAULT '[]',
    duration_ms   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_understanding_runs_course
    ON understanding_runs (course_id, started_at);
