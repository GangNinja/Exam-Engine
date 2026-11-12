import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.NAV_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/navigation-tests.json";
const executable = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"].find((candidate) => existsSync(candidate));
const results = [];
let page;
let browser;

function record(name, pass, detail = {}) {
  results.push({ name, pass, detail, at: new Date().toISOString() });
}

async function waitPage() {
  await page.waitForFunction(
    () => {
      const heading = document.querySelector("main h1")?.textContent || "";
      return heading && !/^(Opening|Loading)/.test(heading);
    },
    undefined,
    { timeout: 60000 },
  );
  await page.waitForTimeout(450);
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
    await page.goto(`${base}/?qa=navigation#/dashboard`, { waitUntil: "domcontentloaded" });
    await waitPage();
    const navItems = await page.evaluate(() =>
      [...document.querySelectorAll("a.nav-item")].map((link) => ({
        label: link.innerText.trim(),
        href: link.getAttribute("href"),
      })),
    );
    const unique = [...new Map(navItems.map((item) => [item.href, item])).values()];
    for (const item of unique) {
      const link = page.locator(`a.nav-item[href="${item.href.replace(/"/g, '\\"')}"]`).first();
      await link.click();
      await waitPage();
      const current = await page.evaluate(() => ({
        hash: location.hash,
        heading: document.querySelector("main h1")?.innerText || "",
        error: Boolean(document.querySelector("main .error-panel")),
        selectedCourse: document.querySelector("#course-select")?.value || "",
      }));
      record(`sidebar-${item.label}`, current.hash === item.href && !current.error && current.heading.length > 0, {
        expectedHash: item.href,
        current,
      });
    }

    await page.goto(`${base}/?qa=navigation#/c/gate-physics/analysis/coverage`, { waitUntil: "domcontentloaded" });
    await waitPage();
    const breadcrumbLinks = await page.evaluate(() =>
      [...document.querySelectorAll(".breadcrumbs a")].map((link) => ({
        text: link.innerText.trim(),
        href: link.getAttribute("href"),
      })),
    );
    if (breadcrumbLinks.length) {
      const target = breadcrumbLinks[0];
      await page.locator(".breadcrumbs a").first().click();
      await waitPage();
      const after = await page.evaluate(() => ({ hash: location.hash, heading: document.querySelector("main h1")?.innerText || "" }));
      record("breadcrumb", after.hash === target.href && after.heading.length > 0, { target, after, links: breadcrumbLinks });
    } else {
      record("breadcrumb", false, { error: "No breadcrumb links found on course route" });
    }

    await page.goto(`${base}/?qa=navigation#/dashboard`, { waitUntil: "domcontentloaded" });
    await waitPage();
    await page.locator('a.nav-item[href="#/exams"]').first().click();
    await waitPage();
    const examsHash = await page.evaluate(() => location.hash);
    await page.locator('a.nav-item[href="#/exams/questions"]').first().click();
    await waitPage();
    const questionsHash = await page.evaluate(() => location.hash);
    await page.goBack();
    await waitPage();
    const backHash = await page.evaluate(() => location.hash);
    await page.goForward();
    await waitPage();
    const forwardHash = await page.evaluate(() => location.hash);
    record("back-forward", examsHash === "#/exams" && questionsHash === "#/exams/questions" && backHash === "#/exams" && forwardHash === "#/exams/questions", {
      examsHash,
      questionsHash,
      backHash,
      forwardHash,
    });

    await page.goto(`${base}/?qa=navigation#/c/gate-physics/overview`, { waitUntil: "domcontentloaded" });
    await waitPage();
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitPage();
    const refreshed = await page.evaluate(() => ({
      hash: location.hash,
      heading: document.querySelector("main h1")?.innerText || "",
      selectedCourse: document.querySelector("#course-select")?.value || "",
    }));
    record("refresh-state", refreshed.hash === "#/c/gate-physics/overview" && refreshed.selectedCourse === "gate-physics" && refreshed.heading.includes("Physics"), refreshed);
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
  if (output.summary.fail) console.log(JSON.stringify(results.filter((item) => !item.pass), null, 2).slice(0, 16000));
  if (output.summary.fail) process.exitCode = 1;
}

await main();
