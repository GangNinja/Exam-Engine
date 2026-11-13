import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.A11Y_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/accessibility-tests.json";
const routes = [
  "/dashboard",
  "/exams",
  "/exams/questions",
  "/exams/papers",
  "/new",
  "/datasets",
  "/recovery",
  "/c/gate-physics/overview",
  "/c/gate-physics/course",
  "/c/gate-physics/syllabus",
  "/c/gate-physics/analysis/distribution",
  "/c/gate-physics/analysis/coverage",
  "/c/gate-physics/analysis/exam-dna",
  "/c/gate-physics/analysis/evolution",
  "/c/gate-physics/learning",
  "/c/gate-physics/learning/paths",
  "/c/gate-physics/practice",
  "/c/gate-physics/tutor",
];
const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];
const executable = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].find((candidate) => existsSync(candidate));
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
  await page.waitForTimeout(600);
}

async function main() {
  if (!executable) throw new Error("Chrome/Edge executable not found");
  browser = await chromium.launch({ executablePath: executable, headless: true });
  const context = await browser.newContext({ viewport: viewports[0] });
  page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  try {
    for (const viewport of viewports) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      for (const route of routes) {
        await page.goto(`${base}/?qa=a11y#${route}`, { waitUntil: "domcontentloaded" });
        await waitPage();
        const checks = await page.evaluate(({ route, viewport }) => {
          const visible = (element) => {
            if (!element) return false;
            const style = getComputedStyle(element);
            return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
          };
          const nameFor = (element) => {
            const aria = element.getAttribute("aria-label");
            if (aria?.trim()) return aria.trim();
            const labelledBy = element.getAttribute("aria-labelledby");
            if (labelledBy) {
              const text = labelledBy
                .split(/\s+/)
                .map((id) => document.getElementById(id)?.innerText || "")
                .join(" ")
                .trim();
              if (text) return text;
            }
            if (element.labels?.length) {
              const text = [...element.labels].map((label) => label.innerText).join(" ").trim();
              if (text) return text;
            }
            const text = (element.innerText || element.textContent || element.value || element.getAttribute("placeholder") || element.getAttribute("title") || "").trim();
            return text;
          };
          const controls = [...document.querySelectorAll("a,button,input,select,textarea,summary")].filter(visible);
          const unnamed = controls
            .filter((element) => !nameFor(element))
            .slice(0, 12)
            .map((element) => ({ tag: element.tagName, type: element.type || null, id: element.id || null, html: element.outerHTML.slice(0, 220) }));
          const unnamedCount = controls.filter((element) => !nameFor(element)).length;
          const tables = [...document.querySelectorAll("main table")].map((table) => {
            const wrapper = table.parentElement;
            const style = wrapper ? getComputedStyle(wrapper) : null;
            return {
              caption: table.querySelector("caption")?.innerText || "",
              rows: table.querySelectorAll("tbody tr").length,
              headers: [...table.querySelectorAll("thead th")].map((th) => th.innerText.trim()).slice(0, 12),
              headerScopes: [...table.querySelectorAll("thead th")].every((th) => ["col", "row"].includes(th.getAttribute("scope"))) || table.querySelectorAll("thead th").length === 0,
              wrapperOverflowX: style?.overflowX || null,
              wrapperWidth: wrapper?.clientWidth || 0,
              tableWidth: table.scrollWidth || 0,
              pageContained: !style || style.overflowX === "auto" || style.overflowX === "scroll" || table.scrollWidth <= (wrapper?.clientWidth || 0) + 1,
            };
          });
          const charts = [...document.querySelectorAll("main svg")].filter((svg) => visible(svg));
          const chartDescriptions = [...document.querySelectorAll("main [role=img], main figure figcaption, main .chart-title, main [aria-label]")].filter(visible).map((node) => (node.getAttribute("aria-label") || node.innerText || "").trim()).filter(Boolean);
          const dialog = document.querySelector(".command-dialog, dialog[open]");
          const dialogRect = dialog?.getBoundingClientRect();
          const dialogFits = !dialogRect || (dialogRect.left >= 0 && dialogRect.top >= 0 && dialogRect.right <= window.innerWidth + 1 && dialogRect.bottom <= window.innerHeight + 1);
          return {
            route,
            viewport,
            heading: document.querySelector("main h1")?.innerText || "",
            bodyClientWidth: document.documentElement.clientWidth,
            bodyScrollWidth: document.documentElement.scrollWidth,
            pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
            mainOverflow: document.querySelector("main") ? document.querySelector("main").scrollWidth > document.querySelector("main").clientWidth + 1 : false,
            controls: controls.length,
            unnamedCount,
            unnamed,
            tables,
            tableContained: tables.every((table) => table.pageContained),
            tableHeadersScoped: tables.every((table) => table.headerScopes),
            chartSvgCount: charts.length,
            chartDescriptions: [...new Set(chartDescriptions)].slice(0, 8),
            dialogFits,
          };
        }, { route, viewport: viewport.name });
        results.push(checks);
      }
    }

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${base}/?qa=a11y#/dashboard`, { waitUntil: "domcontentloaded" });
    await waitPage();
    await page.keyboard.press("Tab");
    await page.waitForTimeout(100);
    const focusRings = [];
    for (let index = 0; index < 8; index += 1) {
      const focus = await page.evaluate(() => {
        const element = document.activeElement;
        if (!element) return null;
        const style = getComputedStyle(element);
        return {
          tag: element.tagName,
          text: (element.innerText || element.getAttribute("aria-label") || "").trim().slice(0, 80),
          outlineStyle: style.outlineStyle,
          outlineWidth: style.outlineWidth,
          boxShadow: style.boxShadow,
        };
      });
      if (focus) focusRings.push(focus);
      await page.keyboard.press("Tab");
      await page.waitForTimeout(80);
    }
    const visibleFocus = focusRings.filter((item) => item.outlineStyle !== "none" || item.boxShadow !== "none");
    const keyboard = { focusRings, visibleFocusCount: visibleFocus.length, pass: visibleFocus.length > 0 };

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${base}/?qa=a11y#/dashboard`, { waitUntil: "domcontentloaded" });
    await waitPage();
    await page.keyboard.press("Control+k");
    await page.waitForTimeout(300);
    const commandOpen = await page.evaluate(() => {
      const dialog = document.querySelector(".command-dialog, dialog[open]");
      const rect = dialog?.getBoundingClientRect();
      return {
        open: Boolean(dialog),
        fits: !rect || (rect.left >= 0 && rect.top >= 0 && rect.right <= window.innerWidth + 1 && rect.bottom <= window.innerHeight + 1),
        width: rect?.width || 0,
        height: rect?.height || 0,
        active: document.activeElement?.id || document.activeElement?.tagName,
      };
    });
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    const commandClosed = await page.evaluate(() => {
      const dialog = document.querySelector(".command-dialog");
      return !dialog || !dialog.open;
    });
    const overview = {
      routeCount: routes.length,
      viewportCount: viewports.length,
      overflowFailures: results.filter((item) => item.pageOverflow || item.mainOverflow).map((item) => `${item.viewport}:${item.route}`),
      unnamedControls: results.reduce((total, item) => total + item.unnamedCount, 0),
      tableContainmentFailures: results.filter((item) => !item.tableContained).map((item) => `${item.viewport}:${item.route}`),
      tableHeaderFailures: results.filter((item) => !item.tableHeadersScoped).map((item) => `${item.viewport}:${item.route}`),
      dialogFailures: results.filter((item) => !item.dialogFits).map((item) => `${item.viewport}:${item.route}`),
      keyboard,
      commandOpen,
      commandClosed,
    };
    console.error(JSON.stringify(overview));
    results.push({ kind: "keyboard", ...keyboard });
    results.push({ kind: "command-dialog", open: commandOpen, closed: commandClosed, pass: commandOpen.open && commandOpen.fits && commandClosed });
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    consoleErrors,
    routes,
    viewports,
    summary: {
      totalChecks: results.filter((item) => !item.kind).length,
      pageOverflowFailures: results.filter((item) => item.pageOverflow || item.mainOverflow).length,
      unnamedControlInstances: results.reduce((total, item) => total + (item.unnamedCount || 0), 0),
      tableContainmentFailures: results.filter((item) => item.tableContained === false).length,
      tableHeaderFailures: results.filter((item) => item.tableHeadersScoped === false).length,
      dialogFailures: results.filter((item) => item.dialogFits === false).length,
      keyboardFocusPass: results.find((item) => item.kind === "keyboard")?.pass ?? false,
      commandDialogPass: results.find((item) => item.kind === "command-dialog")?.pass ?? false,
    },
    results,
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify(output.summary));
}

await main();
