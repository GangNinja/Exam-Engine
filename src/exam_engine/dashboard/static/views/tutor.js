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
      title: "Grounded tutor",
      description:
        "Answers are extractive summaries of this course's stored coverage, mapped questions, graph relationships and prerequisite paths.",
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
    placeholder: "e.g. Explain binary search using the stored course material",
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
    "Stored-evidence tutor",
    [
      {
        heading: "What this tutor does",
        text:
          "Ask about a concept in this course. I resolve it against the stored syllabus coverage and knowledge graph, then quote the mapped questions, source pages and prerequisite relationships I find. I do not use a language model and I never invent a definition, a score or a forecast.",
      },
    ],
    { status: "speaking" },
  );

  ctx.api
    .tutorContext(ctx.courseId, { syllabus_id: selectedSyllabus(ctx) }, { signal: ctx.signal })
    .then((context) => {
      capabilityHost.replaceChildren(renderCapabilities(context.capabilities));
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
    composerStatus.textContent = "Searching stored evidence…";
    setAvatar("thinking", "Searching stored syllabus, questions and graph evidence.");
    const pending = addPendingMessage(conversation);
    try {
      const response = await ctx.api.tutorAsk(
        ctx.courseId,
        {
          question: text,
          concept_id: conceptId || undefined,
          syllabus_id: selectedSyllabus(ctx),
        },
        { signal: ctx.signal },
      );
      pending.remove();
      const state = tutorStateFor(response);
      setAvatar(state, state === "speaking" ? "Explaining from stored records." : "Returning an evidence-limited answer.");
      addTutorMessage(conversation, "Tutor", safeArray(response.sections), {
        status: response.status,
        response,
        onFollowUp: (followUp) => ask(followUp, conceptId),
        onGenerate: response.concept
          ? async () => {
              await generateFollowUp(response.concept, syllabus?.querySelector("select")?.value || selectedSyllabus(ctx));
            }
          : null,
      });
      composerStatus.dataset.tone = "positive";
      composerStatus.textContent =
        response.status === "answered"
          ? "Answer assembled from stored records."
          : response.status === "insufficient_evidence"
            ? "No grounded answer; choose a known concept or add course material."
            : "This capability is not implemented.";
    } catch (error) {
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
          include_answer_key: true,
        },
        { signal: ctx.signal },
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
        "aside",
        { class: "tutor-aside" },
        avatarHost,
        h("p", { class: "tutor-disclaimer", text: "The avatar is a presentation layer. Every result is also available as text; the avatar never determines correctness." }),
        panel("Capabilities", { eyebrow: "Honest boundaries" }, capabilityHost, contextStatus),
      ),
      h(
        "section",
        { class: "tutor-main", "aria-label": "Tutor conversation" },
        panel(
          "Ask the tutor",
          {
            eyebrow: "Stored evidence only",
            description: "Questions are scoped to this course. Unsupported capabilities are named, not simulated.",
          },
          syllabus,
          promptHost,
          h("div", { class: "conversation-wrap" }, conversation),
          form,
        ),
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
  const item = h(
    "li",
    { class: "message message-tutor message-pending" },
    h("p", { class: "message-author", text: "Tutor" }),
    h("p", { class: "message-text", text: "Searching stored evidence…" }),
  );
  list.append(item);
  return item;
}

function addTutorMessage(list, author, sections, { status = "answered", response = null, onFollowUp = null, onGenerate = null } = {}) {
  const body = h("div", { class: "message-sections" });
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
                  h("li", {
                    text: `${citation.document_title || citation.title || citation.kind || "record"}${citation.page_number ? `, page ${citation.page_number}` : ""}${citation.excerpt ? ` — “${citation.excerpt}”` : ""}${citation.method ? ` [${humaniseCode(citation.method)}]` : ""}`,
                  }),
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
              onClick: () => onFollowUp?.(`Explain ${suggestion.label}`),
            },
            suggestion.label,
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
