import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, createApi, withQuery } from "../../src/exam_engine/dashboard/static/api.js";

test("query building omits empty values and preserves lists", () => {
  assert.equal(withQuery("/api/x"), "/api/x");
  assert.equal(
    withQuery("/api/x", { a: 1, b: "", c: null, d: ["2", "", "3"] }),
    "/api/x?a=1&d=2&d=3",
  );
});

test("API errors preserve the backend code, status and detail", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        error: {
          code: "unknown_course",
          message: "no such course",
          detail: { course_id: "missing" },
        },
      }),
      { status: 404, headers: { "Content-Type": "application/json" } },
    );
  try {
    const api = createApi();
    await assert.rejects(api.course("missing"), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 404);
      assert.equal(error.code, "unknown_course");
      assert.equal(error.detail.course_id, "missing");
      return true;
    });
  } finally {
    globalThis.fetch = original;
  }
});

test("GET retries one network failure, mutations never retry", async () => {
  const original = globalThis.fetch;
  let attempts = 0;
  globalThis.fetch = async () => {
    attempts += 1;
    if (attempts === 1) throw new TypeError("connection reset");
    return new Response(JSON.stringify({ status: "ok" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const api = createApi();
    assert.deepEqual(await api.health(), { status: "ok" });
    assert.equal(attempts, 2);
  } finally {
    globalThis.fetch = original;
  }

  attempts = 0;
  globalThis.fetch = async () => {
    attempts += 1;
    throw new TypeError("connection reset");
  };
  try {
    const api = createApi();
    await assert.rejects(api.startAnalysis("course", "nlp"), (error) => {
      assert.equal(error.code, "network_error");
      return true;
    });
    assert.equal(attempts, 1);
  } finally {
    globalThis.fetch = original;
  }
});

test("tutor streaming parses split SSE frames and resolves the final payload", async () => {
  const original = globalThis.fetch;
  const encoder = new TextEncoder();
  const frames = [
    'event: status\ndata: {"stage":"model","message":"Asking the model"}\n\n',
    'event: answer\ndata: {"text":"Hello "}\n\nevent: answer\ndata: {"text":"there"}\n\n',
    'event: result\ndata: {"status":"answered","answer":"Hello there"}\n\n',
  ];
  let requestedUrl = "";
  globalThis.fetch = async (url, options) => {
    requestedUrl = url;
    assert.equal(options.headers.Accept, "text/event-stream");
    const stream = new ReadableStream({
      start(controller) {
        for (const frame of frames) {
          const middle = Math.floor(frame.length / 2);
          controller.enqueue(encoder.encode(frame.slice(0, middle)));
          controller.enqueue(encoder.encode(frame.slice(middle)));
        }
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  };
  try {
    const api = createApi();
    const events = [];
    const result = await api.tutorAskStream(
      "course/one",
      { question: "Explain stacks" },
      { onEvent: (event) => events.push(event) },
    );
    assert.equal(requestedUrl, "/api/courses/course%2Fone/tutor/ask/stream");
    assert.deepEqual(
      events.map((event) => event.event),
      ["status", "answer", "answer"],
    );
    assert.equal(
      events
        .filter((event) => event.event === "answer")
        .map((event) => event.data.text)
        .join(""),
      "Hello there",
    );
    assert.deepEqual(result, { status: "answered", answer: "Hello there" });
  } finally {
    globalThis.fetch = original;
  }
});

test("a non-JSON success response is rejected rather than parsed loosely", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("<html>not json</html>", {
      status: 200,
      headers: { "Content-Type": "text/html" },
    });
  try {
    const api = createApi();
    await assert.rejects(api.health(), (error) => {
      assert.equal(error.code, "invalid_response");
      return true;
    });
  } finally {
    globalThis.fetch = original;
  }
});
