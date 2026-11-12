"""Answer recovery with explicit provenance.

Order of evidence:
1. an answer already stored in the row (never overwritten);
2. an identical question in another validated/local record;
3. an explicitly stated final answer in a worked solution;
4. optional official/reputable web leads (recorded as leads, not accepted facts);
5. optional AI solution (never labelled official).

Every candidate records its source, method, verification state and confidence.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from exam_engine.datasets.parsing import ParsedOption, extract_correct_option
from exam_engine.ingest.normalize import fingerprint_text
from exam_engine.models.recovery import AnswerCandidate, Issue, Repair, now_iso
from exam_engine.recovery.structure import extract_solution_answer


@dataclass(frozen=True)
class LocalAnswer:
    question_text: str
    options: tuple[tuple[str, str], ...]
    answer_text: str
    course_id: str
    source_file: str
    source_row: int
    source_question_id: str


class LocalAnswerIndex:
    """Exact and conservative near-duplicate local answer index."""

    def __init__(self) -> None:
        self._items: dict[str, list[LocalAnswer]] = {}
        self._all: list[LocalAnswer] = []
        self._token_index: dict[str, set[int]] = defaultdict(set)

    @staticmethod
    def key(question_text: str) -> str:
        return fingerprint_text(question_text)

    @staticmethod
    def _tokenize(question_text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", question_text.casefold())
            if len(token) > 2
        }

    def add(self, item: LocalAnswer) -> None:
        self._items.setdefault(self.key(item.question_text), []).append(item)
        index = len(self._all)
        self._all.append(item)
        for token in self._tokenize(item.question_text):
            self._token_index[token].add(index)

    def find(self, question_text: str) -> list[LocalAnswer]:
        return list(self._items.get(self.key(question_text), ()))

    def find_near(
        self, question_text: str, *, threshold: float = 0.96, max_scan: int = 25
    ) -> list[tuple[LocalAnswer, float]]:
        """Return unique high-similarity matches, bounded for large corpora.

        Token blocking avoids comparing every missing answer with every indexed
        answer. The final decision still uses a sequence similarity and requires
        compatible options before any answer is accepted.
        """

        wanted = self._tokenize(question_text)
        if not wanted:
            return []
        # Rare tokens are substantially more selective than common words.
        postings = [
            self._token_index.get(token, set())
            for token in sorted(wanted, key=lambda value: len(self._token_index.get(value, ())))[:3]
        ]
        postings = [item for item in postings if item]
        candidate_ids: set[int] = set()
        if postings:
            candidate_ids = set.intersection(*postings)
            if not candidate_ids:
                candidate_ids = set().union(*postings)
        if len(candidate_ids) > max_scan:
            candidate_ids = set(sorted(candidate_ids)[:max_scan])
        wanted_text = " ".join(question_text.casefold().split())
        scored: list[tuple[LocalAnswer, float]] = []
        for index in candidate_ids:
            item = self._all[index]
            candidate_text = " ".join(item.question_text.casefold().split())
            matcher = difflib.SequenceMatcher(None, wanted_text, candidate_text)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            score = matcher.ratio()
            if score >= threshold:
                scored.append((item, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    def __len__(self) -> int:
        return len(self._all)


@dataclass
class AnswerDecision:
    answer_text: str | None
    answer_status: str
    candidates: list[AnswerCandidate] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    correct_option_key: str | None = None


def _option_same(left: list[ParsedOption], right: tuple[tuple[str, str], ...]) -> bool:
    if not left and not right:
        return True
    if len(left) != len(right):
        return False
    return all(
        option.key == key and " ".join(option.text.casefold().split()) == " ".join(text.casefold().split())
        for option, (key, text) in zip(left, right, strict=False)
    )


def _candidate(
    answer: str | None,
    *,
    source_type: str,
    source_name: str,
    status: str,
    confidence: float,
    method: str,
    source_reference: str | None = None,
    source_url: str | None = None,
    evidence: dict[str, Any] | None = None,
    official: bool = False,
) -> AnswerCandidate:
    return AnswerCandidate(
        answer_text=answer,
        source_type=source_type,
        source_name=source_name,
        verification_status=status,
        confidence=confidence,
        method=method,
        source_reference=source_reference,
        source_url=source_url,
        evidence=evidence or {},
        retrieved_at=now_iso(),
        official=official,
    )


def recover_answer(
    *,
    question_text: str,
    options: list[ParsedOption],
    answer_text: str | None,
    answer_type: str,
    solution_steps: str | None,
    course_id: str,
    source_file: str,
    source_row: int,
    source_question_id: str,
    index: LocalAnswerIndex,
    source_leads: list[AnswerCandidate] | None = None,
    official_key: str | None = None,
    official_source_name: str | None = None,
    official_source_reference: str | None = None,
    official_source_url: str | None = None,
    official_source_evidence: dict[str, Any] | None = None,
    model_solver: "ModelSolver | None" = None,
    deterministic_solver: Any = None,
    model_used: int = 0,
    model_limit: int = 0,
) -> AnswerDecision:
    candidates: list[AnswerCandidate] = []
    issues: list[Issue] = []
    repairs: list[Repair] = []
    correct_key = extract_correct_option(answer_text, answer_type, options)

    if answer_text:
        candidates.append(
            _candidate(
                answer_text,
                source_type="csv_row",
                source_name=source_file,
                status="source_stored_unverified",
                confidence=0.45,
                method="source_row",
                source_reference=f"{source_file}#row={source_row}",
                evidence={"answer_type": answer_type, "correct_option_key": correct_key},
            )
        )
        if options and answer_type in {"mcq_option", "multiple_options"} and not correct_key:
            issues.append(
                Issue(
                    code="answer.correct_option_not_identified",
                    category="answer_integrity",
                    severity="warning",
                    message="The stored MCQ answer cannot be matched unambiguously to an option key.",
                    evidence={"answer": answer_text[:240], "options": [o.as_dict() for o in options]},
                    confidence=0.9,
                    remediation="Review the answer key against the normalized options; do not overwrite the answer.",
                    field="answer_text",
                )
            )
        elif correct_key:
            repairs.append(
                Repair(
                    repair_type="recover_correct_option_key",
                    field="correct_option_key",
                    before=None,
                    after=correct_key,
                    method="answer_to_option_match",
                    confidence=0.98,
                    risk="low",
                    reason="The answer text unambiguously identifies one option.",
                )
            )
            return AnswerDecision(
                answer_text=answer_text,
                answer_status="structured_extraction",
                candidates=candidates,
                issues=issues,
                repairs=repairs,
                correct_option_key=correct_key,
            )
        return AnswerDecision(
            answer_text=answer_text,
            answer_status="source_stored_unverified",
            candidates=candidates,
            issues=issues,
            repairs=repairs,
            correct_option_key=correct_key,
        )

    matches = [
        item
        for item in index.find(question_text)
        if item.source_file != source_file or item.source_row != source_row
    ]
    near_scores: dict[tuple[str, int, str], float] = {}
    if not matches:
        near_matches = [
            (item, score)
            for item, score in index.find_near(question_text)
            if item.source_file != source_file or item.source_row != source_row
        ]
        matches = [item for item, _ in near_matches]
        near_scores = {
            (item.source_file, item.source_row, item.source_question_id): score
            for item, score in near_matches
        }
    compatible = [item for item in matches if _option_same(options, item.options)]
    unique_answers: dict[str, list[LocalAnswer]] = {}
    for item in compatible:
        unique_answers.setdefault(" ".join(item.answer_text.split()), []).append(item)
    if len(unique_answers) > 1:
        issues.append(
            Issue(
                code="answer.conflicting_local_evidence",
                category="answer_integrity",
                severity="error",
                message="Identical questions have different stored answers in local datasets.",
                evidence={
                    "answers": [
                        {
                            "answer": answer,
                            "references": [
                                f"{item.source_file}#row={item.source_row}" for item in items
                            ],
                        }
                        for answer, items in unique_answers.items()
                    ]
                },
                confidence=0.95,
                remediation="Do not choose one automatically; require an official/verified source.",
                blocking=True,
                field="answer_text",
            )
        )
        return AnswerDecision(
            answer_text=None,
            answer_status="conflicting",
            candidates=[
                _candidate(
                    None,
                    source_type="local_conflict",
                    source_name="local duplicate index",
                    status="conflicting",
                    confidence=0.95,
                    method="duplicate_answer_conflict",
                    evidence={"answer_count": len(unique_answers)},
                )
            ],
            issues=issues,
        )
    if len(unique_answers) == 1:
        recovered, items = next(iter(unique_answers.items()))
        distinct_courses = {item.course_id for item in items}
        corroborated = len(distinct_courses) >= 2
        similarity = max(
            (
                near_scores.get(
                    (item.source_file, item.source_row, item.source_question_id), 1.0
                )
                for item in items
            ),
            default=1.0,
        )
        is_exact = all(
            LocalAnswerIndex.key(item.question_text) == LocalAnswerIndex.key(question_text)
            for item in items
        )
        candidates.append(
            _candidate(
                recovered,
                source_type="local_duplicate",
                source_name="local dataset duplicate",
                status="corroborated_local" if corroborated else "local_duplicate",
                confidence=(0.88 if corroborated else 0.72) * similarity,
                method=(
                    "exact_question_and_option_match"
                    if is_exact
                    else "near_duplicate_question_and_option_match"
                ),
                source_reference=", ".join(
                    f"{item.source_file}#row={item.source_row}" for item in items[:5]
                ),
                evidence={
                    "independent_courses": sorted(distinct_courses),
                    "matching_rows": len(items),
                    "scope": "cross-course" if corroborated else "same-course",
                    "similarity": round(similarity, 4),
                    "exact_text_match": is_exact,
                },
            )
        )
        repairs.append(
            Repair(
                repair_type="recover_answer_from_local_duplicate",
                field="answer_text",
                before=None,
                after=recovered,
                method=(
                    "exact_question_duplicate" if is_exact else "near_question_duplicate"
                ),
                confidence=(0.88 if corroborated else 0.72) * similarity,
                risk="medium",
                reason=(
                    "Identical question and options found in another course."
                    if corroborated
                    else "Identical question and options found in the same file/course."
                    if is_exact
                    else "Near-identical question and compatible options found locally."
                ),
            )
        )
        return AnswerDecision(
            answer_text=recovered,
            answer_status="corroborated_local" if corroborated else "local_duplicate",
            candidates=candidates,
            issues=issues,
            repairs=repairs,
            correct_option_key=extract_correct_option(
                recovered, "mcq_option" if options else answer_type, options
            ),
        )

    if official_key:
        official = _candidate(
            official_key,
            source_type="official_answer_key",
            source_name=official_source_name or "official answer key",
            status="official_verified",
            confidence=0.98,
            method="official_answer_key_lookup",
            source_reference=official_source_reference,
            source_url=official_source_url,
            evidence={
                "official_key": True,
                **(official_source_evidence or {}),
            },
            official=True,
        )
        candidates.append(official)
        repairs.append(
            Repair(
                repair_type="recover_answer_from_official_key",
                field="answer_text",
                before=None,
                after=official_key,
                method="official_answer_key_lookup",
                confidence=0.98,
                risk="low",
                reason="An explicit official answer-key entry matched this question number.",
            )
        )
        return AnswerDecision(
            answer_text=official_key,
            answer_status="official_verified",
            candidates=candidates,
            issues=issues,
            repairs=repairs,
            correct_option_key=extract_correct_option(
                official_key, "mcq_option" if options else answer_type, options
            ),
        )

    extracted = extract_solution_answer(solution_steps)
    if extracted:
        status = "structured_extraction"
        candidates.append(
            _candidate(
                extracted,
                source_type="solution_steps",
                source_name=source_file,
                status=status,
                confidence=0.8,
                method="explicit_final_answer_marker",
                source_reference=f"{source_file}#row={source_row}",
                evidence={"solution_excerpt": " ".join((solution_steps or "").split())[-300:]},
            )
        )
        repairs.append(
            Repair(
                repair_type="recover_answer_from_solution",
                field="answer_text",
                before=None,
                after=extracted,
                method="explicit_final_answer_marker",
                confidence=0.8,
                risk="medium",
                reason="The worked solution explicitly states a final answer.",
            )
        )
        return AnswerDecision(
            answer_text=extracted,
            answer_status=status,
            candidates=candidates,
            issues=issues,
            repairs=repairs,
            correct_option_key=extract_correct_option(
                extracted, "mcq_option" if options else answer_type, options
            ),
        )

    if source_leads:
        candidates.extend(source_leads)
    if deterministic_solver is not None:
        solved = deterministic_solver.solve(question_text)
        if solved is not None:
            candidate = solved.as_candidate(
                source_reference=f"{source_file}#row={source_row}"
            )
            candidates.append(candidate)
            repairs.append(
                Repair(
                    repair_type="recover_answer_with_deterministic_solver",
                    field="answer_text",
                    before=None,
                    after=solved.answer,
                    method=solved.method,
                    confidence=solved.confidence,
                    risk="high",
                    status="proposed",
                    reason="A deterministic numeric solution is a review candidate, not an official answer.",
                )
            )
            return AnswerDecision(
                answer_text=solved.answer,
                answer_status="review_required",
                candidates=candidates,
                issues=issues,
                repairs=repairs,
                correct_option_key=extract_correct_option(
                    solved.answer, "mcq_option" if options else answer_type, options
                ),
            )
    if model_solver is not None and model_used < model_limit and model_solver.available:
        solved = model_solver.solve(
            question_text=question_text,
            options=[option.as_dict() for option in options],
            course_id=course_id,
            source_reference=f"{source_file}#row={source_row}",
        )
        if solved is not None:
            candidates.append(solved)
            if solved.answer_text:
                repairs.append(
                    Repair(
                        repair_type="recover_answer_with_model",
                        field="answer_text",
                        before=None,
                        after=solved.answer_text,
                        method="model_solution_unverified",
                        confidence=solved.confidence,
                        risk="high",
                        status="proposed",
                        reason="AI-generated answer awaiting independent verification; never official.",
                    )
                )
                return AnswerDecision(
                    answer_text=solved.answer_text,
                    answer_status="ai_generated_unverified",
                    candidates=candidates,
                    issues=issues,
                    repairs=repairs,
                )
    candidates.append(
        _candidate(
            None,
            source_type="none",
            source_name="no reliable source",
            status="not_recoverable",
            confidence=0.0,
            method="exhausted_local_sources",
            evidence={"source_leads": len(source_leads or [])},
        )
    )
    return AnswerDecision(
        answer_text=None,
        answer_status="not_recoverable",
        candidates=candidates,
        issues=issues,
        repairs=repairs,
    )


class ModelSolver(Protocol):
    @property
    def available(self) -> bool: ...

    def solve(
        self,
        *,
        question_text: str,
        options: list[dict[str, str]],
        course_id: str,
        source_reference: str,
    ) -> AnswerCandidate | None: ...


class TutorAnswerSolver:
    """Thin adapter over the existing tutor model; output is always unverified."""

    def __init__(self, tutor_settings: Any) -> None:
        from exam_engine.dashboard.tutor_ai import TutorAI

        self._ai = TutorAI(tutor_settings)

    @property
    def available(self) -> bool:
        return self._ai.status().available

    def solve(
        self,
        *,
        question_text: str,
        options: list[dict[str, str]],
        course_id: str,
        source_reference: str,
    ) -> AnswerCandidate | None:
        options_text = "\n".join(f"{item['key']}. {item['text']}" for item in options)
        system = (
            "You are an examination answer solver. Return JSON only. "
            "If the question cannot be answered reliably, use abstain=true. "
            "Never claim access to an official answer key."
        )
        user = (
            f"Course: {course_id}\nQuestion: {question_text}\nOptions:\n{options_text or '(none)'}\n"
            'Return {"answer": "...", "confidence": 0.0, "reasoning": "...", "abstain": false}.'
        )
        try:
            payload = self._ai.chat_json(system, user)
        except Exception:  # noqa: BLE001 - optional recovery must not fail the run
            return None
        answer = payload.get("answer") if isinstance(payload, dict) else None
        if not isinstance(answer, str) or not answer.strip() or payload.get("abstain"):
            return None
        confidence = payload.get("confidence", 0.5)
        if not isinstance(confidence, (int, float)):
            confidence = 0.5
        return _candidate(
            " ".join(answer.split())[:1000],
            source_type="ai_model",
            source_name="configured tutor model",
            status="ai_generated_unverified",
            confidence=max(0.0, min(float(confidence), 0.75)),
            method="independent_model_solution",
            source_reference=source_reference,
            evidence={
                "reasoning": str(payload.get("reasoning") or "")[:1000],
                "note": "AI-generated candidate; not independently verified and never official.",
            },
            official=False,
        )


__all__ = [
    "AnswerDecision",
    "LocalAnswer",
    "LocalAnswerIndex",
    "LocalAnswerIndex.find_near",
    "ModelSolver",
    "TutorAnswerSolver",
    "recover_answer",
]
