/**
 * Original SVG tutor avatar — written for this project, MIT, no third-party
 * avatar studio code or exported assets. It is a presentation component only:
 * it receives a state and a text caption, and never computes an assessment.
 */
import { h, svg, clear } from "./dom.js";
import { TUTOR_STATES } from "./ui-state.js";

const CAPTIONS = {
  idle: "Idle — ready for a course question.",
  thinking: "Thinking — searching course evidence and live sources.",
  speaking: "Explaining — using cited evidence and subject knowledge.",
  positive: "Completed — the backend returned a real result.",
  corrective: "Needs attention — the question or evidence is incomplete.",
  warning: "Unavailable — check the tutor model or retry.",
};

export function createTutorAvatar(container) {
  if (!container) throw new Error("createTutorAvatar needs a container");
  clear(container);

  const caption = h("p", { class: "avatar-caption", text: CAPTIONS.idle });
  const badge = h(
    "span",
    { class: "avatar-badge", text: "!" },
  );
  const root = h(
    "div",
    {
      class: "tutor-avatar",
      dataset: { state: "idle" },
      role: "img",
      "aria-label": `Tutor avatar. ${CAPTIONS.idle}`,
    },
    buildSvg(),
    caption,
    badge,
  );
  container.append(root);

  let current = "idle";
  let destroyed = false;

  function setState(state, message) {
    if (destroyed) return;
    const next = TUTOR_STATES.includes(state) ? state : "idle";
    current = next;
    const text = message ? String(message) : CAPTIONS[next];
    root.dataset.state = next;
    root.setAttribute("aria-label", `Tutor avatar, ${next}. ${text}`);
    caption.textContent = text;
    badge.hidden = next !== "warning";
  }

  function destroy() {
    destroyed = true;
    container.replaceChildren();
  }

  return {
    setState,
    getIdleState: () => current,
    destroy,
  };
}

function buildSvg() {
  return svg(
    "svg",
    {
      class: "tutor-svg",
      viewBox: "0 0 360 340",
      role: "presentation",
      focusable: "false",
      "aria-hidden": "true",
    },
    svg(
      "g",
      { class: "avatar-orbit" },
      svg("circle", { cx: 180, cy: 154, r: 146, class: "orbit-ring" }),
      svg("circle", { cx: 45, cy: 92, r: 4.5, class: "orbit-dot" }),
      svg("circle", { cx: 318, cy: 120, r: 3.5, class: "orbit-dot" }),
      svg("circle", { cx: 302, cy: 258, r: 4, class: "orbit-dot" }),
      svg("path", { d: "M66 82l24 18M298 104l-14 10", class: "orbit-spoke" }),
    ),
    svg("ellipse", { cx: 180, cy: 322, rx: 92, ry: 10, class: "avatar-shadow" }),
    svg("g", { class: "thinking-dots" },
      svg("circle", { cx: 152, cy: 30, r: 4 }),
      svg("circle", { cx: 180, cy: 24, r: 4 }),
      svg("circle", { cx: 208, cy: 30, r: 4 }),
    ),
    svg("path", {
      d: "M74 326c4-54 30-88 68-98h76c38 10 64 44 68 98z",
      class: "avatar-body",
    }),
    svg("path", { d: "M150 214l30 34 30-34", class: "avatar-collar" }),
    svg("rect", { x: 158, y: 192, width: 44, height: 34, rx: 14, class: "avatar-neck" }),
    svg(
      "g",
      { class: "avatar-head" },
      svg("rect", { x: 104, y: 58, width: 152, height: 154, rx: 56, class: "avatar-face" }),
      svg("path", {
        d: "M104 122c-4-48 28-76 80-76 40 0 68 20 72 62-22-12-40-18-66-16-34 2-58 12-86 30z",
        class: "avatar-hair",
      }),
      svg("path", { d: "M104 140c-10-2-14 6-10 16 4 9 12 12 18 8", class: "avatar-ear" }),
      svg("path", { d: "M256 140c10-2 14 6 10 16-4 9-12 12-18 8", class: "avatar-ear" }),
      svg("g", { class: "avatar-brows" },
        svg("path", { d: "M128 116c10-7 22-7 32 0", class: "brow brow-left" }),
        svg("path", { d: "M200 116c10-7 22-7 32 0", class: "brow brow-right" }),
      ),
      svg("g", { class: "avatar-glasses" },
        svg("circle", { cx: 146, cy: 138, r: 27 }),
        svg("circle", { cx: 214, cy: 138, r: 27 }),
        svg("path", { d: "M173 138h14M119 134l-12 4M241 134l12 4" }),
      ),
      svg("g", { class: "avatar-eye-wrap eye-left" },
        svg("ellipse", { cx: 146, cy: 138, rx: 8.5, ry: 10.5, class: "avatar-eye" }),
        svg("circle", { cx: 148, cy: 139, r: 3.6, class: "avatar-pupil" }),
      ),
      svg("g", { class: "avatar-eye-wrap eye-right" },
        svg("ellipse", { cx: 214, cy: 138, rx: 8.5, ry: 10.5, class: "avatar-eye" }),
        svg("circle", { cx: 216, cy: 139, r: 3.6, class: "avatar-pupil" }),
      ),
      svg("path", { d: "M181 146c-2 8-5 12-1 15", class: "avatar-nose" }),
      svg("g", { class: "avatar-cheeks" },
        svg("ellipse", { cx: 126, cy: 166, rx: 11, ry: 6 }),
        svg("ellipse", { cx: 234, cy: 166, rx: 11, ry: 6 }),
      ),
      svg("path", { d: "M162 174c12 5 24 5 36 0", class: "mouth mouth-neutral" }),
      svg("path", { d: "M158 170c14 16 30 16 44 0", class: "mouth mouth-smile" }),
      svg("path", { d: "M162 176h36", class: "mouth mouth-flat" }),
      svg("path", { d: "M164 170c10 12 22 12 32 0", class: "mouth mouth-speak" }),
      svg("g", { class: "avatar-warning-badge" },
        svg("path", { d: "M252 52l22 38h-44z", class: "warning-triangle" }),
        svg("path", { d: "M252 64v12M252 82h.01", class: "warning-mark" }),
      ),
    ),
  );
}
