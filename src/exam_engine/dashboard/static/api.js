/**
 * Same-origin JSON client with cancellation, timeouts and stable errors.
 * GET requests retry once on a network failure; mutations never retry.
 */

export class ApiError extends Error {
  constructor(message, { status = 0, code = "request_failed", detail = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export function createApi() {
  async function request(path, options = {}) {
    const {
      method = "GET",
      body,
      signal,
      timeoutMs = 60000,
      retry = method === "GET",
      headers = {},
    } = options;
    try {
      return await once(path, { method, body, signal, timeoutMs, headers });
    } catch (error) {
      if (retry && isNetworkError(error) && !signal?.aborted) {
        return once(path, { method, body, signal, timeoutMs, headers });
      }
      throw error;
    }
  }

  async function once(path, { method, body, signal, timeoutMs, headers }) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const forwardAbort = () => controller.abort(signal?.reason);
    if (signal) {
      if (signal.aborted) {
        clearTimeout(timer);
        throw signal.reason ?? new DOMException("aborted", "AbortError");
      }
      signal.addEventListener("abort", forwardAbort, { once: true });
    }
    try {
      const response = await fetch(path, {
        method,
        body,
        headers,
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
      let payload = null;
      const text = await response.text();
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          throw new ApiError("The server returned a non-JSON response.", {
            status: response.status,
            code: "invalid_response",
          });
        }
      }
      if (!response.ok) {
        const error = payload?.error ?? {};
        throw new ApiError(error.message || `Request failed (${response.status}).`, {
          status: response.status,
          code: error.code || "request_failed",
          detail: error.detail ?? null,
        });
      }
      return payload;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (timedOut) {
        throw new ApiError("The request took too long. The backend may still be working.", {
          code: "request_timeout",
        });
      }
      if (signal?.aborted) {
        throw signal.reason ?? new DOMException("aborted", "AbortError");
      }
      if (error?.name === "AbortError") {
        throw new ApiError("The request was cancelled.", { code: "request_aborted" });
      }
      throw new ApiError(
        "Could not reach the local dashboard server. Check that it is still running.",
        { code: "network_error" },
      );
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", forwardAbort);
    }
  }

  const get = (path, options) => request(path, { ...options, method: "GET" });
  const post = (path, payload, options) =>
    request(path, {
      ...options,
      method: "POST",
      body: JSON.stringify(payload ?? {}),
      headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
      retry: false,
    });

  /**
   * Consume the tutor's server-sent event stream. Progress events are forwarded
   * to the caller; only the final validated tutor payload is returned.
   */
  async function streamTutor(courseId, payload, { signal, onEvent } = {}) {
    const controller = new AbortController();
    let timedOut = false;
    let timer = null;
    const armIdleTimer = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, 30000);
    };
    const forwardAbort = () => controller.abort(signal?.reason);
    if (signal) {
      if (signal.aborted) throw signal.reason ?? new DOMException("aborted", "AbortError");
      signal.addEventListener("abort", forwardAbort, { once: true });
    }
    let reader = null;
    try {
      armIdleTimer();
      const response = await fetch(
        `/api/courses/${encodeURIComponent(courseId)}/tutor/ask/stream`,
        {
          method: "POST",
          body: JSON.stringify(payload ?? {}),
          headers: {
            "Content-Type": "application/json",
            Accept: "text/event-stream",
          },
          credentials: "same-origin",
          cache: "no-store",
          signal: controller.signal,
        },
      );
      if (!response.ok) {
        const text = await response.text();
        let errorPayload = null;
        try {
          errorPayload = text ? JSON.parse(text) : null;
        } catch {
          errorPayload = null;
        }
        const error = errorPayload?.error ?? {};
        throw new ApiError(error.message || `Request failed (${response.status}).`, {
          status: response.status,
          code: error.code || "request_failed",
          detail: error.detail ?? null,
        });
      }
      if (!response.body) {
        throw new ApiError("This browser cannot read the tutor stream.", {
          status: response.status,
          code: "stream_unsupported",
        });
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let final = null;
      let streamError = null;
      const parser = createSseParser((event, data) => {
        armIdleTimer();
        if (event === "result") {
          final = data;
          return;
        }
        if (event === "error") {
          const error = data?.error ?? {};
          streamError = new ApiError(error.message || "The tutor stream failed.", {
            status: response.status,
            code: error.code || "stream_error",
            detail: error.detail ?? null,
          });
          return;
        }
        onEvent?.({ event, data });
      });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        parser.push(decoder.decode(value, { stream: true }));
      }
      parser.push(decoder.decode());
      parser.end();
      if (streamError) throw streamError;
      if (!final) {
        throw new ApiError("The tutor stream ended before a final answer.", {
          status: response.status,
          code: "stream_incomplete",
        });
      }
      return final;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (timedOut) {
        throw new ApiError("The tutor stopped sending progress. The backend may still be working.", {
          code: "stream_timeout",
        });
      }
      if (signal?.aborted) {
        throw signal.reason ?? new DOMException("aborted", "AbortError");
      }
      if (error?.name === "AbortError") {
        throw new ApiError("The request was cancelled.", { code: "request_aborted" });
      }
      throw new ApiError(
        "The tutor stream was interrupted. Check that the local dashboard is still running.",
        { code: "network_error" },
      );
    } finally {
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", forwardAbort);
      if (reader) reader.releaseLock();
    }
  }

  return {
    request,
    get,
    post,
    health: (options) => get("/api/health", options),
    capabilities: (options) => get("/api/capabilities", options),
    courses: (options) => get("/api/courses", options),
    recovery: (params = {}, options) => get(withQuery("/api/recovery", params), options),
    recoveryCourses: (params = {}, options) =>
      get(withQuery("/api/recovery/courses", params), options),
    recoveryCourse: (courseId, params = {}, options) =>
      get(
        withQuery(`/api/recovery/courses/${encodeURIComponent(courseId)}`, params),
        options,
      ),
    recoveryRecord: (recordId, params = {}, options) =>
      get(
        withQuery(`/api/recovery/records/${encodeURIComponent(recordId)}`, params),
        options,
      ),
    recoveryIssues: (params = {}, options) =>
      get(withQuery("/api/recovery/issues", params), options),
    recoveryRun: (payload, options) => post("/api/recovery/run", payload, options),
    recoveryReview: (payload, options) => post("/api/recovery/repairs/review", payload, options),
    recoveryRollback: (payload, options) => post("/api/recovery/rollback", payload, options),
    examsOverview: (params = {}, options) => get(withQuery("/api/exams", params), options),
    examQuestions: (params = {}, options) =>
      get(withQuery("/api/exams/questions", params), options),
    examQuestion: (questionId, options) =>
      get(`/api/exams/questions/${encodeURIComponent(questionId)}`, options),
    examPassage: (passageId, options) =>
      get(`/api/exams/passages/${encodeURIComponent(passageId)}`, options),
    examPapers: (params = {}, options) => get(withQuery("/api/exams/papers", params), options),
    examSyllabus: (courseId, options) =>
      get(withQuery("/api/exams/syllabus", { course: courseId }), options),
    course: (courseId, options) => get(`/api/courses/${encodeURIComponent(courseId)}`, options),
    documents: (courseId, options) =>
      get(`/api/courses/${encodeURIComponent(courseId)}/documents`, options),
    questions: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/questions`, params), options),
    analytics: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/analytics`, params), options),
    coverage: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/coverage`, params), options),
    coverageConcept: (courseId, concept, params = {}, options) =>
      get(
        withQuery(
          `/api/courses/${encodeURIComponent(courseId)}/coverage/concepts/${encodeURIComponent(concept)}`,
          params,
        ),
        options,
      ),
    examDna: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/exam-dna`, params), options),
    evolution: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/evolution`, params), options),
    graph: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/graph`, params), options),
    neighborhood: (courseId, params, options) =>
      get(
        withQuery(`/api/courses/${encodeURIComponent(courseId)}/graph/neighborhood`, params),
        options,
      ),
    prerequisites: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/prerequisites`, params), options),
    learningPath: (courseId, payload, options) =>
      post(`/api/courses/${encodeURIComponent(courseId)}/learning-path`, payload, options),
    generated: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/generated`, params), options),
    generationOptions: (courseId, params = {}, options) =>
      get(
        withQuery(`/api/courses/${encodeURIComponent(courseId)}/generation/options`, params),
        options,
      ),
    generate: (courseId, payload, options) =>
      post(`/api/courses/${encodeURIComponent(courseId)}/generate`, payload, options),
    review: (courseId, questionId, payload, options) =>
      post(
        `/api/courses/${encodeURIComponent(courseId)}/generated/${encodeURIComponent(questionId)}/review`,
        payload,
        options,
      ),
    grade: (courseId, questionId, payload, options) =>
      post(
        `/api/courses/${encodeURIComponent(courseId)}/generated/${encodeURIComponent(questionId)}/grade`,
        payload,
        options,
      ),
    advanced: (courseId, options) =>
      get(`/api/courses/${encodeURIComponent(courseId)}/advanced`, options),
    mutate: (courseId, payload, options) =>
      post(`/api/courses/${encodeURIComponent(courseId)}/advanced/mutate`, payload, options),
    adversarial: (courseId, payload, options) =>
      post(
        `/api/courses/${encodeURIComponent(courseId)}/advanced/adversarial`,
        payload,
        options,
      ),
    misconceptions: (courseId, params = {}, options) =>
      get(
        withQuery(`/api/courses/${encodeURIComponent(courseId)}/misconceptions`, params),
        options,
      ),
    analyseMisconceptions: (courseId, payload = {}, options) =>
      post(`/api/courses/${encodeURIComponent(courseId)}/misconceptions/analyse`, payload, options),
    tutorContext: (courseId, params = {}, options) =>
      get(withQuery(`/api/courses/${encodeURIComponent(courseId)}/tutor/context`, params), options),
    tutorAsk: (courseId, payload, options) =>
      post(`/api/courses/${encodeURIComponent(courseId)}/tutor/ask`, payload, options),
    tutorAskStream: (courseId, payload, options) =>
      streamTutor(courseId, payload, options),
    startAnalysis: (courseId, kind, options) =>
      post(
        `/api/courses/${encodeURIComponent(courseId)}/analysis/${encodeURIComponent(kind)}`,
        {},
        options,
      ),
    jobs: (params = {}, options) => get(withQuery("/api/jobs", params), options),
    job: (jobId, options) => get(`/api/jobs/${encodeURIComponent(jobId)}`, options),
    createCourse: (payload, options) => post("/api/courses", payload, options),
    upload: async (courseId, formData, { signal } = {}) => {
      const response = await fetch(
        `/api/courses/${encodeURIComponent(courseId)}/documents`,
        { method: "POST", body: formData, credentials: "same-origin", signal },
      );
      const text = await response.text();
      let payload = null;
      try {
        payload = text ? JSON.parse(text) : null;
      } catch {
        throw new ApiError("The upload returned a non-JSON response.", {
          status: response.status,
          code: "invalid_response",
        });
      }
      if (!response.ok) {
        const error = payload?.error ?? {};
        throw new ApiError(error.message || "The upload failed.", {
          status: response.status,
          code: error.code || "upload_failed",
          detail: error.detail ?? payload,
        });
      }
      return payload;
    },
  };
}

export function createSseParser(onEvent) {
  let buffer = "";

  function dispatch(block) {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (!data.length) return;
    let payload;
    try {
      payload = JSON.parse(data.join("\n"));
    } catch {
      throw new ApiError("The tutor stream sent an invalid event.", {
        code: "invalid_stream_event",
      });
    }
    onEvent(event, payload);
  }

  return {
    push(chunk) {
      buffer = (buffer + chunk).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (block.trim()) dispatch(block);
        boundary = buffer.indexOf("\n\n");
      }
    },
    end() {
      if (buffer.trim()) dispatch(buffer);
      buffer = "";
    },
  };
}

export function withQuery(path, params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) if (item !== undefined && item !== null && item !== "") search.append(key, item);
    } else {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `${path}?${text}` : path;
}

function isNetworkError(error) {
  return error instanceof ApiError && error.code === "network_error";
}
