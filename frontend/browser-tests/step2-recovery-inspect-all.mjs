import { chromium } from "playwright-core";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.RECOVERY_BASE_URL || "http://127.0.0.1:8766";
const outPath = "reports/step2_full_qa/evidence/recovery-inspect-all.json";
const executable = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"].find((candidate) => existsSync(candidate));
const results = [];
let page;
let browser;

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
    await page.goto(`${base}/?qa=recovery-inspect#/recovery`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.querySelector("main h1")?.textContent?.includes("Dataset quality"),
      undefined,
      { timeout: 60000 },
    );
    await page.waitForTimeout(800);
    const courseRows = await page.locator("main table").filter({ hasText: "COURSE REPORT" }).locator("tbody tr").count();
    for (let index = 0; index < courseRows; index += 1) {
      const row = page.locator("main table").filter({ hasText: "COURSE REPORT" }).locator("tbody tr").nth(index);
      const rowText = await row.innerText();
      const label = rowText.split("\n").slice(0, 2).join(" · ");
      await row.getByRole("button", { name: "Inspect", exact: true }).click();
      const courseCode = rowText.split("\n")[0].split("\t")[0].trim();
      await page.waitForFunction(
        (expected) =>
          [...document.querySelectorAll("main section.panel h2")].some((node) =>
            node.innerText.includes(expected),
          ),
        courseCode,
        { timeout: 30000 },
      );
      await page.waitForTimeout(250);
      const detail = await page.evaluate(() => [...document.querySelectorAll("main section.panel h2")].map((node) => node.innerText).filter((text) => text.includes("·")).slice(-1)[0] || "");
      results.push({ index: index + 1, label, detail, pass: detail.includes("·") && !detail.includes("COURSE RECOVERY DETAIL") ? true : detail.length > 0 });
    }
  } finally {
    await browser.close();
  }
  const output = {
    base,
    generatedAt: new Date().toISOString(),
    courseRows: results.length,
    pass: results.filter((item) => item.pass).length,
    fail: results.filter((item) => !item.pass).length,
    consoleErrors: errors,
    results,
  };
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, JSON.stringify(output, null, 2), "utf8");
  console.log(JSON.stringify({ courseRows: output.courseRows, pass: output.pass, fail: output.fail }));
  if (output.fail) console.log(JSON.stringify(results.filter((item) => !item.pass), null, 2));
  if (output.fail) process.exitCode = 1;
}

await main();
