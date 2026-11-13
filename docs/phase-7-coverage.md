# Phase 7 — Exam Coverage Gap Detector

Phase 7 compares the concepts parsed from one course syllabus with the questions
available for that same course and syllabus version. It reports what the available
corpus shows, which concepts have little or no confidently mapped assessment
evidence, and which questions remain unresolved.

It is deliberately **descriptive, not predictive**:

* an `unmapped` concept is absent from the analyzed questions, not proven never to
  have been examined;
* coverage frequency is not a statement that a concept is educationally important;
* the detector does not forecast the next paper or infer examiner intent.

Phase 7 does **not** implement Exam DNA, concept evolution, automatic question
generation, misconception mining, a dashboard, or personal educational data. Those
remain later phases.

## Architecture and storage decision

The implementation adds two modules over existing Phase 1–6 data:

| Module | Responsibility |
|---|---|
| [`src/exam_engine/models/coverage.py`](../src/exam_engine/models/coverage.py) | Validated request, finding, unit, unmapped-question and report schemas |
| [`src/exam_engine/coverage/service.py`](../src/exam_engine/coverage/service.py) | Scope resolution, mapping reuse, metrics, explanations and report assembly |
| [`src/exam_engine/cli_coverage.py`](../src/exam_engine/cli_coverage.py) | `coverage report/concepts/concept/units/unmapped` commands |

Phase 7 adds **no database table and no migration**. It is a read-only projection
over:

* Phase 1 `syllabi`, `syllabus_units`, `documents`, `questions` and source
  provenance;
* Phase 3 `question_analysis` and `question_unit_matches` (the persisted mapping
  candidates and their evidence);
* Phase 5 concept identity, aliases and `ASSESSES` edges through `GraphService`.

Reports are deterministic derived state. Re-running an analysis creates no rows,
does not update mappings, and does not touch source documents or the Phase 5 graph.
If a report file is wanted, `coverage report --output PATH` serializes the same
report as UTF-8 JSON. The report id is a content digest of the course, selected
syllabus, configuration, filters, concepts, question corpus signatures, accepted
mapping evidence and findings; `generated_at` is the only intentionally
nondeterministic field.

The chosen abstraction keeps Phase 7 replaceable: the service depends on existing
repositories and `GraphService`, not on new SQL or a second graph.

## Analysis scope and multi-course integrity

A request always names exactly one course. The selected syllabus version is:

1. the explicit `syllabus_id`, if supplied and owned by that course;
2. the only parsed syllabus, if the course has one;
3. otherwise a structured `ambiguous_syllabus` error listing the valid ids.

There is no automatic merge of syllabus versions. Concepts are identified by
`(course_id, syllabus_id, normalized_topic)`, so identical labels in different
courses or versions are different concepts. Questions are read only through the
course repository. Accepted evidence is restricted to the selected syllabus (or a
course-wide Phase 5 edge only when the course has a single syllabus).

When more than one syllabus exists, questions that cannot be attributed to the
selected version are excluded from the denominator and counted in
`data_quality.unscoped_questions_excluded`. They are not silently treated as
evidence of absence.

Optional filters are validated before analysis:

| Filter | Effect |
|---|---|
| `document_ids` | Only questions from those course documents |
| `source_ids` | Only questions from those dataset sources |
| `unit_ids` | Only concepts/units under review; question denominator otherwise unchanged |
| `question_types` | Only questions whose Phase 3 primary type matches |
| `exam_periods` | Only documents whose resolved period label matches exactly |

Unknown document, source, unit or period labels raise
`invalid_coverage_filter` with the unknown values in `detail`. A filter never
widens scope to another course or syllabus.

A unit filter restricts the syllabus concepts under review but deliberately keeps
the whole selected question corpus as the denominator, so a question mapped only to
another unit appears unresolved in report-level mapped/unmapped counts. That is
not evidence about the selected unit; the report adds a limitation explaining it.

## Coverage statuses

| Status | Rule | Meaning |
|---|---|---|
| `covered` | At least `covered_min_questions` distinct questions have accepted mappings and the average accepted score is at least `low_confidence_threshold`; or one accepted mapping meets `single_mapping_confidence_threshold` | Confident representation in the available corpus |
| `low_coverage` | At least one accepted mapping, but fewer than the covered minimum and below the single-mapping confidence threshold | Observed, but limited, representation |
| `unmapped` | No accepted mapping, enough analysed questions and analysis coverage | Not represented by confidently mapped questions here |
| `uncertain` | Candidate mappings exist but none accepted; or accepted mappings average below the confidence threshold; or the corpus/analysis coverage is too limited to conclude absence | Evidence insufficient for a confident class |
| `insufficient_data` | No usable questions, no Phase 3 analysis, or no usable syllabus concepts | The analysis cannot be performed reliably |

The low-coverage threshold is `covered_min_questions` (default 2). A single
accepted mapping is promoted to covered only when its average score reaches
`single_mapping_confidence_threshold` (default 0.19); otherwise it remains low
coverage. A count below the threshold is not proof that a concept is unimportant.
`unmapped` is not a synonym for "never examined" or "out of syllabus".

Overall report status:

| Status | Meaning |
|---|---|
| `ok` | No small corpus, truncation, unattributed questions, low analysis coverage or uncertain concept |
| `partial` | At least one reliability condition holds; findings remain usable with the listed caveats |
| `insufficient_data` | No concept findings, no selected questions, or no Phase 3 analysis at all |

## Mapping reuse and acceptance rules

Phase 7 does not invent a new semantic matcher. It consumes persisted Phase 3 work
and explicit Phase 5 assessment edges.

### Phase 3 primary mappings

A stored `UnitMatch` is accepted only when all of the following hold:

1. its `syllabus_id` is the selected syllabus;
2. its normalized `topic` equals a selected syllabus topic;
3. its unit belongs to that concept (a topic shared by units may match any of them);
4. the parent question analysis exists and is `mapped`;
5. `rank == 1` (a secondary candidate is retained as review evidence);
6. `score >= mapping_confidence_floor` (default `0.18`, the Phase 3 acceptance
   threshold);
7. a topic is present (a unit-only match cannot establish concept coverage).

Rejected matches remain in `candidate_mappings` with a reason such as
`ambiguous_mapping`, `secondary_mapping`, `below_confidence_floor` or
`unit_only_mapping`. They never contribute to accepted counts, marks, documents or
sessions.

### Phase 5 explicit assessment edges

`ASSESSES` edges with status `explicit`, `reviewed` or `verified` may add or
strengthen an accepted mapping. `candidate` and `inferred` edges are excluded by
default and never promoted by this phase. An explicit edge is scoped to the
selected syllabus; an unscoped edge is accepted only when the course has one
syllabus. The graph's concept aliases can resolve a syllabus topic and an explicit
edge target only when the graph explicitly carries that alias; aliases are never
invented by Phase 7.

Setting `EXAM_ENGINE_Q7_GRAPH_EXPLICIT_MAPPINGS=false` disables graph reads and is
reported as `coverage_graph_disabled`, distinct from a truly missing graph
(`coverage_graph_unavailable`).

### Evidence retained

Each accepted mapping keeps the Phase 3 match id, method, score, matched terms and
evidence lines, plus any Phase 5 edge id/status/evidence. Each finding keeps the
syllabus document and page, unit id/label, question id, question number, source
document id/title/kind, source dataset id, page or row locator, marks, excerpt,
primary question type, period label/source and session when available.

## Metric definitions

All counts are over the **selected** corpus after scope, filters and the
`max_questions` cap. A question id is counted once per metric even if several
mappings point to it.

| Metric | Formula / denominator |
|---|---|
| `total_concepts` | Number of selected syllabus topics after the unit filter |
| `covered_concepts` | Concepts with status `covered` |
| `low_coverage_concepts` | Concepts with status `low_coverage` |
| `unmapped_concepts` | Concepts with status `unmapped` |
| `uncertain_concepts` | Concepts with status `uncertain` |
| `insufficient_data_concepts` | Concepts with status `insufficient_data` |
| `concept_coverage_rate` | `covered_concepts / total_concepts`; `None` when no concepts |
| `total_questions` | Distinct selected question ids |
| `analysed_questions` | Selected questions with a Phase 3 analysis |
| `analysis_coverage_rate` | `analysed_questions / total_questions`; `None` when empty |
| `mapped_questions` | Selected questions with at least one accepted concept mapping |
| `unmapped_questions` | `total_questions - mapped_questions`; includes ambiguous and not-analysed unresolved questions |
| `mapped_question_rate` | `mapped_questions / total_questions`; `None` when empty |
| `ambiguous_questions` | Questions whose Phase 3 mapping status is `ambiguous` |
| `known_sessions` | Distinct explicit `exam_session` identities; academic years are periods, not sessions |
| `total_known_marks` | Sum of stated marks over selected questions; only known values |
| `mapped_marks` | Sum of stated marks for distinct selected questions with an accepted mapping; each question once |
| `questions_with_marks` | Selected questions with a stated mark |
| `missing_marks` | `total_questions - questions_with_marks` |
| `mapped_marks_share` | `mapped_marks / total_known_marks`; `None` when no marks |
| `shared_concept_units` | Concepts present in more than one selected unit |

Concept-level fields:

* `distinct_question_count` — distinct accepted question ids.
* `document_count` — distinct source document ids of accepted questions.
* `session_count` — distinct explicit session keys of accepted questions.
* `mapped_marks` — full stated marks of each accepted question; marks are **not
  split** between the concepts a question supports.
* `questions_with_marks` — accepted questions with known marks.
* `question_type_counts` / `unknown_question_type_count` — primary Phase 3 types.
* `scored_mapping_count`, `average_mapping_confidence`,
  `minimum_mapping_confidence`, `maximum_mapping_confidence` — one score per
  accepted question/concept pair (the strongest evidence score for that pair);
  scores are confidence estimates, not calibrated probabilities.
* `supporting_questions`, `candidate_mappings`, `reasons`, `warnings`,
  `limitations`.

Unit-level fields:

* `concept_count`, `status_counts`, `concept_ids`;
* `distinct_question_count` — distinct questions mapped to a concept in the unit;
* `mapped_marks` — each distinct mapped question's marks counted **once per unit**,
  even if it supports several concepts in that unit;
* `questions_with_marks`;
* `concept_coverage_rate = covered / concept_count` (strictly `covered`, not
  `low_coverage`);
* `assessed_concept_rate = (covered + low_coverage) / concept_count`;
* `question_share = unit distinct mapped questions / selected corpus questions`;
* `mapped_marks_share = unit distinct mapped marks / total known marks`;
* `needs_attention` — labels of non-covered concepts;
* `warnings` — data-completeness notes such as no parsed topics, no mapped
  questions, missing marks or missing session metadata.

Unit totals can overlap when a concept occurs in several units, so they are not
additive. The report exposes `shared_concept_units` and warnings instead of
pretending the unit rows sum to the report total.

Percentages with an invalid or zero denominator are `None`. `concept_coverage_rate`
uses the concept denominator, which is valid even when the question corpus is
empty; that is reported separately by `total_questions == 0` and the
`insufficient_data` report status.

## Report schema and example

The Pydantic `CoverageReport` is serialized with
`schema_version = "phase7-v1"`, `algorithm = "phase7_coverage_gap_detector"` and
`algorithm_version = "phase7.v1"`. Top-level keys:

```text
schema_version, report_id, algorithm, algorithm_version,
course, syllabus, generated_at, status, filters, parameters,
summary, concepts, units, unmapped_questions, data_quality,
warnings, limitations
```

Abbreviated example (values from a deterministic synthetic test fixture):

```json
{
  "schema_version": "phase7-v1",
  "report_id": "cov_...",
  "algorithm": "phase7_coverage_gap_detector",
  "algorithm_version": "phase7.v1",
  "course": {
    "course_id": "phase7-course-v1",
    "course_code": "CS701",
    "title": "Phase 7 synthetic coverage fixture",
    "academic_year": "2024-25",
    "syllabus_version": "v1"
  },
  "syllabus": {
    "syllabus_id": "syl_...",
    "document_id": "doc_syllabus_...",
    "unit_count": 2,
    "concept_count": 4,
    "confidence": 0.9,
    "selected_automatically": true
  },
  "generated_at": "2026-10-29T12:00:00Z",
  "status": "ok",
  "filters": {},
  "parameters": {
    "covered_min_questions": 2,
    "low_confidence_threshold": 0.18,
    "single_mapping_confidence_threshold": 0.19,
    "mapping_confidence_floor": 0.18,
    "small_corpus_questions": 5,
    "min_analysis_coverage": 0.8,
    "include_graph_explicit_mappings": true,
    "selected_syllabus_id": "syl_...",
    "course_syllabus_count": 1,
    "mapping_policy": "phase3_primary_mapping+phase5_explicit_assessment"
  },
  "summary": {
    "total_concepts": 4,
    "covered_concepts": 1,
    "low_coverage_concepts": 0,
    "unmapped_concepts": 3,
    "uncertain_concepts": 0,
    "total_questions": 5,
    "analysed_questions": 5,
    "mapped_questions": 2,
    "unmapped_questions": 3,
    "analysis_coverage_rate": 1.0,
    "mapped_question_rate": 0.4,
    "concept_coverage_rate": 0.25,
    "known_sessions": 1,
    "total_known_marks": 20.0,
    "mapped_marks": 10.0,
    "questions_with_marks": 4,
    "missing_marks": 1,
    "mapped_marks_share": 0.5
  },
  "concepts": [
    {
      "concept_id": "syc_...",
      "label": "Arrays",
      "unit_labels": ["Unit 1"],
      "status": "covered",
      "distinct_question_count": 2,
      "document_count": 1,
      "session_count": 1,
      "mapped_marks": 10.0,
      "average_mapping_confidence": 0.8,
      "reasons": [
        "2 distinct question(s) have accepted mappings; the configured covered minimum is 2.",
        "Average accepted mapping confidence is 0.800."
      ],
      "supporting_questions": [
        {
          "question_id": "q1",
          "document_id": "doc_paper_2024",
          "document_title": "SYNTHETIC question paper",
          "mapping_sources": ["phase3_primary_mapping"],
          "mapping_method": "tfidf",
          "mapping_score": 0.9,
          "analysis_status": "mapped",
          "evidence": ["phase3_unit_match:qum_...", "..."],
          "exam_period": "2024-05",
          "exam_session": "2024-05"
        }
      ],
      "candidate_mappings": [],
      "limitations": ["A concept absent from the available corpus ..."]
    }
  ],
  "units": [
    {
      "unit_id": "unit_...",
      "unit_label": "Unit 1",
      "concept_count": 2,
      "status_counts": {"covered": 1, "unmapped": 1, "uncertain": 0, "low_coverage": 0, "insufficient_data": 0},
      "distinct_question_count": 2,
      "mapped_marks": 10.0,
      "concept_coverage_rate": 0.5,
      "assessed_concept_rate": 0.5,
      "question_share": 0.4,
      "mapped_marks_share": 0.5,
      "needs_attention": ["Linked Lists"],
      "warnings": []
    }
  ],
  "unmapped_questions": [
    {
      "question_id": "q5",
      "document_id": "doc_paper_2024",
      "document_title": "SYNTHETIC question paper",
      "analysis_status": "unmapped",
      "reason": "no_mapping_above_threshold",
      "explanation": "no syllabus unit met the configured mapping threshold (0.18)",
      "existing_mappings": []
    }
  ],
  "data_quality": {
    "documents": 1,
    "document_ids": ["doc_paper_2024"],
    "source_ids": ["phase7-fixture"],
    "questions": 5,
    "analysed_questions": 5,
    "mapped_questions": 2,
    "ambiguous_questions": 0,
    "unmapped_questions": 3,
    "questions_with_marks": 4,
    "missing_marks": 1,
    "known_sessions": 1,
    "questions_with_session": 5,
    "graph_assessment_edges": 0,
    "truncated": false,
    "small_corpus": false
  },
  "warnings": [
    {"code": "coverage_missing_marks", "severity": "info", "message": "..."}
  ],
  "limitations": ["Coverage describes the available question corpus only. ..."]
}
```

An unmapped question is explicitly **not** labelled out of syllabus. Its `reason`
distinguishes `no_mapping_above_threshold`, `ambiguous_mapping`, `unit_only_mapping`,
`other_syllabus_only`, `not_analysed` and `no_accepted_mapping`, and retains any
existing mappings with their scores and failure reasons.

## CLI

The CLI is a thin wrapper over `CoverageService`.

```bash
# One course with one syllabus
python -m exam_engine coverage report --course demo-cs201-2024-v1

# Explicit syllabus version, JSON output to a file
python -m exam_engine coverage report \
  --course demo-cs201-2024-v1 \
  --syllabus syl_... \
  --json --output reports/cs201-coverage.json

# Focused views
python -m exam_engine coverage concepts --course demo-cs201-2024-v1
python -m exam_engine coverage concept --course demo-cs201-2024-v1 \
  --concept "Binary search trees and balancing"
python -m exam_engine coverage units --course demo-cs201-2024-v1
python -m exam_engine coverage unmapped --course demo-cs201-2024-v1

# Narrow the review set; all filters are echoed in the report
python -m exam_engine coverage report --course demo-cs201-2024-v1 \
  --document doc_... --unit unit_... --exam-period 2024-05 \
  --question-type explanation
```

`--json` writes UTF-8 machine-readable JSON. A JSON failure is
`{"error": {"code": ..., "message": ..., "detail": ...}}` with exit code 1.
Human-readable failures print `ERROR: [code] message` and a compact `detail:`
line. `coverage report --output PATH` creates parent directories and writes the
same UTF-8 JSON. `--limit` caps the rows returned by `concepts` and `unmapped`
in both human and JSON modes.

Error codes: `unknown_course`, `no_syllabus`, `unknown_syllabus`,
`ambiguous_syllabus`, `invalid_coverage_filter`, `coverage_export_failed`.
Concept/unit lookup by unknown id prints `not_found`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_Q7_MAX_QUESTIONS` | `20000` | Maximum selected questions; truncation is reported |
| `EXAM_ENGINE_Q7_COVERED_MIN_QUESTIONS` | `2` | Distinct accepted questions required for `covered` |
| `EXAM_ENGINE_Q7_LOW_CONFIDENCE` | `0.18` | Average accepted score below this makes coverage `uncertain` |
| `EXAM_ENGINE_Q7_SINGLE_MAPPING_CONFIDENCE` | `0.19` | Score required for one accepted mapping to count as `covered` |
| `EXAM_ENGINE_Q7_MAPPING_CONFIDENCE_FLOOR` | `0.18` | Minimum persisted Phase 3 score accepted as mapping evidence |
| `EXAM_ENGINE_Q7_SMALL_CORPUS` | `5` | Corpus below this cannot support absence claims |
| `EXAM_ENGINE_Q7_MIN_ANALYSIS_COVERAGE` | `0.8` | Minimum fraction of questions with Phase 3 analysis for a confident negative |
| `EXAM_ENGINE_Q7_GRAPH_EXPLICIT_MAPPINGS` | `true` | Read Phase 5 concept aliases and explicit assessment edges |

All values are also recorded in `report.parameters`; invalid environment values
fall back to defaults. `Settings.with_coverage(...)` provides the same knobs
programmatically.

## Determinism and reproducibility

For identical database content and configuration, everything except
`generated_at` is stable: concepts are sorted by normalized label, supporting
questions by question/document id, units follow syllabus order, and the report id
digests the accepted mapping evidence. Rerunning analysis has no write side
effects. Reports do not claim to be immutable historical snapshots; rerun after
documents, questions, mappings or graph reviews change.

## Testing

```bash
python -m pytest tests/test_coverage.py tests/test_coverage_cli.py tests/test_config.py
# 43 passed in 2.92s

python -m pytest
# 413 passed in 49.85s

python -m ruff check src tests tools
# All checks passed!

python -m ruff format --check src tests tools
# 116 files already formatted

python -m mypy
# Success: no issues found in 83 source files
```

The Phase 7 fixtures are deterministic synthetic graphs and database rows. They
cover covered/low/unmapped/uncertain/insufficient data, explicit, inferred,
candidate and reviewed graph edges, ambiguous and below-floor mappings, alias
resolution, one question to several concepts,unit mark de-duplication, missing marks and sessions, shared sessions, duplicate source records
for distinct documents, empty corpora, no/partial Phase 3 analysis,
configurable thresholds, course and syllabus-version isolation, unscoped graph
edges, filters and unknown values, truncation, read-only repeatability, and the
CLI/JSON/export/error paths. See the actual command outcomes in the final
implementation report and in `tests/test_coverage.py` /
`tests/test_coverage_cli.py`.

## Evaluation readiness

Software tests establish that metrics, thresholds, isolation, filtering,
provenance and error handling behave as specified. They do **not** establish:

* that Phase 3 semantic/lexical mappings are educationally correct;
* that every genuinely underrepresented concept is detected;
* that coverage counts reflect examination importance;
* that results generalize to unseen courses or syllabi.

Before treating a coverage report as evidence about a real course, a reviewer
should:

1. select a representative question sample and manually label question → concept
   pairs, including “outside this syllabus version”;
2. compare Phase 3/Phase 7 accepted mappings with those labels and report
   precision/recall by concept and mapping method;
3. review all `uncertain` concepts and `ambiguous_mapping` /
   `below_confidence_floor` candidates, not just the covered percentages;
4. record syllabus version, document hashes, corpus size, missing marks/dates and
   the reviewer's decisions;
5. repeat across at least two courses or institutions before making any
   cross-course claim.

The repository bundles no labelled educational benchmark. The sample and analytics
fixtures are synthetic and their outputs are software-validation examples only.
Real, permitted syllabi and question papers remain reserved for Phase 13; do not
present synthetic coverage as historical university evidence.

## Provenance and licensing

Every finding retains the source syllabus document/page, question document/source
id, question id and locator, Phase 3 match id and Phase 5 edge id where applicable.
Dataset provenance (source id, dataset type, licence, retrieval date, publisher)
remains on the Phase 1 source/document records; Phase 7 does not relabel it.

The committed fixtures are synthetic and labelled as such. The sample dataset uses
`dataset_type = synthetic`, `license = CC0-1.0` and publisher `Exam Engine project`
(see [`datasets/README.md`](../datasets/README.md)). No third-party syllabus or
question paper is bundled by this phase. A future real dataset must record its
permission/licence and must not be presented as official unless that is true.

## Known limitations

* Mapping quality is inherited from Phase 3; different terminology, paraphrases
  and short questions can remain unmapped or be mapped at low confidence.
  Phase 7 applies a floor and status checks but cannot validate meaning.
* Confidence values are Phase 3 scores, not calibrated probabilities. `covered`
  means “sufficient accepted evidence in this corpus”, not “certain to be examined”.
* A small corpus makes absence uncertain (`uncertain`, not `unmapped`); the
  default threshold is five questions.
* `unmapped` is corpus-relative. The report never proves a concept was never
  examined and never labels a question out of syllabus.
* Only exact resolved period labels are filterable; no arbitrary date range,
  recency weighting or trend claim is implemented.
* Multiple syllabus versions require explicit selection. Course-level
  `syllabus_version` is not a per-document version; `syllabus_id` is the version
  identity. Unattributed questions are excluded and counted.
* A unit filter changes the concepts under review, not the question denominator;
  questions mapped only to other units can appear unresolved in the summary.
* Concept aliases come only from explicit Phase 5 metadata. Phase 7 does not merge
  synonyms or infer cross-course identity.
* Marks are not split between concepts; concept mark totals can overlap and are
  not additive. Unit marks are de-duplicated per question per unit.
* Missing marks and examination dates are never inferred. Session counts are lower
  bounds when metadata is absent.
* Unit membership can overlap when the same normalized topic appears in several
  units; unit totals are not additive.
* There is no report cache or historical report table; every call recomputes from
  current rows. `generated_at` varies between identical runs while `report_id`
  remains stable.
* Phase 5 graph reads are optional derived data. Disabling them or not building the
  graph changes identity/evidence coverage and is reported explicitly.
* No labelled educational benchmark or human review is bundled, so no accuracy,
  precision, recall or pedagogical-optimality claim is made.

## Out of scope

Exam DNA, concept evolution, automatic question generation, advanced assessment
intelligence, misconception mining, a full dashboard and personal educational data
are intentionally not implemented here. Phase 7 stops at an explainable,
reproducible coverage report over the permitted corpus.
