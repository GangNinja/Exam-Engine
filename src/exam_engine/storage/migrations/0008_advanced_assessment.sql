-- Phase 11: advanced assessment intelligence.
--
-- Mutations and adversarial questions reuse the Phase 10 batch/question tables so
-- they inherit the same validation, review and export surface. Only their origin
-- and derivation are new.
ALTER TABLE generation_batches ADD COLUMN capability TEXT NOT NULL DEFAULT 'generation';

ALTER TABLE generated_questions ADD COLUMN origin TEXT NOT NULL DEFAULT 'generation';
ALTER TABLE generated_questions ADD COLUMN source_question_id TEXT;
ALTER TABLE generated_questions ADD COLUMN strategy TEXT;

CREATE INDEX idx_generated_questions_source
    ON generated_questions (source_question_id);
CREATE INDEX idx_generated_questions_origin
    ON generated_questions (course_id, origin, strategy);

-- Student answers are stored anonymously: the id is a hash of the caller's
-- reference, and no name, email or student number column exists.
CREATE TABLE student_responses (
    response_id       TEXT PRIMARY KEY,
    course_id         TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    syllabus_id       TEXT NOT NULL REFERENCES syllabi(syllabus_id) ON DELETE CASCADE,
    question_id       TEXT NOT NULL,
    answer_text       TEXT NOT NULL,
    correctness       TEXT NOT NULL,
    marks_awarded     REAL,
    marks_available   REAL,
    group_ref         TEXT,
    source            TEXT NOT NULL DEFAULT 'manual_import',
    fingerprint       TEXT NOT NULL,
    imported_at       TEXT NOT NULL
);

CREATE INDEX idx_student_responses_course
    ON student_responses (course_id, syllabus_id, question_id);
CREATE INDEX idx_student_responses_fingerprint
    ON student_responses (fingerprint);

-- One row per mined pattern. Support counts distinct response ids; duplicates are
-- never counted twice.
CREATE TABLE misconceptions (
    misconception_id     TEXT PRIMARY KEY,
    schema_version       TEXT NOT NULL,
    course_id            TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    syllabus_id          TEXT NOT NULL REFERENCES syllabi(syllabus_id) ON DELETE CASCADE,
    kind                 TEXT NOT NULL,
    label                TEXT NOT NULL,
    description          TEXT NOT NULL,
    concept_ids_json     TEXT NOT NULL DEFAULT '[]',
    concept_labels_json  TEXT NOT NULL DEFAULT '[]',
    question_ids_json    TEXT NOT NULL DEFAULT '[]',
    evidence_json        TEXT NOT NULL DEFAULT '[]',
    support_count        INTEGER NOT NULL DEFAULT 0,
    distinct_questions   INTEGER NOT NULL DEFAULT 0,
    distinct_groups      INTEGER NOT NULL DEFAULT 0,
    detection_method     TEXT NOT NULL,
    support_level        TEXT NOT NULL,
    confidence           REAL NOT NULL DEFAULT 0.0,
    suggested_correction TEXT,
    follow_up_json       TEXT NOT NULL DEFAULT '[]',
    review_status        TEXT NOT NULL,
    review_history_json  TEXT NOT NULL DEFAULT '[]',
    limitations_json     TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX idx_misconceptions_course
    ON misconceptions (course_id, syllabus_id, review_status, support_level);
CREATE INDEX idx_misconceptions_kind
    ON misconceptions (course_id, kind);
