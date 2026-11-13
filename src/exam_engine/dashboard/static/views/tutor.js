import { h } from "../dom.js";
import { humaniseCode, safeArray, tutorStateFor } from "../ui-state.js";
import {
  emptyState,
  errorState,
  field,
  inlineLoading,
  pageHeader,
  panel,
  selectControl,
  selectedSyllabus,
  statusBadge,
  syllabusSelect,
  textareaControl,
} from "./shared.js";

export function renderTutor(ctx) {
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    pageHeader({
      eyebrow: `${ctx.course.course_code} · tutor workspace`,
      title: "AI tutor",
      description:
        "Ask in your own words. The tutor teaches from this course's stored syllabus and past papers, adds cited web sources, and uses the local AI model when it is available. It labels uncertainty instead of guessing.",
    }),
  );

  const avatarHost = h(
    "div",
    { class: "avatar-host" },
    h(
      "div",
      { class: "avatar-static-fallback", role: "img", "aria-label": "Tutor illustration is loading" },
      h("span", { class: "fallback-orbit", "aria-hidden": "true" }),
      h("span", { class: "fallback-face", "aria-hidden": "true" }),
      h("p", { text: "Avatar loading. The tutor text works independently." }),
    ),
  );
  const capabilityHost = h("div", {}, inlineLoading("Reading tutor capabilities…"));
  const modelNote = h("p", { class: "tutor-model", role: "status", "aria-live": "polite" });
  const contextStatus = h("p", { class: "form-status", role: "status", "aria-live": "polite" });
  const conversation = h("ol", {
    class: "conversation",
    role: "log",
    "aria-live": "polite",
    "aria-relevant": "additions",
  });
  const promptHost = h("div", { class: "prompt-list", "aria-label": "Suggested questions" });
  const conceptSelect = selectControl(
    [{ value: "", label: "Let the tutor match my wording" }],
    { id: "tutor-concept" },
  );
  const syllabus = syllabusSelect(ctx);
  const input = textareaControl({
    name: "question",
    rows: 3,
    id: "tutor-question",
    placeholder: "Ask about a topic, formula, derivation, exam focus or your own answer…",
  });
  const submit = h(
    "button",
    { class: "button button-primary", type: "submit" },
    h("span", { text: "Ask tutor" }),
  );
  const composerStatus = h("p", { class: "form-status", role: "status", "aria-live": "polite" });
  const form = h(
    "form",
    { class: "tutor-composer" },
    h("label", { class: "field" }, h("span", { text: "Your question" }), input),
    h(
      "div",
      { class: "tutor-composer-row" },
      h("label", { class: "field field-inline" }, h("span", { text: "Concept (optional)" }), conceptSelect),
      submit,
    ),
    composerStatus,
  );

  let avatar = null;
  let avatarFailed = false;
  function setAvatar(state, message) {
    if (avatar) {
      avatar.setState(state, message);
      return;
    }
    if (!avatarFailed) {
      const fallback = avatarHost.querySelector(".avatar-static-fallback");
      fallback?.setAttribute("aria-label", `Tutor state: ${state}. ${message || ""}`);
    }
  }

  dynamicAvatar(ctx, avatarHost).then((api) => {
    if (api) {
      avatar = api;
      ctx.addCleanup(() => api.destroy());
    } else {
      avatarFailed = true;
      avatarHost.replaceChildren(
        h(
          "div",
          { class: "avatar-text-fallback" },
          h("p", { class: "eyebrow", text: "Text mode" }),
          h("p", { text: "The animated avatar could not load. All tutor answers remain available as text." }),
        ),
      );
    }
  });

  addTutorMessage(
    conversation,
    "AI tutor",
    [
      {
        heading: "Ask the tutor",
        text:
          "Ask about any topic in this course. I teach from the stored syllabus and past-paper evidence, add cited web context when it helps, and use the local AI model for fuller conversation when it is available. I will say when an answer relies on general subject knowledge, and I never invent a source or a future exam question.",
      },
    ],
    { status: "speaking" },
  );

  ctx.api
    .tutorContext(ctx.courseId, { syllabus_id: selectedSyllabus(ctx) }, { signal: ctx.signal })
    .then((context) => {
      capabilityHost.replaceChildren(renderCapabilities(context.capabilities));
      const ai = context.ai;
      const web = context.web_search;
      if (ai?.available) {
        modelNote.dataset.tone = "positive";
        modelNote.textContent = `Local AI ready${ai.model ? ` · ${ai.model}` : ""}${web?.available ? ` · ${web.provider} web search` : ""}`;
      } else {
        modelNote.dataset.tone = "neutral";
        modelNote.textContent =
          "Offline teacher mode · stored syllabus and past-paper evidence. Start Ollama for conversational answers.";
      }
      contextStatus.textContent = context.recovery?.detail || "";
      const suggestions = safeArray(context.suggestions);
      conceptSelect.append(
        ...suggestions.map((item) =>
          h("option", {
            value: item.concept_id,
            text: `${item.label}${item.unit_labels?.length ? ` — ${item.unit_labels.join(", ")}` : ""}`,
          }),
        ),
      );
      const examples = safeArray(context.examples);
      promptHost.replaceChildren(
        ...(examples.length
          ? examples.map((example) =>
              h(
                "button",
                {
                  type: "button",
                  class: "prompt-chip",
                  onClick: () => {
                    input.value = example;
                    input.focus();
                  },
                },
                example,
              ),
            )
          : [h("p", { class: "muted", text: "No concept suggestions are available yet." })]),
      );
    })
    .catch((error) => {
      if (isAbort(error)) return;
      capabilityHost.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "You can still ask a free-text question; the tutor will refuse to guess if it cannot resolve a stored concept.",
        }),
      );
    });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await ask(input.value, conceptSelect.value || null);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  async function ask(question, conceptId) {
    const text = String(question || "").trim();
    if (!text) {
      composerStatus.dataset.tone = "negative";
      composerStatus.textContent = "Enter a question first.";
      input.focus();
      return;
    }
    addUserMessage(conversation, text);
    input.value = "";
    submit.disabled = true;
    composerStatus.dataset.tone = "neutral";
    composerStatus.textContent = "Searching the course, the web and the local tutor…";
    setAvatar("thinking", "Searching stored course evidence and cited web sources.");
    const pending = addPendingMessage(conversation);
    let hasAnswer = false;
    try {
      const response = await ctx.api.tutorAskStream(
        ctx.courseId,
        {
          question: text,
          concept_id: conceptId || undefined,
          syllabus_id: selectedSyllabus(ctx),
        },
        {
          signal: ctx.signal,
          onEvent: ({ event, data }) => {
            if (event === "status") {
              const message = String(data?.message || "").trim();
              if (message) {
                pending.setStatus(message);
                composerStatus.textContent = message;
              }
              if (data?.stage === "model") {
                setAvatar("thinking", message || "Waiting for the local model.");
              }
              return;
            }
            if (event === "answer" && typeof data?.text === "string" && data.text) {
              if (!hasAnswer) {
                hasAnswer = true;
                pending.setStatus("Streaming the grounded answer…");
              }
              pending.appendAnswer(data.text);
              setAvatar("speaking", "Streaming the grounded answer from the local model.");
              composerStatus.textContent = "Streaming the answer…";
            }
          },
        },
      );
      pending.dispose();
      pending.remove();
      const state = tutorStateFor(response);
      setAvatar(
        state,
        state === "speaking"
          ? "Explaining with course evidence and cited sources."
          : "Returning an evidence-limited answer.",
      );
      addTutorMessage(conversation, "Tutor", safeArray(response.sections), {
        status: response.status,
        response,
        onFollowUp: (followUp, nextConceptId) => ask(followUp, nextConceptId || conceptId),
        onGenerate: response.concept
          ? async () => {
              await generateFollowUp(response.concept, syllabus?.querySelector("select")?.value || selectedSyllabus(ctx));
            }
          : null,
      });
      composerStatus.dataset.tone = "positive";
      composerStatus.textContent =
        response.status === "answered"
          ? response.ai
            ? response.ai.web_used
              ? "Answer generated by the local model with cited web sources."
              : "Answer generated by the local model from stored course evidence."
            : "Offline teacher answer from stored syllabus and past-paper evidence."
          : response.status === "insufficient_evidence"
            ? "No grounded answer; choose a known concept or add course material."
            : "This capability is not implemented.";
    } catch (error) {
      pending.dispose();
      pending.remove();
      if (isAbort(error)) return;
      setAvatar("warning", "The request failed.");
      addTutorMessage(
        conversation,
        "Tutor unavailable",
        [
          {
            heading: "Request failed",
            text: error?.message || "The tutor request failed. The rest of the workspace is still available.",
          },
        ],
        { status: "unavailable" },
      );
      composerStatus.dataset.tone = "negative";
      composerStatus.textContent = error?.message || "The tutor request failed.";
    } finally {
      submit.disabled = false;
      input.focus();
    }
  }

  async function generateFollowUp(concept, syllabusId) {
    setAvatar("thinking", `Resolving ${concept.label} in the generation scope.`);
    try {
      // Tutor concepts and generation concepts are resolved by different
      // services. Ask Phase 10 for its own identities instead of assuming the
      // tutor's coverage ID is accepted by the generator.
      const options = await ctx.api.generationOptions(
        ctx.courseId,
        { syllabus_id: syllabusId },
        { signal: ctx.signal },
      );
      const target = resolveGenerationConcept(options.concepts, concept);
      if (!target) {
        setAvatar("corrective", "This concept is not generation-ready in the selected syllabus.");
        addTutorMessage(
          conversation,
          "Practice generator",
          [
            {
              heading: "Concept not resolvable",
              text: `The generation scope did not resolve “${concept.label}” to exactly one concept. Open Practice to choose from ${safeArray(options.concepts).length} generation-ready concept(s).`,
            },
          ],
          { status: "insufficient_evidence" },
        );
        return;
      }
      setAvatar("thinking", `Generating a grounded practice question about ${target.label}.`);
      const batch = await ctx.api.generate(
        ctx.courseId,
        {
          syllabus_id: options.syllabus_id || syllabusId,
          concept_ids: [target.concept_id],
          count: 1,
          format: "short_answer",
          provider: "template",
          seed: Math.floor(Math.random() * 1_000_000_000) + 1,
          include_answer_key: true,
        },
        { signal: ctx.signal, timeoutMs: 60000 },
      );
      const question = safeArray(batch.questions)[0];
      if (!question) {
        setAvatar("corrective", "No practice question passed validation.");
        addTutorMessage(
          conversation,
          "Practice generator",
          [
            {
              heading: "No question produced",
              text:
                safeArray(batch.rejections)[0]?.message ||
                "The generator returned no validated question. Open Practice to inspect the batch findings.",
            },
          ],
          { status: "insufficient_evidence" },
        );
        return;
      }
      setAvatar("positive", "Practice question generated and stored for review.");
      addTutorMessage(
        conversation,
        "Practice generator",
        [
          {
            heading: `Practice question · ${question.unit_label || "unit unknown"}`,
            text: question.text,
          },
          question.answer_key
            ? { heading: "Answer key", text: question.answer_key }
            : {
                heading: "Answer key",
                text: "No answer key is available; review the item before use.",
              },
          {
            heading: "Review state",
            text: `${question.review_status === "pending_review" ? "Draft" : humaniseCode(question.review_status)} · validation ${humaniseCode(question.validation?.status)}. Open Practice to approve or reject it.`,
          },
        ],
        { status: "answered" },
      );
    } catch (error) {
      if (isAbort(error)) return;
      setAvatar("warning", "Practice generation failed.");
      addTutorMessage(
        conversation,
        "Practice generator",
        [{ heading: "Generation failed", text: error?.message || "The practice batch could not be generated." }],
        { status: "unavailable" },
      );
    }
  }

  wrap.append(
    h(
      "div",
      { class: "tutor-layout" },
      h(
        "section",
        { class: "tutor-main", "aria-label": "Tutor conversation" },
        panel(
          "Ask the tutor",
          {
            eyebrow: "Course-grounded AI",
            description: "Questions stay scoped to this course. Answers cite stored evidence and live web sources; uncertainty is explicit.",
          },
          syllabus,
          promptHost,
          h("div", { class: "conversation-wrap" }, conversation),
          form,
        ),
      ),
      h(
        "aside",
        { class: "tutor-aside", "aria-label": "Tutor status and capabilities" },
        avatarHost,
        h("p", { class: "tutor-disclaimer", text: "The avatar is a presentation layer. Every result is also available as text; the avatar never determines correctness." }),
        panel("Capabilities", { eyebrow: "What is available" }, modelNote, capabilityHost, contextStatus),
      ),
    ),
  );
  return wrap;
}

function renderCapabilities(capabilities) {
  return h(
    "ul",
    { class: "capability-list" },
    ...Object.entries(capabilities || {}).map(([key, value]) =>
      h(
        "li",
        { class: value.available ? "is-available" : "is-unavailable" },
        h(
          "div",
          {},
          h("strong", { text: value.label || humaniseCode(key) }),
          h("p", { text: value.detail }),
        ),
        statusBadge(value.available ? "Available" : "Unavailable", value.available ? "positive" : "neutral"),
      ),
    ),
  );
}

function addUserMessage(list, text) {
  list.append(
    h(
      "li",
      { class: "message message-user" },
      h("p", { class: "message-author", text: "You" }),
      h("p", { class: "message-text", text }),
    ),
  );
}

function addPendingMessage(list) {
  const startedAt = Date.now();
  const statusText = h("span", {
    class: "message-progress-text",
    text: "Preparing the tutor stream…",
  });
  const elapsed = h("span", { class: "message-elapsed", text: "0s elapsed" });
  const preview = h("p", { class: "message-stream-preview" });
  const item = h(
    "li",
    { class: "message message-tutor message-pending", "aria-busy": "true" },
    h("p", { class: "message-author", text: "Tutor" }),
    h(
      "p",
      { class: "message-progress", role: "status", "aria-live": "polite" },
      statusText,
      elapsed,
    ),
    preview,
  );
  const updateElapsed = () => {
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    elapsed.textContent = `${seconds}s elapsed`;
  };
  updateElapsed();
  const timer = setInterval(updateElapsed, 1000);
  list.append(item);
  list.scrollTop = list.scrollHeight;
  return {
    setStatus(text) {
      statusText.textContent = text;
    },
    appendAnswer(text) {
      preview.textContent += text;
      list.scrollTop = list.scrollHeight;
    },
    dispose() {
      clearInterval(timer);
      item.removeAttribute("aria-busy");
    },
    remove() {
      item.remove();
    },
  };
}

function addTutorMessage(list, author, sections, { status = "answered", response = null, onFollowUp = null, onGenerate = null } = {}) {
  const body = h("div", { class: "message-sections" });
  if (response?.ai) {
    const sourceLabel = response.ai.web_used
      ? ` · ${response.ai.web_sources || 0} web source(s)`
      : " · stored evidence";
    body.append(
      h("p", {
        class: "message-meta",
        text: `${humaniseCode(response.ai.provider)} ${response.ai.model || ""}${sourceLabel}${response.confidence ? ` · ${response.confidence} confidence` : ""}`.trim(),
      }),
    );
  }
  for (const section of sections) {
    body.append(
      h(
        "section",
        { class: "message-section" },
        h("h3", { text: section.heading || "Answer" }),
        h("p", { text: section.text || "" }),
        safeArray(section.citations).length
          ? h(
              "details",
              { class: "inline-details" },
              h("summary", { text: `${section.citations.length} citation(s)` }),
              h(
                "ul",
                {},
                ...section.citations.map((citation) =>
                  citationItem(citation),
                ),
              ),
            )
          : null,
      ),
    );
  }
  if (safeArray(response?.warnings).length) {
    body.append(
      h(
        "div",
        { class: "message-warning" },
        ...response.warnings.map((warning) => h("p", { text: `${humaniseCode(warning.code)}: ${warning.message}` })),
      ),
    );
  }
  if (response?.recovery) {
    body.append(
      h(
        "p",
        { class: "message-recovery" },
        h("strong", { text: `${response.recovery.label}. ` }),
        response.recovery.detail,
      ),
    );
  }
  if (safeArray(response?.limitations).length) {
    body.append(
      h(
        "details",
        { class: "inline-details" },
        h("summary", { text: "Limitations" }),
        h("ul", {}, ...response.limitations.map((item) => h("li", { text: item }))),
      ),
    );
  }
  if (safeArray(response?.suggestions).length) {
    body.append(
      h(
        "div",
        { class: "suggestion-list" },
        h("p", { class: "muted", text: "Choose a stored concept:" }),
        ...response.suggestions.map((suggestion) =>
          h(
            "button",
            {
              type: "button",
              class: "prompt-chip",
              onClick: () => onFollowUp?.(`Explain ${suggestion.label}`, suggestion.concept_id),
            },
            suggestion.label,
          ),
        ),
      ),
    );
  }
  if (safeArray(response?.follow_ups).length) {
    body.append(
      h(
        "div",
        { class: "suggestion-list" },
        h("p", { class: "muted", text: "Keep going:" }),
        ...response.follow_ups.map((followUp) =>
          h(
            "button",
            {
              type: "button",
              class: "prompt-chip",
              onClick: () => onFollowUp?.(followUp),
            },
            followUp,
          ),
        ),
      ),
    );
  }
  if (onGenerate) {
    body.append(
      h(
        "button",
        {
          type: "button",
          class: "button button-ghost",
          onClick: () => onGenerate(),
        },
        h("span", { text: "Generate a practice question" }),
      ),
    );
  }
  list.append(
    h(
      "li",
      { class: `message message-tutor message-${status}` },
      h("p", { class: "message-author", text: author }),
      body,
    ),
  );
  list.scrollTop = list.scrollHeight;
}

function citationItem(citation) {
  const label = citation.document_title || citation.title || citation.kind || "record";
  const meta = `${citation.page_number ? `, page ${citation.page_number}` : ""}${citation.excerpt ? ` — “${citation.excerpt}”` : ""}${citation.method ? ` [${humaniseCode(citation.method)}]` : ""}`;
  const item = h("li", { class: "message-citation" });
  item.append(h("span", { text: `${label}${meta}` }));
  if (citation.url) {
    item.append(
      h("a", {
        href: citation.url,
        target: "_blank",
        rel: "noreferrer noopener",
        text: " Open source ↗",
      }),
    );
  }
  return item;
}

async function dynamicAvatar(ctx, host) {
  try {
    const module = await import("../tutor-avatar.js");
    return module.createTutorAvatar(host);
  } catch (error) {
    if (typeof console !== "undefined" && console.debug) {
      console.debug("Tutor avatar unavailable:", error);
    }
    return null;
  }
}

function resolveGenerationConcept(concepts, concept) {
  const rows = safeArray(concepts);
  const byId = rows.find((item) => item.concept_id === concept.concept_id);
  if (byId) return byId;
  const label = String(concept.label || "").trim().toLocaleLowerCase();
  const matches = rows.filter((item) => String(item.label || "").trim().toLocaleLowerCase() === label);
  if (matches.length === 1) return matches[0];
  const units = safeArray(concept.unit_labels);
  const scoped = matches.filter((item) => units.includes(item.unit_label));
  return scoped.length === 1 ? scoped[0] : null;
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
