-- Phase 2 status correction.
--
-- Before this change the service downgraded every run with any warning to
-- `partial`, including informational notes such as `lexical_entities` and the
-- section-topic fallback. The service now downgrades only warning/error
-- findings. This data-only migration aligns stored runs with that rule while
-- leaving their warnings and analysis output untouched.

UPDATE nlp_runs
SET status = 'ok'
WHERE status = 'partial'
  AND errors_json = '[]'
  AND json_valid(warnings_json)
  AND NOT EXISTS (
      SELECT 1
      FROM json_each(nlp_runs.warnings_json)
      WHERE json_extract(json_each.value, '$.severity') IN ('warning', 'error')
  );
