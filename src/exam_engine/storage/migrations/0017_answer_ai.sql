-- Phase 14.2: DeepSeek answer evaluation and candidate generation.
--
-- These tables are additive. They never replace exam_answers, never alter
-- release gates, and never promote an AI result to official verification.
-- `source_answer_id` references the evaluated stored answer; a generated
-- candidate has no source answer and remains a reviewable proposal.

CREATE TABLE IF NOT EXISTS answer_ai_runs (
    run_id              TEXT PRIMARY KEY,
    operation           TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    status              TEXT NOT NULL,
    request_json        TEXT NOT NULL DEFAULT '{}',
    counts_json         TEXT NOT NULL DEFAULT '{}',
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd  REAL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    error_code          TEXT,
    error_message       TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answer_ai_runs_operation
    ON answer_ai_runs(operation, started_at);

CREATE TABLE IF NOT EXISTS answer_ai_records (
    record_id                 TEXT PRIMARY KEY,
    run_id                    TEXT NOT NULL
                              REFERENCES answer_ai_runs(run_id) ON DELETE CASCADE,
    operation                 TEXT NOT NULL,
    status                    TEXT NOT NULL,
    provider                  TEXT NOT NULL,
    model                     TEXT NOT NULL,
    prompt_version            TEXT NOT NULL,
    question_id               TEXT NOT NULL
                              REFERENCES questions(question_id) ON DELETE CASCADE,
    source_answer_id          TEXT
                              REFERENCES exam_answers(answer_id) ON DELETE SET NULL,
    course_id                 TEXT NOT NULL
                              REFERENCES courses(course_id) ON DELETE CASCADE,
    category_id               TEXT NOT NULL,
    subject_code              TEXT NOT NULL,
    question_type             TEXT NOT NULL,
    exam_year                 INTEGER,
    parent_record_id          TEXT
                              REFERENCES answer_ai_records(record_id) ON DELETE SET NULL,
    question_text             TEXT NOT NULL,
    existing_answer_text      TEXT,
    payload_json              TEXT NOT NULL DEFAULT '{}',
    answer_text               TEXT,
    correct_option_key        TEXT,
    explanation               TEXT,
    solution_steps_json       TEXT NOT NULL DEFAULT '[]',
    formulas_json             TEXT NOT NULL DEFAULT '[]',
    correctness               TEXT,
    completeness_score        REAL,
    reasoning_score           REAL,
    calculation_score         REAL,
    relevance_score           REAL,
    format_score              REAL,
    identified_errors_json    TEXT NOT NULL DEFAULT '[]',
    suggested_correction      TEXT,
    confidence                REAL,
    prompt_tokens             INTEGER NOT NULL DEFAULT 0,
    completion_tokens         INTEGER NOT NULL DEFAULT 0,
    total_tokens              INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd        REAL,
    provenance_classification TEXT NOT NULL DEFAULT 'ai_assessment',
    provenance_id             TEXT
                              REFERENCES exam_provenance(provenance_id) ON DELETE SET NULL,
    reference_status          TEXT NOT NULL DEFAULT 'ai_only_unverified_reference',
    review_status             TEXT NOT NULL DEFAULT 'pending_review',
    review_history_json       TEXT NOT NULL DEFAULT '[]',
    error_code                TEXT,
    error_message             TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answer_ai_records_run
    ON answer_ai_records(run_id, operation);
CREATE INDEX IF NOT EXISTS idx_answer_ai_records_question
    ON answer_ai_records(question_id);
CREATE INDEX IF NOT EXISTS idx_answer_ai_records_category
    ON answer_ai_records(category_id, status);
CREATE INDEX IF NOT EXISTS idx_answer_ai_records_review
    ON answer_ai_records(review_status, created_at);
CREATE INDEX IF NOT EXISTS idx_answer_ai_records_parent
    ON answer_ai_records(parent_record_id);
