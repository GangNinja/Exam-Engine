import assert from "node:assert/strict";
import test from "node:test";

import {
  groupQuestionsByPassage,
  passageLabel,
} from "../../src/exam_engine/dashboard/static/views/passage-groups.js";

const passage = {
  passage_id: "psg_p1",
  text: "Complete passage text.\n\nSecond paragraph.",
  source_reference: "GATE 2025 Physics",
  question_count: 2,
  child_question_ids: ["Q1", "Q2"],
};

test("passage questions are grouped, ordered and not repeated as independent cards", () => {
  const questions = [
    { question_id: "q2", passage: passage, passage_id: "psg_p1", passage_order: 2 },
    { question_id: "q1", passage: passage, passage_id: "psg_p1", passage_order: 1 },
    { question_id: "q3", passage: null, passage_id: null },
  ];
  const groups = groupQuestionsByPassage(questions);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].passage.passage_id, "psg_p1");
  assert.deepEqual(groups[0].questions.map((item) => item.question_id), ["q1", "q2"]);
  assert.equal(groups[1].passage, null);
  assert.deepEqual(groups[1].questions.map((item) => item.question_id), ["q3"]);
  assert.equal(passageLabel(groups[0].passage), "GATE 2025 Physics");
  assert.deepEqual(questions.map((item) => item.question_id), ["q2", "q1", "q3"]);
});

test("an independent question remains independent when no passage is linked", () => {
  const groups = groupQuestionsByPassage([
    { question_id: "independent", text: "Standalone item" },
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].passage, null);
  assert.equal(groups[0].questions[0].question_id, "independent");
});
