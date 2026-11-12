-- Remove the retired Phase 14.2 retired answer-generation tables.
--
-- Migration 0017 is retained unchanged so existing installations keep a valid
-- migration checksum. This forward migration removes only its two tables; it
-- does not touch questions, exam_answers, provenance, recovery evidence or any
-- other application data.
DROP TABLE IF EXISTS answer_ai_records;
DROP TABLE IF EXISTS answer_ai_runs;
