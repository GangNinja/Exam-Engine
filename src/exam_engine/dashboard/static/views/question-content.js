import { h } from "../dom.js";

const IMAGE_RE = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g;
const IMAGE_PLACEHOLDER_RE = /\[(?:figure|image)[^\]]*\]/gi;
const TABLE_SEPARATOR_RE = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;
const CODE_FENCE_RE = /^\s*```/;

export function extractImageReferences(text) {
  const references = [];
  const source = String(text ?? "");
  for (const match of source.matchAll(IMAGE_RE)) {
    references.push({ alt: match[1] || "", src: match[2] || "" });
  }
  return references;
}

export function hasMediaPlaceholder(text) {
  return /\[(?:figure|image)[^\]]*\]/i.test(String(text ?? ""));
}

export function isSafeImageSource(src) {
  const value = String(src ?? "").trim();
  if (!value || value.startsWith("/") || value.startsWith("//")) return false;
  return !/^[a-z][a-z0-9+.-]*:/i.test(value);
}

function splitTableRow(line) {
  let value = String(line ?? "").trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  return value.split("|").map((cell) => cell.trim());
}

function parseTable(lines, start) {
  if (start + 1 >= lines.length || !TABLE_SEPARATOR_RE.test(lines[start + 1])) return null;
  const header = splitTableRow(lines[start]);
  if (header.length < 2) return null;
  const separator = splitTableRow(lines[start + 1]);
  if (separator.length !== header.length) return null;
  const rows = [];
  let index = start + 2;
  while (index < lines.length && lines[index].includes("|")) {
    const row = splitTableRow(lines[index]);
    if (row.length !== header.length) break;
    rows.push(row);
    index += 1;
  }
  if (!rows.length) return null;
  return { block: { type: "table", header, rows }, next: index };
}

/**
 * Split original text into display blocks without rewriting its contents.
 * A malformed fence or table remains a text block, so the reader can still
 * inspect the original value.
 */
export function parseQuestionBlocks(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let textStart = 0;
  let index = 0;

  const flushText = (end) => {
    if (end <= textStart) return;
    const value = lines.slice(textStart, end).join("\n");
    if (value.trim()) blocks.push({ type: "text", text: value });
  };

  while (index < lines.length) {
    if (CODE_FENCE_RE.test(lines[index])) {
      const close = lines.findIndex(
        (line, candidate) => candidate > index && CODE_FENCE_RE.test(line),
      );
      if (close !== -1) {
        flushText(index);
        const opening = lines[index].trim().replace(/^```/, "").trim();
        blocks.push({
          type: "code",
          language: opening || "",
          text: lines.slice(index + 1, close).join("\n"),
        });
        index = close + 1;
        textStart = index;
        continue;
      }
    }
    if (lines[index].includes("|")) {
      const table = parseTable(lines, index);
      if (table) {
        flushText(index);
        blocks.push(table.block);
        index = table.next;
        textStart = index;
        continue;
      }
    }
    index += 1;
  }
  flushText(lines.length);
  return blocks;
}

function mediaFallback(label, extra = "") {
  return h(
    "span",
    { class: "media-fallback", role: "note" },
    h("span", { class: "media-fallback-label", text: label }),
    extra ? h("span", { class: "media-fallback-note", text: extra }) : null,
  );
}

function textWithImages(text) {
  const nodes = [];
  let cursor = 0;
  const source = String(text ?? "");
  for (const match of source.matchAll(IMAGE_RE)) {
    if (match.index > cursor) nodes.push(source.slice(cursor, match.index));
    const alt = match[1] || "Referenced image";
    const src = match[2] || "";
    if (isSafeImageSource(src)) {
      nodes.push(
        h(
          "figure",
          { class: "question-media" },
          h("img", {
            src,
            alt,
            loading: "lazy",
            onError: (event) => {
              event.currentTarget.replaceWith(
                mediaFallback("Image unavailable", `Path: ${src}`),
              );
            },
          }),
          alt ? h("figcaption", { text: alt }) : null,
        ),
      );
    } else {
      nodes.push(mediaFallback("Image unavailable", `Path: ${src}`));
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) nodes.push(source.slice(cursor));

  // Placeholders are semantic text, not images. Mark them for the reader
  // without changing the stored wording.
  const decorated = [];
  for (const node of nodes) {
    if (typeof node !== "string") {
      decorated.push(node);
      continue;
    }
    let last = 0;
    for (const match of node.matchAll(IMAGE_PLACEHOLDER_RE)) {
      if (match.index > last) decorated.push(node.slice(last, match.index));
      decorated.push(h("span", { class: "media-placeholder", text: match[0] }));
      last = match.index + match[0].length;
    }
    if (last < node.length) decorated.push(node.slice(last));
  }
  return decorated;
}

function renderTable(block) {
  return h(
    "div",
    { class: "question-table-wrap" },
    h(
      "table",
      { class: "question-table", "aria-label": "Question table" },
      h(
        "thead",
        {},
        h("tr", {}, ...block.header.map((cell) => h("th", { scope: "col", text: cell }))),
      ),
      h(
        "tbody",
        {},
        ...block.rows.map((row) =>
          h("tr", {}, ...row.map((cell) => h("td", { text: cell }))),
        ),
      ),
    ),
  );
}

export function renderQuestionContent(text) {
  const nodes = parseQuestionBlocks(text).map((block) => {
    if (block.type === "code") {
      return h(
        "pre",
        {
          class: "question-code",
          "data-language": block.language || null,
          "aria-label": block.language ? `${block.language} code` : "Question code",
        },
        h("code", { text: block.text || " " }),
      );
    }
    if (block.type === "table") return renderTable(block);
    return h(
      "div",
      { class: "question-text question-formatted-text" },
      ...textWithImages(block.text),
    );
  });
  return h("div", { class: "question-content" }, ...nodes);
}

export function renderOptionContent(text) {
  const value = String(text ?? "");
  if (!hasMediaPlaceholder(value)) return h("span", { class: "option-text-value", text: value });
  return h(
    "span",
    { class: "option-text-value" },
    h("span", { class: "media-placeholder", text: value }),
    h("span", { class: "media-fallback", text: "Visual option not supplied by the source." }),
  );
}
