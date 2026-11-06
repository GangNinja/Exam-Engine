# Phase 13 — Personal Examination Dataset Integration

Integration and validation of the local GATE, CBSE Class 10/12, JEE Advanced, JEE
Mains and NEET corpus across the existing Phase 1–12 engine.

**Status:** complete for the documented import, downstream and dashboard checks.
The corpus is real user-supplied material; the results below describe those files,
not an educational-accuracy benchmark. Missing answers, options, marks and years
remain missing and are never inferred.

---

## 1. Scope and constraints

Phase 13 had to reuse the existing architecture rather than create a second
pipeline:

* preserve the `datasets/` hierarchy and every original CSV/PDF byte;
* discover and match 29 CSVs with 29 subject syllabi;
* import each CSV as a `question_bank` document and each PDF as a `syllabus`
  document;
* keep every exam category and subject isolated in its own course;
* keep repeated questions in different years as separate occurrences;
* preserve source ids, wording, options, subparts, marks and answer text;
* expose real counts and filters in the existing dashboard;
* feed NLP, understanding, analytics, graph, prerequisites, coverage, Exam DNA,
  evolution, generation and advanced assessment;
* report every skipped, unfilled or ambiguous field instead of fabricating it.

No source file is renamed, moved, overwritten or deleted by any Phase 13 code
path.

## 2. Corpus discovered

| Category | Courses | Papers | Questions | Answers |
|---|---:|---:|---:|---:|
| GATE | 10 | 199 | 5,905 | 1,337 |
| CBSE Class 10 | 5 | 203 | 2,670 | 2,572 |
| CBSE Class 12 | 5 | 213 | 2,833 | 1,953 |
| JEE Advanced | 3 | 25 | 450 | 410 |
| JEE Mains | 3 | 42 | 1,164 | 932 |
| NEET | 3 | 39 | 2,410 | 2,105 |
| **Total** | **29** | **721** | **15,432** | **9,309** |

* Files: 58 — 29 exam CSVs and 29 syllabus PDFs.
* Documents imported: 58 — 29 `question_bank`, 29 `syllabus`.
* Syllabus structure: 301 units and 2,769 topics.
* Source ids: preserved in `source_question_id`; the original
  `question_number`/row is retained.
* Years: 2015–2026, derived only from `question_id`. A missing year is `NULL`.
* Explicit options: present on 4,971 rows. Answers: 9,309 rows; 4,036 rows also
  carry solution text.
* Question formats: MCQ 9,298; numerical 2,242; short answer 1,555;
  short-or-long answer 852; long answer 830; MSQ 416; unknown 173;
  assertion/reason 56; reading comprehension 7; value-based 3.

### Source preservation

The importer opens CSVs and PDFs read-only and never writes under `datasets/`.
The audit writes only under `reports/phase13/`; the database is the only mutable
artifact. Original file names, folder hierarchy and content remain the
authoritative copies.

## 3. Audit results

Generated before import:

* `reports/phase13/dataset_inventory.csv` — one row per discovered file;
* `reports/phase13/dataset_validation_report.csv` — one row per finding;
* `reports/phase13/dataset_summary.json` — machine-readable totals.

| Validation result | Count |
|---|---:|
| Rows/records checked | 15,432 |
| Errors | 0 |
| Warnings | 11,720 |
| Informational findings | 10,809 |
| Duplicate source question ids | 0 |
| Corrupted text rows | 21 |
| Syllabus documents needing OCR | 0 |

The main warnings are facts about the source, not discarded questions:

* 6,123 questions have no answer text;
* 10,636 questions have no stated marks;
* 4,907 MCQ/MSQ/assertion questions have no explicit options;
* 467 answer rows have no identifiable option key;
* 173 questions have no recognised type;
* 178 rows contain a solution but no answer;
* 22 syllabus documents required structural inference.

No row with question text was dropped. The 173 unknown types remain in the
database with `question_type=unknown`.

## 4. Storage and import

Migration `0010_exam_datasets.sql` adds paper, answer, question-detail, dataset
run/file and syllabus-projection records:

* `exam_papers` — category/subject/year/paper group, source prefix, counts;
* `exam_questions` projection fields in `exam_question_details` — source row,
  options, type, year, source file;
* `exam_answers` — answer/solution text, option key and answer type;
* `exam_dataset_runs` / `exam_dataset_files` — import audit and per-file results.

The first full import completed in **43.8 s** with 58 files, 15,432 questions,
9,309 answers and 721 paper groups. A repeat import completed in **8.5 s**,
created **0** new questions and skipped all 15,432 existing records by source
identity. It did not merge separate occurrences in different years.

Database integrity checks after import: `PRAGMA integrity_check=ok`, zero orphan
answer/detail rows, zero cross-course papers, zero blank source files and zero
answers without text.

## 5. Downstream integration

Every module was run against all 29 course scopes. The runner and per-course
artifacts are in `reports/phase13/courses/`; aggregated totals are in
`reports/phase13/downstream_summary.json`.

| Stage | Courses ok | Wall time | Actual result |
|---|---:|---:|---|
| NLP | 29 | 1,180 s | 29 runs, 0 errors; each `partial` only because the corpus has fewer than three documents per course and lexical entities need review. 58 informational warnings. |
| Understanding | 29 | 910 s | 15,432/15,432 questions analysed; 29 successful runs. |
| Analytics | 29 | 58 s | 15,432 questions; 6,009 mapped; 8,327 unmapped; 4,796 with stated marks. |
| Graph | 29 | 192 s | Per-course sums: 30,520 nodes, 103,163 edges, 58 shared-node references; 0 errors, 84 informational warnings. |
| Prerequisites | 29 | 67 s | 4,653 review-only candidates created; 9,166 isolated concepts; 0 conflicts. |
| Coverage | 29 | 53 s | 2,757 concepts: 3 covered, 9 low-coverage, 1,762 uncertain, 983 unmapped; 6,009 mapped questions. |
| Exam DNA | 29 | 59 s | 721 papers, 1,741 patterns, 329 recurring patterns, 1,123 near/semantic/concept-related groups. |
| Evolution | 29 | 75 s | 750 document observations (including virtual paper adapters), 39,314 concept observations, 2,684 review-only change events. |

All 174 module/course runs (29 × 6) finished `ok`; total downstream wall time was
**504 s**. The runner is `reports/phase13/run_course_modules.sh` and the status
ledger is `reports/phase13/course_modules_status.tsv`.

The Exam DNA and Evolution readers use read-only virtual paper adapters for the
imported question banks. A year-level ordering date is attached only to those
derived papers, marked low-confidence, and is never written onto the original
question or document.

## 6. Dashboard and API

`/api/exams` and `/api/exams/{papers,questions,syllabus}` expose database counts,
filters, quality signals and the latest import. The `#/exams` workspace renders
those values; there are no hard-coded dashboard counts.

Verified in Chromium after the final backend restart:

* category `gate`, subject `gate-chemistry`, year `2024`, type `mcq`;
* summary: 1 category, 1 subject, 1 paper, 12 questions, 0 answers, 1 syllabus
  document, 3 syllabus units, year 2024;
* paper group `CY2024` with 12 questions and 0 answers;
* question rows show source ids `CY2024_*` and `NO ANSWER` where the CSV has no
  answer;
* syllabus counts are scoped to the filtered course (the initial implementation
  showed all 29 syllabus documents; this was fixed and covered by a test).

The 12 MCQ answers being zero is the source state, not a rendering failure: that
paper has no answer rows. The view reports it as unanswered.

## 7. Generation and advanced assessment

One representative course per category was exercised:

| Check | Courses | Result |
|---|---:|---|
| Phase 10 generation | 6 | 6 batches, 6 questions persisted, all `pending_review`; 0 validated automatically. |
| Adversarial generation | 6 | 6 batches, 6 questions persisted, all need review. |
| Mutation | 6 attempts | 5 refused at `source_concept_unresolved`; 1 concept-resolved source was rejected by validation (`missing_source_evidence`, plus numeric/answer-key checks). No variant was persisted. |
| Misconception mining | 1 | 0 stored student responses; 0 candidate misconceptions, as expected without response data. |

Artifacts are in `reports/phase13/representative/`. The refusals are evidence
boundaries, not generated output: the imported question banks do not contain the
source passages or answer keys needed to justify those particular variants.

## 8. Verification and performance

* `python -m pytest -q` — exit 0 (full suite; log in
  `reports/phase13/pytest_final.log`).
* `python -m ruff check src tests` — `All checks passed!`
  (`reports/phase13/ruff_final.log`).
* Phase 13 tests cover discovery/category isolation, row/option/answer parsing,
  audit, first import, duplicate-safe re-import and dashboard filters/API.
* Measured on the supplied Windows machine: 58-file import 43.8 s; repeat import
  8.5 s; NLP 1,180 s; understanding 910 s; six downstream stages 504 s.

No timing is presented as a benchmark on other hardware.

## 9. Limitations and remaining work

* Mapping coverage is **6,009/15,432 (39%)**; 8,327 questions remain unmapped.
  Coverage, Exam DNA, evolution and prerequisites inherit this limitation.
* 6,123 questions have no answer and 10,636 have no marks. Marks analytics cover
  only stated marks; no total marks are inferred.
* 4,907 objective questions lack options, and 173 have unknown type. They remain
  visible rather than being dropped or silently relabelled.
* NLP is `partial` for every course because each course has one CSV and one
  syllabus, so topic discovery uses sections and entity extraction is lexical.
* Years come from `question_id` only. There is no examination-session/calendar
  metadata, so dated Exam DNA trends and genuine date-based evolution are limited;
  virtual paper years are ordering proxies only.
* Generation/adversarial output is review-only. Mutation produced no persisted
  variant for the tested sources, and misconception mining cannot run without
  student responses.
* The dashboard is loopback-only, single-user and unauthenticated, as in Phase 12.
  No automated cross-browser or WCAG audit was performed.
* No labelled educational benchmark exists for this corpus; all results are
  descriptive software outputs, not claims of examiner intent or student ability.

## 10. Acceptance

Phase 13 is accepted on the evidence above for:

1. source discovery, schema-compatible parsing and source preservation;
2. zero-loss audit/import with explicit missing-data reporting;
3. 29 course-isolated dataset scopes and 15,432 searchable occurrences;
4. downstream execution over all 29 scopes with no module failures;
5. real database-backed dashboard counts and filters;
6. generation/adversarial integration and evidence-bound mutation refusal;
7. full test suite and lint checks passing.

It is not an educational-accuracy validation. The remaining data gaps in §9 are
properties of the supplied corpus, not silently repaired values.
