from __future__ import annotations

import json
from pathlib import Path

import pytest

from exam_engine.models.enums import (
    BloomLevel,
    DifficultyLabel,
    MappingStatus,
    QuestionType,
    ReferenceKind,
    SimilarityKind,
)
from exam_engine.models.question import QuestionRecord, Uncertainty
from exam_engine.models.syllabus import SyllabusUnitRecord
from exam_engine.understanding.classification import (
    classify_bloom,
    classify_question_types,
    estimate_difficulty,
)
from exam_engine.understanding.mapping import (
    MappingSettings,
    SemanticIndex,
    UnitSource,
    map_question,
)
from exam_engine.understanding.similarity import (
    ReasoningProfile,
    SimilaritySettings,
    find_similar_questions,
    normalise_question_text,
)
from exam_engine.understanding.structure import detect_structure
from exam_engine.understanding.taxonomy import (
    DEFAULT_TAXONOMY,
    TaxonomyError,
    load_taxonomy,
)

COURSE = "test-course-2024-v1"


def question(text: str, *, number: str | None = None, unit: str | None = None) -> QuestionRecord:
    return QuestionRecord(
        question_id="q_" + str(abs(hash(text)))[:12],
        document_id="doc_1",
        course_id=COURSE,
        text=text,
        question_number=number,
        unit=unit,
        confidence=0.9,
    )


def unit(
    label: str,
    *,
    unit_id: str,
    title: str,
    topics: list[str],
    number: int | None = None,
) -> SyllabusUnitRecord:
    return SyllabusUnitRecord(
        unit_id=unit_id,
        unit_label=label,
        unit_number=number,
        title=title,
        topics=topics,
        raw_text=f"{label}: {title}\n" + "\n".join(topics),
        confidence=0.9,
    )


# --- structure ---------------------------------------------------------------


def test_subparts_have_exact_spans_and_marks() -> None:
    text = (
        "Consider the following diagram and code below:\n"
        "(a) Explain the flow shown in the diagram. (2 marks)\n"
        "(b) Rewrite the code to avoid the bug. [3 marks]"
    )
    structure = detect_structure(question(text, number="1"))
    assert structure.subquestion_count == 2
    assert [part.label for part in structure.subparts] == ["a", "b"]
    assert [part.marks for part in structure.subparts] == [2.0, 3.0]
    for part in structure.subparts:
        assert text[part.start : part.end] == part.text
    assert {reference.kind for reference in structure.references} == {
        ReferenceKind.DIAGRAM,
        ReferenceKind.CODE,
    }
    for reference in structure.references:
        assert text[reference.start : reference.end] == reference.text


def test_inline_subparts_after_sentence_punctuation_are_detected() -> None:
    text = (
        "Read the following case study. (a) Identify the collision strategy. "
        "(b) Evaluate its impact on lookup time."
    )
    structure = detect_structure(question(text))
    assert [part.label for part in structure.subparts] == ["a", "b"]
    for part in structure.subparts:
        assert text[part.start : part.end] == part.text


def test_letter_parenthesis_subparts_are_detected() -> None:
    text = "Answer both parts: a) Define a stack.\nb) Compare it with a queue."
    structure = detect_structure(question(text))
    assert [part.label for part in structure.subparts] == ["a", "b"]
    for part in structure.subparts:
        assert text[part.start : part.end] == part.text


def test_numbered_material_references_are_detected() -> None:
    text = (
        "Figure 2 shows the pipeline. Table 1 gives the values. "
        "Listing 3 contains the code. The case studies in Section B are relevant."
    )
    structure = detect_structure(question(text))
    assert {reference.kind for reference in structure.references} >= {
        ReferenceKind.DIAGRAM,
        ReferenceKind.TABLE,
        ReferenceKind.CODE,
        ReferenceKind.CASE_STUDY,
    }
    for reference in structure.references:
        assert text[reference.start : reference.end] == reference.text


def test_inline_citation_is_not_a_subpart() -> None:
    structure = detect_structure(question("Use equation (2) from the notes to solve the problem."))
    assert structure.subparts == []
    assert structure.subquestion_count == 0


def test_low_confidence_extraction_and_missing_punctuation_are_recorded() -> None:
    weak = question("Define a stack").model_copy(update={"confidence": 0.4})
    structure = detect_structure(weak)
    codes = {item.code for item in structure.uncertainties}
    assert "low_extraction_confidence" in codes
    assert "missing_terminal_punctuation" in codes
    assert structure.confidence <= 0.55


def test_implausible_subpart_marks_are_recorded_without_crashing() -> None:
    text = "(a) Explain the first part. (150 marks)\n(b) Explain the second part."
    structure = detect_structure(question(text))
    assert structure.subquestion_count == 2
    assert [part.marks for part in structure.subparts] == [None, None]
    assert any(item.code == "implausible_subpart_marks" for item in structure.uncertainties)


def test_trailing_marks_are_not_mistaken_for_truncation() -> None:
    structure = detect_structure(
        question("A binary tree has 15 nodes. What is the maximum height? [8 marks]")
    )
    assert "missing_terminal_punctuation" not in {item.code for item in structure.uncertainties}


def test_empty_question_text_is_not_invented() -> None:
    structure = detect_structure(question("x").model_copy(update={"text": ""}))
    assert structure.subquestion_count == 0
    assert structure.confidence == 0.0
    assert structure.uncertainties[0].code == "empty_text"


def test_phase1_boundary_uncertainties_are_carried_into_structure() -> None:
    upstream = Uncertainty(
        code="numbering_gap",
        message="question 2 is missing from the source",
        detail={"expected": "2"},
    )
    record = question("Explain stacks.").model_copy(update={"uncertainties": [upstream]})
    structure = detect_structure(record)
    assert any(item.code == "numbering_gap" for item in structure.uncertainties)
    assert structure.subquestion_count == 0


# --- taxonomy ----------------------------------------------------------------


def test_builtin_taxonomy_documents_every_label() -> None:
    assert set(DEFAULT_TAXONOMY.question_types) == {item.value for item in QuestionType}
    assert set(DEFAULT_TAXONOMY.bloom_levels) == {item.value for item in BloomLevel}
    assert DEFAULT_TAXONOMY.version


def test_custom_taxonomy_can_change_a_weight(tmp_path: Path) -> None:
    payload = {
        "version": "dept-1",
        "question_types": {
            "definition": {
                "description": "custom",
                "cues": [{"pattern": r"\bstate\b", "weight": 2.0, "note": "custom cue"}],
            }
        },
        "bloom_levels": {"remember": {"description": "custom", "cues": [r"\bstate\b"]}},
    }
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    taxonomy = load_taxonomy(path)
    assert taxonomy.version == "dept-1"
    assert taxonomy.question_types["definition"].cues[0].weight == 2.0
    assert taxonomy.source.endswith("taxonomy.json")


def test_invalid_taxonomy_labels_and_patterns_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "version": "bad",
                "question_types": {
                    "made_up": {"description": "x", "cues": ["x"]},
                },
                "bloom_levels": {"remember": {"description": "x", "cues": ["x"]}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TaxonomyError):
        load_taxonomy(path)

    path.write_text(
        json.dumps(
            {
                "version": "bad",
                "question_types": {
                    "definition": {"description": "x", "cues": ["("]},
                },
                "bloom_levels": {"remember": {"description": "x", "cues": ["x"]}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TaxonomyError):
        load_taxonomy(path)


# --- classification ----------------------------------------------------------


def test_multi_label_question_type_and_bloom() -> None:
    text = "Define a stack and compare it with a queue."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    bloom = classify_bloom(text, structure, types, DEFAULT_TAXONOMY)
    labels = {item.label for item in types.labels}
    assert {"definition", "comparison"} <= labels
    assert types.primary in labels
    assert bloom.primary in {"remember", "understand", "analyze"}
    assert any(
        "type context: comparison demand" in evidence
        for item in bloom.labels
        for evidence in item.evidence
    )


@pytest.mark.parametrize(
    ("text", "type_label", "bloom_label"),
    [
        ("Evaluate the trade-offs of two graph representations.", "evaluation", "evaluate"),
        ("Design a hash table that handles collisions.", "design", "create"),
        ("Write pseudocode to detect a cycle in a linked list.", "programming", "create"),
        ("Calculate the maximum height of a binary tree with 15 nodes.", "numerical", "apply"),
    ],
)
def test_transparent_type_and_bloom_examples(text: str, type_label: str, bloom_label: str) -> None:
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    bloom = classify_bloom(text, structure, types, DEFAULT_TAXONOMY)
    assert types.primary == type_label
    assert bloom.primary == bloom_label


def test_language_name_alone_does_not_force_programming_type() -> None:
    text = "Explain how rust forms on iron."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    assert "programming" not in {label.label for label in types.labels}


def test_generic_implement_is_not_a_programming_question() -> None:
    text = "Implement a change management plan for the hospital."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    assert "programming" not in {label.label for label in types.labels}


def test_implementing_code_still_labels_programming() -> None:
    text = "Implement quicksort in Python."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    assert "programming" in {label.label for label in types.labels}


def test_explain_why_is_analyze_not_only_verb_matching() -> None:
    text = "Explain why comparison sorting cannot beat O(n log n) in the worst case."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    bloom = classify_bloom(text, structure, types, DEFAULT_TAXONOMY)
    assert bloom.primary == "analyze"
    evidence = " ".join(item for label in bloom.labels for item in label.evidence)
    assert "cause" in evidence or "causal" in evidence


def test_unknown_classifications_are_explicit() -> None:
    text = "Zzz qqq."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    bloom = classify_bloom(text, structure, types, DEFAULT_TAXONOMY)
    difficulty = estimate_difficulty(text, structure, types, bloom)
    assert types.labels == []
    assert types.primary is None
    assert any(item.code == "unknown_question_type" for item in types.uncertainties)
    assert bloom.labels == []
    assert bloom.primary is None
    assert any(item.code == "unknown_bloom_level" for item in bloom.uncertainties)
    assert difficulty.label is DifficultyLabel.UNKNOWN
    assert any(item.code == "difficulty_uncertain" for item in difficulty.uncertainties)


def test_marks_alone_do_not_assign_a_difficulty_band() -> None:
    text = "Zzz qqq."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    bloom = classify_bloom(text, structure, types, DEFAULT_TAXONOMY)
    difficulty = estimate_difficulty(text, structure, types, bloom, marks=100)
    assert difficulty.label is DifficultyLabel.UNKNOWN
    assert any(item.code == "difficulty_uncertain" for item in difficulty.uncertainties)


def test_empty_classification_has_zero_confidence() -> None:
    structure = detect_structure(question("x").model_copy(update={"text": ""}))
    types = classify_question_types("", structure, DEFAULT_TAXONOMY)
    bloom = classify_bloom("", structure, types, DEFAULT_TAXONOMY)
    difficulty = estimate_difficulty("", structure, types, bloom, marks=10)
    assert types.primary is None and bloom.primary is None
    assert difficulty.label is DifficultyLabel.UNKNOWN
    assert difficulty.confidence == 0.0


# --- difficulty --------------------------------------------------------------


def test_high_mark_proof_scores_harder_than_short_definition() -> None:
    definition_text = "Define a stack."
    definition_structure = detect_structure(question(definition_text))
    definition_types = classify_question_types(
        definition_text, definition_structure, DEFAULT_TAXONOMY
    )
    definition_bloom = classify_bloom(
        definition_text, definition_structure, definition_types, DEFAULT_TAXONOMY
    )
    definition = estimate_difficulty(
        definition_text,
        definition_structure,
        definition_types,
        definition_bloom,
        marks=2,
    )

    proof_text = (
        "Prove by induction that the recurrence relation for merge sort has "
        "the stated closed form, and derive the assumptions used at each step."
    )
    proof_structure = detect_structure(question(proof_text))
    proof_types = classify_question_types(proof_text, proof_structure, DEFAULT_TAXONOMY)
    proof_bloom = classify_bloom(proof_text, proof_structure, proof_types, DEFAULT_TAXONOMY)
    proof = estimate_difficulty(
        proof_text,
        proof_structure,
        proof_types,
        proof_bloom,
        marks=20,
    )
    assert proof.score > definition.score
    assert proof.confidence > 0.0
    assert any("weak signal" in line for line in proof.explanation)
    assert proof.features["reasoning_demand"] >= definition.features["reasoning_demand"]
    assert proof.features["marks"] == 20.0
    assert proof.features["marks_load"] == 1.0


def test_missing_marks_are_not_invented_in_difficulty() -> None:
    text = "Describe the difference between arrays and linked lists."
    structure = detect_structure(question(text))
    types = classify_question_types(text, structure, DEFAULT_TAXONOMY)
    bloom = classify_bloom(text, structure, types, DEFAULT_TAXONOMY)
    result = estimate_difficulty(text, structure, types, bloom, marks=None)
    assert any("no marks were stated" in line for line in result.explanation)
    assert "marks" not in result.features


# --- mapping -----------------------------------------------------------------


def _mapping_units() -> list[UnitSource]:
    return [
        UnitSource(
            syllabus_id="syl_1",
            unit=unit(
                "Unit 1",
                unit_id="unit_1",
                number=1,
                title="Algorithm Analysis",
                topics=["asymptotic notation", "recurrence relations", "worst case analysis"],
            ),
        ),
        UnitSource(
            syllabus_id="syl_1",
            unit=unit(
                "Unit 2",
                unit_id="unit_2",
                number=2,
                title="Linear Structures",
                topics=["arrays", "linked lists", "stacks", "queues"],
            ),
        ),
    ]


def test_explicit_unit_reference_is_authoritative_and_evidence_is_kept() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Explain the LIFO behaviour of a stack.",
        question_unit="Unit 2",
        sources=_mapping_units(),
    )
    assert result.status is MappingStatus.MAPPED
    assert result.top is not None
    assert result.top.unit_id == "unit_2"
    assert any("explicit unit reference" in item for item in result.top.evidence)


def test_composite_unit_metadata_resolves_to_its_number() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Explain the LIFO behaviour of a stack.",
        question_unit="Unit 2: Linear Structures",
        sources=_mapping_units(),
    )
    assert result.status is MappingStatus.MAPPED
    assert result.top is not None
    assert result.top.unit_id == "unit_2"


def test_measurement_wording_is_not_an_explicit_unit_reference() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="A unit 2 kg mass is attached to a spring.",
        question_unit=None,
        sources=_mapping_units(),
    )
    assert not any(
        "explicit unit reference" in evidence
        for candidate in result.candidates
        for evidence in candidate.evidence
    )


def test_multiple_text_unit_references_are_not_collapsed_to_the_first() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Compare the stacks in Unit 2 with the recurrence relations in Unit 1.",
        question_unit=None,
        sources=_mapping_units(),
    )
    assert result.status is MappingStatus.AMBIGUOUS
    assert {candidate.unit_id for candidate in result.candidates} == {"unit_1", "unit_2"}
    assert any(item.code == "ambiguous_mapping" for item in result.uncertainties)

    capped = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Compare the stacks in Unit 2 with the recurrence relations in Unit 1.",
        question_unit=None,
        sources=_mapping_units(),
        settings=MappingSettings(max_candidates=1),
    )
    assert capped.status is MappingStatus.AMBIGUOUS
    assert len(capped.candidates) == 1


def test_any_unresolved_reference_prevents_a_partial_mapping() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Compare Unit 2 with Unit 9.",
        question_unit=None,
        sources=_mapping_units(),
    )
    assert result.status is MappingStatus.UNMAPPED
    assert result.candidates == []
    assert any("Unit 9" in item.message for item in result.uncertainties)


def test_lowercase_unit_reference_with_context_still_resolves() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Answer any two questions from unit 2.",
        question_unit=None,
        sources=_mapping_units(),
    )
    assert result.status is MappingStatus.MAPPED
    assert result.top is not None
    assert result.top.unit_id == "unit_2"
    assert any("explicit unit reference" in item for item in result.top.evidence)


def test_shared_terms_exclude_topic_words_not_in_the_question() -> None:
    sources = [
        UnitSource(
            syllabus_id="syl_1",
            unit=unit(
                "Unit 2",
                unit_id="unit_2",
                number=2,
                title="Linear Structures",
                topics=["linked lists and stacks"],
            ),
        )
    ]
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Explain stacks.",
        question_unit=None,
        sources=sources,
        settings=MappingSettings(threshold=0.05),
    )
    assert result.top is not None
    assert "stack" in result.top.matched_terms
    assert "link" not in result.top.matched_terms
    assert "list" not in result.top.matched_terms


def test_unresolved_explicit_unit_stays_unmapped_without_cross_course_fallback() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Explain the material in Unit 9.",
        question_unit="Unit 9",
        sources=_mapping_units(),
    )
    assert result.status is MappingStatus.UNMAPPED
    assert result.candidates == []
    assert any(item.code == "unmapped_question" for item in result.uncertainties)
    assert "no other course" in result.uncertainties[0].message


def test_lexical_mapping_is_course_local_and_can_remain_unmapped() -> None:
    mapped = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Compare arrays and linked lists for insertion.",
        question_unit=None,
        sources=_mapping_units(),
    )
    assert mapped.status is MappingStatus.MAPPED
    assert mapped.top is not None
    assert mapped.top.unit_id == "unit_2"

    unmapped = map_question(
        course_id=COURSE,
        question_id="q_2",
        text="Who wrote the novel Pride and Prejudice?",
        question_unit=None,
        sources=_mapping_units(),
    )
    assert unmapped.status is MappingStatus.UNMAPPED
    assert unmapped.candidates == []


def test_ambiguous_mapping_is_reported_not_forced() -> None:
    sources = [
        UnitSource(
            syllabus_id="syl_1",
            unit=unit(
                "Unit 1",
                unit_id="unit_a",
                number=1,
                title="Trees",
                topics=["binary trees", "tree traversal"],
            ),
        ),
        UnitSource(
            syllabus_id="syl_1",
            unit=unit(
                "Unit 1",
                unit_id="unit_b",
                number=1,
                title="Trees",
                topics=["binary trees", "tree traversal"],
            ),
        ),
    ]
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Explain binary tree traversal.",
        question_unit=None,
        sources=sources,
        settings=MappingSettings(threshold=0.05, margin=0.04),
    )
    assert result.status is MappingStatus.AMBIGUOUS
    assert len(result.candidates) == 2
    assert any(item.code == "ambiguous_mapping" for item in result.uncertainties)

    capped = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Explain binary tree traversal.",
        question_unit=None,
        sources=sources,
        settings=MappingSettings(threshold=0.05, margin=0.04, max_candidates=1),
    )
    assert capped.status is MappingStatus.AMBIGUOUS
    assert len(capped.candidates) == 1


def test_no_syllabus_is_unmapped_with_a_named_reason() -> None:
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Define a stack.",
        question_unit=None,
        sources=[],
    )
    assert result.status is MappingStatus.UNMAPPED
    assert any(item.code == "no_syllabus" for item in result.uncertainties)


def test_semantic_signal_is_used_only_when_same_model_has_both_vectors() -> None:
    sources = _mapping_units()
    semantic = SemanticIndex(
        model_id="test-model",
        question_vectors={"q_1": [1.0, 0.0]},
        unit_vectors={"unit_1": [1.0, 0.0], "unit_2": [0.0, 1.0]},
    )
    result = map_question(
        course_id=COURSE,
        question_id="q_1",
        text="Who wrote Pride and Prejudice?",
        question_unit=None,
        sources=sources,
        semantic=semantic,
        settings=MappingSettings(threshold=0.1, semantic_weight=0.9),
    )
    assert result.status is MappingStatus.MAPPED
    assert result.top is not None
    assert result.top.unit_id == "unit_1"
    assert any("semantic cosine" in item for item in result.top.evidence)


# --- similarity --------------------------------------------------------------


def test_exact_duplicate_ignores_case_whitespace_and_leading_number() -> None:
    first = question("Define a stack and describe LIFO behaviour.")
    second = question("1.  Define a stack and describe LIFO behaviour.  ")
    assert normalise_question_text(first.text) == normalise_question_text(second.text)
    result = find_similar_questions(COURSE, [first, second])
    assert len(result.matches) == 1
    assert result.matches[0].kind is SimilarityKind.EXACT
    assert result.matches[0].score == 1.0


def test_exact_duplicates_are_not_hidden_by_top_k() -> None:
    questions = [
        question("Define a stack and describe its last-in-first-out behaviour.").model_copy(
            update={"question_id": f"q_dup_{index}"}
        )
        for index in range(8)
    ]
    result = find_similar_questions(COURSE, questions, settings=SimilaritySettings(top_k=1))
    assert len(result.matches) == 28
    assert all(match.kind is SimilarityKind.EXACT for match in result.matches)


def test_near_duplicate_is_not_called_exact() -> None:
    first = question("Define a stack and describe its last-in-first-out behaviour.")
    second = question("Define a stack and describe its last-in-first-out behavior.")
    result = find_similar_questions(
        COURSE, [first, second], settings=SimilaritySettings(near_threshold=0.75)
    )
    assert len(result.matches) == 1
    assert result.matches[0].kind is SimilarityKind.NEAR
    assert result.matches[0].lexical_score >= 0.75
    assert result.matches[0].lexical_score < 1.0


def test_semantic_match_and_different_reasoning_are_distinguished() -> None:
    first = question("Explain how a stack removes the most recent item.")
    second = question("Analyse why a stack removes the most recent item.")
    semantic = SemanticIndex(
        model_id="unit-test",
        question_vectors={
            first.question_id: [1.0, 0.0],
            second.question_id: [1.0, 0.0],
        },
    )
    settings = SimilaritySettings(
        near_threshold=1.0,
        semantic_threshold=0.8,
        concept_threshold=0.3,
        max_candidates=5,
    )
    same = find_similar_questions(
        COURSE,
        [first, second],
        semantic=semantic,
        profiles={
            first.question_id: ReasoningProfile("explanation", "understand"),
            second.question_id: ReasoningProfile("explanation", "understand"),
        },
        settings=settings,
    )
    assert len(same.matches) == 1
    assert same.matches[0].kind is SimilarityKind.SEMANTIC
    assert same.semantic_used is True

    different = find_similar_questions(
        COURSE,
        [first, second],
        semantic=semantic,
        profiles={
            first.question_id: ReasoningProfile("explanation", "understand"),
            second.question_id: ReasoningProfile("evaluation", "evaluate"),
        },
        settings=settings,
    )
    assert different.matches[0].kind is SimilarityKind.CONCEPT_RELATED
    assert any("reasoning demands differ" in item for item in different.matches[0].evidence)


def test_semantic_similarity_with_unknown_reasoning_is_concept_related() -> None:
    first = question("Explain how a stack removes the most recent item.")
    second = question("Analyse why a stack removes the most recent item.")
    semantic = SemanticIndex(
        model_id="unit-test",
        question_vectors={first.question_id: [1.0, 0.0], second.question_id: [1.0, 0.0]},
    )
    result = find_similar_questions(
        COURSE,
        [first, second],
        semantic=semantic,
        profiles={
            first.question_id: ReasoningProfile(),
            second.question_id: ReasoningProfile(),
        },
        settings=SimilaritySettings(
            near_threshold=1.0,
            semantic_threshold=0.8,
            concept_threshold=0.3,
        ),
    )
    assert len(result.matches) == 1
    assert result.matches[0].kind is SimilarityKind.CONCEPT_RELATED
    assert any("profile is incomplete" in item for item in result.matches[0].evidence)


def test_concept_related_below_semantic_threshold() -> None:
    first = question("Define a stack.")
    second = question("A stack supports push and pop.")
    semantic = SemanticIndex(
        model_id="unit-test",
        question_vectors={first.question_id: [1.0, 0.0], second.question_id: [0.5, 0.5]},
    )
    result = find_similar_questions(
        COURSE,
        [first, second],
        semantic=semantic,
        settings=SimilaritySettings(
            near_threshold=1.0,
            semantic_threshold=0.99,
            concept_threshold=0.2,
            max_candidates=5,
        ),
    )
    assert len(result.matches) == 1
    assert result.matches[0].kind is SimilarityKind.CONCEPT_RELATED


def test_similarity_reports_lexical_fallback_without_embeddings() -> None:
    first = question("Define a stack.")
    second = question("Define a queue.")
    result = find_similar_questions(COURSE, [first, second])
    assert result.semantic_used is False
    assert any(item.code == "similarity_fallback" for item in result.uncertainties)


def test_single_question_does_not_produce_pairs() -> None:
    result = find_similar_questions(COURSE, [question("Define a stack.")])
    assert result.matches == []
    assert any(item.code == "insufficient_data" for item in result.uncertainties)
