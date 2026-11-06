import { h, icon } from "../dom.js";
import { barChart, lineChart } from "../charts.js";
import {
  formatDate,
  formatInteger,
  formatPercent,
  humaniseCode,
  safeArray,
} from "../ui-state.js";
import {
  emptyState,
  errorState,
  linkButton,
  loadingPanel,
  pageHeader,
  panel,
  statusBadge,
} from "./shared.js";

const CATEGORY_NAMES = {
  gate: "GATE",
  "cbse-class-10": "CBSE Class 10",
  "cbse-class-12": "CBSE Class 12",
  neet: "NEET",
  "jee-mains": "JEE Main",
  "jee-advanced": "JEE Advanced",
};

export function renderDashboard(ctx) {
  const wrap = h("div", { class: "view-stack dashboard-view" });
  wrap.append(
    pageHeader({
      eyebrow: "Intelligence overview",
      title: "A clearer view of your examination corpus.",
      description:
        "Questions, papers, syllabus structure and evidence quality across every locally imported subject. Every figure is read from the active database.",
      actions: [
        linkButton("Browse question bank", "#/exams/questions", {
          variant: "primary",
          iconName: "search",
        }),
        linkButton("Dataset health", "#/datasets", { iconName: "upload" }),
      ],
    }),
  );

  const host = h("div", { class: "view-stack" }, loadingPanel("Reading examination intelligence", 6));
  wrap.append(host);

  ctx.api
    .examsOverview({}, { signal: ctx.signal })
    .then((overview) => host.replaceChildren(renderDashboardBody(ctx, overview)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "The dashboard needs the local API. Courses and other workspaces remain available when it returns.",
        }),
      );
    });

  return wrap;
}

function renderDashboardBody(ctx, overview) {
  const summary = overview.summary || {};
  const quality = overview.quality || {};
  const latest = overview.latest_import;
  const categories = safeArray(overview.categories);
  const subjects = safeArray(overview.subjects);
  const years = safeArray(overview.years);
  const questionTypes = safeArray(overview.question_types);
  const filterCategories = new Map(
    safeArray(overview.filters?.categories).map((item) => [item.category_id, item]),
  );
  const answerCoverage = summary.questions ? (summary.answers || 0) / summary.questions : 0;
  const activeCourse =
    ctx.courses?.find((course) => course.course_id === ctx.activeCourseId) || ctx.courses?.[0] || null;
  const latestDate = latest?.finished_at ? formatDate(latest.finished_at, { includeTime: true }) : null;
  const body = h("div", { class: "view-stack" });

  body.append(
    h(
      "div",
      { class: "corpus-line" },
      h(
        "div",
        { class: "corpus-line-primary" },
        h("span", { class: "status-indicator", "aria-hidden": "true" }),
        h(
          "span",
          {},
          latest
            ? h("strong", { text: `Corpus synchronised ${latestDate}` })
            : h("strong", { text: "Local corpus ready" }),
          h("small", {
            text: latest
              ? `${formatInteger(latest.totals?.files)} source files in the latest import · ${formatInteger(latest.totals?.failed_files || 0)} failures`
              : "No import audit has been recorded yet.",
          }),
        ),
      ),
      h(
        "div",
        { class: "corpus-line-meta" },
        h("span", { text: `${formatInteger(summary.years)} exam years` }),
        h("span", { text: `${formatInteger(summary.syllabus_documents)} syllabus documents` }),
        h("a", { href: "#/datasets", class: "text-link" }, "View dataset audit"),
      ),
    ),
  );

  body.append(
    h(
      "div",
      { class: "kpi-grid" },
      kpiCard({
        label: "Question records",
        value: formatInteger(summary.questions),
        detail: summary.years
          ? `${summary.year_min ?? "—"}–${summary.year_max ?? "—"} · ${formatInteger(summary.papers)} source papers`
          : "No examination years recorded",
        iconName: "practice",
        tone: "green",
      }),
      kpiCard({
        label: "Subjects & categories",
        value: formatInteger(summary.subjects),
        detail: `${formatInteger(summary.categories)} examination categories`,
        iconName: "course",
        tone: "blue",
      }),
      kpiCard({
        label: "Answer records",
        value: formatInteger(summary.answers),
        detail: `${formatPercent(answerCoverage, 0)} of questions have a paired answer`,
        iconName: "check",
        tone: "amber",
        progress: answerCoverage,
      }),
      kpiCard({
        label: "Syllabus footprint",
        value: formatInteger(summary.syllabus_units),
        detail: `Across ${formatInteger(summary.syllabus_documents)} parsed syllabus documents`,
        iconName: "analysis",
        tone: "violet",
      }),
    ),
  );

  const mainColumn = h(
    "div",
    { class: "dashboard-main" },
    panel(
      "Examination landscape",
      {
        eyebrow: "Categories",
        description: "Question density by examination family. Open a category to narrow the explorer.",
        actions: [linkButton("Open explorer", "#/exams", { iconName: "arrow" })],
        className: "panel-dashboard-landscape",
      },
      categories.length
        ? h(
            "div",
            { class: "category-list" },
            ...categories.map((category) => {
              const meta = filterCategories.get(category.category_id);
              const share = summary.questions ? category.questions / summary.questions : 0;
              return h(
                "a",
                {
                  class: "category-row",
                  href: `#/exams?category=${encodeURIComponent(category.category_id)}`,
                  "aria-label": `Explore ${categoryLabel(category.category_id)}, ${formatInteger(category.questions)} questions`,
                },
                h(
                  "span",
                  { class: "category-monogram", "aria-hidden": "true" },
                  categoryInitials(category.category_id),
                ),
                h(
                  "span",
                  { class: "category-copy" },
                  h("strong", { text: categoryLabel(category.category_id) }),
                  h("small", {
                    text: `${formatInteger(meta?.courses)} ${meta?.courses === 1 ? "subject" : "subjects"} · ${formatPercent(share, 1)} of corpus`,
                  }),
                ),
                h(
                  "span",
                  { class: "category-meter", "aria-hidden": "true" },
                  h("span", { style: { width: `${Math.max(share * 100, 3)}%` } }),
                ),
                h(
                  "span",
                  { class: "category-value" },
                  h("strong", { text: formatInteger(category.questions) }),
                  h("small", { text: "questions" }),
                ),
                icon("arrow"),
              );
            }),
          )
        : emptyState({
            title: "No categories imported",
            body: "Run the local import command to populate examination categories.",
            iconName: "upload",
          }),
    ),
    panel(
      "Question volume across examination years",
      {
        eyebrow: "Corpus trend",
        description: "Questions are counted by the year encoded in the source record. This is not a forecast.",
      },
      years.length
        ? lineChart(
            years.map((item) => ({ label: String(item.year), value: item.questions })),
            {
              title: "Questions observed by examination year",
              description: `Observed question records from ${summary.year_min ?? "—"} to ${summary.year_max ?? "—"}.`,
              xLabel: "Examination year",
              yLabel: "Questions",
              valueFormat: (value) => formatInteger(value),
            },
          )
        : h("p", { class: "muted", text: "No examination years could be derived from source records." }),
    ),
  );

  mainColumn.append(
    h(
      "div",
      { class: "dashboard-lower-grid" },
      panel(
        "Subject focus",
        {
          eyebrow: "Largest subject collections",
          description: "Open a subject to move from the corpus view into its evidence workspace.",
          actions: [linkButton("All questions", "#/exams/questions", { iconName: "arrow" })],
        },
        subjects.length
          ? h(
              "ol",
              { class: "subject-list" },
              ...subjects.slice(0, 6).map((subject, index) => {
                const share = subject.questions / Math.max(subjects[0]?.questions || 1, 1);
                return h(
                  "li",
                  {},
                  h("span", { class: "subject-rank", text: String(index + 1).padStart(2, "0") }),
                  h(
                    "span",
                    { class: "subject-copy" },
                    h("strong", { text: subject.title || subject.subject_code }),
                    h("small", { text: subject.subject_code }),
                  ),
                  h(
                    "span",
                    { class: "subject-meter", "aria-hidden": "true" },
                    h("span", { style: { width: `${Math.max(share * 100, 4)}%` } }),
                  ),
                  h("span", { class: "subject-count", text: formatInteger(subject.questions) }),
                  h(
                    "a",
                    {
                      class: "row-link",
                      href: `#/c/${encodeURIComponent(subject.course_id)}/overview`,
                      "aria-label": `Open ${subject.title || subject.subject_code}`,
                    },
                    icon("arrow"),
                  ),
                );
              }),
            )
          : h("p", { class: "muted", text: "No subject collections are available." }),
      ),
      panel(
        "Question formats",
        { eyebrow: "Recorded schema", description: "Formats observed in the local question records." },
        questionTypes.length
          ? barChart(
              questionTypes.map((item) => ({
                label: humaniseCode(item.label),
                value: item.questions,
              })),
              {
                title: "Questions by recorded format",
                description: "Counts use the stored question type; unknown formats remain visible.",
                limit: 8,
                valueFormat: (value) => formatInteger(value),
              },
            )
          : h("p", { class: "muted", text: "No question formats are available." }),
      ),
    ),
  );

  const aside = h(
    "aside",
    { class: "dashboard-aside", "aria-label": "Corpus readiness and workspace shortcuts" },
    panel(
      "Corpus readiness",
      {
        eyebrow: "Evidence quality",
        description: "Gaps are reported, not filled. Quality findings come from the source records.",
        actions: [linkButton("Audit", "#/datasets", { iconName: "arrow" })],
      },
      h(
        "div",
        { class: "readiness-block" },
        h(
          "div",
          {
            class: "readiness-ring",
            style: { "--score": `${Math.max(0, Math.min(100, answerCoverage * 100))}%` },
            role: "img",
            "aria-label": `${formatPercent(answerCoverage, 0)} answer coverage`,
          },
          h("span", { text: formatPercent(answerCoverage, 0) }),
        ),
        h(
          "div",
          { class: "readiness-copy" },
          h("strong", { text: "Answer coverage" }),
          h("p", {
            text: `${formatInteger(summary.answers)} of ${formatInteger(summary.questions)} question records have a paired answer.`,
          }),
          h("span", { class: "readiness-note", text: "Remaining records are explicitly unanswered." }),
        ),
      ),
      h(
        "div",
        { class: "quality-list" },
        qualityRow("Questions without a paired answer", quality.unanswered_questions, "caution"),
        qualityRow("Questions without explicit options", quality.missing_options, "neutral"),
        qualityRow("Unknown question format", quality.unknown_question_type, "neutral"),
      ),
    ),
    panel(
      "Continue analysis",
      {
        eyebrow: activeCourse ? activeCourse.course_code : "Choose a subject",
        description: activeCourse
          ? `${activeCourse.title}. All links use the current subject context.`
          : "Select a subject from the header to open a course workspace.",
      },
      activeCourse
        ? h(
            "div",
            { class: "shortcut-list" },
            shortcut(
              "Paper analyzer",
              "Question distribution and paper structure",
              `#/c/${encodeURIComponent(activeCourse.course_id)}/analysis/distribution`,
              "analysis",
            ),
            shortcut(
              "Coverage gaps",
              "Concepts without corpus evidence",
              `#/c/${encodeURIComponent(activeCourse.course_id)}/analysis/coverage`,
              "check",
            ),
            shortcut(
              "Knowledge graph",
              "Stored concepts and relationships",
              `#/c/${encodeURIComponent(activeCourse.course_id)}/learning`,
              "learning",
            ),
            shortcut(
              "Practice studio",
              "Generate and review grounded questions",
              `#/c/${encodeURIComponent(activeCourse.course_id)}/practice`,
              "spark",
            ),
          )
        : emptyState({
            title: "No subject selected",
            body: "Import a subject or choose one in the header to open its intelligence workspace.",
            actions: [linkButton("Manage datasets", "#/datasets", { variant: "primary", iconName: "upload" })],
            iconName: "course",
          }),
    ),
    panel(
      "Latest import",
      { eyebrow: "Recent activity", description: "The most recent ingestion audit stored in this database." },
      latest
        ? h(
            "div",
            { class: "import-summary" },
            h(
              "div",
              { class: "import-status" },
              statusBadge(latest.status || "unknown", latest.status === "ok" ? "positive" : "caution"),
              h("span", { text: latestDate || "time not recorded" }),
            ),
            h("code", { class: "run-code", text: latest.run_id }),
            h(
              "dl",
              { class: "import-facts" },
              importFact("Files scanned", formatInteger(latest.totals?.files)),
              importFact("Unchanged records", formatInteger(latest.totals?.skipped)),
              importFact("Failed files", formatInteger(latest.totals?.failed_files)),
              importFact("Duration", formatDuration(latest.duration_ms)),
            ),
            h(
              "a",
              { class: "text-link", href: "#/datasets" },
              "Open full import history",
              icon("arrow"),
            ),
          )
        : emptyState({
            title: "No import audit yet",
            body: "The dataset manager explains how to run the local import.",
            actions: [linkButton("Open datasets", "#/datasets", { variant: "primary", iconName: "upload" })],
            iconName: "upload",
          }),
    ),
  );

  body.append(h("div", { class: "dashboard-grid" }, mainColumn, aside));
  return body;
}

function kpiCard({ label, value, detail, iconName, tone = "green", progress = null }) {
  return h(
    "article",
    { class: `kpi-card tone-${tone}` },
    h(
      "div",
      { class: "kpi-top" },
      h("span", { class: "kpi-icon", "aria-hidden": "true" }, icon(iconName)),
      h("span", { class: "kpi-label", text: label }),
    ),
    h("p", { class: "kpi-value", text: value }),
    h("p", { class: "kpi-detail", text: detail }),
    progress === null
      ? null
      : h(
          "div",
          { class: "kpi-progress", "aria-hidden": "true" },
          h("span", { style: { width: `${Math.max(0, Math.min(100, progress * 100))}%` } }),
        ),
  );
}

function categoryLabel(categoryId) {
  return CATEGORY_NAMES[categoryId] || humaniseCode(categoryId);
}

function categoryInitials(categoryId) {
  return categoryLabel(categoryId)
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 3)
    .toUpperCase();
}

function qualityRow(label, value, tone) {
  return h(
    "div",
    { class: "quality-row" },
    h("span", { class: `quality-dot quality-${tone}`, "aria-hidden": "true" }),
    h("span", { text: label }),
    h("strong", { text: formatInteger(value) }),
  );
}

function shortcut(label, detail, href, iconName) {
  return h(
    "a",
    { class: "shortcut", href },
    h("span", { class: "shortcut-icon", "aria-hidden": "true" }, icon(iconName)),
    h("span", { class: "shortcut-copy" }, h("strong", { text: label }), h("small", { text: detail })),
    icon("arrow"),
  );
}

function importFact(label, value) {
  return h("div", {}, h("dt", { text: label }), h("dd", { text: value }));
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return "—";
  const value = Number(milliseconds);
  if (!Number.isFinite(value)) return "—";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)} s`;
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
