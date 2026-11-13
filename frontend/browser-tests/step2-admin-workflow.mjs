import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.MUTATION_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/admin-workflow-tests.json";
const fixture = path.resolve("reports/step2_full_qa/evidence/qa_upload_fixture.txt");
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
  await page.waitForTimeout(350);
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  const stamp = Date.now();
  const courseId = `qa-audit-${stamp}`;
  const courseCode = `QA${String(stamp).slice(-8)}`;
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
    await page.goto(`${base}/?qa=admin#/new`, { waitUntil: "domcontentloaded" });
    await waitReady();
    await page.waitForSelector("#new-course-id", { timeout: 30000 });

    let postCount = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/api/courses")) postCount += 1;
    });
    await page.getByRole("button", { name: "Create course" }).click();
    await page.waitForTimeout(800);
    const required = await page.evaluate(() => ({
      active: document.activeElement?.id || "",
      invalid: [...document.querySelectorAll("form [required]")].map((node) => ({
        id: node.id,
        valid: node.validity?.valid,
        valueMissing: node.validity?.valueMissing,
      })),
      status: document.querySelector(".form-status")?.innerText || "",
    }));
    record("new-course", "required-validation", postCount === 0 && required.invalid.some((item) => item.valueMissing), {
      postCount,
      required,
    });

    const values = {
      "#new-course-id": courseId,
      "#new-course-code": courseCode,
      "#new-course-title": "QA Audit Synthetic Course",
      "#new-course-inst-code": "qa-audit",
      "#new-course-inst-name": "QA Audit Institution",
      "#new-course-department": "Quality Assurance",
      "#new-course-programme": "Audit",
      "#new-course-year": "2026-27",
      "#new-course-version": "qa-v1",
    };
    for (const [selector, value] of Object.entries(values)) {
      await page.locator(selector).fill(value);
    }
    const createPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" && response.url().endsWith("/api/courses"),
      { timeout: 60000 },
    );
    await page.getByRole("button", { name: "Create course" }).click();
    const createResponse = await createPromise;
    const created = await createResponse.json();
    record("new-course", "create", createResponse.status() === 201 && created.created === true, {
      status: createResponse.status(),
      courseId: created.course?.course_id,
      response: created,
    });
    await page.waitForFunction(
      (id) => location.hash.includes(`/c/${id}/course`),
      courseId,
      { timeout: 20000 },
    );
    await waitReady();
    const coursePage = await page.evaluate(() => ({
      heading: document.querySelector("main h1")?.innerText,
      selected: document.querySelector("#course-select")?.value,
      text: document.querySelector("main")?.innerText.slice(0, 1200),
    }));
    record("new-course", "created-route", coursePage.selected === courseId && coursePage.heading.includes("QA Audit Synthetic Course"), coursePage);

    await page.waitForSelector("#upload-file", { timeout: 30000 });
    await page.locator("#upload-file").setInputFiles(fixture);
    await page.locator("#upload-kind").selectOption("notes");
    const uploadPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/api/courses/${encodeURIComponent(courseId)}/documents`),
      { timeout: 120000 },
    );
    await page.getByRole("button", { name: "Ingest document" }).click();
    const uploadResponse = await uploadPromise;
    const uploaded = await uploadResponse.json();
    record("upload", "fixture-ingest", uploadResponse.status() === 201 && uploaded.result?.status === "ingested", {
      status: uploadResponse.status(),
      result: uploaded.result,
    });
    await page.waitForTimeout(800);
    const documentState = await page.evaluate(() => ({
      documents: document.querySelectorAll("main details.disclosure").length,
      result: document.querySelector(".upload-result")?.innerText || "",
      text: document.querySelector("main")?.innerText.slice(-1200) || "",
    }));
    record("upload", "result-disclosure", documentState.result.length > 0 || documentState.text.includes("QA SAMPLE"), documentState);

    try {
      await page.getByRole("button", { name: "Reload workspace" }).click();
      await page.waitForTimeout(1200);
      await waitReady();
      const refreshed = await page.evaluate(() => ({
        documents: document.querySelectorAll("main details.disclosure").length,
        text: document.querySelector("main")?.innerText.slice(-900) || "",
      }));
      record("upload", "reload-workspace", refreshed.text.includes("qa_upload_fixture.txt"), refreshed);
    } catch (error) {
      record("upload", "reload-workspace", false, { error: error.message });
    }

    for (const [kind, label] of [
      ["nlp", "Run text analysis"],
      ["understanding", "Run question understanding"],
      ["graph", "Build knowledge graph"],
      ["prerequisites", "Discover prerequisites"],
    ]) {
      try {
        const jobPromise = page.waitForResponse(
          (response) =>
            response.request().method() === "POST" &&
            response.url().endsWith(`/analysis/${kind}`),
          { timeout: 30000 },
        );
        await page.getByRole("button", { name: label }).click();
        const startedResponse = await jobPromise;
        const started = await startedResponse.json();
        const jobId = started.job?.job_id;
        let job = started.job;
        const deadline = Date.now() + 240000;
        while (job && ["queued", "running"].includes(job.status) && Date.now() < deadline) {
          await page.waitForTimeout(1000);
          job = await fetch(`${base}/api/jobs/${job.job_id}`).then((response) => response.json());
        }
        const jobState = await page.evaluate(() => [...document.querySelectorAll(".job-control")].map((node) => node.innerText.trim()));
        record("analysis-job", kind, startedResponse.status() === 202 && job?.status === "succeeded", {
          status: startedResponse.status(),
          jobId,
          created: started.created,
          finalStatus: job?.status,
          error: job?.error,
          finalStates: jobState,
        });
      } catch (error) {
        record("analysis-job", kind, false, { error: error.message });
      }
    }
  } finally {
    await browser.close();
  }
  const output = {
    base,
    courseId,
    courseCode,
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
