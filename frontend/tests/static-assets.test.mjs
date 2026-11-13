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

test("tutor distinguishes an offline evidence summary from a model answer", async () => {
  const tutor = await readFile(resolve(staticDir, "views/tutor.js"), "utf8");
  assert.match(tutor, /Offline teacher answer from stored syllabus and past-paper evidence\./);
  assert.match(tutor, /response\.status === "answered"\s*\? response\.ai/);
});

test("upload errors do not append a literal null when no issue list is present", async () => {
  const course = await readFile(resolve(staticDir, "views/course.js"), "utf8");
  assert.match(course, /const errorDetails = error\?\.detail\?\.result\?\.errors;/);
  const errorBlock = course.slice(
    course.indexOf("const errorDetails"),
    course.indexOf("status.replaceChildren(...children)"),
  );
  assert.ok(errorBlock.length > 0);
  assert.doesNotMatch(errorBlock, /:\s*null/);
  assert.match(course, /status\.replaceChildren\(\.\.\.children\);/);
});

test("chart labels are not outlined by the chart-grid layout container", async () => {
  const charts = await readFile(resolve(staticDir, "charts.js"), "utf8");
  const css = await readFile(resolve(staticDir, "styles-v2.css"), "utf8");
  const baseCss = await readFile(resolve(staticDir, "styles.css"), "utf8");

  assert.match(charts, /class: "chart-grid-lines"/);
  assert.doesNotMatch(charts, /class: "chart-grid"/);
  assert.match(css, /\.chart-grid-lines\s*\{[^}]*stroke:\s*var\(--line\)/s);
  assert.match(css, /\.chart-label\s*\{[^}]*stroke:\s*none/s);
  assert.match(css, /\.chart-value\s*\{[^}]*stroke:\s*none/s);
  assert.match(baseCss, /\.chart-grid-lines\s*\{[^}]*stroke:\s*var\(--line\)/s);
});

test("coverage groups concepts as covered or low coverage without marks or extra statuses", async () => {
  const analysis = await readFile(resolve(staticDir, "views/analysis.js"), "utf8");
  const start = analysis.indexOf("function renderCoverageReport");
  const end = analysis.indexOf("// -- distribution", start);
  const block = analysis.slice(start, end);
  assert.match(block, /Coverage by unit/);
  assert.match(block, /Low coverage/);
  assert.match(block, /Question mappings/);
  assert.match(block, /safeArray\(report\.question_assignments\)/);
  assert.match(block, /barChart\(/);
  assert.doesNotMatch(block, /stackedBar\(/);
  assert.doesNotMatch(block, /\bUnmapped\b|\bUncertain\b|\bInsufficient\b/i);
  assert.doesNotMatch(block, /Known marks|Mapped marks|Missing marks|marks/i);
});

test("evolution uses dated windows and readable change explanations", async () => {
  const analysis = await readFile(resolve(staticDir, "views/analysis.js"), "utf8");
  assert.match(analysis, /function changeSideSummary/);
  const start = analysis.indexOf("function renderEvolutionReport");
  const block = analysis.slice(start);
  assert.match(block, /Earlier window/);
  assert.match(block, /Later window/);
  assert.doesNotMatch(block, /row\.earlier\.document_title/);
  assert.doesNotMatch(block, /row\.later\.document_title/);
});

test("the marks panel reports coverage when no stated allocations exist", async () => {
  const analysis = await readFile(resolve(staticDir, "views/analysis.js"), "utf8");
  assert.match(analysis, /const hasAllocations = allocations\.length > 0;/);
  assert.match(analysis, /Marks data coverage/);
  assert.match(analysis, /Marks not stated/);
  assert.match(analysis, /no marks are inferred/);
  assert.match(analysis, /valueFormat: \(value\) => formatInteger\(value\)/);
});

test("the generated learning path is mounted outside the concept-detail column", async () => {
  const learning = await readFile(resolve(staticDir, "views/learning.js"), "utf8");
  const css = await readFile(resolve(staticDir, "styles-v2.css"), "utf8");

  assert.match(learning, /const pathResultHost = h\("div", \{ class: "path-result-host path-result-wide"/);
  assert.match(learning, /wrap\.append\(pathResultHost\);/);
  assert.match(learning, /renderConceptDetail\(ctx, graph, concept, neighborhood, pathResultHost\)/);
  assert.match(css, /\.path-result-wide\s*\{[^}]*width:\s*100%/s);
});
