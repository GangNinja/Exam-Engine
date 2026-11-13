import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.MUTATION_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/practice-mutation-tests.json";
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
    undefined,
    { timeout: 45000 },
  );
  await page.waitForFunction(
    () => !document.querySelector("main .loading-panel, main .boot-panel"),
    undefined,
    { timeout: 45000 },
  );
  await page.waitForTimeout(300);
}

async function generatedCounts() {
  const response = await fetch(`${base}/api/courses/gate-physics/generated?limit=200`);
  return response.json();
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  const before = await generatedCounts();
  browser = await chromium.launch({ executablePath: executable, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  const badResponses = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) badResponses.push({ status: response.status(), url: response.url() });
  });
  try {
    await page.goto(`${base}/?qa=practice-mutation#/c/gate-physics/practice`, {
      waitUntil: "domcontentloaded",
    });
    await waitReady();
    await page.waitForSelector("#generate-concept", { timeout: 45000 });
    record("practice", "initial-form", (await page.locator("#generate-concept option").count()) > 1, {
      heading: await page.locator("main h1").innerText(),
      options: await page.locator("#generate-concept option").count(),
    });

    await page.locator("#generate-concept").selectOption("con_896f670219eba562be0ba7ce");
    await page.locator("#generate-format").selectOption("short_answer");
    await page.locator("#generate-count").fill("2");
    const generatePromise = page.waitForResponse(
      (response) => response.request().method() === "POST" && response.url().endsWith("/generate"),
      { timeout: 120000 },
    );
    await page.getByRole("button", { name: "Generate questions" }).click();
    const generateResponse = await generatePromise;
    const batch = await generateResponse.json();
    record("practice", "generate", generateResponse.status() === 201 && batch.counts?.persisted > 0, {
      status: generateResponse.status(),
      batchId: batch.batch_id,
      counts: batch.counts,
      validation: (batch.questions || []).map((item) => item.validation?.status),
      rejected: batch.counts?.rejected,
    });
    await page.waitForTimeout(1500);

    const generated = await generatedCounts();
    record("practice", "persist-draft", generated.total === before.total + (batch.counts?.persisted || 0), {
      before: before.total,
      after: generated.total,
      persisted: batch.counts?.persisted,
    });

    const approveLocator = page.getByRole("button", { name: "Approve" }).first();
    const approveCount = await page.getByRole("button", { name: "Approve" }).count();
    if (approveCount) {
      const approvePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().includes("/generated/") &&
          response.url().endsWith("/review"),
        { timeout: 30000 },
      );
      await approveLocator.click();
      const approveResponse = await approvePromise;
      const approved = await approveResponse.json();
      record("practice", "approve", approveResponse.status() === 200 && approved.review_status === "approved", {
        status: approveResponse.status(),
        questionId: approved.question_id,
        reviewStatus: approved.review_status,
        revision: approved.revision,
      });
    } else {
      record("practice", "approve", false, { error: "No Approve control rendered for the new drafts" });
    }

    try {
      const reject = page.getByRole("button", { name: "Reject" }).first();
      if (await reject.count()) {
        const rejectPromise = page.waitForResponse(
          (response) =>
            response.request().method() === "POST" &&
            response.url().includes("/generated/") &&
            response.url().endsWith("/review"),
          { timeout: 30000 },
        );
        await reject.click();
        const rejectResponse = await rejectPromise;
        const rejected = await rejectResponse.json();
        record("practice", "reject", rejectResponse.status() === 200 && rejected.review_status === "rejected", {
          status: rejectResponse.status(),
          questionId: rejected.question_id,
          reviewStatus: rejected.review_status,
        });
      } else {
        record("practice", "reject", false, { error: "No Reject control rendered" });
      }
    } catch (error) {
      record("practice", "reject", false, { error: error.message });
    }

    try {
      const firstCard = page.locator(".question-item").filter({ has: page.locator(".answer-check") }).first();
      const check = firstCard.locator(".answer-check");
      await check.locator("summary").click();
      const textarea = check.locator("textarea");
      const answer = await textarea.count()
        ? "A short answer based on the stored concept."
        : null;
      if (textarea.count()) {
        await textarea.fill(answer);
        const gradePromise = page.waitForResponse(
          (response) => response.request().method() === "POST" && response.url().endsWith("/grade"),
          { timeout: 30000 },
        );
        await check.getByRole("button", { name: "Check answer" }).click();
        const gradeResponse = await gradePromise;
        const grade = await gradeResponse.json();
        record("practice", "answer-check", gradeResponse.status() === 200 && typeof grade.verdict === "string", {
          status: gradeResponse.status(),
          verdict: grade.verdict,
          coverage: grade.coverage,
          result: await check.locator(".answer-feedback").innerText(),
        });
      } else {
        record("practice", "answer-check", false, { error: "No answer-check textarea found" });
      }
    } catch (error) {
      record("practice", "answer-check", false, { error: error.message });
    }

    try {
      const filter = page.locator("#generated-filter");
      await filter.selectOption("pending_review");
      await page.waitForTimeout(700);
      const pendingCards = await page.locator("main .question-item").count();
      const pendingText = await page.locator("main").innerText();
      record("practice", "review-filter", pendingCards > 0 && pendingText.includes("Draft"), {
        pendingCards,
      });
    } catch (error) {
      record("practice", "review-filter", false, { error: error.message });
    }

    try {
      const challengePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" && response.url().endsWith("/advanced/adversarial"),
        { timeout: 120000 },
      );
      await page.getByRole("button", { name: "Generate challenge" }).click();
      const challengeResponse = await challengePromise;
      const challenge = await challengeResponse.json();
      record("practice", "generate-challenge", challengeResponse.status() === 201 && challenge.counts?.persisted > 0, {
        status: challengeResponse.status(),
        batchId: challenge.batch_id,
        counts: challenge.counts,
        validation: (challenge.questions || []).map((item) => item.validation?.status),
      });
    } catch (error) {
      record("practice", "generate-challenge", false, { error: error.message });
    }

    try {
      const minePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().endsWith("/misconceptions/analyse"),
        { timeout: 60000 },
      );
      await page.getByRole("button", { name: "Mine stored responses" }).click();
      const mineResponse = await minePromise;
      const report = await mineResponse.json();
      record("practice", "mine-responses", mineResponse.status() === 200, {
        status: mineResponse.status(),
        responsesSupplied: report.responses_supplied,
        records: report.records?.length,
      });
    } catch (error) {
      record("practice", "mine-responses", false, { error: error.message });
    }

    const after = await generatedCounts();
    record("practice", "final-count", after.total >= generated.total, {
      before: before.total,
      afterGeneration: generated.total,
      afterMutations: after.total,
    });
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    before,
    consoleErrors: errors,
    badResponses,
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
