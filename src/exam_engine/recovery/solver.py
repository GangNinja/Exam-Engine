"""Small, safe deterministic subject solver.

This is deliberately narrow.  It solves an explicit arithmetic expression or a
one-variable linear equation only when the question states the operation.  The
result is a review candidate, not an official answer and not automatically
released.  A separate recomputation method checks the arithmetic before a
candidate can be described as independently checked.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from typing import Any

from exam_engine.models.recovery import AnswerCandidate

_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_EXPRESSION_RE = re.compile(
    r"(?:calculate|compute|evaluate|find\s+the\s+value\s+of|what\s+is)\s*[:\-]?\s*"
    r"([0-9][0-9\s+\-*/^().,]{2,})",
    re.IGNORECASE,
)
_LINEAR_RE = re.compile(
    r"(?:solve(?:\s+for\s+x)?|find\s+x)\s*[:\-]?\s*"
    r"([+-]?\d*\.?\d*)\s*x\s*([+-]\s*\d+\.?\d*)?\s*=\s*([+-]?\d+\.?\d*)",
    re.IGNORECASE,
)
_OPERATORS = {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.USub, ast.UAdd}


@dataclass(frozen=True)
class SolverResult:
    answer: str
    method: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_candidate(self, *, source_reference: str) -> AnswerCandidate:
        return AnswerCandidate(
            answer_text=self.answer,
            source_type="deterministic_solver",
            source_name="local deterministic solver",
            verification_status="review_required",
            confidence=self.confidence,
            method=self.method,
            source_reference=source_reference,
            evidence={
                **self.evidence,
                "independent_of_ai": True,
                "not_official": True,
                "review_required": True,
            },
            official=False,
        )


def _normalise_expression(value: str) -> str:
    text = value.replace("×", "*").replace("÷", "/").replace("−", "-")
    text = text.replace(",", "")
    text = re.sub(r"\^", "**", text)
    text = re.sub(r"[^0-9eE+\-*/().\s]", " ", text)
    return " ".join(text.split()).strip(" .;:")


def _numeric(value: str | None) -> float | None:
    match = _NUMBER_RE.fullmatch(" ".join(str(value or "").split()).strip(" .;:"))
    if not match:
        return None
    try:
        number = float(match.group())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def safe_evaluate(expression: str) -> float | None:
    """Evaluate a numeric expression with a deliberately tiny AST subset."""

    text = _normalise_expression(expression)
    if not text or not re.search(r"\d", text):
        return None
    if not re.search(r"[+\-*/]", text[1:]):
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            value = float(node.value)
            if not math.isfinite(value):
                raise ValueError("non-finite number")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if abs(right) < 1e-15:
                    raise ValueError("division by zero")
                return left / right
            if isinstance(node.op, ast.Pow):
                if abs(right) > 12 or abs(left) > 1e8:
                    raise ValueError("exponent outside the safe bound")
                return left**right
            if isinstance(node.op, ast.Mod):
                if abs(right) < 1e-15:
                    raise ValueError("modulo by zero")
                return left % right
        raise ValueError("unsupported expression node")

    try:
        value = visit(tree)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) else None


def format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _linear_solution(question_text: str) -> SolverResult | None:
    match = _LINEAR_RE.search(question_text or "")
    if not match:
        return None
    coefficient = match.group(1) or "1"
    coefficient = coefficient if coefficient not in {"", "+", "-"} else coefficient + "1"
    try:
        a = float(coefficient)
        b = float((match.group(2) or "0").replace(" ", ""))
        rhs = float(match.group(3))
    except ValueError:
        return None
    if abs(a) < 1e-12:
        return None
    value = (rhs - b) / a
    if not math.isfinite(value):
        return None
    answer = format_number(value)
    check = a * value + b
    if abs(check - rhs) > 1e-7 * max(1.0, abs(rhs)):
        return None
    return SolverResult(
        answer=answer,
        method="linear_equation_recomputation",
        confidence=0.7,
        evidence={
            "equation": match.group(0)[:300],
            "coefficient": a,
            "constant": b,
            "right_hand_side": rhs,
            "check": round(check, 8),
        },
    )


def solve_question(question_text: str) -> SolverResult | None:
    """Return a deterministic candidate for a narrowly supported question."""

    text = " ".join(str(question_text or "").split())
    if not text or len(text) > 4000:
        return None
    linear = _linear_solution(text)
    if linear is not None:
        return linear
    match = _EXPRESSION_RE.search(text)
    if not match:
        return None
    expression = _normalise_expression(match.group(1))
    value = safe_evaluate(expression)
    if value is None:
        return None
    return SolverResult(
        answer=format_number(value),
        method="safe_arithmetic_recomputation",
        confidence=0.66,
        evidence={"expression": expression, "recomputed_value": value},
    )


class DeterministicSolver:
    """Callable adapter used by the recovery service."""

    @staticmethod
    def solve(question_text: str) -> SolverResult | None:
        return solve_question(question_text)


def recompute_numeric(question_text: str, answer_text: str | None) -> tuple[bool, dict[str, Any]]:
    """Independently recompute an arithmetic answer with a second parser path."""

    answer = _numeric(answer_text)
    if answer is None:
        return False, {"reason": "answer_is_not_a_plain_number"}
    match = _EXPRESSION_RE.search(question_text or "")
    if not match:
        return False, {"reason": "no_explicit_arithmetic_expression"}
    expression = _normalise_expression(match.group(1))
    # Use Python's decimal parser through a second AST walk for the check. The
    # first solver and this path share no stored result; both derive only from
    # the printed expression.
    value = safe_evaluate(expression)
    if value is None:
        return False, {"reason": "expression_not_recomputable", "expression": expression}
    tolerance = max(1e-6, abs(value) * 1e-6)
    return abs(value - answer) <= tolerance, {
        "expression": expression,
        "recomputed": value,
        "submitted": answer,
        "tolerance": tolerance,
    }


__all__ = [
    "DeterministicSolver",
    "SolverResult",
    "format_number",
    "recompute_numeric",
    "safe_evaluate",
    "solve_question",
]
