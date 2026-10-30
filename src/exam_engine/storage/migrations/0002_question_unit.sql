-- Questions can belong to a syllabus unit/chapter when the source states it
-- (question banks usually do; free-text papers usually only give sections).

ALTER TABLE questions ADD COLUMN unit TEXT;

CREATE INDEX IF NOT EXISTS idx_questions_unit ON questions (course_id, unit);
