# Phase 8 — Exam DNA & Question-Setting Patterns

Phase 8 describes observable question-setting patterns in the **available**
examination papers for one course and syllabus version. It reports what the
collection shows, with the supporting papers, questions and denominators.

It is deliberately **descriptive, not predictive**:

* a recurrence rate is a historical count, not a probability for a future paper;
* a recurring command verb or unit does not prove examiner intent;
* the profile does not claim that a topic "will" appear, and it does not infer
  examiner identity;
* no small collection is presented as a universal examination trend.

Phase 8 does **not** implement concept evolution, automatic question generation,
advanced assessment intelligence, a dashboard, misconception mining, or personal
educational data. Those remain later phases.

## Status and architecture

Phase 8 is implemented as a read-only projection over Phases 1–7. It adds **no
table and no migration**; running it writes nothing.

| Module | Responsibility |
|---|---|
| [`src/exam_engine/models/exam_dna.py`](../src/exam_engine/models/exam_dna.py) | Validated request, paper, distribution, pattern, group and profile schemas |
| [`src/exam_engine/exam_dna/service.py`](../src/exam_engine/exam_dna/service.py) | Scope, eligibility, duplicate copies, distributions, recurrence and grouping |
| [`src/exam_engine/exam_dna/structure.py`](../src/exam_engine/exam_dna/structure.py) | Explicit command-verb, choice-instruction and wording-difference cues |
| [`src/exam_engine/cli_exam_dna.py`](../src/exam_engine/cli_exam_dna.py) | `exam-dna profile/patterns/questions/papers` |

Integration points:

* **Phase 1** documents, questions, marks, sections, provenance and extraction
  status define paper identity and eligibility.
* **Phase 2/3** `question_analysis`, unit matches, `TypeLabel`, `BloomLabel`,
  `DifficultyAssessment` and `SimilarityMatch` supply classification and similarity
  evidence. Type/Bloom distributions use the stored primary label, exactly like
  Phase 4.
* **Phase 4** `resolve_exam_period` is reused, so a session/date is never inferred
  from upload time.
* **Phase 7** coverage is called for the selected papers. Its accepted concept
  mappings and statuses are reused; Phase 8 does not implement a second coverage
  algorithm. Concept presence in a paper comes from Phase 7 `supporting_questions`.

The Phase 7 call is scoped to the Phase 8 eligible `document_ids` plus the source
and period filters, so the two analyses describe the same paper selection. When no
paper is eligible (for example a course with no question papers), the coverage
section is the course/syllabus baseline instead of a paper-scoped subset; the
profile is `insufficient_data` and says so.

## Analysis scope

A request names exactly one course. The syllabus version is:

1. the explicit `syllabus_id`, when supplied and owned by the course;
2. the only parsed syllabus, when the course has one;
3. otherwise a structured `ambiguous_syllabus` error.

The selection itself is delegated to `CoverageService`, so Phase 8 and Phase 7
cannot disagree about which syllabus version is in force.

Phase 3 unit matches are stored per syllabus version. The profile keeps only the
matches that belong to the selected version before unit distributions, unit
patterns or per-paper unit counts are computed, so a paper written against a
different version contributes no unit evidence. Any question whose only matches
belong to another version is counted in
`data_quality.questions_mapped_to_other_syllabus_versions` / `...papers_...` and
produces `exam_dna_mixed_syllabus_versions` instead of being silently merged into
the selected version's unit profile.

Filters:

| Filter | Effect |
|---|---|
| `--document` | Only that paper document |
| `--source` | Only papers from that dataset source |
| `--exam-type` | Only papers whose resolved examination type matches |
| `--exam-period` | Only papers whose resolved period label/session matches |
| `--date-from` / `--date-to` | ISO examination-date range using `ExamPeriod.exact_date` |

Unknown document, source, exam-type or period values raise
`invalid_exam_dna_filter` with the unknown values in `detail`. A date range never
matches a paper with no exact examination date; those papers are listed in
`data_quality.exclusions` with reason `filtered` and produce a missing-date
warning instead of being silently dropped.

Scope does not cross course boundaries. Questions are read through the
course repository, and the Phase 7 call is course-scoped. Institution identity is
copied from the course record; two institutions with the same course code are
still separate courses.

## Paper eligibility and duplicate copies

Only `DocumentKind.QUESTION_PAPER` documents are papers. Question banks, notes,
syllabi and other documents are never counted as independent paper evidence; they
are reported in `data_quality.question_banks_excluded` / `other_documents`.

A paper is **eligible** when it has at least one stored question and its extraction
status is not `failed`, `empty` or `needs_ocr`. A `partial` extraction is eligible
but produces a warning and makes the profile `partial`.

Duplicate copies are excluded from independent evidence and reported explicitly:

* documents with the same non-empty `text_fingerprint`;
* papers whose complete set of normalized question texts is identical, when the
  paper has at least `duplicate_signature_min_questions` questions (default 3).
  The minimum prevents a short paper that legitimately repeats one question from
  being mistaken for a copied paper.

Within each duplicate group the canonical paper is the earliest resolved period,
then the lowest document id. Every other copy keeps its summary with
`eligible = false`, `exclusion_reason = duplicate_copy` and `duplicate_of`. Only
canonical papers enter denominators.

## Observed characteristics

For every eligible paper the profile records:

* question count, analysed count, sub-question count and low-confidence count;
* stated marks coverage, known marks, missing marks;
* primary question-type counts;
* primary Bloom-level counts;
* difficulty-label counts;
* unit question counts and unit marks (a question counts once per unit; marks are
  not split between units or concepts);
* section labels from stored question metadata;
* command-verb counts;
* explicit optional/compulsory instructions.

Distributions over the selected corpus provide `question_count`,
`papers_observed`, `share_of_questions`, `share_of_papers`, `marks`,
`questions_with_marks` and `share_of_marks`. Questions without analysis appear as
`not_analysed`, which is kept distinct from `unclassified` (analysed but no label
reached the threshold). Missing marks are counted, never replaced with zero.

### Question format and cognitive demand

Question format uses the existing Phase 3 `QuestionType` labels; Phase 8 does not
create a second classifier. Bloom uses the stored `bloom_primary` and
`bloom_confidence`. Both are estimates, and their uncertainty is preserved in
`data_quality.low_confidence_questions` and the limitations.

### Command verbs

A small rule-based detector reads the opening words of each question and returns
the first recognised command verb (`explain`, `calculate`, `compare`, `design`,
`prove`, …). A question with no recognised opening verb contributes to no verb
count; nothing is inferred from the middle of the question.

### Section and internal-choice structure

Section labels come only from stored `question.section`. Optional/compulsory
structure comes only from explicit wording:

* `all_compulsory` — "answer all questions", "all questions are compulsory", …
* `choose_any` — "answer any two of the following questions", "choose any three
  questions", … with the required count recorded.

A phrase such as "answer any of the following questions" has no count and is
therefore **not** turned into a rule; the profile emits
`exam_dna_choice_uncertain` instead. Section structure is only compared when
every included paper has section metadata; a mix of present and absent metadata
makes the profile `partial`.

## Pattern categories

| Category | Key | Observed in a paper when … |
|---|---|---|
| `question_type` | type value | at least one question has that primary type |
| `bloom_level` | Bloom value | at least one question has that primary Bloom level |
| `unit_presence` | unit label | at least one question is assigned to that unit |
| `unit_marks_share` | unit label | the unit receives at least `dominant_unit_marks_share` of that paper's known marks |
| `command_verb` | verb | at least one question opens with that verb |
| `section_structure` | joined section labels | the paper has that sorted set of section labels |
| `question_type_mix` | joined type values | the paper's set of primary types matches |
| `marks_value` | marks value | at least one question states that exact value |
| `concept_type_pair` | concept + type | the concept's accepted questions include that primary type |
| `choice_structure` | rule key | the paper contains that explicit optional/compulsory instruction |

`concept_type_pair` is only emitted when the pair is observed in at least
`min_concept_paper_occurrences` papers (default 2). Other categories are retained
even for a single observation so that the absence of recurrence is visible.

## Recurrence definitions

For every pattern:

```text
recurrence_rate = papers_observed / eligible_papers
```

A rate is `null` when `eligible_papers == 0`. The rate is **not** a probability.

Session recurrence is reported separately:

```text
eligible_sessions = distinct explicit examination-session keys among eligible papers
session_recurrence_rate = sessions_observed / eligible_sessions   (null when 0)
```

Papers from the same explicit session count as separate papers but one session.
Two papers from one session therefore cannot inflate a session denominator, and a
pattern that appears only inside one paper is never described as cross-paper
recurrence.

Four frequencies stay distinct: within-paper occurrences
(`within_paper_occurrences`), across papers (`papers_observed`), across independent
sessions (`sessions_observed` / `eligible_sessions`) and, per paper,
`exam_types`. Frequency across different examination *sets* of one sitting (for
example Set A and Set B variants) is not reported: ingestion carries no
examination-set metadata, so a set is never inferred from document titles.

`is_recurring` requires all of:

* at least two observed papers;
* `recurrence_rate >= recurrence_threshold` (default 0.5);
* `eligible_papers >= min_papers_for_recurrence` (default 3).

A pattern outside these bounds is still reported with its counts, evidence and
limitations. `evidence_quality` is `high` for a recurring pattern, `medium` when
observed in at least two papers, and `low` otherwise.

Every pattern carries:

* `pattern_id`, `category`, `label`, `description`, `scope`;
* `papers_observed`, `eligible_papers`, `recurrence_rate`, `denominator_valid`;
* `within_paper_occurrences`;
* `sessions_observed`, `eligible_sessions`, `session_recurrence_rate`;
* supporting document and question ids, periods, sessions and exam types;
* evidence lines and limitations (single paper, small collection, shared session).

## Repeated and similar questions

Four categories are kept strictly distinct:

| Kind | Source | Meaning |
|---|---|---|
| `exact` | normalized stored text | identical wording |
| `near_duplicate` | stored Phase 3 `near` pairs | highly similar wording |
| `semantic` | stored Phase 3 `semantic` pairs | similar meaning |
| `concept_related` | stored Phase 3 `concept_related` pairs | same concept, possibly different demand |

Pair-level reuse:

* both questions must belong to eligible papers (duplicate copies excluded);
* `score >= similarity_min_score` (default 0, i.e. every stored pair is reused);
* pairs at or above the floor are grouped by connected component per kind;
* stored exact pairs are not duplicated here because the exact groups are derived
  directly from normalized text.

Each group reports question/document/session counts, the best and minimum stored
score, the method, shared accepted concepts, shared primary types, a shared command
verb, a wording-difference summary, review status and evidence quality.

`template_variant` is a conservative heuristic: it is true only for a non-exact
group whose members share an accepted concept, a primary type and a command verb.
It remains a review candidate, not a claim that the questions are equivalent.

`exact` groups have `review_required = false` because the wording is proved
identical — but identical wording still does not prove the same role or expected
answer, which is stated in the group's limitations. All non-exact groups require
review.

## Dated session series and descriptive changes

Papers carrying an explicit session key or an exact examination date form an
ordered `sessions` series. A paper with neither stays in paper-level observations
but is excluded from the series, and is reported through `questions_without_session`
and `papers_without_dates`.

Each row reports the session key and label, period, examination date when known,
examination types, document ids, question/analysed/marked counts, known marks and
three shares:

* `question_type_shares` and `bloom_shares` divide by that session's analysed
  questions;
* `unit_marks_shares` divides by that session's known marks, so unit shares can
  overlap and need not sum to 1;
* a label observed elsewhere in the series but absent from a session with a valid
  denominator is recorded as `0.0`; a session with no analysed questions or no
  known marks has no shares at all, so a missing denominator never becomes a
  fabricated zero.

`trends` is emitted only when at least three dated sessions are available. Each row
names a dimension (`question_type`, `bloom_level`, `unit_marks_share`) and a label,
aligns the share series to the chronological session order and reports:

```text
change = last_value - first_value
direction = increased | decreased | stable | unknown
stable band = |change| < 0.05
```

`change` is null (and `direction` is `unknown`) when either boundary session has no
valid denominator. Every row carries the limitation that it is a descriptive change
between the observed sessions — not a projection, a calibrated probability or a
claim about examiner intent. Concept evolution across time belongs to Phase 9.

## Data quality rules

The profile reports:

* eligible, analysed and excluded papers, plus every exclusion and reason;
* duplicate groups and excluded copies, question banks and other documents;
* questions, analysed questions and analysis coverage;
* marks coverage and missing marks;
* classified, unclassified and Bloom-classified question counts;
* low-confidence classifications (type/Bloom confidence below 0.5);
* low-confidence structure (Phase 3 `structure_confidence` below 0.5) and questions
  with detected sub-questions;
* uncertain Phase 7 concept mappings carried into the profile;
* explicit-session coverage (`questions_with_session` / `without_session`);
* questions and papers whose only unit mappings belong to another syllabus version;
* papers with and without exact examination dates;
* examination types present;
* papers with and without reliable section metadata;
* detected choice rules;
* stored similarity pairs considered, reused and below threshold;
* truncation and small-sample flags, including capped patterns and question groups.

Exclusion reasons are `question_bank`, `duplicate_copy`, `no_questions`,
`incomplete_extraction` and `filtered` (scope/date-range exclusion). Nothing is
dropped silently.

Status:

| Status | Rule |
|---|---|
| `insufficient_data` | no eligible papers, no questions, or no Phase 3 analysis at all |
| `partial` | fewer papers than the recurrence minimum, excluded duplicate copies, missing sessions/dates, incomplete analysis or marks, unclassified or low-confidence labels, low-confidence structure, mixed section availability, mixed exam types, mixed syllabus versions, truncation, or a partial extraction |
| `ok` | none of the partial conditions hold |

Warnings use stable codes, including `exam_dna_small_sample`,
`exam_dna_duplicate_paper`, `exam_dna_missing_dates`,
`exam_dna_mixed_exam_types`, `exam_dna_different_marks_totals`,
`exam_dna_missing_marks`, `exam_dna_missing_analysis`,
`exam_dna_low_confidence`, `exam_dna_missing_classification`,
`exam_dna_uncertain_segmentation`, `exam_dna_uncertain_mapping`,
`exam_dna_mixed_syllabus_versions`, `exam_dna_partial_extraction`, `exam_dna_no_sections`,
`exam_dna_choice_uncertain`, `exam_dna_no_similarity`,
`exam_dna_graph_unavailable` and `exam_dna_truncated`.

## Report schema and example

`ExamDnaProfile` uses `schema_version = "phase8-v1"`,
`algorithm = "phase8_exam_dna_pattern_detector"` and
`algorithm_version = "phase8.v1"`. Top-level keys:

```text
schema_version, profile_id, algorithm, algorithm_version,
course, syllabus, generated_at, status, filters, scope, parameters,
summary, papers, distributions, patterns, question_groups,
sessions, trends, coverage, data_quality, warnings, limitations
```

Abbreviated example (synthetic fixture values):

```json
{
  "schema_version": "phase8-v1",
  "profile_id": "dna_...",
  "course": {"course_id": "phase8-course-v1", "course_code": "CS801"},
  "syllabus": {"syllabus_id": "syl_...", "unit_count": 2, "concept_count": 4},
  "status": "ok",
  "scope": {
    "document_ids": ["doc_paper_2024", "doc_paper_2025", "doc_paper_2026"],
    "sessions": ["exam_session:2024-05", "..."],
    "scope_statement": "3 of 3 question paper(s) for course ..."
  },
  "summary": {
    "total_papers": 3,
    "eligible_papers": 3,
    "questions": 6,
    "pattern_count": 39,
    "recurring_pattern_count": 19
  },
  "patterns": [
    {
      "category": "question_type",
      "key": "explanation",
      "papers_observed": 3,
      "eligible_papers": 3,
      "recurrence_rate": 1.0,
      "within_paper_occurrences": 4,
      "sessions_observed": 3,
      "eligible_sessions": 3,
      "is_recurring": true,
      "evidence_quality": "high",
      "evidence": ["Observed in 3 of 3 eligible papers (100%)."],
      "limitations": []
    }
  ],
  "question_groups": [
    {
      "kind": "exact",
      "question_count": 2,
      "document_count": 2,
      "score": 1.0,
      "review_required": false,
      "shared_concepts": ["Arrays"],
      "template_variant": false
    }
  ],
  "sessions": [
    {
      "session": "exam_session:2024-05|type:final",
      "question_count": 2,
      "analysed_questions": 2,
      "known_marks": 10.0,
      "question_type_shares": {"explanation": 0.5, "comparison": 0.5}
    }
  ],
  "trends": [
    {
      "dimension": "question_type",
      "key": "explanation",
      "values": [1.0, 0.5, 0.5],
      "change": -0.5,
      "direction": "decreased",
      "limitations": ["A descriptive change between observed dated sessions only; ..."]
    }
  ],
  "coverage": {"report_id": "cov_...", "status": "partial", "covered_concepts": 4},
  "data_quality": {
    "question_papers": 3,
    "eligible_papers": 3,
    "duplicate_copies_excluded": 0,
    "questions": 6,
    "missing_marks": 0,
    "low_confidence_questions": 0,
    "exclusions": []
  },
  "limitations": ["This profile describes the available question papers only. ..."]
}
```

## CLI

```bash
# Profile for one course (syllabus is optional when only one exists)
python -m exam_engine exam-dna profile --course demo-cs201-2024-v1

# Explicit syllabus, JSON export
python -m exam_engine exam-dna profile --course demo-cs201-2024-v1 \
  --syllabus syl_... --json --output reports/cs201-exam-dna.json

# Recurring patterns only
python -m exam_engine exam-dna patterns --course demo-cs201-2024-v1 --recurring
python -m exam_engine exam-dna patterns --course demo-cs201-2024-v1 \
  --category unit_marks_share --json

# Repeated/similar question groups
python -m exam_engine exam-dna questions --course demo-cs201-2024-v1 --kind exact
python -m exam_engine exam-dna questions --course demo-cs201-2024-v1 --limit 20

# Paper inventory, including exclusions
python -m exam_engine exam-dna papers --course demo-cs201-2024-v1

# Filters (echoed into the report)
python -m exam_engine exam-dna profile --course demo-cs201-2024-v1 \
  --exam-type final --date-from 2024-01-01 --date-to 2025-12-31 \
  --document doc_... --source demo-sample
```

`--json` writes UTF-8 machine-readable JSON. A JSON failure is
`{"error": {"code": ..., "message": ..., "detail": ...}}` with exit code 1.
`--output` creates parent directories and writes the same profile as UTF-8 JSON.

Errors: `unknown_course`, `unknown_syllabus`, `ambiguous_syllabus`,
`invalid_exam_dna_filter`, `exam_dna_export_failed`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_Q8_MAX_QUESTIONS` | `20000` | Maximum included questions; truncation is reported |
| `EXAM_ENGINE_Q8_MIN_PAPERS_FOR_RECURRENCE` | `3` | Eligible papers required before a pattern may be called recurring |
| `EXAM_ENGINE_Q8_RECURRENCE_THRESHOLD` | `0.5` | Minimum `papers_observed / eligible_papers` |
| `EXAM_ENGINE_Q8_DOMINANT_UNIT_SHARE` | `0.25` | Unit mark share that counts as a dominant-unit observation |
| `EXAM_ENGINE_Q8_MIN_CONCEPT_PAPERS` | `2` | Papers required before a concept/type pair is reported |
| `EXAM_ENGINE_Q8_DUPLICATE_MIN_QUESTIONS` | `3` | Minimum paper size for full-question-set copy detection |
| `EXAM_ENGINE_Q8_SIMILARITY_MIN_SCORE` | `0.0` | Minimum stored similarity score reused |
| `EXAM_ENGINE_Q8_MAX_PATTERNS` | `500` | Pattern cap after deterministic ordering |
| `EXAM_ENGINE_Q8_MAX_GROUPS` | `200` | Question-group cap after deterministic ordering |
| `EXAM_ENGINE_Q8_LOW_CONFIDENCE` | `0.5` | Classification confidence reported as low |

All values are echoed in `profile.parameters`, together with the Phase 7
`coverage_report_id`, selected syllabus id, mapping policy and
`forecasting = "out_of_scope"`.

## Determinism and read-only behaviour

For identical database content and configuration, everything except
`generated_at` is stable:

* papers are ordered by resolved period, then document id;
* questions by paper, stored number, then question id;
* patterns by category, descending recurrence rate, then key;
* groups by kind, descending score, then group id;
* the profile id digests the course, syllabus, filters, parameters, paper
  signatures, question ids, pattern ids/rates, group ids and the Phase 7 report id.

Re-running writes no rows, adds no tables and does not modify Phase 3/5/7 data.

## Testing

```bash
python -m pytest tests/test_exam_dna.py tests/test_exam_dna_cli.py
# 30 passed

python -m pytest
# 444 passed

python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m mypy
```

Deterministic synthetic fixtures cover: type, marks, unit and Bloom
distributions; marks bands and missing marks; missing sessions and dates; empty
data; every-paper and some-paper recurrence; within-paper repetition; duplicate
paper copies; same-session papers; exact duplicates; near/semantic/concept groups;
low-score exclusion; template variants; concept/type pairs; unit mark share;
section and explicit choice cues; command verbs; course, institution and
syllabus-version isolation (including unit matches from another version being
excluded); dated session series and descriptive changes; undated-paper exclusion;
mixed exam types and marks totals; partial extraction;
low-confidence labels; question banks; truncation; filters and error paths;
read-only repeatability; and the CLI/JSON/export commands.

These tests prove software behavior. They do not prove that Phase 3 type/Bloom
labels are educationally correct, that similarity groups are true duplicates, or
that the patterns generalize.

## Evaluation readiness

Before treating a profile as evidence about a real course, a reviewer should:

1. draw a representative paper sample and manually label question types, Bloom
   levels, sections and repeated-question groups;
2. compare the profile's groupings and distributions with those labels and report
   precision/recall by category;
3. review every `low`/`medium` evidence-quality group and every `concept_related`
   or semantic group, not only the recurring patterns;
4. record paper provenance, session/date evidence, marks schemes and the number of
   duplicate copies excluded;
5. repeat on at least two courses or institutions before making any cross-course
   claim, and never convert a recurrence rate into a future probability.

The repository bundles no labelled educational benchmark. The sample and analytics
fixtures are synthetic and their outputs are software-validation examples only.
Real, permitted papers remain reserved for Phase 13; synthetic results must not be
presented as historical university evidence.

## Provenance and licensing

Every pattern and group retains source document ids, question ids, periods and
sessions. Dataset provenance (source id, dataset type, licence, retrieval date,
publisher) stays on the Phase 1 records; Phase 8 does not relabel it. The committed
fixtures are synthetic (`dataset_type = synthetic`, `CC0-1.0`, publisher
`Exam Engine project`). No third-party paper is bundled by this phase.

## Known limitations

* Question-type, Bloom and difficulty labels are Phase 3 estimates, not validated
  measurements; low-confidence labels make the profile `partial`.
* Similarity groups reuse stored Phase 3 pairs. `exact` proves identical wording;
  `near`, `semantic` and `concept_related` remain review candidates and may not be
  equivalent or share an expected answer.
* Duplicate-paper detection depends on a stored text fingerprint or an identical
  full question set of at least three questions. A re-typeset copy with different
  wording is not detected.
* A paper with no explicit session or date still contributes to paper-level
  recurrence but not to session-level recurrence. Academic years are periods, not
  sessions. The `sessions` series and its changes therefore omit those papers.
* No examination-set metadata (Set A/Set B) exists, so intra-session set frequency
  is not reported and is never guessed from titles or filenames.
* Unit evidence is restricted to the selected syllabus version. A paper whose
  questions are only mapped to a different version is reported, not merged, so its
  unit evidence is absent from this profile by design.
* `trends` describes the difference between the first and last dated session only.
  It requires at least three dated sessions, ignores undated papers and never
  projects, calibrates or asserts examiner intent.
* Section and choice patterns rely on stored section labels and explicit wording.
  Ambiguous extraction is reported, not interpreted.
* Marks are used only when stated. They are not split between units or concepts,
  so unit/concept mark totals can overlap and papers with different totals are not
  normalised.
* Command-verb detection is rule-based and only reads the opening words.
* There is no persisted profile table or cache; every run recomputes from current
  rows, and `generated_at` changes while `profile_id` stays stable.
* No labelled benchmark is bundled, so no accuracy, generalisation or predictive
  claim is made.

## Out of scope

Concept evolution (Phase 9), automatic question generation (Phase 10), advanced
assessment intelligence (Phase 11), full dashboard integration (Phase 12) and
personal data integration/validation (Phase 13) are intentionally not implemented.
Phase 8 stops at an explainable, reproducible historical pattern profile.
