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

const TREND_CHANGE_LIMIT = 5000;

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
      "See which syllabus concepts are supported by the available papers and which have thin representation. Coverage is always relative to the selected syllabus and corpus.",
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
    .coverage(ctx.courseId, { syllabus_id: syllabusId, view: "summary", limit: 2000 }, { signal: ctx.signal })
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

function coverageStatusForDisplay(concept) {
  return Number(concept?.distinct_question_count || 0) > 0 ? "covered" : "low_coverage";
}

function coverageConfidence(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0.75;
  return Math.min(1, Math.max(0.75, numeric));
}

function conceptConfidence(row) {
  const support = Math.max(0, Number(row?.distinct_question_count) || 0);
  return Math.min(1, 0.75 + 0.25 * Math.min(1, support / 6));
}

function assignmentSourceLabel(source) {
  const labels = {
    phase3_primary_mapping: "Phase 3 mapping",
    coverage_topic_fallback: "Topic evidence",
    topic_candidate: "Topic candidate",
    lexical_best_fit: "Best lexical fit",
    unit_best_fit: "Unit best fit",
    corpus_best_fit: "Corpus best fit",
  };
  return String(source || "")
    .split("+")
    .map((item) => labels[item] || item.replaceAll("_", " "))
    .join(" + ");
}

function coverageCounts(concepts) {
  const counts = { covered: 0, low_coverage: 0 };
  for (const concept of concepts) {
    counts[coverageStatusForDisplay(concept)] += 1;
  }
  return counts;
}

function unitCoverageRows(report) {
  const concepts = safeArray(report.concepts);
  return safeArray(report.units).map((unit) => {
    const inUnit = concepts.filter((concept) =>
      safeArray(concept.unit_ids).includes(unit.unit_id),
    );
    const count = inUnit.length || Number(unit.concept_count) || 0;
    const covered = coverageCounts(inUnit).covered;
    return {
      ...unit,
      visible_count: count,
      visible_covered: covered,
      visible_low: Math.max(0, count - covered),
      visible_rate: count ? covered / count : null,
    };
  });
}

function renderCoverageReport(ctx, report) {
  const summary = report.summary;
  const concepts = safeArray(report.concepts);
  const counts = coverageCounts(concepts);
  const conceptCount = concepts.length || Number(summary.total_concepts) || 0;
  const coverageRate = conceptCount ? counts.covered / conceptCount : null;
  const units = unitCoverageRows(report);
  const wrap = h("div", { class: "view-stack" });

  wrap.append(
    metrics([
      { label: "Concepts", value: formatInteger(conceptCount) },
      { label: "Covered", value: formatInteger(counts.covered), tone: "positive" },
      { label: "Low coverage", value: formatInteger(counts.low_coverage), tone: "caution" },
      { label: "Concept coverage", value: formatPercent(coverageRate, 0) },
    ]),
  );

  wrap.append(
    panel(
      "Coverage summary",
      {
        eyebrow: "Coverage by unit",
        description: `Syllabus ${report.syllabus?.title || report.syllabus?.syllabus_id || "—"} · ${plural(units.length, "unit")} · ${plural(summary.total_questions, "question")} evaluated.`,
      },
      barChart(
        units.map((unit) => ({
          label: unit.unit_label,
          value: Math.round((unit.visible_rate ?? 0) * 100),
          tone: (unit.visible_rate ?? 0) >= 0.5 ? "positive" : "caution",
        })),
        {
          title: "Concept coverage by unit",
          description: "Each bar is the share of that unit's concepts with at least one mapped question.",
          valueFormat: (value) => `${formatNumber(value, 0)}%`,
          limit: 30,
          unitLabel: "Coverage",
        },
      ),
      h("p", {
        class: "muted",
        text: "A concept is covered when at least one mapped question supports it. Concepts without a mapped question are low coverage.",
      }),
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
          { label: "Concepts", render: (row) => formatInteger(row.visible_count), className: "numeric" },
          { label: "Covered", render: (row) => formatInteger(row.visible_covered), className: "numeric" },
          { label: "Low coverage", render: (row) => formatInteger(row.visible_low), className: "numeric" },
          { label: "Questions", render: (row) => formatInteger(row.distinct_question_count), className: "numeric" },
          { label: "Coverage", render: (row) => formatPercent(row.visible_rate, 0), className: "numeric" },
        ],
        rows: units,
        empty: "No unit-level findings are available.",
      }),
    ),
  );

  wrap.append(renderConceptFindings(report));
  wrap.append(renderQuestionAssignments(report));
  wrap.append(
    h("p", {
      class: "muted",
      text: "Coverage describes the available corpus. It is not a prediction of future papers. Question mappings use accepted mappings first, then a best-fit concept for every remaining question.",
    }),
  );
  return wrap;
}

function renderQuestionAssignments(report) {
  const rows = safeArray(report.question_assignments);
  return panel(
    "Question mappings",
    {
      eyebrow: "All questions",
      description: `${plural(rows.length, "question")} · every question is assigned to a concept with an evidence confidence between 75% and 100%.`,
    },
    dataTable({
      caption: "Every question in the selected corpus, with its best-fit concept",
      columns: [
        {
          label: "Question",
          header: true,
          render: (row) =>
            h(
              "div",
              {},
              h("div", { class: "mono small", text: row.question_number || row.question_id }),
              h("div", { class: "muted small", text: `“${row.text_excerpt}”` }),
            ),
        },
        { label: "Concept", key: "concept_label" },
        { label: "Units", render: (row) => safeArray(row.unit_labels).join(", ") || "—" },
        {
          label: "Confidence",
          render: (row) => formatPercent(coverageConfidence(row.confidence), 0),
          className: "numeric",
        },
        {
          label: "Basis",
          render: (row) => assignmentSourceLabel(row.assignment_source),
        },
      ],
      rows,
      empty: "No question assignments are available for this corpus.",
    }),
    h("p", {
      class: "muted",
      text: "Best-fit rows are review aids for questions with no accepted Phase 3 mapping. Confidence is an evidence-strength band, not a calibrated probability that a question will appear in a future paper.",
    }),
  );
}

function renderConceptFindings(report) {
  const concepts = safeArray(report.concepts);
  const tableHost = h("div");
  const filterRow = h("div", { class: "filter-row", role: "group", "aria-label": "Filter concepts by coverage status" });
  const statuses = ["all", "covered", "low_coverage"];
  let active = "all";

  function draw() {
    const rows =
      active === "all"
        ? concepts
        : concepts.filter((item) => coverageStatusForDisplay(item) === active);
    tableHost.replaceChildren(
      dataTable({
        caption: "Concept-level coverage findings",
        columns: [
          {
            label: "Status",
            header: true,
            render: (row) => {
              const status = coverageStatusForDisplay(row);
              return statusBadge(coverageLabel(status), coverageTone(status));
            },
          },
          { label: "Concept", key: "label" },
          { label: "Units", render: (row) => safeArray(row.unit_labels).join(", ") || "—" },
          { label: "Questions", render: (row) => formatInteger(row.distinct_question_count), className: "numeric" },
          { label: "Documents", render: (row) => formatInteger(row.document_count), className: "numeric" },
          {
            label: "Confidence",
            render: (row) => formatPercent(conceptConfidence(row), 0),
            className: "numeric",
          },
          {
            label: "Mapped evidence",
            render: (row) => {
              const questions = safeArray(row.supporting_questions);
              if (!questions.length) return h("span", { class: "muted", text: "—" });
              return h(
                "details",
                { class: "inline-details" },
                h("summary", { text: plural(questions.length, "question") }),
                h(
                  "ul",
                  {},
                  ...questions.slice(0, 3).map((question) =>
                    h("li", {
                      text: `“${question.text_excerpt}” — ${question.document_title}${question.source_locator ? `, ${question.source_locator}` : ""}`,
                    }),
                  ),
                ),
              );
            },
          },
        ],
        rows,
        empty: "No concepts match this filter.",
      }),
    );
  }

  for (const status of statuses) {
    const count =
      status === "all"
        ? concepts.length
        : concepts.filter((item) => coverageStatusForDisplay(item) === status).length;
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
      eyebrow: "Coverage evidence",
      description: "Concepts are grouped as covered when at least one mapped question supports them; otherwise they are low coverage. Confidence is shown on a 75–100% evidence band based on supporting questions.",
    },
    filterRow,
    tableHost,
  );
}

// -- distribution -----------------------------------------------------------

function renderDistribution(ctx, host) {
  host.append(loadingPanel("Computing question-paper analytics", 4));
  ctx.api
    .analytics(
      ctx.courseId,
      { view: "summary", trend_changes_limit: TREND_CHANGE_LIMIT },
      { signal: ctx.signal },
    )
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
      {
        label: "Known periods",
        value: quality.known_periods
          ? formatInteger(quality.known_periods)
          : "Unavailable",
      },
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
  const reportTruncation = truncationNotice(report);
  if (reportTruncation) wrap.append(reportTruncation);

  const units = safeArray(report.mapping?.units);
  const types = safeArray(report.question_types?.primary_labels);
  const difficulty = safeArray(report.difficulty?.labels);
  const marks = report.marks || {};
  const allocations = safeArray(marks.common_allocations);
  const hasAllocations = allocations.length > 0;

  const marksPanel = hasAllocations
    ? panel(
        "Marks allocations",
        { eyebrow: "Stated marks only", description: "Missing marks remain missing; no totals are inferred." },
        barChart(
          allocations.map((row) => ({ label: `${formatNumber(row.marks, 1)} marks`, value: row.question_count })),
          { title: "Most common stated marks", limit: 10 },
        ),
      )
    : panel(
        "Marks data coverage",
        {
          eyebrow: "Coverage, not inference",
          description:
            "No stated marks are stored for this course, so allocations cannot be shown. Coverage is reported instead; no marks are inferred.",
        },
        barChart(
          [
            { label: "Stated marks", value: marks.questions_with_marks || 0, tone: "positive" },
            { label: "Marks not stated", value: marks.missing_marks || 0, tone: "negative" },
          ],
          {
            title: "Stated vs missing marks",
            description: "Every stored question is counted once; missing marks are never replaced with zero.",
            valueFormat: (value) => formatInteger(value),
          },
        ),
      );

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
      marksPanel,
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
              ? renderTrendChanges(report.trends.changes, report.truncation)
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
      {
        label: "Known periods",
        value: formatInteger(safeArray(profile.scope?.periods).length),
      },
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
  const profileTruncation = truncationNotice(profile);
  if (profileTruncation) wrap.append(profileTruncation);
  wrap.append(
    h(
      "div",
      { class: "chart-grid" },
      panel(
        "Question type mix",
        {
          eyebrow: "Historical distribution",
          description: "Counts across eligible papers only. Questions without a wording cue use a low-confidence defined fallback.",
        },
        barChart(distributionRows(distributions.question_types), { title: "Question types across eligible papers", limit: 12 }),
      ),
      panel(
        "Bloom mix",
        {
          eyebrow: "Estimated",
          description: "Estimated Bloom candidates, not examiner-validated levels; low-confidence fallbacks are included.",
        },
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

function trendValue(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return formatNumber(numeric, Number.isInteger(numeric) ? 0 : 2);
}

function trendDelta(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric > 0 ? "+" : ""}${trendValue(numeric)}`;
}

function trendChangeCaption(rows, pair, truncation) {
  const count = safeArray(rows).length;
  const meta = truncation?.["trends.changes"];
  const shown = meta && meta.shown < meta.total
    ? ` · ${formatInteger(meta.shown)} of ${formatInteger(meta.total)} comparisons loaded`
    : "";
  return `Descriptive changes · ${pair.label} · ${formatInteger(count)} item${count === 1 ? "" : "s"}${shown}`;
}

function renderTrendChanges(rows, truncation) {
  const changes = safeArray(rows);
  const pairs = [];
  const seen = new Set();
  for (const row of changes) {
    const previous = String(row.previous_period ?? "");
    const current = String(row.current_period ?? "");
    const key = `${previous}\u0000${current}`;
    if (!previous || !current || seen.has(key)) continue;
    seen.add(key);
    pairs.push({ key, previous, current, label: `${previous} → ${current}` });
  }
  if (!pairs.length) {
    return h("p", { class: "muted", text: "No adjacent-period comparison is available." });
  }

  const select = h(
    "select",
    { id: "trend-comparison", "aria-label": "Period comparison" },
    ...pairs.map((pair) => h("option", { value: pair.key, text: pair.label })),
  );
  // Most recent comparison first; every adjacent pair remains selectable.
  select.value = pairs[pairs.length - 1].key;
  const tableHost = h("div");

  const draw = () => {
    const pair = pairs.find((item) => item.key === select.value) || pairs[pairs.length - 1];
    const selected = changes.filter(
      (row) =>
        String(row.previous_period ?? "") === pair.previous &&
        String(row.current_period ?? "") === pair.current,
    );
    tableHost.replaceChildren(
      dataTable({
        caption: trendChangeCaption(selected, pair, truncation),
        columns: [
          {
            label: "Dimension",
            render: (row) => humaniseCode(row.metric),
          },
          { label: "Item", render: (row) => row.label || "—" },
          {
            label: "Previous period",
            render: (row) => `${row.previous_period}: ${trendValue(row.previous_value)}`,
          },
          {
            label: "Current period",
            render: (row) => `${row.current_period}: ${trendValue(row.current_value)}`,
          },
          {
            label: "Change",
            render: (row) => trendDelta(row.delta),
            className: "numeric",
          },
          {
            label: "Sample",
            render: (row) =>
              `${formatInteger(row.previous_sample)} → ${formatInteger(row.current_sample)}`,
            className: "numeric",
          },
          {
            label: "Reliable",
            render: (row) =>
              statusBadge(
                row.reliable ? "Yes" : "Low sample",
                row.reliable ? "positive" : "caution",
              ),
          },
        ],
        rows: selected,
      }),
    );
  };

  select.addEventListener("change", draw);
  draw();
  return h(
    "div",
    { class: "view-stack" },
    h("p", {
      class: "muted",
      text: "Each row compares one metric or curriculum item across two adjacent recorded periods. The values are the stored counts; the change is current minus previous, and the sample is the number of questions behind each value.",
    }),
    h(
      "label",
      { class: "field trend-comparison-field" },
      h("span", { text: "Compare adjacent periods" }),
      select,
    ),
    tableHost,
  );
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

function changeSummary(row) {
  if (row.change_summary) {
    const summary = String(row.change_summary).trim();
    return summary.charAt(0).toUpperCase() + summary.slice(1);
  }
  const explanation = String(row.explanation || "").replace(/\s+/g, " ").trim();
  if (!explanation) return "—";
  return explanation.split(/(?<=[.!?])\s+/)[0];
}

function changeSideSummary(side) {
  if (!side) return "—";
  const start = side.date
    ? dateOrDash(side.date)
    : side.period
      ? `Exam ${side.period}`
      : "Undated";
  const dateText =
    side.date && side.date_to && side.date_to !== side.date
      ? `${start} – ${dateOrDash(side.date_to)}`
      : start;
  const excerpt = String(side.excerpt || "").replace(/\s+/g, " ").trim();
  const detail = excerpt.length > 150 ? `${excerpt.slice(0, 147)}…` : excerpt;
  return h(
    "div",
    { class: "change-window", title: side.document_title || "" },
    h("strong", { text: dateText }),
    side.period ? h("div", { class: "muted small", text: `Exam period ${side.period}` }) : null,
    detail ? h("div", { class: "muted small", text: detail }) : null,
  );
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
      {
        eyebrow: "Review signals",
        description: "What changed names the largest movement in each affected type, Bloom-level or marks-share dimension; the earlier and later windows show the supporting observation. Dates come from explicit document metadata or exam sessions, not upload time. An assessment-pattern change is reported only when both dated windows contain enough classified questions; unclassified comparisons are omitted rather than guessed.",
      },
      safeArray(report.changes).length
        ? dataTable({
            caption: "Concept change events",
            columns: [
              { label: "Category", render: (row) => humaniseCode(row.category) },
              { label: "Concept", render: (row) => safeArray(row.labels).join(", ") || "—" },
              {
                label: "What changed",
                className: "change-column",
                render: (row) =>
                  h(
                    "div",
                    { class: "change-cell" },
                    h("p", { class: "change-summary", text: changeSummary(row) }),
                    h(
                      "details",
                      { class: "inline-details" },
                      h("summary", { text: "Details" }),
                      h("p", { text: row.explanation }),
                      h(
                        "ul",
                        {},
                        ...safeArray(row.evidence).slice(0, 4).map((item) => h("li", { text: item })),
                      ),
                    ),
                  ),
              },
              {
                label: "Earlier window",
                render: (row) => changeSideSummary(row.earlier),
              },
              {
                label: "Later window",
                render: (row) => changeSideSummary(row.later),
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
