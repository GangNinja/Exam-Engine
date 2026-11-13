-- Phase 15: canonical passage context for the Phase 13 examination read model.
-- Additive only. The v5/v6 source files, question text, options and answers are
-- not rewritten. Existing independent questions keep a NULL passage_id.

CREATE TABLE IF NOT EXISTS exam_passages (
    passage_id              TEXT PRIMARY KEY,
    course_id               TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    passage_text            TEXT NOT NULL,
    source_reference        TEXT,
    source_file             TEXT,
    child_question_ids_json TEXT NOT NULL DEFAULT '[]',
    confidence              REAL NOT NULL DEFAULT 0.0,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exam_passages_course
    ON exam_passages(course_id);

ALTER TABLE exam_question_details ADD COLUMN passage_id TEXT
    REFERENCES exam_passages(passage_id) ON DELETE SET NULL;
ALTER TABLE exam_question_details ADD COLUMN passage_order INTEGER;

CREATE INDEX IF NOT EXISTS idx_exam_details_passage
    ON exam_question_details(passage_id, passage_order);
