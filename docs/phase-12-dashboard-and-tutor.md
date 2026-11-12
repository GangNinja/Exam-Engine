# Phase 12 — Integrated Dashboard, Interactive Illustrations and Animated Tutor

A local educational-intelligence workspace over the Phase 1–11 services, a
framework-free browser client, vendored Hairline illustrations, and an original
animated tutor that combines stored corpus evidence, cited web sources and an
optional local model, with an extractive fallback.

**Status:** complete for the software deliverables described here. The dashboard
starts, every workspace reads and writes through the existing service layer, the
production package contains its assets, and all Python and Node checks pass.
Practice now includes local answer checks, concept-expanding batches and an
explicit Draft/approve review flow. The limitations in §13 are real: answer checks
are lexical study feedback rather than an examination grade, and there is no
authentication, forecasting, a full automated browser E2E suite or labelled
real-world accuracy validation. No educational-accuracy claim is made.

---

## 1. Baseline and constraints

Before Phase 12 the repository was a Python 3.11+ CLI/library application:

* SQLite with checksum-verified migrations, no web framework, no API server.
* Pydantic models and service classes per phase (`nlp`, `understanding`,
  `analytics`, `graph`, `prerequisite`, `coverage`, `exam_dna`, `evolution`,
  `generation`, `advanced`).
* Typer command groups; no frontend, no JavaScript dependencies, no bundler.
* 602 passing tests.

Two constraints shaped the design:

1. **Do not introduce a new framework to host a dashboard.** The application is a
   local, single-user tool. A React/Next or FastAPI rewrite would add a runtime,
   a build step and a second source of truth for the API.
2. **Do not duplicate service logic in HTTP handlers.** Every screen calls the
   same services the CLI calls, through a thin adapter.

The result is a Python standard-library HTTP server plus framework-free ES
modules. There is no bundler, no CDN and no telemetry. The dashboard itself uses
loopback; optional web-search and model endpoints are described in §8.

---

## 2. Architecture

```
src/exam_engine/
  cli_dashboard.py        `exam-engine dashboard` command, loopback guard
  generation/grading.py   local MCQ and lexical free-text answer checks
  dashboard/
    api.py                HTTP-facing adapter over the Phase 1-11 services
    errors.py             domain exception -> stable JSON error mapping
    jobs.py               bounded in-process job runner + de-duplication
    server.py             threading HTTP server, static host, CSP, same-origin
    tutor.py              course-scoped AI tutor, grounded fallback + capability report
    tutor_ai.py           local Ollama / OpenAI-compatible model adapter
    websearch.py          optional Exa and keyless Wikipedia retrieval
    static/
      index.html          shell, skip link, CSP-compatible module entry
      app.js              hash router, course context, theme, toasts
      api.js              fetch wrapper, cancellation, typed errors
      dom.js              safe element builder (textContent, no innerHTML)
      ui-state.js         pure formatting + tutor state machine (Node-tested)
      charts.js           SVG bar/segment charts from real values (Node-tested)
      hairline.js         lazy Hairline adapter, cleanup and fallback
      tutor-avatar.js     original SVG avatar, six states, text captions
      styles.css          tokens, light/dark themes, responsive layout
      views/              overview, course, analysis, learning, practice, tutor
      vendor/hairline/    pinned MIT build + LICENSE + PROVENANCE.json
frontend/                 exact dependency pin, vendoring script, Node tests
tests/test_dashboard.py   API, isolation, jobs, upload, HTTP and CSP tests
```

Request flow:

```
browser ES module
  -> fetch /api/...
  -> DashboardRequestHandler (same-origin check, size limit, routing)
  -> DashboardAPI (validation, course scope)
  -> existing Phase 1-11 service
  -> SQLite
```

Long-running work goes through `JobManager`: a bounded thread pool opens its own
database connection, de-duplicates an active job of the same kind for the same
course, and exposes `queued`/`running`/`succeeded`/`failed` plus duration and
error. Jobs are intentionally ephemeral; the data they write is durable. There is
no durable queue, because no earlier phase had one.

### 2.1 API surface

All routes are course-scoped beneath `/api/courses/{course_id}` unless stated.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/health` | liveness, version, explicit `authentication.enabled=false` |
| GET | `/api/capabilities` | implemented modules, tutor capabilities, artwork licences, privacy |
| GET | `/api/courses` | course list with data-state summary |
| POST | `/api/courses` | create a course offering |
| GET | `/api/courses/{id}` | course workspace, documents, syllabi, status, next steps |
| GET | `/api/courses/{id}/documents` | documents and provenance |
| POST | `/api/courses/{id}/documents` | multipart upload through Phase 1 validation |
| GET | `/api/courses/{id}/questions` | paged question list with filters |
| GET | `/api/courses/{id}/analytics` | Phase 4 report |
| GET | `/api/courses/{id}/coverage` | Phase 7 report, with `syllabus_id` |
| GET | `/api/courses/{id}/coverage/concepts/{cid}` | concept evidence |
| GET | `/api/courses/{id}/exam-dna` | Phase 8 profile |
| GET | `/api/courses/{id}/evolution` | Phase 9 report |
| GET | `/api/courses/{id}/graph` | Phase 5 nodes and edges |
| GET | `/api/courses/{id}/graph/neighborhood` | bounded neighborhood |
| GET | `/api/courses/{id}/prerequisites` | Phase 6 candidates/paths |
| POST | `/api/courses/{id}/learning-path` | dependency-ordered path |
| GET | `/api/courses/{id}/generation/options` | real generation scope, formats and targets |
| POST | `/api/courses/{id}/generate` | Phase 10 generation |
| GET | `/api/courses/{id}/generated` | stored questions and batches |
| POST | `/api/courses/{id}/generated/{qid}/review` | review decision |
| POST | `/api/courses/{id}/generated/{qid}/grade` | local answer check for a stored practice question |
| GET | `/api/courses/{id}/advanced` | Phase 11 capabilities and stored counts |
| POST | `/api/courses/{id}/advanced/mutate` | derive variants of a stored question |
| POST | `/api/courses/{id}/advanced/adversarial` | generate a grounded challenge with a rubric |
| GET | `/api/courses/{id}/misconceptions` | Phase 11 records |
| POST | `/api/courses/{id}/misconceptions/analyse` | misconception mining |
| GET | `/api/courses/{id}/tutor/context` | concepts available to the tutor |
| POST | `/api/courses/{id}/tutor/ask` | grounded or model-assisted explanation |
| POST | `/api/courses/{id}/tutor/ask/stream` | the same tutor response over server-sent events, with status and answer progress |
| POST | `/api/courses/{id}/analysis/{kind}` | background job: nlp, understanding, graph, prerequisites |
| GET | `/api/jobs`, `/api/jobs/{job_id}` | job polling |

Errors use one envelope (`error.code`, `error.message`, `error.detail`,
`request_id`). Domain exceptions map to 400/404/409/422; unexpected exceptions
become a 500 without internal detail. Every response carries security headers and
a request id.

The streaming tutor route uses `text/event-stream` with `status`, `answer`,
`heartbeat`, `result` and `error` events. `answer` events contain decoded
plain-text deltas from the structured `answer` field; raw model JSON is never
rendered. The final `result` event is the complete validated tutor payload, so a
client can replace its preview with the normal sections, citations, warnings and
refusal states. `/tutor/ask` remains the non-streaming contract.

### 2.2 A real integration defect found and fixed

The tutor suggested concepts by the Phase 2/5 concept id (`syc_…`), while Phase 10
generation resolves syllabus concepts by its own id. The Practice generator
initially rejected tutor suggestions with `unknown_concept`.

Rather than translate ids in the browser, Phase 12 added
`GET /api/courses/{id}/generation/options`, backed by the Phase 10 scope
resolver. Practice now selects from that real scope, and the tutor's "generate a
practice question" action resolves through the same endpoint. This is tested in
`test_generation_scope_ids_are_accepted_by_generation`, which asks for a target
from the endpoint and asserts that generation succeeds.

### 2.3 Defects found while verifying this phase

The verification pass was not limited to confirming the happy path. It found and
fixed six defects, each with a regression test or a direct end-to-end check:

1. **Tutor misdescribed graph-only concepts.** A concept known to Phase 5 but
   absent from the stored syllabus could be introduced as "recorded in this
   course's stored syllabus". The wording now distinguishes syllabus placement
   from graph-only evidence, and
   `test_tutor_does_not_claim_graph_only_concepts_are_syllabus_placed` locks it
   down.
2. **Phase 11 was advertised but not reachable from the dashboard.** Mutation and
   adversarial generation existed only in the CLI, while the capabilities
   response advertised advanced assessment. The API now exposes `advanced`,
   `mutate` and `adversarial`, and Practice includes a review-first advanced
   panel. This is covered by `test_advanced_assessment_is_exposed_and_course_scoped`.
3. **A second server could shadow a running one on Windows.**
   `allow_reuse_address` was unconditional, and Windows `SO_REUSEADDR` allows a
   second process to bind a port that is already listening. The CLI reported
   success while requests still reached the older process — observed during this
   verification. Reuse is now enabled only on POSIX;
   `test_http_server_refuses_a_port_already_in_use` asserts the bind fails.
4. **Rephrase duplicated the source's own phrasing.** A question already
   containing "in your own words" received that framing again in a mutation. The
   builder now chooses framings that do not repeat the phrase, tested by
   `test_rephrase_does_not_repeat_personalisation_already_in_the_source`.
5. **Practice kept stale panels after generating.** A new question was stored but
   the review queue still said none existed and the mutation panel still said
   there was no source question. The generation handler now refreshes the queue
   and the advanced panel from the backend.
6. **A second review on the same card failed.** `set_review_status` incremented
   the stored revision in the repository but returned the pre-update model, so
   the next optimistic save sent a stale `expected_revision` and received a
   conflict. The service now returns the stored row, and the card's badge follows
   its replacement. Covered by `test_review_returns_the_revision_it_stored`.

---

## 3. Dashboard

The client is a hash-routed single-page workspace with six areas:

| Area | Content |
|---|---|
| Overview | selected course and syllabus, data-state, ingestion and analysis status, document provenance, real coverage and distribution charts, recent jobs, next actions |
| Course | offering creation, syllabus versions, document table, upload with validation feedback |
| Analysis | Phase 4 analytics, Phase 7 coverage, Phase 8 Exam DNA, Phase 9 evolution, Phase 5 graph |
| Learning | graph/neighborhood, prerequisite candidates, learning path |
| Practice | generation scope, formats, concept-expanding batch creation, question detail, local answer checks, Draft/approve/reject decisions, Phase 11 mutation/adversarial generation |
| Tutor | prominent ask panel, compact animated avatar, concept scope, model/web status, citations, capability table and practice generation |

Design decisions:

* A restrained editorial palette (warm paper, ink, one accent), a single serif
  display face with a system sans body, and a consistent spacing scale. No
  gradients, no oversized hero, no repeated KPI tiles.
* Charts are hand-built SVG with a text summary and a data table; the illustration
  layer is explicitly labelled and never doubles as a chart.
* Light and dark themes are token-driven; the toggle persists in `localStorage`.
* Loading skeletons, explicit empty states with the next action, and error panels
  with a retry action.
* Keyboard navigation: skip link, visible focus ring, native controls, labelled
  form fields, `aria-current` navigation, charts with accessible descriptions.
* Responsive at desktop, tablet and narrow widths; the sidebar becomes an
  off-canvas drawer and tables scroll inside their own region.
* `prefers-reduced-motion` disables transitions, avatar animation and Hairline
  playback.

All user text is written with `textContent`/`createTextNode`; the only HTML string
in the client is static. That is what allows the strict `script-src 'self'` CSP.

### 3.1 Practice checks, draft states and batch expansion

Practice generation defaults `expand_batch` on. When a request asks for more
questions than the selected concept(s) can produce, the service continues through
other concepts in the selected syllabus in deterministic order. The batch report
adds a `generation_batch_expanded` note and keeps the selected concepts first.
Questions remain distinct prompts; a skipped duplicate is recorded as a
similarity decision rather than padded, and a duplicate that a spare concept fills
does not by itself turn an otherwise complete batch into `partial`. If the
syllabus has too few grounded concepts, the batch still reports the shortfall.

The Practice starting concept defaults to **Random concept from this syllabus**,
and every request carries a fresh seed. Short-answer generation can choose among
grounded wording variants, so a repeat does not have to reuse the same prompt.
Generated items remain **Draft** until a human approves them; the answer key is
kept behind a closed **Show answer** disclosure.
A successful batch scopes the review queue to that batch so earlier questions do
not look like new output; **View all stored questions** restores the full queue.

Each stored card has a `Check your answer` disclosure. Multiple-choice answers are
compared with the stored option key. Written answers are checked locally against a
stored outline built from the question's solution steps, then syllabus facets and
the stored reference as fallbacks. The response separates fully covered, partly
covered and missing key ideas, shows which words/phrases are still absent, and
returns explicit limitations; it does not return marks or an awarded score. A
small fixed set of regular/irregular plural forms and common synonyms is matched
lexically; it does not infer meaning from arbitrary paraphrases. The route caps
answers at 4,000 characters and persists no attempt. The UI calls this a **local
coverage check**, never an examination grade, and labels the outline as stored
review material rather than a marking scheme.

`pending_review` is surfaced as `Draft` in the review queue and cards. A Draft has
a quick Approve action plus explicit Approve/Reject/Save-status controls; every
decision carries the optimistic revision and is written to the question's review
history. Generation never approves an item.

---

## 4. Data integrity, isolation and failure behaviour

* Every read and write resolves a course first; unknown courses return
  `unknown_course`. Analysis, coverage, generation and tutor calls take the course
  id from the route, not from a request body.
* Syllabus version is an explicit selection. Coverage and Exam DNA calls pass it
  through; the API does not merge versions.
* Upload goes through the Phase 1 validator (suffix, magic bytes, size, page and
  text caps) and is stored under the resolved course. A rejected upload writes
  nothing. Cross-course upload attempts are tested.
* A failed optional visualization never blocks the feature: Hairline and the
  avatar are dynamically imported; on failure the host shows a text fallback and
  the rest of the screen continues to work. The tutor was manually verified with
  the avatar module removed and remained fully usable as text.
* Tutor state is presentation only. The answer text, status, evidence and review
  state come from the backend; a visual state cannot change correctness.
* Jobs are de-duplicated per course and kind, and a job failure is reported as a
  job error rather than a 500.

---

## 5. Hairline integration

**Package:** `@lucasmarkes/hairline` **0.5.1**, exact pin in
`frontend/package.json` and `frontend/package-lock.json`. **Licence:** MIT.
**Runtime:** framework-free DOM entry point, vendored, no React runtime.

Compatibility assessment: Hairline also ships React 18+ components, but the
dashboard has no React and does not need one. Its DOM factory
(`figure(element, options)`) fits the existing module architecture directly, so
the React entry point is neither installed nor vendored.

Integration:

* `frontend/scripts/sync-hairline.mjs` refuses a range or non-exact version,
  checks the licence, vendors `dist/index.js` into
  `src/exam_engine/dashboard/static/vendor/hairline/`, and records the SHA-256 in
  `PROVENANCE.json`. `frontend/scripts/check-assets.mjs` verifies the provenance,
  version, licence and served bytes.
* The wrapper `hairline.js` lazy-imports the vendored module, mounts with
  `theme: "auto"`, `intensity: 0.35`, `play: false`, and an accessible label. It
  returns a cleanup function that calls the figure's `destroy()` and clears the
  host; views register it with the router so navigation does not leak listeners.
* Three figures are used, each on the screen where it adds context:
  `hub` (knowledge-graph overview), `branches` (prerequisite relationships),
  `riffle` (practice card tray). They are labelled "Illustration" and captioned
  "Decorative only — not a data chart."
* Failure is contained: a missing figure, import failure or constructor error
  renders "Illustration unavailable. The workspace is unaffected." and logs at
  debug level.
* MIT obligations are met by keeping the upstream `LICENSE` and provenance file
  in the distribution.

Hairline is never used to render a coverage percentage, graph edge, recurrence
rate or assessment result. Those are computed server-side and drawn by
`charts.js` or rendered in tables.

---

## 6. Avatar decision and licence review

### 6.1 The question

Bible Strong Avatar Lab (`github.com/smontlouis/bible-strong-avatar-lab`) is
**AGPL-3.0-only**. Copying its renderer or bundling a derivative would make the
combined work subject to AGPL obligations, including source disclosure and, for
a modified version offered over a network, the AGPL §13 network-use offer. That
is incompatible with keeping this dashboard's licensing simple and with a
possible hosted/commercial distribution. Downloading exported avatar assets does
not by itself resolve the licence of the art or of any renderer needed to display
it.

Three options were considered:

| Option | Decision |
|---|---|
| Import the Studio/renderer and drive it from the tutor | **Rejected** — AGPL copyleft would extend to the distributed dashboard; the Studio is also far larger than needed. |
| Use exported character assets with a small custom renderer | **Rejected** — asset licensing and derivative-work status are unclear; "exported" is not a licence grant. |
| Implement an original avatar component | **Chosen** — no third-party code or assets, project MIT, no AGPL obligation. |

This is an engineering licence assessment, not legal advice. No Bible Strong
source, bundle, asset or network request is present in the repository. The
capability response reports this explicitly (`studio_bundled: false`,
`studio_license: "AGPL-3.0-only"`).

### 6.2 The component

`tutor-avatar.js` is an original inline SVG (head, glasses, orbit, thinking dots)
with six states: `idle`, `thinking`, `speaking`, `positive`, `corrective`,
`warning`. Each state changes CSS-driven expression/motion and a visible caption;
the caption is also the accessible name. Blinking and ambient motion are
decorative, stop under reduced motion, and stop when the element is destroyed.
The avatar is mounted lazily and replaced by a static labelled fallback if the
module fails.

The backend returns a suggested `state` and `event` with each tutor answer. The
view applies `thinking` before the request, then the returned state
(`speaking`/`corrective`), `positive` after a stored generation, and `warning` on
transport/HTTP failure. It does not invent an assessment.

---

## 7. Tutor behaviour and model path

The tutor is **course-scoped**. It resolves the question against stored concepts,
then asks a local Ollama model (or an explicitly configured OpenAI-compatible
endpoint) to answer using:

| Context | Source |
|---|---|
| Concept resolution and syllabus placement | Phase 2 concepts, Phase 5 graph |
| Coverage status and mapped-question evidence | Phase 7 `CoverageService` |
| Question locators, marks and review state | Phase 3 mappings, Phase 1 storage |
| Prerequisite path | Phase 6 `LearningPathService` |
| Practice generation | Phase 10 generation scope and service |
| Live sources | Optional Exa or keyless Wikipedia search with URLs |

The model must return a JSON object with `answer`, `key_points`, citation IDs,
`confidence` and follow-up questions. The service validates IDs against the
supplied stored/web citations, rejects invented IDs and labels low confidence. If
the model is unavailable or fails, the extractive path composes a cited summary
from the same stored records. Answers use a status (`answered`,
`insufficient_evidence`), a presentation state, sections, citations and explicit
limitations; ambiguous questions return suggested concepts rather than a guess.

Capability report (also exposed at `/api/capabilities`):

| Capability | Available | Note |
|---|---|---|
| Grounded explanations | yes | local/configured model with stored evidence; extractive fallback |
| Concept lookup | yes | selected course only |
| Prerequisite paths | yes | stored, reviewable relationships |
| Practice generation | yes | Phase 10; fast deterministic grounded template by default, with an opt-in OpenAI-compatible provider |
| Local answer checks | yes | stored practice only; exact MCQ key and lexical free-text coverage, never an examination grade |
| Open-ended conversation | yes when a model is available | no model call is made when the provider is off; the extractive fallback remains |
| Live web sources | yes unless disabled | Exa when configured, otherwise Wikipedia; links are shown |
| Evidence-based exam focus | yes when a model is available | grounded in stored coverage and recurrence; never a forecast |
| Forecasting | **no** | reports describe the available corpus; they do not predict future papers |

---

## 8. Security and privacy

* Binds to `127.0.0.1` by default. A non-loopback bind is refused unless
  `--allow-remote` is passed, and then prints an explicit warning. There are no
  accounts, sessions, cookies or authorization claims.
* Same-origin enforcement on API mutations; foreign `Origin` is rejected with
  403. No CORS headers.
* Strict CSP (`script-src 'self'`; no inline scripts), `nosniff`, `DENY` framing,
  `no-referrer`, restrictive permissions policy.
* Static serving is restricted to the asset directory with path-traversal checks,
  known content types and no directory listing. API bodies are size-limited;
  uploads go through the Phase 1 validator.
* User content is never inserted as HTML.
* Fonts, scripts, charts and illustrations are local. The dashboard API stays on
  loopback, and there is no analytics or telemetry.
* The tutor may call the public Wikipedia search/summary APIs when web search is
  enabled (the default), or Exa when `EXA_API_KEY` is set. It may call a local
  Ollama model on loopback, or an explicitly configured OpenAI-compatible endpoint.
  Web search can be disabled with `EXAM_ENGINE_TUTOR_WEB_SEARCH=off`.
* The tutor sends the selected course, the user's question and selected evidence
  snippets to the configured model. Keep private course material on the local
  model; do not configure a remote endpoint without permission.
* Errors expose a stable code and message, not tracebacks or file paths.
* The illustrations receive no data beyond the selected course and the user's
  question.

---

## 9. Verification

### 9.1 Automated tests

| Command | Result |
|---|---|
| `python -m pytest` | **628 passed** (includes later Practice answer-check, batch-expansion, wording-variation, review, NLP status and tutor-streaming regressions) |
| `cd frontend && npm test` | **17 passed**, 0 failed (Node 22 built-in runner) |
| `cd frontend && npm run test:browser` | **1 passed**, 0 failed (Playwright Core with installed Chrome) |
| `python -m ruff check src tests tools` | **All checks passed** |
| `python -m ruff format --check src tests tools` | **177 files already formatted** |
| `python -m mypy` | **Success: no issues found in 133 source files** |
| `cd frontend && npm run check:assets` | 18 first-party modules, 6 local references, Hairline 0.5.1 (MIT) |
| `python -m pip wheel . -w <dir> --no-deps` | succeeded; 155 files, **26 files under `dashboard/`** (plus `cli_dashboard.py`), including 7 views and vendored Hairline |

`tests/test_dashboard.py` covers: health/capability honesty; course scope and
document isolation; real analytics/coverage/Exam DNA/evolution reads; the
generation-scope fix; tutor grounding, refusal and unavailable capabilities;
tutor stream escape extraction and the SSE HTTP response; background job
execution; upload validation and cross-course rejection; course creation/duplicate
offerings; HTTP headers, CSP, static assets and foreign-origin rejection.

`tests/test_dashboard.py` also covers the Phase 11 mutation/adversarial routes and
cross-course rejection; `tests/test_advanced.py` covers the rephrase regression and
`tests/test_generation.py` covers the review-revision contract.

A server can no longer bind a port that is already listening, so a second launch
fails with a reported bind error instead of silently serving stale code.

The frontend tests cover: the pure tutor state machine, coverage labels/tones,
segment and bar chart data (including missing denominators and zero shares),
formatting, truncation notes, API response parsing, avatar state/caption/cleanup,
static HTML/CSP constraints, reduced-motion declarations, and the Hairline pin,
provenance and local-reference checks. The separate browser smoke test (§9.5)
runs the real server and asset code against a disposable seeded database.

No existing test was deleted, skipped or weakened.

### 9.2 Manual end-to-end verification (Chromium, demo database)

Exercised against an ephemeral demo database built from the synthetic sample
(2 courses, 7 documents) and served by `python -m exam_engine dashboard`:

1. Overview renders real counts, coverage and distribution charts; course switch
   reloads scoped data.
2. Course workspace lists documents and syllabi with provenance; upload feedback
   and validation errors render.
3. Analysis tabs load Phase 4/7/8/9 output; empty/insufficient states appear where
   the corpus cannot support a claim.
4. Learning workspace renders graph nodes/edges, prerequisite candidates and a
   real learning path; `hub` and `branches` illustrations mount.
5. Practice lists 16 generation-ready concepts, generates a stored item, shows
   its evidence and validation findings, and saves a review decision.
6. Tutor resolves "Stacks and queues", returns an evidence-backed answer and a
   speaking state; a vague question returns `insufficient_evidence` with concept
   suggestions and a corrective state; the generate action creates a pending
   question and shows the positive state.
7. Removing `tutor-avatar.js` at runtime left the tutor fully usable as text with
   its fallback; restoring the file restored the avatar.
8. Practice's advanced panel listed nine mutation and thirteen challenge
   strategies from the real Phase 11 registry, derived two stored variants from a
   stored question (`201`), and stored a grounded challenge using the `transfer`
   strategy (`201`). A deterministic repeat of an earlier challenge returned
   `201` with `persisted: 0` and an exact-duplicate record instead of padding the
   batch; both outcomes were rendered as batch results, and the stored items
   appeared in the review queue below. The mutation text no longer repeated the
   source's phrasing. Console and network panels showed no errors.
9. Generating another question updated the stored review queue and the mutation
   source list without a reload. Saving `approved` and then `rejected` on the same
   batch card updated the badge both times and reported `Saved`; the second save
   previously failed with a revision conflict.
10. Theme toggle switched light/dark tokens; narrow viewport removed horizontal
    overflow and collapsed the sidebar; console showed no errors.

This initial pass was manual verification rather than an automated browser
suite. A narrow smoke test now supplements it (§9.5).

### 9.3 Practice hardening after the initial dashboard pass

A later verification pass exercised every Practice control in Chromium and
targeted the generation and review paths that were still confusing:

* **Answer checks became available.** A new grade route compares MCQ choices with
  the stored key and gives written answers a local lexical coverage check against
  the stored reference and expected points. Feedback states its limits and is not
  presented as a mark.
* **Batch requests expand across concepts.** A multi-question request continues
  through other in-scope concepts, keeps the chosen concepts first, and records
  spare targets so a rejected or duplicate attempt can still produce the requested
  number of distinct prompts.
* **Question wording and answer guidance were tightened.** Evidence now prefers
  sentences that name the concept exactly, label-only sources produce a
  `rubric_only` expected-answer outline rather than a fake definition, and
  difficulty/Bloom warnings distinguish explicit from defaulted requests.
* **Review states are explained.** `pending_review` is shown as `Draft`, cards have
  a quick Approve action, and explicit Approve/Reject/Save-status controls keep the
  stored revision current.
* **Phase labels and stale panels were removed.** Practice, Analysis, Course,
  Overview and Tutor no longer show internal phase numbers; generation refreshes
  the queue and advanced panel without a reload.
* **A client parse failure was fixed.** `practice.js` declared `loadGenerated`
  twice, which caused `SyntaxError: Unexpected end of input`; the duplicate was
  removed and every browser module now parses.

Verification for this pass: 66 targeted Python tests (dashboard and generation),
Ruff lint, parsing of every browser module, `npm run check:assets` over 18
first-party modules, 17/17 frontend tests, and an 18/18-route Chromium harness with
zero timeouts, console errors or horizontal-overflow findings. The live Practice
view loaded the stored queue, displayed Draft/Approve, and completed a real
`Check your answer` request without a console or network error. These were local
verification runs; the committed browser coverage is the focused smoke test in
§9.5, not a full E2E suite.

### 9.4 Tutor streaming verification

A live local-model check exercised the new route with Ollama `qwen3:8b` and the
stored JEE Advanced maths corpus. Stage events arrived immediately
(`stored_evidence`, `web_search`, `model`), heartbeats continued while the model
worked, and decoded answer deltas began at about 62 seconds in a direct client
check; the final validated response arrived at about 120 seconds. In Chromium,
the pending message showed the current stage, a one-second elapsed timer and a
live plain-text preview at 65 seconds, then was replaced by the normal structured
message with 3 sections and 3 citations. The browser console had no errors. These
timings are observations, not performance guarantees.

### 9.5 Automated browser smoke test

`cd frontend && npm run test:browser` exercises the real Practice UI and backend
with Playwright Core and an installed Chrome or Edge executable; it does not
download a browser or run under normal `npm test`. The test seeds a temporary
SQLite database from the synthetic sample via `tools/seed_dashboard_smoke.py`,
starts the dashboard on an available loopback port, and removes both afterwards.
It never opens the operator's `exam_engine.db`.

The flow verifies that a pending short-answer question renders, submits a weak
answer and receives a local lexical-coverage response with the "not a grade"
label, approves the Draft through the UI, confirms the review response and then
reloads the page to verify the approved state was persisted. Browser page errors
and console errors fail the test. `BROWSER_EXECUTABLE` overrides browser
detection. Coverage is intentionally narrow: this is not a full navigation or
accessibility suite.

---

## 10. Performance observations

Environment: Windows, Python 3.13, local SQLite, single client, loopback HTTP,
no compression. Demo corpus: 2 courses / 7 documents. Times are wall-clock from
`urllib`; GET samples are 10–30 requests after warm-up.

| Endpoint | p50 | p95 | Response |
|---|---:|---:|---:|
| `GET /api/health` | 1.3 ms | 1.5 ms | 253 B |
| `GET /api/courses` | 40.8 ms | 52.2 ms | 2.2 KB |
| `GET /api/courses/{id}` | 49.3 ms | 57.8 ms | 10.2 KB |
| `GET …/coverage` | 26.1 ms | 35.1 ms | 52.2 KB |
| `GET …/exam-dna` | 33.4 ms | 34.9 ms | 51.3 KB |
| `GET …/generation/options` | 33.0 ms | 39.8 ms | 9.0 KB |
| `GET …/graph?limit=400` | 23.4 ms | 25.5 ms | 74.5 KB |
| `GET …/tutor/context` | 28.0 ms | 29.1 ms | 3.1 KB |
| `POST …/tutor/ask` | 29.9 ms (5 runs, extractive fallback) | — | 2.9 KB |

* Static client: **449 KiB** across 17 requests, of which 217 KiB is the vendored
  Hairline module loaded only on illustration pages. Median asset response 1.7 ms;
  slowest first read 35.8 ms.
* Background `understanding` analysis over the demo corpus: **14.3 s**, reported
  `succeeded`, with the HTTP server responsive to polls during the run.
* Startup performs one migration check and then serves; health answered within
  2 s of process start.
* No memory profile was captured; the server is a single process with a two-worker
  job pool and per-request SQLite connections that are closed in `finally`.

These numbers describe the extractive fallback on a synthetic local corpus and
are not a production throughput claim. A real `qwen3:8b` answer took 116.3 s on
the development machine; the UI timeout is 240 s. Local-model latency is
hardware-dependent and is not represented in the table. There is no HTTP
compression, caching layer or CDN.

---

## 11. Production build and packaging

There is no frontend compilation: `index.html` loads ES modules directly. The
"production build" for this project is the Python distribution plus asset
verification:

* `npm run check:assets` validates every local reference, the module graph and
  the Hairline pin/provenance.
* `python -m pip wheel . --no-deps` produces a universal wheel containing all
  static files (verified after the final code changes: 26 entries under
  `dashboard/` plus `cli_dashboard.py`, including all 7 `static/views/*.js`, the
  three vendored Hairline files and the licence).
* The server refuses to start on a busy port and reports the bind error; the CLI
  closes the API/job pool on exit.

---

## 12. Files added or modified

Added:

* `src/exam_engine/dashboard/{__init__,api,errors,jobs,server,tutor,tutor_ai,websearch}.py`
* `src/exam_engine/dashboard/static/**` (shell, modules, styles, vendor)
* `src/exam_engine/cli_dashboard.py`
* `tests/test_dashboard.py`
* `frontend/{package.json,package-lock.json,README.md}`
* `frontend/scripts/{sync-hairline,check-assets}.mjs`
* `frontend/tests/{ui-state,api-client,tutor-avatar,static-assets}.test.mjs`
* `frontend/browser-tests/practice-smoke.mjs`
* `tools/seed_dashboard_smoke.py`
* this report

Modified:

* `src/exam_engine/cli.py` — registers the `dashboard` command.
* `pyproject.toml` — package data for dashboard assets.
* `src/exam_engine/advanced/mutation.py` — rephrase avoids repeating the source's
  own "in your own words" framing; `tests/test_advanced.py` keeps the regression.
* `src/exam_engine/generation/service.py` — review returns the stored revision;
  `tests/test_generation.py` keeps the regression.

No service module from Phases 1–11 was rewritten. The only service-layer changes
are the Phase 12 adapter, the generation-scope read described in §2.2, and the
verification fixes in §2.3. The later Practice hardening added
`generation/grading.py` and touched the generation service, provider, templates,
models, dashboard API/tutor, Practice and workspace views, styles and tests.

---

## 13. Known limitations

1. **No authentication or authorization.** The dashboard is a local single-user
   tool. Do not expose it; `--allow-remote` prints that warning. There is no
   per-user separation because there are no users.
2. **Answer checks are local and lexical, not an examination grade.** MCQ
   choices are compared with the stored key; free text is scored by term and
   expected-point coverage. Regular/listed irregular plurals and a small fixed
   set of common synonyms are normalized, but other paraphrases can still be
   under-credited and keyword-heavy prose over-credited. Attempts are not
   stored, and only stored generated questions can be checked.
3. **No forecasting or autonomous approval.** Open-ended conversation is
   available when a model is configured, but reports describe only the available
   corpus and never predict a future paper.
4. **Model answers can be wrong or slow.** The tutor prefers a local Ollama
   model; its answers may use general subject knowledge when stored and web
   sources are thin, and a local answer can take up to the configured timeout
   (240 seconds by default). The extractive fallback is bounded by the analysed
   corpus. Concept resolution inherits Phase 2/5 identity quality.
5. **Browser coverage is a narrow smoke test, not a full E2E suite.** It covers
   the Practice answer-check and Draft approval flows against a seeded database.
   Accessibility was checked structurally and by keyboard/theme review, not by a
   formal WCAG audit or screen-reader matrix.
6. **Jobs are in-process and ephemeral.** A restart loses job history (not the
   data written). There is no durable queue, progress percentage or cancellation.
7. **Single-process local server.** No TLS, compression, caching, rate limiting or
   multi-user concurrency design. SQLite remains the only store.
8. **Illustrations are decorative.** Three Hairline figures are used; none
   represents application data.
9. **No third-party avatar code or assets.** The avatar is original; the AGPL
   Studio is not integrated. This assessment is not legal advice.
10. **No real-world educational validation.** All measurements and workflows used
    the labelled synthetic sample. Accuracy against real syllabi, papers or
    students is Phase 13 work.
11. **Packaging is Python-only.** The frontend has no bundler/minifier and ships
    readable modules; this is intentional but means a larger transfer than a
    compiled bundle.
12. **Derived questions can themselves be mutated.** The Practice source list
    includes stored mutation variants, so repeated derivation can accumulate
    framing even though the rephrase duplication bug is fixed. Every result is
    still pending human review; the UI does not label a source's derivation
    depth.

---

## 14. Phase 13 boundary

Phase 12 does not ingest private syllabi or personal question papers. Phase 13
should:

1. Import permissioned material through the existing manifest/provenance path
   with `dataset_type=permissioned`, keeping it out of version control.
2. Validate extraction, mapping, coverage and generation against that material,
   and record error rates rather than asserting accuracy.
3. Re-run the end-to-end workflows and performance measurements on a realistic
   corpus; revisit pagination, caching and job progress if latency grows.
4. Decide whether authentication is required for the intended deployment before
   any remote hosting; the current dashboard must not be presented as multi-user.
5. Expand the focused browser smoke test only where it can reuse the existing
   static contracts without duplicating the Node suite.

---

## 15. Reproduction

```bash
python -m pip install -e ".[dev]"
python -m exam_engine init-db
python -m exam_engine import datasets/sample/manifest.json
python -m exam_engine nlp analyze
python -m exam_engine understanding analyze
python -m exam_engine graph build
python -m exam_engine prerequisites discover
python -m exam_engine dashboard --port 8765

# checks
python -m pytest
python -m ruff check src tests
cd frontend && npm ci && npm run check:assets && npm test
cd frontend && npm run test:browser                        # Chrome/Edge required
```

The dashboard prints its URL and database path, serves only loopback by default,
and stops on Ctrl+C.
