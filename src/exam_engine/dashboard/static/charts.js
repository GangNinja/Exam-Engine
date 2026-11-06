/**
 * Accessible SVG charts. Every chart is built from the values passed in and
 * includes a data table, so the visual is never the only source of meaning.
 * There is no charting library and no generated or placeholder series.
 */
import { h, svg, fragment } from "./dom.js";
import { barData, formatNumber, formatPercent, segmentData, titleCase } from "./ui-state.js";

let chartId = 0;

export function barChart(items, options = {}) {
  const {
    title = "Distribution",
    description = "",
    valueFormat = (value) => formatNumber(value, 2),
    tone = "accent",
    limit = 30,
    unitLabel = "",
  } = options;
  const data = barData(items, { limit });
  if (!data.length) return emptyChart(title, description || "No data is available.");
  const id = `chart-${++chartId}`;
  const width = 640;
  const rowHeight = 30;
  const top = 16;
  const bottom = 16;
  const labelWidth = 220;
  const valueWidth = 86;
  const barWidth = width - labelWidth - valueWidth - 24;
  const height = top + data.length * rowHeight + bottom;
  const max = Math.max(...data.map((item) => item.value), 1);

  const bars = data.map((item, index) => {
    const y = top + index * rowHeight;
    const widthValue = Math.max(item.value > 0 ? 3 : 0, (item.value / max) * barWidth);
    return svg(
      "g",
      { class: `bar-row bar-${item.tone || tone}` },
      svg("text", {
        x: 0,
        y: y + 19,
        class: "chart-label",
        text: truncate(item.label, 34),
      }),
      svg("rect", {
        x: labelWidth,
        y: y + 6,
        width: barWidth,
        height: 14,
        rx: 3,
        class: "chart-track",
      }),
      svg("rect", {
        x: labelWidth,
        y: y + 6,
        width: widthValue,
        height: 14,
        rx: 3,
        class: "chart-bar",
      }),
      svg("text", {
        x: labelWidth + barWidth + 12,
        y: y + 18,
        class: "chart-value",
        text: valueFormat(item.value),
      }),
    );
  });

  const svgNode = svg(
    "svg",
    {
      viewBox: `0 0 ${width} ${height}`,
      class: "chart-svg",
      role: "img",
      "aria-labelledby": `${id}-title ${id}-desc`,
    },
    svg("title", { id: `${id}-title`, text: title }),
    svg("desc", { id: `${id}-desc`, text: description || "Horizontal bar chart." }),
    ...bars,
  );

  return chartFrame({
    title,
    description,
    visual: svgNode,
    columns: ["Label", unitLabel || "Value"],
    rows: data.map((item) => [item.label, valueFormat(item.value)]),
  });
}

export function stackedBar(segments, options = {}) {
  const {
    title = "Distribution",
    description = "",
    valueFormat = (value) => formatNumber(value, 0),
    showLegend = true,
  } = options;
  const { total, segments: rows } = segmentData(segments);
  if (!rows.length || total === 0) {
    return emptyChart(title, description || "No values are available.");
  }
  const width = 640;
  const height = 30;
  let offset = 0;
  const parts = rows.map((item) => {
    const partWidth = item.share * width;
    const part = svg("rect", {
      x: offset,
      y: 0,
      width: Math.max(0, partWidth),
      height,
      class: `segment segment-${item.tone}`,
    });
    offset += partWidth;
    part.append(svg("title", { text: `${item.label}: ${valueFormat(item.value)} (${formatPercent(item.share)})` }));
    return part;
  });
  const svgNode = svg(
    "svg",
    {
      viewBox: `0 0 ${width} ${height}`,
      class: "stacked-svg",
      role: "img",
      "aria-label": `${title}. ${rows.map((item) => `${item.label}: ${valueFormat(item.value)}`).join(", ")}`,
      preserveAspectRatio: "none",
    },
    ...parts,
  );
  const legend = showLegend
    ? h(
        "ul",
        { class: "legend" },
        ...rows.map((item) =>
          h(
            "li",
            {},
            h("span", { class: `legend-swatch segment-${item.tone}`, "aria-hidden": "true" }),
            h("span", { class: "legend-label", text: item.label }),
            h("span", { class: "legend-value", text: `${valueFormat(item.value)} · ${formatPercent(item.share)}` }),
          ),
        ),
      )
    : null;
  return chartFrame({
    title,
    description,
    visual: h("div", { class: "stacked-visual" }, svgNode, legend),
    columns: ["Label", "Value", "Share"],
    rows: rows.map((item) => [item.label, valueFormat(item.value), formatPercent(item.share)]),
  });
}

export function lineChart(points, options = {}) {
  const {
    title = "Observed series",
    description = "",
    valueFormat = (value) => formatNumber(value, 1),
    xLabel = "Period",
    yLabel = "Value",
  } = options;
  const rows = (points || [])
    .map((point) => ({
      label: String(point.label ?? "—"),
      value: Number(point.value ?? 0),
    }))
    .filter((point) => Number.isFinite(point.value));
  if (!rows.length) return emptyChart(title, description || "No observed periods are available.");
  const width = 640;
  const height = 260;
  const padding = { top: 24, right: 24, bottom: 58, left: 54 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const max = Math.max(...rows.map((row) => row.value), 1);
  const xFor = (index) => padding.left + (rows.length === 1 ? plotWidth / 2 : (index / (rows.length - 1)) * plotWidth);
  const yFor = (value) => padding.top + plotHeight - (value / max) * plotHeight;
  const path = rows.map((row, index) => `${index === 0 ? "M" : "L"}${xFor(index)},${yFor(row.value)}`).join(" ");
  const labelEvery = Math.max(1, Math.ceil(rows.length / 7));

  const svgNode = svg(
    "svg",
    {
      viewBox: `0 0 ${width} ${height}`,
      class: "chart-svg",
      role: "img",
      "aria-label": `${title}. ${rows.map((row) => `${row.label}: ${valueFormat(row.value)}`).join(", ")}`,
    },
    ...[0, 0.5, 1].map((ratio) =>
      svg("line", {
        x1: padding.left,
        x2: width - padding.right,
        y1: padding.top + plotHeight * ratio,
        y2: padding.top + plotHeight * ratio,
        class: "chart-grid",
      }),
    ),
    svg("path", { d: path, class: "chart-line" }),
    ...rows.map((row, index) =>
      svg(
        "g",
        {},
        svg("circle", {
          cx: xFor(index),
          cy: yFor(row.value),
          r: 4,
          class: "chart-dot",
        }),
        svg("title", { text: `${row.label}: ${valueFormat(row.value)}` }),
      ),
    ),
    svg("text", {
      x: padding.left,
      y: padding.top - 8,
      class: "chart-axis-label",
      text: yLabel,
    }),
    ...rows
      .map((row, index) =>
        index % labelEvery === 0 || index === rows.length - 1
          ? svg("text", {
              x: xFor(index),
              y: height - 26,
              class: "chart-tick",
              "text-anchor": "middle",
              text: truncate(row.label, 14),
            })
          : null,
      )
      .filter(Boolean),
  );

  return chartFrame({
    title,
    description,
    visual: svgNode,
    columns: [xLabel, yLabel],
    rows: rows.map((row) => [row.label, valueFormat(row.value)]),
  });
}

export function chartFrame({ title, description, visual, columns, rows }) {
  return h(
    "figure",
    { class: "chart" },
    h("figcaption", { class: "chart-caption" }, h("strong", { text: title }), description ? h("span", { text: description }) : null),
    visual,
    h(
      "details",
      { class: "chart-data" },
      h("summary", { text: "View data table" }),
      h(
        "div",
        { class: "table-scroll" },
        h(
          "table",
          {},
          h("caption", { class: "visually-hidden", text: `${title} data` }),
          h("thead", {}, h("tr", {}, ...columns.map((column) => h("th", { scope: "col", text: column })))),
          h(
            "tbody",
            {},
            ...rows.map((row) => h("tr", {}, ...row.map((cell, index) => h(index === 0 ? "th" : "td", { scope: index === 0 ? "row" : null, text: String(cell) })))),
          ),
        ),
      ),
    ),
  );
}

export function emptyChart(title, message) {
  return h(
    "div",
    { class: "chart chart-empty" },
    h("p", { class: "chart-caption" }, h("strong", { text: title })),
    h("p", { class: "muted", text: message }),
  );
}

export function distributionFromCounts(counts, labels = {}) {
  return Object.entries(counts || {})
    .map(([key, value]) => ({
      label: labels[key] || titleCase(key),
      value: Number(value) || 0,
    }))
    .filter((item) => item.value > 0);
}

function truncate(value, max) {
  const text = String(value ?? "");
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

export { formatPercent };
