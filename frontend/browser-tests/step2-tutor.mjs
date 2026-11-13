import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.TUTOR_BASE_URL || "http://127.0.0.1:8766";
const courseId = process.env.TUTOR_COURSE || "gate-physics";
const outPath = "reports/step2_full_qa/evidence/tutor-tests.json";
const executable = [
  process.env.BROWSER_EXECUTABLE,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].find((candidate) => candidate && existsSync(candidate));
const results = [];
let page;
let browser;
let askStatus = null;

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
  page.on("response", async (response) => {
    if (response.url().includes("/tutor/ask/stream")) {
      askStatus = response.status();
    }
  });
  try {
    await page.goto(`${base}/?qa=tutor#/c/${encodeURIComponent(courseId)}/tutor`, {
      waitUntil: "domcontentloaded",
    });
    await page.waitForSelector("#tutor-question", { timeout: 60000 });
    await page.waitForFunction(
      () => document.querySelector(".capability-list") || document.querySelector(".tutor-model")?.textContent,
      undefined,
      { timeout: 60000 },
    );
    await page.waitForTimeout(500);
    const initial = await page.evaluate(() => ({
      heading: document.querySelector("main h1")?.innerText,
      model: document.querySelector(".tutor-model")?.innerText,
      capabilities: [...document.querySelectorAll(".capability-list li")].map((node) => node.innerText.trim()),
      syllabusOptions: [...document.querySelectorAll("#syllabus-select option")].map((node) => ({ value: node.value, text: node.innerText })),
      conceptOptions: [...document.querySelectorAll("#tutor-concept option")].map((node) => ({ value: node.value, text: node.innerText })),
      prompts: [...document.querySelectorAll(".prompt-chip")].map((node) => node.innerText.trim()),
      conversation: document.querySelector(".conversation")?.innerText.slice(0, 1000),
    }));
    record("load", initial.heading === "AI tutor" && Boolean(initial.model), initial);
    record("capability-disclosure", initial.capabilities.length > 0 && Boolean(initial.model), {
      model: initial.model,
      capabilities: initial.capabilities,
    });
    record("scope-selector", initial.syllabusOptions.length > 0, { options: initial.syllabusOptions });
    record("suggested-prompts", initial.prompts.length > 0, { prompts: initial.prompts });

    await page.locator("#tutor-question").fill("");
    await page.getByRole("button", { name: "Ask tutor", exact: true }).click();
    await page.waitForTimeout(200);
    const empty = await page.evaluate(() => document.querySelector(".tutor-composer .form-status")?.innerText || "");
    record("empty-input", /Enter a question first/i.test(empty), { status: empty });

    if (initial.prompts.length) {
      await page.locator(".prompt-chip").first().click();
      const chipValue = await page.locator("#tutor-question").inputValue();
      record("suggestion-click", chipValue.length > 0, { value: chipValue });
    } else {
      record("suggestion-click", false, { error: "No suggested prompt buttons were rendered" });
    }

    const question = "Explain free electron theory and give one exam-focused takeaway.";
    await page.locator("#tutor-question").fill(question);
    const conceptSelect = page.locator("#tutor-concept");
    const conceptCount = await conceptSelect.locator("option").count();
    if (conceptCount > 1) {
      await conceptSelect.selectOption({ index: 1 });
    }
    const selectedConcept = await conceptSelect.inputValue();
    await page.getByRole("button", { name: "Ask tutor", exact: true }).click();
    try {
      await page.waitForFunction(
        () => {
          const status = document.querySelector(".tutor-composer .form-status")?.innerText || "";
          return /No grounded answer|Request failed|Offline|evidence|answered|Streaming the answer|Local AI|insufficient/i.test(status);
        },
        undefined,
        { timeout: 120000 },
      );
    } catch {
      // Capture the settled state below even when the provider does not answer.
    }
    await page.waitForTimeout(1200);
    const asked = await page.evaluate(() => ({
      status: document.querySelector(".tutor-composer .form-status")?.innerText || "",
      model: document.querySelector(".tutor-model")?.innerText || "",
      conversation: document.querySelector(".conversation")?.innerText.slice(-2600) || "",
      pending: Boolean(document.querySelector(".message-pending")),
      submitDisabled: document.querySelector(".tutor-composer button[type=submit]")?.disabled,
    }));
    const offline = /Offline evidence mode/i.test(asked.model);
    const truthfulFallback = !offline || /Stored-evidence summary; no language model was available/i.test(asked.status);
    const hasTutorReply = /\bTUTOR\b/.test(asked.conversation);
    record("ask-bounded", Boolean(askStatus) && asked.submitDisabled === false && truthfulFallback && hasTutorReply, {
      question,
      selectedConcept,
      conceptOptions: conceptCount,
      askStatus,
      offline,
      truthfulFallback,
      asked,
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
  if (output.summary.fail) console.log(JSON.stringify(results.filter((item) => !item.pass), null, 2).slice(0, 16000));
  if (output.summary.fail) process.exitCode = 1;
}

await main();
