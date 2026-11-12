import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.EXAMS_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/exams-filter-tests.json";
const executable = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"].find((candidate) => existsSync(candidate));
const labels = ["Examination category", "Subject", "Examination year", "Question type", "Answer format", "Syllabus unit", "Topic"];
const results = [];
let page;
let browser;

function record(name, pass, detail = {}) {
  results.push({ name, pass, detail, at: new Date().toISOString() });
}

async function state() {
  return page.evaluate(() => ({
    heading: document.querySelector("main h1")?.innerText,
    status: document.querySelector(".filter-status")?.innerText || "",
    summary: document.querySelector(".results-summary-note")?.innerText || "",
    rows: document.querySelectorAll("main table tbody tr").length,
    cards: document.querySelectorAll("main .question-card, main article").length,
    selects: [...document.querySelectorAll(".filter-grid select")].map((select) => ({
      label: select.closest("label")?.innerText.split("\n")[0] || select.getAttribute("aria-label"),
      value: select.value,
      options: [...select.options].map((option) => ({ value: option.value, text: option.innerText })),
    })),
  }));
}

async function apply() {
  const wait = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/exams" && response.request().method() === "GET",
    { timeout: 60000 },
  );
  await page.getByRole("button", { name: "Apply filters", exact: true }).click();
  await wait;
  await page.waitForTimeout(600);
}

async function reset() {
  await page.getByRole("button", { name: "Reset", exact: true }).click();
  await page.waitForTimeout(900);
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  browser = await chromium.launch({ executablePath: executable, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
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
    await page.goto(`${base}/?qa=exams-filters#/exams`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.querySelector("main h1")?.innerText === "Examination explorer", undefined, { timeout: 60000 });
    await page.waitForTimeout(900);
    const initial = await state();
    record("initial-load", initial.status.includes("15,432") && initial.selects.length === 7, initial);

    for (const label of labels) {
      await reset();
      const contextual = label === "Syllabus unit" || label === "Topic";
      if (contextual) {
        await page.getByLabel("Examination category", { exact: true }).selectOption("gate");
        await page.getByLabel("Subject", { exact: true }).selectOption("gate-physics");
        await page.waitForTimeout(900);
        if (label === "Topic") {
          const units = page.getByLabel("Syllabus unit", { exact: true });
          if ((await units.locator("option").count()) > 1) {
            await units.selectOption({ index: 1 });
            await page.waitForTimeout(500);
          }
        }
      }
      const control = page.getByLabel(label, { exact: true });
      const optionCount = await control.locator("option").count();
      if (optionCount < 2) {
        record(`filter-${label}`, false, { error: "No non-empty option available", optionCount, contextual });
        continue;
      }
      const selected = await control.locator("option").nth(1).innerText();
      await control.selectOption({ index: 1 });
      await apply();
      const after = await state();
      record(`filter-${label}`, after.status.length > 0 && !after.status.includes("15,432"), {
        selected,
        contextual,
        status: after.status,
        summary: after.summary,
        rows: after.rows,
        optionCount,
      });
    }

    await reset();
    await page.getByLabel("Search imported questions", { exact: true }).fill("quantum");
    await apply();
    const searchState = await state();
    record("filter-search", searchState.status.length > 0 && !searchState.status.includes("15,432"), searchState);

    await reset();
    await page.getByLabel("Examination category", { exact: true }).selectOption("gate");
    await page.getByLabel("Examination year", { exact: true }).selectOption({ label: "2024" });
    await page.getByLabel("Question type", { exact: true }).selectOption("mcq");
    await apply();
    const combo = await state();
    record("filter-combination", combo.status.includes("414") || combo.summary.includes("414"), combo);

    await reset();
    const final = await state();
    record("reset", final.status.includes("15,432") && final.selects.every((select) => !select.value), final);
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    consoleErrors: errors,
    badResponses: bad,
    summary: { total: results.length, pass: results.filter((item) => item.pass).length, fail: results.filter((item) => !item.pass).length },
    results,
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify(output.summary));
  if (output.summary.fail) console.log(JSON.stringify(results.filter((item) => !item.pass), null, 2).slice(0, 16000));
  if (output.summary.fail) process.exitCode = 1;
}

await main();
