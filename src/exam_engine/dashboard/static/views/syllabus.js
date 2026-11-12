import { h, icon } from "../dom.js";
import { formatInteger, formatPercent, humaniseCode, safeArray } from "../ui-state.js";
import {
  emptyState,
  errorState,
  inlineLoading,
  linkButton,
  loadingPanel,
  pageHeader,
  panel,
  statusBadge,
} from "./shared.js";

export function renderSyllabus(ctx) {
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    pageHeader({
      eyebrow: `${ctx.course.course_code} · Curriculum`,
      title: "Syllabus explorer",
      description:
        "Follow the stored course outline from unit to topic. Related questions come from mapped records only; an absence of questions is not evidence that a topic is unimportant.",
      actions: [
        linkButton("Question bank", `#/exams/questions?course=${encodeURIComponent(ctx.courseId)}`, {
          variant: "primary",
          iconName: "search",
        }),
        linkButton("Coverage gaps", `#/c/${encodeURIComponent(ctx.courseId)}/analysis/coverage`, {
          iconName: "check",
        }),
      ],
    }),
  );
  const host = h("div", { class: "view-stack" }, loadingPanel("Reading stored syllabus structure", 5));
  wrap.append(host);

  ctx.api
    .examSyllabus(ctx.courseId, { signal: ctx.signal })
    .then((payload) => host.replaceChildren(drawSyllabus(ctx, payload)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context:
            "The syllabus explorer reads the parsed syllabus records. The original documents remain in the course workspace.",
        }),
      );
    });
  return wrap;
}

function drawSyllabus(ctx, payload) {
  const syllabi = safeArray(payload.syllabi);
  if (!syllabi.length) {
    return emptyState({
      title: "No parsed syllabus is available",
      body: "Add a syllabus document in the course workspace, then run the syllabus and analysis pipeline. The explorer will show units and topics as soon as they are stored.",
      actions: [
        linkButton("Open course workspace", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
          variant: "primary",
          iconName: "upload",
        }),
      ],
      iconName: "course",
    });
  }

  const wrap = h("div", { class: "view-stack" });
  const preferred = ctx.preferences?.syllabusId;
  let selectedSyllabusId =
    syllabi.find((syllabus) => syllabus.syllabus_id === preferred)?.syllabus_id ||
    syllabi[0].syllabus_id;
  let selectedUnitId = null;
  let questionSequence = 0;

  const syllabusSelect = h(
    "select",
    { id: "syllabus-select", "aria-label": "Syllabus version" },
    ...syllabi.map((syllabus) =>
      h("option", {
        value: syllabus.syllabus_id,
        text: `${syllabus.title || syllabus.syllabus_id} · ${formatInteger(syllabus.units?.length || 0)} units`,
      }),
    ),
  );
  syllabusSelect.value = selectedSyllabusId;
  syllabusSelect.addEventListener("change", () => {
    selectedSyllabusId = syllabusSelect.value;
    ctx.setPreference("syllabusId", selectedSyllabusId);
    selectedUnitId = null;
    draw();
  });

  const search = h("input", {
    type: "search",
    placeholder: "Find a unit or topic",
    "aria-label": "Filter syllabus units and topics",
    autocomplete: "off",
  });
  search.addEventListener("input", () => drawIndex());

  const indexHost = h("div", { class: "syllabus-index-list" });
  const detailHost = h("div", { class: "syllabus-detail" });
  const sourceHost = h("div", { class: "syllabus-source" });
  const selectedLabel = h("p", { class: "syllabus-selected-kicker" });

  function currentSyllabus() {
    return syllabi.find((syllabus) => syllabus.syllabus_id === selectedSyllabusId) || syllabi[0];
  }

  function units() {
    return safeArray(currentSyllabus()?.units);
  }

  function filteredUnits() {
    const needle = search.value.trim().toLocaleLowerCase();
    if (!needle) return units();
    return units().filter((unit) => {
      const haystack = [unit.unit_label, unit.title, ...safeArray(unit.topics)]
        .join(" ")
        .toLocaleLowerCase();
      return haystack.includes(needle);
    });
  }

  function drawIndex() {
    const items = filteredUnits();
    if (!selectedUnitId || !items.some((unit) => unit.unit_id === selectedUnitId)) {
      selectedUnitId = items[0]?.unit_id || null;
    }
    indexHost.replaceChildren(
      ...items.map((unit, index) => {
        const active = unit.unit_id === selectedUnitId;
        return h(
          "button",
          {
            type: "button",
            class: `syllabus-unit-button ${active ? "is-active" : ""}`.trim(),
            "aria-current": active ? "true" : null,
            onClick: () => {
              selectedUnitId = unit.unit_id;
              drawIndex();
              drawDetail();
            },
          },
          h(
            "span",
            { class: "unit-index-marker", "aria-hidden": "true" },
            unitBadge(unit, index),
          ),
          h(
            "span",
            { class: "unit-index-copy" },
            h("strong", { text: unit.title || unit.unit_label || "Untitled unit" }),
            h("small", {
              text: `${unit.unit_label || "Unit"} · ${formatInteger(safeArray(unit.topics).length)} topics`,
            }),
          ),
          icon("arrow"),
        );
      }),
    );
    if (!items.length) {
      indexHost.append(h("p", { class: "muted", text: "No unit or topic matches this search." }));
    }
    drawSource();
  }

  function drawDetail() {
    const unit = units().find((item) => item.unit_id === selectedUnitId) || filteredUnits()[0];
    if (!unit) {
      detailHost.replaceChildren(
        emptyState({
          title: "No unit selected",
          body: "Search the outline or choose a stored unit to read its topics.",
          iconName: "course",
        }),
      );
      return;
    }
    selectedLabel.textContent = `${unit.unit_label || "Syllabus unit"} · ${formatInteger(safeArray(unit.topics).length)} topics`;
    const syllabus = currentSyllabus();
    const confidence = typeof unit.confidence === "number" ? unit.confidence : syllabus?.confidence;
    const topics = safeArray(unit.topics);
    detailHost.replaceChildren(
      h(
        "div",
        { class: "syllabus-unit" },
        h(
          "div",
          { class: "syllabus-unit-head" },
          h(
            "div",
            {},
            h("p", { class: "eyebrow", text: unit.unit_label || "Syllabus unit" }),
            h("h2", { text: unit.title || unit.unit_label || "Untitled unit" }),
            h("p", {
              class: "panel-description",
              text: unit.hours ? `${unit.hours} recorded study hours.` : "No study-hour estimate is stored for this unit.",
            }),
          ),
          h(
            "div",
            { class: "syllabus-unit-badges" },
            confidence !== undefined
              ? statusBadge(`Extraction ${formatPercent(confidence, 0)}`, confidence >= 0.75 ? "positive" : "caution")
              : null,
            syllabus?.needs_review ? statusBadge("Needs attention", "caution") : statusBadge("Parsed", "positive"),
          ),
        ),
        h(
          "section",
          { class: "topic-section" },
          h(
            "div",
            { class: "section-heading" },
            h("div", {}, h("h3", { text: "Topics in this unit" }), h("p", { text: "Stored syllabus topics, in source order." })),
            h("span", { class: "count-pill", text: `${formatInteger(topics.length)} topics` }),
          ),
          topics.length
            ? h(
                "ol",
                { class: "topic-list" },
                ...topics.map((topic, index) =>
                  h(
                    "li",
                    { class: "topic-row" },
                    h("span", { class: "topic-number", "aria-hidden": "true" }, String(index + 1).padStart(2, "0")),
                    h("p", { text: topic }),
                  ),
                ),
              )
            : h("p", { class: "muted", text: "No topic text was extracted for this unit." }),
        ),
        h(
          "section",
          { class: "related-section" },
          h(
            "div",
            { class: "section-heading" },
            h(
              "div",
              {},
              h("h3", { text: "Related question evidence" }),
              h("p", { text: "Questions mapped to this unit in the current corpus." }),
            ),
            h(
              "a",
              {
                class: "text-link",
                href: `#/exams/questions?course=${encodeURIComponent(ctx.courseId)}&unit_id=${encodeURIComponent(unit.unit_id)}`,
              },
              "Browse question bank",
              icon("arrow"),
            ),
          ),
          h("div", { class: "related-question-host" }, inlineLoading("Reading related questions…")),
        ),
        h(
          "div",
          { class: "syllabus-evidence-note" },
          icon("info"),
          h(
            "p",
            {},
            h("strong", { text: "Evidence boundary. " }),
            "The count below reflects stored question mappings only. It does not prove curriculum emphasis, and an absent question is not proof that a topic is not examined.",
          ),
        ),
      ),
    );
    drawSource();
    loadRelatedQuestions(unit, detailHost.querySelector(".related-question-host"));
  }

  function drawSource() {
    const syllabus = currentSyllabus();
    const documents = safeArray(ctx.course?.documents);
    const sourceDoc = documents.find((doc) => doc.document_id === syllabus?.document_id);
    sourceHost.replaceChildren(
      h(
        "div",
        { class: "syllabus-source-head" },
        h("span", { class: "syllabus-source-icon", "aria-hidden": "true" }, icon("course")),
        h(
          "div",
          {},
          h("strong", { text: sourceDoc?.title || syllabus?.title || "Syllabus source" }),
          h("small", {
            text: sourceDoc?.original_filename || syllabus?.syllabus_id || "Source document not recorded",
          }),
        ),
      ),
      h(
        "dl",
        { class: "source-facts" },
        sourceFact("Format", sourceDoc ? (sourceDoc.format || "not recorded").toUpperCase() : "not recorded"),
        sourceFact("Pages", sourceDoc?.page_count ? formatInteger(sourceDoc.page_count) : "not recorded"),
        sourceFact("Extraction", sourceDoc ? humaniseCode(sourceDoc.extraction_status || "unknown") : "not recorded"),
        sourceFact("Units", formatInteger(units().length)),
      ),
      h("p", {
        class: "source-note",
        text: "The original source file stays in the local dataset folder. This dashboard does not expose it as a public download.",
      }),
      h(
        "a",
        { class: "text-link", href: `#/c/${encodeURIComponent(ctx.courseId)}/course` },
        "Open course documents",
        icon("arrow"),
      ),
    );
  }

  async function loadRelatedQuestions(unit, host) {
    const sequence = ++questionSequence;
    try {
      const payload = await ctx.api.examQuestions(
        { course: ctx.courseId, unit_id: unit.unit_id, limit: 4 },
        { signal: ctx.signal },
      );
      if (sequence !== questionSequence) return;
      const rows = safeArray(payload.questions);
      const total = Number(payload.total || 0);
      if (!rows.length) {
        host.replaceChildren(
          h(
            "p",
            { class: "muted related-empty" },
            "No question is mapped to this unit in the current corpus. This is a mapping gap, not a claim about the examination.",
          ),
        );
        return;
      }
      host.replaceChildren(
        h(
          "div",
          { class: "related-question-list" },
          ...rows.map((question) =>
            h(
              "a",
              {
                class: "related-question",
                href: `#/exams/questions?course=${encodeURIComponent(ctx.courseId)}&unit_id=${encodeURIComponent(unit.unit_id)}&question_id=${encodeURIComponent(question.question_id)}&search=${encodeURIComponent(question.source_question_id || question.question_id || "")}`,
              },
              h(
                "span",
                { class: "related-question-meta" },
                h("span", { text: question.exam_year ? String(question.exam_year) : "Year not recorded" }),
                h("span", { text: humaniseCode(question.question_type) }),
              ),
              h("strong", { text: compact(question.text, 180) }),
              icon("arrow"),
            ),
          ),
          h(
            "a",
            {
              class: "text-link related-count",
              href: `#/exams/questions?course=${encodeURIComponent(ctx.courseId)}&unit_id=${encodeURIComponent(unit.unit_id)}`,
            },
            `${formatInteger(total)} mapped question${total === 1 ? "" : "s"} in the question bank`,
            icon("arrow"),
          ),
        ),
      );
    } catch (error) {
      if (isAbort(error) || sequence !== questionSequence) return;
      host.replaceChildren(
        h(
          "p",
          { class: "muted related-empty" },
          "Question mapping is unavailable for this unit. The syllabus content remains readable.",
        ),
      );
    }
  }

  function draw() {
    wrap.replaceChildren(
      h(
        "div",
        { class: "syllabus-overview" },
        h(
          "div",
          { class: "syllabus-overview-copy" },
          h("p", { class: "eyebrow", text: "Curriculum structure" }),
          h("h2", { text: currentSyllabus()?.title || "Syllabus" }),
          h("p", {
            class: "panel-description",
            text: "Select an outline unit to read its topics, source document and mapped question evidence.",
          }),
        ),
        h(
          "div",
          { class: "syllabus-overview-controls" },
          h("label", { class: "field syllabus-select-field" }, h("span", { text: "Syllabus version" }), syllabusSelect),
          h("div", { class: "syllabus-overview-stats" },
            h("div", {}, h("strong", { text: formatInteger(units().length) }), h("span", { text: "units" })),
            h("div", {}, h("strong", { text: formatInteger(units().reduce((total, unit) => total + safeArray(unit.topics).length, 0)) }), h("span", { text: "topics" })),
          ),
        ),
      ),
      h(
        "div",
        { class: "syllabus-layout" },
        h(
          "aside",
          { class: "syllabus-index-column", "aria-label": "Syllabus outline" },
          panel(
            "Course outline",
            {
              eyebrow: "Navigate without losing context",
              description: "Choose a unit. Your place in the course stays beside the detail pane.",
            },
            h(
              "label",
              { class: "field syllabus-search" },
              h("span", { class: "visually-hidden", text: "Search syllabus" }),
              h("span", { class: "search-field-icon", "aria-hidden": "true" }, icon("search")),
              search,
            ),
            selectedLabel,
            indexHost,
          ),
          sourceHost,
        ),
        h(
          "div",
          { class: "syllabus-detail-column" },
          detailHost,
        ),
      ),
    );
    drawIndex();
    drawDetail();
  }

  draw();
  return wrap;
}

function sourceFact(label, value) {
  return h("div", {}, h("dt", { text: label }), h("dd", { text: value }));
}

// The outline marker must show the stored unit identity, not the row's position
// in an outline that can start at Unit 2 or skip a section.
function unitBadge(unit, index) {
  const labelled = String(unit?.unit_label || "").match(/\b(?:unit|section)\s+(\d{1,2})\b/i);
  if (labelled) return labelled[1].padStart(2, "0");
  const number = Number(unit?.unit_number);
  if (Number.isInteger(number) && number > 0 && number < 100) return String(number).padStart(2, "0");
  return String(index + 1).padStart(2, "0");
}

function compact(value, max) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
