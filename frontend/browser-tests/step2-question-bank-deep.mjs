import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.BASE_URL || "http://127.0.0.1:8765";
const outPath = "reports/step2_full_qa/evidence/question-bank-deep-tests.json";
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
let navCounter = 0;

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
  navCounter += 1;
  await page.goto(`${base}/?qa=question-bank-${navCounter}#${hash}`, { waitUntil: "domcontentloaded" });
  await waitReady();
}

async function applyFilters() {
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().includes("/api/exams/questions?") &&
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

async function state() {
  return page.evaluate(() => ({
    status: [...document.querySelectorAll("main [role=status]")].map((node) => node.innerText.trim()),
    summary: document.querySelector("main")?.innerText.match(/Showing[^\n]*/)?.[0] || null,
    articles: document.querySelectorAll("main article").length,
    passageGroups: document.querySelectorAll("main .passage-group").length,
    passageQuestions: document.querySelectorAll("main .passage-group article").length,
    errors: document.querySelectorAll("main .error-state").length,
    empty: document.querySelectorAll("main .empty-state").length,
  }));
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  browser = await chromium.launch({ executablePath: executable, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  const bad = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) bad.push({ status: response.status(), url: response.url() });
  });
  try {
    await navigate("/exams/questions?course=gate-physics");
    await page.waitForSelector("#course-select", { timeout: 20000 });
    const courseState = await page.evaluate(() => ({
      course: document.querySelector("#course-select")?.value,
      subject: document.querySelector('[aria-label="Subject"]')?.value,
      units: document.querySelector('[aria-label="Syllabus unit"]')?.options.length,
      topics: document.querySelector('[aria-label="Topic"]')?.options.length,
    }));
    record("question-bank", "course-scope-load", courseState.subject === "gate-physics" && courseState.units > 1, courseState);

    const syllabus = await fetch(`${base}/api/exams/syllabus?course=gate-physics`).then((response) => response.json());
    const unit = syllabus.syllabi[0].units.find((item) => item.topics?.length);
    const unitQuery = new URLSearchParams({ course: "gate-physics", unit_id: unit.unit_id, limit: "50" });
    const unitApi = await fetch(`${base}/api/exams/questions?${unitQuery}`).then((response) => response.json());
    await page.locator('[aria-label="Syllabus unit"]').selectOption(unit.unit_id);
    const unitPayload = await applyFilters();
    const unitOnlyState = await state();
    record("question-bank-filter", "unit", unitPayload.total === unitApi.total, {
      unit: unit.unit_id,
      uiTotal: unitPayload.total,
      apiTotal: unitApi.total,
      state: unitOnlyState,
    });
    const topicOptions = await page.locator('[aria-label="Topic"] option').evaluateAll((options) =>
      options.map((option) => ({ value: option.value, text: option.text })).filter((item) => item.value),
    );
    const chosenTopic = topicOptions[0];
    await page.locator('[aria-label="Topic"]').selectOption(chosenTopic.value);
    const payload = await applyFilters();
    const unitState = await state();
    const topicQuery = new URLSearchParams({
      course: "gate-physics",
      unit_id: unit.unit_id,
      topic: chosenTopic.value,
      limit: "50",
    });
    const topicApi = await fetch(`${base}/api/exams/questions?${topicQuery}`).then((response) => response.json());
    record("question-bank-filter", "topic", payload.total === topicApi.total && chosenTopic.value === (payload.filters_applied?.topic ?? chosenTopic.value), {
      topic: chosenTopic,
      uiTotal: payload.total,
      apiTotal: topicApi.total,
      topicOptions: topicOptions.length,
    });

    await navigate("/exams/questions?course=gate-physics");
    await page.locator('[aria-label="Examination year"]').selectOption("2025");
    const yearPayload = await applyFilters();
    const passageState = await state();
    record("question-bank-filter", "year=2025-passage-load", yearPayload.total === 65 && passageState.passageGroups > 0, {
      total: yearPayload.total,
      state: passageState,
    });
    if (passageState.passageGroups > 0) {
      const group = page.locator("main .passage-group").first();
      const passage = await group.evaluate((node) => ({
        id: node.dataset.passageId,
        text: node.querySelector(".passage-text")?.innerText || "",
        count: node.querySelector(".passage-question-count")?.innerText || "",
        articleCount: node.querySelectorAll("article").length,
        articleText: [...node.querySelectorAll("article")].map((article) => article.innerText.slice(0, 500)),
        articleOptions: [...node.querySelectorAll("article")].map((article) => article.querySelectorAll(".option-row").length),
        answerGaps: [...node.querySelectorAll("article")].map((article) => article.querySelector(".question-gap")?.innerText || ""),
      }));
      const details = group.locator("details.question-details");
      const detailCount = await details.count();
      for (let index = 0; index < detailCount; index += 1) {
        await details.nth(index).locator("summary").click();
      }
      const opened = await group.evaluate((node) => ({
        detailsOpen: [...node.querySelectorAll("article details.question-details")].map((item) => item.open),
        fullText: [...node.querySelectorAll("article .question-full")].map((item) => item.innerText.slice(0, 1500)),
        options: [...node.querySelectorAll("article .option-list .option-row")].map((item) => item.innerText.trim()),
        noOptionMessages: [...node.querySelectorAll("article .question-gap")].map((item) => item.innerText.trim()),
        answerStatus: [...node.querySelectorAll("article .answer-head")].map((item) => item.innerText.trim()),
      }));
      record(
        "question-bank",
        "passage-group",
        passage.articleCount === 2 &&
          detailCount === 2 &&
          passage.text.length > 100 &&
          opened.detailsOpen.every(Boolean) &&
          opened.options.length === 8 &&
          opened.answerStatus.every((item) => item.includes("No answer")),
        { passage, opened, detailCount },
      );
    } else {
      record("question-bank", "passage-group", false, { state: passageState });
    }

    await navigate("/exams/questions?course=gate-physics");
    await page.getByPlaceholder("Search question or answer text").fill("two -level quantum");
    const searchPayload = await applyFilters();
    const searchState = await state();
    record("question-bank-filter", "search", searchPayload.total === 1 && searchState.articles === 1, {
      total: searchPayload.total,
      state: searchState,
    });

    const resetPromise = page.waitForResponse(
      (response) => response.request().method() === "GET" && response.url().includes("/api/exams/questions?"),
      { timeout: 30000 },
    );
    await page.getByRole("button", { name: "Reset" }).first().click();
    await resetPromise.catch(() => null);
    await page.waitForTimeout(500);
    const resetState = await state();
    record("question-bank", "course-reset", resetState.errors === 0 && resetState.articles === 50, resetState);
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    consoleErrors: errors,
    badResponses: bad,
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
