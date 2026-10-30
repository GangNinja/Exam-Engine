# Phase 1 — Ingestion architecture

This document describes how a file on disk becomes validated, provenance-tracked,
course-scoped records in SQLite. It is the reference for extending or debugging the
ingestion layer.

## 1. Goals and non-goals

**Goals**

1. Ingest multiple courses, subjects, institutions and syllabus versions in one
   batch without cross-contamination.
2. Preserve provenance (dataset type, licence, source URL, retrieval date) and
   source traceability (document → page → question/syllabus unit).
3. Report uncertainty instead of inventing content.
4. Survive bad input: one unreadable file must not abort a batch.

**Non-goals (later phases)**

Question analysis, Exam DNA, knowledge graphs, question generation, misconception
mining, answer validation, embeddings. Phase 1 emits the raw material and metadata
those phases will consume; it does not interpret questions semantically.

## 2. Pipeline stages

```
manifest.json
   │  validate (pydantic, duplicate course/path checks)
   ▼
ingestion_runs row  ──────────────────────────────────────────────┐
   │                                                              │
   ▼                                                              │
course + dataset_source upsert (conflict warnings)                │
   │                                                              │
   ▼  for each document (own transaction)                         │
resolve path ──► validate (suffix, magic bytes, size)             │
   │                                                              │
   ▼                                                              │
sha256 ──► exact-duplicate check ──► policy: skip / replace / fail│
   │                                                              │
   ▼                                                              │
adapter.extract()  (pages, structured questions for CSV/JSON)     │
   │                                                              │
   ▼                                                              │
OCR only for pages flagged needs_ocr                              │
   │                                                              │
   ▼                                                              │
normalise + character cap + quality score                         │
   │                                                              │
   ▼                                                              │
segmentation (question paper / bank) or syllabus units            │
   │                                                              │
   ▼                                                              │
build records ──► course-scoped duplicate marking ──► persist    │
   │                                                              │
   ▼                                                              ▼
IngestionResult (status, counts, errors, warnings)      ingestion_results rows
```

A document-specific failure produces an `IngestionResult(status=failed)` with a
structured `IngestionError`; the loop continues with the next document. This is a
deliberate fault boundary: every document runs through `_run_document`, which turns
**any** unexpected exception (including a bug in a segmenter or record builder)
into an `unexpected_error` result instead of aborting the batch. The run summary is
written after the loop, in a separate transaction, so a reporting failure cannot
roll back successfully ingested documents (it is logged instead).

## 3. Manifest

The manifest is the repeatable import unit: one JSON file describing one dataset
and its courses, with document paths relative to the manifest file.

```json
{
  "version": 1,
  "dataset": {
    "source_id": "demo-synthetic-2024",
    "name": "Phase 1 synthetic sample dataset",
    "dataset_type": "synthetic",
    "retrieved_at": "2026-10-20",
    "license": "CC0-1.0",
    "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "publisher": "Exam Engine project",
    "source_url": null,
    "usage_terms": null,
    "notes": "Generated fixtures, not official papers.",
    "external_dataset": false
  },
  "courses": [
    {
      "course_id": "demo-cs201-2024-v1",
      "course_code": "CS201",
      "title": "Data Structures and Algorithms",
      "institution_code": "demo-iot",
      "institution_name": "Demo Institute of Technology",
      "department": "Computer Science and Engineering",
      "programme": "B.Tech",
      "academic_year": "2024-25",
      "syllabus_version": "v1",
      "description": null,
      "metadata": {},
      "documents": [
        {
          "path": "cs201/question_paper_midterm.pdf",
          "kind": "question_paper",
          "format": null,
          "title": "CS201 Midterm",
          "academic_year": "2024-25",
          "publication_date": null,
          "language": "en",
          "metadata": {}
        }
      ]
    }
  ]
}
```

Rules enforced at load time (`ingest/manifest.py`):

| Rule | Failure |
|---|---|
| `dataset_type=openly_licensed` requires `license` | validation error |
| `dataset_type=synthetic` requires `license` or `usage_terms` | validation error |
| `source_id` and `course_id` are lowercase slugs | validation error |
| duplicate `course_id` | validation error |
| duplicate document path within a course (`./a` == `a`) | validation error |
| at least one course, one document per course | validation error |
| all fields `extra="forbid"` | unknown keys rejected |

`kind` is one of `question_paper`, `syllabus`, `notes`, `question_bank`, `other`.
`format` is an optional hint; the actual bytes always win and a mismatch is
recorded as an informational `format_mismatch` warning.

A copyable template lives at
[`datasets/templates/manifest.example.json`](../datasets/templates/manifest.example.json).

## 4. Validation and limits

`ingest/validation.py`

* **Path containment** — manifest paths are resolved against the manifest
  directory; `../` escapes are rejected with `path_outside_root`. Absolute paths
  and escapes require an explicit `--allow-external` (useful for ad-hoc
  `ingest`, never for untrusted manifests).
* **Suffix allowlist** — `.pdf .docx .txt .text .md .csv .json .png .jpg .jpeg
  .tif .tiff .bmp .webp`.
* **Magic bytes** — PDF must start with `%PDF-`; DOCX must be a zip containing
  `word/document.xml` and `[Content_Types].xml`; images must carry a known image
  header. Text formats containing NUL/control bytes are rejected as binary.
* **Limits** — file size, page count, extracted character count, questions per
  document (see configuration table in the README). Oversized text is truncated
  deterministically and reported (`document_truncated`), never silently dropped.

## 5. Adapters

Adapters implement one protocol and are registered by suffix:

```python
class DocumentAdapter(Protocol):
    name: str
    suffixes: tuple[str, ...]
    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument: ...
```

`ExtractedDocument` carries pages, optional structured questions, metadata,
warnings, `needs_ocr_pages`, table count and a title hint.

### PDF (`pdf_adapter.py`)

* pdfplumber is primary; if it raises, PyMuPDF retries the whole file and the
  recovery is recorded as an informational warning.
* Encrypted/password-protected files are rejected as `pdf_encrypted`.
* Page count is checked before extraction (`pdf_page_limit`).
* A page with fewer than `min_page_chars` characters **and** images is flagged
  for OCR. A page with an empty text layer and no images simply contributes no
  text.
* If no page yields text with either backend, all empty pages are flagged for OCR
  so the document becomes `needs_ocr` rather than silently empty.
* Metadata records extractor name, page count and producer/creator/title when the
  PDF exposes them. PDF tables are read through the text layer in reading order;
  they are not reconstructed into structured rows in Phase 1.

### DOCX (`docx_adapter.py`)

* Body blocks are walked in document order so paragraphs and tables stay in
  context. Table rows are flattened as `cell | cell | cell`.
* DOCX has no fixed pagination, so the document is one logical page and question
  locators use line ranges.
* Invalid containers raise `docx_invalid`. Macros (`docm`) are not supported, and
  DOCX content is never executed — only text is read.

### TXT (`text_adapter.py`)

* UTF-8/UTF-8-BOM first, then `charset_normalizer`; a low-confidence guess is
  reported as a warning. Form feed (`\x0c`) separates pages.
* UTF-16/32 guesses are discarded unless the payload contains the NUL bytes real
  UTF-16 text has, because short single-byte files are otherwise misread as CJK.
  If the detected decoding looks dirty, cp1252 is used as a conservative fallback.

### CSV / JSON banks (`bank_adapter.py`)

* Header aliasing (`Q.No`, `question_number`, `Question`, `question_text`, `Marks`,
  `points`, …) resolves to `text`, `number`, `marks`, `section`, `unit`, `answer`,
  `difficulty`, `tags`.
* JSON accepts a list of rows, `{"questions": [...]}`, `{"items"/"data"/"rows": []}`,
  a single question object, or `{"sections": [{"name": ..., "questions": [...]}]}`.
* A row without text is skipped with a warning; a bank with no usable rows fails
  the document (`empty_document` / `parse_error`).
* `marks` accepts numbers or strings; unparseable, non-finite (NaN/Infinity) or
  out-of-range values are `invalid_marks_value` and stay `None`.
* `unit` is a first-class question field (not free-form metadata), so bank rows can
  be tied to syllabus units.
* `source_locator` is `row N` (CSV, header = row 1) or `item N` (JSON).
* Identical rows (same number and text) collapse to one record, and both banks and
  heuristic segmentation are capped at `EXAM_ENGINE_MAX_QUESTIONS` per document
  with a `question_limit_exceeded` warning.
* Bank-only documents have no pages and are quality-scored from question
  confidence instead.

### Images (`image_adapter.py`)

Images are never parsed directly; they are always routed through the OCR
interface and marked `needs_ocr`.

### Adding a format

```python
from pathlib import Path

from exam_engine.ingest.base import (
    ExtractedDocument, ExtractedPage, ExtractionContext, register_adapter,
)


class HtmlAdapter:
    name = "html"
    suffixes: tuple[str, ...] = (".html",)

    def extract(self, path: Path, context: ExtractionContext) -> ExtractedDocument:
        text = path.read_text(encoding="utf-8", errors="replace")
        return ExtractedDocument(
            format=SourceFormat.TXT,  # or extend the enum
            pages=[ExtractedPage(page_number=1, text=text)],
            metadata={"extractor": "html"},
        )


register_adapter(HtmlAdapter())  # before constructing IngestionPipeline
```

Add the suffix to `FORMAT_BY_SUFFIX` in `validation.py` so it passes validation.

## 6. OCR interface

```python
class OCRProvider(Protocol):
    name: str
    def available(self) -> bool: ...
    def extract_text(self, image_bytes: bytes) -> str: ...
```

* Default provider is `none` (`NullOCRProvider`): documents that need OCR are
  stored with `extraction_status=needs_ocr`, empty page text, and an
  `ocr_unavailable` warning. Nothing is fabricated.
* `tesseract` is built in but optional (`pip install -e ".[ocr]"` plus the system
  binary). `available()` never raises.
* PDF pages are rasterised with PyMuPDF at `ocr_dpi` (default 200) before OCR.
* Failures are per page: `ocr_failed` warnings, other pages still processed.

Custom engine:

```python
from exam_engine.ingest.ocr import register_ocr_provider

register_ocr_provider("myocr", lambda: MyProvider())
# then: EXAM_ENGINE_OCR_PROVIDER=myocr  (or --ocr-provider myocr)
```

Tests use a stub provider this way, which is how replaceability is verified.

## 7. Question segmentation

`ingest/segmentation.py` is a pure function of pages — no database, no network.

**Markers accepted** (line start): `Q1.`, `Q.1)`, `Question 3:`, `1.`, `1)`, `1]`,
`(1)`. Markers above 150 are ignored (years, mark totals). Decimal lines (`3.14 …`)
are never markers.

**Monotonic acceptance.** Numbers must advance. A lower number is rejected as a
`numbering_conflict` (duplicate numbering), unless a `SECTION`/`PART` heading
appeared and the number restarts at 1 or 2 (`numbering_reset`). Gaps are recorded
(`numbering_gap`) and **no placeholder question is created**.

**Preamble instructions.** Papers often number their instructions (`1. Answer all
questions`, `2. Time allowed…`) before the first `SECTION`/`PART` heading and then
restart at `1.` for the real questions. Numbered items in that pre-section region
that carry no marks are treated as instructions and dropped, so the real questions
keep their numbering instead of the instructions being stored as questions. The
drop is reported as an `instruction_markers_dropped` note (INFO) with the text of
each removed marker; a preamble item that carries marks is kept as a question.

**Numbered instruction repair.** When a non-instruction marker collides, trailing
markers that block it are removed *if the accepted run contains instruction-like
text*, the collision is resolved, and the removed markers are reported as an
`instruction_markers_dropped` note (INFO) with their text. Real duplicate numbering
is still reported as a conflict — the repair only fires when instruction text sits
in the run being repaired.

**Body assembly.** A question's text runs from its marker to the next marker or
section heading, preserving the original text (only whitespace is normalised,
page-number furniture removed). Markers with no body are skipped with an
`empty_question_body` document-level uncertainty.

**Marks.** Patterns, in order of preference:

| Pattern | Source label |
|---|---|
| `[5 marks]`, `(5 Marks)`, `[5 m]` | `explicit` |
| `5 marks` | `explicit` |
| `Marks: 5`, `marks = 5` | `labelled` |
| trailing `[5]` / `(5)` | `bare_bracket` (confidence penalty) |

The last match wins. Several distinct values produce `multiple_marks_values`
(possibly per-part marks) and a penalty. No marks means `None` + `no_marks_detected`.

**Confidence.** Base `0.92` for `Q…` markers, `0.86` for bare numeric markers, then
penalties: gap −0.08, section reset −0.05, short text −0.30, instruction-like text
−0.25, ambiguous marks −0.10, bare bracket −0.05. Values are clamped to
`[0.05, 0.99]`; `needs_review` is `confidence < EXAM_ENGINE_MIN_QUESTION_CONFIDENCE`
(default 0.6).

**Uncertainty codes** (per question unless stated): `numbering_gap`,
`numbering_reset`, `spans_pages`, `no_marks_detected`, `marks_bare_bracket`,
`multiple_marks_values`, `short_question_text`, `possible_instruction`,
`duplicate_question`, `question_text_truncated`.

**Document-level status**: `segmented` (clean), `partial` (gaps/conflicts/no
questions/duplicates/low confidence), `unsegmented` (no markers, zero questions).
INFO-level notes such as `instruction_markers_dropped` do not downgrade the status.
`needs_review` is also set for `possible_instruction`, `short_question_text` and
`multiple_marks_values`, even when the numeric confidence stays above the threshold.
`source_locator` is `page 2, lines 4-6` or `pages 1-2` when a question spans pages.

Known weak spots: sub-parts `(a)/(i)`, two-column layouts, questions numbered with
letters, and unmarked preamble questions in a sectioned paper (dropped as
instructions and reported, never silently).

## 8. Syllabus segmentation

`ingest/syllabus_segmenter.py` recognises `UNIT`, `MODULE` and `CHAPTER` headings
with roman or arabic numbers (`Unit I`, `MODULE 3:`, `Chapter 4 –`). For each unit it
extracts:

* `title` — heading remainder (hours stripped) or the first body line;
* `hours` — `(8 hours)`, `10 hrs`, `12 periods`, `lecture hours`, a numeric table
  cell, or a bare count pushed into a layout column (`Unit I: Data Structures   12`);
  the two-space column gap is required, so `Unit 3: HTML 5` keeps `5` in the title;
* `topics` — body lines with bullets and numbering stripped, split on `;` and `|`
  (DOCX table cells); comma lists are split only when the line has no colon and
  every part starts with a capital letter, so
  `traversals: preorder, inorder, postorder` stays intact;
* `raw_text` — the untouched unit block for traceability.

DOCX tables flattened to `Unit 1 | Foundations | 8` are understood: a numeric last
cell in 0..200 becomes `hours`, the first cell becomes the title and any middle
cells become topic text. Identical unit ids are collapsed (with an INFO note) rather
than failing the document on a primary-key collision.

Numbering gaps and duplicate unit numbers are flagged; no unit is invented.
Syllabus tables flattened from DOCX (`Unit 1 | Trees | 8`) are handled by the
`|` separator and `lstrip("|")`.

## 9. Quality and duplicates

**Quality score** (`quality.py`, `0..1`) penalises: whitespace-only text, pages
below `min_page_chars` (`low_text_yield`), image pages without text
(`scanned_pages`), control bytes, replacement characters (`\ufffd`), long repeated
character runs, very sparse text and mojibake markers. Flags are stored on the
document and a `low_quality_text` warning is emitted. Quality is advisory: it
never deletes content.

**Document duplicates**

| Signal | Behaviour |
|---|---|
| identical `sha256` for the same course | policy `skip` (default) / `replace` / `fail` |
| identical normalised text, different bytes | informational `duplicate_document` warning; both kept |
| same bytes in a different course | independent document (course isolation) |

**Question duplicates** are detected by a fingerprint of normalised text
(case, spacing, punctuation ignored; digits and math operators kept) within the
course. The later question gets `duplicate_of` and a `duplicate_question`
uncertainty. Nothing is deleted — repeated questions can be legitimate.

## 10. Persistence

SQLite via SQLAlchemy 2.0. Migrations are plain SQL files applied once and
checksum-verified; editing an applied migration is an error.

```
courses ──< documents ──< pages
   │            │  └────< questions (course_id also denormalised for querying)
   │            └────< syllabi ──< syllabus_units
   │
dataset_sources ──< documents
ingestion_runs ──< ingestion_results
```

* `courses` — one row per offering; unique-ish identity on
  (institution, code, year, version) with conflict warnings if a second slug
  claims the same offering.
* `documents` — one row per (course, content) pair, with provenance, hashes,
  extraction status, quality score/flags, relative path, metadata, warnings.
* `pages` — per-page text, method (`embedded_text`, `plain_text`, `structured`,
  `ocr`, `none`), image/OCR flags.
* `questions` — number, section, unit, text, marks + source, page, locator,
  confidence, review flag, uncertainties, fingerprint, duplicate pointer, metadata
  (answer key from banks is kept as opaque metadata).
* `syllabi` / `syllabus_units` — unit label/number/title/hours/topics/raw text.
* `ingestion_runs` / `ingestion_results` — durable per-document reports.

`PRAGMA foreign_keys=ON` is set on every connection, so deleting a document
cascades to its pages, questions and syllabi. Deleting a course cascades to its
documents.

All SQL goes through repositories; values are always bound parameters.

## 11. Batch semantics

* Each document is one transaction: parse → validate → build → persist. A failure
  leaves no partial rows for that document.
* `--dry-run` parses, segments and reports but writes nothing (no run row either).
* `--on-duplicate skip|replace|fail` selects duplicate behaviour.
* `--allow-external` allows paths outside the manifest directory (ad-hoc use).
* The CLI exits `1` when any document failed, after printing the full report.
* `exam-engine run <run_id>` replays a stored report; results include per-document
  status, counts, errors and warnings.

## 12. Security and reliability

* Path containment for manifest inputs; absolute paths only by explicit opt-in.
* Extension allowlist plus magic-byte verification (a renamed `.exe` is rejected).
* Size, page, character and question caps; PDFs are inspected before extraction.
* Document content is never executed: no macros, no embedded objects, no shell
  calls. OCR runs on rendered images only.
* Database access is parameterised; manifests are validated with
  `extra="forbid"` to catch typos that would otherwise silently drop provenance.
* Parse failures are caught per document and reported with a stable error code
  (`ERROR_CODES` in `models/enums.py`); unexpected exceptions are logged with a
  traceback and surfaced as `unexpected_error`/`parse_error`, never swallowed.
* Absolute filesystem paths are not stored on documents (`relative_path` is
  relative to the dataset root, `None` for ad-hoc external files).

## 13. Verification

```bash
python -m pytest -q                                  # 139 tests
python -m pytest --cov=exam_engine --cov-report=term-missing
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m mypy
```

What the suite covers:

| Area | Tests |
|---|---|
| Models and ids | `tests/test_models.py` |
| Normalisation / fingerprints | `tests/test_normalize.py` |
| File validation and traversal | `tests/test_validation.py` |
| Every adapter (incl. tables, encodings, banks, images) | `tests/test_adapters.py` |
| Question segmentation, marks, uncertainty | `tests/test_segmentation.py` |
| Syllabus units/topics/hours | `tests/test_syllabus_segmenter.py` |
| Quality and duplicates | `tests/test_quality.py` |
| Migrations, drift, repositories, cascades | `tests/test_storage.py` |
| Batch, isolation, OCR, partial failure | `tests/test_pipeline.py` |
| CLI | `tests/test_cli.py` |
| Sample dataset artefact | `tests/test_sample_dataset.py` |

These tests establish plumbing correctness on synthetic fixtures. They are not an
accuracy benchmark for the segmentation heuristics or for later NLP phases.

## 14. Extending safely

1. **New format** — add an adapter and register it; add its suffix to
   `FORMAT_BY_SUFFIX`.
2. **New OCR engine** — implement `OCRProvider`, register it, select via env/CLI.
3. **Schema change** — add the next numbered migration (`0003_*.sql`; `0002` added
   `questions.unit`), update `storage/schema.py`, keep the drift test green. Never
   edit an applied migration — checksums reject it.
4. **New extracted field** — add it to the Pydantic model, the mapper, the ORM row
   and a migration; populate it in the pipeline, never by post-hoc mutation.
5. **New uncertainty** — use a stable snake_case code and include a human message;
   downstream phases can filter on codes without re-parsing text.
