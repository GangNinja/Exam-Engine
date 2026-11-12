-- Dataset Intelligence & Recovery (Phase 14).
-- Additive only: original datasets, questions and answers are not modified.
-- Detail rows are append-only audit evidence; release decisions remain separate.

CREATE TABLE IF NOT EXISTS recovery_runs (
    run_id           TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,
    mode             TEXT NOT NULL,
    dry_run          INTEGER NOT NULL DEFAULT 0,
    root             TEXT NOT NULL,
    recovery_version TEXT NOT NULL,
    rule_version     TEXT NOT NULL,
    options_json     TEXT NOT NULL DEFAULT '{}',
    totals_json      TEXT NOT NULL DEFAULT '{}',
    warnings_json    TEXT NOT NULL DEFAULT '[]',
    errors_json      TEXT NOT NULL DEFAULT '[]',
    duration_ms      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_course_reports (
    run_id          TEXT NOT NULL,
    course_id       TEXT NOT NULL,
    course_code     TEXT NOT NULL,
    course_name     TEXT NOT NULL,
    category_id     TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    source_sha256   TEXT NOT NULL,
    schema_json     TEXT NOT NULL DEFAULT '{}',
    row_count       INTEGER NOT NULL DEFAULT 0,
    analyzed_count  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    quality_json    TEXT NOT NULL DEFAULT '{}',
    counts_json     TEXT NOT NULL DEFAULT '{}',
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    errors_json     TEXT NOT NULL DEFAULT '[]',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (run_id, course_id)
);

CREATE TABLE IF NOT EXISTS recovery_records (
    run_id              TEXT NOT NULL,
    record_id           TEXT NOT NULL,
    course_id           TEXT NOT NULL,
    source_file         TEXT NOT NULL,
    source_row          INTEGER NOT NULL,
    source_question_id  TEXT NOT NULL,
    source_sha256       TEXT NOT NULL,
    analysis_hash       TEXT NOT NULL,
    original_json       TEXT NOT NULL,
    normalized_json     TEXT NOT NULL,
    repaired_json       TEXT NOT NULL,
    structure_json      TEXT NOT NULL,
    quality_json        TEXT NOT NULL,
    answer_status       TEXT NOT NULL,
    source_fidelity     TEXT NOT NULL,
    release_status      TEXT NOT NULL,
    issue_count         INTEGER NOT NULL DEFAULT 0,
    blocking_count      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS recovery_issues (
    issue_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    course_id      TEXT NOT NULL,
    record_id      TEXT NOT NULL,
    code           TEXT NOT NULL,
    category       TEXT NOT NULL,
    severity       TEXT NOT NULL,
    message        TEXT NOT NULL,
    evidence_json  TEXT NOT NULL DEFAULT '{}',
    confidence     REAL NOT NULL DEFAULT 0.5,
    remediation    TEXT NOT NULL DEFAULT '',
    blocking       INTEGER NOT NULL DEFAULT 0,
    field          TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_repairs (
    repair_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    course_id      TEXT NOT NULL,
    record_id      TEXT NOT NULL,
    repair_type    TEXT NOT NULL,
    field          TEXT NOT NULL,
    before_json    TEXT NOT NULL DEFAULT 'null',
    after_json     TEXT NOT NULL DEFAULT 'null',
    method         TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 0.5,
    risk           TEXT NOT NULL DEFAULT 'low',
    status         TEXT NOT NULL DEFAULT 'proposed',
    reason         TEXT NOT NULL DEFAULT '',
    reviewer       TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_answer_candidates (
    candidate_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    course_id           TEXT NOT NULL,
    record_id           TEXT NOT NULL,
    answer_text         TEXT,
    source_type         TEXT NOT NULL,
    source_name         TEXT NOT NULL,
    source_url          TEXT,
    source_reference    TEXT,
    verification_status TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0.0,
    method              TEXT NOT NULL,
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    retrieved_at        TEXT NOT NULL,
    official            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recovery_source_documents (
    source_document_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      TEXT NOT NULL,
    course_id                   TEXT NOT NULL,
    path                        TEXT NOT NULL,
    kind                        TEXT NOT NULL,
    sha256                      TEXT NOT NULL,
    page_count                  INTEGER,
    text_chars                  INTEGER,
    question_fidelity_available INTEGER NOT NULL DEFAULT 0,
    metadata_json               TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_recovery_course_course ON recovery_course_reports (course_id);
CREATE INDEX IF NOT EXISTS idx_recovery_records_course ON recovery_records (course_id, release_status);
CREATE INDEX IF NOT EXISTS idx_recovery_records_answer ON recovery_records (course_id, answer_status);
CREATE INDEX IF NOT EXISTS idx_recovery_issues_course ON recovery_issues (run_id, course_id, severity);
CREATE INDEX IF NOT EXISTS idx_recovery_issues_code ON recovery_issues (run_id, code);
CREATE INDEX IF NOT EXISTS idx_recovery_repairs_record ON recovery_repairs (run_id, record_id, status);
CREATE INDEX IF NOT EXISTS idx_recovery_candidates_record ON recovery_answer_candidates (run_id, record_id);
CREATE INDEX IF NOT EXISTS idx_recovery_sources_course ON recovery_source_documents (run_id, course_id, kind);
