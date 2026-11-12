import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.DATASETS_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/datasets-tests.json";
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

async function waitDatasets() {
  await page.waitForFunction(
    () => document.querySelector("main h1")?.textContent === "Dataset management",
    undefined,
    { timeout: 60000 },
  );
  await page.waitForTimeout(700);
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  browser = await chromium.launch({ executablePath: executable, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: base });
  page = await context.newPage();
  const errors = [];
  const bad = [];
  let overviewRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/exams") overviewRequests += 1;
  });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) bad.push({ status: response.status(), url: response.url() });
  });
  try {
    await page.goto(`${base}/?qa=datasets#/datasets`, { waitUntil: "domcontentloaded" });
    await waitDatasets();
    const loaded = await page.evaluate(async () => {
      const api = await fetch("/api/exams").then((response) => response.json());
      const tables = [...document.querySelectorAll("main table")].map((table) => ({
        caption: table.querySelector("caption")?.innerText || "",
        headers: [...table.querySelectorAll("thead th")].map((node) => node.innerText.trim()),
        rows: table.querySelectorAll("tbody tr").length,
        first: table.querySelector("tbody tr")?.innerText || "",
      }));
      return {
        heading: document.querySelector("main h1")?.innerText,
        apiSummary: api.summary,
        apiCategories: api.categories?.length,
        apiSubjects: api.subjects?.length,
        tables,
        buttons: [...document.querySelectorAll("main button")].map((node) => node.innerText.trim()),
        links: [...document.querySelectorAll("main a")].map((node) => ({ text: node.innerText.trim(), href: node.getAttribute("href") })),
        copyCode: document.querySelector(".command-line code")?.innerText,
      };
    });
    const summary = loaded.apiSummary || {};
    const categoryTable = loaded.tables.find((table) => table.caption.toLowerCase().includes("categories"));
    const subjectTable = loaded.tables.find((table) => table.caption.toLowerCase().includes("subjects"));
    record(
      "page-load",
      loaded.heading === "Dataset management" &&
        String(summary.questions) !== "undefined" &&
        categoryTable?.rows === loaded.apiCategories &&
        subjectTable?.rows === loaded.apiSubjects,
      { summary, apiCategories: loaded.apiCategories, apiSubjects: loaded.apiSubjects, tables: loaded.tables.slice(0, 5) },
    );
    const normalizedHeaders = (table) => (table?.headers || []).map((header) => header.toUpperCase());
    const expected = ["CATEGORY", "SUBJECTS", "QUESTIONS", "EXPLORE"];
    record("category-table-headers", expected.every((header) => normalizedHeaders(categoryTable).includes(header)), categoryTable);
    record("subject-table-headers", ["SUBJECT", "QUESTIONS", "WORKSPACE"].every((header) => normalizedHeaders(subjectTable).includes(header)), subjectTable);

    const beforeRefresh = overviewRequests;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.waitForTimeout(1800);
    await waitDatasets();
    record("refresh", overviewRequests > beforeRefresh, { before: beforeRefresh, after: overviewRequests });

    const copyButton = page.getByRole("button", { name: "Copy command", exact: true });
    await copyButton.click();
    await page.waitForTimeout(250);
    const copied = await page.evaluate(async () => {
      const toast = [...document.querySelectorAll(".toast")].map((node) => node.innerText).join("\n");
      let clipboard = "";
      try {
        clipboard = await navigator.clipboard.readText();
      } catch (error) {
        clipboard = `ERROR:${error.message}`;
      }
      return { toast, clipboard };
    });
    record("copy-command", copied.clipboard === loaded.copyCode && /copied/i.test(copied.toast), copied);

    const exploreAll = page.getByRole("link", { name: "Explore all", exact: true });
    await exploreAll.click();
    await page.waitForFunction(() => /Examination explorer/i.test(document.querySelector("main h1")?.textContent || ""), undefined, { timeout: 30000 });
    record("explore-all-link", page.url().includes("#/exams"), { url: page.url(), heading: await page.locator("main h1").innerText() });
    await page.goBack();
    await waitDatasets();

    const categoryRow = page.locator("main table").filter({ hasText: "Categories and their stored question counts" }).locator("tbody tr").first();
    const categoryHref = await categoryRow.locator("a").first().getAttribute("href");
    await categoryRow.locator("a").first().click();
    await page.waitForTimeout(700);
    record("category-open-link", page.url().includes("#/exams?category=") && page.url().includes(encodeURIComponent(categoryHref.split("category=")[1])), {
      href: categoryHref,
      url: page.url(),
    });
    await page.goBack();
    await waitDatasets();

    const subjectRow = page.locator("main table").filter({ hasText: "Subjects in the local corpus" }).locator("tbody tr").first();
    const subjectLink = subjectRow.locator("a").first();
    const subjectHref = await subjectLink.getAttribute("href");
    await subjectLink.click();
    await page.waitForTimeout(900);
    record("subject-open-link", page.url().includes("/overview"), { href: subjectHref, url: page.url() });
    await page.goBack();
    await waitDatasets();

    const questionBank = page.locator("main").getByRole("link", { name: "Question bank", exact: true });
    await questionBank.click();
    await page.waitForTimeout(700);
    record("question-bank-link", page.url().includes("#/exams/questions"), { url: page.url() });
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
