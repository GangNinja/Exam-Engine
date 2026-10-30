-- Phase 2 NLP foundation.
--
-- Every row here is derived data: it can be deleted and recomputed from the
-- Phase 1 tables without losing provenance. Raw text is never overwritten; the
-- normalised representation lives beside it with its own config hash.

CREATE TABLE IF NOT EXISTS processed_texts (
    text_id          TEXT PRIMARY KEY,
    course_id        TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    owner_type       TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    document_id      TEXT REFERENCES documents (document_id) ON DELETE CASCADE,
    raw_sha256       TEXT NOT NULL,
    config_id        TEXT NOT NULL,
    language         TEXT,
    text             TEXT NOT NULL,
    sentence_count   INTEGER NOT NULL DEFAULT 0,
    token_count      INTEGER NOT NULL DEFAULT 0,
    char_count       INTEGER NOT NULL DEFAULT 0,
    sentences_json   TEXT NOT NULL DEFAULT '[]',
    uncertainties_json TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    UNIQUE (owner_type, owner_id)
);

CREATE INDEX IF NOT EXISTS idx_processed_texts_course ON processed_texts (course_id, owner_type);
CREATE INDEX IF NOT EXISTS idx_processed_texts_document ON processed_texts (document_id);

CREATE TABLE IF NOT EXISTS document_sections (
    section_id     TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    course_id      TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    section_index  INTEGER NOT NULL,
    heading        TEXT,
    text           TEXT NOT NULL,
    char_start     INTEGER NOT NULL DEFAULT 0,
    char_end       INTEGER NOT NULL DEFAULT 0,
    page_start     INTEGER,
    page_end       INTEGER,
    UNIQUE (document_id, section_index)
);

CREATE INDEX IF NOT EXISTS idx_document_sections_document
    ON document_sections (document_id, section_index);

-- Vector cache: text-level, model-scoped. Model ids embed corpus fingerprints
-- when a model is fitted, so a changed corpus cannot return stale vectors.
CREATE TABLE IF NOT EXISTS embedding_cache (
    cache_key    TEXT PRIMARY KEY,
    model_id     TEXT NOT NULL,
    text_sha256  TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (model_id, text_sha256)
);

CREATE TABLE IF NOT EXISTS embeddings (
    embedding_id  TEXT PRIMARY KEY,
    course_id     TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    owner_type    TEXT NOT NULL,
    owner_id      TEXT NOT NULL,
    document_id   TEXT REFERENCES documents (document_id) ON DELETE CASCADE,
    model_id      TEXT NOT NULL,
    dim           INTEGER NOT NULL,
    cache_key     TEXT NOT NULL REFERENCES embedding_cache (cache_key) ON DELETE CASCADE,
    text_sha256   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (owner_type, owner_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_course ON embeddings (course_id, owner_type);

CREATE TABLE IF NOT EXISTS keyphrases (
    keyphrase_id  TEXT PRIMARY KEY,
    course_id     TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    owner_type    TEXT NOT NULL,
    owner_id      TEXT NOT NULL,
    document_id   TEXT REFERENCES documents (document_id) ON DELETE CASCADE,
    term          TEXT NOT NULL,
    normalized    TEXT NOT NULL,
    score         REAL NOT NULL,
    method        TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    n_gram        INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_keyphrases_owner
    ON keyphrases (course_id, owner_type, owner_id, method, rank);

CREATE TABLE IF NOT EXISTS entities (
    entity_id     TEXT PRIMARY KEY,
    course_id     TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    document_id   TEXT REFERENCES documents (document_id) ON DELETE CASCADE,
    owner_type    TEXT NOT NULL,
    owner_id      TEXT NOT NULL,
    text          TEXT NOT NULL,
    normalized    TEXT NOT NULL,
    label         TEXT NOT NULL,
    method        TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.5,
    start_char    INTEGER NOT NULL DEFAULT 0,
    end_char      INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_entities_course ON entities (course_id, label);
CREATE INDEX IF NOT EXISTS idx_entities_owner ON entities (owner_type, owner_id);

CREATE TABLE IF NOT EXISTS concept_candidates (
    concept_id    TEXT PRIMARY KEY,
    course_id     TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    term          TEXT NOT NULL,
    normalized    TEXT NOT NULL,
    origin        TEXT NOT NULL,
    method        TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.5,
    verified      INTEGER NOT NULL DEFAULT 0,
    unit_id       TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    UNIQUE (course_id, origin, normalized)
);

CREATE INDEX IF NOT EXISTS idx_concepts_course ON concept_candidates (course_id, origin);

CREATE TABLE IF NOT EXISTS nlp_topics (
    topic_id      TEXT PRIMARY KEY,
    course_id     TEXT NOT NULL REFERENCES courses (course_id) ON DELETE CASCADE,
    scope         TEXT NOT NULL,
    scope_id      TEXT,
    method        TEXT NOT NULL,
    k             INTEGER NOT NULL,
    label         TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    params_json   TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nlp_topics_course ON nlp_topics (course_id, scope);

CREATE TABLE IF NOT EXISTS nlp_topic_terms (
    topic_id   TEXT NOT NULL REFERENCES nlp_topics (topic_id) ON DELETE CASCADE,
    term       TEXT NOT NULL,
    normalized TEXT NOT NULL,
    weight     REAL NOT NULL,
    rank       INTEGER NOT NULL,
    PRIMARY KEY (topic_id, normalized)
);

CREATE TABLE IF NOT EXISTS nlp_topic_members (
    topic_id    TEXT NOT NULL REFERENCES nlp_topics (topic_id) ON DELETE CASCADE,
    owner_type  TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    document_id TEXT,
    weight      REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (topic_id, owner_type, owner_id)
);

CREATE TABLE IF NOT EXISTS nlp_runs (
    run_id       TEXT PRIMARY KEY,
    course_id    TEXT,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    model_ids_json   TEXT NOT NULL DEFAULT '[]',
    methods_json     TEXT NOT NULL DEFAULT '[]',
    totals_json      TEXT NOT NULL DEFAULT '{}',
    config_json      TEXT NOT NULL DEFAULT '{}',
    warnings_json    TEXT NOT NULL DEFAULT '[]',
    errors_json      TEXT NOT NULL DEFAULT '[]',
    duration_ms      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_nlp_runs_course ON nlp_runs (course_id, started_at);
