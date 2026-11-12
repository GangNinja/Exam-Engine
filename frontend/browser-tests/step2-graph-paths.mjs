import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.BASE_URL || "http://127.0.0.1:8765";
const outPath = "reports/step2_full_qa/evidence/graph-path-tests.json";
const executable = [
  process.env.BROWSER_EXECUTABLE,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].find((candidate) => candidate && existsSync(candidate));

const results = [];
let page;
let browser;

function record(kind, name, pass, detail = {}) {
  results.push({ kind, name, pass, detail, at: new Date().toISOString() });
}

async function waitReady() {
  await page.waitForFunction(
    () => {
      const heading = document.querySelector("main h1")?.textContent || "";
      return heading && !/^(Opening|Loading)/.test(heading);
    },
    { timeout: 45000 },
  );
  await page.waitForFunction(
    () => !document.querySelector("main .loading-panel, main .boot-panel"),
    { timeout: 45000 },
  );
  await page.waitForTimeout(300);
}

async function navigate(hash) {
  await page.goto(`${base}/?qa=graph-test#${hash}`, { waitUntil: "domcontentloaded" });
  await waitReady();
}

async function graphControls() {
  await navigate("/c/gate-physics/learning");
  await page.waitForSelector(".concept-button", { timeout: 30000 });
  const initial = await page.evaluate(() => ({
    count: document.querySelectorAll(".concept-button").length,
    active: document.querySelector(".concept-button.is-active")?.innerText.trim() || "",
    caption: document.querySelector("main table caption")?.innerText || "",
    rows: document.querySelectorAll("main table tbody tr").length,
    note: [...document.querySelectorAll("main .truncation-note")].map((node) => node.innerText),
  }));
  record("graph", "initial-load", initial.count > 100 && initial.rows > 0, initial);

  const concepts = await page.evaluate(() =>
    [...document.querySelectorAll(".concept-button")].map((button) => ({
      id: button.dataset.nodeId,
      label: button.querySelector("span")?.innerText.trim() || "",
    })),
  );
  let passed = 0;
  for (const concept of concepts) {
    try {
      const locator = page.locator(`.concept-button[data-node-id="${concept.id}"]`);
      await locator.click({ timeout: 8000 });
      await page.waitForFunction(
        (label) => {
          const node = document.querySelector(".concept-detail .concept-heading h3");
          return node?.textContent.trim() === label && !document.querySelector(".concept-detail .inline-loading");
        },
        concept.label,
        { timeout: 15000 },
      );
      const detail = await page.evaluate(() => ({
        heading: document.querySelector(".concept-detail .concept-heading h3")?.textContent.trim() || "",
        text: document.querySelector(".concept-detail")?.innerText.slice(0, 900) || "",
        error: document.querySelectorAll(".concept-detail .error-state").length,
      }));
      const ok = detail.heading === concept.label && detail.error === 0 && detail.text.includes("SELECTED CONCEPT");
      if (ok) passed += 1;
      else record("graph-concept", concept.label, false, { conceptId: concept.id, detail });
    } catch (error) {
      record("graph-concept", concept.label, false, { conceptId: concept.id, error: error.message });
    }
  }
  record("graph", "all-concepts", passed === concepts.length, {
    tested: concepts.length,
    passed,
    failed: concepts.length - passed,
  });

  const table = await page.evaluate(() => ({
    caption: document.querySelector("main table caption")?.innerText || "",
    headers: [...document.querySelectorAll("main table thead th")].map((node) => node.innerText),
    rows: document.querySelectorAll("main table tbody tr").length,
    statuses: [...document.querySelectorAll("main table tbody tr td:nth-child(3)")].map((node) => node.innerText.trim()).slice(0, 10),
    selfLoops: [...document.querySelectorAll("main table tbody tr")].filter((row) => {
      const cells = row.querySelectorAll("td");
      return cells[0] && cells[1] && cells[0].innerText.trim() === cells[1].innerText.trim();
    }).length,
  }));
  record("graph", "relationship-table", table.rows === 100 && table.caption.toLowerCase().includes("100 of 167"), table);

  const search = page.getByLabel("Filter concepts");
  await search.fill("quantum");
  await page.waitForTimeout(250);
  const filtered = await page.evaluate(() => ({
    shown: document.querySelectorAll(".concept-button").length,
    noMatch: [...document.querySelectorAll(".concept-list p")].some((node) => node.innerText.includes("No concept matches")),
  }));
  await search.fill("");
  await page.waitForTimeout(250);
  record("graph", "concept-filter", filtered.shown > 0 && filtered.shown < concepts.length && !filtered.noMatch, filtered);

  await page.locator(".concept-button").first().focus();
  await page.keyboard.press("End");
  await page.waitForTimeout(300);
  const keyboard = await page.evaluate(() => ({
    activeText: document.querySelector(".concept-button.is-active")?.innerText.trim() || "",
    focusedText: document.activeElement?.innerText?.trim() || "",
    detail: document.querySelector(".concept-detail .concept-heading h3")?.textContent.trim() || "",
  }));
  record("graph", "keyboard-navigation", keyboard.focusedText.startsWith(keyboard.activeText.split("\n")[0]), keyboard);
  await page.keyboard.press("Home");
  await page.waitForTimeout(250);

  const graphData = await fetch(`${base}/api/courses/gate-physics/graph?limit=1200`).then((response) => response.json());
  const edges = graphData.prerequisite_edges || [];
  const selfLoops = edges.filter((edge) => edge.source_node_id === edge.target_node_id).length;
  const candidate = edges.filter((edge) => edge.status === "candidate").length;
  const verified = edges.filter((edge) => edge.status !== "candidate").length;
  record("graph", "edge-integrity", selfLoops === 0 && candidate + verified === edges.length && candidate > 0, {
    total: edges.length,
    candidate,
    verified,
    selfLoops,
    allCandidate: verified === 0,
  });

  const cycleNodes = new Map();
  const adjacency = new Map();
  for (const edge of edges.filter((edge) => edge.status !== "candidate")) {
    if (!adjacency.has(edge.source_node_id)) adjacency.set(edge.source_node_id, []);
    adjacency.get(edge.source_node_id).push(edge.target_node_id);
  }
  const visiting = new Set();
  const visited = new Set();
  let cycle = null;
  function visit(node) {
    if (visiting.has(node)) {
      cycle = [...visiting, node];
      return true;
    }
    if (visited.has(node)) return false;
    visiting.add(node);
    cycleNodes.set(node, true);
    for (const next of adjacency.get(node) || []) {
      if (visit(next)) return true;
    }
    visiting.delete(node);
    visited.add(node);
    return false;
  }
  for (const node of adjacency.keys()) {
    if (visit(node)) break;
  }
  record("graph", "no-verified-cycles", cycle === null, { verifiedEdges: verified, cycle });
}

async function pathStudio() {
  await navigate("/c/gate-physics/learning/paths");
  await page.waitForSelector("#path-target", { timeout: 30000 });
  const initial = await page.evaluate(() => ({
    targetOptions: document.querySelector("#path-target")?.options.length || 0,
    checkbox: document.querySelector("[id^=candidates-]")?.checked,
    depth: document.querySelector("[id^=depth-]")?.value,
    button: [...document.querySelectorAll("main button")].map((node) => node.innerText.trim()),
  }));
  record("learning-path", "conservative-default", initial.checkbox === false, initial);

  const target = await page.locator("#path-target").inputValue();
  const responsePromise = page.waitForResponse(
    (response) => response.url().includes("/learning-path") && response.request().method() === "POST",
    { timeout: 60000 },
  );
  await page.getByRole("button", { name: "Generate path" }).click();
  const firstResponse = await responsePromise;
  const firstPlan = await firstResponse.json();
  await page.waitForTimeout(500);
  const first = await page.evaluate(() => ({
    status: document.querySelector(".form-status")?.innerText || "",
    tableRows: document.querySelectorAll("main table tbody tr").length,
    headings: [...document.querySelectorAll("main h2, main h3")].map((node) => node.innerText).slice(-10),
    body: document.querySelector("main")?.innerText.slice(-1200) || "",
  }));
  const firstHasCandidates = (firstPlan.steps || []).some((step) => step.status === "candidate");
  record("learning-path", "default-generation", !firstHasCandidates, {
    target,
    status: firstPlan.status,
    steps: (firstPlan.steps || []).length,
    candidateSteps: (firstPlan.steps || []).filter((step) => step.status === "candidate").length,
    limitations: firstPlan.limitations,
    first,
  });

  await page.locator("[id^=candidates-]").check();
  const secondResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/learning-path") && response.request().method() === "POST",
    { timeout: 60000 },
  );
  await page.getByRole("button", { name: "Generate path" }).click();
  const secondResponse = await secondResponsePromise;
  const secondPlan = await secondResponse.json();
  await page.waitForTimeout(400);
  record("learning-path", "candidate-opt-in", Boolean(secondPlan), {
    status: secondPlan.status,
    steps: (secondPlan.steps || []).length,
    candidateSteps: (secondPlan.steps || []).filter((step) => step.status === "candidate").length,
    limitations: secondPlan.limitations,
  });

  const invalid = await page.request.post(
    `${base}/api/courses/gate-physics/learning-path`,
    { data: { target_node_id: "nod_does_not_exist", include_candidates: false, max_depth: 4 } },
  );
  const invalidBody = await invalid.json();
  record("learning-path", "invalid-target", invalid.status() === 200 && invalidBody.status === "invalid_target" && (invalidBody.steps || []).length === 0, {
    status: invalid.status(),
    bodyStatus: invalidBody.status,
    steps: (invalidBody.steps || []).length,
  });
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  browser = await chromium.launch({ executablePath: executable, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await graphControls();
    await pathStudio();
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    consoleErrors: errors,
    summary: {
      total: results.length,
      pass: results.filter((item) => item.pass).length,
      fail: results.filter((item) => !item.pass).length,
    },
    results,
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify(output.summary));
  const failures = results.filter((item) => !item.pass);
  if (failures.length) console.log(JSON.stringify(failures, null, 2).slice(0, 16000));
  if (failures.length) process.exitCode = 1;
}

await main();
