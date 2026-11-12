import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:8765";
const course = process.env.QA_COURSE || "gate-physics";
const routes = [
  "/dashboard",
  "/new",
  "/datasets",
  "/recovery",
  "/exams",
  "/exams/questions",
  "/exams/papers",
  `/c/${course}/overview`,
  `/c/${course}/course`,
  `/c/${course}/analysis/coverage`,
  `/c/${course}/analysis/distribution`,
  `/c/${course}/analysis/exam-dna`,
  `/c/${course}/analysis/evolution`,
  `/c/${course}/learning`,
  `/c/${course}/learning/paths`,
  `/c/${course}/practice`,
  `/c/${course}/tutor`,
  `/c/${course}/syllabus`,
];

function executable() {
  const candidates = [
    process.env.BROWSER_EXECUTABLE,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ];
  return candidates.find((item) => item && existsSync(item));
}

async function waitForReady(page) {
  await page.waitForFunction(
    () => {
      const database = document.querySelector("#db-name")?.textContent || "";
      return database && !database.includes("connecting to database");
    },
    { timeout: 30000 },
  );
  await page.waitForFunction(
    () => {
      const heading = document.querySelector("main h1")?.textContent || "";
      const loading = /^(Opening the local database|Loading course workspace)/.test(heading.trim());
      return Boolean(document.querySelector("main .error-state")) || (heading && !loading);
    },
    { timeout: 30000 },
  );
  await page.waitForFunction(
    () => !document.querySelector("main .loading-panel, main .boot-panel"),
    { timeout: 30000 },
  );
  await page.waitForTimeout(500);
}

async function inspectPage(page, route) {
  return page.evaluate((route) => {
    const text = (node) => (node?.innerText || "").replace(/\s+/g, " ").trim();
    const control = (el) => ({
      tag: el.tagName.toLowerCase(),
      text: text(el).slice(0, 180),
      aria: el.getAttribute("aria-label"),
      href: el.getAttribute("href"),
      disabled: Boolean(el.disabled),
      type: el.getAttribute("type"),
      value: el.value ?? null,
    });
    const tables = [...document.querySelectorAll("table")].map((table) => ({
      caption: text(table.querySelector("caption")),
      headers: [...table.querySelectorAll("thead th")].map((th) => text(th)),
      rows: [...table.querySelectorAll("tbody tr")].map((tr) =>
        [...tr.querySelectorAll(":scope > th, :scope > td")].map((cell) => text(cell)),
      ),
      visible: Boolean(table.offsetParent),
    }));
    const charts = [...document.querySelectorAll("figure.chart, .chart")].map((figure) => ({
      title: text(figure.querySelector(".chart-caption strong")),
      aria: figure.querySelector("[role=img]")?.getAttribute("aria-label") || null,
      dataTable: [...figure.querySelectorAll("details.chart-data")].map((details) => ({
        open: details.open,
        headers: [...details.querySelectorAll("thead th")].map((th) => text(th)),
        rows: [...details.querySelectorAll("tbody tr")].map((tr) =>
          [...tr.querySelectorAll("th, td")].map((cell) => text(cell)),
        ),
      })),
    }));
    return {
      route,
      url: location.href,
      title: document.title,
      heading: text(document.querySelector("main h1")),
      main: text(document.querySelector("main")).slice(0, 4000),
      error: text(document.querySelector("main .error-state, #view > .error-state")),
      counts: {
        links: document.querySelectorAll("main a").length,
        buttons: document.querySelectorAll("main button").length,
        selects: document.querySelectorAll("main select").length,
        inputs: document.querySelectorAll("main input, main textarea").length,
        details: document.querySelectorAll("main details").length,
        tables: tables.length,
        charts: charts.length,
        svg: document.querySelectorAll("main svg").length,
      },
      controls: {
        links: [...document.querySelectorAll("main a")].map(control),
        buttons: [...document.querySelectorAll("main button")].map(control),
        selects: [...document.querySelectorAll("main select")].map((select) => ({
          ...control(select),
          options: [...select.options].map((option) => ({
            text: text(option),
            value: option.value,
            selected: option.selected,
          })),
        })),
        inputs: [...document.querySelectorAll("main input, main textarea")].map(control),
      },
      tables,
      charts,
      scroll: {
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight,
      },
    };
  }, route);
}

async function main() {
  const browserPath = executable();
  if (!browserPath) throw new Error("No Chrome/Edge/Chromium executable found");
  const browser = await chromium.launch({ executablePath: browserPath, headless: true });
  const results = [];
  try {
    for (const [label, viewport] of [
      ["desktop", { width: 1440, height: 900 }],
      ["mobile", { width: 390, height: 844 }],
    ]) {
      const page = await browser.newPage({ viewport });
      const consoleErrors = [];
      const pageErrors = [];
      const badResponses = [];
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });
      page.on("pageerror", (error) => pageErrors.push(error.message));
      page.on("response", (response) => {
        if (response.status() >= 400) {
          badResponses.push({ status: response.status(), url: response.url() });
        }
      });
      for (const route of routes) {
        const beforeConsole = consoleErrors.length;
        const beforePage = pageErrors.length;
        try {
          await page.goto(`${baseUrl}/#${route}`, { waitUntil: "domcontentloaded", timeout: 30000 });
          await waitForReady(page);
          const summaries = page.locator("details.chart-data summary");
          for (let index = 0; index < (await summaries.count()); index += 1) {
            await summaries.nth(index).click({ timeout: 3000 }).catch(() => {});
          }
          const info = await inspectPage(page, route);
          results.push({
            viewport: label,
            ...info,
            consoleErrors: consoleErrors.slice(beforeConsole),
            pageErrors: pageErrors.slice(beforePage),
            badResponses: badResponses.splice(0),
          });
        } catch (error) {
          results.push({
            viewport: label,
            route,
            url: page.url(),
            failure: `${error?.name || "Error"}: ${error?.message || String(error)}`,
            consoleErrors: consoleErrors.slice(beforeConsole),
            pageErrors: pageErrors.slice(beforePage),
            badResponses: badResponses.splice(0),
          });
        }
      }
      await page.close();
    }
  } finally {
    await browser.close();
  }
  const outputDirectory = path.resolve(process.cwd(), "reports/step2_full_qa");
  await mkdir(outputDirectory, { recursive: true });
  const output = path.join(outputDirectory, "ui-sweep.json");
  await writeFile(output, JSON.stringify(results, null, 2), "utf8");
  const summary = results.map((item) => ({
    viewport: item.viewport,
    route: item.route,
    title: item.title,
    heading: item.heading,
    failure: item.failure,
    error: item.error,
    counts: item.counts,
    controls: item.controls && {
      links: item.controls.links.length,
      buttons: item.controls.buttons.length,
      selects: item.controls.selects.length,
      inputs: item.controls.inputs.length,
    },
    consoleErrors: item.consoleErrors?.length || 0,
    pageErrors: item.pageErrors?.length || 0,
    badResponses: item.badResponses?.length || 0,
    scroll: item.scroll,
  }));
  console.log(JSON.stringify({ output, pages: results.length, summary }, null, 2));
}

await main();
