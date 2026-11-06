-- Phase 13: personal examination dataset integration.
--
-- These tables extend the existing Phase 1 hierarchy without altering it:
-- courses stay the isolation boundary, documents stay the source-file record and
-- questions stay the canonical extracted question. Exam-specific occurrence,
-- option and answer data lives here so earlier phases keep reading `questions`.

CREATE TABLE IF NOT EXISTS exam_papers (
    paper_id              TEXT PRIMARY KEY,
    course_id             TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    document_id           TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    category_id           TEXT NOT NULL,
    subject_code          TEXT NOT NULL,
    paper_label           TEXT NOT NULL,
    exam_year             INTEGER,
    source_question_prefix TEXT,
    question_count        INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    UNIQUE (course_id, paper_label)
);

CREATE INDEX IF NOT EXISTS idx_exam_papers_course
    ON exam_papers(course_id, exam_year);
CREATE INDEX IF NOT EXISTS idx_exam_papers_category
    ON exam_papers(category_id, subject_code);


CREATE TABLE IF NOT EXISTS exam_question_details (
    question_id             TEXT PRIMARY KEY
        REFERENCES questions(question_id) ON DELETE CASCADE,
    course_id               TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    paper_id                TEXT NOT NULL REFERENCES exam_papers(paper_id) ON DELETE CASCADE,
    category_id             TEXT NOT NULL,
    subject_code            TEXT NOT NULL,
    source_question_id      TEXT NOT NULL,
    source_row              INTEGER,
    original_question_number TEXT,
    question_type           TEXT NOT NULL DEFAULT 'unknown',
    question_type_raw       TEXT,
    options_json            TEXT NOT NULL DEFAULT '[]',
    has_options             INTEGER NOT NULL DEFAULT 0,
    exam_year               INTEGER,
    source_file             TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    UNIQUE (course_id, source_question_id)
);

CREATE INDEX IF NOT EXISTS idx_exam_question_details_course
    ON exam_question_details(course_id, exam_year, question_type);
CREATE INDEX IF NOT EXISTS idx_exam_question_details_paper
    ON exam_question_details(paper_id);


CREATE TABLE IF NOT EXISTS exam_answers (
    answer_id          TEXT PRIMARY KEY,
    question_id        TEXT NOT NULL UNIQUE
        REFERENCES questions(question_id) ON DELETE CASCADE,
    course_id          TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    source_answer_id   TEXT,
    answer_text        TEXT NOT NULL,
    answer_type        TEXT NOT NULL,
    answer_type_raw    TEXT,
    correct_option_key TEXT,
    solution_steps     TEXT,
    match_method       TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 1.0,
    warnings_json      TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exam_answers_course
    ON exam_answers(course_id, answer_type);


CREATE TABLE IF NOT EXISTS exam_import_runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    totals_json  TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json  TEXT NOT NULL DEFAULT '[]',
    duration_ms  INTEGER NOT NULL DEFAULT 0
);


CREATE TABLE IF NOT EXISTS exam_import_files (
    file_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL
        REFERENCES exam_import_runs(run_id) ON DELETE CASCADE,
    category_id         TEXT NOT NULL,
    subject_code        TEXT NOT NULL,
    course_id           TEXT NOT NULL,
    relative_path       TEXT NOT NULL,
    kind                TEXT NOT NULL,
    status              TEXT NOT NULL,
    row_count           INTEGER NOT NULL DEFAULT 0,
    imported_questions  INTEGER NOT NULL DEFAULT 0,
    imported_answers    INTEGER NOT NULL DEFAULT 0,
    skipped_count       INTEGER NOT NULL DEFAULT 0,
    failed_count        INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    report_json         TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_exam_import_files_run
    ON exam_import_files(run_id);
CREATE INDEX IF NOT EXISTS idx_exam_import_files_course
    ON exam_import_files(course_id, kind);
