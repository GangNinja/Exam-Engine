import assert from "node:assert/strict";
import test from "node:test";

import {
  extractImageReferences,
  hasMediaPlaceholder,
  isSafeImageSource,
  parseQuestionBlocks,
} from "../../src/exam_engine/dashboard/static/views/question-content.js";

test("fenced code is separated without changing its contents", () => {
  const source = "Read the program:\n```python\nfor i in range(3):\n    print(i)\n```\nWhat is printed?";
  const blocks = parseQuestionBlocks(source);
  assert.deepEqual(blocks.map((block) => block.type), ["text", "code", "text"]);
  assert.equal(blocks[1].language, "python");
  assert.equal(blocks[1].text, "for i in range(3):\n    print(i)");
  assert.equal(blocks[0].text, "Read the program:");
  assert.equal(blocks[2].text, "What is printed?");
});

test("a malformed fence stays readable as source text", () => {
  const source = "```python\nprint(1)\nstill open";
  const blocks = parseQuestionBlocks(source);
  assert.deepEqual(blocks.map((block) => block.type), ["text"]);
  assert.equal(blocks[0].text, source);
});

test("a valid Markdown table retains row and column order", () => {
  const source = "| Name | Value |\n| --- | ---: |\n| A | 1 |\n| B | 2 |";
  const blocks = parseQuestionBlocks(source);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "table");
  assert.deepEqual(blocks[0].header, ["Name", "Value"]);
  assert.deepEqual(blocks[0].rows, [["A", "1"], ["B", "2"]]);
});

test("an invalid table shape is not silently forced into a table", () => {
  const source = "| A | B |\n| --- | --- |\n| only-one-cell |";
  const blocks = parseQuestionBlocks(source);
  assert.equal(blocks[0].type, "text");
  assert.equal(blocks[0].text, source);
});

test("Markdown image references are extracted in order, but HTML tags are not assets", () => {
  const text = "See ![first](img/a.png) and ![second](img/b.png).";
  assert.deepEqual(extractImageReferences(text), [
    { alt: "first", src: "img/a.png" },
    { alt: "second", src: "img/b.png" },
  ]);
  assert.deepEqual(extractImageReferences("<img src='code-example.png'>"), []);
  assert.deepEqual(extractImageReferences("<img> is the HTML tag being taught."), []);
});

test("figure placeholders are marked for a clear fallback", () => {
  assert.equal(hasMediaPlaceholder("Which option? A) [figure/image option in source PDF]"), true);
  assert.equal(hasMediaPlaceholder("A) [figure-based option]"), true);
  assert.equal(hasMediaPlaceholder("No visual reference here."), false);
});

test("only local relative image sources are accepted", () => {
  assert.equal(isSafeImageSource("img/diagram.png"), true);
  assert.equal(isSafeImageSource("/assets/diagram.png"), false);
  assert.equal(isSafeImageSource("https://example.test/diagram.png"), false);
  assert.equal(isSafeImageSource("javascript:alert(1)"), false);
  assert.equal(isSafeImageSource(""), false);
});
