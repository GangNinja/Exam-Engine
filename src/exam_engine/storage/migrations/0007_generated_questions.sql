-- Phase 10: generated questions live separately from historical questions and keep
-- their grounding, validation and review state. Batches store the reproducible
-- request plus the full report, so rejections remain inspectable.

CREATE TABLE generation_batches (
    batch_id         TEXT PRIMARY KEY,
    course_id        TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    syllabus_id      TEXT NOT NULL REFERENCES syllabi(syllabus_id) ON DELETE CASCADE,
    status           TEXT NOT NULL,
    provider         TEXT NOT NULL,
    model            TEXT,
    request_json     TEXT NOT NULL DEFAULT '{}',
    parameters_json  TEXT NOT NULL DEFAULT '{}',
    counts_json      TEXT NOT NULL DEFAULT '{}',
    report_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX idx_generation_batches_course ON generation_batches (course_id, created_at);
CREATE INDEX idx_generation_batches_syllabus ON generation_batches (syllabus_id);

CREATE TABLE generated_questions (
    question_id                 TEXT PRIMARY KEY,
    batch_id                    TEXT NOT NULL
                                REFERENCES generation_batches(batch_id) ON DELETE CASCADE,
    course_id                   TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    syllabus_id                 TEXT NOT NULL REFERENCES syllabi(syllabus_id) ON DELETE CASCADE,
    unit_id                     TEXT NOT NULL,
    unit_label                  TEXT NOT NULL,
    unit_title                  TEXT,
    concept_ids_json            TEXT NOT NULL DEFAULT '[]',
    concept_labels_json         TEXT NOT NULL DEFAULT '[]',
    learning_objective          TEXT NOT NULL,
    format                      TEXT NOT NULL,
    question_type               TEXT,
    requested_difficulty        TEXT NOT NULL,
    estimated_difficulty        TEXT NOT NULL,
    difficulty_confidence       REAL NOT NULL DEFAULT 0.0,
    difficulty_method           TEXT NOT NULL,
    requested_bloom             TEXT NOT NULL,
    estimated_bloom             TEXT,
    bloom_confidence            REAL NOT NULL DEFAULT 0.0,
    marks                       REAL NOT NULL,
    text                        TEXT NOT NULL,
    options_json                TEXT NOT NULL DEFAULT '[]',
    correct_option_key          TEXT,
    answer_key                  TEXT,
    explanation                 TEXT,
    solution_steps_json         TEXT NOT NULL DEFAULT '[]',
    distractor_explanations_json TEXT NOT NULL DEFAULT '{}',
    source_references_json      TEXT NOT NULL DEFAULT '[]',
    generation_metadata_json    TEXT NOT NULL DEFAULT '{}',
    validation_status           TEXT NOT NULL,
    validation_json             TEXT NOT NULL DEFAULT '{}',
    review_status               TEXT NOT NULL,
    review_history_json         TEXT NOT NULL DEFAULT '[]',
    revision                    INTEGER NOT NULL DEFAULT 1,
    warnings_json               TEXT NOT NULL DEFAULT '[]',
    limitations_json            TEXT NOT NULL DEFAULT '[]',
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE INDEX idx_generated_questions_course
    ON generated_questions (course_id, syllabus_id, review_status);
CREATE INDEX idx_generated_questions_batch ON generated_questions (batch_id);
CREATE INDEX idx_generated_questions_unit ON generated_questions (unit_id);
