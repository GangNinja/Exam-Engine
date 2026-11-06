import { h } from "../dom.js";
import { barChart, stackedBar } from "../charts.js";
import {
  coverageLabel,
  coverageTone,
  dataStateLabel,
  formatInteger,
  formatPercent,
  humaniseCode,
  safeArray,
} from "../ui-state.js";
import {
  dataTable,
  dateOrDash,
  disclaimer,
  emptyState,
  errorState,
  jobControl,
  linkButton,
  loadingPanel,
  metrics,
  pageHeader,
  panel,
  selectedSyllabus,
  sourceSummary,
  statusBadge,
} from "./shared.js";

export function renderOverview(ctx) {
  const course = ctx.course;
  const counts = course.counts;
  const analysis = course.analysis;
  const wrap = h("div", { class: "view-stack" });

  wrap.append(
    pageHeader({
      eyebrow: `${course.course_code} · ${course.institution_name}`,
      title: course.title,
      description:
        course.description ||
        "Course-scoped workspace for ingested syllabi, question papers and grounded practice.",
      actions: [
        linkButton("Manage course", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
          variant: "primary",
          iconName: "course",
        }),
        linkButton("Practice", `#/c/${encodeURIComponent(ctx.courseId)}/practice`, {
          iconName: "practice",
        }),
      ],
    }),
  );

  wrap.append(
    h(
      "div",
      { class: "state-strip" },
      h("span", { class: "state-label", text: "Workspace state" }),
      statusBadge(dataStateLabel(course.data_state), course.data_state === "graph_ready" ? "positive" : "caution"),
      h("span", { class: "state-divider", "aria-hidden": "true" }),
      h("span", {
        class: "state-detail",
        text: `${course.institution_name} · ${course.academic_year || "academic year not recorded"} · syllabus version ${course.syllabus_version || "not recorded"}`,
      }),
    ),
  );

  wrap.append(
    metrics([
      { label: "Documents", value: formatInteger(counts.documents) },
      { label: "Questions extracted", value: formatInteger(counts.questions) },
      { label: "Analysed", value: formatInteger(analysis.understanding.analyzed) },
      { label: "Graph concepts", value: formatInteger(analysis.graph.concepts) },
      { label: "Extraction checks", value: formatInteger(counts.needs_review) },
      { label: "Syllabus units", value: formatInteger(counts.syllabus_units) },
    ]),
  );

  wrap.append(renderNextSteps(ctx));
  wrap.append(renderSnapshots(ctx));

  wrap.append(
    panel(
      "Documents and provenance",
      {
        eyebrow: "Source material",
        description: "Each document belongs to this course only; licence and dataset type stay visible.",
        actions: [
          linkButton("All documents", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
            iconName: "arrow",
          }),
        ],
      },
      dataTable({
        caption: "Ingested documents for this course",
        columns: [
          { label: "Document", key: "title" },
          {
            label: "Kind",
            render: (row) => humaniseCode(row.kind),
          },
          { label: "Pages", key: "page_count", className: "numeric" },
          { label: "Questions", key: "question_count", className: "numeric" },
          {
            label: "Status",
            render: (row) =>
              statusBadge(
                humaniseCode(row.extraction_status),
                row.extraction_status === "ok" ? "positive" : "warning",
              ),
          },
          {
            label: "Provenance",
            render: (row) => h("span", { class: "muted", text: sourceSummary(row) }),
          },
        ],
        rows: safeArray(course.documents),
        empty: "No documents yet. Add a syllabus and a question paper to begin.",
      }),
    ),
  );

  wrap.append(
    panel(
      "Recent activity",
      { eyebrow: "Audit trail", description: "Ingestion and analysis events stored in this database." },
      safeArray(course.activity).length
        ? h(
            "ol",
            { class: "activity-list" },
            ...course.activity.map((event) =>
              h(
                "li",
                {},
                h("span", { class: "activity-dot", "aria-hidden": "true" }),
                h(
                  "span",
                  { class: "activity-copy" },
                  h("strong", { text: event.title }),
                  h("span", { text: `${humaniseCode(event.kind)} · ${event.detail}` }),
                ),
                h("time", { datetime: event.at || "", text: dateOrDash(event.at, true) }),
              ),
            ),
          )
        : h("p", { class: "muted", text: "No ingestion or analysis activity is stored yet." }),
      disclaimer(
        "This workspace is local and single-user. It enforces no accounts, so do not expose it beyond a trusted machine.",
      ),
    ),
  );

  return wrap;
}

function renderNextSteps(ctx) {
  const steps = safeArray(ctx.course.next_steps);
  return panel(
    "Next actions",
    {
      eyebrow: "Guided path",
      description: "Steps are derived from the actual ingestion and analysis state of this course.",
    },
    h(
      "ol",
      { class: "step-list" },
      ...steps.map((step) => {
        let action = null;
        if (step.action === "upload") {
          action = linkButton("Add document", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
            variant: "primary",
            iconName: "upload",
          });
        } else if (step.action === "job") {
          action = jobControl(ctx, step.kind, step.label);
        } else if (step.action === "navigate") {
          action = linkButton("Review", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
            iconName: "arrow",
          });
        }
        return h(
          "li",
          { class: step.optional ? "is-optional" : "" },
          h("span", { class: "step-index", "aria-hidden": "true" }),
          h(
            "div",
            { class: "step-copy" },
            h("strong", { text: step.label }),
            h("p", { text: step.detail }),
            step.optional ? h("span", { class: "badge badge-neutral", text: "Optional" }) : null,
          ),
          action ? h("div", { class: "step-action" }, action) : null,
        );
      }),
    ),
  );
}

function renderSnapshots(ctx) {
  const coverageHost = h("div", {}, loadingPanel("Computing coverage snapshot", 3));
  const analyticsHost = h("div", {}, loadingPanel("Reading question distribution", 3));

  const syllabusId = selectedSyllabus(ctx);
  if (!syllabusId) {
    coverageHost.replaceChildren(
      emptyState({
        title: "Coverage needs a syllabus",
        body: "No parsed syllabus is stored for this course, so coverage cannot be claimed. Add a syllabus document first.",
        actions: [
          linkButton("Add syllabus", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
            variant: "primary",
            iconName: "upload",
          }),
        ],
        iconName: "analysis",
      }),
    );
  } else {
    ctx.api
      .coverage(ctx.courseId, { syllabus_id: syllabusId, view: "summary", limit: 60 }, { signal: ctx.signal })
      .then((report) => {
        coverageHost.replaceChildren(renderCoverageSnapshot(ctx, report));
      })
      .catch((error) => {
        if (isAbort(error)) return;
        coverageHost.replaceChildren(
          errorState(error, {
            retry: () => ctx.reload(),
            context: "The rest of the overview is still usable. Coverage depends on question mappings and one syllabus version.",
          }),
        );
      });
  }

  if (!ctx.course.analysis.understanding.analyzed) {
    analyticsHost.replaceChildren(
      emptyState({
        title: "No analysed questions yet",
        body: "Question distribution appears after question understanding has run.",
        actions: [jobControl(ctx, "understanding", "Run question understanding")],
        iconName: "analysis",
      }),
    );
  } else {
    ctx.api
      .analytics(ctx.courseId, { view: "summary" }, { signal: ctx.signal })
      .then((report) => analyticsHost.replaceChildren(renderAnalyticsSnapshot(report)))
      .catch((error) => {
        if (isAbort(error)) return;
        analyticsHost.replaceChildren(
          errorState(error, {
            retry: () => ctx.reload(),
            context: "Analytics is read-only; a failure here does not affect stored questions.",
          }),
        );
      });
  }

  return h(
    "div",
    { class: "overview-grid" },
    panel(
      "Coverage snapshot",
      {
        eyebrow: "Coverage",
        description: "Only corpus-relative evidence; absence is never proof.",
        actions: [
          linkButton(
            "Open coverage",
            `#/c/${encodeURIComponent(ctx.courseId)}/analysis/coverage`,
            { iconName: "arrow" },
          ),
        ],
      },
      coverageHost,
    ),
    panel(
      "Question distribution",
      {
        eyebrow: "Exam periods",
        description: "Unit and question-type counts from analysed papers.",
        actions: [
          linkButton(
            "Open distribution",
            `#/c/${encodeURIComponent(ctx.courseId)}/analysis/distribution`,
            { iconName: "arrow" },
          ),
        ],
      },
      analyticsHost,
    ),
  );
}

export function renderCoverageSnapshot(ctx, report) {
  const summary = report.summary;
  const statuses = [
    { label: coverageLabel("covered"), value: summary.covered_concepts, tone: "positive" },
    { label: coverageLabel("low_coverage"), value: summary.low_coverage_concepts, tone: "caution" },
    { label: coverageLabel("unmapped"), value: summary.unmapped_concepts, tone: "negative" },
    { label: coverageLabel("uncertain"), value: summary.uncertain_concepts, tone: "warning" },
    {
      label: coverageLabel("insufficient_data"),
      value: summary.insufficient_data_concepts,
      tone: "neutral",
    },
  ];
  const attention = safeArray(report.concepts)
    .filter((concept) => ["unmapped", "low_coverage", "uncertain"].includes(concept.status))
    .slice(0, 4);
  return h(
    "div",
    { class: "snapshot" },
    metrics([
      { label: "Concept coverage", value: formatPercent(summary.concept_coverage_rate, 0) },
      { label: "Mapped questions", value: formatPercent(summary.mapped_question_rate, 0) },
      { label: "Analysed", value: formatPercent(summary.analysis_coverage_rate, 0) },
    ]),
    stackedBar(statuses, { title: "Coverage status across syllabus concepts", description: `${summary.total_concepts} concepts in the selected syllabus.` }),
    attention.length
      ? h(
          "div",
          { class: "attention-list" },
          h("h3", { text: "Concepts needing review" }),
          h(
            "ul",
            {},
            ...attention.map((concept) =>
              h(
                "li",
                {},
                h("span", { text: concept.label }),
                statusBadge(coverageLabel(concept.status), coverageTone(concept.status)),
              ),
            ),
          ),
        )
      : h("p", { class: "muted", text: "No concept is flagged low-coverage or unresolved in this corpus." }),
    h("p", { class: "muted", text: report.limitations?.[0] || "Coverage describes the available corpus only." }),
  );
}

export function renderAnalyticsSnapshot(report) {
  const units = safeArray(report.mapping?.units)
    .slice(0, 8)
    .map((unit) => ({ label: unit.unit_label, value: unit.question_count }));
  const types = safeArray(report.question_types?.primary_labels)
    .slice(0, 6)
    .map((row) => ({ label: humaniseCode(row.label), value: row.question_count }));
  return h(
    "div",
    { class: "snapshot" },
    metrics([
      {
        label: "Analysis coverage",
        value: formatPercent(report.data_quality?.analysis_coverage, 0),
      },
      { label: "Mapping coverage", value: formatPercent(report.data_quality?.mapping_coverage, 0) },
      { label: "Known sessions", value: formatInteger(report.data_quality?.known_sessions) },
    ]),
    units.length
      ? barChart(units, { title: "Questions per syllabus unit", description: "Counts include each ambiguous assignment; totals are shown in Analysis." })
      : h("p", { class: "muted", text: "No unit mappings are available for the chart." }),
    types.length
      ? barChart(types, { title: "Primary question types", description: "Wording-based estimates, not trained classifications." })
      : null,
  );
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
