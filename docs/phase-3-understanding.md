# Phase 3 — Question understanding and classification

Phase 3 turns the questions stored by Phase 1 into structured, searchable records:
sub-question spans, explicit material references, course-scoped syllabus
candidates, question-type labels, Bloom candidates, a provisional difficulty
estimate and batched question similarity.

Nothing in this phase is an empirically validated educational measurement. The
outputs are estimates with confidence, evidence and explicit `unknown`/`unmapped`
outcomes. The pipeline never re-parses a source file, never borrows another
course's syllabus, and never invents missing text, marks, answers or metadata.

**In scope:** question structure, syllabus mapping, question-type classification,
Bloom classification, difficulty estimation, similarity/duplicate detection,
persistence, CLI and tests.

**Out of scope (later phases):** exam analytics dashboards, the knowledge graph,
prerequisite discovery, coverage-gap reporting, Exam DNA, concept evolution,
question generation, misconception mining, answer validation and application
integration. The user's own syllabus and paper data remain reserved for Phase 13;
the committed sample is labelled synthetic.

---

## 1. Data flow

```
Phase 1 questions ──────────────► structure (subparts, references)
        │                              │
        ├── current course syllabus ───┼──► course-scoped unit candidates
        │                              │
        └── Phase 2 embeddings ────────┼──► semantic mapping signal
                                       │
                                       ├──► question types (multi-label)
                                       ├──► Bloom candidates
                                       ├──► difficulty estimate
                                       └──► question_analysis summary
                                                  │
Phase 1 questions ──► candidate retrieval ──► similarity pairs
                                                  │
                              understanding_runs (audit report)
```

`UnderstandingService.analyze_course` runs in one course transaction, after
clearing that course's Phase 3 rows. A per-question savepoint prevents one bad
question from leaving partial rows or aborting the rest of the batch. Phase 1
and Phase 2 tables are never modified.

## 2. Question structure

Phase 1 already detects numbered question boundaries, sections, marks and source
locators. Phase 3 reuses those records and adds:

| Signal | Method | What is recorded |
|---|---|---|
| Sub-questions | markers at line start, after `;`, `:`, or sentence punctuation: `(a)`, `a)`, `(iv)`, `i)`, `(2)`, `a.` | label, exact text span, end-of-part marks when stated; implausible marks become an uncertainty, not data |
| Diagram reference | "diagram/figure/flowchart/graph/chart shown/below/given", numbered references | kind, matched phrase, exact span |
| Code reference | "code/program/snippet/pseudocode below", fenced code marker | kind, phrase, span |
| Table reference | "table/dataset below/given" | kind, phrase, span |
| Equation reference | "equation/formula below/given", `equation (3)` | kind, phrase, span |
| Case study reference | "case study"/"case studies", "read the following passage/scenario" | kind, phrase, span |
| Word/sentence counts | deterministic token/sentence split | features for difficulty |

Question boundaries remain the Phase 1 boundaries. The original question text is
unchanged, and every span indexes that stored text exactly, so evidence can be
checked without re-reading the source file. Low upstream confidence, a
`needs_review` flag, or a question with no terminal punctuation produces an
uncertainty instead of being treated as reliable. Phase 1 doubts about a
question (a numbering gap, ambiguous marks, a possible instruction) are carried
into `question_analysis.uncertainties`; Phase 3 does not discard an upstream
boundary doubt merely because the text looks plausible. A trailing marks
annotation such as `[8 marks]` is ignored when checking terminal punctuation,
so a complete question is not reported as truncated merely because the marks
sit after its final sentence.

**What it does not do:** free-text unnumbered papers are not re-segmented. Phase
1 requires numbered markers for prose papers; structured question banks may
contain rows without a number, and those are analysed normally. Multi-column or
handwritten layouts remain a Phase 1 limitation.

## 3. Syllabus and unit mapping

Mapping is always scoped to the question's own course. The service loads only
that course's syllabus records/units, so identical terminology in unrelated
courses cannot cause a cross-course assignment.

Signals are combined in this order:

1. **Explicit unit reference.** `questions.unit` (from a bank) or wording such
   as "from Unit 2" is authoritative. Composite metadata is resolved to its own
   number, so `Unit 2`, `2`, and `Unit 2: Linear Structures` all reach Unit 2. If
   the reference does not resolve within this course, the question stays
   `unmapped`; there is no lexical fallback and no other-course lookup. This
   prevents a typo or a stale bank column from silently mapping to the wrong
   syllabus. In question wording, a lowercase `unit 2` only counts when context
   supports it (start of text, title-cased keyword, or a cue such as
   "from"/"in"), so a measurement phrase like "a unit 2 kg mass" is not turned
   into a syllabus reference. When wording names several units, each is
   resolved: several in-syllabus references become `ambiguous` candidates,
   while any reference outside the syllabus keeps the question `unmapped`
   rather than mapping the resolvable subset.
2. **TF-IDF cosine.** An in-house TF-IDF model compares case-folded, lightly
   lemmatised question terms with the unit's title, topics and body text. Titles
   and named topics are repeated to reflect their curated status.
3. **Topic similarity.** Each unit's individual topics are compared with the
   question; the best topic is attached to the unit candidate when it is a
   stronger signal than the whole-unit score.
4. **Phase 2 embeddings.** Question and syllabus-unit vectors are used only when
   both were produced by the same model id. The default blend is 60% semantic,
   40% TF-IDF. Missing or mismatched vectors fall back to lexical mapping and
   record `embedding_signal_unavailable`.

Candidate scores, matched terms and textual evidence are stored. The top
candidate is selected only if its score reaches `EXAM_ENGINE_Q3_MAPPING_THRESHOLD`
(default `0.18`) and it leads the runner-up by at least
`EXAM_ENGINE_Q3_MAPPING_MARGIN` (default `0.04`). Otherwise the result is
`ambiguous` and up to `EXAM_ENGINE_Q3_MAPPING_CANDIDATES` candidates are kept for
human review. No unit above threshold means `unmapped`; no syllabus at all means
`unmapped` with a `no_syllabus` warning. A question is never forced into a unit.

A match stores its `course_id`, `syllabus_id`, unit label/title, optional topic,
score, rank, method and evidence. Multiple syllabus versions in one course are
kept as separate `syllabus_id`s; if they contain indistinguishable candidates the
question is `ambiguous` rather than silently choosing a version.

## 4. Configurable taxonomy

`src/exam_engine/understanding/taxonomy.py` defines every question-type and
Bloom label with:

* a description shown by `understanding taxonomy`;
* weighted regex cues, each with a short rationale;
* a taxonomy version recorded in every run and label row.

The default (`builtin-1`) covers:

| Question type | Examples of cues |
|---|---|
| `definition` | define, state laws/properties, list/name, "what is a …" |
| `explanation` | explain, describe, discuss, illustrate, why, how |
| `comparison` | compare, contrast, difference between, distinguish |
| `application` | apply, solve, find, trace, demonstrate, show how |
| `numerical` | calculate, compute, numeric quantities/units, arithmetic |
| `scenario` | case study, scenario, suppose, "a company…" |
| `proof` | prove, derive, show that, verify that, formal proof |
| `programming` | write code/pseudocode, implement, language names, complexity |
| `design` | design, architecture, draw, construct, formulate |
| `evaluation` | evaluate, assess, critique, justify, recommend, trade-offs |

The taxonomy can be replaced with `EXAM_ENGINE_Q3_TAXONOMY=/path/to/taxonomy.json`.
The JSON shape is documented by `load_taxonomy`: `question_types` and
`bloom_levels` are objects of label -> `{description, cues: [...]}`; a cue is a
regex string or `{pattern, weight, note}`. Labels must belong to the built-in
enums, weights must be in `(0, 3]`, and every regex is compiled during
validation. An invalid custom file falls back to the built-in taxonomy and adds
`degraded_fallback` to the run report; it is never silently ignored.

### Question-type assignment

All cue weights that fire are summed (capped at 1.0). Every label at or above
`EXAM_ENGINE_Q3_TYPE_THRESHOLD` (default `0.30`) is stored, up to five, ranked by
score. Structural evidence can add weight: a code reference points to
`programming`, a case-study reference to `scenario`, a diagram reference to
`design`, and multiple sub-questions/multi-step wording to `application`.
The highest-ranked label is the primary type. If no label reaches the threshold,
no type is assigned and `unknown_question_type` is recorded with the best score.

`definition` cues deliberately require imperative or definitional wording
("list two…", "what is a…"), not a noun such as "linked list".
`evaluation` can fire on "justify your answer" even when the surrounding verb is
"state", which is why multi-label output is retained rather than collapsing to
one verb.

## 5. Bloom's Taxonomy

Bloom classification uses cue weights plus context and structure. It is not a
verb lookup: for example, "explain why comparison sorting cannot beat
O(n log n)" adds a causal-context weight to `analyze` and subtracts from generic
`understand`, so the causal demand wins rather than the verb "explain".

Other context adjustments include comparison -> `analyze`, numerical -> `apply`,
proof/derivation -> `analyze`, evaluation -> `evaluate`, design -> `create`,
definition -> `remember`, multiple sub-questions/sentences -> `analyze`, and a
case study -> `analyze`/`evaluate`.

All levels at or above `EXAM_ENGINE_Q3_BLOOM_THRESHOLD` (default `0.30`) are
stored, up to four, ranked. Primary confidence reflects both the winning score
and the gap to the runner-up. No level above threshold produces
`unknown_bloom_level`; no level is fabricated. There is no labelled educational
benchmark in this repository, so no accuracy claim is made.

## 6. Difficulty

Difficulty is an **estimate from observable features**, not measured student
difficulty:

| Feature | Source |
|---|---|
| Reasoning demand | primary/high-scoring question type and Bloom level |
| Wording length | word count |
| Structural load | sentence count and number of sub-questions |
| Concept load | distinct case-folded, lemmatised content terms (a proxy, not a verified concept count) |
| Quantitative/derivation demand | numerical/proof type or equation reference |
| Stated marks | only when the source states them; treated as a weak signal |

Weights are 0.35 reasoning, 0.15 wording, 0.15 structure, 0.15 concept load, 0.10
quantitative, and 0.10 marks when marks exist (weights renormalise when they do
not). Bands use `EXAM_ENGINE_Q3_DIFFICULTY_EASY_BELOW` (default `0.32`) and
`EXAM_ENGINE_Q3_DIFFICULTY_HARD_ABOVE` (default `0.60`).

`DifficultyAssessment.estimated` is always `true` in Phase 3. The stored feature
dictionary records every value used, including the raw marks and their
normalised load when marks exist. The explanation lists the feature values and
says explicitly that marks/wording do not establish how hard a question is for
students. Too little evidence (empty text, or no type and no Bloom level)
produces `unknown` with `difficulty_uncertain`, even when marks are present:
marks and wording alone never choose a band. Marks are never defaulted to zero.

## 7. Similarity and duplicate detection

Comparison is course-scoped and batched. Candidate generation uses two indexes:

* an inverted term index over case-folded, lemmatised tokens proposes lexical
  neighbours;
* a nearest-neighbour index over Phase 2 question vectors proposes semantic
  neighbours (only when at least two question vectors from one model exist).

Only the union of those candidate pairs is scored; the service does not score
every pair. Exact groups are always included.

Lexical similarity is the stronger of TF-IDF cosine and token-set Dice overlap.
Dice is included because TF-IDF over-penalises a one-word spelling variation in a
tiny course. Semantic similarity is cosine between Phase 2 vectors. Pair score is
the maximum available signal.

| Kind | Rule |
|---|---|
| `exact` | normalised text is identical (case, whitespace and a leading question marker are ignored) |
| `near` | lexical similarity >= `EXAM_ENGINE_Q3_SIMILARITY_NEAR` (default `0.78`) |
| `semantic` | semantic cosine >= `EXAM_ENGINE_Q3_SIMILARITY_SEMANTIC` (default `0.68`) and both questions have the same *known* primary type and Bloom level |
| `concept_related` | semantic similarity is high but the primary type or Bloom level differs, or the pair reaches only `EXAM_ENGINE_Q3_SIMILARITY_CONCEPT` (default `0.35`) |

`concept_related` is how questions that share a concept but demand materially
different reasoning are kept separate from paraphrases. If either question's
reasoning profile is incomplete (no primary type or Bloom level), a high
semantic score is also stored as `concept_related` with that reason in the
evidence, rather than assuming the two questions ask for the same demand.
Evidence stores the lexical/semantic scores, shared terms, embedding model id
and the differing or missing labels. `top_k` (default 5) keeps each question's strongest relationships, but
exact pairs are always retained; each unordered pair is stored once. The CLI
and docs repeat that similarity is evidence, not proof of duplication: only
`exact` is exact.

## 8. Persistence

Migration `0004_question_understanding.sql` adds:

| Table | Contents |
|---|---|
| `question_analysis` | one summary row per question: hashes, structure counts/spans, mapping outcome, primary labels, confidence |
| `question_unit_matches` | ranked candidate units/topics with syllabus id and evidence |
| `question_types` | all multi-label type candidates |
| `question_bloom` | all Bloom candidates |
| `question_difficulty` | provisional difficulty, feature dict, explanation |
| `question_similarity` | unordered question pairs with kind and lexical/semantic scores |
| `understanding_runs` | status, methods, model ids, totals, config, warnings, errors, duration |

All derived classification rows cascade from questions/courses/units; the
`understanding_runs` audit rows are retained independently. Every course run
replaces its derived rows, so changed question text or a changed syllabus cannot
leave stale classifications. `understanding reset` deletes derived rows but
keeps Phase 1/2 data and run history. The original `questions.text` is never
rewritten.

Classifier provenance is stored in row `method` values: cue methods include the
taxonomy version (for example `cue_rules:builtin-1`), difficulty uses
`observable_features`, and mapping/similarity methods distinguish `tfidf` from
`tfidf+semantic`/`tfidf+embeddings` when the semantic signal was actually used.
Embedding model ids are recorded on the run and in match evidence.

## 9. CLI

```bash
exam-engine understanding analyze [--course ID] [--json]
exam-engine understanding status [--json]
exam-engine understanding questions --course ID [--status mapped|ambiguous|unmapped] [--limit N] [--json]
exam-engine understanding show QUESTION_ID [--json]
exam-engine understanding similar --course ID [--kind exact|near|semantic|concept_related] [--threshold F] [--limit N] [--json]
exam-engine understanding runs [--limit N] [--json]
exam-engine understanding taxonomy [--json]
exam-engine understanding reset (--course ID | --all) [--yes]
```

`analyze` prints per-course totals and warnings; `--json` emits the full report.
`show` prints provenance, structure (including subparts and references), mapping
candidates with evidence, every type/Bloom candidate, the difficulty explanation,
ranked similar questions and uncertainties. `questions` is the quick review
table, and `similar` shows pair scores with truncated question text.

An unknown course makes `analyze` exit `1` with a clean error, not a traceback.
`reset` without `--course` or `--all` exits `2`.

## 10. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_Q3_MAX_QUESTIONS` | `2000` | Questions analysed per course |
| `EXAM_ENGINE_Q3_MAX_SIMILARITY_QUESTIONS` | `2000` | Questions entering similarity |
| `EXAM_ENGINE_Q3_TAXONOMY` | unset | Custom taxonomy JSON path |
| `EXAM_ENGINE_Q3_MAPPING_THRESHOLD` | `0.18` | Minimum unit score to map |
| `EXAM_ENGINE_Q3_MAPPING_MARGIN` | `0.04` | Minimum lead over the runner-up |
| `EXAM_ENGINE_Q3_MAPPING_CANDIDATES` | `3` | Candidates kept for review |
| `EXAM_ENGINE_Q3_MAPPING_SEMANTIC_WEIGHT` | `0.6` | Embedding share when both vectors exist |
| `EXAM_ENGINE_Q3_TYPE_THRESHOLD` | `0.30` | Minimum type score |
| `EXAM_ENGINE_Q3_BLOOM_THRESHOLD` | `0.30` | Minimum Bloom score |
| `EXAM_ENGINE_Q3_DIFFICULTY_EASY_BELOW` | `0.32` | Easy/moderate boundary |
| `EXAM_ENGINE_Q3_DIFFICULTY_HARD_ABOVE` | `0.60` | Moderate/hard boundary |
| `EXAM_ENGINE_Q3_SIMILARITY_NEAR` | `0.78` | Near-duplicate lexical threshold |
| `EXAM_ENGINE_Q3_SIMILARITY_SEMANTIC` | `0.68` | Semantic threshold |
| `EXAM_ENGINE_Q3_SIMILARITY_CONCEPT` | `0.35` | Concept-related threshold |
| `EXAM_ENGINE_Q3_SIMILARITY_TOP_K` | `5` | Strongest relationships per question |
| `EXAM_ENGINE_Q3_SIMILARITY_CANDIDATES` | `30` | Candidate neighbours per question per index |

Phase 2 settings still control embeddings (`EXAM_ENGINE_EMBEDDING_PROVIDER`,
etc.). The default hashing provider runs offline; semantic quality is limited by
the provider that actually ran. A sentence-transformer model requires installation
and an explicit download policy.

## 11. Status and uncertainty

Run status:

* `ok` — analysis completed with no warnings;
* `partial` — completed with warnings, a per-question failure, or a similarity
  stage failure whose classifications were kept;
* `insufficient_data` — no questions to analyse;
* `failed` — course-level failure or every question failed.

Codes used by this phase include `question_analysis_failed`,
`unknown_question_type`, `unknown_bloom_level`, `difficulty_uncertain`,
`no_syllabus`, `unmapped_question`, `ambiguous_mapping`,
`low_extraction_confidence`, `missing_terminal_punctuation`,
`implausible_subpart_marks`, `embedding_signal_unavailable`,
`similarity_fallback`, `analysis_truncated` and `degraded_fallback`.

## 12. Verification

`tests/test_understanding*.py` cover:

* numbered bank questions and an unnumbered bank row;
* subparts with exact spans and marks, references, low-confidence text,
  missing punctuation and Phase 1 boundary doubts carried forward;
* explicit/lexical mapping, composite unit metadata, the measurement-phrase
  guard, multiple references, an unresolved explicit unit that stays unmapped,
  ambiguity, no syllabus, shared-term evidence, course isolation and
  syllabus-version isolation;
* multi-label types, Bloom context, unknown classifications and configurable
  taxonomy validation;
* difficulty ordering, low-confidence/unknown difficulty and missing marks;
* exact, near, semantic and concept-related similarity, incomplete reasoning
  profiles, lexical fallback and single-question input;
* service persistence, rerun replacement after changed text, truncation,
  one-question failure handling, Phase 2 embedding integration and reset;
* CLI JSON/plain output, status, show, similar, taxonomy, runs and exit codes.

Run all checks with:

```bash
python -m pytest -q
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m mypy
```

The suite had 285 tests when Phase 3 was completed and now has 309 with the
Phase 4 analytics tests included; all pass. These tests validate software behaviour and
determinism. They are **not** a labelled educational benchmark: no precision,
recall, F1 or accuracy is reported for type, Bloom, difficulty or mapping because
no suitable ground-truth set is bundled. Synthetic fixtures prove that the code
handles cases; they do not prove pedagogical correctness.

## 13. Known limitations

* Question boundaries still come from Phase 1. Free-text papers without numbered
  markers are not segmented in Phase 3, and multi-column/handwriting remains out
  of scope.
* Sub-part detection is pattern-based; unusual numbering or nested sub-parts can
  be missed, and a sub-part's end mark is taken from the text before the next
  marker.
* Mapping is lexical/semantic similarity plus explicit references. Synonyms not
  covered by the syllabus text (for example "time complexity" vs "worst-case
  analysis") can miss; a question can legitimately remain unmapped. A lowercase
  "unit 2" in question wording without supporting context is ignored rather
  than risk treating a measurement phrase as a syllabus reference; an explicit
  `questions.unit` field is unaffected.
* Mapping uses a single embedding model per course run (the one with the most
  question + unit coverage). Vectors from different model ids are never mixed.
* Bloom and type decisions are transparent cue rules, not trained models. A
  multi-demand question can carry several labels, and primary selection is
  score-based. Cross-domain wording can still trigger a label (generic "design"
  or "implement" in a non-programming course); weights are configurable per
  deployment. Similarity only calls two questions `semantic` when both
  reasoning profiles are known and equal; unknown profiles fall back to
  `concept_related` instead of assuming equivalent demand.
* Difficulty is uncalibrated. Marks are a weak signal and are never treated as a
  measurement; no student-response data exists in this phase.
* Similarity thresholds are defaults for a small synthetic corpus. They are
  configurable, but near/concept boundaries need tuning on real, labelled
  material before operational use.
* Candidate retrieval is approximate by design; a highly paraphrased question
  that is not among a question's proposed neighbours can be missed.
* Only English cues are bundled. Questions in other languages can still be
  stored and compared character/token-wise, but classification is unreliable.
* The optional neural embedding and spaCy paths are not exercised in CI because
  they require model downloads.
* The Phase 13 boundary is intact: no personal syllabus or paper data is bundled,
  and the sample dataset remains synthetic.
