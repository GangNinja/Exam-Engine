from __future__ import annotations

import pytest

from exam_engine.nlp.concept_quality import (
    concept_noise_reason,
    filter_meaningful_concepts,
    is_meaningful_concept,
)


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        ("NOT", "instruction_or_furniture"),
        ("PAPER", "instruction_or_furniture"),
        ("PDF", "instruction_or_furniture"),
        ("AB", "schematic_label"),
        ("A1", "option_or_part_label"),
        ("A(c)", "bare_notation"),
        ("P(b)", "bare_notation"),
        ("f(x)", "bare_notation"),
        ("CH2", "schematic_label"),
        ("of the matrix", "fragment_leading_connective"),
        ("Page 1 of 3", "page_artifact"),
        ("This checklist lightly normalises formatting", "source_furniture"),
        ("II", "roman_numeral"),
        ("SNAKE", "uppercase_fragment"),
        ("point are equal", "sentence_fragment"),
        ("compound events)", "unbalanced_parenthesis"),
    ],
)
def test_concept_gate_rejects_derived_noise(label: str, reason: str) -> None:
    assert concept_noise_reason(label) == reason
    assert is_meaningful_concept(label) is False


@pytest.mark.parametrize(
    "label",
    [
        "Total derivative",
        "Ohm's law",
        "escape velocity",
        "Random variables",
        "Probability mass function",
        "matrix",
        "BST",
        "DNA",
        "ATP",
        "AC",
        "C++",
        "O(n log n)",
        "H2O",
        "CO2",
        "C–X bond",
        "Binary search tree",
    ],
)
def test_concept_gate_preserves_legitimate_terms(label: str) -> None:
    assert concept_noise_reason(label) is None
    assert is_meaningful_concept(label) is True


def test_concept_filter_preserves_original_order() -> None:
    labels = ["Arrays", "NOT", "Binary search tree", "A1", "Graphs"]
    assert filter_meaningful_concepts(labels) == ["Arrays", "Binary search tree", "Graphs"]


def test_concept_gate_does_not_rewrite_labels() -> None:
    label = "  Ohm's law  "
    assert is_meaningful_concept(label) is True
    assert concept_noise_reason(label) is None
    # The helper is a predicate; it does not mutate or normalize the caller's value.
    assert label == "  Ohm's law  "
