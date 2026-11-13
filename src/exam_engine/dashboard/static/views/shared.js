import { h, append, button, icon, toneBadge } from "../dom.js";
import { illustrationBlock } from "../hairline.js";
import {
  formatDate,
  humaniseCode,
  jobStatusLabel,
  safeArray,
  truncationNotes,
} from "../ui-state.js";

export function pageHeader({ eyebrow, title, description, actions = [] }) {
  return h(
    "header",
    { class: "page-header" },
    h(
      "div",
      { class: "page-heading" },
      eyebrow ? h("p", { class: "eyebrow", text: eyebrow }) : null,
      h("h1", { text: title }),
      description ? h("p", { class: "lede", text: description }) : null,
    ),
    actions.length ? h("div", { class: "page-actions" }, ...actions) : null,
  );
}

export function panel(title, { eyebrow, description, actions = [], className = "", id } = {}, ...content) {
  return h(
    "section",
    { class: `panel ${className}`.trim(), id },
    h(
      "header",
      { class: "panel-header" },
      h(
        "div",
        {},
        eyebrow ? h("p", { class: "eyebrow", text: eyebrow }) : null,
        h("h2", { text: title }),
        description ? h("p", { class: "panel-description", text: description }) : null,
      ),
      actions.length ? h("div", { class: "panel-actions" }, ...actions) : null,
    ),
    h("div", { class: "panel-body" }, ...content),
  );
}

export function linkButton(label, href, { variant = "ghost", iconName } = {}) {
  return h(
    "a",
    { class: `button button-${variant}`, href },
    iconName ? icon(iconName) : null,
    h("span", { text: label }),
  );
}

export function emptyState({ title, body, actions = [], iconName = "info" }) {
  return h(
    "div",
    { class: "empty-state" },
    h("div", { class: "empty-icon", "aria-hidden": "true" }, icon(iconName)),
    h(
      "div",
      { class: "empty-copy" },
      h("h3", { text: title }),
      body ? h("p", { text: body }) : null,
      actions.length ? h("div", { class: "empty-actions" }, ...actions) : null,
    ),
  );
}

export function errorState(error, { retry, context } = {}) {
  const code = error?.code || "request_failed";
  const status = error?.status ? `HTTP ${error.status}` : "request failed";
  const details = safeArray(error?.detail?.errors);
  return h(
    "div",
    { class: "error-state", role: "alert" },
    h("div", { class: "error-mark", "aria-hidden": "true" }, "!"),
    h(
      "div",
      { class: "error-copy" },
      h("h3", { text: "This part of the workspace could not load" }),
      h("p", { text: error?.message || "The request failed." }),
      h("p", { class: "error-meta", text: `${humaniseCode(code)} · ${status}` }),
      context ? h("p", { class: "muted", text: context }) : null,
      details.length
        ? h(
            "ul",
            { class: "error-details" },
            ...details.map((item) =>
              h("li", { text: `${safeArray(item.loc).join(".") || "request"}: ${item.msg || "invalid"}` }),
            ),
          )
        : null,
      retry ? h("div", { class: "error-actions" }, button("Retry", { onClick: retry, iconName: "refresh" })) : null,
    ),
  );
}

export function loadingPanel(label = "Loading", rows = 3) {
  return h(
    "div",
    { class: "loading-panel", role: "status", "aria-label": label },
    h("p", { class: "eyebrow", text: label }),
    h(
      "div",
      { class: "skeleton-stack", "aria-hidden": "true" },
      ...Array.from({ length: rows }, () => h("span")),
    ),
  );
}

export function inlineLoading(label = "Loading…") {
  return h("p", { class: "inline-loading", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), label);
}

export function metrics(items) {
  return h(
    "dl",
    { class: "metrics" },
    ...items
      .filter(Boolean)
      .map((item) =>
        h(
          "div",
          { class: "metric" },
          h("dt", { text: item.label }),
          h(
            "dd",
            {},
            item.value instanceof Node ? item.value : h("span", { class: "metric-value", text: String(item.value ?? "—") }),
            item.detail ? h("small", { text: item.detail }) : null,
          ),
        ),
      ),
  );
}

export function dataTable({ columns, rows, caption, empty = "No records.", rowClass }) {
  const body = h("tbody");
  if (!rows.length) {
    body.append(
      h(
        "tr",
        {},
        h("td", { colspan: String(columns.length), class: "muted", text: empty }),
      ),
    );
  } else {
    for (const row of rows) {
      const tr = h("tr", { class: rowClass ? rowClass(row) : "" });
      for (const column of columns) {
        const value = column.render ? column.render(row) : row[column.key];
        const cell = h(column.header ? "th" : "td", {
          scope: column.header ? "row" : null,
          class: column.className || "",
        });
        if (value instanceof Node) cell.append(value);
        else cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
        tr.append(cell);
      }
      body.append(tr);
    }
  }
  return h(
    "div",
    { class: "table-scroll" },
    h(
      "table",
      { class: "data-table" },
      caption ? h("caption", { class: "table-caption", text: caption }) : null,
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          ...columns.map((column) =>
            h("th", { scope: "col", class: column.className || "" }, column.label),
          ),
        ),
      ),
      body,
    ),
  );
}

export function definitionList(items) {
  return h(
    "dl",
    { class: "definition-list" },
    ...items
      .filter(Boolean)
      .map(([term, description]) =>
        h(
          "div",
          {},
          h("dt", { text: term }),
          h("dd", {}, description instanceof Node ? description : String(description ?? "—")),
        ),
      ),
  );
}

export function notice(text, tone = "neutral", title) {
  return h(
    "div",
    { class: `notice notice-${tone}` },
    title ? h("strong", { text: title }) : null,
    h("p", { text }),
  );
}

export function disclaimer(text) {
  return h("p", { class: "disclaimer", text });
}

export function statusBadge(text, tone = "neutral") {
  return toneBadge(text, tone);
}

export function truncationNotice(payload) {
  const notes = truncationNotes(payload?.truncation);
  if (!notes.length) return null;
  return h("p", { class: "truncation-note", text: `Summarised response — ${notes.join("; ")}.` });
}

/**
 * Count one best-fit unit per question from the complete coverage review table.
 * The table includes questions without a confident Phase 3 mapping, so unit
 * charts can represent the whole corpus instead of only the confident subset.
 */
export function unitAssignmentSummary(report) {
  const assignments = safeArray(report?.question_assignments);
  const unitLabels = new Map(
    safeArray(report?.units).map((unit) => [unit.unit_id, unit.unit_label]),
  );
  const counts = new Map();
  const questionIds = new Set();
  for (const assignment of assignments) {
    const unitIds = safeArray(assignment.unit_ids);
    if (!unitIds.length) continue;
    questionIds.add(assignment.question_id);
    unitIds.forEach((unitId, index) => {
      const label =
        safeArray(assignment.unit_labels)[index] || unitLabels.get(unitId) || unitId;
      if (!counts.has(label)) counts.set(label, new Set());
      counts.get(label).add(assignment.question_id);
    });
  }
  const rows = [...counts]
    .map(([label, ids]) => ({ label, value: ids.size }))
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label));
  const total =
    Number(report?.data_quality?.questions) || Number(report?.summary?.total_questions) || 0;
  return {
    assigned: questionIds.size,
    total,
    rate: total ? questionIds.size / total : 0,
    rows,
  };
}

export function tabs(ctx, items, active) {
  return h(
    "nav",
    { class: "tab-nav", "aria-label": "Analysis views" },
    ...items.map((item) =>
      h(
        "a",
        {
          class: `tab ${item.key === active ? "is-active" : ""}`.trim(),
          href: `#/c/${encodeURIComponent(ctx.courseId)}/analysis/${item.key}`,
          "aria-current": item.key === active ? "page" : null,
        },
        item.label,
      ),
    ),
  );
}

export function illustration(ctx, name, caption) {
  return illustrationBlock(ctx, name, {
    label: `${caption}. Decorative isometric illustration; it does not display application data.`,
    caption,
  });
}

export function selectedSyllabus(ctx) {
  const syllabi = safeArray(ctx.course?.syllabi);
  const stored = ctx.preferences?.syllabusId;
  const match = syllabi.find((item) => item.syllabus_id === stored);
  return match?.syllabus_id || syllabi[0]?.syllabus_id || null;
}

export function syllabusSelect(ctx, { onChange } = {}) {
  const syllabi = safeArray(ctx.course?.syllabi);
  if (!syllabi.length) return null;
  const select = h(
    "select",
    { id: "syllabus-select", "aria-label": "Syllabus version" },
    ...syllabi.map((item) =>
      h("option", {
        value: item.syllabus_id,
        text: `${item.title || item.syllabus_id} · ${item.unit_count} units`,
      }),
    ),
  );
  select.value = selectedSyllabus(ctx) || syllabi[0].syllabus_id;
  select.addEventListener("change", () => {
    ctx.setPreference("syllabusId", select.value);
    if (onChange) onChange(select.value);
    else ctx.reload();
  });
  return h(
    "label",
    { class: "field field-inline" },
    h("span", { text: "Syllabus version" }),
    select,
  );
}

export function field(labelText, control, hint) {
  const id = control.id || `field-${Math.random().toString(36).slice(2, 9)}`;
  control.id = id;
  return h(
    "label",
    { class: "field", for: id },
    h("span", { text: labelText }),
    control,
    hint ? h("small", { text: hint }) : null,
  );
}

export function selectControl(options, { name, value, id } = {}) {
  const select = h(
    "select",
    { name, id },
    ...options.map((option) =>
      h("option", { value: option.value, text: option.label, disabled: option.disabled }),
    ),
  );
  if (value !== undefined) select.value = value;
  return select;
}

export function inputControl({ name, type = "text", value = "", placeholder, required, id, min, max, step, maxlength } = {}) {
  return h("input", {
    type,
    name,
    value,
    placeholder,
    required,
    id,
    min,
    max,
    step,
    maxlength,
  });
}

export function textareaControl({ name, value = "", placeholder, rows = 4, id } = {}) {
  const textarea = h("textarea", { name, placeholder, rows, id });
  textarea.value = value;
  return textarea;
}

export function jobControl(ctx, kind, label, { onDone } = {}) {
  const status = h("span", { class: "job-state", role: "status", "aria-live": "polite" });
  const action = button(label, { variant: "primary", iconName: "spark" });
  action.addEventListener("click", async () => {
    action.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Queued…";
    try {
      const started = await ctx.api.startAnalysis(ctx.courseId, kind, { signal: ctx.signal });
      let job = started.job;
      const began = Date.now();
      while (job.status === "queued" || job.status === "running") {
        if (Date.now() - began > 10 * 60 * 1000) {
          throw new Error("The analysis is still running after ten minutes. Check the CLI for details.");
        }
        await delay(1200, ctx.signal);
        job = await ctx.api.job(job.job_id, { signal: ctx.signal });
        status.textContent = `${jobStatusLabel(job.status)}…`;
      }
      if (job.status === "failed") {
        status.textContent = job.error?.message || "The analysis failed.";
        status.dataset.tone = "negative";
        action.disabled = false;
        return;
      }
      status.textContent = "Completed";
      status.dataset.tone = "positive";
      ctx.toast(`${label} completed.`, "positive");
      if (onDone) onDone(job);
      else ctx.reload();
    } catch (error) {
      if (error?.name === "AbortError") return;
      status.textContent = error?.message || "The analysis failed.";
      status.dataset.tone = "negative";
      action.disabled = false;
    }
  });
  return h("div", { class: "job-control" }, action, status);
}

export function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException("aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason ?? new DOMException("aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

export function sourceSummary(document) {
  const source = document?.source;
  if (!source) return "provenance missing";
  return [
    humaniseCode(source.dataset_type),
    source.license || "licence not recorded",
    source.publisher || source.name,
  ].join(" · ");
}

export function docKindLabel(kind) {
  return humaniseCode(kind);
}

export function dateOrDash(value, includeTime = false) {
  return value ? formatDate(value, { includeTime }) : "—";
}

export function appendAll(parent, children) {
  append(parent, children);
  return parent;
}
