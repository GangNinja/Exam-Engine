/**
 * Tiny DOM builder. All application data is written with text nodes or
 * textContent, never with innerHTML, so records cannot inject markup.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  applyProps(element, props);
  append(element, children);
  return element;
}

export function svg(tag, props = {}, ...children) {
  const element = document.createElementNS(SVG_NS, tag);
  applyProps(element, props, true);
  append(element, children);
  return element;
}

export function fragment(...children) {
  const node = document.createDocumentFragment();
  append(node, children);
  return node;
}

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function applyProps(element, props = {}, isSvg = false) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class" || key === "className") {
      element.setAttribute("class", String(value));
    } else if (key === "text" || key === "textContent") {
      element.textContent = String(value);
    } else if (key === "html") {
      // Only for static, trusted SVG path markup defined in this module.
      element.innerHTML = String(value);
    } else if (key === "dataset") {
      for (const [dataKey, dataValue] of Object.entries(value)) {
        if (dataValue !== null && dataValue !== undefined) {
          element.dataset[dataKey] = String(dataValue);
        }
      }
    } else if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "style" && typeof value === "object") {
      Object.assign(element.style, value);
    } else if (isSvg && (key === "viewBox" || key === "preserveAspectRatio")) {
      element.setAttribute(key, String(value));
    } else if (key in element && !isSvg && key !== "list" && key !== "form") {
      element[key] = value;
    } else {
      element.setAttribute(key, String(value));
    }
  }
}

export function icon(name) {
  const paths = ICONS[name] || ICONS.dot;
  return svg(
    "svg",
    {
      viewBox: "0 0 24 24",
      class: "icon",
      "aria-hidden": "true",
      focusable: "false",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "1.7",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    },
    ...paths.map((d) => svg("path", { d })),
  );
}

const ICONS = {
  overview: ["M4 5h16", "M4 12h10", "M4 19h7", "M18 12v7", "M15 15l3 3 3-3"],
  course: ["M4 5.5A2.5 2.5 0 0 1 6.5 3H20v18H6.5A2.5 2.5 0 0 0 4 23", "M4 5.5v15", "M8 7h8", "M8 11h6"],
  analysis: ["M4 20V10", "M10 20V4", "M16 20v-7", "M22 20H2"],
  learning: ["M12 3l8 4.5-8 4.5-8-4.5L12 3z", "M4 12l8 4.5 8-4.5", "M4 16.5L12 21l8-4.5"],
  practice: ["M4 4h11l5 5v11H4z", "M15 4v5h5", "M8 13h8", "M8 17h5"],
  tutor: ["M12 3a4 4 0 0 1 4 4v1a5 5 0 0 1 3 4.6V17a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-4.4A5 5 0 0 1 8 8V7a4 4 0 0 1 4-4z", "M9 12h.01", "M15 12h.01", "M9 16h6"],
  plus: ["M12 5v14", "M5 12h14"],
  upload: ["M12 16V4", "M7 9l5-5 5 5", "M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"],
  refresh: ["M20 6v5h-5", "M4 18v-5h5", "M20 11a8 8 0 0 0-14-4L4 9", "M4 13a8 8 0 0 0 14 4l2-2"],
  arrow: ["M5 12h14", "M13 6l6 6-6 6"],
  check: ["M5 12l4 4L19 6"],
  alert: ["M12 3l9 17H3L12 3z", "M12 9v5", "M12 17h.01"],
  info: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 11v5", "M12 8h.01"],
  search: ["M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z", "M21 21l-4.3-4.3"],
  dot: ["M12 12h.01"],
  close: ["M6 6l12 12", "M18 6L6 18"],
  spark: ["M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z"],
};

export function toneBadge(text, tone = "neutral") {
  return h("span", { class: `badge badge-${tone}`, text });
}

export function button(label, { onClick, variant = "ghost", type = "button", disabled = false, iconName } = {}) {
  return h(
    "button",
    {
      class: `button button-${variant}`,
      type,
      disabled,
      onClick,
    },
    iconName ? icon(iconName) : null,
    h("span", { text: label }),
  );
}

export function externalLink(label, href) {
  return h(
    "a",
    { class: "text-link", href, target: "_blank", rel: "noopener noreferrer" },
    label,
    icon("arrow"),
  );
}
