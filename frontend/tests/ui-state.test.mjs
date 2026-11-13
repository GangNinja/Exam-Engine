import assert from "node:assert/strict";
import test from "node:test";
import {
  TUTOR_STATES,
  barData,
  barSeries,
  coverageLabel,
  coverageTone,
  formatPercent,
  segmentData,
  truncationNotes,
  tutorStateFor,
} from "../../src/exam_engine/dashboard/static/ui-state.js";

test("tutor state follows only explicit backend signals", () => {
  assert.deepEqual(TUTOR_STATES, [
    "idle",
    "thinking",
    "speaking",
    "positive",
    "corrective",
    "warning",
  ]);
  assert.equal(tutorStateFor({ status: "answered" }), "speaking");
  assert.equal(tutorStateFor({ status: "insufficient_evidence" }), "corrective");
  assert.equal(tutorStateFor({ status: "unavailable" }), "warning");
  assert.equal(tutorStateFor({ status: "answered", state: "positive" }), "positive");
  assert.equal(tutorStateFor({ status: "answered", state: "warning" }), "warning");
  assert.equal(tutorStateFor(null), "warning");
  assert.equal(tutorStateFor({ state: "not-a-state" }), "warning");
});

test("coverage labels and tones are stable for every backend status", () => {
  for (const status of [
    "covered",
    "low_coverage",
    "unmapped",
    "uncertain",
    "insufficient_data",
  ]) {
    assert.ok(coverageLabel(status));
    assert.ok(coverageTone(status));
  }
  assert.equal(coverageLabel("low_coverage"), "Low coverage");
  assert.equal(coverageTone("covered"), "positive");
  assert.equal(coverageTone("not-a-status"), "neutral");
});

test("segment data never invents a denominator", () => {
  const result = segmentData([
    { label: "Covered", value: 3, tone: "positive" },
    { label: "Unmapped", value: 1, tone: "negative" },
  ]);
  assert.equal(result.total, 4);
  assert.equal(result.segments[0].share, 0.75);
  const empty = segmentData([]);
  assert.equal(empty.total, 0);
  assert.deepEqual(empty.segments, []);
});

test("bar data sorts real values and keeps a zero-share safe", () => {
  const rows = barData([
    { label: "A", value: 2 },
    { label: "B", value: 6 },
    { label: "C", value: 0 },
  ]);
  assert.deepEqual(rows.map((row) => row.label), ["B", "A", "C"]);
  assert.equal(rows[0].share, 1);
  assert.equal(rows[2].share, 0);
});

test("bar series reports omitted categories instead of silently slicing them", () => {
  const items = [
    { label: "A", value: 3 },
    { label: "B", value: 2 },
    { label: "C", value: 1 },
  ];
  const limited = barSeries(items, { limit: 2 });
  assert.equal(limited.shown, 2);
  assert.equal(limited.total, 3);
  assert.equal(limited.truncated, true);
  assert.deepEqual(limited.rows.map((row) => row.label), ["A", "B"]);
  const complete = barSeries(items, { limit: 3 });
  assert.equal(complete.shown, 3);
  assert.equal(complete.truncated, false);
});

test("percent formatting distinguishes missing from zero", () => {
  assert.equal(formatPercent(null), "—");
  assert.equal(formatPercent(0, 1), "0.0%");
  assert.equal(formatPercent(0.456, 1), "45.6%");
});

test("truncation notes only appear when rows were actually dropped", () => {
  assert.deepEqual(truncationNotes(null), []);
  assert.deepEqual(truncationNotes({ concepts: { shown: 10, total: 10 } }), []);
  assert.deepEqual(truncationNotes({ concepts: { shown: 10, total: 20 } }), [
    "Concepts: showing 10 of 20",
  ]);
});
