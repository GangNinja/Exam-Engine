import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.CONFLICT_BASE_URL || "http://127.0.0.1:8766";
const courseId = process.env.CONFLICT_COURSE || "qa-audit-1794460988371";
const outPath = "reports/step2_full_qa/evidence/course-conflict-tests.json";
const executable = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"].find((candidate) => existsSync(candidate));
const results = [];
let page;
let browser;

function record(name, pass, detail = {}) {
  results.push({ name, pass, detail, at: new Date().toISOString() });
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  const before = await fetch(`${base}/api/courses`).then((response) => response.json());
  browser = await chromium.launch({ executablePath: executable, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(`${base}/?qa=course-conflict#/new`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => Boolean(document.querySelector("#new-course-id")), undefined, { timeout: 60000 });
    await page.locator("#new-course-id").fill(courseId);
    await page.locator("#new-course-code").fill("QA60988371");
    await page.locator("#new-course-title").fill("QA Audit Synthetic Course");
    await page.locator("#new-course-inst-code").fill("qa-audit");
    await page.locator("#new-course-inst-name").fill("QA Audit Institution");
    const responsePromise = page.waitForResponse(
      (response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/courses",
      { timeout: 30000 },
    );
    await page.getByRole("button", { name: "Create course", exact: true }).click();
    const response = await responsePromise;
    const body = await response.json();
    await page.waitForTimeout(800);
    const ui = await page.locator(".form-status").last().innerText();
    const after = await fetch(`${base}/api/courses`).then((result) => result.json());
    record("duplicate-id-upsert", response.status() === 200 && body.created === false && after.total === before.total && /Existing course updated/i.test(ui), {
      status: response.status(),
      created: body.created,
      courseId: body.course?.course_id,
      beforeTotal: before.total,
      afterTotal: after.total,
      ui,
      url: page.url(),
    });
  } finally {
    await browser.close();
  }
  const output = {
    base,
    courseId,
    generatedAt: new Date().toISOString(),
    consoleErrors: errors,
    summary: { total: results.length, pass: results.filter((item) => item.pass).length, fail: results.filter((item) => !item.pass).length },
    results,
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify(output.summary));
}

await main();
