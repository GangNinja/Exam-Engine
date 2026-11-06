#!/usr/bin/env node
/**
 * Vendor the pinned Hairline ESM build into the dashboard's static assets.
 *
 * The dashboard has no runtime build step and performs no network requests.
 * The published package is copied once, pinned by package-lock.json, with its
 * MIT licence and a provenance record so the served bytes are auditable.
 */
import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..");
const workspace = resolve(frontend, "..");
const packageRoot = resolve(frontend, "node_modules/@lucasmarkes/hairline");
const target = resolve(workspace, "src/exam_engine/dashboard/static/vendor/hairline");

const declared = JSON.parse(await readFile(resolve(frontend, "package.json"), "utf8"));
const expected = declared.dependencies?.["@lucasmarkes/hairline"];
if (!expected || /[~^*><=]/.test(expected)) {
  throw new Error(`Hairline must be pinned to an exact version, found ${expected ?? "nothing"}`);
}

const packageJson = JSON.parse(await readFile(resolve(packageRoot, "package.json"), "utf8"));
if (packageJson.version !== expected) {
  throw new Error(`installed Hairline ${packageJson.version} does not match pinned ${expected}`);
}
if (packageJson.license !== "MIT") {
  throw new Error(`unexpected Hairline licence: ${packageJson.license}`);
}

const entry = resolve(packageRoot, "dist/index.js");
const licence = resolve(packageRoot, "LICENSE");
const bytes = await readFile(entry);
const sha256 = createHash("sha256").update(bytes).digest("hex");

await mkdir(target, { recursive: true });
await copyFile(entry, resolve(target, "index.js"));
await copyFile(licence, resolve(target, "LICENSE"));
await writeFile(
  resolve(target, "PROVENANCE.json"),
  `${JSON.stringify(
    {
      package: packageJson.name,
      version: packageJson.version,
      license: packageJson.license,
      source: "https://github.com/lucasmarkes/hairline",
      documentation: "https://hairline.lucasmarkes.com/",
      entry: "dist/index.js",
      sha256,
      vendored_by: "frontend/scripts/sync-hairline.mjs",
      runtime_network_access: false,
      note: "Illustrations are decorative and never represent application data.",
    },
    null,
    2,
  )}\n`,
  "utf8",
);
console.log(`vendored ${packageJson.name}@${packageJson.version} (${sha256.slice(0, 12)}…)`);
