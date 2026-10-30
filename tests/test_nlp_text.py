from __future__ import annotations

from exam_engine.models.enums import TextOwnerType
from exam_engine.models.nlp import PreprocessConfig
from exam_engine.nlp.preprocess import Preprocessor, split_section_chunks, split_sections
from exam_engine.nlp.text import (
    detect_language,
    is_stopword,
    lemmatize,
    normalize_for_analysis,
    segment_sentences,
    tokenize,
)


def test_normalisation_preserves_notation_and_symbols() -> None:
    raw = "Find   T(n) = 2T(n/2) + O(n)  for  n ≤ 100  [5 marks]"
    normalized = normalize_for_analysis(raw)
    assert "T(n) = 2T(n/2) + O(n)" in normalized
    assert "≤" in normalized
    assert "[5 marks]" in normalized
    # Intra-line alignment gaps collapse, wording and notation survive.
    assert "  " not in normalized


def test_unicode_compatibility_is_normalised_but_meaning_kept() -> None:
    assert normalize_for_analysis("e\ufb03cient algorithm") == "efficient algorithm"
    assert normalize_for_analysis("ﬁle x_i + y^2") == "file x_i + y^2"


def test_paragraph_breaks_survive_whitespace_cleanup() -> None:
    normalized = normalize_for_analysis("Unit 1\n\n\n\nTopic A\nTopic B")
    assert normalized == "Unit 1\n\nTopic A\nTopic B"


def test_sentence_segmentation_handles_abbreviations_and_decimals() -> None:
    text = (
        "Use e.g. quicksort for the average case. "
        "The value is 3.14 for pi. "
        "Dr. Rao proved it. "
        "What is the complexity?"
    )
    sentences = [s.text for s in segment_sentences(text)]
    assert len(sentences) == 4
    assert sentences[0].startswith("Use e.g. quicksort")
    assert "3.14" in sentences[1]
    assert sentences[2] == "Dr. Rao proved it."
    assert sentences[3] == "What is the complexity?"


def test_sentence_segmentation_keeps_math_calls_intact() -> None:
    sentences = segment_sentences("Show that O(n log n) sorting is not linear. Then explain why.")
    assert len(sentences) == 2
    assert "O(n log n)" in sentences[0].text


def test_numbered_lists_become_separate_sentences() -> None:
    sentences = segment_sentences("1. Define a stack. 2. Define a queue.")
    assert [s.text for s in sentences] == ["1. Define a stack.", "2. Define a queue."]


def test_sentence_spans_point_back_into_the_normalised_text() -> None:
    text = "First sentence here. Second sentence here."
    for sentence in segment_sentences(text):
        assert text[sentence.start : sentence.end] == sentence.text


def test_empty_text_yields_no_sentences() -> None:
    assert segment_sentences("   \n\n ") == []


def test_tokenizer_keeps_technical_tokens() -> None:
    text = "Use quick_sort on C++ data; O(n log n) and 3.14 kg."
    values = [token.text for token in tokenize(text)]
    assert "quick_sort" in values
    assert "C++" in values
    assert "O(n log n)" in values
    assert "3.14" in values


def test_token_offsets_are_exact() -> None:
    text = "Define x_i and y^2 clearly."
    for token in tokenize(text):
        assert text[token.start : token.end] == token.text


def test_stopwords_keep_technical_modifiers() -> None:
    assert is_stopword("the")
    assert is_stopword("analysis") is False
    assert is_stopword("all") is False
    assert is_stopword("no") is False


def test_lemmatiser_reduces_regular_forms_only() -> None:
    assert lemmatize("algorithms") == "algorithm"
    assert lemmatize("studies") == "study"
    assert lemmatize("running") == "run"
    assert lemmatize("indices") == "index"
    assert lemmatize("TCP") == "tcp"
    assert lemmatize("x_i") == "x_i"


def test_section_chunks_carry_exact_spans() -> None:
    text = "Heading\nA B C about trees.\n\nSecond paragraph here about sorting."
    chunks = split_section_chunks(text, normalize=False)
    assert chunks
    for chunk in chunks:
        assert 0 <= chunk.start <= chunk.end <= len(text)
        assert text[chunk.start : chunk.end] == chunk.text
    assert split_sections(text) == [chunk.text for chunk in chunks]


def test_unsupported_expected_language_is_flagged() -> None:
    record = Preprocessor(PreprocessConfig(language="fr")).preprocess(
        "The algorithm sorts the array.",
        course_id="c",
        owner_type=TextOwnerType.DOCUMENT,
        owner_id="d",
    )
    assert any(uncertainty.code == "language_unsupported" for uncertainty in record.uncertainties)


def test_language_detection_is_explicit() -> None:
    assert detect_language("The algorithm sorts the array in linear time.") == "en"
    assert detect_language("算法对数组进行排序") == "zh"
    assert detect_language("?") is None
