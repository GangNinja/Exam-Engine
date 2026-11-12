-- Phase 14.1: source discovery, verification evidence and passage groups.
-- Additive only. Existing recovery audit rows and raw datasets are untouched.

ALTER TABLE recovery_records ADD COLUMN verification_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS recovery_source_candidates (
    candidate_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT NOT NULL,
    course_id             TEXT NOT NULL,
    source_id             TEXT NOT NULL,
    title                 TEXT NOT NULL,
    url                   TEXT NOT NULL,
    document_type         TEXT NOT NULL,
    source_tier           INTEGER NOT NULL,
    reliability           REAL NOT NULL DEFAULT 0.0,
    provider              TEXT NOT NULL DEFAULT 'unknown',
    snippet               TEXT NOT NULL DEFAULT '',
    query                 TEXT NOT NULL DEFAULT '',
    classification_reason TEXT NOT NULL DEFAULT '',
    retrieval_status      TEXT NOT NULL DEFAULT 'discovered',
    discovered_at         TEXT NOT NULL,
    evidence_json         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS recovery_verifications (
    verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    course_id       TEXT NOT NULL,
    record_id       TEXT NOT NULL,
    status          TEXT NOT NULL,
    method          TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.0,
    answer_text     TEXT,
    evidence_json   TEXT NOT NULL DEFAULT '{}',
    issues_json     TEXT NOT NULL DEFAULT '[]',
    independent     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_passages (
    passage_id        TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    course_id         TEXT NOT NULL,
    passage_text      TEXT NOT NULL,
    parent_record_id  TEXT,
    child_record_ids_json TEXT NOT NULL DEFAULT '[]',
    source_reference  TEXT,
    confidence        REAL NOT NULL DEFAULT 0.0,
    evidence_json     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, course_id, passage_id)
);

CREATE INDEX IF NOT EXISTS idx_recovery_source_candidates_course
    ON recovery_source_candidates (run_id, course_id, document_type);
CREATE INDEX IF NOT EXISTS idx_recovery_verifications_record
    ON recovery_verifications (run_id, record_id, status);
CREATE INDEX IF NOT EXISTS idx_recovery_passages_course
    ON recovery_passages (run_id, course_id);
