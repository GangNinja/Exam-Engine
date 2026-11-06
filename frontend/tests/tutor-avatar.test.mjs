import assert from "node:assert/strict";
import test from "node:test";

/**
 * A deliberately tiny DOM shim: the avatar component only uses createElementNS,
 * append, replaceChildren, setAttribute, dataset, textContent and hidden. Testing
 * those transitions here keeps the browser check focused on integration.
 */
class FakeNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.textContent = "";
    this.hidden = false;
  }

  get firstChild() {
    return this.children[0] ?? null;
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  removeChild(child) {
    this.children = this.children.filter((item) => item !== child);
    return child;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
}

globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_namespace, tag) => new FakeNode(tag),
  createTextNode: (text) => {
    const node = new FakeNode("#text");
    node.textContent = String(text);
    return node;
  },
};

const { createTutorAvatar } = await import(
  "../../src/exam_engine/dashboard/static/tutor-avatar.js"
);

function childWithClass(root, className) {
  return root.children.find((child) => child.attributes.class === className);
}

test("avatar exposes every state as text and never as the sole signal", () => {
  const container = new FakeNode("div");
  const avatar = createTutorAvatar(container);
  const root = container.children[0];
  const caption = childWithClass(root, "avatar-caption");
  const badge = childWithClass(root, "avatar-badge");

  assert.equal(root.dataset.state, "idle");
  assert.match(root.attributes["aria-label"], /idle/i);
  assert.match(caption.textContent, /ready/i);
  assert.equal(badge.hidden, false);

  const states = {
    thinking: /thinking|searching/i,
    speaking: /explaining|reading/i,
    positive: /completed|result/i,
    corrective: /incomplete|attention/i,
    warning: /not implemented|unavailable/i,
  };
  for (const [state, pattern] of Object.entries(states)) {
    avatar.setState(state);
    assert.equal(root.dataset.state, state);
    assert.match(root.attributes["aria-label"], new RegExp(state, "i"));
    assert.match(caption.textContent, pattern);
    assert.equal(badge.hidden, state !== "warning");
  }

  avatar.setState("warning", "Practice generation failed.");
  assert.equal(caption.textContent, "Practice generation failed.");
  assert.match(root.attributes["aria-label"], /Practice generation failed/);

  avatar.setState("not-a-state");
  assert.equal(root.dataset.state, "idle");

  avatar.destroy();
  assert.equal(container.children.length, 0);
  avatar.setState("speaking");
  assert.equal(container.children.length, 0);
});
