# Intelligent Exam Knowledge & Assessment Engine — Phases 1–11

Multi-course ingestion, NLP analysis, question understanding, question-paper
analytics, a persistent question knowledge graph, prerequisite discovery with
explainable learning paths, evidence-backed coverage-gap detection, historical
Exam DNA question-setting patterns, a review-only concept evolution
tracker, grounded, review-first automatic question generation, and advanced
assessment intelligence — question mutation, adversarial questions and
evidence-bounded misconception mining — for the exam-knowledge platform.

Phase 1 turns messy course material — question papers, syllabi, notes and question
banks — into validated, provenance-tracked, course-scoped records. Phase 2 derives
normalised text, keyphrases, entity candidates, verified/inferred concepts,
embeddings and topics from those records, and reports where the data is too thin to
support a conclusion. Phase 3 adds question structure, course-scoped syllabus
mapping, multi-label question types, Bloom candidates, provisional difficulty and
batched similarity. Phase 4 adds read-only question-paper analytics: repetition,
unit/topic frequency, marks, estimated type/difficulty distributions, observed
period trends and evidence-based historical topic prominence. Phase 5 adds a
persistent, course-scoped knowledge graph over institutions, subjects, courses,
syllabi, units, topics, concepts, questions, documents and examination sessions,
with evidence-bearing relationships and a query/review layer. Phase 6 adds
reviewable prerequisite candidates and an explainable dependency-ordered learning
path service that reuses that graph. Phase 7 adds a read-only, evidence-backed
coverage-gap detector that compares syllabus concepts with the available question
corpus and reports covered, low-coverage, unmapped, uncertain and
insufficient-data concepts without treating absence as proof. Phase 8 adds a
read-only Exam DNA profile: paper- and session-level distributions of question
types, marks, units, Bloom levels, sections and command verbs, recurring-pattern
denominators, and clearly separated exact/near/semantic/concept-related question
groups. It never turns historical frequency into a forecast. Phase 9 adds a
read-only concept evolution tracker: per-document date provenance, concept
timelines, review-only change events for terminology, context, syllabus placement
and assessment emphasis, version comparisons, and relationship changes observed in
the graph. It describes evolution inside the analysed corpus and never rewrites a
canonical concept.

Phase 10 adds grounded automatic question generation: for a course's own syllabus
concepts it composes reviewable practice questions, quotes the evidence each one is
built on, records the requested and estimated difficulty and Bloom level, validates
every item against named checks and rejects duplicates — while never treating
validation as proof of correctness and never using examination papers as sources.

Phase 11 adds advanced assessment intelligence in three separable capabilities.
A *question mutation* engine derives variants of a stored question while declaring,
per strategy, which properties must stay invariant (objective, concepts, answer key,
single correct answer, format). An *adversarial question generator* writes thirteen
kinds of challenging but fair question from real course passages, each with a rubric
and a fairness check, and never invents a model answer. *Misconception mining* reads
stored anonymised answers against the reference material for the question and reports
candidate patterns with their evidence, support level, confidence and limitations —
discarding answers it cannot read instead of calling them misconceptions.

**Status:** Phases 1–11 complete. No later-phase intelligence (dashboard integration,
answer grading, forecasting) is implemented here, and no generated item or mined
pattern is ever approved automatically.

---

## What Phases 1–11 deliver

| Capability | Where |
|---|---|
| Validated data models (course, document, page, question with section/unit, syllabus, provenance, results) | [`src/exam_engine/models/`](src/exam_engine/models) |
| Adapters for PDF, DOCX (incl. tables), TXT, CSV/JSON banks, images | [`src/exam_engine/ingest/adapters/`](src/exam_engine/ingest/adapters) |
| Replaceable OCR interface for scanned PDFs and images | [`src/exam_engine/ingest/ocr/`](src/exam_engine/ingest/ocr) |
| Question segmentation with marks extraction and uncertainty reporting | [`segmentation.py`](src/exam_engine/ingest/segmentation.py) |
| Syllabus unit/topic segmentation with teaching hours | [`syllabus_segmenter.py`](src/exam_engine/ingest/syllabus_segmenter.py) |
| Quality scoring, duplicate detection, partial-failure batch ingestion | [`quality.py`](src/exam_engine/ingest/quality.py), [`pipeline.py`](src/exam_engine/ingest/pipeline.py) |
| SQLite storage with checksum-verified migrations and repositories | [`src/exam_engine/storage/`](src/exam_engine/storage) |
| Repeatable manifest-driven import + CLI + persisted run reports | [`manifest.py`](src/exam_engine/ingest/manifest.py), [`cli.py`](src/exam_engine/cli.py) |
| Clearly labelled synthetic sample dataset (2 courses, 7 documents) | [`datasets/sample/`](datasets/sample) |
| Normalised text, keyphrases (TF-IDF/RAKE/hybrid), entity candidates, concepts, embeddings, topics | [`src/exam_engine/nlp/`](src/exam_engine/nlp) |
| Cached, recomputable derived data with per-run audit reports | [`0003_nlp_foundation.sql`](src/exam_engine/storage/migrations/0003_nlp_foundation.sql), [`service.py`](src/exam_engine/nlp/service.py) |
| Question structure (subparts, material references) and course-scoped syllabus mapping | [`src/exam_engine/understanding/`](src/exam_engine/understanding) |
| Configurable question-type, Bloom and difficulty classifiers with evidence | [`taxonomy.py`](src/exam_engine/understanding/taxonomy.py), [`classification.py`](src/exam_engine/understanding/classification.py) |
| Batched exact/near/semantic/concept-related question comparison | [`similarity.py`](src/exam_engine/understanding/similarity.py) |
| Phase 3 persistence and run audit | [`0004_question_understanding.sql`](src/exam_engine/storage/migrations/0004_question_understanding.sql) |
| Read-only analytics engine for repetition, mapping distributions and marks | [`src/exam_engine/analytics/`](src/exam_engine/analytics) |
| Observed-period trends and transparent historical topic priority | [`service.py`](src/exam_engine/analytics/service.py) |
| Structured analytics reports and focused inspectors | [`models/analytics.py`](src/exam_engine/models/analytics.py), [`cli_analytics.py`](src/exam_engine/cli_analytics.py) |
| Clearly labelled multi-session synthetic analytics fixture | [`datasets/analytics_sample/`](datasets/analytics_sample) |
| Persistent typed knowledge graph with evidence, review status and provenance | [`src/exam_engine/graph/`](src/exam_engine/graph), [`models/graph.py`](src/exam_engine/models/graph.py) |
| Concept identity, alias preservation and review-only duplicate candidates | [`builder.py`](src/exam_engine/graph/builder.py), [`service.py`](src/exam_engine/graph/service.py) |
| SQLite adjacency storage, indexed lookups and referential integrity | [`0005_knowledge_graph.sql`](src/exam_engine/storage/migrations/0005_knowledge_graph.sql), [`graph_repository.py`](src/exam_engine/storage/graph_repository.py) |
| Graph build/query/review/integrity CLI | [`cli_graph.py`](src/exam_engine/cli_graph.py) |
| Reviewable prerequisite candidates with merged evidence and cycle/conflict detection | [`src/exam_engine/prerequisite/`](src/exam_engine/prerequisite), [`models/prerequisite.py`](src/exam_engine/models/prerequisite.py) |
| Explainable learning paths for concepts, topics or units with known-concept input | [`discovery.py`](src/exam_engine/prerequisite/discovery.py), [`path.py`](src/exam_engine/prerequisite/path.py) |
| Prerequisite discovery/review/validate/path CLI | [`cli_prerequisite.py`](src/exam_engine/cli_prerequisite.py) |
| Evidence-backed syllabus coverage, unit summaries and unmapped-question reports | [`src/exam_engine/coverage/`](src/exam_engine/coverage), [`models/coverage.py`](src/exam_engine/models/coverage.py) |
| Coverage statuses, thresholds, provenance and explainable findings | [`service.py`](src/exam_engine/coverage/service.py) |
| Coverage report/concepts/concept/units/unmapped CLI | [`cli_coverage.py`](src/exam_engine/cli_coverage.py) |
| Historical Exam DNA profile: formats, marks, units, Bloom, sections and command verbs | [`src/exam_engine/exam_dna/`](src/exam_engine/exam_dna), [`models/exam_dna.py`](src/exam_engine/models/exam_dna.py) |
| Paper/session recurrence denominators, duplicate-paper exclusion and reviewable pattern evidence | [`service.py`](src/exam_engine/exam_dna/service.py) |
| Ordered dated-session series and descriptive share changes (never forecasts) | [`service.py`](src/exam_engine/exam_dna/service.py) |
| Exact/near/semantic/concept-related question groups with wording differences | [`structure.py`](src/exam_engine/exam_dna/structure.py) |
| Exam DNA profile/patterns/questions/papers CLI | [`cli_exam_dna.py`](src/exam_engine/cli_exam_dna.py) |
| Concept evolution report: dated observations, timelines, review-only change events | [`src/exam_engine/evolution/`](src/exam_engine/evolution), [`models/evolution.py`](src/exam_engine/models/evolution.py) |
| Explicit per-document date resolution with type, field, conflicts and invalid values | [`dates.py`](src/exam_engine/evolution/dates.py) |
| Syllabus version comparison, assessment-window reuse and relationship changes | [`service.py`](src/exam_engine/evolution/service.py) |
| Evolution report/timeline/changes/compare CLI | [`cli_evolution.py`](src/exam_engine/cli_evolution.py) |
| Deterministic question templates and default marks/Bloom per format | [`templates.py`](src/exam_engine/generation/templates.py) |
| Provider interface, offline template provider and opt-in OpenAI-compatible provider | [`providers.py`](src/exam_engine/generation/providers.py) |
| Named structural, alignment, answerability, MCQ, clarity and cognitive validation checks | [`validation.py`](src/exam_engine/generation/validation.py) |
| Duplicate/variant detection reusing Phase 3 similarity, over generated and historical questions | [`dedupe.py`](src/exam_engine/generation/dedupe.py) |
| Grounded generation, provenance-bearing assembly, review state and batch reports | [`service.py`](src/exam_engine/generation/service.py), [`models/generation.py`](src/exam_engine/models/generation.py) |
| Batch and question persistence with review history and revision-safe writes | [`0007_generated_questions.sql`](src/exam_engine/storage/migrations/0007_generated_questions.sql), [`generation_repository.py`](src/exam_engine/storage/generation_repository.py) |
| Generation generate/formats/list/show/batches/failures/review/review-batch/export/review-summary CLI | [`cli_generation.py`](src/exam_engine/cli_generation.py) |
| Mutation strategy registry with declared invariants and deterministic variant builders | [`mutation.py`](src/exam_engine/advanced/mutation.py), [`source.py`](src/exam_engine/advanced/source.py) |
| Thirteen fair adversarial challenge strategies with rubric generation | [`adversarial.py`](src/exam_engine/advanced/adversarial.py) |
| Misconception rules, support levels, evidence excerpts and discard reasons | [`misconception.py`](src/exam_engine/advanced/misconception.py) |
| Phase 11 invariant and fairness checks layered on Phase 10 validation | [`verification.py`](src/exam_engine/advanced/verification.py) |
| Reused scope/evidence/similarity context (new public `ScopeContext`) | [`service.py`](src/exam_engine/generation/service.py), [`service.py`](src/exam_engine/advanced/service.py) |
| Response and misconception persistence with review-history preservation | [`0008_advanced_assessment.sql`](src/exam_engine/storage/migrations/0008_advanced_assessment.sql), [`0009_response_identified.sql`](src/exam_engine/storage/migrations/0009_response_identified.sql), [`advanced_repository.py`](src/exam_engine/storage/advanced_repository.py) |
| Advanced mutate/adversarial/variants/show/review, responses and misconceptions CLI | [`cli_advanced.py`](src/exam_engine/cli_advanced.py) |

## Quickstart

```bash
python -m pip install -e .          # or: python -m pip install -e ".[dev,ocr]"
python -m exam_engine init-db
python -m exam_engine import datasets/sample/manifest.json
python -m exam_engine stats
python -m exam_engine questions --course demo-cs201-2024-v1 --limit 10
python -m exam_engine syllabus --course demo-cs201-2024-v1
python -m exam_engine nlp analyze            # Phase 2: analyse every course
python -m exam_engine nlp status
python -m exam_engine nlp concepts --course demo-cs201-2024-v1 --origin syllabus
python -m exam_engine understanding analyze       # Phase 3: all courses
python -m exam_engine understanding status
python -m exam_engine understanding questions --course demo-cs201-2024-v1
python -m exam_engine understanding similar --course demo-cs201-2024-v1
python -m exam_engine analytics report --course demo-cs201-2024-v1        # Phase 4
python -m exam_engine analytics repetitions --course demo-cs201-2024-v1
python -m exam_engine analytics trends --course demo-cs201-2024-v1
python -m exam_engine analytics topics --course demo-cs201-2024-v1
python -m exam_engine graph build                # Phase 5: all courses
python -m exam_engine graph status
python -m exam_engine graph concepts --course demo-cs201-2024-v1
python -m exam_engine graph duplicates --course demo-cs201-2024-v1
python -m exam_engine graph validate --course demo-cs201-2024-v1
python -m exam_engine prerequisites discover --course demo-cs201-2024-v1   # Phase 6
python -m exam_engine prerequisites candidates --course demo-cs201-2024-v1
python -m exam_engine prerequisites validate --course demo-cs201-2024-v1
python -m exam_engine prerequisites path --course demo-cs201-2024-v1 --target "Binary Search"
python -m exam_engine coverage report --course demo-cs201-2024-v1          # Phase 7
python -m exam_engine coverage units --course demo-cs201-2024-v1
python -m exam_engine coverage concepts --course demo-cs201-2024-v1 --status uncertain
python -m exam_engine coverage unmapped --course demo-cs201-2024-v1
python -m exam_engine exam-dna profile --course demo-cs201-2024-v1           # Phase 8
python -m exam_engine exam-dna patterns --course demo-cs201-2024-v1 --recurring
python -m exam_engine exam-dna questions --course demo-cs201-2024-v1 --kind exact
python -m exam_engine exam-dna papers --course demo-cs201-2024-v1
python -m exam_engine evolution report --course demo-cs201-2024-v1            # Phase 9
python -m exam_engine evolution timeline --course demo-cs201-2024-v1 --concept "Stacks and queues"
python -m exam_engine evolution changes --course demo-cs201-2024-v1 --review-only
python -m exam_engine generation formats                                    # Phase 10
python -m exam_engine generation generate --course demo-cs201-2024-v1 \
    --concept "Stacks and queues" --count 3 --format explanation --bloom understand
python -m exam_engine generation list --course demo-cs201-2024-v1
python -m exam_engine generation failures --batch gb_…
python -m exam_engine generation review --question gq_… --status approved
python -m exam_engine advanced capabilities                                # Phase 11
python -m exam_engine advanced mutate --course demo-cs201-2024-v1 \
    --question gq_… --strategy rephrase --strategy scenario --count 2
python -m exam_engine advanced variants --course demo-cs201-2024-v1 --origin mutation
python -m exam_engine advanced adversarial --course demo-cs201-2024-v1 \
    --concept "Stacks and queues" --count 3
python -m exam_engine advanced responses import --course demo-cs201-2024-v1 \
    --file responses.json
python -m exam_engine advanced misconceptions analyse --course demo-cs201-2024-v1
python -m exam_engine advanced misconceptions list --course demo-cs201-2024-v1 --status candidate
python -m exam_engine advanced misconceptions export --course demo-cs201-2024-v1 \
    --syllabus syl_… --out reports/misconceptions.json
```

The separate multi-session analytics fixture demonstrates period trends without
pretending to be real university history:

```bash
python -m exam_engine import datasets/analytics_sample/manifest.json
python -m exam_engine understanding analyze
python -m exam_engine analytics report --course demo-analytics-cs201-2024-v1
python -m exam_engine graph build --course demo-analytics-cs201-2024-v1
python -m exam_engine graph status
python -m exam_engine coverage report --course demo-analytics-cs201-2024-v1
python -m exam_engine exam-dna profile --course demo-analytics-cs201-2024-v1
```

`python -m exam_engine` always works. If your Python `Scripts` directory is on
`PATH`, the equivalent `exam-engine …` console script is available too.

Database path defaults to `data/exam_engine.db`; override with `--db PATH` or
`EXAM_ENGINE_DB`.

### Ingesting your own material

```bash
python -m exam_engine ingest paper.pdf \
  --course-id my-uni-cs201-2024-v1 \
  --course-code CS201 \
  --course-title "Data Structures" \
  --institution-code my-uni \
  --institution-name "My University" \
  --kind question_paper \
  --source-id my-uni-department-papers \
  --dataset-type permissioned \
  --license "Used with permission of the department" \
  --academic-year 2024-25 \
  --syllabus-version v1
```

For more than a handful of files, write a manifest: see
[docs/datasets.md](docs/datasets.md).

## Supported inputs

| Format | Adapter | Notes |
|---|---|---|
| `.pdf` | pdfplumber, PyMuPDF fallback | Page-level text + page references; detects image-only pages for OCR; rejects encrypted/oversized PDFs |
| `.docx` | python-docx | Paragraph + table content in document order; DOCX has no page numbers, so locators use line ranges |
| `.txt`, `.text`, `.md` | charset-normalizer | UTF-8 → detected encoding fallback; form-feed (`\x0c`) splits pages |
| `.csv`, `.json` | question-bank adapter | Column/field aliasing, row-level error reporting, `sections[].questions[]` JSON form supported |
| `.png`, `.jpg`, `.tiff`, `.bmp`, `.webp` | OCR interface | Always needs OCR; stored as `needs_ocr` when no provider is configured |

## Multi-course design

Every document, page, question and syllabus belongs to exactly one `course_id`.
A course carries institution, programme, academic year and syllabus version, so
the same syllabus code at two institutions, or two revisions of one course, never
collide:

```
course_id = demo-cs201-2024-v1
institution_code = demo-iot
course_code = CS201
academic_year = 2024-25
syllabus_version = v1
```

Document ids are content-addressed (`sha256(course_id + content_sha256)`), so
re-importing the same bytes into the same course is idempotent, while the same
bytes in a different course produce an independent document with its own
questions.

Every document also records provenance: dataset source id, dataset type
(`official`, `public`, `openly_licensed`, `permissioned`, `synthetic`, `unknown`),
licence, source URL, retrieval date and publisher.

## Uncertainty is data, not log noise

Extraction never silently invents questions, marks or syllabus units:

* a numbering gap produces no placeholder question — it is recorded as an
  uncertainty on the surrounding questions and on the document;
* missing marks stay `None` (never `0`), with a `no_marks_detected` uncertainty;
* ambiguous marks (`[5]`, several values in one question) get an explicit code
  and a confidence penalty;
* instruction-like text is flagged `possible_instruction` instead of dropped;
* numbered instruction blocks that would swallow real question numbers are excluded
  from the question list, with an `instruction_markers_dropped` note recording what
  was removed and why;
* every question carries `confidence`, `needs_review` and a source locator such
  as `page 2, lines 4-6` or `row 14`.

## Architecture

```
src/exam_engine/
  config.py              settings + safety limits (env driven)
  models/                Pydantic records + enums (the validated domain model)
  ingest/
    validation.py        suffix/magic-byte/size/path checks
    normalize.py         conservative cleaning + duplicate fingerprints
    adapters/            one module per input format (registry-based)
    ocr/                 OCRProvider protocol, Tesseract impl, PDF rasteriser
    segmentation.py      numbered-question extraction, marks, uncertainty
    syllabus_segmenter.py UNIT/MODULE/CHAPTER parsing, hours, topics
    quality.py           text-quality scoring, duplicate marking
    manifest.py          validated JSON manifest (the repeatable import unit)
    pipeline.py          per-document transactions, OCR, persistence, reports
  nlp/
    text.py              normalisation, sentence splitter, tokeniser, language hint
    preprocess.py        ProcessedText builder, config hashing, section splitter
    keywords.py          TF-IDF / RAKE / hybrid keyphrase extractors
    entities.py          rule-based extractor + optional spaCy adapter
    embeddings.py        hashing / LSA / sentence-transformers providers + cache keys
    vectors.py           vector codec and cosine utilities
    topics.py            TF-IDF -> SVD -> k-means with small-corpus guards
    service.py           per-course orchestration, concepts, run reports
  understanding/
    taxonomy.py          configurable label definitions and weighted cues
    structure.py         sub-part spans, material references, extraction doubt
    mapping.py           course-scoped TF-IDF + embedding syllabus candidates
    classification.py    question types, Bloom levels, provisional difficulty
    similarity.py        candidate retrieval and four similarity kinds
    service.py           per-course orchestration and run reports
  storage/
    migrations/*.sql     checksum-verified schema (0003 = Phase 2, 0004 = Phase 3, 0005–0006 = Phase 5, 0007 = Phase 10, 0008–0009 = Phase 11)
    schema.py            ORM rows mirroring the migrations
    repositories.py      the only SQL surface (ingest)
    nlp_repositories.py  the only SQL surface (derived NLP data)
    question_repositories.py  the only SQL surface (Phase 3 data)
    mappers.py           model <-> row conversion (ingest and NLP)
    understanding_mappers.py  model <-> row conversion (Phase 3)
    generation_repository.py  the only SQL surface (Phase 10 batches and questions)
    generation_mappers.py     model <-> row conversion (Phase 10)
    advanced_repository.py    the only SQL surface (Phase 11 responses and misconceptions)
    advanced_mappers.py       model <-> row conversion (Phase 11)
  analytics/
    metadata.py         explicit examination-period resolution; never upload time
    service.py          read-only reports, denominators and ranking factors
  graph/
    builder.py          pure Phase 1-4 -> typed nodes/edges with evidence
    service.py          transactional build, queries, validation and review
  prerequisite/
    discovery.py        weak-evidence prerequisite candidates and validation
    graph_ops.py        iterative cycle/reverse-conflict detection
    path.py             target composition and explainable topological ordering
  cli.py                 init-db, import, ingest, courses, documents, questions, syllabus, runs, stats
  cli_nlp.py             nlp analyze/status/providers/keywords/entities/concepts/topics/preprocess
  cli_understanding.py   understanding analyze/status/questions/show/similar/runs/taxonomy/reset
  cli_analytics.py       analytics report/repetitions/topics/trends/status
  cli_graph.py           graph build/status/concepts/questions/topics/related/neighborhood/evidence/duplicates/isolated/unmapped/validate/review/reset
  cli_prerequisite.py    prerequisites discover/candidates/validate/path/review/disable/enable
  coverage/
    service.py          read-only coverage projection, mapping reuse, metrics and explanations
  cli_coverage.py        coverage report/concepts/concept/units/unmapped
  exam_dna/
    service.py          read-only paper/session recurrence, duplicate copies and question groups
    structure.py        explicit command-verb, choice and wording cues
  cli_exam_dna.py        exam-dna profile/patterns/questions/papers
  generation/
    templates.py        deterministic (format, Bloom) task recipes, defaults, capability matrix
    providers.py        provider protocol, offline template provider, opt-in model provider
    validation.py       named structural/alignment/answerability/MCQ/clarity/cognitive checks
    dedupe.py           duplicate decisions over generated and historical questions (Phase 3 reuse)
    service.py          scope, evidence, provider call, assembly, validation, persistence, review
  cli_generation.py      generation generate/formats/list/show/batches/failures/review/review-batch/export/review-summary
  advanced/
    source.py           error type, source-question and evidence-context views
    mutation.py         strategy registry, invariants and deterministic variant builders
    adversarial.py      challenge registry, fair question builders and rubric generation
    misconception.py    response detectors, support levels, evidence and discard reasons
    verification.py     invariant and fairness checks on top of Phase 10 validation
    service.py          scope, assembly, dedupe, persistence, mining and review
  cli_advanced.py        advanced capabilities/mutate/adversarial/variants/show/review,
                         advanced responses …, advanced misconceptions …
tools/build_sample_dataset.py   regenerates the synthetic sample dataset
datasets/sample/                committed synthetic sample + manifest
datasets/analytics_sample/      synthetic three-session fixture for Phase 4/tests
tests/                          602 tests across models, adapters, segmentation, storage, pipeline, NLP, understanding, analytics, graph, prerequisites, coverage, Exam DNA, concept evolution, question generation, advanced assessment, CLI, artefacts
```

Deeper design notes: [docs/phase-1-ingestion.md](docs/phase-1-ingestion.md),
[docs/phase-2-nlp.md](docs/phase-2-nlp.md),
[docs/phase-3-understanding.md](docs/phase-3-understanding.md),
[docs/phase-4-analytics.md](docs/phase-4-analytics.md),
[docs/phase-5-knowledge-graph.md](docs/phase-5-knowledge-graph.md),
[docs/phase-6-prerequisites.md](docs/phase-6-prerequisites.md),
[docs/phase-7-coverage.md](docs/phase-7-coverage.md),
[docs/phase-8-exam-dna.md](docs/phase-8-exam-dna.md),
[docs/phase-9-concept-evolution.md](docs/phase-9-concept-evolution.md),
[docs/phase-10-question-generation.md](docs/phase-10-question-generation.md) and
[docs/phase-11-advanced-assessment.md](docs/phase-11-advanced-assessment.md).
Dataset/licensing workflow: [docs/datasets.md](docs/datasets.md).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_DB` | `data/exam_engine.db` | SQLite database path |
| `EXAM_ENGINE_OCR_PROVIDER` | `none` | `none`, `tesseract`, or a registered provider |
| `EXAM_ENGINE_MAX_FILE_MB` | `50` | Per-file size limit |
| `EXAM_ENGINE_MAX_PAGES` | `500` | Per-document page limit |
| `EXAM_ENGINE_MAX_DOC_CHARS` | `2000000` | Extracted-text cap per document |
| `EXAM_ENGINE_MAX_QUESTIONS` | `2000` | Questions kept per document |
| `EXAM_ENGINE_MIN_QUESTION_CONFIDENCE` | `0.6` | Below this, a question is flagged `needs_review` |
| `EXAM_ENGINE_EMBEDDING_PROVIDER` | `auto` | `auto`, `hashing`, `lsa` or `sentence-transformers` |
| `EXAM_ENGINE_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Neural embedding model name |
| `EXAM_ENGINE_EMBEDDING_DIM` | `256` | Hashing-vector dimension |
| `EXAM_ENGINE_LSA_DIM` | `128` | LSA dimension |
| `EXAM_ENGINE_ALLOW_MODEL_DOWNLOAD` | `false` | Permit downloading a neural embedding model |
| `EXAM_ENGINE_NLP_KEYWORDS` | `tfidf` | `tfidf`, `rake` or `hybrid` |
| `EXAM_ENGINE_NLP_ENTITIES` | `rule_based` | `rule_based` or `spacy` (needs the extra + model) |
| `EXAM_ENGINE_NLP_TOP_K` | `20` | Keyphrases kept per document/course / inferred concepts |
| `EXAM_ENGINE_NLP_TOPICS_K` | `5` | Requested topic count (capped by corpus size) |
| `EXAM_ENGINE_NLP_MAX_DOCUMENTS` | `200` | Documents analysed per course |
| `EXAM_ENGINE_NLP_MAX_SECTIONS` | `200` | Sections analysed per document |
| `EXAM_ENGINE_NLP_LANGUAGE` | `en` | Expected analysis language |
| `EXAM_ENGINE_NLP_LEMMATIZE` | `false` | Lemmatise keyphrase terms (`algorithms` → `algorithm`) |
| `EXAM_ENGINE_NLP_STRIP_STOPWORDS` | `true` | Drop stopwords from keyphrase n-grams |
| `EXAM_ENGINE_Q3_MAX_QUESTIONS` | `2000` | Questions analysed per course |
| `EXAM_ENGINE_Q3_MAX_SIMILARITY_QUESTIONS` | `2000` | Questions entering similarity |
| `EXAM_ENGINE_Q3_TAXONOMY` | unset | Custom taxonomy JSON path |
| `EXAM_ENGINE_Q3_MAPPING_THRESHOLD` | `0.18` | Minimum syllabus-unit score |
| `EXAM_ENGINE_Q3_MAPPING_MARGIN` | `0.04` | Required lead over runner-up |
| `EXAM_ENGINE_Q3_TYPE_THRESHOLD` | `0.30` | Minimum question-type score |
| `EXAM_ENGINE_Q3_BLOOM_THRESHOLD` | `0.30` | Minimum Bloom score |
| `EXAM_ENGINE_Q3_DIFFICULTY_EASY_BELOW` | `0.32` | Easy/moderate boundary |
| `EXAM_ENGINE_Q3_DIFFICULTY_HARD_ABOVE` | `0.60` | Moderate/hard boundary |
| `EXAM_ENGINE_Q3_SIMILARITY_NEAR` | `0.78` | Near-duplicate threshold |
| `EXAM_ENGINE_Q3_SIMILARITY_SEMANTIC` | `0.68` | Semantic threshold |
| `EXAM_ENGINE_Q3_SIMILARITY_CONCEPT` | `0.35` | Concept-related threshold |
| `EXAM_ENGINE_Q4_MAX_QUESTIONS` | `20000` | Questions per analytics report |
| `EXAM_ENGINE_Q4_SMALL_SAMPLE` | `5` | Small-sample warning threshold |
| `EXAM_ENGINE_Q4_LOW_CONFIDENCE` | `0.5` | Analytics low-confidence threshold |
| `EXAM_ENGINE_Q4_MIN_TREND_PERIODS` | `2` | Minimum observed periods for trends |
| `EXAM_ENGINE_Q4_MIN_PERIOD_QUESTIONS` | `3` | Minimum questions for a reliable change |
| `EXAM_ENGINE_Q4_SIMILARITY_THRESHOLD` | `0.0` | Report-level similarity cutoff |
| `EXAM_ENGINE_Q4_PRIORITY_FREQUENCY` | `0.30` | Topic-priority frequency weight |
| `EXAM_ENGINE_Q4_PRIORITY_RECENCY` | `0.20` | Topic-priority recency weight |
| `EXAM_ENGINE_Q4_PRIORITY_MARKS` | `0.20` | Topic-priority marks weight |
| `EXAM_ENGINE_Q4_PRIORITY_COVERAGE` | `0.20` | Topic-priority session-coverage weight |
| `EXAM_ENGINE_Q4_PRIORITY_CONFIDENCE` | `0.10` | Topic-priority mapping-confidence weight |
| `EXAM_ENGINE_Q5_MAX_QUESTIONS` | `20000` | Questions per course graph |
| `EXAM_ENGINE_Q5_MAX_SIMILARITIES` | `50000` | Similarity pairs read per course graph |
| `EXAM_ENGINE_Q5_INCLUDE_INFERRED_CONCEPTS` | `true` | Include Phase 2 inferred concepts |
| `EXAM_ENGINE_Q5_INCLUDE_SIMILARITIES` | `true` | Create `SIMILAR_TO`/`RELATED_TO` edges |
| `EXAM_ENGINE_Q5_INFER_PREREQUISITES` | `false` | Create candidate prerequisites from topic order |
| `EXAM_ENGINE_Q5_CONCEPT_DUPLICATE_THRESHOLD` | `0.80` | Duplicate-review similarity cutoff |
| `EXAM_ENGINE_Q6_ORDER_WINDOW` | `2` | Topic positions considered in syllabus-order evidence |
| `EXAM_ENGINE_Q6_CROSS_UNIT_ORDER` | `false` | Link the end of one unit to the start of the next |
| `EXAM_ENGINE_Q6_MAX_CANDIDATES` | `6` | Candidate pairs kept per source concept |
| `EXAM_ENGINE_Q6_COOCCURRENCE_MIN` | `2` | Shared questions required for co-occurrence support |
| `EXAM_ENGINE_Q6_COOCCURRENCE_JACCARD` | `0.34` | Minimum Jaccard for co-occurrence support |
| `EXAM_ENGINE_Q6_PATH_MAX_DEPTH` | `8` | Default prerequisite hops in a learning path |
| `EXAM_ENGINE_Q7_MAX_QUESTIONS` | `20000` | Questions per coverage analysis |
| `EXAM_ENGINE_Q7_COVERED_MIN_QUESTIONS` | `2` | Distinct accepted questions required for `covered` |
| `EXAM_ENGINE_Q7_LOW_CONFIDENCE` | `0.5` | Average accepted mapping score below this makes coverage uncertain |
| `EXAM_ENGINE_Q7_MAPPING_CONFIDENCE_FLOOR` | `0.18` | Minimum persisted Phase 3 mapping score accepted as evidence |
| `EXAM_ENGINE_Q7_SMALL_CORPUS` | `5` | Corpus size below which absence claims are uncertain |
| `EXAM_ENGINE_Q7_MIN_ANALYSIS_COVERAGE` | `0.8` | Minimum Phase 3 analysis coverage for a confident negative |
| `EXAM_ENGINE_Q7_GRAPH_EXPLICIT_MAPPINGS` | `true` | Use Phase 5 aliases and explicit assessment edges |
| `EXAM_ENGINE_Q8_MAX_QUESTIONS` | `20000` | Questions per Exam DNA profile |
| `EXAM_ENGINE_Q8_MIN_PAPERS_FOR_RECURRENCE` | `3` | Papers required before a pattern may be called recurring |
| `EXAM_ENGINE_Q8_RECURRENCE_THRESHOLD` | `0.5` | Minimum paper recurrence rate |
| `EXAM_ENGINE_Q8_DOMINANT_UNIT_SHARE` | `0.25` | Unit mark share counted as dominant |
| `EXAM_ENGINE_Q8_MIN_CONCEPT_PAPERS` | `2` | Papers required for a concept/type pair |
| `EXAM_ENGINE_Q8_DUPLICATE_MIN_QUESTIONS` | `3` | Minimum paper size for question-set copy detection |
| `EXAM_ENGINE_Q8_SIMILARITY_MIN_SCORE` | `0.0` | Minimum stored similarity score reused |
| `EXAM_ENGINE_Q8_MAX_PATTERNS` | `500` | Pattern cap |
| `EXAM_ENGINE_Q8_MAX_GROUPS` | `200` | Question-group cap |
| `EXAM_ENGINE_Q8_LOW_CONFIDENCE` | `0.5` | Classification low-confidence threshold |
| `EXAM_ENGINE_Q9_MAX_DOCUMENTS` | `2000` | Documents read per evolution report |
| `EXAM_ENGINE_Q9_MAX_CONCEPTS` | `500` | Concept cap |
| `EXAM_ENGINE_Q9_MAX_OBSERVATIONS` | `200` | Observations kept per concept |
| `EXAM_ENGINE_Q9_MAX_CHANGES` | `1000` | Change-event cap |
| `EXAM_ENGINE_Q9_CONTEXT_SIMILARITY` | `0.55` | Excerpt lexical-similarity threshold |
| `EXAM_ENGINE_Q9_MIN_CHANGE_CONFIDENCE` | `0.5` | Minimum reported change confidence |
| `EXAM_ENGINE_Q9_MIN_DATED_OBSERVATIONS` | `2` | Dated observations needed for chronology checks |
| `EXAM_ENGINE_Q9_MIN_SYLLABUS_CONFIDENCE` | `0.6` | Minimum later-syllabus confidence for introduction/removal |
| `EXAM_ENGINE_Q9_MIN_ASSESSMENT_QUESTIONS` | `2` | Questions required per assessment window |
| `EXAM_ENGINE_Q9_ASSESSMENT_SHARE_DELTA` | `0.25` | Share delta counted as an assessment change |
| `EXAM_ENGINE_Q9_MAX_EXCERPT_CHARS` | `240` | Excerpt length in observations |
| `EXAM_ENGINE_Q9_INCLUDE_EXAM_DNA` | `true` | Cross-reference Phase 8 profiles |
| `EXAM_ENGINE_Q10_MAX_QUESTIONS` | `20` | Generated questions allowed per request |
| `EXAM_ENGINE_Q10_MIN_MCQ_OPTIONS` / `_MAX_MCQ_OPTIONS` | `3` / `6` | Multiple-choice option bounds |
| `EXAM_ENGINE_Q10_MAX_EVIDENCE` | `4` | Evidence passages attached per question |
| `EXAM_ENGINE_Q10_MAX_EVIDENCE_CHARS` | `240` | Evidence excerpt length cap |
| `EXAM_ENGINE_Q10_MAX_RELATED` | `4` | Related concept labels considered |
| `EXAM_ENGINE_Q10_DUPLICATE_NEAR` | `0.78` | Generated near-duplicate threshold |
| `EXAM_ENGINE_Q10_DUPLICATE_SEMANTIC` | `0.68` | Generated semantic-duplicate threshold |
| `EXAM_ENGINE_Q10_REQUIRE_ANSWER_EVIDENCE` | `true` | Reject a key quoting evidence absent from the cited passages |
| `EXAM_ENGINE_Q10_PROVIDER_TIMEOUT` | `20.0` | Model-provider timeout, seconds |
| `EXAM_ENGINE_Q10_PROVIDER_RETRIES` | `2` | Retries after a retryable provider failure |
| `EXAM_ENGINE_Q10_RETRY_BACKOFF` | `0.5` | Retry backoff, seconds |
| `EXAM_ENGINE_Q10_LLM_BASE_URL` | unset | OpenAI-compatible endpoint for the model provider |
| `EXAM_ENGINE_Q10_LLM_MODEL` | unset | Model name; the model provider stays unavailable without it |
| `EXAM_ENGINE_Q10_LLM_API_KEY` | unset | Credential, read from the environment only (never stored) |
| `EXAM_ENGINE_Q11_MAX_VARIANTS` | `10` | Variants or challenges allowed per request |
| `EXAM_ENGINE_Q11_MAX_RESPONSES` | `5000` | Responses read per misconception analysis |
| `EXAM_ENGINE_Q11_MIN_SUPPORT` | `2` | Minimum independent responses for a candidate pattern |
| `EXAM_ENGINE_Q11_RECURRING_SUPPORT` | `3` | Independent responses required for `recurring` |
| `EXAM_ENGINE_Q11_RECURRING_QUESTIONS` | `2` | Distinct questions required for `recurring` |
| `EXAM_ENGINE_Q11_MIN_ANSWER_WORDS` | `3` | Below this an answer cannot be read as an attempt |
| `EXAM_ENGINE_Q11_EXCERPT_CHARS` | `240` | Evidence excerpt length kept per record (capped at 240) |
| `EXAM_ENGINE_Q11_MIN_REFERENCE_OVERLAP` | `0.08` | Reference-overlap floor recorded in reports |
| `EXAM_ENGINE_Q11_MAX_EVIDENCE` | `6` | Evidence excerpts kept per misconception record |

See [docs/phase-3-understanding.md](docs/phase-3-understanding.md) for the
remaining candidate/top-k settings.

## Tests and checks

```bash
python -m pytest                                     # 602 tests
python -m pytest --cov=exam_engine --cov-report=term-missing
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m mypy
```

Tests use synthetic fixtures for plumbing (generated PDFs, DOCX, scanned PDFs,
CSV/JSON banks) plus the committed synthetic sample. They prove ingestion,
validation, provenance, persistence, partial-failure behaviour, the Phase 2
derived-data contracts, Phase 3 structure/mapping/classification/similarity
contracts including unknown outcomes and course isolation, Phase 4 analytics
denominators, repetition, marks, trends, ranking and data-quality contracts,
Phase 5 graph identity, evidence, idempotency, review, scope,
shared-edge stability, provenance and query contracts, Phase 6 prerequisite
discovery, candidate filtering, cycle/conflict handling, explainable paths, known
starting points, syllabus isolation, target-retaining length limits, mixed-status
explanations and rebuild preservation, Phase 7 coverage statuses, mapping
acceptance, alias and graph-edge filtering, metric denominators, multi-course and
multi-syllabus isolation, missing data, truncation, deterministic reports and CLI
JSON/export/error paths, and Phase 8 paper/session recurrence denominators,
duplicate-copy exclusion, distributions, exact versus similar groups, template
variants, command/choice cues, course and syllabus isolation (including unit
matches from another syllabus version being excluded), dated-session series and
descriptive changes, deterministic read-only profiles and CLI JSON/export/error
paths, and Phase 9 date resolution, ordering, timeline provenance, terminology and
context review events, syllabus version comparison, assessment-window reuse, course
isolation, read-only reproducibility and CLI JSON/export/error paths, and Phase 10
request validation, scope refusal (unknown course, ambiguous or foreign syllabus),
target resolution including unplaced concepts, exclusion of examination material
from grounding, provider capability reporting, unsupported format/Bloom pairs,
template determinism and seeded options, every validation group with its codes and
statuses, thin-evidence handling, dedupe against generated and historical questions
with review-only match kinds, partial batches with explained rejections,
persistence round-trips, revision-safe review, approved-overwrite protection, batch
export and review summaries, and Phase 11 mutation invariants (wording, scenario,
numerical, distractor, related-concept, unsupported-format), request and source
refusals, partial batches, duplicate-copy rejection, objective/concept/answer-key
preservation, format constraints and format checks, keyless-source answer handling,
fairness and scope checks for every challenge
strategy, MCQ distractor validation, review status of uncertain output, single-
observation versus recurring misconception evidence, duplicate imports, related-
concept confusion, partial credit, missing reference answers and concept mappings,
anonymised identifiers, independence warnings for unidentified responses, provenance,
repeatable analysis, review-decision persistence, course and syllabus isolation and
CLI JSON/error paths.
They do **not** establish that keyphrases, types, Bloom levels,
difficulty, mappings, coverage findings, inferred concept changes or generated
questions are educationally correct; no labelled benchmark is bundled, and segmentation
heuristics are deliberately conservative rather than benchmark-optimal.

## Known limitations

* Question/syllabus segmentation is heuristic. Phase 1 recognises numbered
  markers, `SECTION`/`PART` headings and `marks` wording; Phase 3 detects
  `(a)/(i)/(2)` sub-parts when they start a line, follow `;`, or follow sentence
  punctuation. Multi-column layouts and lettered/hand-written numbering remain
  unsupported, and unnumbered prose papers are not segmented.
* Syllabus units come from `UNIT`/`MODULE`/`CHAPTER` headings or DOCX table rows
  that include that keyword and a numeric hours cell; tables whose first column is
  just `1`, `2`, … are not detected.
* Sub-questions share their parent's marks; per-part marks are surfaced as a
  `multiple_marks_values` uncertainty.
* DOCX has no reliable page geometry: one logical page, line-range locators.
* Bare page numbers on their own line are preserved (they are indistinguishable
  from content); `Page 3 of 10`-style furniture is removed.
* Scanned content requires an OCR provider; without one the document is stored
  with status `needs_ocr` and empty page text, never fake text.
* Answer keys are preserved only as opaque metadata on bank questions, not
  modelled as validated answers.
* Encoding detection rejects implausible UTF-16/32 guesses on short files and falls
  back to cp1252; extremely short or mixed-encoding files can still be misread, and
  low-confidence decodings are reported as a warning.
* No network acquisition is performed by this phase; dataset sourcing is a
  documented manual/repeatable workflow.
* Phase 2 keyphrase scoring is statistical: rare but important domain phrases can
  lose to frequent boilerplate, and synonyms are never merged.
* Inferred concepts and entity candidates are explicitly unverified; nothing in
  this phase decides that two strings mean the same concept.
* The default entity extractor is lexical (regex rules), not semantic; spaCy is
  opt-in and untested in CI because it needs a model download.
* Topic discovery refuses small or degenerate corpora instead of producing
  unstable clusters, so a two-document course reports `insufficient_data`.
* Section splitting depends on blank lines and heading-like blocks; PDF text with
  hard-wrapped lines yields coarser sections.
* Phase 3 mapping is lexical/semantic and can leave a question unmapped when the
  syllabus uses different terminology. Ambiguous candidates are flagged for
  review, never force-assigned. A lowercase "unit N" in wording needs supporting
  context; measurement phrases are not treated as syllabus references.
* Question-type and Bloom labels are transparent cue rules, not trained models;
  difficulty is uncalibrated and must not be presented as measured student
  difficulty.
* Similarity thresholds are defaults tuned for the synthetic corpus. Candidate
  retrieval is approximate, so a heavily paraphrased question can be missed, and
  similarity is never treated as proof of duplication except for `exact`.
* Phase 3 bundles English cues only. Semantic quality depends on whichever Phase 2
  embedding provider actually ran; the neural path remains opt-in and untested in
  CI.
* Phase 4 analytics are read-only and recalculated on demand; there is no cache or
  scheduler. Reports can be recomputed after questions, mappings or metadata
  change, and no derived analytics table can go stale.
* Trend periods come only from explicit `exam_session`/`exam_date` metadata or
  academic year. Upload and publication dates are never substituted. Missing
  periods are not interpolated, and low-sample changes are marked unreliable.
* Topic priority is a dataset-relative descriptive ranking, not a forecast. A
  topic absent from the available papers may still appear in unseen examinations.
* Unit/topic frequency can count one ambiguous question in several buckets; the
  report exposes assignment totals and multi-unit/topic counts instead of
  pretending the buckets sum to the question total. Concept frequency is a
  syllabus-topic proxy, not a semantic concept classifier.
* Marks analytics include only stated marks and report missing values; `total_marks`
  is not an inferred paper total. Sub-part/parent mark mismatches are flagged, not
  corrected.
* No labelled analytics evaluation set is bundled, so no accuracy, precision,
  recall, F1, significance or forecasting claim is made for Phase 4.
* The Phase 5 graph is relational adjacency storage, not a graph database. It has
  no native shortest-path, centrality or graph-query language; multi-hop traversal
  is capped at 10 and runs in Python.
* Phase 6 candidate metadata is JSON on the edge; evidence sources are not indexed,
  and there is no immutable review-history table. Explicit prerequisite sentences
  are not parsed from raw prose, and co-occurrence cannot determine direction.
* Learning paths order explicit/reviewed/verified dependencies by default. They are
  not shown to be pedagogically optimal, do not estimate study time, and rely on
  question coverage for topic targets. Candidate-only plans are intentionally
  marked `partial` and should not be presented as established facts.
* Concept identity merges only identical normalized labels within one course.
  Synonyms and abbreviations stay separate; duplicate candidates combine lexical
  label similarity with cosine of mean Phase 2 question embeddings, are
  review-only, and never merge automatically. Concepts are never merged across
  courses.
* Phase 6 prerequisite candidates remain `candidate` until reviewed. Syllabus
  order is weak evidence, co-occurrence/semantic relatedness cannot establish
  direction, and explicit prerequisites currently require syllabus metadata rather
  than prose parsing. No prerequisite accuracy benchmark is bundled; learning paths
  are dependency orderings, not proven pedagogy. Phase 7 coverage is a separate
  read-only analysis, not a prerequisite input or an accuracy claim.
* Graph evidence is stored on edges (`evidence`, document/page, method, model
  version). A separate `evidence` node type is reserved but not materialised.
* A human `reviewed`/`verified` status survives rebuilds and status transitions
  append to `metadata.review_history`, but there is no immutable review-history
  table; the current edge metadata is the audit record.
* Deleting a shared institution/subject node affects every course using it. Rebuild
  the affected courses after any source-level deletion. The shared
  `subject -> institution` edge is excluded from per-course edge counts and is
  pruned when no course references either endpoint.
* Phase 7 coverage is a read-only projection, not a persisted report or cache.
  Every run recomputes from current rows; identical inputs produce the same
  `report_id`, while `generated_at` changes. Rerun after document, mapping or graph
  changes rather than treating an old report as a snapshot.
* Coverage quality is inherited from Phase 3 mappings. Different terminology,
  paraphrases and short questions can remain unmapped or low-confidence even when
  an examiner intended the concept. The detector applies status/confidence filters
  but cannot validate meaning.
* `unmapped` is relative to the available corpus and never means a concept was
  never examined. A small corpus makes absence `uncertain`, and no coverage output
  is a forecast or a claim of educational importance.
* Confidence scores are not calibrated probabilities; `covered` means enough
  accepted evidence in this corpus, not certainty. Marks are counted once for unit
  totals but are not split between concepts, so concept mark totals can overlap.
* Multiple syllabus versions must be selected explicitly; questions that cannot be
  attributed to the selected version are excluded and counted. Course-level
  `syllabus_version` is not a per-document version label; use `syllabus_id`.
* Phase 5 graph aliases and explicit assessment edges are optional and only used
  when present. Candidate/inferred assessment edges are never promoted; disabling
  graph mappings is reported distinctly from a missing graph.
* Phase 8 Exam DNA is a read-only projection, not a persisted profile or cache.
  Every run recomputes from current rows; identical inputs produce the same
  `profile_id`, while `generated_at` changes. No table or migration is added.
* Exam DNA recurrence rates are historical descriptions with an explicit paper
  denominator. They are never probabilities, and a pattern seen in one paper is
  never described as cross-paper recurrence. Papers sharing a session are
  separate papers but not independent sittings.
* Exam DNA unit evidence is restricted to matches for the selected syllabus
  version. A paper mapped only to a different version is reported
  (`exam_dna_mixed_syllabus_versions`) rather than merged into the profile.
* Exam DNA session series and descriptive changes use only papers with an explicit
  session or examination date. A change needs at least three dated sessions and is
  a description of the observed papers, never a projection or probability.
* Duplicate paper copies are excluded from denominators because their full text
  fingerprint or identical question set shows they are the same paper. A
  re-typeset copy with different wording is not detected, and small papers are
  protected from being mistaken for copies.
* Question-type, Bloom and difficulty labels are Phase 3 estimates; low-confidence
  labels make the profile `partial`. Exact repetition proves identical wording,
  not identical expected answers; near/semantic/concept-related groups remain
  review candidates.
* Section and optional/compulsory patterns use stored section labels and explicit
  wording cues only. Ambiguous cues are reported, not interpreted. Marks are
  counted only when stated and are not split between units or concepts.
* No labelled Exam DNA benchmark is bundled. Synthetic fixture results validate
  software behavior only, not real-world examiner patterns or generalization.
* Phase 9 concept evolution is a read-only projection, not a persisted report. Every
  run recomputes from current rows; identical inputs produce the same `report_id`,
  while `generated_at` changes. No table or migration is added.
* Only one source date is selected per document, with its date type and field
  recorded. Ingestion time is never used, a bare academic year is not converted into
  an exact date, and conflicting or unparsable dates are reported rather than
  resolved silently.
* An examination session such as `2021-05` names a period, not a day. Such papers can
  order an assessment window but cannot support terminology or context chronology,
  which require exact dates. The Phase 4 academic-year fallback is deliberately not
  used to place assessment questions.
* "First observed" and "no longer observed" describe the analysed corpus only. A
  missing mention is never proof that a concept was removed, renamed or forgotten,
  and no inferred change renames, merges, splits or deletes a canonical concept.
* Context changes compare lexical excerpts with token Jaccard; the score is a review
  signal, not a semantic measure, and it will not recognize paraphrase or detect a
  genuine meaning change on its own.
* Syllabus comparisons depend on segmentation quality. Unknown version order,
  incomplete later-version extraction or low segmentation confidence produce an
  `insufficient_evidence` event rather than introduction/removal claims.
* Assessment-window shares use Phase 3 primary type/Bloom labels over analysed
  questions only, need at least two questions per window, and never measure
  educational importance. Marks are summed only when stated and are not split across
  concepts.
* Marks-value shares, unit counts and repetition counts inherit Phase 3 labels and
  Phase 8 groups; a repeated question is not a pedagogically desirable one, and the
  counts describe the analyzed corpus only.
* Relationship changes require a graph edge with a resolvable source-document date;
  edges without one are ignored, and a relationship no longer observed is not a
  relationship that no longer exists.
* No labelled concept-evolution benchmark is bundled. Synthetic fixtures validate
  software behavior only, not whether terms are truly synonymous or a change is
  educationally meaningful. Phase 13 personal material remains out of scope.
* Phase 10 questions are proposed practice material. They start in
  `pending_review`, carry their evidence and validation findings, and are never a
  claim of educational correctness or a prediction of real examination content.
* The deterministic provider has one construction per format and Bloom level, so
  repeated requests for a single concept produce the same wording: duplicates are
  detected and the batch is reported `partial` rather than padded. `--count` is a
  cap, not a guarantee, and breadth comes from more targets, formats or levels.
* Templates wrap quoted evidence in a task statement. They do not paraphrase,
  define terms or write model answers, so an open-ended item often has no answer
  key by design. `numerical` items are model-provider only, and multiple-choice
  distractors are other syllabus concepts from the same unit.
* The language-model provider is opt-in and its path is exercised with a fake
  transport, not a live endpoint. Its output is still validated and deduplicated,
  which does not make it correct. Content generation is English; other language
  tags fail with `unsupported_language`.
* Validation checks observable properties only. `needs_review` is the honest
  outcome for judgement-dependent findings and low-confidence Phase 3 labels, and
  no labelled benchmark of generated-question quality, difficulty calibration or
  Bloom correctness is bundled.
* Phase 11 mutation is textual and deterministic: rephrasing reframes the original
  task, scenario mutation adds a setting, and neither rewrites the content. Variants
  that change the answer (`numerical`, `difficulty`, `cognitive`, `related_concept`,
  `application`) drop the source key and stay `needs_review` until a reviewer writes
  one; numerical variants are not re-solved.
* Variant similarity is a review signal, not evidence of meaning. Only an identical
  normalised text is rejected as a duplicate — a variant is expected to resemble its
  source — and answer preservation is checked against the source key, never against
  an embedding score.
* A mutation never converts between formats: an open question is not rewritten as a
  multiple-choice item. The optional `--format` constraint must match the source, and a
  mismatched request is refused (`format_constraint_mismatch`) instead of producing a
  variant in a format the strategy cannot guarantee.
* Adversarial challenges have one construction per strategy, are English-only and
  always carry review flags: the system supplies a rubric and the quoted passage
  instead of a model answer, so `needs_review` is the designed steady state.
* Misconception detection is lexical rule-matching over answers to questions that
  already have a reference answer and a concept mapping. Paraphrase, diagrams and code
  answers are largely invisible; `unclear_or_unrelated` is never inferred, such answers
  are discarded with a reason. Support counts depend on the response identifiers
  supplied, so they are described as response counts, never as class-wide rates, and
  no record makes a claim about a student's ability.

## Roadmap position

Phases 1–11 are implemented: ingestion, text analysis, question understanding,
read-only question-paper analytics, a persistent evidence-bearing knowledge graph,
prerequisite discovery with explainable learning paths, an evidence-backed
coverage-gap detector, a historical Exam DNA pattern profile, a read-only concept
evolution tracker, grounded automatic question generation with named validation
checks, deduplication and human review, and advanced assessment intelligence:
question mutation with declared invariants, fair adversarial questions with rubrics,
and evidence-bounded misconception mining with review. The design stays extensible
for the remaining phases: adapters, OCR, embedding/entity providers, classifiers and
graph storage backends are replaceable; segmentation and topic models are pure
functions of their inputs; provenance and per-record uncertainty are first-class; and
course isolation is enforced at the schema level. Later phases consume questions,
Phase 2 NLP rows, the Phase 3 `question_analysis`, mapping, type, Bloom, difficulty
and similarity tables, Phase 4 reports, Phase 5–6 graph nodes/edges, Phase 7 coverage
reports, Phase 8 Exam DNA profiles, Phase 9 evolution reports, Phase 10 generated
questions and Phase 11 variants, challenge items and misconception records instead of
re-parsing raw files. The next recommended phase is Phase 12 (full integration,
dashboard and final evaluation); Phase 11 does not implement answer grading,
forecasting, automatic approval or dashboard integration.

**Phase 13 — Personal Data Integration & Validation** will reuse the same manifest,
provenance and pipeline path with `dataset_type=permissioned`; no personal material
is bundled or assumed here.
