/**
 * Pure presentation helpers and the explicit tutor state machine.
 * These functions contain no DOM access so they can be tested with Node.
 */

export const TUTOR_STATES = Object.freeze([
  "idle",
  "thinking",
  "speaking",
  "positive",
  "corrective",
  "warning",
]);

export function tutorStateFor(response) {
  if (!response || typeof response !== "object") return "warning";
  if (TUTOR_STATES.includes(response.state)) return response.state;
  if (response.status === "answered") return "speaking";
  if (response.status === "insufficient_evidence") return "corrective";
  return "warning";
}

export function formatInteger(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(number);
}

export function formatNumber(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  }).format(number);
}

export function formatPercent(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${(number * 100).toFixed(digits)}%`;
}

export function formatDate(value, { includeTime = false } = {}) {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

export function titleCase(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function shorten(value, max = 120) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

export function plural(count, singular, pluralForm = `${singular}s`) {
  return `${formatInteger(count)} ${Number(count) === 1 ? singular : pluralForm}`;
}

export const COVERAGE_LABELS = Object.freeze({
  covered: "Covered",
  low_coverage: "Low coverage",
  unmapped: "Unmapped",
  uncertain: "Uncertain",
  insufficient_data: "Insufficient data",
});

export function coverageLabel(status) {
  return COVERAGE_LABELS[status] || titleCase(status || "unknown");
}

export function coverageTone(status) {
  return {
    covered: "positive",
    low_coverage: "caution",
    unmapped: "negative",
    uncertain: "warning",
    insufficient_data: "neutral",
  }[status] || "neutral";
}

export function reviewTone(status) {
  return {
    approved: "positive",
    rejected: "negative",
    pending_review: "caution",
    candidate: "caution",
    reviewed: "positive",
    verified: "positive",
  }[status] || "neutral";
}

export function validationTone(status) {
  return {
    validated: "positive",
    needs_review: "caution",
    incomplete: "warning",
    invalid: "negative",
    rejected: "negative",
  }[status] || "neutral";
}

export function jobStatusLabel(status) {
  return {
    queued: "Queued",
    running: "Running",
    succeeded: "Completed",
    failed: "Failed",
  }[status] || titleCase(status || "unknown");
}

export function dataStateLabel(state) {
  return {
    empty: "Empty",
    documents_only: "Documents only",
    ingested: "Syllabus parsed",
    analysed: "Questions analysed",
    graph_ready: "Graph ready",
  }[state] || titleCase(state || "unknown");
}

export function humaniseCode(code) {
  return String(code || "")
    .replace(/[_.]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function countBy(items, key) {
  const counts = new Map();
  for (const item of items || []) {
    const value = typeof key === "function" ? key(item) : item?.[key];
    if (value === null || value === undefined || value === "") continue;
    counts.set(value, (counts.get(value) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value || a.label.localeCompare(b.label));
}

export function barData(items, { labelKey = "label", valueKey = "value", limit = 30 } = {}) {
  const rows = (items || [])
    .map((item) => ({
      label: String(item?.[labelKey] ?? "—"),
      value: Number(item?.[valueKey] ?? 0),
      percent: item?.percent,
      tone: item?.tone,
    }))
    .filter((item) => Number.isFinite(item.value) && item.value >= 0)
    .sort((a, b) => b.value - a.value)
    .slice(0, limit);
  const max = rows.reduce((current, item) => Math.max(current, item.value), 0);
  return rows.map((item) => ({
    ...item,
    share: max > 0 ? item.value / max : 0,
  }));
}

export function barSeries(items, { limit = 30 } = {}) {
  const all = barData(items, { limit: (items || []).length || 1 });
  const rows = all.slice(0, Math.max(0, Number(limit) || 0));
  return { rows, total: all.length, shown: rows.length, truncated: rows.length < all.length };
}

export function segmentData(segments) {
  const rows = (segments || [])
    .map((item) => ({
      label: String(item.label ?? "—"),
      value: Math.max(0, Number(item.value ?? 0)),
      tone: item.tone || "neutral",
    }))
    .filter((item) => Number.isFinite(item.value));
  const total = rows.reduce((sum, item) => sum + item.value, 0);
  return {
    total,
    segments: rows.map((item) => ({
      ...item,
      share: total > 0 ? item.value / total : 0,
    })),
  };
}

export function truncationNotes(truncation) {
  if (!truncation || typeof truncation !== "object") return [];
  return Object.entries(truncation)
    .filter(([, value]) => value && value.shown < value.total)
    .map(([key, value]) => `${humaniseCode(key)}: showing ${value.shown} of ${value.total}`);
}

export function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

export function uniqueStrings(values) {
  return [...new Set((values || []).filter((value) => typeof value === "string" && value))];
}
