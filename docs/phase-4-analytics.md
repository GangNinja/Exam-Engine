# Phase 4 — Question-paper analytics

Phase 4 turns the questions, syllabus mappings, classifications and similarities
stored by Phases 1–3 into explicit, reusable reports. It does not add a dashboard,
a knowledge graph, coverage-gap detection, Exam DNA, question generation or any
other later-phase feature.

The engine is deliberately **read-only**: every report is recalculated from the
current rows in the database. There is no analytics cache and no new derived
table that can go stale. If a question, mapping, classification or provenance
record changes, the next report reflects that change.

**In scope:** question/document/session frequency, exact and similar repetition,
unit/topic/concept distributions, marks analysis, estimated difficulty and
question-type summaries, observed-period trends, historical topic prominence,
data-quality reporting, filtering, CLI and tests.

**Out of scope:** a question-paper dashboard, knowledge graph, prerequisites,
coverage-gap reporting, Exam DNA, concept evolution, question generation,
misconception mining, answer validation and final application integration. The
user's own syllabus and paper data remain reserved for Phase 13; all committed
fixtures are synthetic.

---

## 1. Data flow

```
Phase 1 courses/questions/documents ─────┐
Phase 2 embeddings (already used by P3) ─┤
Phase 3 analysis/mapping/type/Bloom ─────┼──► AnalyticsService ──► AnalyticsReport
Phase 3 similarity pairs ────────────────┤         │
explicit exam metadata ──────────────────┘         ├── CLI JSON/plain reports
                                                    └── later interfaces
```

`AnalyticsService.analyze_course` opens a read session, loads the course, its
documents/questions and its Phase 3 derived rows, resolves examination periods,
applies one filter set and builds a Pydantic `AnalyticsReport`. No ingestion or
classifier code is duplicated.

All course filters define a **syllabus/grading boundary**. A request for several
courses returns a list of separate reports; the engine never adds their
questions into one cross-syllabus total.

## 2. Examination periods

Temporal reports use only explicit metadata describing the examination:

| Metadata key | Used as |
|---|---|
| `exam_session` / `examination_session` | session label (for example `2024-05`) |
| `exam_date` / `examination_date` | ISO examination date |
| `exam_type` / `examination_type` | midterm/final/other label |
| document `academic_year` | coarse fallback period |
| course `academic_year` | coarse fallback when the document has none |

`ingested_at` is **never** an examination date. `publication_date` is also not
used as one: a paper can be published after the examination, and a syllabus can
be published before it. If none of the keys above is available, the document is
counted in `questions_without_period` and excluded from trends; it is not given
an invented date.

A period's `session` key includes the examination type, so a midterm and a final
in the same month remain distinct sessions. Only an explicitly labelled
`exam_session` has a session key: an academic-year period, or an `exam_date`
period without a session label, is still a usable trend period but is **not**
counted as an examination session. Trend periods group by the resolved metadata
source and period label; multiple exam types inside one period are listed and
produce a `mixed_exam_types` warning. No missing period is imputed.

## 3. Metric definitions and denominators

Every report carries `data_quality` as the denominator/coverage record.

| Metric | Definition |
|---|---|
| `questions` | Stored question **appearances** after filters. Repeated appearances are not silently deleted. |
| `distinct_question_texts` | Unique question texts after the exact-repetition normalisation (case/whitespace, leading question marker and trailing marks annotation). |
| `documents` | Selected source documents, including documents that contain no questions. |
| `documents_with_questions` | Documents that contribute at least one filtered question. |
| `question_papers` / `question_banks` | Selected documents by their Phase 1 kind. |
| `known_sessions` | Distinct explicitly labelled examination sessions, including exam type. Academic-year and date-only periods are not sessions. |
| `known_periods` | Distinct resolved period keys (metadata source + label), whether or not a session label exists. |
| `analysis_coverage` | Questions with Phase 3 analysis / `questions`. |
| `mapping_coverage` | Questions with any selected syllabus unit / `questions`; ambiguous mappings count as covered. |
| `marks_coverage` | Questions with a stated mark / `questions`. |

### Repetition

`exact` means normalised question text is identical. Normalisation ignores case,
whitespace, a leading question marker, and a trailing marks annotation; the
original text and marks remain on every appearance. The report groups those
appearances and shows:

* question, document and known-session counts;
* whether the group is within one document, across documents, and across
  sessions;
* every question reference with document title, source id, page/locator, marks,
  period and the explicit session label when one exists.

`data_quality.exact_duplicate_appearances` counts every appearance in those
groups, while `repetition.exact_repeated_appearances` counts only the additional
appearances beyond the first in each group.

Near/semantic/concept pairs come from Phase 3 `question_similarity`. They retain
the stored lexical/semantic scores, method and evidence, and mark whether the
pair is within one document or session. `similarity_coverage` summarizes each
kind with pair, distinct-question, document and session counts plus within/across
document and same/different/unknown-session pair counts. It does not cluster
pairs or assert that they are duplicates. Only `exact` establishes identical
wording; a semantic pair is review evidence, not proof of duplication.

`similarity_threshold` is configurable. Exact groups remain in the report at any
threshold. Stored pairs below a requested threshold are omitted from the
repetition list, but the underlying Phase 3 rows are not modified.

### Mapping, units, topics and concepts

Mapping uses only the candidates Phase 3 actually selected:

* `mapped`: the rank-1 selected unit;
* `ambiguous`: every selected candidate is retained;
* `unmapped`: no assignment;
* `not analysed`: no Phase 3 row.

Unit and topic frequency counts a question once per selected unit/topic bucket.
A question can therefore appear in several buckets, so bucket counts may exceed
the question total. The report exposes `unit_assignments`,
`multi_unit_questions`, `topic_assignments`, `multi_topic_questions`,
`questions_with_any_unit`, and the unmapped/ambiguous references. Unit, topic and
concept rows also carry the sorted source `document_ids`, so each aggregate has
source-level evidence. The overall question total is never inflated by summing
bucket counts.

`concept` rows are a documented **topic-level proxy**: Phase 3 has no separate
question-to-concept classifier, so the report deduplicates syllabus topic labels
across units. This is not a Phase 2 inferred concept and is not a validated
semantic concept.

### Marks

Only explicitly stated marks are used. Missing marks stay missing and are
reported; they are never set to zero.

* `question_marks` gives marks per question with its primary unit/topic/type and
  difficulty.
* `common_allocations` counts exact stated values; `marks_sources` counts only
  questions with stated marks.
* `by_section`, `by_unit`, `by_topic`, `by_question_type` and `by_difficulty`
  show counts, coverage, totals, means and shares.
* Unit/topic/type/difficulty mark breakdowns use the **primary** assignment or
  primary label, to avoid double-counting a multi-assignment question.
* `subpart_mismatches` flags a question whose stated sub-part marks do not add up
  to its stated parent marks. The engine records the discrepancy; it does not
  rewrite either value.
* `anomalies` exposes Phase 1 marks doubts such as `multiple_marks_values` and
  `invalid_marks_value`, independently of whether Phase 3 analysed the question.

`total_marks` is the sum of marks stated in the selected dataset, not an inferred
full-paper total.

### Question types and difficulty

Primary distributions count each question once. Multi-label `all_labels` counts
are reported separately and may exceed the question total. `unclassified`
includes questions with no primary type; `not_analysed` identifies questions
without Phase 3 analysis.

Difficulty is always the Phase 3 **estimate**. `estimated=True` and
`empirical_measurement_available=False` are explicit. The report never presents
marks, wording or model output as measured student difficulty. Unknown and
low-confidence counts are reported.

### Trends

Observed periods are sorted by their resolved sort key. For each period the
report provides question/document/session counts, the contributing
`document_ids`, marks coverage, and counts by primary unit, topic, question type
and difficulty. Changes between adjacent
periods are descriptive; each change carries the two sample sizes and a
`reliable` flag based on `min_period_questions`. Marks changes use the number of
questions with stated marks as their sample, not the overall question count.

New topics are those first observed after the initial period. Periods are not
interpolated, and a topic absent from the dataset is not described as never
examined. Status values:

* `ok` — at least two periods and every period meets the sample threshold;
* `low_sample` — at least two periods, but one or more is small;
* `insufficient_data` — fewer than two periods;
* `no_period_metadata` — no usable examination period.

### Historical topic priority

Topic priority is a transparent descriptive ranking, not a prediction. Factors
are normalized to 0–1:

| Factor | Calculation |
|---|---|
| `frequency` | distinct question papers containing the topic; if the dataset has no question papers at all, distinct documents. Divided by the maximum topic value. |
| `recency` | position of the topic's latest observed period among comparable periods, 0 (oldest) to 1 (newest); omitted unless at least two periods are available and every observed period uses one metadata source. Academic-year periods count when no finer date exists. |
| `marks` | mean stated marks for the topic, divided by the maximum topic mean; omitted if no marks exist or every stated mark is zero |
| `coverage` | distinct known examination sessions containing the topic / total known sessions; omitted if no sessions exist |
| `confidence` | mean Phase 3 mapping score for the topic's selected matches |

Default weights are frequency 0.30, recency 0.20, marks 0.20, coverage 0.20 and
mapping confidence 0.10. Weights are renormalised over available factors, so a
missing date or marks value does not contribute an invented zero. Every row
shows the factor values, effective weights, raw question/paper/session counts,
source `document_ids`, latest period and an explanation. The ranking describes
what is **historically prominent in the available dataset**; it is not an exam
forecast.

## 4. Filters

The CLI accepts:

| Filter | Scope |
|---|---|
| `--course` (repeatable) | Course; multiple courses produce separate reports |
| `--institution`, `--syllabus-version` | Course selection |
| `--source`, `--document` | Dataset source / document |
| `--unit`, `--topic` | Selected Phase 3 mapping bucket |
| `--exam-type`, `--period` | Explicit examination metadata |
| `--type` | Primary question type |
| `--difficulty` | Estimated primary difficulty (including `unknown`) |
| `--marks-min`, `--marks-max` | Stated marks; missing marks are excluded by a marks filter |
| `--similarity-threshold` | Report-level minimum stored similarity score |

Unit/topic filters use selected assignments, not runner-up evidence. A question
type filter uses the primary label; multi-label evidence is retained in the
report's `all_labels`. A marks-range filter excludes missing marks rather than
inferring a value.

Cross-course reporting is explicit: `analytics report --course A --course B`
prints a warning and returns two reports. There are no pooled totals across
different syllabi or grading schemes.

## 5. CLI and outputs

```bash
exam-engine analytics report [--course ID ...] [filters] [--json]
exam-engine analytics repetitions --course ID [--kind exact|near|semantic|concept_related] [--threshold F] [--limit N] [--json]
exam-engine analytics topics --course ID [--limit N] [--json]
exam-engine analytics trends --course ID [--json]
exam-engine analytics status [--json]
```

`report` emits the complete structured report; `--json` is the machine-readable
form used by later phases. `repetitions`, `topics` and `trends` are focused
inspectors. `status` shows whether questions have Phase 3 analysis before an
analytics report is interpreted.

Every report contains:

* course scope, applied filters, settings and generation time;
* data quality, coverage, denominators and sample-size flags;
* mapping unit/topic/concept tables and unmapped/ambiguous references;
* marks summary, per-question marks and breakdowns;
* primary/all question types and estimated difficulty;
* exact repeated groups, similar-pair coverage and document frequency;
* observed periods, changes and newly observed topics;
* topic priority factors and ranking;
* warnings and limitations.

## 6. Synthetic demonstration

The committed fixture is
[`datasets/analytics_sample/`](../datasets/analytics_sample): one synthetic course,
one syllabus and three papers with explicit `exam_session`, `exam_date` and
`exam_type`. It is not official university history.

```bash
python -m exam_engine init-db
python -m exam_engine import datasets/analytics_sample/manifest.json
python -m exam_engine understanding analyze
python -m exam_engine analytics report --course demo-analytics-cs201-2024-v1
python -m exam_engine analytics repetitions --course demo-analytics-cs201-2024-v1 --kind exact
python -m exam_engine analytics trends --course demo-analytics-cs201-2024-v1
```

Current deterministic result on that fixture:

```text
questions=18 distinct=14 documents=4 papers=3 sessions=3 periods=3
mapping: mapped=16 ambiguous=2 unmapped=0
marks: stated=18/18 total=124
repetition: exact_groups=4 exact_in_groups=8 exact_extra=4 near=2 concept_related=12
trends: ok periods=3 range=2024-05..2025-04
```

Those numbers validate the calculations against synthetic expected values. They
are not precision/recall/F1 or a claim about real examination behaviour.

## 7. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_Q4_MAX_QUESTIONS` | `20000` | Question limit per report |
| `EXAM_ENGINE_Q4_SMALL_SAMPLE` | `5` | Below this, the report marks a small sample |
| `EXAM_ENGINE_Q4_LOW_CONFIDENCE` | `0.5` | Type/difficulty/mapping confidence threshold |
| `EXAM_ENGINE_Q4_MIN_TREND_PERIODS` | `2` | Minimum observed periods for a comparison |
| `EXAM_ENGINE_Q4_MIN_PERIOD_QUESTIONS` | `3` | Minimum questions for a reliable period change |
| `EXAM_ENGINE_Q4_SIMILARITY_THRESHOLD` | `0.0` | Default report-level similarity cutoff |
| `EXAM_ENGINE_Q4_PRIORITY_FREQUENCY` | `0.30` | Topic-priority frequency weight |
| `EXAM_ENGINE_Q4_PRIORITY_RECENCY` | `0.20` | Recency weight |
| `EXAM_ENGINE_Q4_PRIORITY_MARKS` | `0.20` | Marks weight |
| `EXAM_ENGINE_Q4_PRIORITY_COVERAGE` | `0.20` | Session-coverage weight |
| `EXAM_ENGINE_Q4_PRIORITY_CONFIDENCE` | `0.10` | Mapping-confidence weight |

Priority weights need not sum to one; available factors are renormalised. A
weight of `0` disables that factor when enough other factors remain. Invalid
values fall back to defaults, matching the rest of the settings layer.

## 8. Verification and limitations

Run:

```bash
python -m pytest -q
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m mypy
```

At Phase 4 completion the suite had 309 tests; all passed (309 passed, 0 failed).
The current repository-wide suite is larger because Phase 5 added graph tests.
Analytics tests cover:

* exact repetition within one document and across documents/sessions, with source references;
* near/semantic/concept evidence, source coverage counts and configurable thresholds;
* Phase 1 marks anomalies without Phase 3 analysis;
* unit/topic/concept distributions, multiple assignments and unmapped questions;
* marks totals, distributions, missing values and subpart mismatches;
* difficulty/type coverage, low confidence and estimated-vs-empirical labelling;
* observed periods, insufficient periods, low samples, mixed exam types and
  newly observed topics;
* marks-trend reliability using the marked-question sample;
* topic-priority factors, documented formula and renormalised weights, including
  recency from coarse academic-year periods, document-frequency fallback for a
  question-bank-only view and explicit all-zero-weight handling;
* filters, direct course-scope enforcement, course isolation and separate
  multi-course reports;
* repeated document text, empty datasets, missing metadata, no Phase 3 analysis,
  deterministic reports and recalculation after a question change;
* CLI JSON/plain output and clean errors.

Known limitations:

* No labelled analytics benchmark exists. The fixture proves calculation
  behaviour, not historical or predictive validity.
* Mapping, type and difficulty inherit Phase 3's limitations. Ambiguous mappings
  can contribute to multiple unit/topic buckets; the report exposes this rather
  than hiding it.
* Topic trends use the primary mapping only; alternative ambiguous candidates
  are not trended.
* Concept frequency is a topic-label proxy, not a semantic concept classifier.
* Marks totals include only stated marks and can be incomplete.
* Periods rely on explicit metadata or academic year. Publication/upload dates
  are intentionally not substituted.
* Trends are descriptive with sample-size warnings; they are not significance
  tests and do not forecast future papers.
* Topic priority is dataset-relative. A topic absent from the available papers
  is not shown as absent from all examinations.
* There is no UI or scheduler; reports are computed on demand through the
  service/CLI. The Phase 13 personal-data boundary remains intact.
