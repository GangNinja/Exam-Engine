# Phase 11 — Advanced Assessment Intelligence

Question mutation, adversarial question generation and evidence-bounded
misconception mining, built on the Phase 1–10 corpus, graph, similarity and
generation infrastructure.

**Status:** complete. All three capabilities are implemented, tested offline and
wired into the CLI. No dashboard, no automatic grading and no personal data are part
of this phase.

---

## 1. Objectives and what "done" means here

| Capability | Objective | What the implementation refuses to claim |
|---|---|---|
| Question mutation | Derive meaningful variants of a stored question while keeping the learning objective, concept and answer properties the strategy promises | That two variants mean the same thing because they are similar |
| Adversarial generation | Write challenging but fair questions from real course material | That a generated challenge is fair, unambiguous or correctly graded without review |
| Misconception mining | Report candidate misunderstandings in stored answers, with the evidence for each | That an incorrect answer is a misconception, or that a small sample describes a population |

Three principles run through the phase:

1. **A strategy declares what it can keep invariant.** A mutation request that
   demands a property the strategy cannot preserve is rejected, not silently
   degraded.
2. **Every claim carries its evidence and its uncertainty.** Findings name the rule
   that fired, the excerpt it fired on and what could not be established.
3. **Nothing is approved automatically.** Generated, mutated and challenged questions
   start `pending_review`; mined patterns start `candidate`.

---

## 2. Architecture and integration points

```
advanced/
  source.py         AdvancedError (stable codes) + SourceQuestion/EvidenceContext views
  mutation.py       strategy registry, invariants, deterministic variant builders
  adversarial.py    challenge registry, fair question builders, rubric generation
  misconception.py  response→signal detectors, support levels, record assembly
  verification.py   Phase 11 checks layered on Phase 10 validation
  service.py        orchestration: scope, assembly, dedupe, persistence, review
cli_advanced.py     advanced capabilities/mutate/adversarial/variants/show/review,
                    advanced responses …, advanced misconceptions …
models/advanced.py  requests, candidates, responses, misconception records/reports
storage/migrations/0008_advanced_assessment.sql
storage/migrations/0009_response_identified.sql
```

Reused rather than re-implemented:

| Need | Reused from |
|---|---|
| Course/syllabus/concept resolution and evidence passages | Phase 10 `QuestionGenerationService.load_scope` (new public `ScopeContext`) |
| Structural, alignment, answerability, MCQ, clarity and cognitive checks | Phase 10 `validate_question` |
| Duplicate/near-duplicate decisions and Phase 3 reasoning profiles | Phase 10 `dedupe` |
| Question storage, review history, revision safety and export | Phase 10 `GenerationRepository` |
| Question type, Bloom and difficulty estimates | Phase 3 classification |
| Concept identity, related concepts and prerequisites | Phase 2 concepts, Phase 5 graph nodes/edges |
| Answer text fingerprints | Phase 1 `fingerprint_text` |

`GeneratedQuestion` gained three fields (`origin`, `source_question_id`,
`strategy`) and `GenerationBatch` gained `capability`; both default to their Phase 10
values, so existing rows and queries are unaffected. Phase 11 therefore writes to the
same tables — one storage surface, one review workflow, one export path — and
`generation list`, `generation show` and `generation review` still see the variants.

### Isolation between capabilities

Each capability is an independent service call. A rejected mutation leaves other
variants of the same batch intact, an unavailable challenge strategy is recorded as a
rejection on its batch, and a misconception analysis failure writes no response and
no question. No capability reads another's result to proceed.

---

## 3. Question mutation

### 3.1 Strategies and their invariants

| Strategy | Formats | Invariants it claims | Changes answer? |
|---|---|---|---|
| `rephrase` | any | objective, concepts, answer key, task, single correct answer | no |
| `scenario` | any | objective, concepts, answer key, task, single correct answer | no |
| `reorder` | enumerated questions | objective, concepts, answer key, task | yes (order) |
| `application` | long-form formats | objective, concepts | yes |
| `numerical` | `numerical` | objective, concepts | yes |
| `difficulty` | any | objective, concepts | yes |
| `cognitive` | long-form formats | concepts | yes |
| `related_concept` | any | none (concept changes by design) | yes |
| `distractor` | `multiple_choice` | objective, concepts, answer key, single correct answer | no |

`reorder` needs enumerated sub-tasks, `application` needs a quoted course passage,
`numerical` needs a substitutable number, `related_concept` and `distractor` need
sibling concepts in the same unit, and `distractor` needs a source option marked
correct. A strategy whose inputs are absent is refused with a code
(`no_reorderable_parts`, `insufficient_mutation_context`, `no_numeric_parameters`,
`no_related_concept`, `insufficient_distractors`, `invalid_multiple_choice_source`).

### 3.2 Request validation

`MutationRequest` requires a course, a question id, and either explicit strategies or
none (the defaults are chosen from what the material supports). It rejects duplicate
strategies, a count above `EXAM_ENGINE_Q11_MAX_VARIANTS`, an unknown or foreign
question (`unknown_source_question`, `course_mismatch`), a question from another
syllabus version (`syllabus_mismatch`) and an historical question that Phase 3 never
mapped to a unit (`source_unmapped`).

`preserve_objective`, `preserve_concept`, `preserve_answer` and
`allow_format_change` state the caller's promises. They only apply to strategies that
claim the invariant: `related_concept` changes the objective by design, so asking for
objective preservation does not make it an error — it simply never promised one.

`format` is the optional constraint on the response format. Because no strategy
converts between formats (an open question cannot become a multiple-choice item
without inventing options), a constraint that disagrees with the source question is
refused with `format_constraint_mismatch` before any variant is built.

### 3.3 What each variant records

Original question id, original and intended learning objective, original and variant
concept ids/labels, the variant text, strategy, difficulty and Bloom (requested and
estimated), source references, generation metadata (index, invariants, changed
properties), validation result, review status, warnings and limitations.

Answer handling is explicit:

* `rephrase`, `scenario`, `distractor`, `reorder` carry the original answer key
  forward, and a changed key is a **rejection** when preservation was requested;
* `numerical`, `difficulty`, `cognitive`, `related_concept` and `application` drop it.
  The limitation text says the answer must be recomputed or rewritten, and the
  candidate is reported `needs_review` — never `validated`.

A carried-over key is only `stale_answer_key` when the source actually had a key.
Varying a question whose origin never had one produces `answer_equivalence_unverified`
instead, because there is no key that could have gone stale.

### 3.4 Validation statuses

| Status | Meaning here |
|---|---|
| `VALIDATED` | every checkable, promised property held — not a claim of educational correctness |
| `NEEDS_REVIEW` | the question is usable but a judgement is outstanding (dropped key, unverified equivalence, label disagreement) |
| `REJECTED` | a promised property was violated or a hard Phase 10 check failed; excluded from the batch and reported as a rejection |
| `VALIDATION_INCOMPLETE` | only informational findings; treated as needing a reviewer |

Phase 10's `missing_answer_key` error is downgraded to a review warning for
answer-changing strategies, because a derived variant is expected to have no key until
one is written. The finding keeps its code so the downgrade is visible in reports.

### 3.5 Similarity is a signal, not evidence

Near-duplicate findings against the source and against sibling variants are recorded
in `batch.duplicates` with their score and are **kept**, because a variant is supposed
to resemble its origin. Only an identical normalised text (`duplicate_exact`) is
rejected. Similarity scores never substitute for the answer-equivalence check, which
is reported as `answer_equivalence_unverified` whenever the answer changed.

---

## 4. Adversarial question generation

### 4.1 Challenge strategies

| Strategy | Reasoning tested | Needs |
|---|---|---|
| `unfamiliar_application` | transfer of a definition to an unseen case | passage |
| `transfer` | which part of an explanation is general | passage, sibling |
| `multi_step` | sequencing state → operation → check | passage |
| `false_assumption` | separating a condition of use from a statement | passage |
| `error_diagnosis` | comparing a partial answer with requirements | passage |
| `competing_approach` | judging alternatives against a criterion | passage |
| `boundary` | where a rule stops applying | passage |
| `counterexample` | whether a claim is genuinely universal | passage |
| `solution_evaluation` | assessing quality against criteria | passage |
| `missing_prerequisite` | dependency between ideas | passage, in-syllabus prerequisite |
| `edge_case` | behaviour at the limits | passage |
| `distinguish_concepts` | discriminating neighbouring ideas | passage, sibling |
| `why_wrong` | why a shortcut does not satisfy the requirement | passage |

Only real document text grounds a challenge. A syllabus concept label is an anchor,
not material, so thin label-only passages are filtered out before generation; when
nothing remains the request fails with `insufficient_evidence` or
`no_supported_strategy` instead of quoting a bare term as a "situation".

### 4.2 Fairness rules

Every challenge is built to satisfy, and then checked for:

* the task is stated in full and the material it depends on is quoted
  (`adversarial.context_supplied`);
* an open-ended question carries a model answer or a rubric
  (`adversarial.grading_material`, `adversarial.rubric_depth`);
* the answer is not printed in the student-facing text
  (`adversarial.answer_not_revealed`);
* nothing outside the declared syllabus scope is required unless the request allows it
  (`adversarial.scope`, `out_of_scope_concept`);
* one task is asked, not several (`adversarial.single_task` for three or more `?`);
* no "trick question" framing unless the strategy evaluates a claim
  (`adversarial.fair_wording`);
* the stated difficulty is consistent with the Bloom level
  (`adversarial.difficulty_justification`).

Where an incorrect or incomplete answer is shown, it is a *constructed* prompt
("an answer says only: …", "a claim is made …") attached to a real quote; course text
is never presented as a wrong answer.

### 4.3 Teacher-facing material

The rubric is stored in `solution_steps` and also copied into
`generation_metadata.teacher_notes`. The reference passage is stored in
`explanation` and `source_references`. `answer_key` stays `None`: the system did not
solve the task, so it does not invent a model answer.

Because an open-ended challenge has no answer key, its Phase 10 verdict is
`answer_key_not_derived` plus any label disagreement, so adversarial questions reach
`needs_review` by design. That is the honest steady state, not a defect.

---

## 5. Misconception mining

### 5.1 Inputs

* stored responses (`student_responses`), imported from a JSON file;
* the question's reference answer (`answer_key`) or grading rubric
  (`solution_steps`), and its concept mapping and marks;
* sibling concept labels for the same unit and in-syllabus prerequisites from the
  graph.

Responses for questions the course does not own are refused at import
(`unknown_question`), and a question with no concept mapping or no reference answer is
discarded rather than mined.

### 5.2 Detection rules

| Kind | Rule (marker) | Observable condition |
|---|---|---|
| `concept_confusion` | `sibling_concept_dominates_answer` | the answer discusses a sibling concept and never the asked concept |
| `incorrect_definition` | `definition_frame_without_reference_terms` | a definition is offered with under 25 % coverage of reference terms |
| `formula_misapplication` | `numeric_result_differs_from_reference` | reference is formula-like with a value; the answer states a different value |
| `missing_reasoning_step` | `terms_without_justification` | reference terms appear, no causal connective, answer is wrong |
| `partial_understanding` | `partial_term_coverage` | response is marked partially correct and covers reference terms |
| `invalid_assumption` | `unconditional_claim_against_conditional_reference` | absolute quantifier in the answer, condition in the reference, answer wrong |
| `procedural_error` | `step_sequence_omits_required_term` | explicit step markers, under 50 % term coverage, a required term missing |
| `terminology_misuse` | `term_variant_instead_of_reference_term` | a near-variant of a reference term is used and the answer is wrong |
| `prerequisite_gap` | `prerequisite_absent_from_answer_but_required` | the reference relies on a prerequisite the answer never mentions |

`unclear_or_unrelated` is **not** inferred: such answers are discarded with a reason
(`empty_answer`, `answer_too_short`, `no_detectable_overlap`, `response_correct`,
`missing_reference_answer`, `missing_concept_mapping`, `unknown_question`,
`no_supported_signal`). Discards are reported, not hidden.

### 5.3 Record schema

`schema_version`, `misconception_id` (deterministic over course, syllabus, kind and
concept scope), `course_id`, `syllabus_id`, `kind`, `label`, `description`,
`concept_ids`, `concept_labels`, `question_ids`, `evidence[]`, `support_count`,
`distinct_questions`, `distinct_groups`, `detection_method`, `support_level`,
`confidence`, `suggested_correction`, `follow_up_questions`, `review_status`,
`review_history`, `limitations`, timestamps. Each evidence entry holds
`response_id`, `question_id`, a bounded `excerpt`, the `marker`, the `method` version
and a weight.

### 5.4 Support levels and confidence

| Level | Meaning |
|---|---|
| `single_observation` | one independent response — an observation, never a pattern |
| `candidate` | at least `min_support` responses but short of the recurring thresholds |
| `recurring` | at least `EXAM_ENGINE_Q11_RECURRING_SUPPORT` responses **and** at least `EXAM_ENGINE_Q11_RECURRING_QUESTIONS` distinct questions |

Support counts distinct response ids after fingerprint de-duplication; two copies of one
answer count once, and two students who happen to answer identically stay independent
because the student reference is part of the fingerprint. Confidence rises with support
and question diversity but is capped; a below-threshold pattern still produces a record
*and* an explicit `misconception_below_support_threshold` warning.

Independence cannot be assumed. The import records whether each answer carried a
student or group reference (the fact, never the value), and an analysis in which no
response did reports `independent_responses_unverifiable` with a warning severity and a
matching report limitation: the counts are then answer patterns, not verified
independent responses.

### 5.5 Privacy safeguards

* A `student_ref` / `group_ref` is hashed on import (`response_ref`); the plain value
  is never stored, logged, reported or exported. Only a boolean saying that a reference
  was supplied is stored (migration `0009`), so independence can be judged later.
* Only a bounded excerpt of an answer is kept in evidence, not the whole answer. The
  configured length is capped at the 240 characters the evidence schema accepts, and the
  effective value is reported in `parameters`.
* Reports and exports contain hashes only; a test asserts a supplied name and group
  never appear in serialised output.
* No record describes a learner: every description is about the answer pattern, and
  record limitations deny any population claim.

---

## 6. CLI

```bash
python -m exam_engine advanced capabilities

# A. mutation
python -m exam_engine advanced mutate --course demo-cs201-2024-v1 \
    --question gq_… --strategy rephrase --strategy scenario --count 2
python -m exam_engine advanced mutate --course demo-cs201-2024-v1 \
    --question gq_… --strategy rephrase --format explanation   # constraint must match source
python -m exam_engine advanced variants --course demo-cs201-2024-v1 --origin mutation
python -m exam_engine advanced show gq_…
python -m exam_engine advanced review gq_… --status approved --note "checked by hand"

# B. adversarial
python -m exam_engine advanced adversarial --course demo-cs201-2024-v1 \
    --concept "Stacks and queues" --count 3 --strategy multi_step

# C. misconception mining
python -m exam_engine advanced responses import --course demo-cs201-2024-v1 \
    --file responses.json --source "2024 midterm export"
python -m exam_engine advanced responses list --course demo-cs201-2024-v1
python -m exam_engine advanced misconceptions analyse --course demo-cs201-2024-v1
python -m exam_engine advanced misconceptions list --course demo-cs201-2024-v1 --status candidate
python -m exam_engine advanced misconceptions show mc_…
python -m exam_engine advanced misconceptions review mc_… --status approved --note "matched rubric"
python -m exam_engine advanced misconceptions export --course demo-cs201-2024-v1 \
    --syllabus syl_… --out reports/misconceptions.json
```

All commands accept `--json`, validate before touching the database, and report
failures as `ERROR: [code] message` with exit code 1 rather than a traceback.

Response import file format:

```json
{"responses": [
  {"question_id": "q_…", "answer": "A queue removes the item added first…",
   "marks_awarded": 0, "marks_available": 4, "student_ref": "student-17",
   "group_ref": "class-A"}
]}
```

`correctness` may be supplied (`correct`, `partially_correct`, `incorrect`,
`unclear`); when it is absent it is inferred from marks and the import reports how many
were inferred.

---

## 7. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAM_ENGINE_Q11_MAX_VARIANTS` | `10` | Variants or challenges per request |
| `EXAM_ENGINE_Q11_MAX_RESPONSES` | `5000` | Responses read per analysis run |
| `EXAM_ENGINE_Q11_MIN_SUPPORT` | `2` | Minimum independent responses for a candidate |
| `EXAM_ENGINE_Q11_RECURRING_SUPPORT` | `3` | Responses required for `recurring` |
| `EXAM_ENGINE_Q11_RECURRING_QUESTIONS` | `2` | Distinct questions required for `recurring` |
| `EXAM_ENGINE_Q11_MIN_ANSWER_WORDS` | `3` | Below this, an answer is unreadable |
| `EXAM_ENGINE_Q11_EXCERPT_CHARS` | `240` | Evidence excerpt length (capped at 240) |
| `EXAM_ENGINE_Q11_MIN_REFERENCE_OVERLAP` | `0.08` | Minimum reference overlap recorded in reports |
| `EXAM_ENGINE_Q11_MAX_EVIDENCE` | `6` | Evidence excerpts kept per record |

Mutation and adversarial generation also read the Phase 10 settings
(`EXAM_ENGINE_Q10_*`) for evidence limits, document kinds and duplicate thresholds.

---

## 8. Testing

```bash
python -m pytest tests/test_advanced.py tests/test_misconception.py tests/test_advanced_cli.py
python -m pytest                     # full suite, including Phases 1–10
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy
```

Recorded results for the completed phase:

| Command | Result |
|---|---|
| `python -m pytest tests/test_advanced.py tests/test_misconception.py tests/test_advanced_cli.py` | 79 passed |
| `python -m pytest` | 602 passed |
| `python -m ruff check src tests` | all checks passed |
| `python -m ruff format --check src tests` | 155 files already formatted |
| `python -m mypy` | no issues in 114 source files |

Coverage maps onto the phase requirements: meaning-preserving rephrasing, scenario and
numerical mutation, invalid requests, missing sources, objective/concept/answer
preservation, answer-key consistency, duplicate rejection, unsupported strategies and
partial batch failures; valid application, multi-step, error-diagnosis and edge-case
challenges, missing context, ambiguity, out-of-scope prerequisites, answer-key and
distractor validation and review status; single observations, repeated errors,
duplicate imports, related-concept confusion, partial credit, missing reference
answers, malformed answers, missing concept mappings, insufficient support, anonymity
and provenance; and course/syllabus isolation, persistence integrity, repeatable
analysis and Phase 1–10 regressions.

Every test runs offline against synthetic fixtures. No provider, network or paid API is
used; Phase 11 itself makes no model-provider call, so Phase 10's provider-timeout and
malformed-response tests remain the coverage for that boundary.

---

## 9. Known limitations

* Mutation is **textual and deterministic**: rephrasing wraps the original task in a
  different frame, scenario mutation adds a setting, and neither rewrites the content.
  A linguistically novel rewrite needs the Phase 10 model provider or a human author.
* `application` and `cognitive` variants drop the answer key, and `difficulty` variants
  change the demand without a matching key. All three are review material until a
  reviewer writes the key.
* Numerical mutation changes one parameter by a small deterministic step. It does not
  re-solve the problem, so the variant's answer must be recomputed; the record says so.
* Answer preservation is checked by normalised comparison against the source key, not
  by semantic equivalence. Embedding similarity is never used as proof of meaning.
* Challenge templates are finite and English-only. Each strategy has one construction,
  so repeated requests for one concept and strategy produce the same wording.
* Adversarial questions always carry review flags, because the system supplies a rubric
  and source passage rather than a model answer.
* Misconception detection is lexical rule-matching over English answers to questions
  that already have a reference answer and a concept mapping. Paraphrase, diagrams,
  equations written in prose and code answers are largely invisible to it.
* Support counts depend on the identifiers supplied. If two imports of the same answer
  carry different student references, they are treated as independent; if a reference
  is missing, identical answers collapse and the run reports
  `independent_responses_unverifiable`. The count is described as what it is, never as
  a class-wide rate.
* No labelled benchmark for mutation quality, challenge fairness or misconception
  precision is bundled.

---

## 10. Evaluation plan (software correctness vs educational value)

This phase ships a plan, not an empirical result. Anything below requires labelled data
that is not part of the repository.

**Question mutation**

| Question | Method | Separation |
|---|---|---|
| Learning-objective preservation | expert judgement on a sample of variants per strategy; report agreement | educational |
| Answer consistency | compare each variant's key with the source key and a spot-checked recomputation | educational + software |
| Syllabus relevance | Phase 10 alignment check plus expert review | both |
| Semantic duplication | precision of exact/near findings against a hand-labelled pair set | software |
| Human-review acceptance | approved/rejected ratio per strategy from stored review history | operational |

**Adversarial questions**

Fairness, answerability, difficulty alignment, reasoning quality, rubric consistency and
syllabus alignment are rated by two independent markers per item on a 3-point scale
(adequate / needs work / inadequate), with inter-rater agreement reported. Automated
fairness findings are compared against the markers' verdicts to measure false-positive
and false-negative rates of the checks.

**Misconception mining**

Precision of pattern labels against expert annotation of the same answers, recall
against a hand-built list of known misunderstandings, Cohen's κ for agreement, and a
false-positive audit stratified by response length and question. The effect of sample
size is studied by subsampling known groups; independent-response counting is checked
by seeding duplicates deliberately.

**Data provenance and licensing.** All fixtures in this repository are synthetic and
labelled as such; they validate the pipeline and are not evidence about learners.
Public or appropriately licensed question banks (for example openly licensed
introductory computing course material) may be used for the mutation and challenge
parts, with their licence and provenance recorded in the manifest. Misconception
evaluation needs anonymised, licensed student answers; without them this analysis can be
validated only as software. Personal teaching material remains reserved for Phase 13.

---

## 11. Scope boundaries

Implemented: mutation, adversarial generation, misconception mining, review and export,
plus the migration, models, CLI and tests above.

Not implemented: Phase 13 (personal data integration). The Phase 12 dashboard
integrates these services. Also absent by design: automatic approval, examination
scoring, forecasting, and any inference about a student's ability. Local Practice
answer checks are different: they are transparent lexical feedback over a stored
question, not an assessment of a learner.
