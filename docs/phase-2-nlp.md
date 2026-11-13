# Phase 2 — NLP analysis architecture

This document describes how Phase 1 rows (documents, sections, questions, syllabi)
become derived, provenance-linked NLP records. Every row described here is
recomputable: deleting the Phase 2 tables and rerunning `exam-engine nlp analyze`
must reproduce them from the Phase 1 tables alone.

## 1. Goals and non-goals

**Goals**

1. Turn stored text into a stable, queryable representation for later phases:
   normalised text, keyphrases, entity candidates, concepts, vectors and topics.
2. Never overwrite evidence. Raw page/question text stays exactly as ingested;
   analysis output lives in its own tables with the config hash, model id and
   source SHA-256 that produced it.
3. Keep *verified* syllabus concepts strictly separate from *inferred* candidates;
   everything inferred is marked `verified = false` and needs review.
4. Degrade explicitly. A missing optional model downgrades to an offline
   provider and records a warning rather than failing a course.
5. Stay offline by default: a fresh checkout works with no model downloads.
6. Refuse to invent structure: too-small or degenerate corpora produce an
   `insufficient_data` result, not fake topics or empty clusters.

**Non-goals (later phases)**

Exam DNA, difficulty/learning-objective classification, question generation,
knowledge graphs, misconception mining, answer validation and personalisation.
Phase 2 organises text; it does not decide what a question *means* or whether an
answer is correct.

## 2. Data flow

```
courses ─┬─ documents.pages ──┬─ document text ───────┐
         │                    └─ sections ───────────┤
         ├─ questions ───────────── question text ───┤──► processed_texts
         └─ syllabus units/topics ── syllabus text ──┘         │
                                                              ├──► keyphrases
                                                              ├──► entities
                                                              ├──► concept_candidates
                                                              ├──► embedding_cache + embeddings
                                                              └──► nlp_topics (+ terms, members)
                                                                       │
                                                              nlp_runs (report)
```

`NlpService.analyze_course` runs the stages in one transaction per course:

1. **Load** the course bundle (`documents`, `document_texts`, `sections`,
   `questions`, `units`). Question-bank documents that have no page text get
   their text from the stored questions, so `.csv`/`.json` banks are analysable
   without fabricating pages.
2. **Preprocess** documents first, then sections split from the normalised
   document text, then questions and syllabus units. Each owner becomes a
   `ProcessedText`, reusing the cached row when `raw_sha256` and `config_id`
   still match (unless `--force`).
3. **Keyphrases** in one corpus-relative pass over documents, attributed to the
   document where each term scores highest and aggregated to course level.
4. **Entities** over every processed text (document, section, question, syllabus
   unit) with the configured extractor.
5. **Embeddings** for every owner, using the text-level vector cache.
6. **Topics** over document texts (or section texts when a course has fewer than
   three documents).
7. **Concepts**: verified syllabus topics first, then inferred candidates.
8. **Replace** prior derived rows for the course, so a rerun never mixes old and
   new output. Cached `ProcessedText` rows are kept; rows for owners that no
   longer exist are pruned.
9. **Summarise** into an `nlp_runs` row (`status`, `totals`, `model_ids`,
   warnings, errors, duration).

## 3. Ownership model

Analysis rows are keyed by `(owner_type, owner_id)` where owner is one of
`course`, `document`, `section`, `question` or `syllabus`. This keeps
course-level statistics separate from document-level ones, lets a section carry
its own processed text, entities and vector, and makes "show me what this
document contributes" a single indexed lookup. Keyphrases are attributed at
document and course level; embeddings and entities cover every owner.
`course_id` is stored on every row, and cross-course reads are filtered by it.

Each `ProcessedText` records:

| Field | Meaning |
|---|---|
| `raw_sha256` | SHA-256 of the exact text handed to the preprocessor |
| `config_id` | hash of `PreprocessConfig` (unicode form, notation, language, min sentence chars) |
| `language` | detected language hint (`en` only is fully supported) |
| `text`, `sentences`, `token_count` | the normalised representation |
| `uncertainties` | `empty_text`, `language_unsupported`, … |

Reuse requires both the raw hash and the config hash to match; changing either
recomputes the row. Raw page text is never modified.

## 4. Preprocessing

`nlp/text.py` provides the deterministic text primitives:

* `normalize_for_analysis` — NFKC, line-ending and whitespace cleanup, optional
  preservation of notation such as `O(n)`, `a[i]`, `→`, operators and digit
  ranges (the default `PreprocessConfig(preserve_notation=True)` keeps them,
  because exam text is full of them);
* `segment_sentences` — sentence splitter that does not break on `e.g.`, `i.e.`,
  `Fig.`, abbreviations, decimals or initials, and drops fragments shorter than
  `min_sentence_chars`;
* `tokenize` — word/punctuation tokens with offsets;
* `detect_language` — cheap stopword- and script-based hint. English is
  confirmed by stop-word density; non-Latin scripts are reported with their
  script code and flagged `language_unsupported`; Latin-script text with too
  little evidence is reported as unknown.

`split_section_chunks` in `nlp/preprocess.py` makes coarse sections from
blank-line blocks: a short, unpunctuated or upper-case block is treated as a
heading and starts a section, wrapped short blocks are merged forward, and the
result is capped by `EXAM_ENGINE_NLP_MAX_SECTIONS`. It also returns each
section's span. The service splits the already-normalised document text, so
`document_sections.char_start`/`char_end` are exact offsets into the document's
`ProcessedText.text` and `text[start:end]` equals the section text. Expected
language comes from `EXAM_ENGINE_NLP_LANGUAGE`; an unsupported expectation or a
detected unsupported language is recorded as `language_unsupported`.

## 5. Keyphrases

Three interchangeable extractors (`--method` / `EXAM_ENGINE_NLP_KEYWORDS`):

| Method | Approach |
|---|---|
| `tfidf` (default) | corpus TF-IDF over word 1–3-grams, sublinear TF, phrase preference over their sub-phrases |
| `rake` | RAKE word-degree scoring over stopword/punctuation-delimited phrases |
| `hybrid` | blends both, preferring multi-word phrases that both agree on |

All three share the same tokenisation (`analysis_tokens`) and the same noise
filter: page furniture, publisher boilerplate, pure numbers, single letters and
terms that only occur inside a longer kept phrase are removed. Stop words are
dropped by default (`EXAM_ENGINE_NLP_STRIP_STOPWORDS=true`); set it to `false`
to keep them in n-grams. `EXAM_ENGINE_NLP_LEMMATIZE=true` reduces regular
plurals and verb endings before scoring (`algorithms` → `algorithm`). Scores are
corpus-relative, so the top terms reflect what distinguishes this course rather
than what is merely frequent. Each record stores `term`, `normalized`, `score`,
`rank`, `n_gram`, `method` and the owner it was attributed to.

## 6. Entities

`rule_based` (default, offline) matches labelled lexical patterns and keeps the
highest-priority non-overlapping spans:

`NOTATION` (e.g. `f(n)`), `METHOD`, `PERSON`, `ORG`, `DATE`, `ACRONYM`,
`TECHNIQUE` (e.g. "binary tree", "merge sort") and `PROPER_NOUN`.

`spacy` is opt-in and requires `pip install -e ".[nlp-spacy]"` plus a downloaded
model; if the package or model is missing the service records
`model_unavailable` and falls back to the rule-based extractor. Every entity row
carries `method`, `confidence`, character offsets into that owner's
`ProcessedText.text` and a metadata blob. Entities are extracted for every
processed owner, so the same surface form can appear once per document, section,
question and syllabus unit. Entities are *candidates*, not concepts — the CLI
says so explicitly.

## 7. Concepts

`concept_candidates` has two origins:

* **`syllabus`** — one record per syllabus topic, `verified = true`,
  `confidence = 0.9`, linked to its `unit_id`, evidence `syllabus_unit:<id>`.
  These are the only concepts treated as ground truth.
* **`inferred`** — top course keyphrases (confidence `0.35–0.75` by relative
  score) plus `METHOD`/`TECHNIQUE`/`ACRONYM`/`NOTATION` entity candidates from
  any owner (confidence `≤ 0.7`). All are `verified = false`.

Inference never duplicates verification: a term already present as a syllabus
concept is skipped, so the same string cannot appear once verified and once
inferred. The table's unique key is `(course_id, origin, normalized)`, and
re-analysis rewrites inferred rows deterministically.

## 8. Embeddings

Providers are resolved by name (`EXAM_ENGINE_EMBEDDING_PROVIDER`, default
`auto`):

| Provider | Model id | Scope | Notes |
|---|---|---|---|
| `sentence-transformers` | `st:<model>` | fitted/downloaded | opt-in; used only when already installed and cached unless downloads are allowed |
| `lsa` | `lsa-tfidf-v1-<corpus-fingerprint>-d<dim>` | corpus-fitted | TF-IDF → truncated SVD, deterministic |
| `hashing` | `hashing-tfidf-v1-d<dim>` | stateless | signed feature hashing of word uni/bi-grams; always available |

`auto` prefers the neural model when it is already available, then LSA, then
hashing, recording a warning for each downgrade. Vectors are L2-normalised.

**Cache correctness.** Vectors live in `embedding_cache`, keyed by model id and
text SHA-256. Stateless models key on text alone; corpus-fitted models embed a
fingerprint of the fitted corpus in their `model_id`, so changing course text
cannot return stale vectors. `embeddings` links each owner to its cached vector
and stores `text_sha256`, `dim` and `model_id`.

Report totals distinguish text-level work from row-level output:
`embedding_texts_computed`, `embedding_texts_cached` and `embeddings` (owner
rows). Owners that share identical text share one cached vector.

## 9. Topics

The default model is TF-IDF → truncated SVD → k-means on the reduced space,
with topic terms read from the cluster centroid in TF-IDF space (deterministic,
fixed seed, no downloads). Guards, in order:

* fewer than three documents → `insufficient_data`, no topics;
* fewer than five vocabulary terms → `insufficient_data`;
* fewer than two distinct texts → `insufficient_data` (identical documents have
  no structure to discover);
* `k` is capped at `min(requested, unique_texts, len(docs) // 2)`, and the cap
  is reported with the requested and used values;
* a corpus that collapses into a single topic emits an `insufficient_data`
  warning saying it should be read as "no structure found", not a discovery;
* empty clusters are dropped with a note;
* the projection is refused when fewer than two components are possible.

Topics store `scope` (`document`, or `section` when the course has fewer than
three documents), `method`, `k`, `size`, term weights, member weights (cosine to
centroid) and the clustering parameters. Topic warnings are run-level and live
on `nlp_runs`, not on individual topics.

## 10. Persistence

Migration `0003_nlp_foundation.sql` adds: `processed_texts`, `document_sections`,
`embedding_cache`, `embeddings`, `keyphrases`, `entities`, `concept_candidates`,
`nlp_topics`, `nlp_topic_terms`, `nlp_topic_members` and `nlp_runs`. All of them
cascade from `courses`/`documents`, so removing a course removes its analysis
without touching other courses. `nlp_runs` is the audit trail for every analysis
attempt, including failures, so the CLI can show what ran and with which models.
Migration `0011_nlp_info_only_status.sql` is a data-only repair: it reclassifies
legacy `partial` runs whose findings are all informational and leaves their
warnings attached.

Every analysis run replaces the course's derived rows: keyphrases, entities,
concepts, topics, sections and embeddings are deleted and rebuilt, while
`processed_texts` is reused when its hash is valid and pruned when an owner no
longer exists. `nlp reset` deletes the same derived rows plus the cached texts,
but leaves Phase 1 data and the shared `embedding_cache` untouched.

## 11. CLI

```bash
exam-engine nlp analyze [--course ID] [--force] [--json]
exam-engine nlp status [--json]
exam-engine nlp providers
exam-engine nlp keywords --course ID [--method tfidf|rake|hybrid] [--scope course|document] [--limit N] [--json]
exam-engine nlp entities --course ID [--label LABEL] [--limit N] [--json]
exam-engine nlp concepts --course ID [--origin all|syllabus|inferred] [--limit N] [--json]
exam-engine nlp topics --course ID [--json]
exam-engine nlp preprocess --document ID [--sentences N]
```

`analyze` prints one line per course with the status, stage totals and the
embedding model id that actually ran, then any warnings; `--json` emits the full
run report (including the keyword, entity and topic methods).

## 12. Uncertainty codes

| Code | Meaning |
|---|---|
| `empty_text` | nothing analysable after normalisation; course status `insufficient_data` |
| `language_unsupported` | configured or detected language outside the supported set; results unreliable |
| `model_unavailable` | requested optional model missing; rule-based/offline fallback used |
| `model_failure` | embeddings or analysis stage failed; run marked `partial`/`failed` |
| `insufficient_data` | corpus too small or degenerate for topics; details record the fallback |
| `lexical_entities` (info) | entities came from lexical rules, not a semantic model |

A run is `ok` when it produced data and has only informational notes. It is
`partial` when it succeeded with at least one `warning`- or `error`-severity
finding, `insufficient_data` when there was nothing to analyse, and `failed` when
a stage raised. Informational notes such as `lexical_entities` or the
section-topic fallback stay in the report without downgrading an otherwise
successful run.

## 13. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_EMBEDDING_PROVIDER` | `auto` | `auto`, `hashing`, `lsa`, `sentence-transformers` |
| `EXAM_ENGINE_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | neural model name |
| `EXAM_ENGINE_EMBEDDING_DIM` | `256` | hashing dimension |
| `EXAM_ENGINE_LSA_DIM` | `128` | LSA dimension |
| `EXAM_ENGINE_ALLOW_MODEL_DOWNLOAD` | `false` | allow downloading a neural model |
| `EXAM_ENGINE_NLP_KEYWORDS` | `tfidf` | `tfidf`, `rake` or `hybrid` |
| `EXAM_ENGINE_NLP_ENTITIES` | `rule_based` | `rule_based` or `spacy` |
| `EXAM_ENGINE_SPACY_MODEL` | `en_core_web_sm` | spaCy model name |
| `EXAM_ENGINE_NLP_TOP_K` | `20` | keyphrases kept per document/course / inferred concepts |
| `EXAM_ENGINE_NLP_TOPICS_K` | `5` | requested topic count (capped by corpus size) |
| `EXAM_ENGINE_NLP_MAX_DOCUMENTS` | `200` | documents analysed per course |
| `EXAM_ENGINE_NLP_MAX_SECTIONS` | `200` | sections analysed per document |
| `EXAM_ENGINE_NLP_LANGUAGE` | `en` | expected language |
| `EXAM_ENGINE_NLP_LEMMATIZE` | `false` | lemmatise keyphrase terms |
| `EXAM_ENGINE_NLP_STRIP_STOPWORDS` | `true` | drop stopwords from keyphrase n-grams |

Dependencies: `numpy`, `scikit-learn` and `joblib` are required (they ship with
scikit-learn). `sentence-transformers` and `spacy` are optional extras
(`pip install -e ".[neural]"`, `pip install -e ".[nlp-spacy]"`).

## 14. Verification

`tests/test_nlp_*.py` cover the primitives (`text`, `keywords`, `embeddings`,
`entities`, `topics`) and the service end to end:

* persisted rows for every stage, with model ids and a stored run report;
* syllabus concepts verified, inferred concepts unverified and disjoint;
* course isolation of keyphrases, concepts, embeddings;
* cache reuse on rerun (no duplicate rows, stable ids) and invalidation when a
  document's text changes;
* section spans that slice the stored document text exactly;
* stale derived rows removed when a document loses its text, and by `nlp reset`;
* entities for documents, sections, questions and syllabus units;
* `lemmatize` and `strip_stopwords` actually changing keyphrase terms;
* status reporting the latest failed run;
* informational notes leaving a successful run `ok`, while a warning-level finding
  still marks it `partial`;
* `insufficient_data` for empty courses, identical texts and tiny corpora;
* CLI commands and JSON output.

These tests prove plumbing and determinism. They do **not** establish that
keyphrases, entity labels or topic assignments are pedagogically correct; no
labelled NLP benchmark is bundled with this phase.

## 15. Known limitations

* Keyphrase scoring is statistical. Domain phrases that appear once may be
  outranked by frequent boilerplate; the noise filter removes obvious furniture
  but not course-specific filler.
* Inferred concepts are candidates only. Nothing in this phase decides that two
  different strings mean the same concept; synonym merging belongs to a later
  phase.
* The rule-based entity extractor is lexical: it can label a capitalised heading
  as `PROPER_NOUN` and does not disambiguate people, organisations or terms.
  Because entities are extracted per owner, the same surface form appears once
  per document, section, question and syllabus unit; there is no cross-owner
  roll-up table yet.
* Section splitting is a heuristic on blank lines and headings; PDF text with
  hard line breaks (no blank lines) yields one section per paragraph group, not
  one per heading.
* `--force` recomputes text and derived rows but still reuses cached vectors
  when the text hash is unchanged.
* Topic quality degrades on small corpora by design; the guards prefer "no
  topics" over unstable clusters.
* Only English preprocessing is tuned. Non-Latin scripts are detected and
  flagged; other Latin-script languages usually come back as `unknown` (no
  stop-word evidence) and are analysed with English rules, so their results are
  unreliable.
* The neural embedding provider is untested in CI because it requires a model
  download; the hashing and LSA paths are exercised by tests.
