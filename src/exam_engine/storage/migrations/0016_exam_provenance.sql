-- Phase 14.2: evidence-bound provenance for imported examination material.
--
-- One row per document, paper, question or answer scope. Every column that the
-- source material did not state stays NULL, and the status columns default to
-- unknown/unresolved so an import can never imply official identity.
--
-- The table is additive: existing questions, papers, answers and recovery
-- evidence are untouched.

CREATE TABLE IF NOT EXISTS exam_provenance (
    provenance_id           TEXT PRIMARY KEY,
    scope                   TEXT NOT NULL
        CHECK (scope IN ('document', 'paper', 'question', 'answer')),
    course_id               TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    document_id             TEXT REFERENCES documents(document_id) ON DELETE CASCADE,
    paper_id                TEXT REFERENCES exam_papers(paper_id) ON DELETE CASCADE,
    question_id             TEXT REFERENCES questions(question_id) ON DELETE CASCADE,
    answer_id               TEXT REFERENCES exam_answers(answer_id) ON DELETE CASCADE,
    source_id               TEXT REFERENCES dataset_sources(source_id) ON DELETE SET NULL,
    source_url              TEXT,
    source_publisher        TEXT,
    retrieved_at            TEXT,
    source_document_sha256  TEXT,
    examining_authority     TEXT,
    examination_name        TEXT,
    exam_year               INTEGER,
    exam_session            TEXT,
    subject_code            TEXT,
    paper_code              TEXT,
    set_code                TEXT,
    region                  TEXT,
    language                TEXT,
    original_question_id    TEXT,
    original_option_ids_json TEXT NOT NULL DEFAULT '[]',
    original_question_number TEXT,
    extraction_method       TEXT,
    extraction_confidence   REAL
        CHECK (extraction_confidence IS NULL
               OR (extraction_confidence >= 0 AND extraction_confidence <= 1)),
    source_classification   TEXT NOT NULL DEFAULT 'unknown'
        CHECK (source_classification IN ('official', 'third_party', 'compiled', 'mixed', 'unknown')),
    identity_status         TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (identity_status IN ('confirmed', 'partial', 'unresolved', 'not_applicable')),
    paper_identity_status   TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (paper_identity_status IN ('confirmed', 'partial', 'unresolved', 'not_applicable')),
    key_identity_status     TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (key_identity_status IN ('confirmed', 'partial', 'unresolved', 'not_applicable')),
    notes                   TEXT,
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exam_provenance_course
    ON exam_provenance(course_id, scope);
CREATE INDEX IF NOT EXISTS idx_exam_provenance_document
    ON exam_provenance(document_id);
CREATE INDEX IF NOT EXISTS idx_exam_provenance_paper
    ON exam_provenance(paper_id);
CREATE INDEX IF NOT EXISTS idx_exam_provenance_question
    ON exam_provenance(question_id);
CREATE INDEX IF NOT EXISTS idx_exam_provenance_answer
    ON exam_provenance(answer_id);
CREATE INDEX IF NOT EXISTS idx_exam_provenance_status
    ON exam_provenance(source_classification, identity_status, paper_identity_status);
