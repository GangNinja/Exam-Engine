# Phase 9 — Concept Evolution Tracker

Phase 9 describes how concepts appear, are worded, are placed and are assessed in the
**dated documents available for one course**. It reports observations inside that
corpus with explicit date provenance and evidence; it does not reconstruct the
complete history of a concept.

It is deliberately **descriptive and review-only**:

* a concept "first observed" date is the earliest usable source date in this
  collection, not the date the concept entered the curriculum;
* a term that disappears from the analysed documents is reported as *no longer
  observed*, never as removed from teaching;
* an excerpt-similarity score is a review trigger, never proof of a changed meaning;
* no canonical concept, alias, graph edge or syllabus record is modified.

Phase 9 does **not** implement automatic question generation (Phase 10), advanced
assessment intelligence (Phase 11), dashboard integration (Phase 12) or personal
data integration (Phase 13).

## Status and architecture

Phase 9 is implemented as a read-only projection over Phases 1–8. It adds **no table
and no migration**; every run recomputes from current rows and writes nothing.

| Module | Responsibility |
|---|---|
| [`src/exam_engine/models/evolution.py`](../src/exam_engine/models/evolution.py) | Validated filters, observations, timelines, change events, syllabus comparisons, assessment windows and report schema |
| [`src/exam_engine/evolution/dates.py`](../src/exam_engine/evolution/dates.py) | One resolved source date per document, with type, field, conflicts and unparsable values |
| [`src/exam_engine/evolution/service.py`](../src/exam_engine/evolution/service.py) | Scope, concept registry, timelines, change detection, syllabus comparison, assessment reuse and report assembly |
| [`src/exam_engine/cli_evolution.py`](../src/exam_engine/cli_evolution.py) | `evolution report/timeline/changes/compare` |

Integration points:

* **Phase 1** documents, pages, questions, marks, extraction status and provenance
  define what can be observed and whether a document is trustworthy.
* **Phase 2/3** `question_analysis`, unit matches, `TypeLabel`, `BloomLabel` and
  difficulty rows supply mappings, type/Bloom labels and mapping confidence.
* **Phase 4** `resolve_exam_period` is reused, so an examination date is never taken
  from upload time; its coarse academic-year fallback is **not** used to order
  assessment questions.
* **Phase 5** concept nodes, aliases, duplicate candidates and evidence-bearing
  `RELATED_TO`/`PREREQUISITE_OF`/`SIMILAR_TO` edges supply identity and relationship
  evidence.
* **Phase 7** coverage findings supply concept-to-question evidence and report ids;
  **Phase 8** Exam DNA profiles are referenced by id, never recomputed in parallel.

Every report records the Phase 7 coverage report ids and Phase 8 profile ids it used,
so a metric can be traced back to the same source as those phases.

## Temporal model

### Date types

`EvolutionDateType` distinguishes `syllabus_effective`, `edition`, `publication`,
`examination` and `unknown`. They are not interchangeable.

### Selection rules

Exactly one date is selected per document, in this precedence:

| Document kind | First choice | Fallback |
|---|---|---|
| Syllabus | explicit effective date (`effective_date`, `effective_from`, `syllabus_effective_date`, `valid_from`, `implemented_from`) | publication date |
| Notes/other with edition metadata | `edition_date`, `revision_date`, `edition_published` | publication date |
| Question paper | resolved exact examination date (`exam_date`/`examination_date`) | publication date |
| Everything else | publication date | — |

Rules that follow from this:

* Every parsed source date is preserved as a `date_candidates` entry with its field
  and date type, whether or not it was selected. The selected date, its type and its
  field are reported separately, so the original evidence is never discarded.
* The validated `document.publication_date` column outranks a free-form
  `metadata.publication_date`; a disagreement is recorded rather than silently
  resolved.
* Values that disagree **inside one date type** (several effective dates, several
  edition dates, or several publication dates including the column) keep the first
  documented value in precedence order, set `date_confident` to `false`, list every
  field and value, and raise `evolution_conflicting_dates`. An effective date and a
  publication date are different date types, so they are not a conflict.
* An unparsable date field is kept in the document's `invalid` list and reported as
  `evolution_invalid_date`; it is never coerced into a date.
* A missing date stays missing: `date=None`, `date_type=unknown`, and the document is
  counted as undated. `ingested_at` is never a candidate, and a bare academic year is
  not converted into an exact date.
* An examination session such as `2021-05` names a **period**, not a day. It produces
  no exact date; the period key is recorded for assessment windows instead.
* Syllabus versions are ordered by their resolved dates. Two versions uploaded in a
  different order are not reordered by upload time; if the order cannot be
  established, `order_known` is `false` and version differences are not classified as
  introduction or removal.
* A date filter (`date_from`/`date_to`) excludes documents without a usable exact
  date and records the exclusion reason; it never widens the course scope.

### Ordering

Observations are ordered by:

1. dated before undated;
2. date (ISO);
3. date-type enum value;
4. concept id, document id, observed term (case-folded), observation id.

The final tie-breakers make equal dates and repeated runs deterministic regardless of
row insertion order, which is also why reports are reproducible.

## Concept timelines

A timeline is built per concept from:

* syllabus topic placements (unit, topic label, mapping confidence);
* question-paper/bank questions, matched through Phase 3 unit matches or a direct,
  unambiguous alias mention;
* other documents (notes, textbooks and study material) matched through aliases.

Each `EvolutionObservation` records concept id, canonical label, observed term,
whether that term is verified, document id/title/kind, date and date type, date
source, date confidence, syllabus id, unit label/number, a short excerpt (≤ 240
characters by default), page number, related question ids, related concepts, mapping
method and mapping confidence. The per-document row additionally lists every
`date_candidates` entry (source, date, type) behind the selected date. A full source
document is never copied into a timeline.

Filters narrow, and are echoed into the report: concept ids or labels, document ids,
document kinds, syllabus ids, change categories and a date range. Course isolation is
enforced by the repository query; filters that name concepts, documents or syllabi
from another course raise `invalid_evolution_filter`. Filtering never crosses a
course, so the institution in `report.course` is scope information rather than a
cross-course selector.

`evidence_quality` per timeline is `high` with at least three dated observations from
at least two documents, `medium` with at least two dated observations, otherwise
`low`.

## Change detection

Every change is a `ConceptChange` with a stable id, category, concept ids/labels,
both sides (document, date and date type, term, unit, excerpt, question ids, syllabus
id where relevant), whether temporal order is known, method, confidence, evidence
quality, review flags, explanation, evidence and limitations. The course is the
report's scope; a syllabus-version comparison also records both syllabus ids.

| Category | Method | Confidence | Review |
|---|---|---|---|
| `alias_introduced` | new term in later observations that is not in the earliest ones | 0.80 verified concept, 0.55 unverified | required only when unverified |
| `terminology_changed` | earliest and latest term sets are completely disjoint | 0.70 | required |
| `context_changed` | token Jaccard of earliest/latest excerpts below the 0.55 threshold | 0.55 | required |
| `concept_introduced` | present in the later syllabus version only, with known order and acceptable extraction | 0.70 | required |
| `concept_no_longer_observed` | present in the earlier version only, with known order and acceptable extraction | 0.65 | required |
| `syllabus_unit_changed` | concept's unit labels differ between ordered versions | 0.70 | required |
| `description_changed` | unit title or topic list differs between versions | 0.60 | required |
| `possible_concept_split` / `possible_concept_merge` | Phase 5 duplicate candidate plus version presence pattern | ≤ 0.60 | required |
| `assessment_pattern_changed` | dated question windows differ in type, Bloom or marks-value shares | 0.65 | required |
| `relationship_changed` | graph edge first/last seen only before/after the concepts' observed span | 0.50 | required |
| `insufficient_evidence` | syllabus versions differ but order or extraction is not reliable | 0.30 | required |

### Terminology

Terminology is only compared when a concept has at least
`min_dated_observations` (default 2) dated observations whose earliest and latest
dates differ. The earliest-date group and latest-date group are compared by observed
term. Similar names alone never merge concepts; ambiguous aliases (one term mapping to
several concepts) are excluded from direct text matching and reported as
`evolution_ambiguous_alias`.

### Context

The earliest and latest dated excerpts (at least ten characters, different documents)
are tokenised to alphanumeric words longer than two characters and compared with
Jaccard similarity. A score below `context_similarity_threshold` (default 0.55)
produces a review event with a warning that lexical similarity is not semantic
evidence. A high score is not treated as proof that meaning is unchanged.

### Syllabus versions

Consecutive versions are compared after ordering by resolved date and syllabus id.
The comparison records shared concepts, concepts only in the later version, concepts
only in the earlier version, unit moves and description changes. Introduction and
removal are asserted only when the order is known **and** the later syllabus document
has complete extraction with confidence at or above
`min_syllabus_confidence` (default 0.6). Otherwise a single `insufficient_evidence`
event reports the differing concept set (up to 20) and states why.

### Assessment

Assessment evolution reuses Phase 7 coverage findings (concept-to-question evidence)
and Phase 8 profile ids; it does not recount coverage with its own rules. For each
concept, question rows are ordered by exact date when known, otherwise by an explicit
examination period key (`exam_session`/`exam_date` only). Rows sharing a date/period
anchor form one group; the ordered groups are split at `(groups + 1) // 2` into an
earlier and later window, so a window is a contiguous slice of time and never mixes
two sessions on either side of an arbitrary question count.

* A change is evaluated only when both windows have at least
  `min_assessment_questions` (default 2) questions.
* `type_share(label) = questions with that primary type / analysed questions in the
  same window`; the same formula applies to Bloom labels.
* `marks_share(value) = questions stating that marks value / questions with known
  marks in the window`; labels use the compact `:g` form (`5`, `10`), as in Phase 8.
* `max_type_share_delta`, `max_bloom_share_delta` and `max_marks_share_delta` are the
  largest absolute differences across labels; a delta at or above
  `assessment_share_delta` (default 0.25) adds a reason and marks the concept
  changed.
* `unit_counts` records the top Phase 3 unit match for each window question, so unit
  representation is visible even where it does not trigger an event.
* `repetition_counts` counts distinct window questions that Phase 8 placed in an
  exact, near-duplicate or semantic group. Concept-related groups are excluded
  because sharing a concept is already the subject of the analysis. No grouping is
  recomputed here, so the counts stay consistent with the referenced profile.
* Raw counts (`questions`, `papers`, `sessions`, `known_marks`,
  `questions_with_marks`, `marks_counts`) stay separate from shares. Marks are summed,
  never split across concepts, and missing marks stay missing.
* Questions with neither an exact date nor an explicit examination period are
  excluded from windows; the document-level `evolution_missing_date` warning and the
  small-sample warning (`evolution_small_assessment`) remain visible.

### Relationships

Only `RELATED_TO`, `PREREQUISITE_OF` and `SIMILAR_TO` edges that name two analysed
concepts and carry a resolvable source document date are compared. An edge first seen
after the concepts' first observation, or last seen before their last observation,
produces a low-confidence review event: a relationship that is no longer observed is
not a relationship that no longer exists.

## Confidence and review semantics

* Confidence is a transparent heuristic weight per method, not a calibrated
  probability.
* `review_required` is `false` only for verified-alias terminology events; every other
  change category is review-only, and `review_status` starts at `pending_review`.
* Changes below `min_change_confidence` (default 0.5) are dropped, except
  `insufficient_evidence`, which is kept so uncertainty remains visible.
* No change event mutates the Phase 5 graph, syllabus records, Phase 7 coverage or
  Phase 8 profiles. Accepting or rejecting a change is outside Phase 9.

## Report status

* `insufficient_data` — no documents, no observations, or no observation with a usable
  date. The report is still returned with warnings; no timeline claim is made.
* `partial` — any undated document, conflicting or invalid date, incomplete
  extraction, missing graph, truncation, ambiguous alias, unknown syllabus order, or
  coverage/mixed-format/small-observation warning.
* `ok` — none of the above.

Each report also carries `scope.scope_statement`, the exact document/syllabus/concept
ids, filters, parameters, data-quality counters, exclusions, warnings and limitations.

## Determinism and read-only behaviour

`report_id` is a SHA-256 digest of the course, filters, parameters, selected documents
and their dates, observation ids and dates, change ids, assessment concept/changed
flags, coverage ids and Exam DNA ids. Identical inputs therefore produce an identical
`report_id`; only `generated_at` changes. No table is written, so running the analysis
repeatedly cannot create duplicate events.

## CLI

```bash
# Full report for one course
python -m exam_engine evolution report --course demo-cs201-2024-v1

# JSON export, with the assessment/relationship sections disabled
python -m exam_engine evolution report --course demo-cs201-2024-v1 \
  --no-assessment --no-relationships --json --output /tmp/evolution.json

# One concept's dated observations and date provenance
python -m exam_engine evolution timeline --course demo-cs201-2024-v1 --concept "Stacks and queues"

# Review queue, filtered by category and confidence
python -m exam_engine evolution changes --course demo-cs201-2024-v1 \
  --category possible_concept_merge --min-confidence 0.5 --review-only

# Two syllabus versions of the same course
python -m exam_engine evolution compare --course demo-cs201-2024-v1 \
  --earlier syl_2023 --later syl_2025
```

Filters available on `report`, `timeline` and `changes`: `--concept`, `--document`,
`--kind`, `--syllabus`, `--date-from`, `--date-to`; `report` also accepts
`--no-assessment`, `--no-exam-dna`, `--no-relationships`, `--output` and `--json`.
Errors use the same shape as earlier phases: `{"error": {"code", "message", "detail"}}`
with a non-zero exit status.

Example report header on the committed (undated) synthetic sample:

```text
course=demo-cs201-2024-v1 status=insufficient_data report=evo_ec2564656589342902074e21
documents  dated  concepts  observations  dated obs  changes  review  comparisons
---------  -----  --------  ------------  ---------  -------  ------  -----------
4          0      16        16            0          0        0       0
scope: 4 document(s) and 1 syllabus version(s) for course demo-cs201-2024-v1
  warning: evolution_no_dated_observations: No observation carries a usable source date;
  chronological change cannot be established.
```

The sample dataset ships no publication or examination dates, so the tracker
deliberately refuses to invent a timeline. The synthetic Phase 9 fixtures in
[`tests/test_evolution.py`](../tests/test_evolution.py) supply dates to exercise the
dated paths.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_Q9_MAX_DOCUMENTS` | `2000` | Documents read per report |
| `EXAM_ENGINE_Q9_MAX_CONCEPTS` | `500` | Concept cap |
| `EXAM_ENGINE_Q9_MAX_OBSERVATIONS` | `200` | Observations kept per concept |
| `EXAM_ENGINE_Q9_MAX_CHANGES` | `1000` | Change-event cap |
| `EXAM_ENGINE_Q9_CONTEXT_SIMILARITY` | `0.55` | Excerpt-similarity threshold |
| `EXAM_ENGINE_Q9_MIN_CHANGE_CONFIDENCE` | `0.5` | Minimum reported change confidence |
| `EXAM_ENGINE_Q9_MIN_DATED_OBSERVATIONS` | `2` | Dated observations needed for terminology/context checks |
| `EXAM_ENGINE_Q9_MIN_SYLLABUS_CONFIDENCE` | `0.6` | Minimum later-syllabus confidence for introduction/removal |
| `EXAM_ENGINE_Q9_MIN_ASSESSMENT_QUESTIONS` | `2` | Questions required per assessment window |
| `EXAM_ENGINE_Q9_ASSESSMENT_SHARE_DELTA` | `0.25` | Share delta that counts as an assessment change |
| `EXAM_ENGINE_Q9_MAX_EXCERPT_CHARS` | `240` | Excerpt length in observations |
| `EXAM_ENGINE_Q9_INCLUDE_EXAM_DNA` | `true` | Cross-reference Phase 8 profiles |

## Testing

```bash
python -m pytest tests/test_evolution.py tests/test_evolution_cli.py  # 26 passed
python -m pytest                             # 470 passed
python -m ruff check src tests tools         # All checks passed
python -m ruff format --check src tests tools  # 130 files already formatted
python -m mypy                               # Success: no issues found in 93 source files
```

`tests/test_evolution.py` covers the Phase 9 requirements with deterministic
synthetic fixtures: chronological ordering, all date types, equal-date tie-breaking,
unknown/conflicting (publication and effective) /invalid dates, ingestion time never
used, invalid date ranges,
stable terminology, verified and likely aliases, similar-but-distinct concepts,
possible split and merge, low-confidence matching, context change with excerpts,
missing excerpts, edition-date precedence, syllabus
introduction/removal/movement/description change, unknown version order, incomplete
extraction, period-window question-count/marks changes, type changes, missing marks
and examination dates, incompatible examination formats, small samples, reuse of
Phase 7/8 ids, course and institution isolation, reproducibility, read-only behaviour,
partial failures and filter validation. `tests/test_evolution_cli.py`
exercises report/timeline/changes/compare JSON and human output, export, review
filtering, date-range filtering and structured errors. Synthetic fixtures validate
software behaviour only; they are not evidence of real-world concept evolution.

## Performance considerations

* Document text is scanned once per concept alias; cost grows with
  concepts × aliases × corpus text, so the document/concept/observation caps bound
  memory and runtime.
* Excerpts are trimmed to 240 characters to keep reports small; the full document is
  never copied.
* Assessment reuse calls Phase 7 coverage and Phase 8 Exam DNA once per syllabus
  version. On large corpora, disable them with `--no-assessment`/`--no-exam-dna` when
  only timelines or terminology are needed.
* Nothing is cached: repeated runs recompute, which keeps results consistent with the
  current database but costs time proportional to corpus size.

## Evaluation readiness

Software tests establish correct ordering, metric arithmetic, provenance, stable
report generation, course isolation and safe treatment of uncertainty. They **cannot**
establish that two terms are genuinely synonymous, that a concept's meaning changed,
that a syllabus concept was truly removed, or that an assessment trend is
educationally meaningful.

An evaluation should therefore:

1. Sample dated observation pairs per concept and have a subject expert mark the term
   pair as same concept / likely same / different, measuring alias precision and
   recall against that manual judgement.
2. Sample syllabus version pairs and have a reviewer classify each difference as
   genuine change, extraction loss, or ambiguous, measuring how often
   `insufficient_evidence` is used correctly.
3. Review a sample of assessment-window changes against the underlying papers and
   confirm the counts and shares by hand.
4. Record inter-rater agreement and keep the reviewed pairs as a versioned benchmark
   separate from synthetic fixtures.

Public or appropriately licensed educational materials may be used, with source,
licence and retrieval date recorded in the dataset manifest
(see [docs/datasets.md](datasets.md)). Personal syllabi and question papers are
reserved for Phase 13 and are not required by Phase 9.

## Known limitations

* The tracker sees only the analysed corpus. "First observed" and "no longer observed"
  are statements about that collection.
* Only one date per document is selected; a document with several relevant dates keeps
  the others only as conflict evidence.
* A session-only paper has no exact date. It can order an assessment window by period,
  but cannot support terminology or context chronology, which need exact dates.
* Context comparison is lexical, not semantic, and is sensitive to excerpt length and
  language.
* Syllabus introduction/removal depends on segmentation quality and a confidence
  threshold; low-confidence or partial versions become `insufficient_evidence`.
* Concept identity comes from Phase 5 and syllabus topics. Synonyms not linked as
  aliases stay separate, and an ambiguous alias is excluded from direct matching.
* Terminology and alias detection compare only the earliest-date and latest-date
  observations. A term that appears and disappears between them is not reported.
* Assessment shares use Phase 3 primary labels; label errors propagate. Questions
  without marks or dates are counted as missing, not estimated.
* Graph relationship changes need an edge with a source document date; edges without
  one are ignored.
* Confidence values are uncalibrated heuristics; no labelled Phase 9 benchmark is
  bundled.
* Reports are recomputed, not snapshotted; they describe the database as it is when
  the command runs.

## Out of scope

Phase 10 (automatic question generation), Phase 11 (advanced assessment
intelligence), Phase 12 (dashboard integration) and Phase 13 (personal data
integration and validation) are not implemented. Phase 9 adds no automatic concept
merging, splitting or renaming, and no forecast of future examinations.
