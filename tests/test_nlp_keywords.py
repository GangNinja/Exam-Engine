from __future__ import annotations

from exam_engine.models.enums import NlpMethod, TextOwnerType
from exam_engine.nlp.keywords import (
    HybridKeywordExtractor,
    KeywordDocument,
    RakeKeywordExtractor,
    TfidfKeywordExtractor,
    analysis_tokens,
    normalize_term,
)

COURSE_ID = "test-cs101-2024-v1"


def document(owner_id: str, text: str) -> KeywordDocument:
    return KeywordDocument(
        owner_type=TextOwnerType.DOCUMENT,
        owner_id=owner_id,
        text=text,
        course_id=COURSE_ID,
        document_id=owner_id,
    )


DOCUMENTS = [
    document(
        "doc_a",
        "Binary search tree operations include insertion and deletion. "
        "The binary search tree keeps keys ordered so that search runs in O(log n). "
        "Answer all questions. [5 marks]",
    ),
    document(
        "doc_b",
        "Graph traversal algorithms visit every vertex once. "
        "Breadth first search and depth first search differ in the order they explore. "
        "The binary search tree is a special graph.",
    ),
    document(
        "doc_c",
        "Sorting algorithms such as quicksort and merge sort arrange data. "
        "Merge sort guarantees O(n log n) comparisons; quicksort is faster on average.",
    ),
]


def test_tfidf_is_deterministic() -> None:
    extractor = TfidfKeywordExtractor()
    first = extractor.extract(DOCUMENTS, top_k=10)
    second = extractor.extract(DOCUMENTS, top_k=10)
    assert [(r.term, r.score, r.owner_id) for r in first] == [
        (r.term, r.score, r.owner_id) for r in second
    ]


def test_tfidf_finds_multiword_technical_terms() -> None:
    records = TfidfKeywordExtractor(max_ngram=3).extract(DOCUMENTS, top_k=20)
    terms = {record.term.casefold() for record in records}
    assert any("binary search" in term for term in terms)
    assert all(record.n_gram >= 1 for record in records)


def test_tfidf_filters_paper_furniture_but_keeps_content_phrases() -> None:
    furniture_doc = [
        document(
            "doc_x",
            "Answer all questions. [5 marks] Time allowed: 3 hours. "
            "Unit testing verifies behaviour of small functions.",
        ),
        document("doc_y", "Unit testing catches regressions early in development."),
    ]
    terms = {
        record.term.casefold()
        for record in TfidfKeywordExtractor(max_ngram=3).extract(furniture_doc, top_k=20)
    }
    assert "marks" not in terms
    assert "hours" not in terms
    assert any("unit" in term and "test" in term for term in terms)


def test_tfidf_scores_are_normalised_and_ranked() -> None:
    records = TfidfKeywordExtractor().extract(DOCUMENTS, top_k=8)
    assert records
    # Scores are length-weighted, so they are comparable within a run rather than
    # bounded to [0, 1].
    assert all(record.score > 0 for record in records)
    assert [record.rank for record in records] == list(range(1, len(records) + 1))
    scores = [record.score for record in records]
    assert scores == sorted(scores, reverse=True)


def test_tfidf_attributes_each_term_to_a_real_document() -> None:
    records = TfidfKeywordExtractor().extract(DOCUMENTS, top_k=10)
    owner_ids = {record.owner_id for record in records}
    assert owner_ids <= {doc.owner_id for doc in DOCUMENTS}
    for record in records:
        assert record.method is NlpMethod.TFIDF
        assert record.course_id == COURSE_ID


def test_rake_works_on_a_single_document() -> None:
    records = RakeKeywordExtractor().extract([DOCUMENTS[0]], top_k=10)
    assert records
    assert all(record.score > 0 for record in records)
    assert all(record.method is NlpMethod.RAKE for record in records)


def test_rake_prefers_repeated_phrases() -> None:
    docs = [
        document(
            "doc_r",
            "Binary search is fast. Binary search is exact. Hashing is fast. Sorting is slow.",
        )
    ]
    records = RakeKeywordExtractor().extract(docs, top_k=10)
    assert any(record.term.casefold() == "binary search" for record in records)
    binary = next(r for r in records if r.term.casefold() == "binary search")
    assert binary.n_gram == 2
    assert binary.score > 0


def test_hybrid_combines_both_signals() -> None:
    records = HybridKeywordExtractor().extract(DOCUMENTS, top_k=6)
    assert records
    assert all(record.method is NlpMethod.HYBRID for record in records)
    assert all("component_scores" in record.metadata for record in records)
    component = records[0].metadata["component_scores"]
    assert set(component) == {"tfidf", "rake"}


def test_empty_input_returns_nothing() -> None:
    assert TfidfKeywordExtractor().extract([], top_k=5) == []
    assert RakeKeywordExtractor().extract([], top_k=5) == []
    assert TfidfKeywordExtractor().extract([document("d", "   \n")], top_k=5) == []


def test_top_k_is_respected_and_normalised_terms_are_unique() -> None:
    records = TfidfKeywordExtractor().extract(DOCUMENTS, top_k=4)
    assert len(records) <= 4
    normalized = [record.normalized for record in records]
    assert len(normalized) == len(set(normalized))


def test_stopword_switch_is_honoured() -> None:
    docs = [document("doc_s", "The algorithm and the graph and the tree repeat.")]
    default = {record.normalized for record in TfidfKeywordExtractor().extract(docs, top_k=30)}
    kept = {
        record.normalized
        for record in TfidfKeywordExtractor(drop_stopwords=False).extract(docs, top_k=30)
    }
    assert not any("the" in term.split() for term in default)
    assert any("the" in term.split() for term in kept)


def test_lemmatize_switch_is_honoured() -> None:
    docs = [document("doc_l", "Algorithms sort arrays. The algorithm repeats algorithms.")]
    default = {record.normalized for record in TfidfKeywordExtractor().extract(docs, top_k=30)}
    reduced = {
        record.normalized
        for record in TfidfKeywordExtractor(lemmatize_terms=True).extract(docs, top_k=30)
    }
    assert "algorithms" in default
    assert "algorithms" not in reduced
    assert "algorithm" in reduced


def test_analysis_tokens_keep_technical_notation() -> None:
    tokens = analysis_tokens("Compare O(n log n) with O(n^2) and quick_sort for C++ code.")
    assert "O(n log n)" in tokens
    assert "quick_sort" in tokens
    assert "C++" in tokens
    assert "the" not in tokens


def test_normalize_term_collapses_punctuation() -> None:
    assert normalize_term("  Binary   Search-Tree ") == "binary search tree"
