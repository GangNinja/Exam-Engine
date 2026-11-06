"""Grounding adapter behind the animated tutor.

The tutor is deliberately *not* a language model and does not invent answers. It
resolves a concept scoped to one course, then composes an extractive summary from
stored syllabus coverage, mapped questions, graph relationships and prerequisite
paths. Every sentence is backed by a record returned alongside it. When the
backend cannot support an answer, the tutor says so and names the recovery step.

The frontend maps the returned ``state`` to an avatar expression. Correctness is
never represented by the avatar; all meaning is present in the text payload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from exam_engine.config import Settings
from exam_engine.coverage.service import CoverageError, CoverageService
from exam_engine.graph.service import GraphService
from exam_engine.models.coverage import ConceptCoverageFinding, CoverageRequest
from exam_engine.models.enums import GraphNodeType
from exam_engine.models.graph import GraphNode
from exam_engine.models.prerequisite import LearningPathRequest
from exam_engine.prerequisite.path import LearningPathService
from exam_engine.storage.db import Database
from exam_engine.storage.repositories import DocumentRepository

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "concept",
        "could",
        "do",
        "does",
        "explain",
        "for",
        "from",
        "give",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "of",
        "on",
        "or",
        "please",
        "show",
        "tell",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)

_MAX_QUESTION_CHARS = 2000
_EXCERPT_CHARS = 360

_UNSUPPORTED = (
    (
        (
            "grade my answer",
            "grade this answer",
            "mark my answer",
            "mark this answer",
            "is my answer correct",
            "evaluate my answer",
            "score my answer",
            "how many marks",
        ),
        "answer_grading",
        (
            "The tutor does not grade answers. Open Practice studio and use Check your "
            "answer on a stored generated question for a local answer check with "
            "transparent coverage feedback; it is a study aid, not an examination mark."
        ),
    ),
    (
        (
            "predict the exam",
            "predict my exam",
            "forecast",
            "what will come in the exam",
            "likely exam questions",
            "next exam questions",
            "important questions for tomorrow",
        ),
        "exam_forecasting",
        (
            "Exam forecasting is not implemented and is not supported by the data. Exam DNA "
            "describes recurrence inside the analysed papers with an explicit paper "
            "denominator; it never predicts a future paper or assigns probabilities."
        ),
    ),
)


@dataclass(frozen=True)
class _Candidate:
    concept_id: str
    label: str
    unit_labels: tuple[str, ...]
    question_count: int
    coverage: ConceptCoverageFinding | None
    node: GraphNode | None


class TutorService:
    """Course-scoped, extractive tutor with explicit capability boundaries."""

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.db = database
        self.settings = settings or Settings()
        self.coverage = CoverageService(database, self.settings.coverage)
        self.graph = GraphService(database, self.settings.graph)
        self.paths = LearningPathService(self.graph, self.settings.prerequisite)

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        return {
            "grounded_explanation": {
                "available": True,
                "label": "Evidence summaries",
                "detail": (
                    "Explains a stored concept using syllabus coverage, mapped questions, "
                    "their locators and the course knowledge graph."
                ),
            },
            "concept_lookup": {
                "available": True,
                "label": "Concept lookup",
                "detail": "Resolves concepts inside the selected course only.",
            },
            "prerequisite_path": {
                "available": True,
                "label": "Prerequisite paths",
                "detail": (
                    "Shows dependency-ordered learning paths built from stored, reviewable "
                    "prerequisite relationships."
                ),
            },
            "practice_question_generation": {
                "available": True,
                "label": "Practice generation",
                "detail": (
                    "Generates grounded practice in the Practice workspace; every item "
                    "starts as a draft for human approval."
                ),
            },
            "answer_grading": {
                "available": True,
                "label": "Local answer checks",
                "detail": (
                    "Checks stored generated questions locally: exact multiple-choice "
                    "keys and lexical coverage feedback for written answers."
                ),
            },
            "conversational_reasoning": {
                "available": False,
                "label": "Open-ended conversation",
                "detail": (
                    "Not available. No language-model provider is configured; the tutor "
                    "summarises stored evidence and refuses to invent content."
                ),
            },
            "exam_forecasting": {
                "available": False,
                "label": "Exam prediction",
                "detail": (
                    "Not implemented. Historical recurrence is not a forecast and is never "
                    "presented as one."
                ),
            },
        }

    # -- context -----------------------------------------------------------
    def context(
        self,
        course_id: str,
        *,
        syllabus_id: str | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        candidates, source, error = self._candidates(course_id, syllabus_id)
        suggestions = [
            {
                "concept_id": item.concept_id,
                "label": item.label,
                "unit_labels": list(item.unit_labels),
                "question_count": item.question_count,
            }
            for item in sorted(candidates, key=lambda c: (-c.question_count, c.label.lower()))[
                :limit
            ]
        ]
        if error is not None:
            return {
                "course_id": course_id,
                "syllabus_id": syllabus_id,
                "source": source,
                "concept_count": 0,
                "suggestions": [],
                "examples": [],
                "capabilities": self.capabilities(),
                "recovery": self._recovery(error),
                "warnings": [
                    {
                        "code": getattr(error, "code", "tutor_context_unavailable"),
                        "message": getattr(error, "message", str(error)),
                    }
                ],
            }
        examples = [f"Explain {item['label']}" for item in suggestions[:4]]
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "source": source,
            "concept_count": len(candidates),
            "suggestions": suggestions,
            "examples": examples,
            "capabilities": self.capabilities(),
            "recovery": None,
            "warnings": [],
        }

    # -- ask ---------------------------------------------------------------
    def ask(
        self,
        course_id: str,
        question: str,
        *,
        concept_id: str | None = None,
        syllabus_id: str | None = None,
    ) -> dict[str, Any]:
        clean = " ".join(str(question or "").split())[:_MAX_QUESTION_CHARS]
        if not clean:
            return self._unavailable(
                course_id,
                syllabus_id,
                "missing_question",
                "Ask about a concept, for example: “Explain binary search”.",
                question="",
            )

        unsupported = self._unsupported_intent(clean)
        if unsupported is not None:
            code, message = unsupported
            return self._unavailable(
                course_id,
                syllabus_id,
                code,
                message,
                question=clean,
            )

        candidates, source, error = self._candidates(course_id, syllabus_id)
        if error is not None and not candidates:
            return self._insufficient(
                course_id,
                syllabus_id,
                clean,
                "The tutor has no stored concept evidence for this course yet.",
                suggestions=[],
                warnings=[
                    {
                        "code": getattr(error, "code", "tutor_no_evidence"),
                        "message": getattr(error, "message", str(error)),
                    }
                ],
                recovery=self._recovery(error),
            )

        match, ambiguous = self._resolve(clean, concept_id, candidates)
        if match is None:
            suggestions = [
                {
                    "concept_id": item.concept_id,
                    "label": item.label,
                    "unit_labels": list(item.unit_labels),
                }
                for item in sorted(candidates, key=lambda c: (-c.question_count, c.label.lower()))[
                    :8
                ]
            ]
            if ambiguous:
                message = (
                    "That question could refer to more than one stored concept. "
                    "Choose one of the concepts below so the answer stays course-scoped."
                )
            else:
                message = (
                    "No stored concept matched that question. The tutor will not guess a "
                    "syllabus concept; choose one of the known concepts below or add the "
                    "course material first."
                )
            return self._insufficient(
                course_id,
                syllabus_id,
                clean,
                message,
                suggestions=suggestions,
                warnings=[],
                recovery=None,
                ambiguous=ambiguous,
            )

        node = match.node or self._node_for(course_id, match)
        units = list(match.unit_labels)
        finding = match.coverage
        evidence: list[dict[str, Any]] = []
        related = self._related(course_id, node, evidence)
        prerequisites = self._prerequisites(course_id, node, evidence)
        path = self._path(course_id, node)
        syllabus_evidence = self._source_excerpt(course_id, finding)
        if syllabus_evidence is not None:
            evidence.insert(0, syllabus_evidence)

        sections: list[dict[str, Any]] = []
        if finding is not None:
            where = (
                f"{match.label} is recorded in this course's stored syllabus"
                + (f" under {', '.join(units)}" if units else "")
                + "."
            )
        elif units:
            where = (
                f"{match.label} is recorded in this course's syllabus coverage data"
                + f" under {', '.join(units)}"
                + "."
            )
        else:
            where = (
                f"{match.label} is recorded as a concept in this course's knowledge graph, "
                "but has no syllabus placement in the analysed corpus."
            )
        if syllabus_evidence is not None:
            where += (
                f" Source: {syllabus_evidence['document_title']}"
                + (
                    f", page {syllabus_evidence['page_number']}"
                    if syllabus_evidence.get("page_number")
                    else ""
                )
                + "."
            )
        sections.append(
            {
                "heading": "Where it appears",
                "text": where,
                "citations": [syllabus_evidence] if syllabus_evidence else [],
            }
        )

        if finding is not None:
            status_text = _coverage_label(finding.status.value)
            corpus = (
                f"Corpus status is “{status_text}”. {finding.distinct_question_count} distinct "
                f"question(s) from {finding.document_count} document(s) and "
                f"{finding.session_count} known session(s) are mapped to this concept."
            )
            if finding.average_mapping_confidence is not None:
                corpus += (
                    f" The average accepted mapping confidence is "
                    f"{finding.average_mapping_confidence * 100:.0f}%."
                )
            if finding.reasons:
                corpus += " " + " ".join(finding.reasons)
            sections.append(
                {
                    "heading": "What the analysed corpus shows",
                    "text": corpus,
                    "citations": [
                        {
                            "kind": "coverage",
                            "title": finding.label,
                            "excerpt": reason,
                            "method": "phase7_coverage",
                        }
                        for reason in finding.reasons[:3]
                    ],
                }
            )
            for support in finding.supporting_questions[:3]:
                sections.append(
                    {
                        "heading": "Mapped question evidence",
                        "text": (
                            f"“{support.text_excerpt}” — {support.document_title}"
                            + (f", {support.source_locator}" if support.source_locator else "")
                            + (
                                f" (mapping score {support.mapping_score:.2f})"
                                if support.mapping_score is not None
                                else ""
                            )
                            + "."
                        ),
                        "citations": [
                            {
                                "kind": "question",
                                "question_id": support.question_id,
                                "document_id": support.document_id,
                                "document_title": support.document_title,
                                "page_number": support.page_number,
                                "locator": support.source_locator,
                                "excerpt": support.text_excerpt,
                                "method": support.mapping_method,
                                "score": support.mapping_score,
                            }
                        ],
                    }
                )
        else:
            sections.append(
                {
                    "heading": "What the analysed corpus shows",
                    "text": (
                        "No syllabus coverage row is available for this concept, so the "
                        "tutor cannot state how often it is assessed. Only graph metadata is "
                        "shown below."
                    ),
                    "citations": [],
                }
            )

        if related:
            sections.append(
                {
                    "heading": "Related concepts",
                    "text": "Stored graph relationships connect this concept to: "
                    + ", ".join(item["label"] for item in related[:6])
                    + ".",
                    "citations": [
                        {
                            "kind": "graph_edge",
                            "edge_id": item["edge_id"],
                            "title": item["label"],
                            "excerpt": item["method"],
                            "method": item["method"],
                        }
                        for item in related[:6]
                    ],
                }
            )

        if path and path.get("steps"):
            step_labels = [step["label"] for step in path["steps"]]
            sections.append(
                {
                    "heading": "Learning path",
                    "text": (
                        f"Dependency order stored in the graph ({path['status']}): "
                        + " → ".join(step_labels[:10])
                        + (
                            ". Some steps are reviewable candidates and are not proven "
                            "prerequisites."
                            if any(step.get("review_recommended") for step in path["steps"])
                            else "."
                        )
                    ),
                    "citations": [
                        {
                            "kind": "learning_path",
                            "title": step["label"],
                            "excerpt": step["rationale"],
                            "method": step.get("method") or "phase6_learning_path",
                        }
                        for step in path["steps"][:10]
                    ],
                }
            )
        elif prerequisites:
            sections.append(
                {
                    "heading": "Learning path",
                    "text": (
                        "Direct prerequisite relationships are stored for: "
                        + ", ".join(item["label"] for item in prerequisites[:6])
                        + ". A full path was not generated for this concept."
                    ),
                    "citations": [
                        {
                            "kind": "prerequisite",
                            "edge_id": item["edge_id"],
                            "title": item["label"],
                            "excerpt": item["method"],
                            "method": item["method"],
                        }
                        for item in prerequisites[:6]
                    ],
                }
            )
        else:
            sections.append(
                {
                    "heading": "Learning path",
                    "text": (
                        "No stored prerequisite relationship was found for this concept in "
                        "this course. That is not evidence that the concept has no "
                        "prerequisites; it means none is present in the analysed graph."
                    ),
                    "citations": [],
                }
            )

        limitations = [
            "This is an extractive summary of stored records, not a generated explanation "
            "and not a claim about examination likelihood.",
            "Coverage describes the available corpus only; a missing question is not proof "
            "that a concept is unimportant.",
        ]
        if finding is not None:
            limitations.extend(finding.limitations)
        if node is None:
            limitations.append(
                "The knowledge graph has no matching node, so related concepts and "
                "prerequisite ordering could not be used."
            )
        if error is not None:
            limitations.append(
                "Coverage evidence was unavailable: "
                f"{getattr(error, 'code', 'coverage_unavailable')}."
            )

        status = "answered"
        state = "speaking"
        if finding is not None and finding.status.value == "insufficient_data":
            status = "insufficient_evidence"
            state = "corrective"
        if finding is None and not related and not prerequisites and path is None:
            status = "insufficient_evidence"
            state = "corrective"

        answer = "\n\n".join(f"{section['heading']}\n{section['text']}" for section in sections)
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "question": clean,
            "status": status,
            "state": state,
            "event": "tutor_answered" if status == "answered" else "tutor_insufficient",
            "concept": {
                "concept_id": match.concept_id,
                "node_id": node.node_id if node else None,
                "label": match.label,
                "unit_labels": units,
                "coverage_status": finding.status.value if finding else None,
                "question_count": match.question_count,
            },
            "answer": answer,
            "sections": sections,
            "evidence": evidence,
            "related": related,
            "prerequisites": prerequisites,
            "learning_path": path,
            "suggestions": [],
            "limitations": _dedupe(limitations),
            "capabilities": self.capabilities(),
            "recovery": self._recovery(error) if error is not None else None,
            "warnings": [],
        }

    # -- concept loading ---------------------------------------------------
    def _candidates(
        self,
        course_id: str,
        syllabus_id: str | None,
    ) -> tuple[list[_Candidate], str, BaseException | None]:
        error: BaseException | None = None
        findings: list[ConceptCoverageFinding] = []
        try:
            report = self.coverage.analyze(
                CoverageRequest(course_id=course_id, syllabus_id=syllabus_id)
            )
            findings = report.concepts
        except (CoverageError, KeyError, ValueError) as exc:
            error = exc

        nodes = {node.node_id: node for node in self.graph.concepts(course_id)}
        by_entity: dict[str, GraphNode] = {}
        for graph_node in nodes.values():
            if graph_node.entity_id:
                by_entity[graph_node.entity_id] = graph_node

        candidates: list[_Candidate] = []
        seen: set[str] = set()
        for finding in findings:
            node = by_entity.get(finding.concept_id)
            candidates.append(
                _Candidate(
                    concept_id=finding.concept_id,
                    label=finding.label,
                    unit_labels=tuple(finding.unit_labels),
                    question_count=finding.distinct_question_count,
                    coverage=finding,
                    node=node,
                )
            )
            seen.add(finding.concept_id)
            if node is not None:
                seen.add(node.node_id)

        # Graph concepts that Phase 7 did not report (for example an inferred
        # concept with no syllabus placement) are still real, citable records.
        for graph_node in nodes.values():
            if graph_node.node_type is not GraphNodeType.CONCEPT:
                continue
            key = graph_node.entity_id or graph_node.node_id
            if key in seen or graph_node.node_id in seen:
                continue
            candidates.append(
                _Candidate(
                    concept_id=key,
                    label=graph_node.label,
                    unit_labels=(),
                    question_count=0,
                    coverage=None,
                    node=graph_node,
                )
            )

        source = "coverage+graph" if findings and nodes else "coverage" if findings else "graph"
        return candidates, source, error

    def _resolve(
        self,
        question: str,
        concept_id: str | None,
        candidates: list[_Candidate],
    ) -> tuple[_Candidate | None, bool]:
        if concept_id:
            for candidate in candidates:
                if concept_id in {
                    candidate.concept_id,
                    candidate.node.node_id if candidate.node else None,
                }:
                    return candidate, False
            return None, False

        scored: list[tuple[float, _Candidate]] = []
        lowered = question.casefold()
        for candidate in candidates:
            score = _label_score(question, candidate.label)
            if candidate.label.casefold() and candidate.label.casefold() in lowered:
                score = max(score, 0.95)
            if score > 0:
                scored.append((score, candidate))
        scored.sort(key=lambda pair: (-pair[0], pair[1].label))
        if not scored or scored[0][0] < 0.5:
            return None, False
        best = scored[0]
        close = [
            pair
            for pair in scored[1:]
            if pair[0] >= best[0] - 0.08 and pair[1].label.casefold() != best[1].label.casefold()
        ]
        if close and best[0] < 0.92:
            return None, True
        return best[1], False

    def _node_for(self, course_id: str, candidate: _Candidate) -> GraphNode | None:
        if candidate.node is not None:
            return candidate.node
        for node in self.graph.concepts(course_id):
            if node.entity_id == candidate.concept_id:
                return node
            if node.normalized_label and node.normalized_label == candidate.label.casefold():
                return node
        return None

    # -- evidence ----------------------------------------------------------
    def _source_excerpt(
        self,
        course_id: str,
        finding: ConceptCoverageFinding | None,
    ) -> dict[str, Any] | None:
        if finding is None or not finding.source_document_id:
            return None
        with self.db.session() as session:
            document = DocumentRepository(session).get(finding.source_document_id)
            if document is None or document.course_id != course_id:
                return None
            pages = DocumentRepository(session).list_pages(finding.source_document_id)
        page = None
        if finding.source_page:
            page = next((p for p in pages if p.page_number == finding.source_page), None)
        if page is None:
            page = next((p for p in pages if p.text.strip()), None)
        if page is None:
            return None
        excerpt = _excerpt(page.text, finding.label)
        return {
            "kind": "syllabus_source",
            "document_id": document.document_id,
            "document_title": document.title,
            "page_number": page.page_number,
            "locator": f"page {page.page_number}",
            "excerpt": excerpt,
            "method": "phase1_extracted_text",
            "score": finding.extraction_confidence,
        }

    def _related(
        self,
        course_id: str,
        node: GraphNode | None,
        evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if node is None:
            return []
        nodes = {item.node_id: item for item in self.graph.concepts(course_id)}
        rows: list[dict[str, Any]] = []
        try:
            edges = self.graph.related_concepts(node.node_id)
        except (KeyError, ValueError):
            return []
        for edge in edges[:8]:
            other_id = (
                edge.target_node_id if edge.source_node_id == node.node_id else edge.source_node_id
            )
            other = nodes.get(other_id)
            if other is None:
                continue
            row = {
                "edge_id": edge.edge_id,
                "node_id": other.node_id,
                "label": other.label,
                "relationship": edge.relationship.value,
                "status": edge.status.value,
                "confidence": edge.confidence,
                "method": edge.method,
            }
            rows.append(row)
            evidence.append(
                {
                    "kind": "graph_edge",
                    "edge_id": edge.edge_id,
                    "document_id": edge.source_document_id,
                    "page_number": edge.source_page,
                    "document_title": other.label,
                    "excerpt": " · ".join(edge.evidence[:2]) or edge.method,
                    "method": edge.method,
                    "score": edge.confidence,
                }
            )
        return rows

    def _prerequisites(
        self,
        course_id: str,
        node: GraphNode | None,
        evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if node is None:
            return []
        nodes = {item.node_id: item for item in self.graph.concepts(course_id)}
        rows: list[dict[str, Any]] = []
        for edge in self.graph.prerequisite_edges(course_id):
            if edge.target_node_id != node.node_id:
                continue
            source = nodes.get(edge.source_node_id)
            if source is None:
                continue
            rows.append(
                {
                    "edge_id": edge.edge_id,
                    "node_id": source.node_id,
                    "label": source.label,
                    "status": edge.status.value,
                    "confidence": edge.confidence,
                    "method": edge.method,
                    "evidence": edge.evidence[:2],
                }
            )
        return rows

    def _path(self, course_id: str, node: GraphNode | None) -> dict[str, Any] | None:
        if node is None:
            return None
        try:
            plan = self.paths.generate(
                LearningPathRequest(
                    course_id=course_id,
                    target_node_id=node.node_id,
                    target_node_type=GraphNodeType.CONCEPT,
                    include_candidates=True,
                    include_inferred=False,
                    max_depth=6,
                )
            )
        except (KeyError, ValueError):
            return None
        steps = [
            {
                "position": step.position,
                "concept_id": step.concept_id,
                "label": step.label,
                "role": step.role,
                "status": step.status.value if step.status else None,
                "confidence": step.confidence,
                "method": step.method,
                "rationale": step.rationale,
                "review_recommended": step.review_recommended,
            }
            for step in plan.steps[:12]
        ]
        return {
            "status": plan.status,
            "steps": steps,
            "known_concepts_skipped": [
                {"concept_id": item.concept_id, "label": item.label}
                for item in plan.known_concepts_skipped
            ],
            "limitations": plan.limitations,
        }

    # -- responses ---------------------------------------------------------
    def _recovery(self, error: BaseException | None) -> dict[str, Any] | None:
        if error is None:
            return None
        code = str(getattr(error, "code", "tutor_unavailable"))
        if code == "unknown_course":
            return {
                "code": "select_course",
                "label": "Select an existing course",
                "detail": "The selected course is not present in this database.",
            }
        if code in {"no_syllabus", "ambiguous_syllabus", "unknown_syllabus"}:
            return {
                "code": "ingest_syllabus",
                "label": "Add and parse a syllabus",
                "detail": (
                    "Coverage evidence needs one explicitly selected syllabus version for "
                    "this course. Upload a syllabus, then run question understanding."
                ),
            }
        if code in {"no_grounding_documents", "empty_syllabus"}:
            return {
                "code": "ingest_material",
                "label": "Add course material",
                "detail": "The course has no usable syllabus grounding yet.",
            }
        return {
            "code": "review_data",
            "label": "Check course data",
            "detail": str(getattr(error, "message", str(error))),
        }

    def _unavailable(
        self,
        course_id: str,
        syllabus_id: str | None,
        code: str,
        message: str,
        *,
        question: str,
    ) -> dict[str, Any]:
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "question": question,
            "status": "unavailable",
            "state": "warning",
            "event": "tutor_unavailable",
            "concept": None,
            "answer": message,
            "sections": [{"heading": "Unavailable capability", "text": message, "citations": []}],
            "evidence": [],
            "related": [],
            "prerequisites": [],
            "learning_path": None,
            "suggestions": [],
            "limitations": ["The tutor does not simulate a capability the backend lacks."],
            "capabilities": self.capabilities(),
            "recovery": None,
            "warnings": [{"code": code, "message": message}],
        }

    def _insufficient(
        self,
        course_id: str,
        syllabus_id: str | None,
        question: str,
        message: str,
        *,
        suggestions: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        recovery: dict[str, Any] | None,
        ambiguous: bool = False,
    ) -> dict[str, Any]:
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "question": question,
            "status": "insufficient_evidence",
            "state": "corrective",
            "event": "tutor_insufficient",
            "concept": None,
            "answer": message,
            "sections": [
                {
                    "heading": "Cannot answer from stored records",
                    "text": message,
                    "citations": [],
                }
            ],
            "evidence": [],
            "related": [],
            "prerequisites": [],
            "learning_path": None,
            "suggestions": suggestions,
            "limitations": [
                "The tutor does not invent a syllabus concept, a definition or an answer."
            ],
            "capabilities": self.capabilities(),
            "recovery": recovery,
            "warnings": warnings,
            "ambiguous": ambiguous,
        }

    @staticmethod
    def _unsupported_intent(question: str) -> tuple[str, str] | None:
        lowered = question.casefold()
        for phrases, code, message in _UNSUPPORTED:
            if any(phrase in lowered for phrase in phrases):
                return code, message
        return None


def _label_score(question: str, label: str) -> float:
    query_tokens = {
        token for token in _TOKEN_RE.findall(question.casefold()) if token not in _STOPWORDS
    }
    label_tokens = {
        token for token in _TOKEN_RE.findall(label.casefold()) if token not in _STOPWORDS
    }
    if not query_tokens or not label_tokens:
        return 0.0
    overlap = len(query_tokens & label_tokens) / len(label_tokens)
    return min(1.0, overlap * 0.9)


def _coverage_label(value: str) -> str:
    return {
        "covered": "covered by at least the configured number of questions",
        "low_coverage": "low coverage in the available corpus",
        "unmapped": "no confident question mapping in the available corpus",
        "uncertain": "uncertain because the evidence or analysis is incomplete",
        "insufficient_data": "insufficient data to make a coverage claim",
    }.get(value, value.replace("_", " "))


def _excerpt(text: str, label: str) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    lowered = compact.casefold()
    index = lowered.find(label.casefold())
    if index < 0:
        return compact[:_EXCERPT_CHARS]
    start = max(0, index - 120)
    end = min(len(compact), index + len(label) + _EXCERPT_CHARS - 120)
    prefix = "…" if start else ""
    suffix = "…" if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = ["TutorService"]
