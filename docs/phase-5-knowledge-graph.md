# Phase 5 — Question Knowledge Graph

Phase 5 turns already-validated Phase 1–4 rows into a persistent, course-scoped
typed graph and a query/review layer. The graph is **derived state**: rebuilding a
course replaces its structure from current questions, syllabi, Phase 2 concepts,
Phase 3 mappings/similarities and Phase 4 examination periods. It does not
re-ingest files, re-run NLP or re-classify questions.

All examples below use the committed **synthetic** fixtures. Nothing here is
official university data, and no graph relationship is a verified educational fact.

---

## 1. Storage choice and rationale

The existing project persists everything in a single checksum-verified SQLite
database. Phase 5 reuses it with two adjacency tables instead of adding a graph
database:

* `graph_nodes` — one row per typed entity with a stable `node_id`, scope columns
  (`course_id`, `syllabus_id`, `document_id`, `question_id`) and JSON metadata.
* `graph_edges` — one row per directed relationship with its evidence, status,
  confidence, method, document/page and model version.

Rationale:

1. **No new operational dependency.** The corpus is small (hundreds to tens of
   thousands of questions), and SQLite transactions, foreign keys and indexes are
   already proven by Phases 1–4.
2. **Rebuild and review are transactional.** A failed build rolls back and leaves
   the previous graph intact.
3. **The query surface is replaceable.** `GraphRepository` is the only SQL layer;
   `GraphService` exposes semantics. A Neo4j/PostgreSQL adjacency backend can be
   added later without changing callers.
4. **Referential integrity is enforced by the database.** Deleting a source
   question, document or syllabus cascades to graph nodes and then to edges.
   `GraphService.validate()` additionally detects dangling edges, endpoint-type
   contract violations and cross-scope edges.

Limitations of this choice: no native graph query language, no built-in shortest
path/centrality algorithms, and multi-hop traversal is an iterative breadth-first
walk in Python. `hops` is capped at 10. Indexes cover course/type, syllabus/type,
document, question, normalized label, and edge source/target/relationship.
Repository lookups additionally filter edges by course and syllabus version.

Migrations: [`0005_knowledge_graph.sql`](../src/exam_engine/storage/migrations/0005_knowledge_graph.sql)
and [`0006_graph_entity_index.sql`](../src/exam_engine/storage/migrations/0006_graph_entity_index.sql).
`0006` is separate so databases that already applied `0005` keep valid checksums.

---

## 2. Graph schema and identity

| Node type | Scope | Source |
|---|---|---|
| `institution` | shared across courses | Phase 1 course metadata |
| `subject` | shared per institution + course code | Phase 1 course metadata |
| `course` | course | Phase 1 course |
| `syllabus` | course + syllabus version | Phase 1 syllabus |
| `unit` | course + syllabus | Phase 1 syllabus unit |
| `topic` | course + unit | syllabus topic or Phase 3 mapping |
| `concept` | course | syllabus topic or Phase 2 concept candidate |
| `question` | course + document | Phase 1 question |
| `source_document` | course | Phase 1 document |
| `exam_session` | course | Phase 4 period resolution |
| `evidence` | reserved | not materialised in Phase 5; evidence lives on edges |

`node_id = "nod_" + sha256(node_type, node_key)[:24]`. Course-scoped keys include
the `course_id`, so the same concept label in two courses produces two distinct
concept nodes. Institution and subject keys intentionally omit the course so those
entities are shared. Node rows keep `entity_id` (the original unit/question/
document/syllabus id) for traceability.

Question nodes follow Phase 1's document-scoped question identity. A repeated
wording in another paper is a separate source appearance because it can carry a
different number, marks, page or section; it is linked by `SIMILAR_TO`/Phase 4
repetition evidence rather than silently collapsed. The node metadata stores the
question fingerprint and source question id so a later canonical-question view can
group appearances without losing provenance. Re-importing the same Phase 1
question record updates the same node instead of creating a duplicate.

A concept node also records:

* `label` (most frequent source spelling), `normalized_label`;
* `aliases` — every source spelling merged into the node;
* `origins` — `syllabus` and/or `inferred`;
* `source_concept_ids` — Phase 2 concept rows that contributed;
* `unit_ids` and `document_ids` — syllabus/source scopes that contributed;
* `verified`, `confidence` and `evidence`.

### Relationship taxonomy

| Relationship | Meaning | Usual status |
|---|---|---|
| `CONTAINS` | syllabus/course contains a unit/topic/concept | `explicit` for syllabus-stated topics; `inferred` for Phase 2/3 candidates |
| `COVERS` | question or document covers a topic/concept | `inferred`, or `candidate` for ambiguous Phase 3 mappings |
| `ASSESSES` | question assesses a concept | `inferred`, or `candidate` for ambiguous Phase 3 mappings |
| `APPEARS_IN` | question belongs to a document or examination session | `explicit` |
| `DERIVED_FROM` | derived node/edge originates from its source | `explicit` or `inferred` |
| `BELONGS_TO` | entity belongs to course/subject/institution | `explicit` |
| `SIMILAR_TO` | two questions have a Phase 3 similarity link | `inferred` |
| `RELATED_TO` | concepts are related through Phase 3 concept-related evidence | `inferred`, or `candidate` when either question mapping is ambiguous |
| `PREREQUISITE_OF` | one concept is a prerequisite of another | `explicit` only from syllabus metadata; otherwise `candidate` |

Statuses are ordered for review: `candidate < inferred < explicit < reviewed <
verified`. Phase 3 ambiguous mappings, `RELATED_TO` edges derived from them, and
syllabus-order prerequisites are **candidate**; an inferred prerequisite also
carries `validated: false` and is never promoted automatically. Reviewing a
`PREREQUISITE_OF` candidate sets `validated: true` and `validated_by: human_review`;
the review survives rebuilds. A later status correction clears the current review
markers, but appends the transition to `metadata.review_history` rather than
erasing the audit trail.

### Evidence fields on every edge

`edge_id`, `course_id`, `syllabus_id`, source/target node ids, relationship,
`status`, `confidence` (nullable), `method`, `evidence[]`,
`source_document_id`, `source_page`, `model_version`, `metadata`, `created_at`,
`updated_at`.

Confidence is recorded only where the producing method supplies one. Structural
edges have `confidence = null`; document-level aggregate `COVERS` edges do not
invent a score. IDs are deterministic:

```text
edge_id = "rel_" + sha256(source_node_id, relationship, target_node_id)[:24]
```

So re-importing the same material updates the same node/edge instead of
duplicating it.

---

## 3. Concept identity and deduplication

Concept resolution is deliberately conservative:

1. Normalize with `normalize_term`: Unicode case-fold, replace punctuation/
   whitespace runs, trim. No stemming, synonym expansion or stop-word deletion.
2. Merge only **identical normalized labels within one course**. Source spellings
   are retained in `aliases`, and source Phase 2 ids in `source_concept_ids`, so
   the merge is auditable and rebuildable.
3. Similar labels (`a binary search` vs `binary search`, `binary trees` vs
   `Binary trees`) are **not** merged. `exam-engine graph duplicates --course …`
   lists review candidates from two signals: lexical label similarity and the
   cosine of the mean Phase 2 embeddings of questions that assess each concept.
   Each candidate records its method and score; no merge happens automatically.
   When no question embeddings exist, only the lexical signal is available.
4. Labels are never compared across courses for identity. A later cross-course
   comparison phase may link them explicitly, but Phase 5 keeps them separate.
   `GraphService.find_concepts(label)` without a course filter returns every
   course-specific node with that normalized label, supporting explicit
   shared-label discovery without merging context.

This means a missed synonym stays separate (false negative) rather than silently
collapsing two concepts with different meanings (false merge). Review candidates
are advisory; source identities are never deleted.

---

## 4. Build, update and review semantics

```bash
python -m exam_engine graph build --course demo-analytics-cs201-2024-v1
python -m exam_engine graph build                 # every course
```

For each course, `GraphService.build_course` runs in one transaction:

1. Load course, documents, questions, syllabi/units/topics, Phase 2 concepts,
   Phase 3 analyses/matches/similarities and Phase 4 periods.
2. Build nodes/edges in memory with deterministic ids.
3. Replace the course's graph rows. Shared institution/subject nodes and their
   shared link are reused; orphaned shared links and nodes are pruned.
4. Preserve `created_at` for unchanged nodes and edges.
5. Preserve `reviewed`/`verified` edge status, review evidence and review metadata
   across rebuilds.

`GraphBuildReport.node_count`, `edge_count`, `nodes_by_type` and
`edges_by_relationship` count **course-scoped** rows. The shared
institution/subject rows are counted separately as `shared_node_count`. The one
shared `subject -> institution` link is reported as `shared_edge_count` and is
excluded from per-course edge totals, so `edge_count` agrees with `graph status`.
Neither shared count is attributed to any one course.

Warnings are returned in `GraphBuildReport.warnings` for missing syllabus,
no concepts, no questions, missing or partial Phase 3 analysis, no exam-session
metadata, partially missing session metadata, no supported question mappings,
unmapped analysed questions, ambiguous question mappings, truncated limits,
ambiguous prerequisite lists, mapping gaps and disabled similarities. A prerequisite list without target topics is reported
as `ambiguous_prerequisites` instead of being assigned to every topic. A course
with no questions and no syllabus is reported as `empty`;
it is not silently presented as an analysed graph. A course with only Phase 2
concepts is not empty and is reported as an OK graph with missing-syllabus and
missing-question warnings.

Rebuild after changing questions, mappings, classifications or metadata:

```bash
python -m exam_engine understanding analyze --course COURSE
python -m exam_engine graph build --course COURSE
```

Deleting a graph does not delete source records:

```bash
python -m exam_engine graph reset --course COURSE --yes
```

---

## 5. Queries and CLI examples

Build/query commands accept `--json` for machine-readable output; `delete-edge`
and `reset` are confirmation-only destructive commands. Course filters are applied
by the service, not only by the CLI.

```bash
# counts, candidate and dangling-edge state
python -m exam_engine graph status

# concepts, optionally scoped to a unit or syllabus id
python -m exam_engine graph concepts --course demo-analytics-cs201-2024-v1
python -m exam_engine graph concepts --course COURSE --unit UNIT_ID

# questions covering a topic / assessing a concept
python -m exam_engine graph questions --course COURSE --topic NODE_ID
python -m exam_engine graph questions --course COURSE --concept NODE_ID
# include ambiguous mappings explicitly; they remain unverified
python -m exam_engine graph questions --course COURSE --concept NODE_ID \
  --include-candidates

# topics covered by one question or source document
python -m exam_engine graph topics --course COURSE --question QUESTION_ID
python -m exam_engine graph topics --course COURSE --document DOCUMENT_ID
python -m exam_engine graph topics --course COURSE --document SYLLABUS_DOCUMENT_ID
# restrict coverage to one syllabus version
python -m exam_engine graph topics --course COURSE --question QUESTION_ID \
  --syllabus SYLLABUS_ID
# candidate document/question coverage is hidden unless requested
python -m exam_engine graph topics --course COURSE --question QUESTION_ID \
  --include-candidates

# related concepts (candidate links excluded unless requested)
python -m exam_engine graph related NODE_ID --course COURSE
python -m exam_engine graph related NODE_ID --course COURSE --syllabus SYLLABUS_ID

# bounded multi-hop exploration
python -m exam_engine graph neighborhood NODE_ID --course COURSE --hops 2 \
  --relationship ASSESSES --relationship COVERS --direction both
python -m exam_engine graph neighborhood NODE_ID --course COURSE \
  --syllabus SYLLABUS_ID

# evidence behind one edge
python -m exam_engine graph evidence EDGE_ID

# concept duplicate review candidates (lexical + question-embedding signal)
python -m exam_engine graph duplicates --course COURSE --threshold 0.85

# nodes with no relationships, and questions with no supported concept mapping
python -m exam_engine graph isolated --course COURSE
python -m exam_engine graph unmapped --course COURSE

# dangling endpoints, relationship-endpoint contracts and scope violations
python -m exam_engine graph validate --course COURSE

# human review survives later rebuilds; a downgrade is recorded in review_history
python -m exam_engine graph review EDGE_ID --status reviewed \
  --evidence "checked against syllabus page 2"
```

Programmatic use:

```python
from exam_engine.graph import GraphService

service = GraphService(database)

report = service.build_course("demo-analytics-cs201-2024-v1")
concepts = service.concepts("demo-analytics-cs201-2024-v1", unit_id=unit_id)
questions = service.questions_for("demo-analytics-cs201-2024-v1", topic_id=topic_node_id)
all_questions = service.questions_for(
    "demo-analytics-cs201-2024-v1", topic_id=topic_node_id, include_candidates=True
)
topics = service.topics_for_question(
    question_id, course_id=course_id, syllabus_id=syllabus_id
)
concepts = service.concepts_for_question(
    question_id, course_id=course_id, syllabus_id=syllabus_id
)
topics_from_syllabus = service.topics_for_document(
    syllabus_document_id, course_id=course_id, syllabus_id=syllabus_id
)
evidence = service.evidence_for_edge(edge_id)
hood = service.neighborhood(
    concept_node_id, hops=2, course_id=course_id, syllabus_id=syllabus_id
)
problems = service.validate(course_id)
```

`GraphService` is independent of the CLI and the future dashboard. For a syllabus
document, `topics_for_document` follows the `syllabus -> unit -> topic`
`CONTAINS` chain rather than pretending the syllabus has question mappings.

---

## 6. Multi-course integrity

* Every edge is course-scoped except the shared `subject -> institution` link;
  concept/question/topic/unit nodes are course-scoped.
* Shared `institution`/`subject` nodes have `course_id = null`. Course-to-
  institution/subject edges are course-scoped; the `subject -> institution`
  `BELONGS_TO` link is itself shared (`course_id = null`) because both endpoints
  are shared. It is stored once, survives sibling builds and is excluded from
  per-course edge counts. `validate()` reports any course-scoped edge whose
  endpoint belongs to a different course, plus edges whose node types violate the
  relationship contract.
* `concepts()`, `questions_for()`, `topics_for_question()`,
  `concepts_for_question()`, `topics_for_document()`, `related_concepts()`,
  `neighborhood()` and `isolated_nodes()` accept a course filter. Queries reject
  a node from another course rather than silently returning mixed results.
* `questions_for()`, `topics_for_question()`, `concepts_for_question()`,
  `topics_for_document()` and `concepts()` accept a syllabus-version filter;
  `related_concepts()` and `neighborhood()` can restrict crossed edges by syllabus
  id.
* Question/topic queries return **supported** edges by default. Unreviewed
  `candidate` mappings are exposed only with `include_candidates=True` (CLI:
  `--include-candidates`), so an ambiguous mapping is never presented as a
  verified coverage fact. `graph unmapped` likewise treats candidate-only
  assessment edges as unsupported.
* Missing institution/course/syllabus metadata produces a warning or a missing
  node, never a fabricated value.
* Cross-course comparison is intentionally not implemented in this phase; the
  schema keeps the scope needed for it.

---

## 7. Data requirements and interpretation

* **Concepts/topics** need a parsed syllabus or Phase 2 concept rows. Without
  them, question-to-concept queries are empty and the build reports `no_concepts`.
* **Question mapping** needs Phase 3 analyses and unit matches. A question without
  a supported (non-candidate) `ASSESSES` edge appears in `graph unmapped`.
  Candidate-only mappings remain review items, not established coverage.
* **Duplicate candidates** accept two independent signals: lexical label
  similarity and cosine similarity of the mean Phase 2 question embeddings for
  each concept. The embedding signal is skipped when no question vectors exist;
  neither signal merges or deletes a concept.
* **Examination sessions** need explicit `exam_session`/`exam_date`/exam type
  metadata (Phase 4 resolution); upload/publication dates are never used.
  Papers without metadata simply have no session node.
* **Similarity edges** need Phase 3 similarity rows. Semantic similarity is
  evidence, not proof: only exact wording is treated as duplicate text, and all
  question nodes remain separate. `RELATED_TO` inherits the weaker of the two
  question mapping statuses, so an ambiguous mapping cannot produce an inferred
  concept relation.
* **Prerequisites** are explicit only when syllabus unit metadata contains
  prerequisites. Topic order yields review candidates only when
  `EXAM_ENGINE_Q5_INFER_PREREQUISITES=1` (or `--infer-prerequisites`).
* A topic absent from the graph is absent from the **available dataset**, not
  proof that it was never examined.

---

## 8. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `EXAM_ENGINE_Q5_MAX_QUESTIONS` | `20000` | Maximum questions per course graph |
| `EXAM_ENGINE_Q5_MAX_SIMILARITIES` | `50000` | Maximum similarity pairs read per course |
| `EXAM_ENGINE_Q5_INCLUDE_INFERRED_CONCEPTS` | `1` | Include Phase 2 inferred concepts |
| `EXAM_ENGINE_Q5_INCLUDE_SIMILARITIES` | `1` | Create `SIMILAR_TO`/`RELATED_TO` edges |
| `EXAM_ENGINE_Q5_INFER_PREREQUISITES` | `0` | Create candidate prerequisite edges from topic order |
| `EXAM_ENGINE_Q5_CONCEPT_DUPLICATE_THRESHOLD` | `0.80` | Default duplicate-review cutoff |

---

## 9. Tests and actual results

Focused Phase 5 tests:

```bash
python -m pytest tests/test_graph.py tests/test_graph_cli.py -q
```

Full suite and static checks:

```bash
python -m pytest -q
python -m ruff check src tests
python -m mypy
```

Last verified run (2026-10-29, Windows, Python 3.13):

* `tests/test_graph.py` — **33 passed**
* `tests/test_graph_cli.py` — **2 passed**
* full suite — **345 passed**
* `ruff check src tests` — **All checks passed**
* `ruff format --check src tests` — **102 files already formatted**
* `mypy` — **Success: no issues found in 73 source files**

The tests cover deterministic node/edge ids, exact-vs-similar concept identity,
aliases and source ids, evidence/confidence presence, candidate prerequisites vs
explicit syllabus prerequisites, cross-course concept isolation, shared
institution/subject retention, shared-edge scope across sibling builds and orphan
pruning, idempotent rebuilds, builder-instance reuse, review persistence, review
downgrade history, created-at preservation, source deletion/cascade, empty courses,
course-scoped queries, syllabus filters, syllabus-document topic coverage,
multi-hop neighbourhoods, evidence retrieval, similarity source-document/page
provenance, invalid-endpoint rollback, invalid relationship-endpoint contracts,
reviewed prerequisite validation, explicit-vs-inferred concept provenance,
ambiguous mappings as candidates, partial-analysis reporting, candidate exclusion
from supported queries, lexical and embedding duplicate-review signals, and CLI
error paths.

No labelled graph benchmark is bundled. These tests prove structural correctness
and reproducibility, not that inferred pedagogical relationships are true.

---

## 10. Known limitations and future extensions

* The graph is a relational adjacency model, not a graph database; traversal is
  bounded and iterative, with no centrality, community detection or shortest-path
  API yet.
* Question nodes are document-scoped Phase 1 appearances, not a canonical question
  identity across papers. Exact repeated wording is linked by `SIMILAR_TO` and
  Phase 4 repetition, and the node stores its fingerprint; canonical grouping is
  deferred to a later phase.
* Concept resolution stops at normalized exact match. Synonyms, abbreviations and
  cross-course identity require a review workflow that is intentionally deferred.
  Duplicate candidates are advisory: lexical label similarity plus cosine of the
  mean Phase 2 question embeddings. Phase 2 does not emit concept-level vectors,
  and neither signal merges or splits a concept automatically.
* Course graph status and build reports count course-scoped rows. The shared
  `subject -> institution` edge is reported separately as `shared_edge_count`; it
  is deleted when no course references either shared node.
* `EVIDENCE` is a reserved node type. Evidence currently lives on edges
  (`evidence[]`, document/page, method/model version); no separate evidence-node
  table is materialised.
* An explicit human review survives a rebuild, and status transitions append to
  `metadata.review_history`. There is no separate immutable review-history table,
  so the edge metadata remains the audit record.
* Deleting a shared institution/subject node removes its edges for every course;
  prefer deleting course-scoped nodes or rebuilding the affected courses.
* Inferred prerequisites come only from syllabus topic order and remain
  candidates. Phase 6 adds the separate prerequisite-discovery, validation and
  learning-path services over this graph; see
  [phase-6-prerequisites.md](phase-6-prerequisites.md).
* Coverage-gap detection, concept evolution, Exam DNA, question generation,
  misconception mining and dashboard integration are out of scope for Phase 5.
* The user's own syllabi and question papers remain reserved for Phase 13; this
  phase uses the committed synthetic fixtures only.
