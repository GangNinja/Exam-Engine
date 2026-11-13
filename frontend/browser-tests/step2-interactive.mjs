import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.BASE_URL || "http://127.0.0.1:8765";
const outPath = "reports/step2_full_qa/evidence/interaction-tests.json";

function executable() {
  return [
    process.env.BROWSER_EXECUTABLE,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].find((candidate) => candidate && existsSync(candidate));
}

const results = [];
let browser;
let page;
let consoleErrors = [];

function record(kind, name, pass, detail = {}) {
  results.push({ kind, name, pass, detail, at: new Date().toISOString() });
}

async function waitReady() {
  await page.waitForFunction(
    () => {
      const database = document.querySelector("#db-name")?.textContent || "";
      const heading = document.querySelector("main h1")?.textContent || "";
      const loading = /^(Opening the local database|Loading course workspace)/.test(heading.trim());
      return database && !database.includes("connecting to database") && heading && !loading;
    },
    { timeout: 45000 },
  );
  await page.waitForFunction(
    () => !document.querySelector("main .loading-panel, main .boot-panel"),
    { timeout: 45000 },
  );
  await page.waitForTimeout(350);
}

async function navigate(hash) {
  await page.goto(`${base}/?qa=interactive#${hash}`, {
    waitUntil: "domcontentloaded",
    timeout: 45000,
  });
  await waitReady();
}

async function applyFilters(responsePart) {
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().includes(responsePart) &&
      response.status() === 200,
    { timeout: 30000 },
  );
  await page.getByRole("button", { name: "Apply filters" }).click();
  const response = await responsePromise;
  const payload = await response.json();
  await page.waitForFunction(
    () => {
      const button = [...document.querySelectorAll("main button")].find((item) =>
        item.textContent.includes("Apply filters"),
      );
      return button && !button.disabled;
    },
    { timeout: 20000 },
  );
  return payload;
}

async function readFilterState() {
  return page.evaluate(() => ({
    status: [...document.querySelectorAll("main [role=status]")].map((node) => node.innerText.trim()),
    summary: document.querySelector("main")?.innerText.match(/Showing[^\n]*/)?.[0] || null,
    articles: document.querySelectorAll("main article").length,
    empty: document.querySelectorAll("main .empty-state").length,
    error: document.querySelectorAll("main .error-state").length,
  }));
}

async function resetFilters() {
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      (response.url().includes("/api/exams/questions?") || response.url().includes("/api/exams?")),
    { timeout: 30000 },
  );
  await page.getByRole("button", { name: "Reset" }).first().click();
  await responsePromise.catch(() => null);
  await page.waitForTimeout(300);
}

async function testCourseIsolation() {
  const coursesResponse = await fetch(`${base}/api/courses`);
  const catalog = await coursesResponse.json();
  const courses = catalog.courses || [];
  record("course-isolation", "catalog", courses.length === 29, { count: courses.length });
  let previousTitle = null;
  for (const course of courses) {
    try {
      await navigate(`/c/${encodeURIComponent(course.course_id)}/overview`);
      const state = await page.evaluate(() => ({
        heading: document.querySelector("main h1")?.textContent?.trim() || "",
        selected: document.getElementById("course-select")?.value || "",
        main: document.querySelector("main")?.innerText || "",
        tables: document.querySelectorAll("main table").length,
        error: document.querySelectorAll("main .error-state").length,
      }));
      const pass =
        state.selected === course.course_id &&
        state.heading === course.title &&
        state.error === 0 &&
        state.main.includes(course.title);
      record("course-isolation", course.course_id, pass, {
        expectedTitle: course.title,
        heading: state.heading,
        selected: state.selected,
        tables: state.tables,
        error: state.error,
        previousTitleStillPresent:
          Boolean(previousTitle) && previousTitle !== course.title && state.main.includes(previousTitle),
      });
      previousTitle = course.title;
    } catch (error) {
      record("course-isolation", course.course_id, false, {
        expectedTitle: course.title,
        error: error.message,
      });
    }
  }
}

async function testQuestionBankFilters() {
  await navigate("/exams/questions");
  const baseState = await readFilterState();
  record("question-bank", "default-state", baseState.articles === 50, baseState);

  const cases = [
    ["category=gate", () => page.getByLabel("Examination category").selectOption("gate")],
    ["subject=gate-physics", () => page.getByLabel("Subject", { exact: true }).selectOption("gate-physics")],
    ["year=2024", () => page.getByLabel("Examination year").selectOption("2024")],
    ["type=mcq", () => page.getByLabel("Question type").selectOption("mcq")],
    ["answer=mcq_option", () => page.getByLabel("Answer format").selectOption("mcq_option")],
  ];
  const expected = {
    "category=gate": 5905,
    "subject=gate-physics": 447,
  };
  for (const [name, set] of cases) {
    try {
      await resetFilters();
      await set();
      const payload = await applyFilters("/api/exams/questions?");
      const state = await readFilterState();
      const pass =
        expected[name] === undefined
          ? payload.total > 0 && state.error === 0
          : payload.total === expected[name] &&
            state.status.join(" ").replaceAll(",", "").includes(String(expected[name]));
      record("question-bank-filter", name, pass, {
        total: payload.total,
        shown: payload.shown,
        state,
        filters: payload.filters_applied,
      });
    } catch (error) {
      record("question-bank-filter", name, false, { error: error.message });
    }
  }

  try {
    await resetFilters();
    await page.getByRole("button", { name: /^Load 50 more$/ }).click();
    await page.waitForTimeout(700);
    const after = await readFilterState();
    record("question-bank", "load-more", after.articles === 100, after);
  } catch (error) {
    record("question-bank", "load-more", false, { error: error.message });
  }

  try {
    await resetFilters();
    await page.getByLabel("Examination category").selectOption("gate");
    await page.getByLabel("Examination year").selectOption("2024");
    await page.getByLabel("Question type").selectOption("mcq");
    const payload = await applyFilters("/api/exams/questions?");
    const state = await readFilterState();
    record("filter-combination", "gate+2024+mcq", payload.total > 0 && state.error === 0, {
      total: payload.total,
      state,
      filters: payload.filters_applied,
    });
  } catch (error) {
    record("filter-combination", "gate+2024+mcq", false, { error: error.message });
  }

  try {
    await resetFilters();
    await page.getByPlaceholder("Search question or answer text").fill("zzzz-no-such-question");
    const payload = await applyFilters("/api/exams/questions?");
    const state = await readFilterState();
    record("question-bank", "empty-search", payload.total === 0 && state.empty > 0, {
      total: payload.total,
      state,
    });
  } catch (error) {
    record("question-bank", "empty-search", false, { error: error.message });
  }

  try {
    await resetFilters();
    const state = await readFilterState();
    const payload = await fetch(`${base}/api/exams/questions?limit=50`).then((response) => response.json());
    record("question-bank", "reset", state.articles === 50 && payload.total === 15432, {
      state,
      total: payload.total,
    });
  } catch (error) {
    record("question-bank", "reset", false, { error: error.message });
  }
}

async function testExaminationExplorer() {
  await navigate("/exams");
  const initial = await page.evaluate(() => ({
    heading: document.querySelector("main h1")?.textContent?.trim(),
    selects: document.querySelectorAll("main select").length,
    error: document.querySelectorAll("main .error-state").length,
  }));
  record("exams", "default-state", initial.heading === "Examination explorer" && initial.selects === 7, initial);
  try {
    await page.getByLabel("Examination category").selectOption("gate");
    const responsePromise = page.waitForResponse(
      (response) => response.url().includes("/api/exams?") && response.status() === 200,
      { timeout: 30000 },
    );
    await page.getByRole("button", { name: "Apply filters" }).click();
    const response = await responsePromise;
    const payload = await response.json();
    const state = await page.evaluate(() => ({
      error: document.querySelectorAll("main .error-state").length,
      main: document.querySelector("main")?.innerText.slice(0, 500),
    }));
    record("exams-filter", "category=gate", payload.summary?.questions === 5905 && state.error === 0, {
      questions: payload.summary?.questions,
      state,
    });
  } catch (error) {
    record("exams-filter", "category=gate", false, { error: error.message });
  }
}

async function testAnalysisAndCoverage() {
  await navigate("/c/gate-physics/analysis/distribution");
  const initial = await page.evaluate(() => ({
    heading: document.querySelector("main h1")?.textContent?.trim(),
    charts: document.querySelectorAll("main figure.chart").length,
    tables: document.querySelectorAll("main table").length,
    error: document.querySelectorAll("main .error-state").length,
  }));
  record("paper-analyzer", "load-and-data-tables", initial.heading === "Question paper analyzer" && initial.charts >= 4 && initial.error === 0, initial);

  await navigate("/c/gate-physics/analysis/coverage");
  const coverage = await page.evaluate(() => ({
    heading: document.querySelector("main h1")?.textContent?.trim(),
    charts: document.querySelectorAll("main figure.chart").length,
    tables: document.querySelectorAll("main table").length,
    buttons: [...document.querySelectorAll("main button")].map((button) => button.innerText.trim()),
    text: document.querySelector("main")?.innerText.slice(0, 1500),
  }));
  record("coverage", "load", coverage.charts === 1 && coverage.tables >= 6 && coverage.error !== true, coverage);

  for (const name of ["COVERED", "LOW COVERAGE", "UNMAPPED", "UNCERTAIN", "INSUFFICIENT DATA", "ALL"]) {
    try {
      const button = page.locator("main .filter-chip").filter({ hasText: new RegExp(`^${name}`, "i") });
      await button.click();
      await page.waitForTimeout(500);
      const state = await page.evaluate(() => ({
        rows: document.querySelectorAll("main table tbody tr").length,
        empty: document.querySelectorAll("main .empty-state").length,
        error: document.querySelectorAll("main .error-state").length,
        active: [...document.querySelectorAll("main button")].filter((button) =>
          button.getAttribute("aria-pressed") === "true" || button.classList.contains("is-active"),
        ).map((button) => button.innerText.trim()),
      }));
      const expectsRows = !["LOW COVERAGE", "INSUFFICIENT DATA"].includes(name);
      record("coverage-filter", name, state.error === 0 && (state.rows > 0 || state.empty > 0 || !expectsRows), state);
    } catch (error) {
      record("coverage-filter", name, false, { error: error.message });
    }
  }

  for (const hash of [
    "/c/gate-physics/analysis/exam-dna",
    "/c/gate-physics/analysis/evolution",
    "/c/gate-physics/learning",
    "/c/gate-physics/learning/paths",
  ]) {
    try {
      await navigate(hash);
      const state = await page.evaluate(() => ({
        heading: document.querySelector("main h1")?.textContent?.trim(),
        charts: document.querySelectorAll("main figure.chart").length,
        tables: document.querySelectorAll("main table").length,
        buttons: [...document.querySelectorAll("main button")].map((button) => button.innerText.trim()),
        error: document.querySelectorAll("main .error-state").length,
      }));
      record("analysis-route", hash, state.error === 0 && Boolean(state.heading), state);
    } catch (error) {
      record("analysis-route", hash, false, { error: error.message });
    }
  }
}

async function main() {
  const browserPath = executable();
  if (!browserPath) throw new Error("No Chrome/Edge executable found");
  browser = await chromium.launch({ executablePath: browserPath, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  try {
    await navigate("/dashboard");
    record("global", "dashboard-loaded", (await page.locator("main h1").innerText()).includes("clearer view"));

    const beforeTheme = await page.evaluate(() => ({
      theme: document.documentElement.dataset.theme,
      label: document.getElementById("theme-label")?.textContent,
      aria: document.getElementById("theme-toggle")?.getAttribute("aria-label"),
    }));
    await page.locator("#theme-toggle").click();
    const afterTheme = await page.evaluate(() => ({
      theme: document.documentElement.dataset.theme,
      label: document.getElementById("theme-label")?.textContent,
      aria: document.getElementById("theme-toggle")?.getAttribute("aria-label"),
    }));
    record("global", "theme-toggle", beforeTheme.theme !== afterTheme.theme && afterTheme.aria?.includes("Switch"), {
      beforeTheme,
      afterTheme,
    });
    await page.locator("#theme-toggle").click();

    await page.keyboard.press("Control+k");
    const dialogOpen = await page.locator("#command-dialog").evaluate((node) => node.open);
    await page.locator("#command-input").fill("physics");
    await page.waitForTimeout(250);
    const searchResults = await page.locator("#command-results [role=option]").count();
    await page.keyboard.press("Escape");
    const dialogClosed = await page.locator("#command-dialog").evaluate((node) => !node.open);
    record("global", "ctrl-k-search", dialogOpen && searchResults >= 5 && dialogClosed, {
      dialogOpen,
      searchResults,
      dialogClosed,
    });

    await page.locator("#primary-nav a[href='#/exams/questions']").click();
    await page.waitForFunction(() => location.hash === "#/exams/questions", { timeout: 15000 });
    await waitReady();
    record("global", "sidebar-navigation", (await page.locator("main h1").innerText()) === "Question bank", {
      url: page.url(),
    });

    await navigate("/dashboard");
    await page.locator("#sidebar-collapse").click();
    const collapsed = await page.evaluate(() => document.documentElement.classList.contains("rail-collapsed"));
    await page.locator("#sidebar-collapse").click();
    const expanded = await page.evaluate(() => !document.documentElement.classList.contains("rail-collapsed"));
    record("global", "sidebar-collapse", collapsed && expanded, { collapsed, expanded });

    await page.setViewportSize({ width: 390, height: 844 });
    await navigate("/dashboard");
    await page.getByRole("button", { name: "Open navigation" }).click();
    const drawerOpen = await page.evaluate(() => document.body.classList.contains("sidebar-open"));
    await page.keyboard.press("Escape");
    const drawerClosed = await page.evaluate(() => !document.body.classList.contains("sidebar-open"));
    record("global", "mobile-drawer", drawerOpen && drawerClosed, { drawerOpen, drawerClosed });
    await page.setViewportSize({ width: 1440, height: 900 });

    await testCourseIsolation();
    await testQuestionBankFilters();
    await testExaminationExplorer();
    await testAnalysisAndCoverage();
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    consoleErrors,
    results,
    summary: {
      total: results.length,
      pass: results.filter((item) => item.pass).length,
      fail: results.filter((item) => !item.pass).length,
    },
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify(output.summary));
  const failures = results.filter((item) => !item.pass);
  if (failures.length) console.log(JSON.stringify(failures, null, 2).slice(0, 12000));
}

await main();
