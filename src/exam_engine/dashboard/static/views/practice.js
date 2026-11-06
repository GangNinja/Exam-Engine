import { h } from "../dom.js";
import {
  formatInteger,
  formatNumber,
  formatPercent,
  humaniseCode,
  reviewTone,
  safeArray,
  validationTone,
} from "../ui-state.js";
import {
  dataTable,
  disclaimer,
  emptyState,
  errorState,
  field,
  illustration,
  inlineLoading,
  inputControl,
  linkButton,
  loadingPanel,
  notice,
  pageHeader,
  panel,
  selectControl,
  selectedSyllabus,
  statusBadge,
  syllabusSelect,
} from "./shared.js";

const FORMATS = [
  "multiple_choice",
  "short_answer",
  "long_answer",
  "explanation",
  "comparison",
  "application",
  "numerical",
  "case_based",
  "analysis",
  "design",
  "evaluation",
];

const BLOOMS = ["remember", "understand", "apply", "analyze", "evaluate", "create"];
const DIFFICULTIES = ["easy", "moderate", "hard"];
const REVIEW_STATUSES = [
  { value: "pending_review", label: "Draft" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
];

function reviewLabel(status) {
  return status === "pending_review" ? "Draft" : humaniseCode(status);
}

function validationLabel(status) {
  if (status === "validated") return "Checks passed";
  if (status === "needs_review") return "Check findings";
  if (status === "validation_incomplete") return "Checks incomplete";
  if (status === "rejected") return "Checks failed";
  return humaniseCode(status || "unknown");
}

export function renderPractice(ctx) {
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    pageHeader({
      eyebrow: `${ctx.course.course_code} · practice workspace`,
      title: "Generate, review and assess",
      description:
        "Questions are grounded in this course's own stored material, validated by named checks and never approved automatically.",
      actions: [
        linkButton("Tutor", `#/c/${encodeURIComponent(ctx.courseId)}/tutor`, {
          iconName: "tutor",
        }),
      ],
    }),
  );
  wrap.append(
    h(
      "div",
      { class: "practice-hero" },
      illustration(ctx, "riffle", "Card tray illustration. The questions below come from the backend."),
      h(
        "div",
        { class: "practice-hero-copy" },
        notice(
          "Open Check your answer on any stored question for local feedback against its reference and key ideas. It is a study aid, not an examination score.",
          "neutral",
          "Answer checks are available",
        ),
        notice(
          "Generation and answer checking run on this device. The built-in template provider needs no network or API key; numerical formats require a configured model provider.",
          "neutral",
          "Local and grounded",
        ),
      ),
    ),
  );

  const conceptsHost = h("div", { class: "concept-pool" }, inlineLoading("Loading available concepts…"));
  const formHost = h("div");
  const generatedHost = h("div", {}, loadingPanel("Reading stored generated questions", 3));
  const advancedHost = h("div", {}, loadingPanel("Reading advanced assessment options", 2));
  const misconceptionsHost = h("div", {}, loadingPanel("Reading misconception records", 3));

  wrap.append(panel("Generate practice questions", { eyebrow: "Practice builder" }, conceptsHost, formHost));
  wrap.append(generatedHost);
  wrap.append(advancedHost);
  wrap.append(misconceptionsHost);

  let concepts = [];
  ctx.api
    .generationOptions(ctx.courseId, { syllabus_id: selectedSyllabus(ctx) }, { signal: ctx.signal })
    .then((options) => {
      concepts = safeArray(options.concepts);
      if (!concepts.length) {
        conceptsHost.replaceChildren(
          emptyState({
            title: "No concept is available for generation",
            body:
              "Generation resolved this syllabus but found no concept it can ground. Run text analysis and question understanding, or add a syllabus that lists topics.",
            actions: [
              linkButton("Open course workspace", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
                variant: "primary",
                iconName: "course",
              }),
            ],
            iconName: "practice",
          }),
        );
        return;
      }
      conceptsHost.replaceChildren(
        h(
          "div",
          { class: "concept-pool-summary" },
          h("p", {
            text: `${options.concept_count} generation-ready concept(s) across ${safeArray(options.units).length} unit(s). These are the exact stored syllabus identities; no label guessing is involved.`,
          }),
          h(
            "details",
            { class: "disclosure" },
            h("summary", { text: "Show available concepts" }),
            h(
              "div",
              { class: "disclosure-body" },
              h(
                "ul",
                { class: "compact-list" },
                ...concepts.map((concept) =>
                  h("li", { text: `${concept.label}${concept.unit_label ? ` · ${concept.unit_label}` : ""}${concept.verified ? " · syllabus topic" : ""}` }),
                ),
              ),
            ),
          ),
        ),
      );
      formHost.append(renderGenerationForm(ctx, concepts, generatedHost, advancedHost));
    })
    .catch((error) => {
      if (isAbort(error)) return;
      conceptsHost.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "Practice still lists stored questions below; generation options depend on a parsed syllabus and resolved concept scope.",
        }),
      );
    });

  loadGenerated(ctx, generatedHost);
  loadAdvanced(ctx, advancedHost, () => loadGenerated(ctx, generatedHost));
  loadMisconceptions(ctx, misconceptionsHost);
  return wrap;
}

function renderGenerationForm(ctx, concepts, generatedHost, advancedHost) {
  const conceptSelect = selectControl(
    concepts.map((concept) => ({
      value: concept.concept_id,
      label: `${concept.label}${concept.unit_label ? ` — ${concept.unit_label}` : ""}`,
    })),
    { value: concepts[0]?.concept_id, id: "generate-concept" },
  );
  const syllabus = syllabusSelect(ctx, { onChange: () => ctx.reload() });
  const formatSelect = selectControl(
    FORMATS.map((value) => ({ value, label: humaniseCode(value) })),
    { value: "short_answer", id: "generate-format" },
  );
  const bloomSelect = selectControl(
    [{ value: "", label: "Format default" }, ...BLOOMS.map((value) => ({ value, label: humaniseCode(value) }))],
    { value: "", id: "generate-bloom" },
  );
  const difficultySelect = selectControl(
    [{ value: "", label: "Format default" }, ...DIFFICULTIES.map((value) => ({ value, label: humaniseCode(value) }))],
    { value: "", id: "generate-difficulty" },
  );
  const countInput = inputControl({ type: "number", value: "3", min: "1", max: "20", id: "generate-count" });
  const providerSelect = selectControl(
    [
      { value: "template", label: "Offline template provider" },
      { value: "auto", label: "Auto (template unless configured)" },
    ],
    { value: "template", id: "generate-provider" },
  );
  const includeKey = h("input", { type: "checkbox", checked: true, id: "generate-answer-key" });
  const expandBatch = h("input", {
    type: "checkbox",
    checked: true,
    id: "generate-expand-batch",
  });
  const status = h("div", { class: "form-status", role: "status", "aria-live": "polite" });
  const resultHost = h("div", { class: "generation-result" });
  const submit = h(
    "button",
    { class: "button button-primary", type: "submit" },
    h("span", { text: "Generate questions" }),
  );
  const form = h(
    "form",
    { class: "form-grid" },
    syllabus,
    field("Starting concept", conceptSelect, "The first question targets this concept."),
    field("Format", formatSelect),
    field("Bloom level", bloomSelect),
    field("Difficulty", difficultySelect),
    field(
      "Requested count",
      countInput,
      "1–20. If the count exceeds the selected concepts, the batch continues with other concepts instead of repeating one prompt.",
    ),
    field("Provider", providerSelect, "The template provider is deterministic and offline."),
    h(
      "label",
      { class: "checkbox-field" },
      expandBatch,
      h("span", { text: "Continue across other concepts to keep every question distinct" }),
    ),
    h("label", { class: "checkbox-field" }, includeKey, h("span", { text: "Include answer keys where derivable" })),
    h("div", { class: "form-actions" }, submit),
    status,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Generating, validating and checking for duplicates…";
    resultHost.replaceChildren();
    try {
      const payload = {
        syllabus_id: selectedSyllabus(ctx),
        concept_ids: [conceptSelect.value],
        count: Number(countInput.value) || 1,
        expand_batch: expandBatch.checked,
        format: formatSelect.value,
        provider: providerSelect.value,
        include_answer_key: includeKey.checked,
      };
      if (bloomSelect.value) payload.bloom = bloomSelect.value;
      if (difficultySelect.value) payload.difficulty = difficultySelect.value;
      const batch = await ctx.api.generate(ctx.courseId, payload, { signal: ctx.signal });
      status.dataset.tone = "positive";
      status.textContent = `Batch ${batch.batch_id} returned ${batch.counts?.persisted ?? 0} stored question(s).`;
      resultHost.replaceChildren(renderBatch(ctx, batch, () => loadGenerated(ctx, generatedHost)));
      loadGenerated(ctx, generatedHost);
      loadAdvanced(ctx, advancedHost, () => loadGenerated(ctx, generatedHost));
      ctx.toast("Generation batch completed.", "positive");
    } catch (error) {
      if (isAbort(error)) return;
      status.dataset.tone = "negative";
      status.replaceChildren(h("p", { text: error?.message || "Generation failed." }));
      resultHost.replaceChildren(
        notice(error?.message || "Generation failed.", "caution", "Batch not produced"),
      );
    } finally {
      submit.disabled = false;
    }
  });
  return h("div", { class: "generation-form-wrap" }, form, resultHost);
}

function loadAdvanced(ctx, host, onChanged) {
  const syllabusId = selectedSyllabus(ctx);
  Promise.all([
    ctx.api.advanced(ctx.courseId, { signal: ctx.signal }),
    ctx.api.generationOptions(ctx.courseId, { syllabus_id: syllabusId }, { signal: ctx.signal }),
    ctx.api.generated(ctx.courseId, { limit: 200 }, { signal: ctx.signal }),
  ])
    .then(([advanced, options, generated]) => {
      const caps = advanced.capabilities || {};
      const mutationSpecs = safeArray(caps.mutation_strategies);
      const challengeSpecs = safeArray(caps.challenge_strategies);
      const concepts = safeArray(options.concepts);
      const stored = safeArray(generated.questions);
      const sources = stored.filter(
        (item) => item.origin === "generation" || item.origin === "mutation",
      );
      const counts = advanced.counts || {};
      const resultHost = h("div", { class: "generation-result" });
      const mutationStatus = h("div", {
        class: "form-status",
        role: "status",
        "aria-live": "polite",
      });
      const challengeStatus = h("div", {
        class: "form-status",
        role: "status",
        "aria-live": "polite",
      });

      const mutationForm = sources.length
        ? renderMutationForm(
            ctx,
            sources,
            mutationSpecs,
            syllabusId,
            mutationStatus,
            resultHost,
            onChanged,
          )
        : notice(
            "Generate a stored question first. Mutation derives variants of an existing item and never invents a source question.",
            "neutral",
            "No source question",
          );
      const challengeForm = concepts.length
        ? renderChallengeForm(
            ctx,
            concepts,
            challengeSpecs,
            syllabusId,
            challengeStatus,
            resultHost,
            onChanged,
          )
        : notice(
            "Adversarial questions need a generation-ready concept and grounded course evidence. Run analysis and add a parsed syllabus first.",
            "neutral",
            "No challenge target",
          );

      host.replaceChildren(
        panel(
          "Advanced assessment",
          {
            eyebrow: "Variants and challenges",
            description:
              "Derive variants of a stored question, or generate challenging questions with rubrics. Both write ordinary generated questions and enter the same review queue below.",
          },
          h(
            "p",
            { class: "muted" },
            `${formatInteger(counts.mutation)} stored mutation(s) and ${formatInteger(counts.adversarial)} stored adversarial question(s). Validation is not proof of correctness.`,
          ),
          notice(
            "A mutation preserves the properties its strategy declares. An adversarial challenge supplies a rubric and quoted evidence, never an invented model answer.",
            "neutral",
            "What the backend guarantees",
          ),
          h(
            "div",
            { class: "job-grid" },
            h(
              "div",
              {},
              h("h3", { text: "Derive variants" }),
              h(
                "p",
                { text: "Choose a stored generated question and let the service apply its declared invariants." },
              ),
              mutationForm,
              mutationStatus,
            ),
            h(
              "div",
              {},
              h("h3", { text: "Generate a challenge" }),
              h(
                "p",
                { text: "Targets come from the real generation scope; the system never guesses a concept." },
              ),
              challengeForm,
              challengeStatus,
            ),
          ),
          resultHost,
        ),
      );
    })
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => loadAdvanced(ctx, host, onChanged),
          context: "Advanced assessment is optional; the generated-question review queue above is unaffected.",
        }),
      );
    });
}

function renderMutationForm(ctx, sources, specs, syllabusId, status, resultHost, onChanged) {
  const source = selectControl(
    sources.map((item) => ({
      value: item.question_id,
      label: `${humaniseCode(item.format)} · ${item.text.slice(0, 72)}`,
    })),
    { id: "advanced-source" },
  );
  const strategy = selectControl(
    [
      { value: "", label: "Automatic strategy selection" },
      ...specs.map((item) => ({
        value: item.strategy,
        label: `${item.label}${item.changes_answer ? " · changes answer" : ""}`,
      })),
    ],
    { id: "advanced-mutation-strategy" },
  );
  const count = inputControl({
    type: "number",
    value: "2",
    min: "1",
    max: "5",
    id: "advanced-mutation-count",
  });
  const submit = h(
    "button",
    { class: "button button-primary", type: "submit" },
    h("span", { text: "Create variants" }),
  );
  const form = h(
    "form",
    { class: "form-grid" },
    field("Source question", source),
    field("Strategy", strategy, "Automatic lets the service choose a strategy that fits the source format."),
    field("Requested count", count, "1–5; rejected or duplicate variants are reported, not padded."),
    h("div", { class: "form-actions" }, submit),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Deriving variants and checking invariants…";
    try {
      const payload = {
        syllabus_id: syllabusId,
        question_id: source.value,
        count: Number(count.value) || 1,
      };
      if (strategy.value) payload.strategies = [strategy.value];
      const batch = await ctx.api.mutate(ctx.courseId, payload, { signal: ctx.signal });
      status.dataset.tone = "positive";
      status.textContent = `Batch ${batch.batch_id} returned ${batch.counts?.persisted ?? 0} stored variant(s).`;
      resultHost.replaceChildren(renderBatch(ctx, batch, onChanged));
      ctx.toast("Advanced variants stored for review.", "positive");
      onChanged?.();
    } catch (error) {
      if (isAbort(error)) return;
      status.dataset.tone = "negative";
      status.textContent = error?.message || "Mutation failed.";
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

function renderChallengeForm(ctx, concepts, specs, syllabusId, status, resultHost, onChanged) {
  const concept = selectControl(
    concepts.map((item) => ({
      value: item.concept_id,
      label: `${item.label}${item.unit_label ? ` — ${item.unit_label}` : ""}`,
    })),
    { id: "advanced-challenge-concept" },
  );
  const strategy = selectControl(
    [
      { value: "", label: "Automatic challenge selection" },
      ...specs.map((item) => ({ value: item.strategy, label: item.label })),
    ],
    { id: "advanced-challenge-strategy" },
  );
  const count = inputControl({
    type: "number",
    value: "1",
    min: "1",
    max: "5",
    id: "advanced-challenge-count",
  });
  const submit = h(
    "button",
    { class: "button button-primary", type: "submit" },
    h("span", { text: "Generate challenge" }),
  );
  const form = h(
    "form",
    { class: "form-grid" },
    field("Target concept", concept),
    field("Strategy", strategy),
    field("Requested count", count, "1–5; challenges are review material and carry rubrics."),
    h("div", { class: "form-actions" }, submit),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Generating a grounded challenge…";
    try {
      const payload = {
        syllabus_id: syllabusId,
        concept_ids: [concept.value],
        count: Number(count.value) || 1,
      };
      if (strategy.value) payload.strategies = [strategy.value];
      const batch = await ctx.api.adversarial(ctx.courseId, payload, { signal: ctx.signal });
      status.dataset.tone = "positive";
      status.textContent = `Batch ${batch.batch_id} returned ${batch.counts?.persisted ?? 0} stored challenge(s).`;
      resultHost.replaceChildren(renderBatch(ctx, batch, onChanged));
      ctx.toast("Adversarial question stored for review.", "positive");
      onChanged?.();
    } catch (error) {
      if (isAbort(error)) return;
      status.dataset.tone = "negative";
      status.textContent = error?.message || "Challenge generation failed.";
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

function renderBatch(ctx, batch, onReviewed) {
  const counts = batch.counts || {};
  const questions = safeArray(batch.questions);
  return h(
    "div",
    { class: "view-stack batch-result" },
    h(
      "div",
      { class: "batch-header" },
      h("div", {}, h("p", { class: "eyebrow", text: "Generated batch" }), h("h3", { text: batch.batch_id })),
      statusBadge(humaniseCode(batch.status), batch.status === "complete" ? "positive" : "caution"),
    ),
    h(
      "p",
      { class: "muted" },
      `${batch.provider === "template" ? "Built-in local template" : humaniseCode(batch.provider)} · syllabus ${batch.scope?.syllabus_id || "—"} · requested ${counts.requested || 0}, generated ${counts.persisted || 0}, validated ${counts.validated || 0}, rejected ${counts.rejected || 0}, duplicates ${counts.duplicates || 0}. Each item starts as a draft.`,
    ),
    h("p", { class: "scope-statement", text: batch.scope?.statement || "Scope statement unavailable." }),
    questions.length
      ? h("div", { class: "question-list" }, ...questions.map((question) => renderQuestion(ctx, question, onReviewed)))
      : notice("No question passed validation for this request. The rejections below state why.", "caution", "Empty batch"),
    safeArray(batch.warnings).length
      ? h(
          "details",
          { class: "disclosure" },
          h("summary", {}, h("strong", { text: `Batch notes (${batch.warnings.length})` })),
          h(
            "div",
            { class: "disclosure-body" },
            h(
              "ul",
              { class: "compact-list" },
              ...batch.warnings.map((item) => h("li", { text: item.message })),
            ),
          ),
        )
      : null,
    safeArray(batch.rejections).length
      ? h(
          "details",
          { class: "disclosure" },
          h("summary", {}, h("strong", { text: `${batch.rejections.length} rejection(s)` })),
          h(
            "div",
            { class: "disclosure-body" },
            dataTable({
              caption: "Rejected generation candidates",
              columns: [
                { label: "Index", key: "index", className: "numeric" },
                { label: "Concept", key: "concept_label" },
                { label: "Code", key: "code" },
                { label: "Reason", key: "message" },
              ],
              rows: safeArray(batch.rejections),
            }),
          ),
        )
      : null,
    safeArray(batch.duplicates).length
      ? h(
          "details",
          { class: "disclosure" },
          h("summary", {}, h("strong", { text: `${batch.duplicates.length} similarity decision(s)` })),
          h(
            "div",
            { class: "disclosure-body" },
            dataTable({
              caption: "Duplicate and similarity decisions",
              columns: [
                { label: "Index", key: "index", className: "numeric" },
                { label: "Kind", key: "kind" },
                { label: "Matched question", key: "matched_question_id" },
                { label: "Score", render: (row) => formatNumber(row.score, 3), className: "numeric" },
                { label: "Reason", key: "reason" },
              ],
              rows: safeArray(batch.duplicates),
            }),
          ),
        )
      : null,
    h("ul", { class: "limitations" }, ...safeArray(batch.limitations).map((item) => h("li", { text: item }))),
  );
}

function replaceReviewBadge(headerStatus, badge) {
  const current = headerStatus.querySelector("[data-review-badge]");
  badge.dataset.reviewBadge = "true";
  if (current) current.replaceWith(badge);
  else headerStatus.append(badge);
}

function renderQuestion(ctx, question, onReviewed) {
  const validation = question.validation || {};
  const headerStatus = h("div", { class: "question-badges" });
  const reviewBadge = statusBadge(reviewLabel(question.review_status), reviewTone(question.review_status));
  reviewBadge.dataset.reviewBadge = "true";
  const validationBadge = statusBadge(validationLabel(validation.status), validationTone(validation.status));
  headerStatus.append(validationBadge, reviewBadge);

  if (question.review_status === "pending_review") {
    const approve = h(
      "button",
      { class: "button button-small button-ghost", type: "button", title: "Approve this draft" },
      h("span", { text: "Approve" }),
    );
    approve.addEventListener("click", async () => {
      approve.disabled = true;
      try {
        const updated = await ctx.api.review(
          ctx.courseId,
          question.question_id,
          {
            status: "approved",
            note: "Approved from the question card",
            expected_revision: question.revision,
            reviewer: "dashboard-user",
          },
          { signal: ctx.signal },
        );
        question.revision = updated.revision;
        question.review_status = updated.review_status;
        replaceReviewBadge(
          headerStatus,
          statusBadge(reviewLabel(updated.review_status), reviewTone(updated.review_status)),
        );
        approve.remove();
        ctx.toast("Draft approved.", "positive");
        onReviewed?.(updated);
      } catch (error) {
        if (isAbort(error)) return;
        approve.disabled = false;
        ctx.toast(error?.message || "Could not approve this draft.", "negative");
      }
    });
    headerStatus.append(approve);
  }

  const options = safeArray(question.options);
  return h(
    "article",
    { class: "question-item" },
    h(
      "header",
      { class: "question-header" },
      h(
        "div",
        {},
        h("p", { class: "eyebrow", text: safeArray(question.concept_labels).join(" · ") || "No concept label" }),
        h("h3", { text: question.learning_objective || "Practice question" }),
        h("p", { class: "question-meta", text: `${humaniseCode(question.format)} · ${humaniseCode(question.requested_bloom)} · ${humaniseCode(question.requested_difficulty)} · ${formatNumber(question.marks, 1)} marks` }),
      ),
      headerStatus,
    ),
    h("p", { class: "question-text", text: question.text }),
    options.length
      ? h(
          "ol",
          { class: "option-list" },
          ...options.map((option) =>
            h(
              "li",
              { class: option.is_correct ? "is-correct" : "" },
              h("span", { class: "option-key", text: option.key }),
              h("span", { text: option.text }),
              option.is_correct ? statusBadge("Key", "positive") : null,
            ),
          ),
        )
      : null,
    question.answer_key
      ? h(
          "details",
          { class: "disclosure", open: true },
          h("summary", {}, h("strong", { text: "Reference answer and expected points" })),
          h(
            "div",
            { class: "disclosure-body" },
            h("p", { text: question.answer_key }),
            question.generation_metadata?.answer_key_status === "rubric_only"
              ? statusBadge("Source-limited answer", "caution")
              : null,
            question.explanation ? h("p", { class: "muted", text: question.explanation }) : null,
            safeArray(question.solution_steps).length
              ? h("ol", {}, ...question.solution_steps.map((step) => h("li", { text: step })))
              : null,
          ),
        )
      : safeArray(question.solution_steps).length
        ? h(
            "details",
            { class: "disclosure", open: true },
            h("summary", {}, h("strong", { text: "Rubric and review guidance" })),
            h(
              "div",
              { class: "disclosure-body" },
              h(
                "p",
                { class: "muted",
                  text: "No model answer is supplied for this item. The rubric below is review material, not a learner-facing key.",
                },
              ),
              h("ol", {}, ...question.solution_steps.map((step) => h("li", { text: step }))),
            ),
          )
        : h("p", { class: "muted", text: "No answer key is available for this item; review is required." }),
    safeArray(question.source_references).length
      ? h(
          "details",
          { class: "disclosure" },
          h("summary", {}, h("strong", { text: "Grounded evidence" })),
          h(
            "div",
            { class: "disclosure-body" },
            ...question.source_references.map((source) =>
              h(
                "blockquote",
                {},
                h("p", { text: source.excerpt }),
                h("cite", {
                  text: `${source.document_title}${source.page_number ? `, page ${source.page_number}` : ""} · ${humaniseCode(source.method)}`,
                }),
              ),
            ),
          ),
        )
      : h("p", { class: "caution-text", text: "No source passage is attached; treat this item as unsupported until reviewed." }),
    safeArray(validation.issues).length
      ? h(
          "details",
          { class: "disclosure" },
          h("summary", {}, h("strong", { text: `Validation findings (${validation.issues.length})` })),
          h(
            "div",
            { class: "disclosure-body" },
            h(
              "ul",
              { class: "compact-list" },
              ...validation.issues.map((issue) =>
                h("li", { text: `${humaniseCode(issue.severity)} · ${issue.code} (${issue.rule}): ${issue.message}` }),
              ),
            ),
          ),
        )
      : null,
    renderAnswerCheck(ctx, question),
    h(
      "footer",
      { class: "question-footer" },
      h("span", { class: "muted", text: `${question.question_id} · revision ${question.revision}` }),
      h("div", { class: "review-control" }, renderReviewControls(ctx, question, headerStatus, onReviewed)),
    ),
    h("ul", { class: "limitations" }, ...safeArray(question.limitations).map((item) => h("li", { text: item }))),
  );
}

function renderReviewControls(ctx, question, headerStatus, onReviewed) {
  const select = selectControl(REVIEW_STATUSES, {
    value: question.review_status,
    id: `review-${question.question_id}`,
  });
  const note = inputControl({
    placeholder: "Optional reviewer note",
    id: `note-${question.question_id}`,
  });
  const status = h("span", { class: "job-state", role: "status", "aria-live": "polite" });
  const approve = h(
    "button",
    { class: "button button-small button-primary", type: "button" },
    h("span", { text: "Approve" }),
  );
  const reject = h(
    "button",
    { class: "button button-small button-ghost", type: "button" },
    h("span", { text: "Reject" }),
  );
  const submit = h(
    "button",
    { class: "button button-small button-ghost", type: "button" },
    h("span", { text: "Save status" }),
  );

  async function saveStatus(nextStatus) {
    [approve, reject, submit].forEach((button) => {
      button.disabled = true;
    });
    status.dataset.tone = "neutral";
    status.textContent = "Saving…";
    try {
      const updated = await ctx.api.review(
        ctx.courseId,
        question.question_id,
        {
          status: nextStatus,
          note: note.value || undefined,
          expected_revision: question.revision,
          reviewer: "dashboard-user",
        },
        { signal: ctx.signal },
      );
      question.revision = updated.revision;
      question.review_status = updated.review_status;
      select.value = updated.review_status;
      replaceReviewBadge(
        headerStatus,
        statusBadge(reviewLabel(updated.review_status), reviewTone(updated.review_status)),
      );
      status.dataset.tone = "positive";
      status.textContent = reviewLabel(updated.review_status);
      ctx.toast("Review decision saved.", "positive");
      onReviewed?.(updated);
    } catch (error) {
      if (isAbort(error)) return;
      status.dataset.tone = "negative";
      status.textContent = error?.message || "Could not save the review.";
    } finally {
      [approve, reject, submit].forEach((button) => {
        button.disabled = false;
      });
    }
  }

  approve.addEventListener("click", () => saveStatus("approved"));
  reject.addEventListener("click", () => saveStatus("rejected"));
  submit.addEventListener("click", () => saveStatus(select.value));
  return h(
    "div",
    { class: "review-fields" },
    h("div", { class: "review-actions" }, approve, reject, select, note, submit),
    status,
  );
}

function renderAnswerCheck(ctx, question) {
  const options = safeArray(question.options);
  const answer = options.length
    ? selectControl(
        [
          { value: "", label: "Choose an option…" },
          ...options.map((option) => ({ value: option.key, label: `${option.key}. ${option.text}` })),
        ],
        { id: `answer-${question.question_id}` },
      )
    : h("textarea", {
        class: "answer-input",
        id: `answer-${question.question_id}`,
        rows: "4",
        placeholder: "Write your answer here. Use the key ideas named in the question.",
        "aria-label": "Your answer",
      });
  const status = h("div", { class: "form-status", role: "status", "aria-live": "polite" });
  const resultHost = h("div", { class: "answer-feedback", "aria-live": "polite" });
  const submit = h(
    "button",
    { class: "button button-small button-primary", type: "submit" },
    h("span", { text: "Check answer" }),
  );
  const form = h(
    "form",
    { class: "answer-form" },
    h("label", { class: "answer-label", for: answer.id }, h("span", { text: "Your answer" })),
    answer,
    h("div", { class: "form-actions" }, submit),
    status,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Checking against the stored reference…";
    resultHost.replaceChildren();
    try {
      const payload = options.length ? { option_key: answer.value } : { answer: answer.value };
      const result = await ctx.api.grade(ctx.courseId, question.question_id, payload, {
        signal: ctx.signal,
      });
      status.textContent = "";
      resultHost.replaceChildren(renderGradeResult(result));
    } catch (error) {
      if (isAbort(error)) return;
      status.dataset.tone = "negative";
      status.textContent = error?.message || "Could not check this answer.";
    } finally {
      submit.disabled = false;
    }
  });
  return h(
    "details",
    { class: "disclosure answer-check" },
    h("summary", {}, h("strong", { text: "Check your answer" })),
    h(
      "div",
      { class: "disclosure-body" },
      h(
        "p",
        { class: "muted",
          text: "Local feedback only: multiple-choice answers use the stored key; written answers are checked for coverage of the stored reference and key ideas.",
        },
      ),
      form,
      resultHost,
    ),
  );
}

function renderGradeResult(result) {
  const positive = result.verdict === "correct" || result.verdict === "strong_match";
  const title =
    result.verdict === "correct"
      ? "Correct"
      : result.verdict === "strong_match"
        ? "Strong coverage"
        : result.verdict === "partial"
          ? "Partly covered"
          : result.verdict === "self_review"
            ? "Self-check required"
            : "Needs more detail";
  const tone = positive ? "positive" : "caution";
  const points = safeArray(result.expected_points);
  return h(
    "div",
    { class: "grade-result" },
    notice(result.feedback || "Answer checked.", tone, title),
    result.score !== undefined && result.verdict !== "self_review"
      ? h(
          "p",
          { class: "grade-score" },
          h("strong", { text: `${formatPercent(result.score, 0)} match` }),
          h("span", {
            class: "muted",
            text: ` · ${formatNumber(result.awarded_score, 1)} / ${formatNumber(result.max_score, 1)} marks indicated`,
          }),
        )
      : null,
    points.length
      ? h(
          "div",
          { class: "grade-points" },
          h("p", { class: "eyebrow", text: "Expected answer outline" }),
          h("ul", { class: "compact-list" }, ...points.map((point) => h("li", { text: point }))),
        )
      : null,
    safeArray(result.missing_terms).length
      ? h("p", { class: "muted", text: `Not found in your answer: ${result.missing_terms.join(", ")}.` })
      : null,
    result.reference_answer
      ? h(
          "details",
          { class: "disclosure" },
          h("summary", {}, h("strong", { text: "Stored answer guidance" })),
          h("p", { text: result.reference_answer }),
        )
      : null,
    safeArray(result.limitations).length
      ? h("ul", { class: "limitations" }, ...result.limitations.map((item) => h("li", { text: item })))
      : null,
  );
}

function loadGenerated(ctx, host) {
  host.replaceChildren(loadingPanel("Reading stored generated questions", 3));
  const filter = selectControl(
    [{ value: "", label: "All review states" }, ...REVIEW_STATUSES],
    { id: "generated-filter" },
  );
  const listHost = h("div");
  filter.addEventListener("change", () => {
    draw(filter.value);
  });
  function draw(status) {
    listHost.replaceChildren(inlineLoading("Filtering stored questions…"));
    ctx.api
      .generated(ctx.courseId, { limit: 100, review_status: status || undefined }, { signal: ctx.signal })
      .then((payload) => {
        const questions = safeArray(payload.questions);
        listHost.replaceChildren(
          questions.length
            ? h("div", { class: "question-list" }, ...questions.map((question) => renderQuestion(ctx, question, () => draw(filter.value))))
            : h("p", { class: "muted", text: "No stored generated question matches this filter." }),
        );
      })
      .catch((error) => {
        if (isAbort(error)) return;
        listHost.replaceChildren(errorState(error, { retry: () => draw(filter.value) }));
      });
  }
  draw("");
  host.replaceChildren(
    panel(
      "Stored generated questions",
      {
        eyebrow: "Review queue",
        description: "Every item starts as a draft. Approve it when you are ready to use it; the decision is stored in its history.",
        actions: [filter],
      },
      listHost,
    ),
  );
}

function loadMisconceptions(ctx, host) {
  host.replaceChildren(loadingPanel("Reading misconception records", 3));
  const listHost = h("div");
  const status = h("p", { class: "form-status", role: "status", "aria-live": "polite" });
  const mine = h("button", { class: "button button-ghost", type: "button" }, h("span", { text: "Mine stored responses" }));
  mine.addEventListener("click", async () => {
    mine.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Reading stored anonymised responses…";
    try {
      const report = await ctx.api.analyseMisconceptions(
        ctx.courseId,
        { syllabus_id: selectedSyllabus(ctx) },
        { signal: ctx.signal },
      );
      status.dataset.tone = "positive";
      status.textContent = `${safeArray(report.records).length} candidate record(s) from ${report.responses_supplied} supplied response(s).`;
      listHost.replaceChildren(renderMisconceptionResult(ctx, report, listHost));
    } catch (error) {
      if (isAbort(error)) return;
      status.dataset.tone = "negative";
      status.textContent = error?.message || "Misconception analysis failed.";
    } finally {
      mine.disabled = false;
    }
  });

  ctx.api
    .misconceptions(ctx.courseId, { limit: 100 }, { signal: ctx.signal })
    .then((payload) => {
      const rows = safeArray(payload.misconceptions);
      host.replaceChildren(
        panel(
          "Misconception candidates",
          {
            eyebrow: "Evidence-bounded patterns",
            description:
              "Patterns come only from stored anonymised responses with reference answers. Import responses with the CLI before mining.",
            actions: [mine],
          },
          status,
          rows.length
            ? h("div", { class: "view-stack" }, ...rows.map((row) => renderMisconception(row)))
            : h(
                "div",
                { class: "view-stack" },
                notice(
                  "No misconception record is stored and none is invented. Import anonymised response data with the local command-line tool to mine evidence-backed patterns here.",
                  "neutral",
                  "No stored responses",
                ),
                h(
                  "pre",
                  { class: "code-block", text: "python -m exam_engine advanced responses import --course <course_id> --file responses.json" },
                ),
              ),
        ),
      );
    })
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(errorState(error, { retry: () => loadMisconceptions(ctx, host) }));
    });
}

function renderMisconceptionResult(ctx, report, host) {
  const records = safeArray(report.records);
  return h(
    "div",
    { class: "view-stack" },
    notice(
      `${report.responses_supplied} response(s) supplied, ${report.responses_stored} stored, ${report.distinct_responses} distinct. ${safeArray(report.discarded).length} answer(s) were discarded with a reason.`,
      records.length ? "neutral" : "caution",
      "Mining report",
    ),
    records.length
      ? h("div", { class: "view-stack" }, ...records.map((row) => renderMisconception(row)))
      : h("p", { class: "muted", text: "No candidate pattern cleared the support threshold." }),
    safeArray(report.warnings).length
      ? h("ul", { class: "limitations" }, ...report.warnings.map((item) => h("li", { text: `${item.code}: ${item.message}` })))
      : null,
    h("button", {
      class: "button button-ghost",
      type: "button",
      onClick: () => loadMisconceptions(ctx, host.closest(".panel")?.parentElement || host),
      text: "Reload stored records",
    }),
  );
}

function renderMisconception(row) {
  return h(
    "article",
    { class: "question-item" },
    h(
      "header",
      { class: "question-header" },
      h(
        "div",
        {},
        h("p", { class: "eyebrow", text: `${humaniseCode(row.kind)} · ${safeArray(row.concept_labels).join(", ") || "no concept"}` }),
        h("h3", { text: row.label }),
        h("p", { class: "question-meta", text: `${row.support_count} supporting response(s) · ${row.distinct_questions} question(s) · confidence ${formatPercent(row.confidence, 0)}` }),
      ),
      statusBadge(humaniseCode(row.support_level), row.support_level === "recurring" ? "caution" : "neutral"),
    ),
    h("p", { text: row.description }),
    row.suggested_correction ? h("p", { class: "muted", text: `Suggested correction: ${row.suggested_correction}` }) : null,
    safeArray(row.evidence).length
      ? h(
          "details",
          { class: "disclosure" },
          h("summary", {}, h("strong", { text: "Evidence excerpts" })),
          h(
            "div",
            { class: "disclosure-body" },
            ...row.evidence.map((evidence) =>
              h("blockquote", {}, h("p", { text: evidence.excerpt }), h("cite", { text: `${evidence.marker} · ${evidence.method}` })),
            ),
          ),
        )
      : null,
    h("ul", { class: "limitations" }, ...safeArray(row.limitations).map((item) => h("li", { text: item }))),
    disclaimer("Support counts depend on the supplied response identifiers. They are not class-wide rates or claims about a student's ability."),
  );
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
