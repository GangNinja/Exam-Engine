import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.COURSE_BASE_URL || "http://127.0.0.1:8766";
const courseId = process.env.COURSE_ID || "qa-audit-1794460988371";
const outPath = "reports/step2_full_qa/evidence/course-upload-tests.json";
const fixture = path.resolve("reports/step2_full_qa/evidence/qa_upload_fixture.txt");
const invalid = path.resolve("reports/step2_full_qa/evidence/qa_invalid_fixture.bin");
const invalidImage = path.resolve("reports/step2_full_qa/evidence/qa_invalid_image.png");
const pdfFixture = path.resolve("datasets/sample/cs201/question_paper_midterm.pdf");
const executable = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].find((candidate) => existsSync(candidate));
const results = [];
let page;
let browser;

function record(name, pass, detail = {}) {
  results.push({ name, pass, detail, at: new Date().toISOString() });
}

async function statusText() {
  return page.locator("#upload-status, .form-status").last().innerText();
}

async function submitUpload({ file, policy = "skip", kind = "notes" }) {
  await page.locator("#upload-file").setInputFiles(file);
  await page.locator("#upload-kind").selectOption(kind);
  await page.locator("#upload-duplicate").selectOption(policy);
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/courses/${encodeURIComponent(courseId)}/documents`),
    { timeout: 60000 },
  );
  await page.getByRole("button", { name: "Ingest document", exact: true }).click();
  const response = await responsePromise;
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = { text: await response.text() };
  }
  await page.waitForTimeout(700);
  return { status: response.status(), payload, ui: await statusText() };
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
    await page.goto(`${base}/?qa=course-upload#/c/${encodeURIComponent(courseId)}/course`, {
      waitUntil: "domcontentloaded",
    });
    await page.waitForFunction(() => document.querySelector("main h1")?.textContent?.includes("QA Audit Synthetic Course"), undefined, { timeout: 60000 });
    await page.waitForTimeout(700);
    const controls = await page.evaluate(() => ({
      fileAccept: document.querySelector("#upload-file")?.getAttribute("accept"),
      kinds: [...document.querySelectorAll("#upload-kind option")].map((node) => node.value),
      policies: [...document.querySelectorAll("#upload-duplicate option")].map((node) => node.value),
      buttons: [...document.querySelectorAll("main button")].map((node) => node.innerText.trim()),
      newCourseHref: [...document.querySelectorAll("main a")].find((node) => node.innerText.trim() === "New course")?.getAttribute("href"),
    }));
    record("form-controls", controls.kinds.length >= 5 && controls.policies.length === 3 && controls.buttons.includes("Ingest document"), controls);

    await page.locator("#upload-file").setInputFiles([]);
    await page.getByRole("button", { name: "Ingest document", exact: true }).click();
    await page.waitForTimeout(300);
    const noFile = await statusText();
    record("no-file-validation", /Choose a file first/i.test(noFile), { status: noFile });

    const invalidResult = await submitUpload({ file: invalid, policy: "skip" });
    record("unsupported-format-handling", (invalidResult.status >= 400 || /failed|unsupported|error/i.test(invalidResult.ui)) && !/(^|\n)null\s*$/.test(invalidResult.ui), invalidResult);

    const duplicateSkip = await submitUpload({ file: fixture, policy: "skip" });
    const duplicateStatus = String(duplicateSkip.payload?.result?.status || "");
    record(
      "duplicate-skip",
      [200, 201].includes(duplicateSkip.status) && /duplicate/i.test(duplicateStatus),
      { ...duplicateSkip, duplicateStatus },
    );

    const duplicateFail = await submitUpload({ file: fixture, policy: "fail" });
    record(
      "duplicate-fail",
      duplicateFail.status >= 400 && /fail|duplicate|error/i.test(duplicateFail.ui) && !/(^|\n)null\s*$/.test(duplicateFail.ui),
      duplicateFail,
    );

    const invalidImageResult = await submitUpload({ file: invalidImage, policy: "skip", kind: "notes" });
    record(
      "invalid-image-handling",
      invalidImageResult.status >= 400 ||
        /failed|error|ocr|image|warning/i.test(invalidImageResult.ui) ||
        Boolean(invalidImageResult.payload?.result?.warnings?.length),
      invalidImageResult,
    );

    const pdfResult = await submitUpload({ file: pdfFixture, policy: "skip", kind: "question_paper" });
    record(
      "pdf-ingestion",
      [200, 201].includes(pdfResult.status) && Number(pdfResult.payload?.result?.page_count || 0) > 0,
      pdfResult,
    );

    const newCourse = page.getByRole("link", { name: "New course", exact: true }).first();
    await newCourse.click();
    await page.waitForTimeout(800);
    const newHeading = await page.locator("main h1").innerText();
    record("new-course-link", page.url().includes("#/new") && newHeading.length > 0, { url: page.url(), heading: newHeading });
  } finally {
    await browser.close();
  }
  const output = {
    base,
    courseId,
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
