from __future__ import annotations

from exam_engine.ingest.normalize import (
    drop_page_artifacts,
    fingerprint_text,
    is_page_artifact,
    normalize_text,
)


def test_normalisation_preserves_maths_numbering_and_marks() -> None:
    raw = "1.  Solve   x^2 + 3x - 5 ≤ 0\r\n\r\n\r\n2. Derive T(n) = 2T(n/2) + O(n)   [10 marks]\r\n"
    cleaned = normalize_text(raw)
    assert "1. Solve x^2 + 3x - 5 ≤ 0" in cleaned
    assert "2. Derive T(n) = 2T(n/2) + O(n) [10 marks]" in cleaned
    assert "\r" not in cleaned
    assert "\n\n\n" not in cleaned


def test_normalisation_strips_control_and_zero_width_characters() -> None:
    raw = "Unit\u200b 1:\x00 Data\x07 Structures"
    cleaned = normalize_text(raw)
    assert cleaned == "Unit 1: Data Structures"


def test_page_artifacts_are_detected_but_content_is_not() -> None:
    assert is_page_artifact("Page 3 of 10")
    assert is_page_artifact("  - 12 -  ")
    assert is_page_artifact("pg. 4")
    # A bare number is preserved: it is indistinguishable from real content.
    assert not is_page_artifact("7")
    assert not is_page_artifact("3. Explain the difference.")
    assert not is_page_artifact("Unit 3: Trees")

    text = "Question text\nPage 2 of 5\n[5 marks]\n12"
    assert drop_page_artifacts(text) == "Question text\n[5 marks]\n12"


def test_fingerprint_ignores_case_and_spacing_but_not_formulae() -> None:
    assert fingerprint_text("Define a stack.") == fingerprint_text("  define  a STACK ")
    assert fingerprint_text("x^2 + 3x") != fingerprint_text("x^2 + 4x")
    assert fingerprint_text("") == fingerprint_text("   ")


def test_markdown_like_text_survives() -> None:
    raw = "## Unit 1\n- Topic A\n- Topic B"
    assert normalize_text(raw) == "## Unit 1\n- Topic A\n- Topic B"
