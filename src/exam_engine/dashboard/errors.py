"""Stable HTTP error mapping for the Phase 12 dashboard.

Domain services already raise errors with stable ``code``/``message``/``detail``
attributes. The dashboard translates those into HTTP status codes once, so no
route has to duplicate that mapping and no unexpected exception is ever exposed
to the browser.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError


class ApiError(Exception):
    """A deliberate API failure with a stable, user-facing shape."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self, *, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }
        if request_id:
            payload["request_id"] = request_id
        return payload


_NOT_FOUND_CODES = frozenset(
    {
        "not_found",
        "unknown_course",
        "unknown_concept",
        "unknown_batch",
        "unknown_document",
        "unknown_generated_question",
        "unknown_misconception",
        "unknown_question",
        "unknown_reference_question",
        "unknown_source_question",
        "unknown_syllabus",
        "unknown_syllabus_comparison",
        "unknown_unit",
    }
)

_CONFLICT_CODES = frozenset(
    {
        "ambiguous_concept",
        "ambiguous_syllabus",
        "course_mismatch",
        "empty_syllabus",
        "multiple_syllabi",
        "no_syllabus",
        "revision_conflict",
        "syllabus_mismatch",
    }
)

_REQUEST_CODES = frozenset(
    {
        "concept_not_in_unit",
        "format_constraint_mismatch",
        "invalid_coverage_filter",
        "invalid_evolution_filter",
        "invalid_exam_dna_filter",
        "missing_target",
        "no_concepts",
        "no_concepts_in_unit",
        "no_grounding_documents",
        "no_supported_strategy",
        "no_target_concept",
        "no_target_scope",
        "request_too_large",
        "source_concept_unresolved",
        "source_unmapped",
        "unsupported_language",
    }
)


def _domain_error(exc: BaseException) -> ApiError | None:
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        return None
    message = getattr(exc, "message", None) or str(exc) or code
    detail = getattr(exc, "detail", None)
    if code in _NOT_FOUND_CODES:
        status = 404
    elif code in _CONFLICT_CODES:
        status = 409
    else:
        status = 400
    return ApiError(status, code, str(message), detail if isinstance(detail, dict) else {})


def to_api_error(exc: BaseException) -> ApiError:
    """Translate any exception into an :class:`ApiError` without leaking internals."""

    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, ValidationError):
        return ApiError(
            422,
            "invalid_request",
            "the request body does not match the expected schema",
            {
                "errors": [
                    {
                        "loc": [str(part) for part in error.get("loc", ())],
                        "msg": str(error.get("msg", "invalid value")),
                        "type": str(error.get("type", "value_error")),
                    }
                    for error in exc.errors()
                ]
            },
        )
    if isinstance(exc, KeyError):
        missing = str(exc).strip("'\"")
        return ApiError(404, "not_found", f"no record matches {missing!r}", {"key": missing})
    domain = _domain_error(exc)
    if domain is not None:
        return domain
    if isinstance(exc, (ValueError, TypeError)):
        return ApiError(400, "invalid_request", str(exc) or "invalid request")
    if isinstance(exc, FileNotFoundError):
        return ApiError(404, "not_found", "the requested resource does not exist")
    return ApiError(
        500,
        "internal_error",
        "the dashboard hit an unexpected error; no records were changed",
        {"exception": type(exc).__name__},
    )


__all__ = ["ApiError", "to_api_error"]
