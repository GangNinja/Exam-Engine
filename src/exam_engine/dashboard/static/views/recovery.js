import { h } from "../dom.js";
import { formatInteger, humaniseCode } from "../ui-state.js";
import {
  dataTable,
  emptyState,
  errorState,
  loadingPanel,
  metrics,
  pageHeader,
  panel,
  statusBadge,
} from "./shared.js";

function toneForStatus(status) {
  if (status === "ready" || status === "released") return "positive";
  if (status === "official_verified") return "positive";
  if (status === "blocked" || status === "quarantined" || status === "conflicting" || status === "conflicting_evidence") return "negative";
  if (status === "matched_unverified" || status === "unverifiable_source" || status === "review_required" || status === "insufficient_evidence") return "caution";
  if (status === "matched_no_key" || status === "document_retrieved_no_match") return "warning";
  return "neutral";
}

function score(value) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(1);
}

export function renderRecovery(ctx) {
  const wrap = h("div", { class: "view-stack" });
  const host = h("div", { class: "view-stack" }, loadingPanel("Reading recovery evidence", 6));
  let latest = null;
  let selectedCourse = null;

  const runDry = async () => {
    host.replaceChildren(loadingPanel("Running a dry dataset audit", 6));
    try {
      latest = await ctx.api.recoveryRun(
        { root: "datasets", mode: "auto", dry_run: true },
        { signal: ctx.signal, timeoutMs: 600000 },
      );
      ctx.toast("Dry analysis finished; no database rows were written.", "positive");
      await load();
    } catch (error) {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => load(),
          context: "The recovery engine reads the datasets directory from the local server.",
        }),
      );
    }
  };

  const runPersist = async () => {
    if (!window.confirm("Run the audit and persist recovery proposals? Raw CSV/PDF files are never modified.")) return;
    host.replaceChildren(loadingPanel("Running and storing dataset audit", 6));
    try {
      latest = await ctx.api.recoveryRun(
        { root: "datasets", mode: "auto", dry_run: false },
        { signal: ctx.signal, timeoutMs: 900000 },
      );
      ctx.toast("Recovery audit stored. Review quarantined records before release.", "positive");
      await load();
    } catch (error) {
      if (isAbort(error)) return;
      host.replaceChildren(errorState(error, { retry: () => load() }));
    }
  };

  const rollback = async () => {
    if (!latest?.run_id) return;
    if (!window.confirm("Quarantine every release from this recovery run? Audit evidence is retained.")) return;
    try {
      await ctx.api.recoveryRollback(
        { run_id: latest.run_id, confirm: true },
        { signal: ctx.signal },
      );
      ctx.toast("Run quarantined. Raw datasets were not modified.", "positive");
      await load();
    } catch (error) {
      if (isAbort(error)) return;
      ctx.toast(error.message || "Rollback failed.", "negative");
    }
  };

  const load = async () => {
    host.replaceChildren(loadingPanel("Reading recovery evidence", 6));
    try {
      const [overview, coursesPayload] = await Promise.all([
        ctx.api.recovery({}, { signal: ctx.signal }),
        ctx.api.recoveryCourses({}, { signal: ctx.signal }),
      ]);
      latest = overview;
      host.replaceChildren(
        renderOverview(ctx, overview, coursesPayload.courses || [], {
          onDry: runDry,
          onPersist: runPersist,
          onRollback: rollback,
          onCourse: (courseId) => showCourse(courseId),
          onRecord: (recordId) => showRecord(recordId),
        }),
      );
    } catch (error) {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => load(),
          context: "Recovery evidence is stored in the Phase 14 tables of the local database.",
        }),
      );
    }
  };

  const showCourse = async (courseId) => {
    host.replaceChildren(loadingPanel("Reading course recovery detail", 5));
    try {
      const detail = await ctx.api.recoveryCourse(courseId, {}, { signal: ctx.signal });
      selectedCourse = detail;
      const [overview, coursesPayload] = await Promise.all([
        ctx.api.recovery({}, { signal: ctx.signal }),
        ctx.api.recoveryCourses({}, { signal: ctx.signal }),
      ]);
      latest = overview;
      host.replaceChildren(
        renderOverview(
          ctx,
          overview,
          coursesPayload.courses || [],
          {
            onDry: runDry,
            onPersist: runPersist,
            onRollback: rollback,
            onCourse: (id) => showCourse(id),
            onRecord: (recordId) => showRecord(recordId),
          },
          detail,
        ),
      );
    } catch (error) {
      if (isAbort(error)) return;
      host.replaceChildren(errorState(error, { retry: () => load() }));
    }
  };

  const showRecord = async (recordId) => {
    try {
      const record = await ctx.api.recoveryRecord(recordId, {}, { signal: ctx.signal });
      const dialog = h(
        "div",
        { class: "modal-backdrop", role: "presentation" },
        panel(
          "Record evidence",
          {
            eyebrow: record.source_question_id,
            description: `${record.release_status} · ${record.answer_status} · source fidelity: ${record.source_fidelity}`,
            actions: [
              h(
                "button",
                {
                  class: "button button-ghost",
                  type: "button",
                  onClick: () => dialog.remove(),
                },
                "Close",
              ),
            ],
          },
          recordComparison(record),
          recordRepairs(ctx, record, () => {
            dialog.remove();
            showRecord(recordId);
          }),
        ),
      );
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.remove();
      });
      document.body.append(dialog);
    } catch (error) {
      if (isAbort(error)) return;
      ctx.toast(error.message || "Record could not be loaded.", "negative");
    }
  };

  wrap.append(
    pageHeader({
      eyebrow: "Resources · Quality assurance",
      title: "Dataset quality & recovery",
      description:
        "Evidence-first repair audit for every course. Original CSV/PDF files stay untouched; unresolved and AI-assisted answers remain quarantined.",
      actions: [
        h("button", { class: "button button-ghost", type: "button", onClick: runDry }, "Dry audit"),
        h("button", { class: "button button-primary", type: "button", onClick: runPersist }, "Run & save"),
        h("button", { class: "button button-ghost", type: "button", onClick: rollback }, "Roll back releases"),
      ],
    }),
  );
  wrap.append(host);

  // The record inspector is attached lazily, so the route can be aborted
  // without leaving a detached modal behind.
  ctx.addCleanup(() => {
    document.querySelectorAll(".modal-backdrop").forEach((node) => node.remove());
  });

  load();
  return wrap;
}

function renderOverview(ctx, overview, courses, actions, detail = null) {
  const totals = overview.totals || {};
  const run = overview.run || {};
  const releases = totals.by_release_status || {};
  const body = h("div", { class: "view-stack" });
  if (!overview.run_id) {
    body.append(
      emptyState({
        title: "No recovery audit has been stored",
        body: "Run a dry audit or a persisted audit. Nothing is written to the source datasets.",
        iconName: "check",
      }),
    );
    return body;
  }
  body.append(
    h(
      "div",
      { class: "view-stack" },
      h(
        "p",
        { class: "scope-statement", text: `Run ${run.run_id || overview.run_id} · status ${run.status || "unknown"} · mode ${run.mode || "—"}` },
      ),
      metrics([
        { label: "Courses", value: formatInteger(overview.courses), detail: `${overview.courses_requiring_repair || 0} need review` },
        { label: "Questions analyzed", value: formatInteger(overview.records) },
        { label: "Missing answers", value: formatInteger(totals.missing_answers) },
        { label: "Recovered answers", value: formatInteger(totals.recovered_answers), detail: `${formatInteger(totals.verified_recoveries)} verified · ${formatInteger(totals.unverified_recoveries)} unverified` },
        { label: "Sources found", value: formatInteger(totals.source_candidates), detail: `${totals.documents_retrieved || 0} retrieved · ${totals.questions_matched_to_sources || 0} matched` },
        { label: "Papers / keys", value: `${formatInteger(totals.question_papers_found)} / ${formatInteger(totals.answer_keys_found)}`, detail: `${formatInteger(totals.question_papers_discovered)} paper leads · ${formatInteger(totals.cache_hits)} cache hits` },
        { label: "Verified recoveries", value: formatInteger(totals.verified_recoveries), detail: `${formatInteger(totals.officially_verified_recoveries)} official · ${formatInteger(totals.independently_verified_recoveries)} independent` },
        { label: "Passage groups", value: formatInteger(totals.passage_groups), detail: `${totals.reconstructed_children || 0} reconstructed children` },
        { label: "Quarantined", value: formatInteger(releases.quarantined), detail: `${releases.review_required || 0} review required` },
        { label: "Issues", value: formatInteger(totals.issues), detail: `${totals.by_severity?.error || 0} blocking errors` },
        { label: "Crawl4AI", value: overview.crawl4ai?.status || "unknown", detail: overview.crawl4ai?.installed ? `v${overview.crawl4ai?.version || "?"} · ${overview.crawl4ai?.browser_launch_ok ? "browser ok" : "browser check pending"}` : "not installed" },
      ]),
    ),
  );
  body.append(
    panel(
      "Crawl and official archive health",
      {
        eyebrow: "Discovery provenance",
        description: "Crawl4AI renders HTML; official archive adapters discover configured PDFs. Archive-discovered documents remain tier 1 evidence only after retrieval and classification.",
      },
      dataTable({
        columns: [
          { key: "archive", label: "Archive", header: true },
          { key: "provider", label: "Adapter" },
          { key: "year", label: "Year" },
          { key: "subjects", label: "Subjects" },
          { key: "status", label: "Status" },
          { key: "key_availability", label: "Coverage" },
          { key: "reason", label: "Limit / reason" },
        ],
        rows: (overview.official_archives || []).map((item) => ({
          archive: item.id,
          provider: item.provider,
          year: item.year,
          subjects: item.subject_count,
          status: item.coverage_state || (item.available ? "available" : "unavailable"),
          key_availability: item.key_availability || "—",
          reason: item.reason || "",
        })),
        caption: `${(overview.official_archives || []).length} configured official archive(s)`,
        empty: "No official archive adapter is configured.",
      }),
    ),
  );
  body.append(
    panel(
      "Courses",
      {
        eyebrow: "Evidence by course",
        description: "Open a course to inspect its quality components, records and unresolved issues.",
      },
      dataTable({
        columns: [
          { key: "course_code", label: "Code", header: true },
          { key: "course_name", label: "Course" },
          { key: "overall", label: "Quality", render: (row) => score(row.overall) },
          { key: "quality_status", label: "Status", render: (row) => statusBadge(humaniseCode(row.quality_status || row.status), toneForStatus(row.quality_status || row.status)) },
          { key: "analyzed_count", label: "Records", render: (row) => formatInteger(row.analyzed_count) },
          { key: "missing", label: "Missing", render: (row) => formatInteger(row.counts?.missing_answer) },
          { key: "recovered", label: "Recovered", render: (row) => formatInteger(row.counts?.recovered_answers) },
          { key: "verified", label: "Verified recoveries", render: (row) => formatInteger(row.counts?.verified_recoveries) },
          { key: "official", label: "Official", render: (row) => formatInteger(row.counts?.officially_verified_recoveries) },
          { key: "independent", label: "Independent", render: (row) => formatInteger(row.counts?.independently_verified_recoveries) },
          { key: "coverage", label: "Source coverage", render: (row) => statusBadge(humaniseCode(row.counts?.source_coverage?.status || "unknown"), toneForStatus(row.counts?.source_coverage?.status)) },
          { key: "blocker", label: "Blocker", render: (row) => String(row.counts?.source_coverage?.blocker || "—").slice(0, 90) },
          { key: "sources", label: "Sources", render: (row) => formatInteger(row.counts?.source_candidates) },
          { key: "passages", label: "Passages", render: (row) => formatInteger(row.counts?.passage_groups) },
          {
            key: "action",
            label: "",
            render: (row) =>
              h(
                "button",
                { class: "button button-ghost", type: "button", onClick: () => actions.onCourse(row.course_id) },
                "Inspect",
              ),
          },
        ],
        rows: courses,
        caption: `${courses.length} course report(s)`,
        empty: "No course reports are stored in this run.",
      }),
    ),
  );
  if (detail) body.append(renderCourseDetail(ctx, detail, actions));
  return body;
}

function renderCourseDetail(ctx, detail, actions) {
  const quality = detail.quality || {};
  const counts = detail.counts || {};
  const components = quality.components || {};
  const body = h("div", { class: "view-stack" });
  body.append(
    panel(
      `${detail.course_code} · ${detail.course_name}`,
      {
        eyebrow: "Course recovery detail",
        description: `${counts.records || 0} records · ${counts.missing_answer || 0} missing answers · ${counts.source_candidates || 0} source lead(s) · ${counts.documents_retrieved || 0} retrieved document(s) · ${counts.questions_matched_to_sources || 0} source-matched question(s) · coverage ${humaniseCode(counts.source_coverage?.status || "unknown")}${counts.source_coverage?.blocker ? ` · ${counts.source_coverage.blocker}` : ""} · ${counts.reconstructed_children || 0} child question proposal(s)`,
        actions: [statusBadge(humaniseCode(detail.status), toneForStatus(detail.status))],
      },
      h(
        "div",
        { class: "view-stack" },
        h(
          "p",
          { class: "muted", text: `Overall ${score(quality.overall)} · evidence ${quality.evidence_status || "unknown"}. ${quality.limitations || "Scores are evidence summaries, not claims of correctness."}` },
        ),
        quality.indicators
          ? h(
              "p",
              { class: "muted", text: `Structural ${score(quality.indicators.structural_quality)} · answer verification ${score((quality.indicators.answer_verification_coverage || 0) * 100)}% · source verification ${score((quality.indicators.source_verification_coverage || 0) * 100)}% · readiness ${quality.indicators.release_readiness || "unknown"}` },
            )
          : null,
        h(
          "div",
          { class: "kpi-grid kpi-grid-compact" },
          ...Object.entries(components).map(([name, value]) =>
            h("div", { class: "metric" }, h("dt", { text: humaniseCode(name) }), h("dd", {}, h("span", { class: "metric-value", text: score(value) }))),
          ),
        ),
        detail.warnings?.length
          ? h("ul", { class: "compact-list" }, ...detail.warnings.map((item) => h("li", { text: item })))
          : null,
      ),
    ),
  );
  body.append(
    panel(
      "Records",
      { eyebrow: "Original ↔ repaired", description: "Open a record to compare the raw row, proposed repair and answer provenance." },
      dataTable({
        columns: [
          { key: "source_question_id", label: "Question", header: true },
          { key: "question_type", label: "Type" },
          { key: "answer_status", label: "Answer" },
          { key: "verification_status", label: "Verification" },
          { key: "source_fidelity", label: "Source" },
          { key: "release_status", label: "Release", render: (row) => statusBadge(humaniseCode(row.release_status), toneForStatus(row.release_status)) },
          { key: "issue_count", label: "Issues" },
          {
            key: "action",
            label: "",
            render: (row) =>
              h(
                "button",
                { class: "button button-ghost", type: "button", onClick: () => actions.onRecord?.(row.record_id) },
                "View",
              ),
          },
        ],
        rows: detail.records || [],
        caption: `${detail.records_total || 0} record(s); showing ${detail.records?.length || 0}`,
        empty: "No records in this course.",
      }),
    ),
  );
  if (detail.issues?.length) {
    body.append(
      panel(
        "Issues",
        { eyebrow: "Extensible rule registry", description: "Blocking findings prevent release; warnings remain reviewable." },
        dataTable({
          columns: [
            { key: "severity", label: "Severity" },
            { key: "code", label: "Code", header: true },
            { key: "message", label: "Finding" },
            { key: "remediation", label: "Remediation" },
          ],
          rows: detail.issues,
          caption: `${detail.issues.length} finding(s) in this page`,
          empty: "No findings.",
        }),
      ),
    );
  }
  if (detail.source_candidates?.length) {
    body.append(
      panel(
        "Discovered sources",
        { eyebrow: "Provenance, not answer evidence", description: "Snippets are leads only; an answer is never accepted from a search snippet." },
        dataTable({
          columns: [
            { key: "title", label: "Source", header: true },
            { key: "document_type", label: "Type" },
            { key: "source_tier", label: "Tier" },
            { key: "reliability", label: "Reliability", render: (row) => score((row.reliability || 0) * 100) },
            { key: "retrieval_status", label: "Status" },
          ],
          rows: detail.source_candidates,
          caption: `${detail.source_candidates.length} discovered candidate(s)`,
          empty: "No public sources were discovered.",
        }),
      ),
    );
  }
  if (detail.passages?.length) {
    body.append(
      panel(
        "Passage structures",
        { eyebrow: "Evidenced parent/child groups", description: "Only explicit passages or shared passage columns are grouped; no text is invented." },
        dataTable({
          columns: [
            { key: "passage_id", label: "Passage", header: true },
            { key: "passage_text", label: "Excerpt", render: (row) => String(row.passage_text || "").slice(0, 180) },
            { key: "child_record_ids", label: "Children", render: (row) => formatInteger((row.child_record_ids || []).length) },
            { key: "confidence", label: "Confidence", render: (row) => score((row.confidence || 0) * 100) },
          ],
          rows: detail.passages,
          caption: `${detail.passages.length} passage group(s)`,
          empty: "No passage relationships had sufficient evidence.",
        }),
      ),
    );
  }
  return body;
}

function recordComparison(record) {
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    dataTable({
      columns: [
        { key: "label", label: "View", header: true },
        { key: "value", label: "Content" },
      ],
      rows: [
        { label: "Original row", value: JSON.stringify(record.original, null, 2) },
        { label: "Normalized", value: JSON.stringify(record.normalized, null, 2) },
        { label: "Proposed repaired", value: JSON.stringify(record.repaired, null, 2) },
        { label: "Structure", value: JSON.stringify(record.structure, null, 2) },
        { label: "Verification", value: JSON.stringify(record.verification || {}, null, 2) },
        { label: "Source fidelity", value: `${record.source_fidelity}\n${JSON.stringify(record.quality || {}, null, 2)}` },
      ],
      caption: "Original and proposed states are separate; the raw file is immutable.",
    }),
  );
  if (record.answer_candidates?.length) {
    wrap.append(
      dataTable({
        columns: [
          { key: "verification_status", label: "Verification" },
          { key: "source_type", label: "Source" },
          { key: "answer_text", label: "Answer" },
          { key: "confidence", label: "Confidence" },
          { key: "method", label: "Method" },
        ],
        rows: record.answer_candidates,
        caption: "Answer provenance. AI-assisted candidates are never official.",
      }),
    );
  }
  return wrap;
}

function recordRepairs(ctx, record, refresh) {
  if (!record.repairs?.length) return emptyState({ title: "No repair proposals", body: "Nothing was proposed for this record.", iconName: "info" });
  const wrap = h("div", { class: "view-stack" });
  for (const repair of record.repairs) {
    const actions = h("div", { class: "page-actions" });
    if (repair.status !== "approved" && repair.status !== "rejected") {
      actions.append(
        h(
          "button",
          {
            class: "button button-primary",
            type: "button",
            onClick: async () => {
              try {
                await ctx.api.recoveryReview(
                  { repair_id: repair.repair_id, status: "approved", reviewer: "dashboard-user", note: "Approved from record comparison." },
                  { signal: ctx.signal },
                );
                ctx.toast("Repair approved.", "positive");
                refresh();
              } catch (error) {
                ctx.toast(error.message || "Review failed.", "negative");
              }
            },
          },
          "Approve",
        ),
        h(
          "button",
          {
            class: "button button-ghost",
            type: "button",
            onClick: async () => {
              try {
                await ctx.api.recoveryReview(
                  { repair_id: repair.repair_id, status: "rejected", reviewer: "dashboard-user", note: "Rejected from record comparison." },
                  { signal: ctx.signal },
                );
                ctx.toast("Repair rejected.", "positive");
                refresh();
              } catch (error) {
                ctx.toast(error.message || "Review failed.", "negative");
              }
            },
          },
          "Reject",
        ),
      );
    }
    wrap.append(
      panel(
        repair.repair_type,
        {
          eyebrow: `${repair.status} · ${repair.risk} risk`,
          description: repair.reason,
          actions: [statusBadge(repair.status, repair.status === "approved" ? "positive" : repair.status === "rejected" ? "negative" : "caution")],
        },
        h(
          "div",
          { class: "view-stack" },
          h("p", { class: "muted", text: `Method: ${repair.method} · confidence ${score(repair.confidence * 100)}%` }),
          h("pre", { class: "json-block", text: JSON.stringify({ before: repair.before, after: repair.after }, null, 2) }),
          actions,
        ),
      ),
    );
  }
  return wrap;
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
