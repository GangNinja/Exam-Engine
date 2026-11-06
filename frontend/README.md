# Dashboard frontend assets

The Phase 12 dashboard is framework-free HTML, CSS and ES modules. There is no
runtime build step, no bundler, no CDN and no network access: the Python server
serves the files in `src/exam_engine/dashboard/static/` directly.

This directory exists only to pin and vendor third-party browser code.

| Item | Version | Licence | Runtime |
|---|---|---|---|
| [`@lucasmarkes/hairline`](https://github.com/lucasmarkes/hairline) | 0.5.1 (exact) | MIT | Vendored ESM in `src/exam_engine/dashboard/static/vendor/hairline/` |

## Updating Hairline

```bash
cd frontend
npm install                     # installs the exact version in package-lock.json
npm run sync:hairline           # verifies licence/version and re-vendors dist/index.js
npm run check:assets
npm test
```

`scripts/sync-hairline.mjs` refuses a range, a version mismatch or a non-MIT
licence, and records the SHA-256 of the served entry point in
`vendor/hairline/PROVENANCE.json`. The Python test suite verifies that record.

Hairline's React entry point (`@lucasmarkes/hairline/react`) is deliberately not
used or vendored: the dashboard has no React runtime and uses the framework-free
DOM API instead.

## Frontend tests

`npm test` runs Node's built-in test runner over `tests/`: the pure tutor state
machine and formatting helpers (`ui-state.js`), chart data (`charts.js`), API
response parsing, the avatar's state/caption/cleanup contract, and static-asset
guarantees (local references, CSP-compatible scripts, reduced motion, Hairline
pin/provenance). Browser rendering is exercised through the Python HTTP tests
plus manual verification; no competing browser test framework is introduced.
