import { h, icon } from "../dom.js";
import { formatDate, formatInteger, humaniseCode, safeArray } from "../ui-state.js";
import {
  dataTable,
  emptyState,
  errorState,
  linkButton,
  loadingPanel,
  pageHeader,
  panel,
  statusBadge,
} from "./shared.js";

const IMPORT_COMMAND = "python -m exam_engine exams import";

export function renderDatasets(ctx) {
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    pageHeader({
      eyebrow: "Resources · Data operations",
      title: "Dataset management",
      description:
        "Inventory, import audit and evidence-quality checks for the local exam datasets. Importing is an explicit command-line operation; this dashboard never mutates source files.",
      actions: [
        linkButton("Open explorer", "#/exams", { variant: "primary", iconName: "search" }),
        actionButton("Refresh", "refresh", () => ctx.reload()),
      ],
    }),
  );
  const host = h("div", { class: "view-stack" }, loadingPanel("Reading dataset inventory", 6));
  wrap.append(host);

  ctx.api
    .examsOverview({}, { signal: ctx.signal })
    .then((overview) => host.replaceChildren(renderDatasetBody(ctx, overview)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "Dataset management reads the same local database as the exam explorer.",
        }),
      );
    });
  return wrap;
}

function renderDatasetBody(ctx, overview) {
  const summary = overview.summary || {};
  const quality = overview.quality || {};
  const latest = overview.latest_import;
  const categories = safeArray(overview.categories);
  const subjects = safeArray(overview.subjects);
  const categoryMeta = new Map(
    safeArray(overview.filters?.categories).map((item) => [item.category_id, item]),
  );
  const body = h("div", { class: "view-stack" });
  const runTotals = latest?.totals || {};
  const failures = Number(runTotals.failed_files || 0);
  const runTone = latest ? (failures ? "caution" : latest.status === "ok" ? "positive" : "caution") : "neutral";

  body.append(
    h(
      "div",
      { class: `dataset-banner tone-${runTone}` },
      h(
        "div",
        { class: "dataset-banner-icon", "aria-hidden": "true" },
        icon(latest ? (failures ? "alert" : "check") : "upload"),
      ),
      h(
        "div",
        { class: "dataset-banner-copy" },
        h(
          "strong",
          { text: latest ? (failures ? "Latest import completed with issues" : "Local datasets are ready") : "No import audit has been recorded" },
        ),
        h("p", {
          text: latest
            ? `${formatInteger(runTotals.files)} files scanned · ${formatInteger(runTotals.skipped)} unchanged records · ${formatDuration(latest.duration_ms)}.`
            : "Run the import command below to populate the corpus before opening the explorer.",
        }),
      ),
      latest
        ? h(
            "span",
            { class: "dataset-banner-time" },
            h("span", { text: "Finished" }),
            h("strong", { text: formatDate(latest.finished_at, { includeTime: true }) }),
          )
        : null,
    ),
  );

  body.append(
    h(
      "div",
      { class: "kpi-grid kpi-grid-compact" },
      statCard("Source files", formatInteger(runTotals.files), "In the latest import run", "upload"),
      statCard("Question records", formatInteger(summary.questions), "Available to explore", "practice"),
      statCard("Paper groups", formatInteger(summary.papers), "Derived from source records", "analysis"),
      statCard("Syllabus units", formatInteger(summary.syllabus_units), "Across all parsed syllabi", "course"),
    ),
  );

  body.append(
    h(
      "div",
      { class: "dataset-grid" },
      panel(
        "Latest import",
        {
          eyebrow: "Audit trail",
          description: "A stored record of the last explicit ingestion run.",
          actions: [statusBadge(latest?.status || "not recorded", runTone)],
        },
        latest
          ? h(
              "div",
              { class: "view-stack" },
              h(
                "div",
                { class: "audit-meta" },
                h("code", { class: "run-code", text: latest.run_id }),
                h("span", {
                  text: `${formatDate(latest.started_at, { includeTime: true })} → ${formatDate(latest.finished_at, { includeTime: true })}`,
                }),
              ),
              h(
                "dl",
                { class: "audit-facts" },
                auditFact("Files", formatInteger(runTotals.files)),
                auditFact("Questions written", formatInteger(runTotals.questions)),
                auditFact("Answers written", formatInteger(runTotals.answers)),
                auditFact("Unchanged / skipped", formatInteger(runTotals.skipped)),
                auditFact("Failed files", formatInteger(runTotals.failed_files)),
                auditFact("Duration", formatDuration(latest.duration_ms)),
              ),
              safeArray(latest.errors).length
                ? h(
                    "div",
                    { class: "audit-alert" },
                    h("strong", { text: "Import errors" }),
                    h(
                      "ul",
                      { class: "compact-list" },
                      ...safeArray(latest.errors).map((error) => h("li", { text: error })),
                    ),
                  )
                : null,
              safeArray(latest.warnings).length
                ? h(
                    "div",
                    { class: "audit-warning" },
                    h("strong", { text: "Warnings" }),
                    h(
                      "ul",
                      { class: "compact-list" },
                      ...safeArray(latest.warnings).map((warning) => h("li", { text: warning })),
                    ),
                  )
                : null,
            )
          : emptyState({
              title: "No import has been recorded",
              body: "The local import command creates an auditable run and populates the exam tables.",
              iconName: "upload",
            }),
      ),
      panel(
        "Evidence quality",
        {
          eyebrow: "Do not fill gaps",
          description: "Missing fields stay missing. Review the source records before drawing conclusions.",
        },
        h(
          "div",
          { class: "quality-audit-list" },
          qualityAudit(
            "Unanswered questions",
            quality.unanswered_questions,
            summary.questions,
            "No paired answer record.",
            "caution",
          ),
          qualityAudit(
            "Questions without explicit options",
            quality.missing_options,
            summary.questions,
            "Option data is absent, not inferred.",
            "neutral",
          ),
          qualityAudit(
            "Unknown question format",
            quality.unknown_question_type,
            summary.questions,
            "The stored type is unknown.",
            "neutral",
          ),
          qualityAudit(
            "MCQ answers without a stated key",
            quality.missing_correct_option,
            summary.answers,
            "The answer exists but its option key is absent.",
            "caution",
          ),
          qualityAudit(
            "Questions without a source year",
            quality.missing_year,
            summary.questions,
            "Year parsing found no year in the source id.",
            "neutral",
          ),
        ),
      ),
    ),
  );

  body.append(
    panel(
      "Examination inventory",
      {
        eyebrow: "Categories",
        description: "The question corpus grouped by examination family.",
        actions: [linkButton("Explore all", "#/exams", { iconName: "arrow" })],
      },
      categories.length
        ? dataTable({
            caption: "Categories and their stored question counts",
            columns: [
              {
                label: "Category",
                header: true,
                render: (row) => h("strong", { text: humaniseCode(row.category_id) }),
              },
              {
                label: "Subjects",
                render: (row) => formatInteger(categoryMeta.get(row.category_id)?.courses),
                className: "numeric",
              },
              { label: "Questions", render: (row) => formatInteger(row.questions), className: "numeric" },
              {
                label: "Explore",
                render: (row) =>
                  h(
                    "a",
                    {
                      class: "table-action",
                      href: `#/exams?category=${encodeURIComponent(row.category_id)}`,
                    },
                    "Open",
                    icon("arrow"),
                  ),
              },
            ],
            rows: categories,
            empty: "No categories are imported.",
          })
        : emptyState({
            title: "No categories imported",
            body: "Run the import command below to populate the examination inventory.",
            iconName: "upload",
          }),
    ),
  );

  body.append(
    panel(
      "Subject inventory",
      {
        eyebrow: "Courses",
        description: "Every subject collection, with its available question count.",
        actions: [linkButton("Question bank", "#/exams/questions", { iconName: "arrow" })],
      },
      subjects.length
        ? dataTable({
            caption: "Subjects in the local corpus",
            columns: [
              {
                label: "Subject",
                header: true,
                render: (row) =>
                  h(
                    "a",
                    { class: "subject-table-link", href: `#/c/${encodeURIComponent(row.course_id)}/overview` },
                    h("strong", { text: row.title || row.subject_code }),
                    h("small", { text: row.subject_code }),
                  ),
              },
              { label: "Questions", render: (row) => formatInteger(row.questions), className: "numeric" },
              {
                label: "Workspace",
                render: (row) =>
                  h(
                    "a",
                    { class: "table-action", href: `#/c/${encodeURIComponent(row.course_id)}/overview` },
                    "Open",
                    icon("arrow"),
                  ),
              },
            ],
            rows: subjects,
            empty: "No subjects are imported.",
          })
        : emptyState({
            title: "No subjects imported",
            body: "Subject collections appear here after a successful local import.",
            iconName: "course",
          }),
    ),
  );

  body.append(
    panel(
      "Import procedure",
      {
        eyebrow: "Command line",
        description:
          "Importing is deliberately explicit. The command is idempotent for unchanged files and writes an audit run.",
      },
      h(
        "div",
        { class: "command-card" },
        h(
          "div",
          { class: "command-card-copy" },
          h("strong", { text: "Run the local importer" }),
          h("p", {
            text: "Open a terminal at the project root and run the command. Source files are read in place and are never modified.",
          }),
        ),
        h(
          "div",
          { class: "command-line" },
          h("code", { text: IMPORT_COMMAND }),
          h(
            "button",
            {
              class: "button button-ghost",
              type: "button",
              onClick: async () => {
                try {
                  await navigator.clipboard.writeText(IMPORT_COMMAND);
                  ctx.toast("Import command copied.", "positive");
                } catch {
                  ctx.toast("Clipboard access is unavailable in this browser.", "caution");
                }
              },
            },
            icon("course"),
            "Copy command",
          ),
        ),
      ),
      h(
        "p",
        { class: "muted" },
        "The latest audit is refreshed after a run and when this page is reopened. Dashboard metrics always reflect the database, never cached display values.",
      ),
    ),
  );

  if (latest) {
    body.append(
      panel(
        "Files in the latest import",
        {
          eyebrow: "File audit",
          description: "Per-file status recorded during the most recent import.",
        },
        dataTable({
          caption: `Files in ${latest.run_id}`,
          columns: [
            {
              label: "File",
              header: true,
              render: (row) => h("code", { class: "file-path", text: row.relative_path }),
            },
            { label: "Kind", render: (row) => humaniseCode(row.kind) },
            {
              label: "Status",
              render: (row) =>
                statusBadge(
                  row.status,
                  ["ingested", "replaced", "ok"].includes(row.status) ? "positive" : "warning",
                ),
            },
            { label: "Questions", render: (row) => formatInteger(row.imported_questions), className: "numeric" },
            { label: "Answers", render: (row) => formatInteger(row.imported_answers), className: "numeric" },
            {
              label: "Syllabus units",
              render: (row) => formatInteger(row.imported_syllabus_units),
              className: "numeric",
            },
            { label: "Skipped", render: (row) => formatInteger(row.skipped_count), className: "numeric" },
          ],
          rows: safeArray(latest.files),
          empty: "The latest run recorded no files.",
        }),
      ),
    );
  }

  return body;
}

function statCard(label, value, detail, iconName) {
  return h(
    "article",
    { class: "kpi-card kpi-card-compact" },
    h(
      "div",
      { class: "kpi-top" },
      h("span", { class: "kpi-icon", "aria-hidden": "true" }, icon(iconName)),
      h("span", { class: "kpi-label", text: label }),
    ),
    h("p", { class: "kpi-value", text: value }),
    h("p", { class: "kpi-detail", text: detail }),
  );
}

function qualityAudit(label, value, total, description, tone) {
  const count = Number(value || 0);
  const denominator = Number(total || 0);
  const share = denominator ? count / denominator : 0;
  return h(
    "div",
    { class: "quality-audit" },
    h(
      "div",
      { class: "quality-audit-head" },
      h("span", { class: `quality-dot quality-${tone}`, "aria-hidden": "true" }),
      h("strong", { text: label }),
      h("span", { class: "quality-audit-value", text: formatInteger(count) }),
    ),
    h("p", { text: description }),
    h(
      "div",
      { class: "mini-meter", "aria-hidden": "true" },
      h("span", { class: `mini-meter-${tone}`, style: { width: `${Math.max(share * 100, count ? 2 : 0)}%` } }),
    ),
    h("small", { text: denominator ? `${formatInteger(count)} of ${formatInteger(denominator)}` : "No denominator recorded" }),
  );
}

function auditFact(label, value) {
  return h("div", {}, h("dt", { text: label }), h("dd", { text: value }));
}

function actionButton(label, iconName, onClick) {
  return h(
    "button",
    { class: "button button-ghost", type: "button", onClick },
    icon(iconName),
    h("span", { text: label }),
  );
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return "duration not recorded";
  const value = Number(milliseconds);
  if (!Number.isFinite(value)) return "duration not recorded";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)} s`;
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
