import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.CHARTS_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/chart-tests.json";
const routes = [
  "/dashboard",
  "/exams",
  "/exams/questions",
  "/exams/papers",
  "/c/gate-physics/overview",
  "/c/gate-physics/analysis/distribution",
  "/c/gate-physics/analysis/coverage",
  "/c/gate-physics/analysis/exam-dna",
  "/c/gate-physics/analysis/evolution",
];
const executable = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"].find((candidate) => existsSync(candidate));
const results = [];
let page;
let browser;

async function waitPage() {
  await page.waitForFunction(
    () => {
      const heading = document.querySelector("main h1")?.textContent || "";
      return heading && !/^(Opening|Loading)/.test(heading);
    },
    undefined,
    { timeout: 60000 },
  );
  await page.waitForTimeout(700);
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
    for (const route of routes) {
      await page.goto(`${base}/?qa=charts#${route}`, { waitUntil: "domcontentloaded" });
      await waitPage();
      await page.evaluate(() => {
        for (const detail of document.querySelectorAll("details.chart-data")) detail.open = true;
      });
      await page.waitForTimeout(350);
      const figures = await page.evaluate(() =>
        [...document.querySelectorAll("main figure")].map((figure) => {
          const svg = figure.querySelector("svg");
          const chartLabels = [...figure.querySelectorAll(".chart-label")].map((node) => node.textContent.trim());
          const chartValues = [...figure.querySelectorAll(".chart-value")].map((node) => node.textContent.trim());
          const table = figure.querySelector("table");
          const rows = [...(table?.querySelectorAll("tbody tr") || [])].map((row) =>
            [...row.querySelectorAll("th,td")].map((cell) => cell.textContent.trim()),
          );
          const headers = [...(table?.querySelectorAll("thead th") || [])].map((node) => node.textContent.trim());
          const tableValues = rows.map((row) => row[row.length - 1]);
          const description =
            figure.querySelector("figcaption")?.innerText.trim() ||
            svg?.querySelector("title")?.textContent?.trim() ||
            svg?.getAttribute("aria-label") ||
            svg?.querySelector("desc")?.textContent?.trim() ||
            "";
          return {
            caption: figure.querySelector(".chart-caption strong")?.textContent?.trim() || figure.querySelector("figcaption")?.innerText.trim().split("\n")[0] || "",
            description,
            svgTitle: svg?.querySelector("title")?.textContent?.trim() || "",
            svgDesc: svg?.querySelector("desc")?.textContent?.trim() || "",
            chartLabels,
            chartValues,
            hasTable: Boolean(table),
            headers,
            rows,
            tableValues,
            valuesMatch:
              chartValues.length > 0 &&
              chartValues.length === tableValues.length &&
              chartValues.every((value, index) => value === tableValues[index]),
            comparableBar: chartValues.length > 0,
          };
        }),
      );
      results.push({ route, figures });
    }
  } finally {
    await browser.close();
  }
  const allFigures = results.flatMap((item) => item.figures);
  const barFigures = allFigures.filter((item) => item.comparableBar);
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    routes: routes.length,
    consoleErrors: errors,
    summary: {
      chartFigures: allFigures.length,
      withDescription: allFigures.filter((item) => item.description.length > 0).length,
      withDataTable: allFigures.filter((item) => item.hasTable && item.rows.length > 0).length,
      comparableBarCharts: barFigures.length,
      barValuesMatching: barFigures.filter((item) => item.valuesMatch).length,
      barValuesMismatching: barFigures.filter((item) => !item.valuesMatch).map((item) => item.caption),
      withoutDescription: allFigures.filter((item) => !item.description).map((item) => item.caption),
      withoutTable: allFigures.filter((item) => !item.hasTable || item.rows.length === 0).map((item) => item.caption),
    },
    results,
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify(output.summary));
  if (output.summary.withoutDescription.length || output.summary.withoutTable.length || output.summary.barValuesMismatching.length) process.exitCode = 1;
}

await main();
