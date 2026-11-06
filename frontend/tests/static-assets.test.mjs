import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  HAIRLINE_VERSION,
  hairlineFigureNames,
} from "../../src/exam_engine/dashboard/static/hairline.js";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = resolve(here, "../../src/exam_engine/dashboard/static");

test("Hairline is pinned and only the decorative figures in use are exposed", () => {
  assert.equal(HAIRLINE_VERSION, "0.5.1");
  const figures = hairlineFigureNames();
  assert.ok(figures.includes("hub"));
  assert.ok(figures.includes("branches"));
  assert.ok(figures.includes("riffle"));
  assert.equal(new Set(figures).size, figures.length);
});

test("index is local-only, has a skip link and loads a module script", async () => {
  const html = await readFile(resolve(staticDir, "index.html"), "utf8");
  assert.match(html, /class="skip-link" href="#main"/);
  assert.match(html, /<script type="module" src="\/assets\/app\.js">/);
  assert.match(html, /id="main" tabindex="-1"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /<noscript>/);
  assert.doesNotMatch(html, /https?:\/\//);
  const remoteReferences = [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
    .map((match) => match[1])
    .filter((reference) => !reference.startsWith("#") && !reference.startsWith("/assets/"));
  assert.deepEqual(remoteReferences, []);
});

test("reduced motion is explicitly handled for CSS and avatar animation", async () => {
  const css = await readFile(resolve(staticDir, "styles.css"), "utf8");
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /animation-duration: 0\.001ms !important/);
  assert.match(css, /\.tutor-avatar\[data-state="idle"\] \.avatar-eye-wrap/);
  const avatar = await readFile(resolve(staticDir, "tutor-avatar.js"), "utf8");
  assert.match(avatar, /role: "presentation"/);
  assert.match(avatar, /"aria-hidden": "true"/);
  assert.match(avatar, /CAPTIONS/);
});

test("the dashboard does not contain inline scripts that CSP would block", async () => {
  const html = await readFile(resolve(staticDir, "index.html"), "utf8");
  const inline = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>/g)];
  assert.deepEqual(inline, []);
});

test("Hairline provenance records the licence and vendored bytes", async () => {
  const provenance = JSON.parse(
    await readFile(resolve(staticDir, "vendor/hairline/PROVENANCE.json"), "utf8"),
  );
  assert.equal(provenance.package, "@lucasmarkes/hairline");
  assert.equal(provenance.version, HAIRLINE_VERSION);
  assert.equal(provenance.license, "MIT");
  assert.match(provenance.sha256, /^[a-f0-9]{64}$/);
  assert.equal(provenance.runtime_network_access, false);
});
