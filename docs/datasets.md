# Datasets: acquisition, provenance and the sample set

Phase 1 must work without any external dataset, and must never quietly absorb
material it has no right to use. This document covers the sample dataset, the
import workflow for future datasets, and the licensing rules the code enforces.

## 1. What a "dataset" is here

A dataset is one manifest plus the files it references:

* one `dataset` record (provenance: source id, type, licence, retrieval date,
  publisher, notes);
* one or more courses, each with documents.

Every document inherits its dataset's provenance. Reusing a `source_id` with a
different name or type overwrites the provenance record and emits a
`source_conflict` warning — do not reuse ids across genuinely different sources.

## 2. The committed sample dataset

Location: [`datasets/sample/`](../datasets/sample) — 2 courses, 7 documents, all
**synthetic**.

| File | Course | Kind | Exercises |
|---|---|---|---|
| `cs201/question_paper_midterm.pdf` | CS201 | question_paper | PDF pages, sections, explicit marks, page locators |
| `cs201/syllabus.docx` | CS201 | syllabus | DOCX paragraphs + table, `Unit N: Title (8 hours)` |
| `cs201/lecture_notes_stacks.txt` | CS201 | notes | plain text, Unicode maths preserved |
| `cs201/question_bank.csv` | CS201 | question_bank | CSV aliases, missing marks, `row N` locators |
| `phy101/question_paper_final.txt` | PHY101 | question_paper | `PART A/B`, `(5 marks)`, bare `[6]` |
| `phy101/syllabus.txt` | PHY101 | syllabus | `MODULE N:` variant |
| `phy101/question_bank.json` | PHY101 | question_bank | `{"questions": [...]}` JSON, list tags |

Every file states `SYNTHETIC` in its content (or in the PDF's title block), and the
manifest marks the dataset `dataset_type=synthetic` with a CC0 licence. The import
prints:

```
note: dataset_provenance: dataset 'demo-synthetic-2024' is synthetic:
      extracted questions are development fixtures, not official exam papers
```

That warning is intentional: synthetic material must never be presented as an
official exam paper.

Regenerate deterministically (useful after editing content or the writer):

```bash
python tools/build_sample_dataset.py
```

The generator uses PyMuPDF and python-docx, so regenerated files are byte-different
(zip metadata, PDF object order) even when the text is identical. Document ids are
content-addressed, so a regeneration creates new documents rather than silently
rewriting the old ones; re-import into a fresh database, or use
`--on-duplicate replace` only when the content really is the same.

## 3. Importing a new dataset

1. **Decide the rights.** Choose a `dataset_type`:

   | Type | Use when |
   |---|---|
   | `official` | Issued by the institution/exam board you are working with |
   | `public` | Publicly accessible, but reuse terms unclear or not granted |
   | `openly_licensed` | A concrete licence covers reuse (requires `license`) |
   | `permissioned` | Written permission from the rights holder |
   | `synthetic` | Generated for development/testing (requires licence/usage terms) |
   | `unknown` | Last resort; provenance gaps are surfaced as warnings |

   "Publicly accessible" is not a licence. Record `license` (SPDX id or name),
   `license_url`, `usage_terms`, `source_url`, `retrieved_at` and `publisher`.

2. **Store the files** under a per-dataset directory, e.g.
   `datasets/external/<source-id>/...`. Keep the originals untouched; all cleaning
   happens in memory and only extracted text is stored.

3. **Write the manifest.** Copy
   [`datasets/templates/manifest.example.json`](../datasets/templates/manifest.example.json)
   and adjust it. Paths are relative to the manifest.

4. **Dry-run first**, then import:

   ```bash
   python -m exam_engine import datasets/external/my-source/manifest.json --dry-run
   python -m exam_engine import datasets/external/my-source/manifest.json
   python -m exam_engine stats
   python -m exam_engine run <run_id>
   ```

5. **Record what happened.** The run report is persisted, so future phases can
   tell which documents came from which retrieval.

### Versioning a course

A new syllabus revision or academic year is a **new course id**, not an update:

```
demo-cs201-2024-v1   →  demo-cs201-2025-v2
```

That keeps questions tied to the syllabus they were written against. Re-importing
the same course id with new documents is fine; re-importing the same bytes is
handled by the duplicate policy.

### Candidate public sources (evaluate, do not assume)

External acquisition is optional for Phase 1. If you do add public material, verify
the terms on the source page for each item (terms vary per item, and "open" often
means non-commercial or no-derivatives):

| Candidate | What it offers | What to verify before importing |
|---|---|---|
| MIT OpenCourseWare | Lecture notes and past exams for many MIT courses | The per-course licence page; attribution and non-commercial terms; individual PDFs can carry extra notes |
| OpenStax | Peer-reviewed open textbooks (useful for syllabus/notes material) | The licence stated for that book; some titles are non-commercial |
| NPTEL / SWAYAM | Indian engineering course content and assignments | Site terms of use; "free to view" is not automatically redistributable |
| University or board paper archives | Official past question papers published by institutions | Whether the institution permits redistribution; otherwise request permission and record it as `permissioned` |
| Kaggle / Hugging Face dataset hubs | Community exam/question datasets | The dataset card's own licence and the provenance of the underlying papers |
| Zenodo / Figshare | Research and educational datasets | The per-record licence and required attribution |

Naming these sources is not a licence claim, and none of them is bundled with this
repository. Open the licence page for each item you actually download, record what
it says in the manifest, and only then import.

Do not scrape sites that forbid it, bypass paywalls or logins, or assume that a
PDF being readable means it may be redistributed. When in doubt, use
`dataset_type=public` with `usage_terms` describing the constraint, and keep the
files out of version control.

## 4. Do not commit what you may not redistribute

`datasets/external/` is a reasonable place for downloaded material, but keep it
out of git unless the licence clearly permits redistribution. The repository ships
only the synthetic sample, which this project generates itself.

## 5. Phase 13 personal examination corpus

Phase 13 reads the user's GATE, CBSE Class 10/12, JEE Advanced, JEE Mains and NEET
corpus in place under `datasets/`. It never renames, moves, deletes or overwrites
an original CSV or PDF. The source folders remain the authoritative copies.

The import is deliberately separate from the synthetic manifest path:

* one course per category/subject (`gate-chemistry`, `cbse-class-12-physics`,
  `jee-advanced-maths`, `neet-biology`, and so on);
* each CSV is ingested as a `question_bank` document, each matched syllabus PDF as
  a `syllabus` document, recorded against source
  `personal-exam-datasets`;
* import runs, file-level results and skipped duplicates are persisted;
* the audit writes `dataset_inventory.csv`,
  `dataset_validation_report.csv` and `dataset_summary.json` before any database
  write;
* missing answers, options, marks and years stay missing and are counted as
  warnings; nothing is fabricated or inferred across exam categories.

Run:

```bash
python -m exam_engine exams discover
python -m exam_engine exams audit
python -m exam_engine exams import
python -m exam_engine exams status
```

To remove personal material later, use a dedicated database file or delete its
course documents through the existing repository/service. Provenance and import
run rows remain the audit trail. Do not commit the corpus unless its licence
clearly permits redistribution.

## 6. Provenance checklist

Before declaring an import done, confirm:

- [ ] `dataset_type` is accurate and not "official" unless it really is.
- [ ] licence or usage terms are recorded; `openly_licensed` has a licence string.
- [ ] `source_url` and `retrieved_at` are set (the import date is not the
      publication date).
- [ ] synthetic data is labelled synthetic in the manifest *and* in the files.
- [ ] `python -m exam_engine run <run_id>` shows no unexpected failures.
- [ ] `python -m exam_engine stats` shows the expected document/question counts and
      no cross-course leakage (spot-check `questions --course ...`).
