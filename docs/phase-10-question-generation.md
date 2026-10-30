# Phase 10 — Automatic Question Generation

Phase 10 produces **proposed practice questions** for concepts a course already
contains. Every question is traceable to a syllabus version, a unit, a stored
concept and a quoted source passage from that course's own study material.

It is deliberately **grounded and review-first**:

* a request never guesses its target — you name concepts or a unit, and an
  ambiguous or foreign syllabus is refused rather than resolved by guesswork;
* question papers and question banks are **never** used as sources, so generated
  material cannot leak the wording of a historical paper into practice content;
* the deterministic provider composes task statements from templates and real
  evidence, so it cannot invent a definition, number or formula;
* automated validation proves only that checkable properties held — passing it is
  never a claim of educational correctness;
* every question starts in `pending_review` and stays practice material.

Phase 10 does **not** implement advanced assessment intelligence (Phase 11),
dashboard integration (Phase 12) or personal data integration (Phase 13), and it
does not predict real examination content.

## Status and architecture

Phase 10 is implemented as a new `generation` package plus one migration. It reads
Phases 1–9 and writes only to its own two tables.

| Module | Responsibility |
|---|---|
| [`src/exam_engine/models/generation.py`](../src/exam_engine/models/generation.py) | Validated requests, queries, source passages, options, generated questions, batches, counts, rejections and duplicate findings |
| [`src/exam_engine/generation/templates.py`](../src/exam_engine/generation/templates.py) | The deterministic `(format, Bloom level)` task-statement recipes, default marks, default Bloom levels and capability reporting |
| [`src/exam_engine/generation/providers.py`](../src/exam_engine/generation/providers.py) | The `GenerationProvider` protocol, the offline template provider and an opt-in OpenAI-compatible provider |
| [`src/exam_engine/generation/validation.py`](../src/exam_engine/generation/validation.py) | Twenty-five named checks over an assembled question, grouped structure / alignment / answerability / MCQ / quality / cognitive |
| [`src/exam_engine/generation/dedupe.py`](../src/exam_engine/generation/dedupe.py) | Duplicate and variant detection against existing generated and historical questions, reusing Phase 3 similarity |
| [`src/exam_engine/generation/service.py`](../src/exam_engine/generation/service.py) | Scope resolution, target/evidence selection, provider calls, assembly, validation, dedupe, persistence and review |
| [`src/exam_engine/storage/generation_repository.py`](../src/exam_engine/storage/generation_repository.py) | Batch and question persistence, listing, revision-safe saves and report export |
| [`src/exam_engine/storage/generation_mappers.py`](../src/exam_engine/storage/generation_mappers.py) | Row ↔ model mapping, including JSON columns and option/answer keys |
| [`src/exam_engine/storage/migrations/0007_generated_questions.sql`](../src/exam_engine/storage/migrations/0007_generated_questions.sql) | `generation_batches` and `generated_questions` with course/syllabus foreign keys and lookup indexes |
| [`src/exam_engine/cli_generation.py`](../src/exam_engine/cli_generation.py) | `generation generate/formats/list/show/batches/failures/review/review-batch/export/review-summary` |

Integration points:

* **Phase 1** courses, syllabi, units, documents, pages, questions and marks define
  scope, source text and the reference-style input.
* **Phase 2** concepts, aliases and syllabus-topic rows supply the target registry;
  `analysis_tokens` from the keyword layer shortlists dedupe candidates.
* **Phase 3** supplies the question type, Bloom and difficulty estimators used for
  the *estimated* side of each question, the semantic index, and
  `find_similar_questions` for duplicate decisions.
* **Phase 4/7/8/9** are not required to generate, but their reports describe the
  same course data and can be read alongside a batch.
* **Phase 5/6** graph concepts and prerequisite edges are used to resolve concept
  identity and to attach related labels.

Nothing in Phases 1–9 is modified: historical questions are read, never written.

## Generation pipeline

Each `generate` call runs the same six steps, and every step records what it did in
the batch report.

### 1. Scope resolution

The course must exist. `syllabus_id` must belong to that course; if the course has
several syllabus versions and none is named, the request fails instead of picking
one. Every target, passage and dedupe comparison stays inside that syllabus.

### 2. Target resolution

Targets come from `--concept` (id, label or alias) or `--unit`. Concepts must be
attached to a unit of the selected syllabus; concepts that cannot be placed are
counted and reported (`generation_unplaced_concepts`) rather than silently
dropped or substituted. The resolved targets, and any defaults applied, are echoed
into the batch report.

### 3. Evidence selection

Candidate passages are sentences from the course's **syllabus, notes and other
study material** — never question papers or question banks. Documents you name
with `--document` must be grounding material; naming only examination material
fails with `no_grounding_documents`.

A passage is a sentence that names the concept, focused to the matching clause and
capped at `max_evidence_chars`. Sentences that name the concept itself outrank
sentences that only use one of its words, and those words — including a simple
singular — are a documented fallback: a syllabus topic stored as "Stacks and
queues" still finds the notes sentence "A stack is a linear structure …". Table
rows and layout fragments are skipped because they mention a concept without
describing it. Passages keep syllabus-first, then shortest-first order and are cut
at `max_evidence_passages`. When document text is thin, the service falls back to
a syllabus-topic passage so a question is still grounded in something a reader can
check, and marks the batch as thin-evidence.

A comparison additionally needs a passage for the second concept. Same-unit
siblings are tried first (they are also the options a reader sees), and a graph
relation is used only as a fallback; both passages are cited on the question.

### 4. Provider call

The chosen provider renders one draft per target. `--provider auto` (the default)
uses a configured language model when one is available and otherwise falls back to
the deterministic template provider; `--provider template` forces the offline path
and `--provider openai` requires a configured model.

### 5. Assembly

The draft is combined with the resolved scope, unit and concept labels, a learning
objective derived from the Bloom level, the requested difficulty/Bloom, the
estimated type/Bloom/difficulty from Phase 3 (with confidence and method), marks,
options, answer key, explanation, solution steps, distractor notes and every
source reference. Identifiers are content-derived hashes, so the same request over
the same data produces the same `batch_id` and `question_id`.

### 6. Validation and duplicate decisions

Validation runs independently of the provider, then dedupe compares the candidate
with existing generated and historical questions. Accepted questions are persisted
with their findings; rejected ones are kept in the batch report with a machine
code and a human-readable reason.

## Supported formats and Bloom levels

`generation formats` prints this matrix from the same data the service uses, so the
reported capability cannot drift from behaviour.

| Format | Deterministic Bloom levels | Notes |
|---|---|---|
| `multiple_choice` | remember, understand | Needs a passage that describes the concept beyond its own name; only the single most specific name present is masked, and a masked section heading is dropped, so the stem keeps one blank |
| `short_answer` | remember, understand | Answer key is the quoted reference passage |
| `long_answer` | understand, apply, analyze, create, evaluate | Open-ended above `understand`; no fabricated key |
| `explanation` | remember, understand, apply, analyze, evaluate, create | Answer key quotes the source passage |
| `comparison` | understand, analyze, evaluate | Needs a second concept from the same unit *and* a grounding passage for it; a same-unit sibling is tried first, a graph relation is the fallback |
| `application` | apply, analyze | |
| `case_based` | apply, analyze, evaluate | Needs a longer case passage (`CASE_MIN_EXCERPT_CHARS`) |
| `analysis` | analyze, evaluate | |
| `design` | create, evaluate | |
| `evaluation` | analyze, evaluate | |
| `numerical` | — | Model provider only, at apply/analyze |

Requesting an unsupported pair fails with a capability note that names the format,
the level and the reason — it is never silently approximated at another level.

## Difficulty

Difficulty is a **requested label**, not a calibrated difficulty. The estimator's
output is stored beside it as `estimated_difficulty` with `difficulty_confidence`
and `difficulty_method`, and validation reports `difficulty_mismatch` when the two
disagree, or `difficulty_not_estimated` when no Phase 3 row exists. `moderate` is
the default when the request states nothing. Marks default per format
(`multiple_choice` 1, `short_answer` 3, `explanation` 4, `long_answer` 8,
`comparison`/`application`/`analysis` 6, `case_based`/`evaluation` 8, `design` 10)
and are recorded as conventions, not measurements.

## Answer keys and explanations

Where the template's answer mode is a quoted excerpt, the key is that excerpt with
its document title; where two concepts are compared, both excerpts are the
reference material. Open-ended tasks (design, evaluation above the lowest levels)
have **no** answer key, because a template cannot derive one without inventing a
model answer. `--no-answer-key` and `--no-explanation` suppress both.
`EXAM_ENGINE_Q10_REQUIRE_ANSWER_EVIDENCE` (default on) makes a key that quotes
material absent from the cited evidence a hard rejection (`answer_key_unverified`);
with it off, the same finding is a review warning instead. A key that is not a
quotation at all cannot be checked automatically, so it is always a review warning,
never an error — the question is kept for a human, not silently dropped.

## Validation

Validation reports every finding with a stable code, the rule that produced it and
a severity. It runs over the assembled question only — it never queries hidden
state — and it never deletes or rewrites content.

| Group | Checks |
|---|---|
| Structure | `structure.required_fields`, `structure.format_match`, `structure.marks`, `structure.rendering` |
| Alignment | `alignment.concept_scope`, `alignment.unit_scope`, `alignment.course_isolation`, `alignment.concept_named`, `alignment.source_evidence` |
| Answerability | `answerability.answer_key`, `answerability.answer_key_grounded`, `answerability.solution_steps`, `answerability.context` |
| Multiple choice | `mcq.option_count`, `mcq.options_distinct`, `mcq.single_correct`, `mcq.answer_key`, `mcq.answer_leakage`, `mcq.stem_unambiguous` |
| Quality | `quality.single_task`, `quality.repetition`, `quality.vague_wording`, `quality.assumptions` |
| Cognitive | `cognitive.bloom_alignment`, `cognitive.difficulty_alignment` |

Representative codes: `missing_question_text`, `question_too_short`,
`unrendered_placeholder`, `missing_terminal_punctuation`, `format_mismatch`,
`concept_out_of_scope`, `unit_out_of_scope`, `foreign_material_leak`,
`missing_source_evidence`, `thin_evidence`, `missing_answer_key`,
`answer_key_unverified`, `answer_key_not_derived`, `missing_solution_steps`,
`mcq_option_count`, `mcq_duplicate_options`, `mcq_correct_count`,
`mcq_answer_key_mismatch`, `mcq_meta_option`, `mcq_answer_leakage`,
`mcq_ambiguous_stem`, `mcq_missing_distractor_notes`, `multiple_questions`,
`repeated_sentence`, `vague_wording`, `unbound_reference`, `invalid_marks`,
`unusual_marks`, `marks_task_mismatch`, `bloom_mismatch`, `bloom_not_estimated`,
`bloom_low_confidence`, `difficulty_mismatch`, `difficulty_not_estimated`,
`unsupported_numeric_task`, `case_context_missing`.

### Result statuses

| Status | Meaning |
|---|---|
| `validated` | Every error and warning check held. Not a correctness proof. |
| `needs_review` | Only judgement-dependent findings, or a low-confidence estimator. Stored, with the findings attached. |
| `rejected` | A hard error (empty text, wrong format, out-of-scope concept or unit, broken MCQ, ungrounded key). Reported, not stored. |
| `validation_incomplete` | Validation could not run over the draft; the question is kept for review, never promoted. |

## Duplicate detection

Phase 3 already defines what a duplicate is, so `dedupe.py` reuses
`find_similar_questions` and its `exact` / `near` / `semantic` /
`concept_related` taxonomy instead of introducing a second similarity scale.

* Candidates are shortlisted by shared `analysis_tokens` (`max_candidates` 25), so a
  large historical corpus does not make each request quadratic.
* Comparisons span the whole batch (so two targets that produce the same wording
  are caught), earlier stored generated questions, and historical questions from
  the same course and syllabus.
* Thresholds are Phase 3's `duplicate_near_threshold` (0.78) and
  `duplicate_semantic_threshold` (0.68), configurable per run.
* `exact`, `near` and `semantic` matches are treated as duplicates and rejected with
  a `duplicate_<kind>` code naming the colliding question. A `concept_related` match
  is kept as a separate variant and recorded in the batch report, which
  `generation failures` displays.
* A candidate that shares wording with a question on a *different* concept is
  downgraded to `concept_related` unless it is an exact copy, because it assesses
  other material.
* Historical questions are only read. A generated question is never merged into,
  or stored as, a historical one.

Two consequences follow, and both are intended: a request for more questions than
the available targets and templates can support produces a `partial` batch with
explained rejections rather than padded duplicates, and re-running an identical
request reuses the same ids instead of adding rows.

## Persistence and review

Migration `0007_generated_questions.sql` adds:

* `generation_batches` — one row per run: status, provider, model, the request, the
  resolved parameters, the counts and the full report (rejections, duplicates,
  warnings, limitations).
* `generated_questions` — one row per accepted question: scope, concept ids and
  labels, objective, format, requested vs estimated difficulty and Bloom with
  confidence and method, marks, text, options, keys, explanation, solution steps,
  distractor notes, source references, generation metadata, validation result,
  review status and history, revision, warnings and limitations.

Batch status describes **completeness only**: `ok` means every requested question
was produced, `partial` means fewer were produced (rejections, duplicates, provider
failures or incomplete validation), and `failed` means none were. Needing review
does not make a batch partial; items in that state produce a
`generation_review_required` note and remain `pending_review` until a person decides.

Review flow:

```bash
python -m exam_engine generation review --question gq_… --status approved --note "checked against Unit 2 notes"
python -m exam_engine generation review-batch --batch gb_… --status rejected --note "wrong unit emphasis"
python -m exam_engine generation review-summary --course demo-cs201-2024-v1 --syllabus syl_…
```

`--expected-revision` makes a decision fail with `revision_conflict` if the row moved
on since it was read, so two reviewers cannot silently overwrite each other. A
regeneration that would replace an approved question is refused unless
`--allow-overwrite-approved` is passed; the earlier decision stays in
`review_history`.

`generation list`, `show`, `batches` and `export` read stored results only, and
`export` writes a self-contained JSON report for offline review.

## Determinism

Everything except the language-model provider is a pure function of the stored
corpus and the request:

* identifiers are hashes of the course, syllabus, request, provider and concept
  scope, so the same request produces the same `batch_id` and question ids;
* option order is shuffled by `--seed` (default 0), so an item is reproducible but
  a different seed reorders the same options;
* documents are indexed syllabus-first, then notes, then other material, by
  document id within each kind, and passages are ordered by exact-name match before
  word-level match, then by kind, excerpt length and text, so ties do not drift;
* re-running the same request against unchanged rows reuses existing questions
  instead of duplicating them.

The batch `created_at`/`updated_at` timestamps and the language-model provider's
output are the only non-deterministic parts; a model response is still validated
and deduplicated before it is stored.

## CLI

```bash
# Capability matrix: which formats and Bloom levels the configured provider can produce
python -m exam_engine generation formats
python -m exam_engine generation formats --provider openai --json

# Generate for one concept, a list of concepts, or a whole syllabus unit
python -m exam_engine generation generate --course demo-cs201-2024-v1 \
    --concept "Stacks and queues" --count 3 --format explanation --bloom understand
python -m exam_engine generation generate --course demo-cs201-2024-v1 \
    --unit unit_… --count 5 --format multiple_choice --difficulty moderate --seed 7
python -m exam_engine generation generate --course demo-cs201-2024-v1 \
    --concept "Binary Search" --format comparison --bloom analyze --marks 6 \
    --reference q_… --document doc_… --no-persist
```

`generate` options: `--course`, `--syllabus`, `--concept` (repeatable), `--unit`,
`--count`, `--format`, `--difficulty`, `--bloom`, `--marks`, `--document`
(repeatable), `--reference` (repeatable), `--provider`, `--seed`, `--language`,
`--answer-key/--no-answer-key`, `--explanation/--no-explanation`,
`--allow-overwrite-approved`, `--persist/--no-persist`, `--json`.

```bash
# Inspect results
python -m exam_engine generation list --course demo-cs201-2024-v1 --review pending_review
python -m exam_engine generation show --question gq_…          # one question, fully expanded
python -m exam_engine generation show --batch gb_…             # the batch report
python -m exam_engine generation batches --course demo-cs201-2024-v1
python -m exam_engine generation failures --batch gb_…         # rejections, duplicates, review queue
python -m exam_engine generation export --batch gb_… --out reports/gb.json
```

`--json` is available on every command, and malformed requests exit non-zero with
a stable code (`missing_target`, `invalid_request`, `unknown_course`,
`no_grounding_documents`, `unknown_reference_question`, …).

Reference questions only inform length and marks conventions; their wording is
never copied. The style summary is stored in the batch report with the note that
says so.

## Configuration

`GenerationSettings` reads the environment; nothing needs a paid API.

| Variable | Default | Meaning |
|---|---|---|
| `EXAM_ENGINE_Q10_MAX_QUESTIONS` | 20 | Hard cap on `--count` |
| `EXAM_ENGINE_Q10_MIN_MCQ_OPTIONS` / `_MAX_MCQ_OPTIONS` | 3 / 6 | Option-count bounds |
| `EXAM_ENGINE_Q10_MAX_EVIDENCE` | 4 | Passages attached per question |
| `EXAM_ENGINE_Q10_MAX_EVIDENCE_CHARS` | 240 | Excerpt length cap |
| `EXAM_ENGINE_Q10_MAX_RELATED` | 4 | Related concept labels considered |
| `EXAM_ENGINE_Q10_DUPLICATE_NEAR` | 0.78 | Near-duplicate threshold |
| `EXAM_ENGINE_Q10_DUPLICATE_SEMANTIC` | 0.68 | Semantic-duplicate threshold |
| `EXAM_ENGINE_Q10_REQUIRE_ANSWER_EVIDENCE` | true | Reject a key that quotes evidence absent from the cited passages |
| `EXAM_ENGINE_Q10_PROVIDER_TIMEOUT` | 20.0 | Per-request timeout, seconds |
| `EXAM_ENGINE_Q10_PROVIDER_RETRIES` | 2 | Retries after a retryable failure |
| `EXAM_ENGINE_Q10_RETRY_BACKOFF` | 0.5 | Base backoff, seconds |
| `EXAM_ENGINE_Q10_LLM_BASE_URL` | OpenAI-compatible default | Endpoint for the model provider |
| `EXAM_ENGINE_Q10_LLM_MODEL` | — | Model name; without it the model provider is unavailable |
| `EXAM_ENGINE_Q10_LLM_API_KEY` (or `OPENAI_API_KEY`) | — | Credential, read from the environment only |

The API key is never written to configuration, a batch report, a log line or a
question row. `describe()` records only the provider kind, base URL and model.

## Testing

```bash
python -m pytest tests/test_generation.py tests/test_generation_cli.py   # 53 passed
python -m pytest                                                       # 523 passed
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy                                                          # 103 source files
```

The Phase 10 tests cover: request validation, scope refusal for unknown courses and
ambiguous or foreign syllabi, target resolution (including unplaced concepts),
evidence exclusion of question papers and banks, MCQ grounding refusals, provider
capability reporting, unsupported format/Bloom pairs, template determinism,
option seeding, marks and Bloom defaults, every validation group with its codes,
validation statuses, thin-evidence handling, dedupe against generated and
historical questions with review-only kinds, partial batches with explained
rejections, persistence round-trips, revision-safe review, approved-overwrite
protection, batch export, review summaries, and CLI JSON/table/error paths.

They prove software behaviour on synthetic fixtures. No labelled benchmark of
question quality, difficulty calibration or Bloom correctness is bundled, so these
tests cannot show that a generated question is a *good* question.

## Performance considerations

* Evidence indexing stops at 50,000 sentences and reports
  `generation_index_truncated`; it never silently analyses a partial corpus.
* Dedupe shortlists candidates by shared analysis tokens before scoring, so cost
  grows with the shortlist, not the corpus.
* One request generates at most `max_questions` items and calls the provider once
  per target.
* The template provider is pure Python: no network, no model download, no GPU.

## Evaluation readiness

Ready for:

* expert review of generated practice items, with provenance, evidence and
  validation findings attached to each one;
* comparison of template output across formats and Bloom levels for coverage of a
  syllabus;
* regression tests over the deterministic path, because ids and text are stable.

Not ready for:

* automatic grading or unsupervised publication — every item starts in
  `pending_review`;
* difficulty calibration, which would need response data;
* claims that generated items match real examination style, which would need a
  labelled benchmark of examiner-approved items.

## Known limitations

* The deterministic provider has **one** construction per `(format, Bloom level)`
  pair. Asking for several questions on a single concept therefore produces the
  same wording; duplicates are detected and the batch is reported as `partial`
  instead of padded. Breadth comes from more targets, formats or levels, not from
  repetition.
* Templates compose task statements around quoted evidence. They do not paraphrase,
  define terms or supply model answers, so an open-ended item's answer key is often
  absent by design.
* `numerical` items are never produced deterministically: a template cannot invent a
  solvable problem statement or check its arithmetic.
* MCQ distractors are other syllabus concepts from the same unit, which makes a
  well-constructed item but cannot guarantee a pedagogically ideal distractor set.
* A multiple-choice answer is the concept *label*. When the label is a phrase
  ("Stacks and queues") and the evidence describes one part of it ("A stack is
  first-in-first-out"), the item is answerable but less precise than a hand-written
  one, and the mismatch is left for the reviewer rather than hidden.
* Segmentation sometimes glues a section heading to its sentence ("2. Queue A queue
  is …"). Such a heading is dropped when masking consumed it, but a heading that is
  not the answer can still appear in a stem.
* The language-model provider is optional and untested against a live endpoint in
  this repository's test suite; its output is validated and deduplicated, but
  validation still cannot prove correctness.
* Content generation is English-only. A request for another language fails with
  `unsupported_language` rather than being silently translated or ignored.
* Bloom and difficulty estimates are Phase 3 heuristics. `needs_review` is the
  honest outcome for low-confidence labels.
* Dedupe reuses Phase 3 thresholds; a `semantic` match is a review trigger, not
  proof that two questions test the same thing.
* Only the selected syllabus version is used. A concept in another version of the
  same course is out of scope rather than merged.
* No labelled generation benchmark is bundled, and Phase 13 personal material
  remains out of scope.

## Out of scope

Automatic answer grading, student response modelling, adaptive practice
sequencing, exam-paper assembly, misconception mining (Phase 11), dashboard
integration (Phase 12) and personal data integration (Phase 13). Phase 10 produces
reviewable practice questions; it does not write examinations.
