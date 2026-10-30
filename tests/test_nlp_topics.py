from __future__ import annotations

from exam_engine.models.enums import NlpStatus, TextOwnerType
from exam_engine.nlp.topics import LsaKMeansTopicModeler, TopicDocument, get_topic_modeler

COURSE = "test-cs101-2024-v1"


def doc(owner_id: str, text: str) -> TopicDocument:
    return TopicDocument(
        owner_type=TextOwnerType.DOCUMENT,
        owner_id=owner_id,
        document_id=owner_id,
        text=text,
    )


TREES = [
    "Binary search trees keep keys ordered for logarithmic lookup.",
    "Tree traversal visits nodes in preorder, inorder and postorder.",
    "Balanced trees such as AVL trees rotate nodes to stay balanced.",
]
SORTING = [
    "Quicksort partitions arrays around a pivot element.",
    "Merge sort splits arrays and merges sorted halves.",
    "Heapsort builds a heap and repeatedly extracts the maximum.",
]
GRAPHS = [
    "Graph traversal explores vertices and edges with breadth first search.",
    "Dijkstra finds shortest paths in weighted graphs using a priority queue.",
    "Topological sorting orders directed acyclic graphs by dependencies.",
]


def test_small_corpus_is_reported_as_insufficient_not_invented() -> None:
    result = LsaKMeansTopicModeler().fit(
        [doc("d1", TREES[0]), doc("d2", TREES[1])],
        course_id=COURSE,
        scope=TextOwnerType.DOCUMENT,
        k=2,
    )
    assert result.topics == []
    assert result.status is NlpStatus.INSUFFICIENT_DATA
    assert [warning.code for warning in result.warnings] == ["insufficient_data"]
    assert result.params["min_documents"] == 3


def test_three_documents_reduce_k_and_warn() -> None:
    result = LsaKMeansTopicModeler().fit(
        [doc(f"d{i}", TREES[i]) for i in range(3)],
        course_id=COURSE,
        scope=TextOwnerType.DOCUMENT,
        k=5,
    )
    assert result.topics
    assert result.params["k_used"] < result.params["k_requested"]
    assert any(
        warning.code == "insufficient_data" and "requested 5" in warning.message
        for warning in result.warnings
    )


def test_three_themes_produce_interpretable_topics() -> None:
    documents = [doc(f"t{i}", text) for i, text in enumerate(TREES)]
    documents += [doc(f"s{i}", text) for i, text in enumerate(SORTING)]
    documents += [doc(f"g{i}", text) for i, text in enumerate(GRAPHS)]
    result = LsaKMeansTopicModeler().fit(
        documents, course_id=COURSE, scope=TextOwnerType.DOCUMENT, k=3
    )
    assert len(result.topics) == 3
    assert all(topic.terms for topic in result.topics)
    assert all(topic.members for topic in result.topics)
    terms = {term.term.casefold() for topic in result.topics for term in topic.terms}
    assert any("tree" in term or "search" in term for term in terms)
    assert any("sort" in term or "partition" in term for term in terms)
    assert any("graph" in term or "vertex" in term for term in terms)


def test_topic_terms_are_ranked_and_pointers_are_weights() -> None:
    documents = [doc(f"t{i}", text) for i, text in enumerate(TREES)]
    documents += [doc(f"s{i}", text) for i, text in enumerate(SORTING)]
    documents += [doc(f"g{i}", text) for i, text in enumerate(GRAPHS)]
    topics = (
        LsaKMeansTopicModeler()
        .fit(documents, course_id=COURSE, scope=TextOwnerType.DOCUMENT, k=3)
        .topics
    )
    for topic in topics:
        assert [term.rank for term in topic.terms] == list(range(1, len(topic.terms) + 1))
        assert all(0.0 <= member.weight <= 1.0 + 1e-6 for member in topic.members)
        assert sum(1 for member in topic.members) == topic.size


def test_topic_discovery_is_deterministic() -> None:
    documents = [doc(f"t{i}", text) for i, text in enumerate(TREES)]
    documents += [doc(f"s{i}", text) for i, text in enumerate(SORTING)]
    documents += [doc(f"g{i}", text) for i, text in enumerate(GRAPHS)]
    first = LsaKMeansTopicModeler().fit(
        documents, course_id=COURSE, scope=TextOwnerType.DOCUMENT, k=3
    )
    second = LsaKMeansTopicModeler().fit(
        documents, course_id=COURSE, scope=TextOwnerType.DOCUMENT, k=3
    )
    assert [topic.label for topic in first.topics] == [topic.label for topic in second.topics]
    assert [topic.topic_id for topic in first.topics] == [topic.topic_id for topic in second.topics]


def test_uniform_corpus_reports_no_structure() -> None:
    same = "Sorting algorithms order data. Sorting algorithms compare keys."
    documents = [doc(f"d{i}", same) for i in range(6)]
    result = LsaKMeansTopicModeler().fit(
        documents, course_id=COURSE, scope=TextOwnerType.DOCUMENT, k=3
    )
    assert result.topics == []
    assert result.status is NlpStatus.INSUFFICIENT_DATA
    assert any("identical text" in warning.message for warning in result.warnings)


def test_repeated_documents_do_not_ask_for_empty_clusters() -> None:
    documents = [doc(f"d{i}", TREES[i % 2]) for i in range(6)]
    result = LsaKMeansTopicModeler().fit(
        documents, course_id=COURSE, scope=TextOwnerType.DOCUMENT, k=3
    )
    assert result.params["k_used"] <= result.params["unique_documents"]


def test_empty_documents_are_ignored() -> None:
    result = LsaKMeansTopicModeler().fit(
        [doc("d1", ""), doc("d2", "  ")],
        course_id=COURSE,
        scope=TextOwnerType.DOCUMENT,
        k=2,
    )
    assert result.status is NlpStatus.INSUFFICIENT_DATA
    assert result.params["n_documents"] == 0


def test_modeler_registry_rejects_unknown_names() -> None:
    assert get_topic_modeler("kmeans").name.value == "kmeans"
    try:
        get_topic_modeler("lda")
    except KeyError as exc:
        assert "lda" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown modeler must raise")
