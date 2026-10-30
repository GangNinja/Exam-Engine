-- Phase 11: remember whether a response carried a student or group reference.
--
-- The reference itself is never stored (the response id stays a hash), but whether
-- one was supplied cannot be recovered later from a hash. Recording the fact lets
-- misconception analysis state honestly whether independent-response counts can be
-- established for the imported data.
ALTER TABLE student_responses ADD COLUMN identified INTEGER NOT NULL DEFAULT 0;
