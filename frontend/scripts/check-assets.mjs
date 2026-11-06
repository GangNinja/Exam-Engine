#!/usr/bin/env node
/**
 * No-build production asset checks for the dashboard.
 *
 * The dashboard ships plain ES modules; there is no bundler step. This script
 * therefore checks every first-party module with Node's parser, verifies that
 * HTML asset references resolve inside the static directory, and confirms the
 * vendored Hairline build matches its recorded provenance hash.
 */
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFile, readdir, stat } from "node:fs/promises";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const workspace = resolve(here, "../..");
const staticDir = resolve(workspace, "src/exam_engine/dashboard/static");
const vendorDir = resolve(staticDir, "vendor/hairline");

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      if (path === vendorDir) continue;
      files.push(...(await walk(path)));
    } else if (entry.isFile()) {
      files.push(path);
    }
  }
  return files;
}

const failures = [];
const files = await walk(staticDir);
const modules = files.filter((file) => [".js", ".mjs"].includes(extname(file)));
for (const file of modules) {
  try {
    execFileSync(process.execPath, ["--check", file], { stdio: "pipe" });
  } catch (error) {
    failures.push(`${relative(workspace, file)}: syntax check failed\n${error.stderr || error.message}`);
  }
}

const html = await readFile(resolve(staticDir, "index.html"), "utf8");
const references = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map((match) => match[1]);
for (const reference of references) {
  if (reference.startsWith("#")) continue;
  if (!reference.startsWith("/assets/")) {
    failures.push(`index.html references a non-local asset: ${reference}`);
    continue;
  }
  if (reference.endsWith("/")) continue;
  const target = resolve(staticDir, reference.slice("/assets/".length));
  const inside = relative(staticDir, target);
  if (inside.startsWith("..") || inside.includes(`..${process.platform === "win32" ? "\\" : "/"}`)) {
    failures.push(`index.html asset escapes the static directory: ${reference}`);
    continue;
  }
  try {
    if (!(await stat(target)).isFile()) throw new Error("not a file");
  } catch {
    failures.push(`index.html references a missing asset: ${reference}`);
  }
}

const provenance = JSON.parse(await readFile(resolve(vendorDir, "PROVENANCE.json"), "utf8"));
const vendored = await readFile(resolve(vendorDir, provenance.entry.replace(/^dist\//, "")));
const digest = createHash("sha256").update(vendored).digest("hex");
if (digest !== provenance.sha256) {
  failures.push(
    `Hairline provenance mismatch: expected ${provenance.sha256}, served ${digest}`,
  );
}
for (const required of ["LICENSE", "PROVENANCE.json", "index.js"]) {
  try {
    await stat(resolve(vendorDir, required));
  } catch {
    failures.push(`vendored Hairline is missing ${required}`);
  }
}

if (failures.length) {
  console.error(`Asset checks failed (${failures.length}):`);
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}
console.log(
  `Asset checks passed: ${modules.length} first-party module(s), ${references.length} local reference(s), Hairline ${provenance.version} (${provenance.license}).`,
);
