import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, "..", "..");
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function browserExecutable() {
  const candidates = [
    process.env.BROWSER_EXECUTABLE,
    process.platform === "win32"
      ? path.join(process.env.ProgramFiles || "C:\\Program Files", "Google", "Chrome", "Application", "chrome.exe")
      : null,
    process.platform === "win32"
      ? path.join(
          process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
          "Microsoft",
          "Edge",
          "Application",
          "msedge.exe",
        )
      : null,
    process.platform === "win32"
      ? path.join(
          process.env.ProgramFiles || "C:\\Program Files",
          "Microsoft",
          "Edge",
          "Application",
          "msedge.exe",
        )
      : null,
    process.platform === "darwin"
      ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      : null,
    process.platform === "linux" ? "/usr/bin/google-chrome" : null,
    process.platform === "linux" ? "/usr/bin/chromium" : null,
    process.platform === "linux" ? "/usr/bin/chromium-browser" : null,
  ];
  return candidates.find((candidate) => candidate && existsSync(candidate)) || null;
}

function serverEnvironment(dbPath) {
  return {
    ...process.env,
    PYTHONPATH: [path.join(projectRoot, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    PYTHONUNBUFFERED: "1",
    EXAM_ENGINE_DB: dbPath,
    EXAM_ENGINE_TUTOR_PROVIDER: "off",
    EXAM_ENGINE_TUTOR_WEB_SEARCH: "off",
  };
}

function startDashboard(dbPath) {
  const child = spawn(
    python,
    ["-m", "exam_engine", "dashboard", "--host", "127.0.0.1", "--port", "0"],
    {
      cwd: projectRoot,
      env: serverEnvironment(dbPath),
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  let output = "";
  let errorOutput = "";
  let port;
  let resolvePort;
  let rejectPort;
  const portPromise = new Promise((resolve, reject) => {
    resolvePort = resolve;
    rejectPort = reject;
  });
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    output += chunk;
    const match = output.match(/Exam Engine dashboard: http:\/\/127\.0\.0\.1:(\d+)/);
    if (match) resolvePort(Number(match[1]));
  });
  child.stderr.on("data", (chunk) => {
    errorOutput += chunk;
  });
  child.once("error", rejectPort);
  child.once("exit", (code) => {
    if (port === undefined) {
      rejectPort(new Error(`dashboard exited with ${code}\n${output}\n${errorOutput}`));
    }
  });
  return {
    child,
    portPromise: portPromise.then((value) => {
      port = value;
      return value;
    }),
    logs: () => `${output}\n${errorOutput}`.trim(),
  };
}

async function waitForHealth(baseUrl, child, logs) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`dashboard exited before becoming ready\n${logs()}`);
    }
    try {
      const response = await fetch(`${baseUrl}/api/health`);
      if (response.ok) return;
    } catch {
      // The server may still be binding its socket.
    }
    await delay(200);
  }
  throw new Error(`dashboard did not become ready\n${logs()}`);
}

async function stopDashboard(child) {
  if (!child || child.exitCode !== null) return;
  child.kill();
  await Promise.race([once(child, "exit"), delay(5000)]);
}

test(
  "Practice answer check and Draft approval",
  { timeout: 120000 },
  async () => {
    const executable = browserExecutable();
    assert.ok(
      executable,
      "Set BROWSER_EXECUTABLE to a Chrome/Chromium executable, or install Chrome/Edge.",
    );

    const tempDirectory = await mkdtemp(path.join(os.tmpdir(), "exam-engine-browser-smoke-"));
    const dbPath = path.join(tempDirectory, "smoke.db");
    const seedPath = path.join(projectRoot, "tools", "seed_dashboard_smoke.py");
    let server;
    let browser;

    try {
      const seeded = spawnSync(python, [seedPath, dbPath], {
        cwd: projectRoot,
        env: serverEnvironment(dbPath),
        encoding: "utf8",
        timeout: 120000,
        windowsHide: true,
      });
      if (seeded.status !== 0) {
        throw new Error(`seed failed with ${seeded.status}\n${seeded.stdout}\n${seeded.stderr}`);
      }
      const seedData = JSON.parse(
        seeded.stdout
          .trim()
          .split(/\r?\n/)
          .filter(Boolean)
          .at(-1),
      );
      assert.ok(seedData.question_id, "seed did not return a question id");
      assert.equal(seedData.review_status, "pending_review");
      assert.ok(seedData.outline_items > 0, "seed did not produce an answer outline");

      server = startDashboard(dbPath);
      const port = await server.portPromise;
      const baseUrl = `http://127.0.0.1:${port}`;
      await waitForHealth(baseUrl, server.child, server.logs);

      browser = await chromium.launch({ executablePath: executable, headless: true });
      const page = await browser.newPage();
      const consoleErrors = [];
      page.on("pageerror", (error) => consoleErrors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });

      await page.goto(
        `${baseUrl}/#/c/${encodeURIComponent(seedData.course_id)}/practice`,
        { waitUntil: "domcontentloaded" },
      );
      await page.getByRole("heading", { name: "Generate, review and assess" }).waitFor({
        state: "visible",
        timeout: 15000,
      });

      const card = page.locator("article").filter({ hasText: seedData.question_id });
      await card.waitFor({ state: "visible", timeout: 15000 });
      assert.equal(await card.count(), 1, "the seeded question should have exactly one card");

      const reviewSelect = card.locator("select").first();
      assert.equal(await reviewSelect.inputValue(), "pending_review");
      assert.equal(
        (await card.locator('[data-review-badge="true"]').innerText()).trim(),
        "Draft",
      );

      await card.locator("summary").filter({ hasText: "Check your answer" }).click();
      const answerInput = card.locator(`#answer-${seedData.question_id}`);
      await answerInput.waitFor({ state: "visible" });
      await answerInput.fill("I do not know this topic yet.");

      const gradeResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().endsWith(`/generated/${seedData.question_id}/grade`),
      );
      await card.locator('details.answer-check[open] button[type="submit"]').click();
      const gradeResponse = await gradeResponsePromise;
      assert.equal(gradeResponse.status(), 200);
      const gradePayload = await gradeResponse.json();
      assert.equal(gradePayload.coverage?.method, "lexical_coverage");
      assert.ok(gradePayload.expected_outline?.length > 0);
      await card.getByText("Local coverage check · not a grade", { exact: true }).waitFor({
        state: "visible",
      });
      assert.match(await card.innerText(), /not a grade/i);

      const reviewResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().endsWith(`/generated/${seedData.question_id}/review`),
      );
      await card.getByRole("button", { name: "Approve" }).first().click();
      const reviewResponse = await reviewResponsePromise;
      assert.equal(reviewResponse.status(), 200);
      const reviewPayload = await reviewResponse.json();
      assert.equal(reviewPayload.review_status, "approved");

      await page.waitForFunction(
        (questionId) => {
          const article = [...document.querySelectorAll("article")].find((item) =>
            item.textContent.includes(questionId),
          );
          return article?.querySelector("select")?.value === "approved";
        },
        seedData.question_id,
      );

      await page.reload({ waitUntil: "domcontentloaded" });
      const reloadedCard = page.locator("article").filter({ hasText: seedData.question_id });
      await reloadedCard.waitFor({ state: "visible", timeout: 15000 });
      assert.equal(await reloadedCard.locator("select").first().inputValue(), "approved");

      const generatedResponse = await fetch(
        `${baseUrl}/api/courses/${encodeURIComponent(seedData.course_id)}/generated?limit=100`,
      );
      assert.equal(generatedResponse.status, 200);
      const generated = await generatedResponse.json();
      const persisted = generated.questions.find(
        (question) => question.question_id === seedData.question_id,
      );
      assert.equal(persisted?.review_status, "approved");

      assert.deepEqual(consoleErrors, []);
      console.log(
        `Practice smoke passed: ${seedData.question_id} checked (${seedData.outline_items} outline items) and approved.`,
      );
    } finally {
      await browser?.close();
      await stopDashboard(server?.child);
      await rm(tempDirectory, { recursive: true, force: true });
    }
  },
);
