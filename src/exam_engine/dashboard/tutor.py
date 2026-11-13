"""Grounding adapter behind the animated tutor.

The tutor resolves a concept scoped to one course, then asks a local/configured
language model to answer using stored syllabus coverage, mapped questions, graph
relationships, prerequisite paths and optional cited web sources. The model is
required to cite source IDs; when it is unavailable the service falls back to an
extractive summary of the same stored records. When the backend cannot support an
answer, the tutor says so and names the recovery step.

The frontend maps the returned ``state`` to an avatar expression. Correctness is
never represented by the avatar; all meaning is present in the text payload.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from exam_engine.config import Settings
from exam_engine.coverage.service import CoverageError, CoverageService
from exam_engine.dashboard.tutor_ai import TutorAI, TutorAIError
from exam_engine.dashboard.websearch import WebSearch
from exam_engine.graph.service import GraphService
from exam_engine.models.coverage import ConceptCoverageFinding, CoverageRequest
from exam_engine.models.enums import GraphNodeType
from exam_engine.models.graph import GraphNode
from exam_engine.models.prerequisite import LearningPathRequest
from exam_engine.prerequisite.path import LearningPathService
from exam_engine.storage.db import Database
from exam_engine.storage.repositories import (
    CourseRepository,
    DocumentRepository,
    SyllabusRepository,
)

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

TutorEventCallback = Callable[[dict[str, Any]], None]


def _progress(callback: TutorEventCallback | None, stage: str, message: str) -> None:
    if callback is not None:
        callback({"event": "status", "stage": stage, "message": message})


def _answer_delta(callback: TutorEventCallback | None, text: str) -> None:
    if callback is not None and text:
        callback({"event": "answer", "text": text})


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


@dataclass(frozen=True)
class _WebAnswer:
    answer: str
    key_points: list[str]
    citation_ids: list[str]


class TutorService:
    """Course-scoped tutor with a model path and an extractive fallback."""

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.db = database
        self.settings = settings or Settings()
        self.coverage = CoverageService(database, self.settings.coverage)
        self.graph = GraphService(database, self.settings.graph)
        self.paths = LearningPathService(self.graph, self.settings.prerequisite)
        self.ai = TutorAI(self.settings.tutor)
        self.web = WebSearch(self.settings.tutor)

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        ai = self.ai.status()
        web = self.web.status()
        return {
            "grounded_explanation": {
                "available": True,
                "label": "Grounded explanations",
                "detail": (
                    "Explains a concept with a local language model, grounded in stored "
                    "syllabus coverage, mapped questions, page locators and the course "
                    "graph; when no model is available it builds a structured teaching "
                    "guide from the same stored evidence."
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
                "label": "Free-text answer checks",
                "detail": (
                    "Use Practice for local multiple-choice keys and written-answer "
                    "coverage checks, or paste a question and answer to discuss grading "
                    "with the AI tutor."
                ),
            },
            "web_search": {
                "available": web["available"],
                "label": "Live web sources",
                "detail": web["detail"],
            },
            "conversational_reasoning": {
                "available": True,
                "label": "Open-ended conversation",
                "detail": (
                    f"Full free-form conversation with the local model "
                    f"{ai.model or ''} across this course, grounded in stored evidence. "
                    if ai.available
                    else (
                        "Works offline as a course-guided teacher using the stored "
                        "syllabus, past-paper coverage and cited web summaries. A local "
                        "model adds unrestricted follow-up conversation."
                    )
                ),
            },
            "exam_focus": {
                "available": True,
                "label": "Evidence-based exam focus",
                "detail": (
                    "Builds a revision focus from stored coverage and question recurrence, "
                    "with source links. It works offline and never claims to predict a "
                    "future paper."
                ),
            },
        }

    # -- teaching helpers -------------------------------------------------
    def _unit_context(
        self, course_id: str, finding: ConceptCoverageFinding | None
    ) -> dict[str, Any] | None:
        if finding is None or not finding.unit_ids:
            return None
        try:
            with self.db.session() as session:
                syllabi = SyllabusRepository(session).get_by_course(course_id)
        except (KeyError, ValueError):
            return None
        syllabus = next(
            (item for item in syllabi if item.syllabus_id == finding.syllabus_id),
            None,
        )
        if syllabus is None:
            syllabus = next(
                (
                    item
                    for item in syllabi
                    if any(unit.unit_id in finding.unit_ids for unit in item.units)
                ),
                None,
            )
        if syllabus is None:
            return None
        unit = next(
            (item for item in syllabus.units if item.unit_id in finding.unit_ids),
            None,
        )
        if unit is None:
            return None
        raw_text = _strip_unit_heading(unit.raw_text, unit.title)
        clauses = _syllabus_clauses(raw_text)
        target = _target_clause(raw_text, finding.label) or finding.label
        topics = [clause for clause in clauses if clause.casefold() != target.casefold()]
        return {
            "unit_id": unit.unit_id,
            "unit_label": unit.unit_label,
            "unit_title": _display_title(unit.title or ""),
            "topics": topics[:8],
            "target": _compact_text(target)[:500],
            "page_number": unit.page_number,
        }

    def _lesson_text(
        self,
        match: _Candidate,
        unit_context: dict[str, Any] | None,
        finding: ConceptCoverageFinding | None,
    ) -> str:
        label = match.label
        if finding is None:
            return (
                f"{label} is recorded in this course's knowledge graph, but it has no "
                "syllabus placement in the analysed corpus. Start by defining it in one "
                "sentence, then learn one worked example and the conditions or cases where "
                "it applies. Use the connected concepts below to place it in the course."
            )
        if unit_context is None:
            return (
                f"{label} is part of this course's stored syllabus. The syllabus entry "
                "names the topic but does not provide a full textbook explanation, so "
                "build your answer around three things: a one-sentence definition, the "
                "key method or conditions it uses, and one worked example from the "
                "question bank."
            )
        title = unit_context.get("unit_title") or unit_context.get("unit_label")
        target = unit_context.get("target") or label
        text = (
            f"{label} is a topic in {title}. The syllabus expects you to understand: "
            f"“{target}”. A good answer should define the idea in simple language, "
            "explain the method, formula or conditions it uses, and then apply it to a "
            "worked example."
        )
        if match.question_count:
            text += (
                f" The stored question bank contains {match.question_count} past-paper "
                "question(s) on it, so practise selecting the right method and showing "
                "each step clearly."
            )
        return text

    def _learning_points(
        self,
        unit_context: dict[str, Any] | None,
        finding: ConceptCoverageFinding | None,
    ) -> str:
        if unit_context is None:
            if finding is None:
                return (
                    "Work through these steps: (1) write a one-sentence definition; "
                    "(2) identify the stored examples or properties; (3) solve or explain "
                    "one example without looking at the answer; (4) check the method and "
                    "the final statement."
                )
            return (
                "Use the syllabus wording to build a short revision card: definition, "
                "key terms, one worked example, and one common mistake. Then attempt a "
                "past-paper question."
            )
        target = str(unit_context.get("target", ""))
        topics = []
        for topic in unit_context.get("topics", []):
            cleaned = topic
            if target:
                cleaned = re.sub(re.escape(target), "", cleaned, flags=re.IGNORECASE)
            cleaned = _compact_text(cleaned).strip(",;")
            if cleaned and cleaned.casefold() != target.casefold():
                topics.append(cleaned)
        topics = topics[:6]
        lines = [
            f"Target: {target or 'the topic named in the syllabus'}",
        ]
        if topics:
            lines.append("Connected ideas in the same unit: " + "; ".join(topics) + ".")
        lines.append(
            "By the end, be able to: define the topic; explain the method or conditions; "
            "apply it to a worked example; and state one common mistake."
        )
        return "\n".join(lines)

    def _is_exam_focus(self, question: str) -> bool:
        lowered = question.casefold()
        if any(word in lowered for word in ("predict", "forecast", "tomorrow")):
            return False
        phrases = (
            "exam focus",
            "revision focus",
            "what should i study",
            "what should i revise",
            "what to study",
            "important topics",
            "most important topics",
            "high weightage",
            "where should i focus",
        )
        return any(phrase in lowered for phrase in phrases)

    def _exam_focus_answer(
        self, course_id: str, syllabus_id: str | None, question: str
    ) -> dict[str, Any] | None:
        candidates, source, error = self._candidates(course_id, syllabus_id)
        ranked = [
            item
            for item in sorted(
                candidates,
                key=lambda candidate: (
                    -candidate.question_count,
                    candidate.label.casefold(),
                ),
            )
            if _usable_label(item.label)
        ]
        if not ranked:
            return None
        focus = ranked[:8]
        lines = []
        for index, item in enumerate(focus, start=1):
            units = f" ({', '.join(item.unit_labels)})" if item.unit_labels else ""
            lines.append(
                f"{index}. {item.label}{units} — {item.question_count} mapped question(s)"
            )
        sections = [
            {
                "heading": "Your revision focus",
                "text": (
                    "Use the stored question bank as evidence of what has been asked. This "
                    "is not a prediction of the next paper. The topics below have the "
                    "strongest recurrence in this course; start at the top and work down."
                ),
                "citations": [],
            },
            {
                "heading": "Priority topics",
                "text": "\n".join(lines),
                "citations": [
                    {
                        "kind": "coverage",
                        "title": item.label,
                        "excerpt": (
                            f"{item.question_count} mapped question(s) in the stored corpus."
                        ),
                        "method": "phase7_coverage",
                    }
                    for item in focus
                ],
            },
            {
                "heading": "How to use this list",
                "text": (
                    "For each topic: revise the definition and method, solve one past-paper "
                    "question without notes, then use Practice to check your reasoning. Spend "
                    "more time on a topic only when you can explain why each step works, not "
                    "just because its count is high. If the stored corpus is thin, treat the "
                    "order as a starting point rather than a guarantee."
                ),
                "citations": [],
            },
        ]
        limitations = [
            "This is evidence from stored questions only; it is not an examination forecast.",
            "Question recurrence counts distinct questions and does not assign marks or "
            "importance to a topic.",
        ]
        if error is not None:
            limitations.append(
                "Coverage evidence was partial: "
                f"{getattr(error, 'code', 'coverage_unavailable')}."
            )
        answer = "\n\n".join(f"{section['heading']}\n{section['text']}" for section in sections)
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "question": question,
            "status": "answered",
            "state": "speaking",
            "event": "tutor_answered",
            "concept": None,
            "answer": answer,
            "sections": sections,
            "evidence": [
                citation
                for section in sections
                for citation in section.get("citations", [])
            ],
            "related": [],
            "prerequisites": [],
            "learning_path": None,
            "suggestions": [
                {
                    "concept_id": item.concept_id,
                    "label": item.label,
                    "unit_labels": list(item.unit_labels),
                }
                for item in focus[:8]
            ],
            "limitations": limitations,
            "capabilities": self.capabilities(),
            "recovery": self._recovery(error) if error is not None else None,
            "warnings": [],
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
            for item in sorted(candidates, key=lambda c: (-c.question_count, c.label.lower()))
            if _usable_label(item.label)
        ][:limit]
        if error is not None:
            return {
                "course_id": course_id,
                "syllabus_id": syllabus_id,
                "source": source,
                "concept_count": 0,
                "suggestions": [],
                "examples": [],
                "capabilities": self.capabilities(),
                "ai": self.ai.status().as_dict(),
                "web_search": self.web.status(),
                "recovery": self._recovery(error),
                "warnings": [
                    {
                        "code": getattr(error, "code", "tutor_context_unavailable"),
                        "message": getattr(error, "message", str(error)),
                    }
                ],
            }
        examples = [f"Explain {item['label']}" for item in suggestions[:4]]
        examples.append("What should I study for the exam?")
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "source": source,
            "concept_count": len(candidates),
            "suggestions": suggestions,
            "examples": examples,
            "capabilities": self.capabilities(),
            "ai": self.ai.status().as_dict(),
            "web_search": self.web.status(),
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
        on_event: TutorEventCallback | None = None,
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

        _progress(
            on_event,
            "stored_evidence",
            "Searching stored concepts, coverage and source pages…",
        )
        if self._is_exam_focus(clean):
            focus = self._exam_focus_answer(course_id, syllabus_id, clean)
            if focus is not None:
                return focus
        unsupported = self._unsupported_intent(clean)
        if unsupported is not None and not self.ai.status().available:
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
            ai_answer, ai_error = self._ai_answer(
                course_id,
                syllabus_id,
                clean,
                None,
                candidates,
                [],
                [],
                on_event=on_event,
            )
            if ai_answer is not None:
                return ai_answer
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
                warnings=(
                    [
                        {
                            "code": ai_error.code,
                            "message": ai_error.message,
                        }
                    ]
                    if ai_error is not None
                    else []
                ),
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

        unit_context = self._unit_context(course_id, finding)
        peers = [
            item
            for item in candidates
            if item.concept_id != match.concept_id
            and item.unit_labels
            and set(item.unit_labels) & set(units)
            and _usable_label(item.label)
        ]
        peers.sort(key=lambda item: (-item.question_count, item.label.casefold()))
        sections: list[dict[str, Any]] = []
        sections.append(
            {
                "heading": "Explanation",
                "text": self._lesson_text(match, unit_context, finding),
                "citations": [syllabus_evidence] if syllabus_evidence else [],
            }
        )
        sections.append(
            {
                "heading": "What to learn",
                "text": self._learning_points(unit_context, finding),
                "citations": [],
            }
        )
        if peers:
            sections.append(
                {
                    "heading": "Topics to study alongside this one",
                    "text": "In the same part of the syllabus, also revise: "
                    + "; ".join(item.label for item in peers[:5])
                    + ".",
                    "citations": [],
                }
            )

        if finding is not None:
            where = (
                f"{match.label} sits under {', '.join(units)}"
                if units
                else f"{match.label} is recorded in the stored syllabus"
            )
        elif units:
            where = f"{match.label} is recorded in syllabus coverage under {', '.join(units)}"
        else:
            where = (
                f"{match.label} is recorded as a concept in this course's knowledge graph, "
                "but has no syllabus placement in the analysed corpus."
            )
        if syllabus_evidence is not None:
            where += (
                f". It comes from {syllabus_evidence['document_title']}"
                + (
                    f", page {syllabus_evidence['page_number']}"
                    if syllabus_evidence.get("page_number")
                    else ""
                )
                + "."
            )
        sections.append(
            {
                "heading": "Where it sits in the course",
                "text": where,
                "citations": [syllabus_evidence] if syllabus_evidence else [],
            }
        )

        if finding is not None:
            count = finding.distinct_question_count
            exam_text = (
                f"The stored question bank has {count} past-paper question"
                f"{'s' if count != 1 else ''} mapped to this topic. "
                "Use them for practice and compare your method with the answer key. "
                "This is evidence of what has been asked before, not a forecast of the "
                "next paper."
                if count
                else (
                    "No past-paper question is confidently mapped to this topic in the "
                    "stored corpus. Learn the syllabus description first, then use the "
                    "Practice workspace to build a question."
                )
            )
            reasons = [
                {
                    "kind": "coverage",
                    "title": finding.label,
                    "excerpt": reason,
                    "method": "phase7_coverage",
                }
                for reason in finding.reasons[:3]
            ]
            sections.append(
                {
                    "heading": "Exam evidence",
                    "text": exam_text,
                    "citations": reasons,
                }
            )
        else:
            sections.append(
                {
                    "heading": "Exam evidence",
                    "text": (
                        "There is no syllabus coverage row for this graph-only concept, so "
                        "the tutor cannot state how often it has been asked. Only graph "
                        "metadata is available."
                    ),
                    "citations": [],
                }
            )

        if related and not peers:
            sections.append(
                {
                    "heading": "Connected topics",
                    "text": (
                        "Study these nearby ideas together with the target topic: "
                        + ", ".join(item["label"] for item in related[:4])
                        + "."
                    ),
                    "citations": [
                        {
                            "kind": "graph_edge",
                            "edge_id": item["edge_id"],
                            "title": item["label"],
                            "excerpt": item["method"],
                            "method": item["method"],
                        }
                        for item in related[:4]
                    ],
                }
            )

        if path and path.get("steps"):
            step_labels = [step["label"] for step in path["steps"]]
            sections.append(
                {
                    "heading": "Suggested learning order",
                    "text": (
                        "The stored dependency path suggests learning in this order: "
                        + " → ".join(step_labels[:10])
                        + (
                            ". Some steps are reviewable candidates, not proven "
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
                    "heading": "Suggested learning order",
                    "text": (
                        "The graph stores these direct prerequisites: "
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
                    "heading": "Suggested learning order",
                    "text": (
                        "No stored prerequisite relationship is available for this concept. "
                        "Start with its definition and one worked example, then move to "
                        "past-paper questions."
                    ),
                    "citations": [],
                }
            )

        limitations = [
            "This offline answer is a teaching guide built from stored syllabus and "
            "past-paper records; start the local model for a fuller conversational "
            "explanation.",
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

        ai_answer, ai_error = self._ai_answer(
            course_id,
            syllabus_id,
            clean,
            match,
            candidates,
            sections,
            evidence,
            on_event=on_event,
        )
        if ai_answer is not None:
            return ai_answer
        if ai_error is not None:
            limitations.append(f"AI tutor fallback: {ai_error.message}")

        # A matched syllabus concept always gets a teaching answer.  Coverage may
        # be thin or uncertain, but that is a limitation of the evidence, not a
        # reason to refuse to explain the topic.
        status = "answered"
        state = "speaking"

        answer = "\n\n".join(f"{section['heading']}\n{section['text']}" for section in sections)
        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "question": clean,
            "status": status,
            "state": state,
            "event": "tutor_answered",
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

    # -- model answer ------------------------------------------------------
    def _ai_answer(
        self,
        course_id: str,
        syllabus_id: str | None,
        question: str,
        match: _Candidate | None,
        candidates: list[_Candidate],
        sections: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        on_event: TutorEventCallback | None = None,
    ) -> tuple[dict[str, Any] | None, TutorAIError | None]:
        """Ask the configured model, returning a complete tutor response.

        The extractive path still owns the offline fallback. This method only
        builds the prompt, resolves the model's citation IDs and adapts the JSON
        answer to the existing response contract.
        """

        with self.db.session() as session:
            course = CourseRepository(session).get(course_id)
        if course is None:
            return None, None

        ranked = sorted(candidates, key=lambda item: (-item.question_count, item.label.lower()))
        if match is not None:
            ranked = [match] + [item for item in ranked if item.concept_id != match.concept_id]

        local_citations: dict[str, dict[str, Any]] = {}
        local_lines: list[str] = []
        for item in ranked[: max(1, self.settings.tutor.max_local_concepts)]:
            if len(item.label.strip()) < 3:
                continue
            source_id = f"S{len(local_citations) + 1}"
            units = ", ".join(item.unit_labels) or "no unit placement"
            excerpt = (
                f"{item.label}; unit: {units}; mapped questions: {item.question_count}; "
                f"coverage: {item.coverage.status.value if item.coverage else 'graph only'}"
            )
            local_citations[source_id] = {
                "kind": "syllabus_concept",
                "title": item.label,
                "document_title": item.label,
                "excerpt": excerpt,
                "method": "stored_syllabus_and_coverage",
            }
            local_lines.append(f"[{source_id}] {excerpt}")

        seen_evidence: set[str] = set()
        evidence_lines: list[str] = []
        for section in sections:
            for citation in section.get("citations", []):
                if not isinstance(citation, dict):
                    continue
                title = str(
                    citation.get("document_title")
                    or citation.get("title")
                    or citation.get("kind")
                    or "Stored evidence"
                )
                excerpt = str(citation.get("excerpt") or "").strip()
                if not excerpt:
                    continue
                key = f"{title}|{excerpt[:120]}"
                if key in seen_evidence or len(evidence_lines) >= 6:
                    continue
                seen_evidence.add(key)
                source_id = f"S{len(local_citations) + 1}"
                copied = dict(citation)
                copied.update(
                    {
                        "kind": "stored_evidence",
                        "title": title,
                        "document_title": title,
                        "excerpt": excerpt,
                    }
                )
                local_citations[source_id] = copied
                evidence_lines.append(f"[{source_id}] {title}: {excerpt[:500]}")

        web_sources: list[Any] = []
        if self.web.available:
            _progress(on_event, "web_search", "Checking cited web sources…")
            try:
                web_sources = self.web.search(question)
            except Exception:  # noqa: BLE001 - retrieval is optional
                web_sources = []
        web_citations: dict[str, dict[str, Any]] = {}
        web_lines: list[str] = []
        for index, source in enumerate(web_sources, start=1):
            source_id = f"W{index}"
            web_citations[source_id] = source.as_citation()
            web_lines.append(f"[{source_id}] {source.title} ({source.url}): {source.snippet[:800]}")

        if self.settings.tutor.web_first and web_sources:
            fast_answer = self._fast_web_answer(
                course_id,
                syllabus_id,
                question,
                match,
                ranked,
                web_sources,
                web_citations,
                local_citations,
            )
            if fast_answer is not None:
                return fast_answer, None

        status = self.ai.status()
        if not status.available:
            return None, None

        allowed_ids = sorted(local_citations) + sorted(web_citations)
        if not allowed_ids:
            return None, TutorAIError(
                "tutor_no_context",
                "No stored or web context was available for the model.",
            )

        if match is not None:
            unit_text = ", ".join(match.unit_labels) or "no unit placement"
            match_text = (
                f"{match.label} (unit: {unit_text}; mapped questions: {match.question_count})"
            )
        else:
            match_text = "none"
        system = (
            "You are Exam Engine's expert teacher and tutor for "
            f"{course.title}. Teach the student in the voice of a clear, patient "
            "teacher. Answer the actual question; do not merely list syllabus labels "
            "or describe database coverage. Prefer stored course evidence, then the "
            "supplied web sources, then your subject knowledge. Cite source IDs in "
            "square brackets such as [S1] or [W1]. Never invent source IDs, page "
            "numbers, quotes or examination predictions. If sources are thin, say so "
            "and set confidence low. For grading requests, use the supplied question "
            "and answer; if one is missing, ask for it. For exam-focus requests, give "
            "evidence-based revision priorities, never a forecast.\n\n"
            "Write the answer as 2-4 short, connected paragraphs: (1) explain what the "
            "idea is and why it matters, (2) explain the method/formula or key "
            "distinctions, (3) give one short worked example or application, and "
            "(4) finish with a common mistake or exam tip. Use plain text only: no "
            "Markdown headings, no LaTeX, no dollar signs, no backticks. Write maths "
            "with ordinary Unicode symbols such as x², ±, √, ≤ and ≠. Keep it about "
            "250 words. Return only a JSON object with exactly these keys: answer "
            "(string), key_points (array of up to 5 strings), citations (array of "
            "source IDs), confidence (one of high, medium, low), follow_ups (array of "
            "up to 4 short strings)."
        )
        user = "\n".join(
            [
                f"Course: {course.title} ({course.course_code})",
                f"Question: {question}",
                f"Exact syllabus match: {match_text}",
                "Stored course concepts:",
                "\n".join(local_lines) or "(none)",
                "Stored evidence excerpts:",
                "\n".join(evidence_lines) or "(none)",
                "Web sources:",
                "\n".join(web_lines) or "(none)",
                f"Allowed citation IDs: {', '.join(allowed_ids)}",
                (
                    "No exact concept matched the wording. Say briefly that you are using "
                    "the closest stored topics and web sources, then answer the question."
                    if match is None
                    else "Tie the answer back to the stored course evidence."
                ),
                "If the web sources do not resolve the question, use pretrained subject "
                "knowledge and label it as general knowledge in the answer.",
                "Teach the idea rather than reporting coverage statistics. Keep the "
                "answer accurate and about 250 words.",
                "Include one short worked example or concrete application in the answer; "
                "for a calculation, show the substitution and result.",
                "Use plain text and Unicode maths; never emit LaTeX or Markdown.",
            ]
        )
        _progress(
            on_event,
            "model",
            f"Asking local model {status.model or 'the configured model'} for a grounded answer…",
        )
        chunk_callback = (
            (lambda text: _answer_delta(on_event, text)) if on_event is not None else None
        )
        try:
            payload = self.ai.chat_json(system, user, on_answer_chunk=chunk_callback)
        except TutorAIError as exc:
            _progress(on_event, "fallback", "The model did not finish; using stored evidence.")
            return None, exc

        answer = _tidy_model_text(_text_value(payload.get("answer")))
        if not answer:
            _progress(
                on_event,
                "fallback",
                "The model returned no answer text; using stored evidence.",
            )
            return None, TutorAIError(
                "tutor_empty_answer",
                "The tutor model returned no answer text.",
            )
        key_points = _text_list(payload.get("key_points"), limit=5)
        follow_ups = _text_list(payload.get("follow_ups"), limit=4)
        confidence = str(payload.get("confidence") or "medium").strip().casefold()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        requested = _text_list(payload.get("citations"), limit=12)
        citation_map = {**local_citations, **web_citations}
        citation_ids = [source_id for source_id in requested if source_id in citation_map]
        if not citation_ids:
            citation_ids = list(web_citations)[:3]
            if match is not None and local_citations:
                citation_ids.insert(0, next(iter(local_citations)))
            if not citation_ids:
                citation_ids = list(local_citations)[:2]
        answer_citations = [citation_map[source_id] for source_id in citation_ids]

        ai_sections = [
            {
                "heading": "Tutor answer",
                "text": answer,
                "citations": answer_citations,
            }
        ]
        if key_points:
            ai_sections.append(
                {
                    "heading": "Key points",
                    "text": "\n".join(f"• {point}" for point in key_points),
                    "citations": [],
                }
            )
        if match is not None:
            units = ", ".join(match.unit_labels) or "no unit placement"
            connection = (
                f"This answer is tied to {match.label} in {units}. "
                f"The stored corpus maps {match.question_count} question(s) to it."
            )
        else:
            connection = (
                "No exact syllabus label matched. I used the closest stored topics and "
                "cited web sources; use the suggested concepts to narrow the question."
            )
        ai_sections.append(
            {
                "heading": "Course connection",
                "text": connection,
                "citations": [citation_map[next(iter(local_citations))]] if local_citations else [],
            }
        )

        suggestions = [
            {
                "concept_id": item.concept_id,
                "label": item.label,
                "unit_labels": list(item.unit_labels),
            }
            for item in ranked
            if match is None or item.concept_id != match.concept_id
        ][:8]
        limitations = [
            "AI-generated explanation; verify the cited sources before relying on it.",
            "The model may use general subject knowledge when stored and web sources do "
            "not cover a detail.",
        ]
        if not web_sources:
            limitations.append(
                "No live web source was used; this answer relies on stored course "
                "material and the model's pretrained subject knowledge."
            )
        if match is None:
            limitations.append(
                "No exact syllabus concept matched the wording; the suggested concepts "
                "are the closest stored topics."
            )
        return (
            {
                "course_id": course_id,
                "syllabus_id": syllabus_id,
                "question": question,
                "status": "answered",
                "state": "positive" if confidence == "high" else "speaking",
                "event": "tutor_answered",
                "concept": (
                    {
                        "concept_id": match.concept_id,
                        "node_id": match.node.node_id if match.node else None,
                        "label": match.label,
                        "unit_labels": list(match.unit_labels),
                        "coverage_status": (
                            match.coverage.status.value if match.coverage else None
                        ),
                        "question_count": match.question_count,
                    }
                    if match is not None
                    else None
                ),
                "answer": answer,
                "sections": ai_sections,
                "evidence": answer_citations,
                "related": [],
                "prerequisites": [],
                "learning_path": None,
                "suggestions": suggestions,
                "follow_ups": follow_ups,
                "limitations": limitations,
                "capabilities": self.capabilities(),
                "recovery": None,
                "warnings": [],
                "confidence": confidence,
                "ai": {
                    "provider": status.provider,
                    "model": status.model,
                    "web_used": bool(web_sources),
                    "web_sources": len(web_sources),
                },
            },
            None,
        )

    def _fast_web_answer(
        self,
        course_id: str,
        syllabus_id: str | None,
        question: str,
        match: _Candidate | None,
        ranked: list[_Candidate],
        web_sources: list[Any],
        web_citations: dict[str, dict[str, Any]],
        local_citations: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Compose a cited answer from web evidence without starting the model."""

        composed = _compose_web_answer(question, match, web_sources)
        if composed is None:
            return None
        answer_citations = [
            web_citations[source_id]
            for source_id in composed.citation_ids
            if source_id in web_citations
        ]
        if not answer_citations:
            return None

        sections: list[dict[str, Any]] = [
            {
                "heading": "Tutor answer",
                "text": composed.answer,
                "citations": answer_citations,
            }
        ]
        if composed.key_points:
            sections.append(
                {
                    "heading": "Key points",
                    "text": "\n".join(f"• {point}" for point in composed.key_points),
                    "citations": answer_citations,
                }
            )

        if match is not None:
            units = ", ".join(match.unit_labels) or "no unit placement"
            connection = (
                f"This answer is tied to {match.label} in {units}. "
                f"The stored corpus maps {match.question_count} question(s) to it."
            )
        else:
            connection = (
                "No exact syllabus label matched. I used cited web sources; "
                "use the suggested concepts below to narrow the question."
            )
        connection_citations = (
            [local_citations[next(iter(local_citations))]] if local_citations else []
        )
        sections.append(
            {
                "heading": "Course connection",
                "text": connection,
                "citations": connection_citations,
            }
        )

        suggestions = [
            {
                "concept_id": item.concept_id,
                "label": item.label,
                "unit_labels": list(item.unit_labels),
            }
            for item in ranked
            if match is None or item.concept_id != match.concept_id
        ][:8]
        limitations = [
            "This answer was composed from live cited web sources instead of generated "
            "by the local model, so it can return within the latency budget.",
            "Live web sources can be incomplete or simplified; verify important formulas "
            "and assumptions before relying on them.",
        ]
        if match is None:
            limitations.append(
                "No exact syllabus concept matched the wording; the suggested concepts "
                "are the closest stored topics."
            )

        return {
            "course_id": course_id,
            "syllabus_id": syllabus_id,
            "question": question,
            "status": "answered",
            "state": "speaking",
            "event": "tutor_answered",
            "concept": (
                {
                    "concept_id": match.concept_id,
                    "node_id": match.node.node_id if match.node else None,
                    "label": match.label,
                    "unit_labels": list(match.unit_labels),
                    "coverage_status": match.coverage.status.value if match.coverage else None,
                    "question_count": match.question_count,
                }
                if match is not None
                else None
            ),
            "answer": composed.answer,
            "sections": sections,
            "evidence": answer_citations,
            "related": [],
            "prerequisites": [],
            "learning_path": None,
            "suggestions": suggestions,
            "follow_ups": [],
            "limitations": limitations,
            "capabilities": self.capabilities(),
            "recovery": None,
            "warnings": [],
            "confidence": "medium",
            "ai": {
                "provider": "web",
                "model": None,
                "web_used": True,
                "web_sources": len(web_sources),
            },
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
            label = " ".join(candidate.label.casefold().split())
            if len(label) >= 3 and re.search(rf"(?<!\w){re.escape(label)}(?!\w)", lowered):
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


def _compact_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text.strip(" :;-\n\t")


def _display_title(value: str) -> str:
    title = _compact_text(value)
    if title and title.upper() == title:
        return title.title()
    return title


def _strip_unit_heading(raw_text: str, title: str | None) -> str:
    text = _compact_text(raw_text)
    if not text:
        return ""
    text = re.sub(r"(?i)^UNIT\s*\d+\s*:\s*", "", text)
    if title:
        text = re.sub(
            rf"(?i)^{re.escape(_compact_text(title))}\s*:\s*",
            "",
            text,
        )
    return text.strip(" :;-\n\t")


_LABEL_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "between",
        "by",
        "for",
        "from",
        "in",
        "into",
        "its",
        "of",
        "on",
        "or",
        "section",
        "that",
        "the",
        "their",
        "them",
        "these",
        "this",
        "those",
        "to",
        "using",
        "when",
        "with",
    }
)


def _usable_label(label: str) -> bool:
    text = _compact_text(label)
    if len(text) < 8 or len(text) > 220:
        return False
    words = _TOKEN_RE.findall(text.casefold())
    if len(words) < 2:
        return False
    if words[0] in _LABEL_STOP_WORDS or words[-1] in _LABEL_STOP_WORDS:
        return False
    if any(len(token) > 24 for token in words):
        return False
    if "," in text and ", " not in text:
        return False
    if text.startswith(("(", "[")):
        return False
    if "." in text:
        return False
    return not text.endswith((",", ":", ";", "—", "-"))


def _syllabus_clauses(raw_text: str) -> list[str]:
    compact = _compact_text(raw_text)
    if not compact:
        return []
    clauses: list[str] = []
    for part in re.split(r"[;.\n]", compact):
        clause = _compact_text(part)
        if len(clause) >= 8 and clause not in clauses:
            clauses.append(clause)
    return clauses


def _target_clause(raw_text: str, label: str) -> str:
    compact = _compact_text(raw_text)
    if not compact:
        return label
    exact = compact.casefold().find(label.casefold())
    if exact >= 0:
        before = max(
            compact.rfind(";", 0, exact),
            compact.rfind(".", 0, exact),
            compact.rfind("\n", 0, exact),
            compact.rfind(",", 0, exact),
        )
        after = [
            position
            for position in (
                compact.find(";", exact + len(label)),
                compact.find(".", exact + len(label)),
                compact.find("\n", exact + len(label)),
            )
            if position >= 0
        ]
        end = min(after) if after else min(len(compact), exact + len(label) + 180)
        clause = _compact_text(compact[before + 1 : end])
        if clause:
            return clause
    clauses = _syllabus_clauses(compact)
    label_tokens = {
        token for token in _TOKEN_RE.findall(label.casefold()) if token not in _STOPWORDS
    }
    best = ""
    best_score = 0.0
    for clause in clauses:
        clause_tokens = {
            token
            for token in _TOKEN_RE.findall(clause.casefold())
            if token not in _STOPWORDS
        }
        if not clause_tokens or not label_tokens:
            continue
        score = len(label_tokens & clause_tokens) / len(label_tokens)
        if score > best_score:
            best = clause
            best_score = score
    if best:
        return best[:500]
    return label


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WEB_FOCUS_TERMS = (
    "specific energy",
    "critical flow",
    "hydraulic jump",
    "uniform flow",
    "gradually varied",
    "water surface",
    "formula",
    "equation",
)
_WEB_META_PREFIXES = (
    "this document",
    "this article",
    "this chapter",
    "this paper",
    "this lecture",
    "this section",
    "this page",
    "these notes",
    "lecture note",
    "detailed discussion",
    "various calculations",
    "the thirteen possible",
    "the fundamental types",
    "definition of variables",
    "m=",
    "conjugate depths relationships",
    "where:",
    "equation ",
    "figure ",
    "this figure",
    "table ",
    "see also",
    "the document",
    "the article",
    "abstract ",
)
_WEB_EXPLANATORY = (
    " is ",
    " are ",
    " occurs ",
    " means ",
    " describes ",
    " when ",
    " because ",
    " can ",
    " uses ",
    " depends ",
    " equals ",
    " defined as ",
)


def _question_terms(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(text.casefold())
        if token not in _STOPWORDS and len(token) > 1
    }


_WEB_TOPIC_SPECS = (
    ("Specific energy", ("specific energy",)),
    ("Critical flow", ("critical flow", "critical depth", "froude number")),
    ("Hydraulic jump", ("hydraulic jump",)),
    ("Uniform flow", ("uniform flow", "normal depth")),
    ("Gradually varied flow", ("gradually varied", "gradually varying")),
    ("Water-surface profiles", ("water surface", "surface profile", "surface water profiles")),
)


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text) is not None


def _web_topic_specs(question: str) -> list[tuple[str, tuple[str, ...]]]:
    lowered = question.casefold()
    return [
        (label, keywords)
        for label, keywords in _WEB_TOPIC_SPECS
        if any(_contains_phrase(lowered, keyword) for keyword in keywords)
    ]


def _compose_web_answer(
    question: str,
    match: _Candidate | None,
    sources: list[Any],
) -> _WebAnswer | None:
    """Select cited, question-relevant sentences from retrieved web text.

    This is deliberately extractive: the tutor must not invent facts or sources
    just to avoid a model call. A weak or off-topic result returns ``None`` so the
    normal stored-evidence fallback can take over.
    """

    terms = _question_terms(question)
    if match is not None:
        terms |= _question_terms(match.label)
    if len(terms) < 2:
        return None

    ranked: list[tuple[float, int, int, str, set[str]]] = []
    seen: set[str] = set()
    for source_index, source in enumerate(sources, start=1):
        snippet = str(getattr(source, "snippet", "") or "")
        for sentence_index, raw in enumerate(_SENTENCE_RE.split(snippet)):
            sentence = " ".join(raw.split())
            if len(sentence) < 50 or len(sentence) > 520:
                continue
            key = sentence.casefold()
            if key in seen:
                continue
            if key.startswith(_WEB_META_PREFIXES):
                continue
            if sentence[:1].islower():
                continue
            if sentence.endswith(("...", "…")):
                continue
            if len(sentence) > 350 and "=" in sentence:
                continue
            tokens = set(_TOKEN_RE.findall(key))
            overlap = tokens & terms
            if len(overlap) < 2:
                continue
            symbol_count = sum(character in "=+−×÷^/{}[]" for character in sentence)
            if symbol_count / max(1, len(sentence)) > 0.06:
                continue
            score = float(len(overlap)) + min(len(sentence), 500) / 1000.0
            if any(focus in key for focus in _WEB_FOCUS_TERMS):
                score += 1.0
            if any(marker in key for marker in _WEB_EXPLANATORY):
                score += 0.8
            if any(
                word in key
                for word in ("sink", "household", "caltech", "external link", "this example")
            ):
                score -= 2.0
            if len(sentence) > 280:
                score -= (len(sentence) - 280) / 200.0
            seen.add(key)
            ranked.append((score, source_index, sentence_index, sentence, tokens))

    if len(ranked) < 2:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)

    selected: list[tuple[str, tuple[float, int, int, str, set[str]]]] = []
    used: set[tuple[int, int]] = set()
    specs = _web_topic_specs(question)

    def best_matching(
        keywords: tuple[str, ...],
    ) -> tuple[float, int, int, str, set[str]] | None:
        matches: list[tuple[float, int, int, str, set[str]]] = []
        for item in ranked:
            if (item[1], item[2]) in used:
                continue
            lowered = item[3].casefold()
            if any(_contains_phrase(lowered, keyword) for keyword in keywords):
                matches.append(item)
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: (
                any(item[3].casefold().startswith(keyword) for keyword in keywords),
                item[0],
            ),
        )

    overview = best_matching(("in fluid mechanics", "open-channel flow is"))
    if overview is not None:
        selected.append(("Overview", overview))
        used.add((overview[1], overview[2]))

    for label, keywords in specs:
        best = best_matching(keywords)
        if best is not None:
            selected.append((label, best))
            used.add((best[1], best[2]))

    # Topic matching can leave gaps when result snippets are terse. Fill the
    # answer with the strongest remaining sentences so it still reads as an
    # explanation rather than a label.
    if not specs or len(selected) < 2:
        for item in ranked:
            if len(selected) >= 3:
                break
            if (item[1], item[2]) not in used:
                selected.append(("Further detail", item))
                used.add((item[1], item[2]))
    if len(selected) < 2:
        return None

    answer = "\n".join(f"{label}: {item[3]} [W{item[1]}]" for label, item in selected)
    if len(answer) < 120:
        return None

    points: list[str] = []
    for _label, item in selected[:4]:
        sentence = item[3]
        if len(sentence) > 220:
            sentence = sentence[:217].rsplit(" ", 1)[0] + "…"
        points.append(sentence)
    citation_ids = sorted({f"W{item[1]}" for _label, item in selected})
    return _WebAnswer(answer=answer, key_points=points, citation_ids=citation_ids)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _tidy_model_text(value: str) -> str:
    """Remove formatting that renders poorly in the plain-text tutor UI."""

    text = value.strip()
    if not text:
        return ""
    text = text.replace("$$", "").replace("$", "")
    text = text.replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    text = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"√(\1)", text)
    text = re.sub(r"\\text\s*\{([^{}]+)\}", r"\1", text)
    replacements = {
        "\\pm": "±",
        "\\times": "×",
        "\\cdot": "·",
        "\\le": "≤",
        "\\ge": "≥",
        "\\neq": "≠",
        "\\alpha": "α",
        "\\beta": "β",
        "\\theta": "θ",
        "\\pi": "π",
        "\\infty": "∞",
        "\\to": "→",
        "/pm": "±",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _text_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


__all__ = ["TutorService"]
