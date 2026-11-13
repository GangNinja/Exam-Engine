import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.RECOVERY_BASE_URL || "http://127.0.0.1:8767";
const outPath = "reports/step2_full_qa/evidence/recovery-workflow-tests.json";
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
  await page.waitForTimeout(350);
}

async function waitRecoveryOverview() {
  await page.waitForFunction(
    () => document.querySelector("main h1")?.textContent?.includes("Dataset quality"),
    undefined,
    { timeout: 30000 },
  );
  await page.waitForTimeout(700);
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
  page.on("dialog", (dialog) => dialog.accept());
  try {
    await page.goto(`${base}/?qa=recovery-fixture#/recovery`, { waitUntil: "domcontentloaded" });
    await waitRecoveryOverview();
    const initial = await page.evaluate(() => ({
      heading: document.querySelector("main h1")?.innerText,
      buttons: [...document.querySelectorAll("main button")].map((button) => button.innerText.trim()),
      courseRows: [...document.querySelectorAll("main table")].find((table) =>
        table.querySelector("caption")?.innerText.includes("COURSE REPORT"),
      )?.querySelectorAll("tbody tr").length || 0,
      run: document.querySelector("main .scope-statement")?.innerText || "",
    }));
    record("recovery", "initial", initial.heading === "Dataset quality & recovery" && initial.courseRows > 0, initial);

    try {
      const dryPromise = page.waitForResponse(
        (response) => response.request().method() === "POST" && response.url().endsWith("/api/recovery/run"),
        { timeout: 180000 },
      );
      await page.getByRole("button", { name: "Dry audit", exact: true }).click();
      const dryResponse = await dryPromise;
      const dry = await dryResponse.json();
      await waitRecoveryOverview();
      record("recovery", "dry-audit", dryResponse.status() === 200 && dry.dry_run === true, {
        status: dryResponse.status(),
        runId: dry.run_id,
        dryRun: dry.dry_run,
        totals: dry.totals,
        courses: dry.courses?.length,
        sourceFiles: dry.root,
      });
    } catch (error) {
      record("recovery", "dry-audit", false, { error: error.message });
    }

    try {
      const savePromise = page.waitForResponse(
        (response) => response.request().method() === "POST" && response.url().endsWith("/api/recovery/run"),
        { timeout: 180000 },
      );
      await page.getByRole("button", { name: "Run & save", exact: true }).click();
      const saveResponse = await savePromise;
      const saved = await saveResponse.json();
      await waitRecoveryOverview();
      record("recovery", "run-and-save", saveResponse.status() === 200 && saved.dry_run !== true, {
        status: saveResponse.status(),
        runId: saved.run_id,
        dryRun: saved.dry_run,
        totals: saved.totals,
        courses: saved.courses?.length,
      });
    } catch (error) {
      record("recovery", "run-and-save", false, { error: error.message });
    }

    try {
      const courseTable = page.locator("main table").filter({ has: page.locator("caption") }).filter({ hasText: "COURSE REPORT" });
      const rows = await courseTable.locator("tbody tr").count();
      for (let index = 0; index < rows; index += 1) {
        const row = courseTable.locator("tbody tr").nth(index);
        const label = (await row.innerText()).split("\n").slice(0, 2).join(" · ");
        await row.getByRole("button", { name: "Inspect" }).click();
        await page.waitForTimeout(600);
        const detail = await page.evaluate(() => ({
          heading: document.querySelector("main h1")?.innerText,
          courseDetail: [...document.querySelectorAll("main section.panel h2")].some((node) => node.innerText.includes("·")),
          text: [...document.querySelectorAll("main section.panel")].slice(-1)[0]?.innerText.slice(0, 500) || "",
        }));
        record("recovery", `inspect-${index + 1}`, detail.courseDetail, { label, detail });
      }
    } catch (error) {
      record("recovery", "inspect-all", false, { error: error.message });
    }

    try {
      const view = page.getByRole("button", { name: "View", exact: true }).first();
      await view.click();
      await page.waitForTimeout(500);
      const modal = await page.evaluate(() => {
        const node = document.querySelector(".modal-backdrop");
        return {
          open: Boolean(node),
          text: node?.innerText.slice(0, 1200) || "",
          buttons: [...(node?.querySelectorAll("button") || [])].map((button) => button.innerText.trim()),
        };
      });
      record("recovery", "record-inspector", modal.open && modal.text.includes("Record evidence"), modal);
      if (modal.open) {
        const approve = page.locator(".modal-backdrop").getByRole("button", { name: "Approve", exact: true });
        if (await approve.count()) {
          const approveResponsePromise = page.waitForResponse(
            (response) =>
              response.request().method() === "POST" &&
              response.url().endsWith("/api/recovery/repairs/review"),
            { timeout: 30000 },
          );
          await approve.click();
          const approveResponse = await approveResponsePromise;
          const approved = await approveResponse.json();
          record("recovery", "repair-approval", approveResponse.status() === 200, {
            status: approveResponse.status(),
            repairId: approved.record?.repair_id,
            reviewStatus: approved.record?.review_status,
          });
        } else {
          record("recovery", "repair-approval", false, { error: "No Approve control found in record inspector" });
        }
        await page.locator(".modal-backdrop").getByRole("button", { name: "Close", exact: true }).click();
      }
    } catch (error) {
      record("recovery", "record-inspector", false, { error: error.message });
    }

    try {
      const rollbackPromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" && response.url().endsWith("/api/recovery/rollback"),
        { timeout: 60000 },
      );
      await page.getByRole("button", { name: "Roll back releases", exact: true }).click();
      const rollbackResponse = await rollbackPromise;
      const rollback = await rollbackResponse.json();
      await waitRecoveryOverview();
      record("recovery", "rollback", rollbackResponse.status() === 200, {
        status: rollbackResponse.status(),
        response: rollback,
      });
    } catch (error) {
      record("recovery", "rollback", false, { error: error.message });
    }
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
