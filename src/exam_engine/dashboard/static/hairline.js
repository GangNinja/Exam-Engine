/**
 * Isolated adapter around Hairline's framework-free DOM API.
 *
 * Hairline (MIT, pinned 0.5.1) provides decorative interactive illustrations.
 * It is loaded lazily, never on its own, never with autoplay, and a failure here
 * only hides the illustration. The primary workspace is unaffected.
 */
import { h } from "./dom.js";

export const HAIRLINE_VERSION = "0.5.1";

const FIGURES = new Set([
  "riffle",
  "terrain",
  "exploded",
  "phosphor",
  "slow",
  "turntable",
  "keyboard",
  "elevator",
  "phone",
  "laptop",
  "terminal",
  "cabinet",
  "branches",
  "vault",
  "lockers",
  "padlock",
  "patch",
  "dish",
  "router",
  "loupe",
  "sieve",
  "rail",
  "plug",
  "query",
  "drawer",
  "basket",
  "plot",
  "hub",
  "relay",
  "settle",
  "format",
  "rebuild",
  "stack",
]);

let modulePromise = null;

function loadHairline() {
  if (!modulePromise) {
    modulePromise = import("./vendor/hairline/index.js").catch((error) => {
      modulePromise = null;
      throw error;
    });
  }
  return modulePromise;
}

/**
 * Mount one figure. Returns a cleanup function; safe to call after unmount.
 */
export async function mountIllustration(
  container,
  name,
  { label = "Decorative isometric illustration", intensity = 0.35 } = {},
) {
  if (!container || !FIGURES.has(name)) {
    markUnavailable(container);
    return () => {};
  }
  let disposed = false;
  let figure = null;

  try {
    const module = await loadHairline();
    if (disposed) return () => {};
    const factory = module[name];
    if (typeof factory !== "function") throw new Error(`Hairline has no figure ${name}`);
    figure = factory(container, {
      intensity,
      theme: "auto",
      label,
      play: false,
    });
    container.dataset.illustration = name;
    container.dataset.illustrationVersion = HAIRLINE_VERSION;
  } catch (error) {
    markUnavailable(container);
    if (typeof console !== "undefined" && console.debug) {
      console.debug("Hairline illustration unavailable:", error);
    }
  }

  return () => {
    disposed = true;
    try {
      figure?.destroy?.();
    } catch (error) {
      console.debug("Hairline cleanup failed:", error);
    }
    figure = null;
    container.replaceChildren();
  };
}

export function illustrationBlock(ctx, name, { label, caption }) {
  // Hairline owns the host's role and accessible name. Adding our own label
  // here would hide the specific description it installs.
  const mount = h("div", { class: "hairline-mount" });
  const wrapper = h(
    "aside",
    { class: "illustration", "aria-label": caption || "Decorative illustration" },
    mount,
    h(
      "p",
      { class: "illustration-caption" },
      h("span", { class: "illustration-tag", text: "Illustration" }),
      h("span", { text: caption || "Decorative only — not a data chart." }),
    ),
  );
  Promise.resolve().then(async () => {
    const cleanup = await mountIllustration(mount, name, { label });
    if (ctx?.addCleanup) ctx.addCleanup(cleanup);
    else if (!mount.isConnected) cleanup();
  });
  return wrapper;
}

function markUnavailable(container) {
  if (!container) return;
  container.classList.add("is-unavailable");
  container.removeAttribute("aria-hidden");
  if (!container.querySelector(".illustration-fallback")) {
    container.append(
      h("p", {
        class: "illustration-fallback",
        text: "Illustration unavailable. The workspace is unaffected.",
      }),
    );
  }
}

export function hairlineFigureNames() {
  return [...FIGURES];
}
