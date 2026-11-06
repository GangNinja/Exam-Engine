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

  return {
    request,
    get,
    post,
    health: (options) => get("/api/health", options),
    capabilities: (options) => get("/api/capabilities", options),
    courses: (options) => get("/api/courses", options),
    examsOverview: (params = {}, options) => get(withQuery("/api/exams", params), options),
    examQuestions: (params = {}, options) =>
      get(withQuery("/api/exams/questions", params), options),
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
