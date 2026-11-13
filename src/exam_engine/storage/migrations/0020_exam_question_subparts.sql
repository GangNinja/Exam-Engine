-- Preserve the source subparts_json value without deriving or rewriting it.
-- Existing rows receive the explicit empty list; no answer or question text is
-- changed by this migration.

ALTER TABLE exam_question_details ADD COLUMN subparts_json TEXT NOT NULL DEFAULT '[]';
