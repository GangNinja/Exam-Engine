import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.SYLLABUS_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/syllabus-tests.json";
const executable = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"].find((candidate) => existsSync(candidate));
const results = [];
let page;
let browser;

function record(name, pass, detail = {}) {
  results.push({ name, pass, detail, at: new Date().toISOString() });
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  browser = await chromium.launch({ executablePath: executable, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(`${base}/?qa=syllabus#/c/gate-physics/syllabus`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.querySelector("main h1")?.innerText === "Syllabus explorer", undefined, { timeout: 60000 });
    await page.waitForTimeout(900);
    const unitButtons = page.locator(".syllabus-unit-button");
    const count = await unitButtons.count();
    for (let index = 0; index < count; index += 1) {
      const button = unitButtons.nth(index);
      const label = (await button.innerText()).split("\n")[0];
      await button.click();
      await page.waitForTimeout(250);
      const state = await page.evaluate(() => ({
        heading: document.querySelector(".syllabus-selected-kicker")?.innerText || "",
        body: document.querySelector(".syllabus-unit")?.innerText.slice(0, 700) || "",
        active: [...document.querySelectorAll(".syllabus-unit-button")].filter((node) => node.getAttribute("aria-current") === "true").map((node) => node.innerText.split("\n")[0]),
        error: Boolean(document.querySelector("main .error-panel")),
      }));
      record(`unit-${index + 1}`, Boolean(state.body) && !state.error, { label, ...state });
    }
    const filter = page.getByLabel("Filter syllabus units and topics", { exact: true });
    await filter.fill("Quantum");
    await page.waitForTimeout(450);
    const filtered = await page.locator(".syllabus-unit-button").count();
    const filterState = await page.evaluate(() => document.querySelector("main")?.innerText.includes("Quantum"));
    record("filter", filtered > 0 && filtered < count && filterState, { totalUnits: count, filteredUnits: filtered });
    await filter.fill("");
    await page.waitForTimeout(300);
    record("filter-clear", (await page.locator(".syllabus-unit-button").count()) === count, { totalUnits: count });
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    consoleErrors: errors,
    summary: { total: results.length, pass: results.filter((item) => item.pass).length, fail: results.filter((item) => !item.pass).length },
    results,
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify(output.summary));
  if (output.summary.fail) console.log(JSON.stringify(results.filter((item) => !item.pass), null, 2).slice(0, 12000));
  if (output.summary.fail) process.exitCode = 1;
}

await main();
