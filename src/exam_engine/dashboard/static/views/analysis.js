import { h } from "../dom.js";
import { barChart, lineChart, stackedBar } from "../charts.js";
import {
  coverageLabel,
  coverageTone,
  formatInteger,
  formatNumber,
  formatPercent,
  humaniseCode,
  plural,
  safeArray,
  titleCase,
} from "../ui-state.js";
import {
  dataTable,
  dateOrDash,
  disclaimer,
  emptyState,
  errorState,
  illustration,
  jobControl,
  linkButton,
  loadingPanel,
  metrics,
  notice,
  pageHeader,
  panel,
  selectedSyllabus,
  statusBadge,
  syllabusSelect,
  tabs,
  truncationNotice,
} from "./shared.js";

const TABS = [
  { key: "coverage", label: "Coverage gaps" },
  { key: "distribution", label: "Paper analyzer" },
  { key: "exam-dna", label: "Exam DNA" },
  { key: "evolution", label: "Concept evolution" },
];

const TAB_META = {
  coverage: {
    title: "Coverage gap analysis",
    description:
      "See which syllabus concepts have corpus evidence, which are thinly represented, and which remain unmapped. Coverage is always relative to the selected syllabus and available papers.",
  },
  distribution: {
    title: "Question paper analyzer",
    description:
      "Read a paper's recorded structure: question formats, marks, unit distribution and historical comparisons. Every chart is computed from stored papers.",
  },
  "exam-dna": {
    title: "Exam DNA",
    description:
      "Trace recurring concepts, paper structure and topic emphasis across stored examination years. The view reports patterns; it does not predict a future paper.",
  },
  evolution: {
    title: "Concept evolution",
    description:
      "Compare how stored concepts and question emphasis change over recorded examination years, with review-only observations rather than automatic rewrites.",
  },
};

export function renderAnalysis(ctx, tab) {
  const active = TABS.some((item) => item.key === tab) ? tab : "coverage";
  const wrap = h("div", { class: "view-stack" });
  const meta = TAB_META[active];
  wrap.append(
    pageHeader({
      eyebrow: `${ctx.course.course_code} · intelligence workspace`,
      title: meta.title,
      description: meta.description,
      actions: [
        linkButton("Syllabus explorer", `#/c/${encodeURIComponent(ctx.courseId)}/syllabus`, {
          iconName: "course",
        }),
        linkButton("Question bank", `#/exams/questions?course=${encodeURIComponent(ctx.courseId)}`, {
          iconName: "search",
        }),
      ],
    }),
    tabs(ctx, TABS, active),
  );
  const syllabusControl = syllabusSelect(ctx);
  if (syllabusControl) wrap.append(h("div", { class: "toolbar" }, syllabusControl));
  const host = h("div", { class: "view-stack" });
  wrap.append(host);

  const renderers = {
    coverage: renderCoverage,
    distribution: renderDistribution,
    "exam-dna": renderExamDna,
    evolution: renderEvolution,
  };
  renderers[active](ctx, host);
  return wrap;
}

// -- coverage ---------------------------------------------------------------

function renderCoverage(ctx, host) {
  const syllabusId = selectedSyllabus(ctx);
  if (!syllabusId) {
    host.append(
      emptyState({
        title: "Coverage needs a parsed syllabus",
        body: "Add a syllabus document to this course before asking which concepts the available papers cover.",
        actions: [
          linkButton("Add syllabus", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
            variant: "primary",
            iconName: "upload",
          }),
        ],
        iconName: "analysis",
      }),
    );
    return;
  }
  host.append(loadingPanel("Computing coverage against the selected syllabus", 4));
  ctx.api
    .coverage(ctx.courseId, { syllabus_id: syllabusId, view: "summary", limit: 150 }, { signal: ctx.signal })
    .then((report) => host.replaceChildren(renderCoverageReport(ctx, report)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context:
            "Coverage needs a syllabus version and question-to-concept mappings. A mapping gap is not an out-of-syllabus claim.",
        }),
      );
    });
}

function renderCoverageReport(ctx, report) {
  const summary = report.summary;
  const wrap = h("div", { class: "view-stack" });
  const statusSegments = [
    { label: coverageLabel("covered"), value: summary.covered_concepts, tone: "positive" },
    { label: coverageLabel("low_coverage"), value: summary.low_coverage_concepts, tone: "caution" },
    { label: coverageLabel("unmapped"), value: summary.unmapped_concepts, tone: "negative" },
    { label: coverageLabel("uncertain"), value: summary.uncertain_concepts, tone: "warning" },
    { label: coverageLabel("insufficient_data"), value: summary.insufficient_data_concepts, tone: "neutral" },
  ];
  wrap.append(
    metrics([
      { label: "Concepts", value: formatInteger(summary.total_concepts) },
      { label: "Covered", value: formatInteger(summary.covered_concepts) },
      { label: "Unmapped", value: formatInteger(summary.unmapped_concepts) },
      { label: "Uncertain", value: formatInteger(summary.uncertain_concepts) },
      { label: "Concept coverage", value: formatPercent(summary.concept_coverage_rate, 0) },
      { label: "Mapped questions", value: formatPercent(summary.mapped_question_rate, 0) },
    ]),
  );
  wrap.append(
    panel(
      "Coverage summary",
      {
        eyebrow: "Coverage by unit",
        description: `Syllabus ${report.syllabus?.title || report.syllabus?.syllabus_id || "—"} · report status ${humaniseCode(report.status)}.`,
      },
      stackedBar(statusSegments, {
        title: "Concept coverage status",
        description: `${plural(summary.total_concepts, "syllabus concept")} evaluated against ${plural(summary.total_questions, "question")}.`,
      }),
      h(
        "div",
        { class: "two-col" },
        metrics([
          { label: "Analysis coverage", value: formatPercent(summary.analysis_coverage_rate, 1) },
          { label: "Mapped questions", value: formatInteger(summary.mapped_questions) },
          { label: "Known sessions", value: formatInteger(summary.known_sessions) },
        ]),
        metrics([
          { label: "Known marks", value: formatNumber(summary.total_known_marks, 1) },
          { label: "Mapped marks", value: formatNumber(summary.mapped_marks, 1) },
          { label: "Missing marks", value: formatInteger(summary.missing_marks) },
        ]),
      ),
      truncationNotice(report),
      warningsList(report.warnings),
    ),
  );

  wrap.append(
    panel(
      "Unit coverage",
      { eyebrow: "Where the gaps are", description: "Coverage rates are relative to this syllabus version and this corpus." },
      dataTable({
        caption: "Unit-level coverage",
        columns: [
          { label: "Unit", key: "unit_label" },
          { label: "Concepts", key: "concept_count", className: "numeric" },
          {
            label: "Covered",
            render: (row) => formatInteger(row.status_counts?.covered),
            className: "numeric",
          },
          {
            label: "Low",
            render: (row) => formatInteger(row.status_counts?.low_coverage),
            className: "numeric",
          },
          {
            label: "Unmapped",
            render: (row) => formatInteger(row.status_counts?.unmapped),
            className: "numeric",
          },
          {
            label: "Questions",
            render: (row) => formatInteger(row.distinct_question_count),
            className: "numeric",
          },
          {
            label: "Coverage",
            render: (row) => formatPercent(row.concept_coverage_rate, 0),
            className: "numeric",
          },
          {
            label: "Needs attention",
            render: (row) =>
              safeArray(row.needs_attention).length
                ? safeArray(row.needs_attention).join("; ")
                : h("span", { class: "muted", text: "—" }),
          },
        ],
        rows: safeArray(report.units),
        empty: "No unit-level findings are available.",
      }),
    ),
  );

  wrap.append(renderConceptFindings(report));
  wrap.append(renderUnmapped(report));

  wrap.append(
    panel(
      "Report limits",
      { eyebrow: "What this cannot establish" },
      h(
        "ul",
        { class: "limitations" },
        ...safeArray(report.limitations).map((item) => h("li", { text: item })),
        h("li", { text: "An unmapped concept is unresolved in this corpus; it was not proven absent from teaching or future exams." }),
      ),
    ),
  );
  return wrap;
}

function renderConceptFindings(report) {
  const concepts = safeArray(report.concepts);
  const tableHost = h("div");
  const filterRow = h("div", { class: "filter-row", role: "group", "aria-label": "Filter concepts by coverage status" });
  const statuses = ["all", "covered", "low_coverage", "unmapped", "uncertain", "insufficient_data"];
  let active = "all";

  function draw() {
    const rows = active === "all" ? concepts : concepts.filter((item) => item.status === active);
    tableHost.replaceChildren(
      dataTable({
        caption: "Concept-level coverage findings",
        columns: [
          {
            label: "Status",
            header: true,
            render: (row) => statusBadge(coverageLabel(row.status), coverageTone(row.status)),
          },
          { label: "Concept", key: "label" },
          { label: "Units", render: (row) => safeArray(row.unit_labels).join(", ") || "—" },
          { label: "Questions", render: (row) => formatInteger(row.distinct_question_count), className: "numeric" },
          { label: "Documents", render: (row) => formatInteger(row.document_count), className: "numeric" },
          { label: "Sessions", render: (row) => formatInteger(row.session_count), className: "numeric" },
          {
            label: "Confidence",
            render: (row) =>
              row.average_mapping_confidence === null
                ? "—"
                : formatPercent(row.average_mapping_confidence, 0),
            className: "numeric",
          },
          {
            label: "Evidence",
            render: (row) =>
              h(
                "details",
                { class: "inline-details" },
                h("summary", { text: `${safeArray(row.reasons).length} reason(s)` }),
                h(
                  "ul",
                  {},
                  ...safeArray(row.reasons).map((reason) => h("li", { text: reason })),
                  ...safeArray(row.supporting_questions).slice(0, 3).map((question) =>
                    h("li", {
                      text: `“${question.text_excerpt}” — ${question.document_title}${question.source_locator ? `, ${question.source_locator}` : ""}`,
                    }),
                  ),
                  ...safeArray(row.candidate_mappings).slice(0, 3).map((candidate) =>
                    h("li", { text: `Candidate: ${candidate.reason} (${candidate.unit_label || "no unit"})` }),
                  ),
                ),
              ),
          },
        ],
        rows,
        empty: "No concepts match this filter.",
      }),
    );
  }

  for (const status of statuses) {
    const count =
      status === "all" ? concepts.length : concepts.filter((item) => item.status === status).length;
    const button = h(
      "button",
      {
        type: "button",
        class: `filter-chip ${active === status ? "is-active" : ""}`.trim(),
        "aria-pressed": active === status ? "true" : "false",
      },
      `${status === "all" ? "All" : coverageLabel(status)} `,
      h("span", { class: "filter-count", text: String(count) }),
    );
    button.addEventListener("click", () => {
      active = status;
      filterRow.querySelectorAll(".filter-chip").forEach((chip) => {
        const selected = chip === button;
        chip.classList.toggle("is-active", selected);
        chip.setAttribute("aria-pressed", selected ? "true" : "false");
      });
      draw();
    });
    filterRow.append(button);
  }
  draw();
  return panel(
    "Concept findings",
    {
      eyebrow: "Evidence and reasons",
      description: "Each finding keeps its reasons, mapped questions and rejected candidates. Candidates never count as coverage.",
    },
    filterRow,
    tableHost,
  );
}

function renderUnmapped(report) {
  const rows = safeArray(report.unmapped_questions);
  if (!rows.length) {
    return panel(
      "Unmapped questions",
      { eyebrow: "Residual questions" },
      h("p", { class: "muted", text: "No selected question is unmapped in this corpus." }),
    );
  }
  return panel(
    "Unmapped questions",
    {
      eyebrow: "Residual questions",
      description: "Unmapped means unresolved here, not out of syllabus. The explanation states the reason.",
    },
    h(
      "div",
      { class: "disclosure-list" },
      ...rows.slice(0, 30).map((row) =>
        h(
          "details",
          { class: "disclosure" },
          h(
            "summary",
            {},
            h("strong", { text: row.question_number ? `Question ${row.question_number}` : row.question_id }),
            h("span", { class: "muted", text: `${row.document_title} · ${humaniseCode(row.reason)}` }),
            statusBadge(humaniseCode(row.analysis_status || "not analysed"), "warning"),
          ),
          h(
            "div",
            { class: "disclosure-body" },
            h("blockquote", { text: row.text_excerpt }),
            h("p", { text: row.explanation }),
            safeArray(row.existing_mappings).length
              ? dataTable({
                  caption: "Existing mapping candidates considered for this question",
                  columns: [
                    { label: "Unit", key: "unit_label" },
                    { label: "Topic", key: "topic" },
                    { label: "Reason", key: "reason" },
                    { label: "Score", render: (item) => formatNumber(item.score, 2), className: "numeric" },
                  ],
                  rows: safeArray(row.existing_mappings),
                })
              : null,
          ),
        ),
      ),
    ),
  );
}

// -- distribution -----------------------------------------------------------

function renderDistribution(ctx, host) {
  host.append(loadingPanel("Computing question-paper analytics", 4));
  ctx.api
    .analytics(ctx.courseId, { view: "summary" }, { signal: ctx.signal })
    .then((report) => host.replaceChildren(renderDistributionReport(ctx, report)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "Analytics is read-only and recomputed on demand; a failure here leaves stored questions untouched.",
        }),
      );
    });
}

function renderDistributionReport(ctx, report) {
  const wrap = h("div", { class: "view-stack" });
  const quality = report.data_quality || {};
  wrap.append(
    metrics([
      { label: "Questions", value: formatInteger(quality.questions) },
      { label: "Analysed", value: formatPercent(quality.analysis_coverage, 0) },
      { label: "Mapped", value: formatPercent(quality.mapping_coverage, 0) },
      { label: "Marks known", value: formatPercent(quality.marks_coverage, 0) },
      { label: "Papers", value: formatInteger(quality.question_papers) },
      { label: "Known sessions", value: formatInteger(quality.known_sessions) },
    ]),
  );
  if (quality.small_sample) {
    wrap.append(
      notice(
        "This corpus is below the configured small-sample threshold. Counts are real but too small for broad educational conclusions.",
        "caution",
        "Small sample",
      ),
    );
  }
  wrap.append(truncationNotice(report));

  const units = safeArray(report.mapping?.units);
  const types = safeArray(report.question_types?.primary_labels);
  const difficulty = safeArray(report.difficulty?.labels);
  const allocations = safeArray(report.marks?.common_allocations);

  wrap.append(
    h(
      "div",
      { class: "chart-grid" },
      panel(
        "Questions per unit",
        { eyebrow: "Evidence scope", description: "Ambiguous questions can appear in more than one bucket." },
        barChart(
          units.map((row) => ({ label: row.unit_label, value: row.question_count })),
          { title: "Questions per syllabus unit", description: `${plural(units.length, "unit")} observed.`, limit: 14 },
        ),
      ),
      panel(
        "Primary question types",
        { eyebrow: "Wording classifier", description: "Cue-based estimates, not a trained model." },
        barChart(
          types.map((row) => ({ label: humaniseCode(row.label), value: row.question_count })),
          { title: "Primary question types", limit: 12 },
        ),
      ),
      panel(
        "Estimated difficulty",
        { eyebrow: "Provisional", description: "Uncalibrated estimates; never measured student difficulty." },
        barChart(
          difficulty.map((row) => ({ label: humaniseCode(row.label), value: row.question_count })),
          { title: "Estimated difficulty labels", limit: 8 },
        ),
      ),
      panel(
        "Marks allocations",
        { eyebrow: "Stated marks only", description: "Missing marks remain missing; no totals are inferred." },
        barChart(
          allocations.map((row) => ({ label: `${formatNumber(row.marks, 1)} marks`, value: row.question_count })),
          { title: "Most common stated marks", limit: 10 },
        ),
      ),
    ),
  );

  wrap.append(
    panel(
      "Observed periods and trends",
      { eyebrow: "Evidence scope", description: "Only explicitly recorded exam periods are used; upload time never substitutes." },
      report.trends?.available
        ? h(
            "div",
            { class: "view-stack" },
            lineChart(
              safeArray(report.trends.periods).map((period) => ({
                label: period.period,
                value: period.question_count,
              })),
              {
                title: "Questions per observed period",
                description: "Missing periods are not interpolated.",
                valueFormat: (value) => formatInteger(value),
              },
            ),
            dataTable({
              caption: "Observed period summaries",
              columns: [
                { label: "Period", key: "period" },
                { label: "Questions", key: "question_count", className: "numeric" },
                { label: "Documents", key: "document_count", className: "numeric" },
                { label: "Known marks", render: (row) => formatNumber(row.total_marks, 1), className: "numeric" },
                {
                  label: "Reliability",
                  render: (row) => statusBadge(row.low_sample ? "Low sample" : "Recorded", row.low_sample ? "caution" : "positive"),
                },
              ],
              rows: safeArray(report.trends.periods),
            }),
            safeArray(report.trends.changes).length
              ? dataTable({
                  caption: "Descriptive changes between adjacent periods",
                  columns: [
                    { label: "Metric", key: "metric" },
                    { label: "From", key: "previous_period" },
                    { label: "To", key: "current_period" },
                    {
                      label: "Change",
                      render: (row) => `${formatNumber(row.delta, 2)}`,
                      className: "numeric",
                    },
                    {
                      label: "Reliable",
                      render: (row) => statusBadge(row.reliable ? "Yes" : "Low sample", row.reliable ? "positive" : "caution"),
                    },
                  ],
                  rows: report.trends.changes,
                })
              : h("p", { class: "muted", text: "No adjacent-period change cleared the reliability thresholds." }),
          )
        : notice(
            report.trends?.reason || "Trends need at least two explicitly dated periods.",
            "neutral",
            "Trend unavailable",
          ),
    ),
  );

  wrap.append(
    panel(
      "Topic priority",
      {
        eyebrow: "Dataset-relative ranking",
        description: "A descriptive ranking of this corpus, not a prediction of future examination importance.",
      },
      safeArray(report.topic_priority?.topics).length
        ? dataTable({
            caption: "Topic priority ranking",
            columns: [
              { label: "#", key: "rank", className: "numeric" },
              { label: "Topic", key: "topic" },
              { label: "Questions", key: "question_count", className: "numeric" },
              { label: "Papers", key: "paper_count", className: "numeric" },
              { label: "Score", render: (row) => formatNumber(row.score, 3), className: "numeric" },
              {
                label: "What contributed",
                render: (row) => safeArray(row.explanation).join(" ") || "—",
              },
            ],
            rows: safeArray(report.topic_priority.topics).slice(0, 25),
          })
        : h("p", { class: "muted", text: "No topic priority can be computed from the selected questions." }),
    ),
  );

  const repetition = report.repetition || {};
  wrap.append(
    panel(
      "Repetition and similarity",
      {
        eyebrow: "Review signal",
        description: "Only exact wording proves duplication; near, semantic and concept-related groups stay review candidates.",
      },
      metrics([
        { label: "Exact groups", value: formatInteger(repetition.exact_group_count) },
        { label: "Exact questions", value: formatInteger(repetition.exact_questions) },
        { label: "Near pairs", value: formatInteger(repetition.near_pairs) },
        { label: "Semantic pairs", value: formatInteger(repetition.semantic_pairs) },
      ]),
      safeArray(repetition.exact_groups).length
        ? dataTable({
            caption: "Exact repeated question groups",
            columns: [
              {
                label: "Appearances",
                render: (row) => formatInteger(row.question_count),
                className: "numeric",
              },
              { label: "Documents", render: (row) => formatInteger(row.document_count), className: "numeric" },
              { label: "Across sessions", render: (row) => (row.across_sessions ? "Yes" : "No") },
              {
                label: "Excerpt",
                render: (row) => row.appearances?.[0]?.text || "—",
              },
            ],
            rows: safeArray(repetition.exact_groups),
          })
        : h("p", { class: "muted", text: "No exact repeated question group was found in this corpus." }),
      safeArray(repetition.document_frequency).length
        ? h(
            "details",
            { class: "disclosure" },
            h("summary", {}, h("strong", { text: "Document frequency detail" })),
            h(
              "div",
              { class: "disclosure-body" },
              dataTable({
                caption: "Question counts by document",
                columns: [
                  { label: "Document", key: "title" },
                  { label: "Period", key: "exam_period" },
                  { label: "Questions", key: "question_count", className: "numeric" },
                  { label: "Known marks", render: (row) => formatNumber(row.total_marks, 1), className: "numeric" },
                  { label: "Missing marks", key: "missing_marks", className: "numeric" },
                ],
                rows: safeArray(repetition.document_frequency),
              }),
            ),
          )
        : null,
    ),
  );

  wrap.append(
    panel(
      "Report limits",
      { eyebrow: "Read before interpreting" },
      h(
        "ul",
        { class: "limitations" },
        ...safeArray(report.warnings).map((warning) => h("li", { text: `${warning.code}: ${warning.message}` })),
        h("li", { text: "Question-type, Bloom and difficulty labels are estimates from wording-based cue rules." }),
        h("li", { text: "Unit and topic counts can overlap when one question maps ambiguously; denominators are shown above." }),
        h("li", { text: "Topic priority is descriptive and dataset-relative; it is never a forecast." }),
      ),
      disclaimer("No labelled analytics benchmark is bundled. These are software outputs, not validated educational measurements."),
    ),
  );
  return wrap;
}

// -- Exam DNA ---------------------------------------------------------------

function renderExamDna(ctx, host) {
  const syllabusId = selectedSyllabus(ctx);
  if (!syllabusId) {
    host.append(
      emptyState({
        title: "Exam DNA needs a syllabus version",
        body: "Historical patterns are scoped to one syllabus so unit evidence from another version cannot leak in.",
        actions: [linkButton("Add syllabus", `#/c/${encodeURIComponent(ctx.courseId)}/course`, { variant: "primary", iconName: "upload" })],
        iconName: "analysis",
      }),
    );
    return;
  }
  host.append(loadingPanel("Profiling historical papers", 4));
  ctx.api
    .examDna(ctx.courseId, { syllabus_id: syllabusId, view: "summary" }, { signal: ctx.signal })
    .then((profile) => host.replaceChildren(renderExamDnaReport(profile)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "Exam DNA reads stored papers and question classifications; it never fetches or predicts a paper.",
        }),
      );
    });
}

function renderExamDnaReport(profile) {
  const summary = profile.summary || {};
  const distributions = profile.distributions || {};
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    metrics([
      { label: "Eligible papers", value: formatInteger(summary.eligible_papers) },
      { label: "Excluded papers", value: formatInteger(summary.excluded_papers) },
      { label: "Questions", value: formatInteger(summary.questions) },
      { label: "Known sessions", value: formatInteger(summary.known_sessions) },
      { label: "Patterns", value: formatInteger(summary.pattern_count) },
      { label: "Recurring", value: formatInteger(summary.recurring_pattern_count) },
    ]),
  );
  wrap.append(
    notice(
      `Profile status: ${humaniseCode(profile.status)}. Recurrence is historical: ${summary.recurring_pattern_count} pattern(s) clear both the paper minimum and the recurrence threshold.`,
      profile.status === "ok" ? "neutral" : "caution",
    ),
  );
  wrap.append(truncationNotice(profile));
  wrap.append(
    h(
      "div",
      { class: "chart-grid" },
      panel(
        "Question type mix",
        { eyebrow: "Historical distribution", description: "Counts across eligible papers only." },
        barChart(distributionRows(distributions.question_types), { title: "Question types across eligible papers", limit: 12 }),
      ),
      panel(
        "Bloom mix",
        { eyebrow: "Estimated", description: "Estimated Bloom candidates, not examiner-validated levels." },
        barChart(distributionRows(distributions.bloom_levels), { title: "Bloom levels across eligible papers", limit: 8 }),
      ),
      panel(
        "Unit emphasis",
        { eyebrow: "Marks and questions", description: "Only matches to the selected syllabus version are counted." },
        barChart(distributionRows(distributions.units), { title: "Questions per unit", limit: 14 }),
      ),
      panel(
        "Command verbs",
        { eyebrow: "Wording cues", description: "Observed verbs; ambiguous cues remain reported, not interpreted." },
        barChart(distributionRows(distributions.command_verbs), { title: "Command verbs", limit: 12 }),
      ),
    ),
  );

  wrap.append(
    panel(
      "Historical patterns",
      {
        eyebrow: "Paper denominators",
        description: "Recurrence rates are fractions of eligible papers, not probabilities.",
      },
      safeArray(profile.patterns).length
        ? dataTable({
            caption: "Observed examination patterns",
            columns: [
              { label: "Category", render: (row) => humaniseCode(row.category) },
              { label: "Pattern", key: "label" },
              {
                label: "Papers",
                render: (row) => `${row.papers_observed}/${row.eligible_papers}`,
                className: "numeric",
              },
              { label: "Rate", render: (row) => formatPercent(row.recurrence_rate, 0), className: "numeric" },
              { label: "Quality", render: (row) => humaniseCode(row.evidence_quality) },
              {
                label: "Recurring",
                render: (row) => statusBadge(row.is_recurring ? "Recurring" : "Observed", row.is_recurring ? "positive" : "neutral"),
              },
              {
                label: "Evidence",
                render: (row) =>
                  h(
                    "details",
                    { class: "inline-details" },
                    h("summary", { text: "Why" }),
                    h(
                      "p",
                      { text: row.description },
                    ),
                    h(
                      "ul",
                      {},
                      ...safeArray(row.evidence).slice(0, 4).map((item) => h("li", { text: item })),
                      ...safeArray(row.limitations).slice(0, 3).map((item) => h("li", { text: item })),
                    ),
                  ),
              },
            ],
            rows: safeArray(profile.patterns),
          })
        : h("p", { class: "muted", text: "No patterns were detected in the eligible papers." }),
    ),
  );

  wrap.append(
    panel(
      "Sessions",
      { eyebrow: "Observed series", description: "Sessions come only from explicit dates or session metadata." },
      safeArray(profile.sessions).length
        ? dataTable({
            caption: "Observed examination sessions",
            columns: [
              { label: "Session", key: "label" },
              { label: "Period", key: "exam_period" },
              { label: "Exam date", render: (row) => dateOrDash(row.exam_date) },
              { label: "Papers", render: (row) => formatInteger(row.document_ids?.length), className: "numeric" },
              { label: "Questions", key: "question_count", className: "numeric" },
              { label: "Known marks", render: (row) => formatNumber(row.known_marks, 1), className: "numeric" },
            ],
            rows: safeArray(profile.sessions),
          })
        : h("p", { class: "muted", text: "No paper carries an explicit session or examination date." }),
    ),
  );

  wrap.append(
    panel(
      "Repeated question groups",
      { eyebrow: "Exact wording is the only duplication proof" },
      safeArray(profile.question_groups).length
        ? dataTable({
            caption: "Question groups by similarity kind",
            columns: [
              { label: "Kind", render: (row) => humaniseCode(row.kind) },
              { label: "Questions", key: "question_count", className: "numeric" },
              { label: "Documents", key: "document_count", className: "numeric" },
              { label: "Score", render: (row) => (row.score === null ? "—" : formatNumber(row.score, 3)), className: "numeric" },
              { label: "Shared concepts", render: (row) => safeArray(row.shared_concepts).join(", ") || "—" },
              { label: "Review required", render: (row) => statusBadge(row.review_required ? "Yes" : "No", row.review_required ? "caution" : "positive") },
            ],
            rows: safeArray(profile.question_groups),
          })
        : h("p", { class: "muted", text: "No repeated or similar question group was found." }),
    ),
  );

  wrap.append(
    panel(
      "Coverage and limitations",
      { eyebrow: "Read before citing" },
      dataTable({
        caption: "Coverage summary attached to this Exam DNA profile",
        columns: [
          { label: "Covered", render: () => formatInteger(profile.coverage?.covered_concepts), className: "numeric" },
          { label: "Low", render: () => formatInteger(profile.coverage?.low_coverage_concepts), className: "numeric" },
          { label: "Unmapped", render: () => formatInteger(profile.coverage?.unmapped_concepts), className: "numeric" },
          { label: "Uncertain", render: () => formatInteger(profile.coverage?.uncertain_concepts), className: "numeric" },
          { label: "Concept coverage", render: () => formatPercent(profile.coverage?.concept_coverage_rate, 0), className: "numeric" },
        ],
        rows: [{}],
      }),
      h(
        "ul",
        { class: "limitations" },
        ...safeArray(profile.limitations).slice(0, 8).map((item) => h("li", { text: item })),
        ...safeArray(profile.warnings).slice(0, 6).map((item) => h("li", { text: `${item.code}: ${item.message}` })),
      ),
      disclaimer(
        "Exam DNA describes the papers in this database. It is not a probability model, a prediction of the next paper, or an official statement from any institution.",
      ),
    ),
  );
  return wrap;
}

function distributionRows(rows) {
  return safeArray(rows).map((row) => ({ label: row.label, value: row.question_count }));
}

// -- evolution --------------------------------------------------------------

function renderEvolution(ctx, host) {
  host.append(loadingPanel("Reading dated concept observations", 4));
  ctx.api
    .evolution(ctx.courseId, { view: "summary" }, { signal: ctx.signal })
    .then((report) => host.replaceChildren(renderEvolutionReport(report)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "Evolution needs documents with resolvable dates. Missing dates are not replaced with ingestion time.",
        }),
      );
    });
}

function renderEvolutionReport(report) {
  const summary = report.summary || {};
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    metrics([
      { label: "Documents", value: formatInteger(summary.documents) },
      { label: "Dated documents", value: formatInteger(summary.dated_documents) },
      { label: "Concepts", value: formatInteger(summary.concepts) },
      { label: "Observations", value: formatInteger(summary.observations) },
      { label: "Change events", value: formatInteger(summary.changes) },
      { label: "Review required", value: formatInteger(summary.review_required_changes) },
    ]),
  );
  const categorySegments = Object.entries(summary.changes_by_category || {}).map(([label, value]) => ({
    label: humaniseCode(label),
    value,
    tone: "accent",
  }));
  wrap.append(
    panel(
      "What changed in the analysed corpus",
      { eyebrow: "Change history", description: "Changes are review-only observations, never canonical renames or deletions." },
      categorySegments.length
        ? stackedBar(categorySegments, { title: "Change events by category", description: `${summary.changes} event(s) retained.` })
        : h("p", { class: "muted", text: "No change event was detected in this corpus." }),
    ),
  );

  wrap.append(
    panel(
      "Change events",
      { eyebrow: "Review signals", description: "Dates come from explicit document metadata or exam sessions, not upload time." },
      safeArray(report.changes).length
        ? dataTable({
            caption: "Concept change events",
            columns: [
              { label: "Category", render: (row) => humaniseCode(row.category) },
              { label: "Concept", render: (row) => safeArray(row.labels).join(", ") || "—" },
              {
                label: "Earlier",
                render: (row) =>
                  row.earlier
                    ? `${row.earlier.document_title}${row.earlier.date ? ` · ${dateOrDash(row.earlier.date)}` : ""}`
                    : "—",
              },
              {
                label: "Later",
                render: (row) =>
                  row.later
                    ? `${row.later.document_title}${row.later.date ? ` · ${dateOrDash(row.later.date)}` : ""}`
                    : "—",
              },
              {
                label: "Confidence",
                render: (row) => formatPercent(row.confidence, 0),
                className: "numeric",
              },
              {
                label: "Review",
                render: (row) =>
                  statusBadge(row.review_required ? "Review" : "Observed", row.review_required ? "caution" : "positive"),
              },
              {
                label: "Explanation",
                render: (row) =>
                  h(
                    "details",
                    { class: "inline-details" },
                    h("summary", { text: "Read" }),
                    h("p", { text: row.explanation }),
                    h(
                      "ul",
                      {},
                      ...safeArray(row.evidence).slice(0, 4).map((item) => h("li", { text: item })),
                    ),
                  ),
              },
            ],
            rows: safeArray(report.changes),
          })
        : h("p", { class: "muted", text: "No concept change event is available." }),
    ),
  );

  wrap.append(
    panel(
      "Document date provenance",
      { eyebrow: "Chronology", description: "One source date per document, with its type and confidence." },
      dataTable({
        caption: "Date provenance for analysed documents",
        columns: [
          { label: "Document", key: "title" },
          { label: "Kind", render: (row) => humaniseCode(row.kind) },
          { label: "Date", render: (row) => dateOrDash(row.date) },
          { label: "Type", render: (row) => humaniseCode(row.date_type) },
          { label: "Source", key: "date_source" },
          { label: "Confident", render: (row) => statusBadge(row.date_confident ? "Yes" : "No", row.date_confident ? "positive" : "caution") },
          { label: "Observations", key: "observation_count", className: "numeric" },
        ],
        rows: safeArray(report.documents),
      }),
    ),
  );

  wrap.append(
    panel(
      "Concept timelines",
      { eyebrow: "Observed, not canonical" },
      safeArray(report.timelines).length
        ? h(
            "div",
            { class: "disclosure-list" },
            ...safeArray(report.timelines).slice(0, 30).map((timeline) =>
              h(
                "details",
                { class: "disclosure" },
                h(
                  "summary",
                  {},
                  h("strong", { text: timeline.canonical_label }),
                  h("span", { class: "muted", text: `${timeline.observation_count} observations · ${timeline.dated_observation_count} dated` }),
                ),
                h(
                  "div",
                  { class: "disclosure-body" },
                  h("p", { class: "muted", text: `First observed ${dateOrDash(timeline.first_observed)} · last observed ${dateOrDash(timeline.last_observed)}` }),
                  dataTable({
                    caption: `Observations for ${timeline.canonical_label}`,
                    columns: [
                      { label: "Document", key: "document_title" },
                      { label: "Date", render: (row) => dateOrDash(row.date) },
                      { label: "Unit", key: "unit_label" },
                      { label: "Excerpt", key: "excerpt" },
                    ],
                    rows: safeArray(timeline.observations).slice(0, 20),
                  }),
                ),
              ),
            ),
          )
        : h("p", { class: "muted", text: "No concept timeline can be built from dated observations." }),
    ),
  );

  wrap.append(
    panel(
      "Limits",
      { eyebrow: "Read before citing" },
      h(
        "ul",
        { class: "limitations" },
        ...safeArray(report.limitations).slice(0, 8).map((item) => h("li", { text: item })),
        ...safeArray(report.warnings).slice(0, 6).map((item) => h("li", { text: `${item.code}: ${item.message}` })),
      ),
      disclaimer(
        "“First observed” and “no longer observed” describe this corpus only. A missing mention is not proof that a concept was removed or renamed.",
      ),
    ),
  );
  return wrap;
}

function warningsList(warnings) {
  const rows = safeArray(warnings);
  if (!rows.length) return null;
  return notice(
    rows
      .slice(0, 4)
      .map((warning) => `${humaniseCode(warning.code)}: ${warning.message}`)
      .join(" "),
    "caution",
    "Evidence notes",
  );
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
