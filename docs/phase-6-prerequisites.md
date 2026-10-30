# Phase 6 — Prerequisite Discovery & Learning Paths

Phase 6 adds two replaceable services over the Phase 5 knowledge graph:

* `PrerequisiteDiscoveryService` proposes reviewable `PREREQUISITE_OF` candidates
  from syllabus order, existing relationships, question associations and semantic
  relatedness.
* `LearningPathService` orders a target concept, topic or unit by explicit
  dependency edges and explains every included prerequisite.

This phase does **not** implement coverage-gap detection, Exam DNA, concept
evolution, question generation, misconception mining, or dashboard integration.
It also does not decide that a syllabus order is pedagogically correct.

## Storage decision

Phase 6 adds no database and no migration. It reuses the Phase 5 `graph_nodes` and
`graph_edges` tables through `GraphService`/`GraphRepository`, because those tables
already provide:

* stable concept/question identity and course/syllabus scope;
* indexed lookup by node id, course, syllabus, relationship and status;
* evidence, method, document/page and review history on every edge;
* one logical edge per `(source, PREREQUISITE_OF, target)`.

A separate graph database would duplicate identity and review semantics for a
subgraph that is small and only traversed in Python. The chosen abstraction stays
replaceable: the discovery and path services depend on `GraphService`, not on SQL.

The reused node types are `INSTITUTION`, `SUBJECT`, `COURSE`, `SYLLABUS`, `UNIT`,
`TOPIC`, `CONCEPT`, `QUESTION`, `SOURCE_DOCUMENT` and `EXAM_SESSION`. The Phase 5
relationship taxonomy remains unchanged (`CONTAINS`, `COVERS`, `PREREQUISITE_OF`,
`RELATED_TO`, `SIMILAR_TO`, `APPEARS_IN`, `DERIVED_FROM`, `BELONGS_TO`,
`ASSESSES`); Phase 6 writes only `PREREQUISITE_OF` edges and reads `CONTAINS`,
`COVERS`, `ASSESSES` and `RELATED_TO` for composition and supporting evidence.
Concept identity, alias preservation and review-only duplicate detection remain
Phase 5 responsibilities; Phase 6 never merges concepts.

### What is stored on a candidate edge

`graph_edges` keeps the normal columns (`status`, `confidence`, `evidence`,
`source_document_id`, `source_page`, `method`, `model_version`). Phase 6 adds
structured provenance inside `metadata_json`:

| Key | Meaning |
|---|---|
| `origin` | `phase6_discovery`; marks a derived edge that may be preserved on rebuild |
| `algorithm`, `algorithm_version`, `search_version` | Reproducibility labels (`phase6_prerequisite_discovery`, `phase6.v1`) |
| `validated`, `review_required` | Explicit/reviewed edges are `true`/`false`; candidates are `false`/`true` |
| `evidence_sources`, `evidence_methods` | Distinct sources/methods that contributed |
| `syllabus_ids`, `unit_ids`, `document_ids`, `distances` | Scope and ordering provenance (lists when merged) |
| `limitations` | Method-specific caveats |
| `prerequisite_evidence` | Serialized evidence objects: source, method, detail, document/page, method version, confidence (only when the source scores), limitations, provenance |
| `configuration` | Order window, cross-unit switch, co-occurrence thresholds/cap and evidence switches used |
| `disabled`, `conflict_with`, `action_history` | Review/override state and audit trail |
| `preserved_on_rebuild`, `preserved_at` | Set when a derived candidate survives a Phase 5 rebuild |

A later candidate proposal cannot downgrade an explicit/reviewed edge's
`validated` flags or a manually disabled edge's `disabled` flag. Re-running
discovery is idempotent at the node/edge level: the same edge id is updated, not
duplicated.

## Evidence sources and inference rules

Direction is never invented from association. A candidate is created only when
syllabus order (or an existing relationship) supplies direction. Co-occurrence and
semantic relatedness contribute **supporting evidence** to that candidate.

| Source label | Rule | Direction? | Confidence |
|---|---|---|---|
| `syllabus_explicit` | Existing `explicit` `PREREQUISITE_OF` edge stated by syllabus metadata | yes | **none** |
| `preexisting_relationship` | Existing reviewed/verified or other graph `PREREQUISITE_OF` edge | yes | preserved from the existing edge; explicit edges have none |
| `syllabus_order` | Two topics in one syllabus unit, up to `order_window` positions apart (default 2) | weak yes | **none**; order is not proof |
| `syllabus_containment` | Optional last concept of one unit → first concept of the next (`EXAM_ENGINE_Q6_CROSS_UNIT_ORDER`) | very weak yes | **none** |
| `related_concept_similarity` | Existing Phase 3 `RELATED_TO` edge between the pair | support only | original Phase 3 score remains on the evidence item; the new candidate has no score |
| `question_concept_mapping` | One or more questions assess both concepts | support only | **none** |
| `question_cooccurrence` | Shared question support ≥ `cooccurrence_min_questions` (2) and Jaccard ≥ `cooccurrence_min_jaccard` (0.34); capped by `max_cooccurrence_candidates` | support only | **none** |

The co-occurrence cap applies only to the extra `question_cooccurrence` signal.
`question_concept_mapping` evidence is retained for every ordered pair, so the
cap cannot hide the fact that the same questions mention both concepts.

Explicit prerequisites come from Phase 5's syllabus-metadata path
(`unit.metadata.prerequisites` / `prerequisite_of`). Phase 6 does not parse raw
syllabus prose for prerequisite sentences, and it does not promote an explicit
edge to a different status. A bare prerequisite list without a target topic is
still reported by Phase 5 as ambiguous and is not guessed.

### Candidate data contract

Every discovered relationship records:

* source and target concept ids/labels (and their course/syllabus scope);
* status (`candidate` or the stronger status of a merged existing edge);
* method and algorithm/config versions;
* evidence items with document/page and limitations;
* `review_required`; candidates are never presented as verified;
* cycle/conflict flags when applicable.

## Status model

| Status | Meaning | Used by default path? |
|---|---|---|
| `candidate` | Automatically proposed; needs review | no |
| `inferred` | Model/structural relationship; unverified | no |
| `explicit` | Stated by syllabus metadata | yes |
| `reviewed` | Human reviewed the evidence | yes |
| `verified` | Human confirmed the prerequisite | yes |

Default paths therefore use only `explicit`, `reviewed` and `verified` edges.
`--include-candidates` and `--include-inferred` are explicit opt-ins and mark the
plan `partial`. A disabled edge is excluded from paths even if its status is
supported; `prerequisites validate` still includes it so conflicts remain visible.

### Review and override commands

```bash
# Promote/downgrade with an audit note; prior evidence and review history remain.
python -m exam_engine prerequisites review <edge_id> \
  --status reviewed --evidence "reviewer: syllabus order + related concept checked"

# Disable without deleting; rediscovery must not silently re-enable it.
python -m exam_engine prerequisites disable <edge_id> --note "hold for review"
python -m exam_engine prerequisites enable <edge_id> --note "cleared"
```

Manual disable/enable decisions survive rediscovery (`manual_override` plus
`action_history`). `graph review` remains available for any graph edge. Deleting
an edge is still a separate explicit `graph delete-edge --yes` operation; Phase 6
never deletes a relationship merely to make the graph acyclic.

`review --status explicit` records a source-stated relationship: it sets
`validated=true` but does not claim `validated_by=human_review`. An explicit
status change does not create a human-review marker. `review --status reviewed`
or `verified` does.

## Cycles and conflicting directions

* `prerequisites validate` runs an iterative DFS and reports every detected cycle
  with its concept labels and edge ids. Traversal is bounded and non-recursive, so
  a cyclic graph cannot hang.
* Reverse pairs (`A → B` and `B → A`) are reported as `order_conflict`. If the
  reverse edge is `explicit`/`reviewed`/`verified` and the candidate is not, the
  candidate is stored with `disabled=true` and `conflict_with=[reverse edge]`.
  Both records and their evidence remain. A reviewer can inspect, disable, or
  promote either one.
* A learning path that encounters a cycle breaks **one edge only for ordering**
  and records the cycle in `plan.cycles`; it does not mutate the graph. The plan is
  marked `partial` and the original edges remain for review.
* `prerequisites validate` exits non-zero when cycles or blocking conflicts remain,
  so CI can fail without the service deleting evidence. Its structured report
  records `algorithm`, `algorithm_version` and the inclusion/configuration flags
  used for the run.

## Learning-path algorithm

Inputs: course, target node, optional syllabus, explicit known-concept ids,
status policy, max depth and max path length.

1. **Target composition**
   * `concept`: the target itself.
   * `unit`: concepts directly contained by the unit (`CONTAINS`).
   * `topic`: questions that cover the topic (`COVERS`) → concepts those questions
     assess (`ASSESSES`). This is explicitly indirect and is reported as such.
   * A valid unit or topic with no composable concepts returns
     `insufficient_evidence`, not `invalid_target`; the target itself is valid but
     there is nothing to plan.
2. **Scope**
   * The course is mandatory and an unknown course is an explicit error.
   * If `--syllabus` is supplied, it must belong to the course and only that
     version's edges are read. An unknown syllabus returns `invalid_target` for a
     path and a structured error for discovery/validation.
   * Otherwise the target node's syllabus is used when it has one.
   * If neither exists and the course has multiple versions, only unversioned
     edges are used and the plan reports the missing version. Versions are
     detected from stored syllabus records, concept scopes and all graph edge
     scopes, not only from the prerequisite subgraph.
   * In that multi-version case, topic and unit composition excludes
     version-specific concepts rather than combining incompatible syllabi. Pass
     `--syllabus` to compose from a selected version.
3. **Ancestors**
   * Breadth-first traversal follows incoming `PREREQUISITE_OF` edges.
   * `max_depth` is the maximum prerequisite hops from a target.
   * Explicitly known concepts are treated as satisfied: traversal stops at them,
     and they are omitted (or shown as `review` with `--review-known`).
   * Concepts beyond the depth limit become `unresolved_concepts`.
4. **Ordering**
   * Kahn topological sort over the selected ancestor subgraph.
   * Ties break on label then node id, so repeated runs are deterministic.
   * One edge per cycle is ignored only for this ordering; the cycle is reported.
   * `max_path_length` truncates prerequisites, marks the plan `partial`, and
     lists omitted required steps as unresolved. Requested target step(s) are
     always retained. If the limit is smaller than the target count, the plan says
     the limit could not be honored rather than silently dropping a target.
5. **Explanation**
   * Each step has `role`, `depth`, `direct`, edge ids, evidence and a rationale.
   * Target, known-concept and unresolved/dropped references retain graph
     provenance (`entity_id`, unit/document ids, aliases) when the node still
     exists.
   * `direct=true` means the step is a prerequisite of one of the selected targets.
   * A step is marked `review_recommended` when **any** supporting relationship is
     candidate/inferred, even if a stronger verified edge also supports the step.
     Its status remains the strongest edge, while the rationale and evidence retain
     the unverified basis.
   * `confidence` is copied only when exactly one relationship supports the step;
     multiple individual scores are not averaged into a fabricated aggregate.

### Plan statuses

| Status | Meaning |
|---|---|
| `ok` | Supported edges only; ordering completed within limits |
| `partial` | Candidate/inferred edges included, cycle broken, unresolved/truncated steps, or conflicting edges present |
| `insufficient_evidence` | No supported prerequisite relationships, or a valid unit/topic composed no concepts; the plan contains only what could be composed |
| `invalid_target` | Unknown target, wrong node type, wrong course, unknown requested syllabus, or syllabus mismatch |

A target explicitly marked known returns `ok` with no study steps unless
`--review-known` asks for a review step. The engine never infers mastery from
opening a document or answering one question.

## Query examples

```bash
# Discover candidates in one course or one syllabus version.
python -m exam_engine prerequisites discover --course demo-cs201-2024-v1
python -m exam_engine prerequisites discover --course demo-cs201-2024-v1 \
  --syllabus <syllabus_id> --no-cooccurrence
python -m exam_engine prerequisites discover --course demo-cs201-2024-v1 \
  --max-cooccurrence-candidates 25

# Dry-run: compute the report but write nothing.
python -m exam_engine prerequisites discover --course demo-cs201-2024-v1 --dry-run

# Inspect/integrity-check without deleting anything.
python -m exam_engine prerequisites candidates --course demo-cs201-2024-v1 --status candidate
python -m exam_engine prerequisites validate --course demo-cs201-2024-v1

# Plan a concept, topic or unit. Labels are accepted when unambiguous.
python -m exam_engine prerequisites path --course demo-cs201-2024-v1 \
  --target "Tree traversal"
python -m exam_engine prerequisites path --course demo-cs201-2024-v1 \
  --target <topic_node_id> --target-type topic
python -m exam_engine prerequisites path --course demo-cs201-2024-v1 \
  --target <unit_node_id> --target-type unit --syllabus <syllabus_id>

# Learner starting point and explicit uncertainty policy.
python -m exam_engine prerequisites path --course demo-cs201-2024-v1 \
  --target "Tree traversal" --known "Arrays" --known "Linked lists"
python -m exam_engine prerequisites path --course demo-cs201-2024-v1 \
  --target "Tree traversal" --include-candidates --max-depth 3 --max-length 12
```

Add `--json` for the full model (`PrerequisiteDiscoveryReport`,
`PrerequisiteValidationReport`, or `LearningPathPlan`). `graph evidence <edge_id>`
and `prerequisites candidates --json` expose the stored evidence.

## Demo on the committed synthetic fixture

The analytics fixture is synthetic and is not an official examination. On a fresh
graph build:

```bash
python -m exam_engine --db data/demo.db import datasets/analytics_sample/manifest.json
python -m exam_engine --db data/demo.db understanding analyze
python -m exam_engine --db data/demo.db graph build --course demo-analytics-cs201-2024-v1
python -m exam_engine --db data/demo.db prerequisites discover \
  --course demo-analytics-cs201-2024-v1 --json
```

Observed result on 2026-10-29:

```text
created 31 updated 0 candidates 31 cycles 0 conflicts 0 isolated 0
evidence_sources ['related_concept_similarity', 'syllabus_order']
```

A second discovery run produced `created 0 updated 31 candidates 31`, confirming
that the same edges are updated rather than duplicated.

A candidate-only plan (`--include-candidates`) for the synthetic concept
`tree traversal` was `partial` with three steps. Excerpt:

```text
1. [prerequisite] Binary trees depth=1 status=candidate confidence=-
   why: Required before 'binary search trees', 'tree traversal'; basis:
        related_concept+syllabus_topic_order (candidate), ...
        This is an unverified candidate relationship included only because the
        request opted in.
   evidence: 'Binary trees' appears 1 topic position(s) before 'binary search trees'
             in syl_.../unit_... doc=... p.1
2. [prerequisite] binary search trees depth=1 status=candidate ...
3. [target] tree traversal depth=0 status=candidate ...
note: Ordering is dependency-based; it is not evidence of pedagogical optimality.
note: Candidate and inferred relationships are never treated as verified facts.
```

The same candidate plan with `--max-length 1` retained `tree traversal` as the
only step and listed the two omitted prerequisites as unresolved. It is `partial`
and says the path was truncated; the target is never silently dropped.

After reviewing only `binary search trees → tree traversal`:

```bash
python -m exam_engine prerequisites review <edge_id> --status reviewed \
  --evidence "reviewer: syllabus order and related concept checked"
python -m exam_engine prerequisites path --course demo-analytics-cs201-2024-v1 \
  --target <tree-traversal-node>
```

the default plan became `ok` with two steps (`binary search trees`, `tree
traversal`). The unreviewed `Binary trees` candidates were excluded. This is the
intended uncertainty behaviour: review can unlock a smaller supported plan without
promoting any other candidate.

## Rebuilding and maintenance

* `graph build` replaces structural Phase 5 content. Phase 6 candidates are
  preserved when both endpoints still exist; `GraphBuildReport` records
  `preserved_edge_count`. Derived metadata, evidence, review status and manual
  disable state are retained.
* If a source record removes an endpoint, the stale candidate is pruned with the
  old nodes. `graph reset --course ... --yes` still removes the course graph.
* `prerequisites discover` can be re-run safely after new imports or Phase 2–4
  analysis. It updates existing candidate evidence and never creates duplicate
  endpoint pairs.
* `graph build` does not automatically rerun discovery. Run discovery after an
  intentional graph rebuild when prerequisite coverage should be refreshed.

## Tests and checks

```bash
python -m pytest                     # 378 passed in 41.02s
python -m pytest tests/test_prerequisite.py           # 30 passed
python -m pytest tests/test_prerequisite_cli.py       # 2 passed
python -m ruff check src tests tools # All checks passed!
python -m ruff format --check src tests tools        # 110 files already formatted
python -m mypy                      # Success: no issues found in 79 source files
```

The Phase 6 tests use deterministic fixtures with known structures: explicit and
weak order edges, question co-occurrence and its cap, semantic support, candidates
vs verified, known starting points, transitive paths, cycles, reverse conflicts,
depth/length limits (including target retention), topic/unit targets,
syllabus-version isolation (including course-wide targets and topic composition),
isolated and empty targets, unknown course/syllabus errors, mixed-status
explanations, cross-unit provenance, dry-run, idempotency, review/disable survival, rebuild
preservation, stale-endpoint pruning, and an end-to-end run over Phase 1–5 pipeline
output. Inferred candidates are asserted to remain candidates unless a human review
changes them.

## Known limitations

* Syllabus order is weak evidence and may be wrong; distance-2 pairs are included
  by default because the window is 2. Use `EXAM_ENGINE_Q6_ORDER_WINDOW=1` for
  adjacent-only evidence.
* Cross-unit ordering is off by default because units may be taught in another
  order. When enabled it remains candidate-only.
* Co-occurrence and semantic relatedness cannot establish direction. They only
  enrich a candidate whose direction came from syllabus order; a pair that appears
  together but is never ordered produces no prerequisite candidate.
* Explicit prerequisite sentences are not parsed from raw prose. Explicit edges
  currently require the Phase 5 syllabus-metadata path.
* Confidence is absent for new candidates by design. Related-concept evidence may
  show a Phase 3 similarity score, but that score is not a probability that the
  prerequisite is true and is never copied onto the candidate as `confidence`.
* The planner uses lexical label tie-breaking and a dependency ordering. It does
  not prove pedagogical optimality, estimate study time, or model learner ability.
* Topic targets are indirect (questions → concepts) and inherit the coverage of
  the available question papers. Unit targets depend on explicit `CONTAINS`
  structure.
* Syllabus scoping is limited by the Phase 5 concept nodes: a course-scoped concept
  with no syllabus id yields a course-wide path with a missing-metadata note. When
  multiple versions exist, only unversioned relationships are safe; the planner
  reports the missing selection rather than assuming one version's order.
* A course-wide topic or unit with multiple syllabus versions cannot compose
  version-specific concepts without a selected version. It returns
  `insufficient_evidence` and a limitation instead of merging those concepts.
* Candidate metadata lives in JSON; it is readable and mergeable but not indexed
  by evidence source. Filtering by source scans the selected edges.
* There is no immutable review-history table; the edge's metadata is the audit
  record. Approved histories are capped at 50 entries. Cycle enumeration is capped
  at 50 cycles per validation call and uses an iterative DFS; a very dense cyclic
  graph may report only the first 50 cycles rather than exhausting memory or recursion.
* No labelled prerequisite benchmark is bundled, so no accuracy or coverage claim
  is made. The synthetic demonstration only exercises plumbing and uncertainty
  handling.

## Future extensions

The same edge/evidence interface can support prerequisite inference from
definitions, validated learning objectives, cross-course shared identities, and
reviewer-confirmed prerequisite rules. The path service can later add study-time
estimates, coverage-gap inputs and concept-evolution constraints without changing
the graph schema. Those are explicitly out of scope for Phase 6.
