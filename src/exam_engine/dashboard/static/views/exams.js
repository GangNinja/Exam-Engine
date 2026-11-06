import { h, icon } from "../dom.js";
import { barChart, lineChart } from "../charts.js";
import { formatInteger, formatPercent, humaniseCode, safeArray } from "../ui-state.js";
import {
  dataTable,
  disclaimer,
  emptyState,
  errorState,
  loadingPanel,
  pageHeader,
  panel,
  statusBadge,
} from "./shared.js";

const PAGE_SIZE = 50;
const MODES = [
  { key: "explore", label: "Explorer", href: "#/exams" },
  { key: "questions", label: "Question bank", href: "#/exams/questions" },
  { key: "papers", label: "Papers", href: "#/exams/papers" },
];

export function renderExams(ctx, { mode = "explore", query = new URLSearchParams() } = {}) {
  const activeMode = MODES.some((item) => item.key === mode) ? mode : "explore";
  const initialFilters = {
    category: clean(query.get("category")),
    course: clean(query.get("course")),
    year: clean(query.get("year")),
    type: clean(query.get("type")),
    answer_type: clean(query.get("answer_type")),
    unit_id: clean(query.get("unit_id")),
    topic: clean(query.get("topic")),
    search: clean(query.get("search")),
  };
  const state = {
    overview: null,
    filters: null,
    filterData: null,
    initial: initialFilters,
    syllabus: null,
    loading: false,
    pending: false,
    loadingMore: false,
    questions: [],
    questionTotal: 0,
    papers: [],
  };
  const categorySelect = selectField("Examination category", "All categories");
  const subjectSelect = selectField("Subject", "All subjects");
  const yearSelect = selectField("Examination year", "All years");
  const typeSelect = selectField("Question type", "All formats");
  const answerSelect = selectField("Answer format", "All answer formats");
  const unitSelect = selectField("Syllabus unit", "All units");
  const topicSelect = selectField("Topic", "All topics");
  const search = h("input", {
    type: "search",
    placeholder: "Search question or answer text",
    "aria-label": "Search imported questions",
    value: initialFilters.search,
    autocomplete: "off",
  });
  const apply = h("button", { class: "button button-primary", type: "button" }, "Apply filters");
  const reset = h("button", { class: "button button-ghost", type: "button" }, "Reset");
  const filterStatus = h("p", { class: "filter-status", role: "status", "aria-live": "polite" });
  const filterCount = h("span", { class: "filter-count", text: "0" });
  const summaryHost = h("div", {}, loadingPanel("Reading stored examination records", 3));
  const contentHost = h("div", { class: "view-stack" }, loadingPanel("Preparing the explorer", 4));

  const filterPanel = h(
    "details",
    { class: "filter-panel", open: true },
    h(
      "summary",
      { class: "filter-summary" },
      h(
        "span",
        { class: "filter-summary-title" },
        icon("analysis"),
        h("span", { text: "Filter the corpus" }),
        filterCount,
      ),
      h("span", { class: "filter-summary-hint", text: "Category · subject · year · format · topic" }),
    ),
    h(
      "div",
      { class: "filter-panel-body" },
      h(
        "div",
        { class: "filter-grid" },
        categorySelect.wrap,
        subjectSelect.wrap,
        yearSelect.wrap,
        typeSelect.wrap,
        answerSelect.wrap,
        unitSelect.wrap,
        topicSelect.wrap,
        h("label", { class: "field field-search" }, h("span", { text: "Search text" }), search),
      ),
      h(
        "div",
        { class: "filter-footer" },
        h("p", { class: "filter-note", text: "Filters narrow the explorer, question list and paper table. Empty fields mean all records." }),
        h("div", { class: "filter-actions" }, reset, apply),
        filterStatus,
      ),
    ),
  );

  const wrap = h("div", { class: "view-stack explorer-view" });
  wrap.append(
    pageHeader({
      eyebrow: "Examinations · Local corpus",
      title:
        activeMode === "questions"
          ? "Question bank"
          : activeMode === "papers"
            ? "Question papers"
            : "Examination explorer",
      description:
        activeMode === "questions"
          ? "Search the original question records, inspect options and answers, and trace every item back to its source paper."
          : activeMode === "papers"
            ? "Source paper groupings derived from stored question identifiers. Every paper is a recorded occurrence, not an inferred examination calendar."
            : "Move from an examination category to a subject, year, paper and individual question. The explorer uses only the records in your local corpus.",
      actions: [
        h("a", { class: "button button-ghost", href: "#/datasets" }, icon("upload"), "Dataset audit"),
        activeMode === "questions"
          ? h("a", { class: "button button-primary", href: "#/exams/papers" }, "Browse papers", icon("arrow"))
          : h("a", { class: "button button-primary", href: "#/exams/questions" }, "Open question bank", icon("search")),
      ],
    }),
    h(
      "nav",
      { class: "workspace-tabs", "aria-label": "Examination views" },
      ...MODES.map((item) =>
        h(
          "a",
          {
            class: `workspace-tab ${item.key === activeMode ? "is-active" : ""}`.trim(),
            href: item.href,
            "aria-current": item.key === activeMode ? "page" : null,
          },
          item.label,
        ),
      ),
    ),
    filterPanel,
    summaryHost,
    contentHost,
  );

  categorySelect.node.addEventListener("change", () => {
    const category = categorySelect.value();
    refreshSubjectOptions(category);
    setOptions(unitSelect.node, [], "All units", "");
    setOptions(topicSelect.node, [], "All topics", "");
    updateFilterCount();
    filterStatus.textContent = "Category changed. Apply filters to update the view.";
  });
  subjectSelect.node.addEventListener("change", () => {
    setOptions(unitSelect.node, [], "All units", "");
    setOptions(topicSelect.node, [], "All topics", "");
    syncSyllabus(subjectSelect.value());
    updateFilterCount();
    filterStatus.textContent = "Subject changed. Apply filters to update the view.";
  });
  unitSelect.node.addEventListener("change", () => {
    fillTopics(unitSelect.value(), "");
    updateFilterCount();
  });
  topicSelect.node.addEventListener("change", updateFilterCount);
  yearSelect.node.addEventListener("change", updateFilterCount);
  typeSelect.node.addEventListener("change", updateFilterCount);
  answerSelect.node.addEventListener("change", updateFilterCount);
  search.addEventListener("input", updateFilterCount);
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      load();
    }
  });
  apply.addEventListener("click", () => load());
  reset.addEventListener("click", () => {
    categorySelect.node.value = "";
    subjectSelect.node.value = "";
    yearSelect.node.value = "";
    typeSelect.node.value = "";
    answerSelect.node.value = "";
    unitSelect.node.value = "";
    topicSelect.node.value = "";
    search.value = "";
    refreshSubjectOptions("");
    setOptions(unitSelect.node, [], "All units", "");
    setOptions(topicSelect.node, [], "All topics", "");
    updateFilterCount();
    load();
  });

  load({ initial: true });
  return wrap;

  function currentFilters() {
    return {
      category: categorySelect.value(),
      course: subjectSelect.value(),
      year: yearSelect.value(),
      type: typeSelect.value(),
      answer_type: answerSelect.value(),
      unit_id: unitSelect.value(),
      topic: topicSelect.value(),
      search: search.value.trim(),
    };
  }

  function updateFilterCount() {
    const filters = currentFilters();
    const count = Object.values(filters).filter(Boolean).length;
    filterCount.textContent = String(count);
  }

  async function load({ initial = false } = {}) {
    if (state.loading) {
      state.pending = true;
      return;
    }
    state.loading = true;
    apply.disabled = true;
    apply.textContent = "Updating…";
    filterStatus.textContent = "Updating the selected view…";
    contentHost.setAttribute("aria-busy", "true");
    const filters = initial ? { ...initialFilters } : currentFilters();
    try {
      const overview = await ctx.api.examsOverview(filters, { signal: ctx.signal });
      state.overview = overview;
      state.filters = filters;
      state.filterData = overview.filters;
      fillFilters(overview.filters, initial ? initialFilters : {});
      updateFilterCount();
      summaryHost.replaceChildren(renderSummary(overview.summary, filters));
      if (initial && initialFilters.course) {
        await syncSyllabus(initialFilters.course, initialFilters.unit_id, initialFilters.topic);
      }
      state.initial = null;

      if (activeMode === "questions") {
        const questions = await ctx.api.examQuestions(
          { ...filters, limit: PAGE_SIZE },
          { signal: ctx.signal },
        );
        state.questions = safeArray(questions.questions);
        state.questionTotal = Number(questions.total || 0);
        drawQuestionBank(state.questions);
      } else if (activeMode === "papers") {
        const papers = await ctx.api.examPapers(filters, { signal: ctx.signal });
        state.papers = safeArray(papers.papers);
        drawPapers(state.papers, Number(papers.total || 0));
      } else {
        const [questions, papers] = await Promise.all([
          ctx.api.examQuestions({ ...filters, limit: 6 }, { signal: ctx.signal }),
          ctx.api.examPapers({ ...filters, limit: 6 }, { signal: ctx.signal }),
        ]);
        drawExplorer(overview, safeArray(questions.questions), safeArray(papers.papers), filters);
      }
      filterStatus.textContent = `${formatInteger(overview.summary?.questions || 0)} question records match this view.`;
    } catch (error) {
      if (isAbort(error)) return;
      summaryHost.replaceChildren();
      contentHost.replaceChildren(
        errorState(error, {
          retry: () => load({ initial: state.initial !== null }),
          context:
            "The explorer reads from the same local database as the dataset manager. No records are changed by this view.",
        }),
      );
      filterStatus.textContent = "The view could not be updated.";
    } finally {
      state.loading = false;
      const pending = state.pending;
      state.pending = false;
      apply.disabled = false;
      apply.textContent = "Apply filters";
      contentHost.removeAttribute("aria-busy");
      if (pending) load({ initial: state.initial !== null });
    }
  }

  function fillFilters(filters, values = {}) {
    const category = values.category ?? categorySelect.value();
    const subject = values.course ?? subjectSelect.value();
    const year = values.year ?? yearSelect.value();
    const type = values.type ?? typeSelect.value();
    const answer = values.answer_type ?? answerSelect.value();
    setOptions(
      categorySelect.node,
      safeArray(filters.categories).map((item) => ({
        value: item.category_id,
        label: `${humaniseCode(item.category_id)} (${item.courses})`,
      })),
      "All categories",
      category,
    );
    refreshSubjectOptions(category, subject);
    setOptions(
      yearSelect.node,
      safeArray(filters.years).map((valueItem) => ({ value: valueItem, label: String(valueItem) })),
      "All years",
      year,
    );
    setOptions(
      typeSelect.node,
      safeArray(filters.question_types).map((valueItem) => ({
        value: valueItem,
        label: humaniseCode(valueItem),
      })),
      "All formats",
      type,
    );
    setOptions(
      answerSelect.node,
      safeArray(filters.answer_types).map((valueItem) => ({
        value: valueItem,
        label: humaniseCode(valueItem),
      })),
      "All answer formats",
      answer,
    );
    if (!values.unit_id) {
      setOptions(unitSelect.node, [], "All units", "");
      setOptions(topicSelect.node, [], "All topics", "");
    }
  }

  function refreshSubjectOptions(category, selected = "") {
    const subjects = safeArray(state.filterData?.subjects).filter(
      (item) => !category || item.category_id === category,
    );
    setOptions(
      subjectSelect.node,
      subjects.map((item) => ({
        value: item.course_id,
        label: item.title || `${item.category_id} ${item.subject_code}`,
      })),
      "All subjects",
      selected,
    );
  }

  async function syncSyllabus(courseId, selectedUnit = "", selectedTopic = "") {
    state.syllabus = null;
    setOptions(unitSelect.node, [], "All units", "");
    setOptions(topicSelect.node, [], "All topics", "");
    if (!courseId) return;
    try {
      const payload = await ctx.api.examSyllabus(courseId, { signal: ctx.signal });
      state.syllabus = payload;
      const units = safeArray(payload.syllabi).flatMap((syllabus) =>
        safeArray(syllabus.units).map((unit) => ({
          value: unit.unit_id,
          label: `${unit.unit_label}${unit.title ? ` — ${unit.title}` : ""}`,
          unit,
        })),
      );
      setOptions(unitSelect.node, units, "All units", selectedUnit);
      fillTopics(selectedUnit, selectedTopic);
    } catch (error) {
      if (isAbort(error)) return;
      setOptions(unitSelect.node, [], "Syllabus outline unavailable", "");
    }
  }

  function fillTopics(unitId, selected = "") {
    const units = safeArray(state.syllabus?.syllabi).flatMap((syllabus) => safeArray(syllabus.units));
    const unit = units.find((item) => item.unit_id === unitId);
    setOptions(
      topicSelect.node,
      safeArray(unit?.topics).map((topic) => ({ value: topic, label: topic })),
      "All topics",
      selected,
    );
    updateFilterCount();
  }

  function renderSummary(summary, filters) {
    const active = Object.values(filters).filter(Boolean).length;
    return h(
      "section",
      { class: "results-summary", "aria-label": "Current examination result summary" },
      h(
        "div",
        { class: "results-summary-head" },
        h(
          "div",
          {},
          h("p", { class: "eyebrow", text: active ? "Filtered corpus" : "Complete local corpus" }),
          h("h2", { text: active ? "Current result set" : "All examination records" }),
        ),
        h(
          "p",
          { class: "results-summary-note" },
          active
            ? "The explorer, question list and paper table all use these filters."
            : "Use the filters to move from the whole corpus into a specific paper or question.",
        ),
      ),
      h(
        "div",
        { class: "summary-stats" },
        summaryStat("Questions", formatInteger(summary.questions), "in the current view"),
        summaryStat("Papers", formatInteger(summary.papers), "source groupings"),
        summaryStat("Answers", formatInteger(summary.answers), `${formatPercent(summary.questions ? summary.answers / summary.questions : 0, 0)} of questions`),
        summaryStat("Subjects", formatInteger(summary.subjects), `${formatInteger(summary.categories)} categories`),
      ),
    );
  }

  function drawExplorer(overview, questions, papers, filters) {
    const wrap = h("div", { class: "view-stack" });
    const categories = safeArray(overview.categories);
    const categoryMeta = new Map(
      safeArray(overview.filters.categories).map((item) => [item.category_id, item]),
    );
    wrap.append(
      panel(
        "Explore by examination",
        {
          eyebrow: "Start with a family",
          description: "Each category carries its own subjects, years and question patterns.",
          actions: [h("a", { class: "text-link", href: "#/datasets" }, "Inspect source inventory", icon("arrow"))],
        },
        categories.length
          ? h(
              "div",
              { class: "explore-category-grid" },
              ...categories.map((category) => {
                const share = overview.summary.questions
                  ? category.questions / overview.summary.questions
                  : 0;
                return h(
                  "a",
                  {
                    class: "explore-category",
                    href: `#/exams?category=${encodeURIComponent(category.category_id)}`,
                  },
                  h(
                    "span",
                    { class: "explore-category-top" },
                    h("span", { class: "category-monogram", "aria-hidden": "true" }, categoryInitials(category.category_id)),
                    icon("arrow"),
                  ),
                  h("strong", { text: categoryLabel(category.category_id) }),
                  h("span", { class: "explore-category-count", text: formatInteger(category.questions) }),
                  h("small", {
                    text: `${formatInteger(categoryMeta.get(category.category_id)?.courses)} subjects · ${formatPercent(share, 1)} of this view`,
                  }),
                  h(
                    "span",
                    { class: "explore-category-meter", "aria-hidden": "true" },
                    h("span", { style: { width: `${Math.max(share * 100, 4)}%` } }),
                  ),
                );
              }),
            )
          : emptyState({
              title: "No categories match",
              body: "Reset the category filter or import more local examination data.",
              iconName: "search",
            }),
      ),
    );

    wrap.append(
      h(
        "div",
        { class: "chart-grid chart-grid-balanced" },
        panel(
          "Question volume by year",
          { eyebrow: "Observed records", description: "Year comes from the stored source identifier." },
          safeArray(overview.years).length
            ? lineChart(
                overview.years.map((item) => ({ label: String(item.year), value: item.questions })),
                {
                  title: "Questions by examination year",
                  description: "Observed question records in the current view.",
                  xLabel: "Year",
                  yLabel: "Questions",
                  valueFormat: (value) => formatInteger(value),
                },
              )
            : h("p", { class: "muted", text: "No examination years are available in this view." }),
        ),
        panel(
          "Question formats",
          { eyebrow: "Stored schema", description: "Original type labels are normalised for display." },
          safeArray(overview.question_types).length
            ? barChart(
                overview.question_types.map((item) => ({
                  label: humaniseCode(item.label),
                  value: item.questions,
                })),
                {
                  title: "Questions by format",
                  description: "Unknown formats are not guessed.",
                  limit: 8,
                  valueFormat: (value) => formatInteger(value),
                },
              )
            : h("p", { class: "muted", text: "No question formats are available in this view." }),
        ),
      ),
    );

    wrap.append(
      panel(
        "Recent source papers",
        {
          eyebrow: "Paper trail",
          description: "A small sample of stored paper groupings.",
          actions: [h("a", { class: "text-link", href: "#/exams/papers" }, "All papers", icon("arrow"))],
        },
        papers.length
          ? dataTable({
              caption: "Recent source papers in this view",
              columns: [
                { label: "Year", key: "exam_year", className: "numeric" },
                { label: "Subject", key: "subject_code" },
                { label: "Paper", key: "paper_label" },
                { label: "Questions", key: "questions", className: "numeric" },
                { label: "Answers", key: "answers", className: "numeric" },
              ],
              rows: papers,
              empty: "No papers match these filters.",
            })
          : emptyState({
              title: "No source papers",
              body: "Adjust the filters to bring paper groupings into view.",
              iconName: "analysis",
            }),
      ),
    );

    if (questions.length) {
      wrap.append(
        panel(
          "Recently matched questions",
          {
            eyebrow: "A first look",
            description: "The question bank provides the full reading experience, options and answers.",
            actions: [h("a", { class: "text-link", href: "#/exams/questions" }, "Open question bank", icon("arrow"))],
          },
          h("div", { class: "question-preview-list" }, ...questions.map((item) => previewQuestion(item))),
        ),
      );
    }
    contentHost.replaceChildren(wrap);
  }

  function previewQuestion(item) {
    const query = [];
    if (item.course_id) query.push(`course=${encodeURIComponent(item.course_id)}`);
    if (item.exam_year) query.push(`year=${encodeURIComponent(item.exam_year)}`);
    const href = query.length ? `#/exams/questions?${query.join("&")}` : "#/exams/questions";
    return h(
      "a",
      { class: "question-preview", href },
      h(
        "span",
        { class: "question-preview-meta" },
        h("span", { text: item.exam_year ? String(item.exam_year) : "Year not recorded" }),
        h("span", { text: item.subject_code || "Subject not recorded" }),
      ),
      h("strong", { text: compact(item.text, 190) }),
      icon("arrow"),
    );
  }

  function drawQuestionBank(questions) {
    const wrap = h("div", { class: "view-stack" });
    wrap.append(
      h(
        "section",
        { class: "question-bank" },
        h(
          "div",
          { class: "question-bank-head" },
          h(
            "div",
            {},
            h("p", { class: "eyebrow", text: "Original records · not generated" }),
            h("h2", { text: "Questions matching the current view" }),
            h("p", {
              class: "panel-description",
              text: "Options, answers and explanations are shown only when the source record provides them. Expand a question to read the complete item.",
            }),
          ),
          h("span", { class: "question-bank-count", id: "question-count", text: `Showing ${formatInteger(questions.length)} of ${formatInteger(state.questionTotal)}` }),
        ),
        questions.length
          ? h(
              "div",
              { class: "question-list" },
              ...questions.map((item) => questionCard(item)),
            )
          : emptyState({
              title: "No questions match",
              body: "Try a broader search, clear one of the filters, or reset the corpus view.",
              actions: [h("button", { class: "button button-primary", type: "button", onClick: () => reset.click() }, "Reset filters")],
              iconName: "search",
            }),
      ),
    );
    if (questions.length) {
      const moreHost = h("div", { class: "load-more" });
      wrap.append(moreHost);
      drawLoadMore(moreHost);
    }
    contentHost.replaceChildren(wrap);
  }

  function drawLoadMore(host) {
    host.replaceChildren();
    if (state.questions.length >= state.questionTotal) {
      host.append(h("p", { class: "list-end", text: `${formatInteger(state.questionTotal)} question records shown.` }));
      return;
    }
    const button = h(
      "button",
      {
        class: "button button-ghost",
        type: "button",
        onClick: async () => {
          if (state.loadingMore) return;
          state.loadingMore = true;
          button.disabled = true;
          button.textContent = "Loading…";
          try {
            const payload = await ctx.api.examQuestions(
              { ...state.filters, limit: PAGE_SIZE, offset: state.questions.length },
              { signal: ctx.signal },
            );
            const newRows = safeArray(payload.questions);
            state.questions.push(...newRows);
            const list = viewRootQuestionList();
            for (const item of newRows) list?.append(questionCard(item));
            state.questionTotal = Number(payload.total || state.questionTotal);
            updateQuestionCount();
            drawLoadMore(host);
          } catch (error) {
            if (isAbort(error)) return;
            host.replaceChildren(
              errorState(error, {
                retry: () => drawLoadMore(host),
                context: "The already loaded questions remain available.",
              }),
            );
          } finally {
            state.loadingMore = false;
            button.disabled = false;
          }
        },
      },
      icon("refresh"),
      `Load ${formatInteger(Math.min(PAGE_SIZE, state.questionTotal - state.questions.length))} more`,
    );
    host.append(button);
    host.append(
      h("p", {
        class: "list-end",
        text: `Showing ${formatInteger(state.questions.length)} of ${formatInteger(state.questionTotal)} matching question records.`,
      }),
    );
  }

  function viewRootQuestionList() {
    return contentHost.querySelector(".question-list");
  }

  function updateQuestionCount() {
    const count = contentHost.querySelector("#question-count");
    if (count) {
      count.textContent = `Showing ${formatInteger(state.questions.length)} of ${formatInteger(state.questionTotal)}`;
    }
  }

  function drawPapers(papers, total) {
    const wrap = h("div", { class: "view-stack" });
    wrap.append(
      panel(
        "Stored source papers",
        {
          eyebrow: "Question paper grouping",
          description: "A paper groups questions that share a source prefix. Different years remain separate occurrences.",
        },
        papers.length
          ? dataTable({
              caption: `${formatInteger(total)} papers in this view`,
              columns: [
                { label: "Year", key: "exam_year", className: "numeric" },
                { label: "Category", render: (row) => humaniseCode(row.category_id) },
                { label: "Subject", key: "subject_code" },
                { label: "Paper", key: "paper_label" },
                { label: "Questions", key: "questions", className: "numeric" },
                { label: "Answers", key: "answers", className: "numeric" },
                {
                  label: "Answered",
                  render: (row) =>
                    row.questions
                      ? formatPercent(row.answers / row.questions, 0)
                      : "—",
                  className: "numeric",
                },
              ],
              rows: papers,
              empty: "No paper groupings match these filters.",
            })
          : emptyState({
              title: "No papers match",
              body: "Clear a filter or select a broader examination category.",
              iconName: "analysis",
            }),
      ),
      disclaimer(
        "Paper groups are derived from the source question identifiers. They are not a complete examination calendar and do not imply that every source paper was supplied.",
      ),
    );
    contentHost.replaceChildren(wrap);
  }
}

function questionCard(item) {
  const text = String(item.text || "").trim();
  const answer = item.answer;
  const options = safeArray(item.options);
  const correctKey = answer?.correct_option_key || null;
  const preview = compact(text, 300);
  const source = item.source_question_id || item.question_id || "source id not recorded";
  const detailLabel = answer ? "Read full question & answer" : "Read full question";
  return h(
    "article",
    { class: "question-card" },
    h(
      "header",
      { class: "question-card-head" },
      h(
        "div",
        { class: "question-tags" },
        h("span", { class: `question-number ${item.question_type === "mcq" ? "is-mcq" : ""}`.trim(), text: item.original_question_number ? `Q${item.original_question_number}` : "Question" }),
        item.exam_year
          ? h("span", { class: "question-tag", text: String(item.exam_year) })
          : h("span", { class: "question-tag is-muted", text: "Year not recorded" }),
        item.subject_code ? h("span", { class: "question-tag", text: item.subject_code }) : null,
        item.question_type
          ? h("span", { class: "question-tag", text: humaniseCode(item.question_type) })
          : null,
      ),
      h(
        "span",
        { class: "question-marks" },
        item.marks !== null && item.marks !== undefined && item.marks !== ""
          ? `${item.marks} ${Number(item.marks) === 1 ? "mark" : "marks"}`
          : "Marks not recorded",
      ),
    ),
    h(
      "div",
      { class: "question-card-body" },
      h("p", { class: "question-preview-text", text: preview || "The source record has no question text." }),
      h(
        "details",
        { class: "question-details" },
        h(
          "summary",
          {},
          h("span", { text: detailLabel }),
          h("span", { class: "summary-hint", text: `${options.length} ${options.length === 1 ? "option" : "options"}` }),
        ),
        h(
          "div",
          { class: "question-full" },
          h("p", { class: "question-text", text: text || "No question text was stored." }),
          options.length
            ? h(
                "ol",
                { class: "option-list", "aria-label": "Question options" },
                ...options.map((option) =>
                  h(
                    "li",
                    { class: `option-row ${option.key === correctKey ? "is-correct" : ""}`.trim() },
                    h("span", { class: "option-key", text: option.key || "—" }),
                    h("span", { class: "option-text", text: option.text || "Option text not recorded" }),
                    option.key === correctKey ? statusBadge("Answer key", "positive") : null,
                  ),
                ),
              )
            : h("p", { class: "question-gap", text: "No structured options were supplied for this record." }),
          h(
            "section",
            { class: "answer-block" },
            h(
              "div",
              { class: "answer-head" },
              h("h4", { text: "Stored answer" }),
              answer
                ? statusBadge(answer.answer_type ? humaniseCode(answer.answer_type) : "Answer present", "positive")
                : statusBadge("No answer", "caution"),
            ),
            answer
              ? h("p", { class: "answer-text", text: answer.answer_text || "The answer record contains no answer text." })
              : h("p", { class: "question-gap", text: "No answer record is paired with this question. The workspace does not fill the gap." }),
            answer?.correct_option_key
              ? h("p", { class: "answer-key-note" }, h("strong", { text: `Correct option ${answer.correct_option_key}` }), " — highlighted above when options are available.")
              : null,
          ),
        ),
      ),
    ),
    h(
      "footer",
      { class: "question-card-foot" },
      h("span", { class: "source-mark", "aria-hidden": "true" }, icon("course")),
      h("span", { class: "source-id", text: `Source ${source}` }),
      item.paper_label ? h("span", { class: "source-paper", text: item.paper_label }) : null,
      h("span", { class: "source-position", text: item.source_row ? `row ${item.source_row}` : "" }),
    ),
  );
}

function summaryStat(label, value, detail) {
  return h(
    "div",
    { class: "summary-stat" },
    h("span", { class: "summary-stat-label", text: label }),
    h("strong", { class: "summary-stat-value", text: value }),
    h("span", { class: "summary-stat-detail", text: detail }),
  );
}

function selectField(label, placeholder) {
  const node = h("select", { "aria-label": label });
  setOptions(node, [], placeholder, "");
  return {
    node,
    wrap: h("label", { class: "field" }, h("span", { text: label }), node),
    value: () => node.value,
  };
}

function setOptions(node, items, placeholder, selected) {
  node.replaceChildren(
    h("option", { value: "", text: placeholder }),
    ...items.map((item) =>
      h("option", { value: item.value, text: item.label, selected: item.value === selected }),
    ),
  );
  node.value = selected || "";
}

function categoryLabel(categoryId) {
  return (
    {
      gate: "GATE",
      "cbse-class-10": "CBSE Class 10",
      "cbse-class-12": "CBSE Class 12",
      neet: "NEET",
      "jee-mains": "JEE Main",
      "jee-advanced": "JEE Advanced",
    }[categoryId] || humaniseCode(categoryId)
  );
}

function categoryInitials(categoryId) {
  return categoryLabel(categoryId)
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 3)
    .toUpperCase();
}

function clean(value) {
  return String(value || "").trim();
}

function compact(value, max) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
