from __future__ import annotations

from types import SimpleNamespace

from exam_engine.models.recovery import Issue, SourceCandidate
from exam_engine.recovery.papers import (
    PaperPassage,
    PaperQuestion,
    extract_paper_questions,
    extract_passage_groups,
    match_paper_questions,
    match_passage_children,
)
from exam_engine.recovery.providers import DuckDuckGoSearch, decode_duckduckgo_url
from exam_engine.recovery.quality import release_assessment_for
from exam_engine.recovery.retrieval import (
    DatabaseRetrievalCache,
    FetchedDocument,
    SourceRetriever,
)
from exam_engine.recovery.solver import recompute_numeric, solve_question
from exam_engine.recovery.verification import answers_equivalent, verify_answer
from exam_engine.storage.db import memory_database
from exam_engine.storage.recovery_repository import RecoveryRepository


def _candidate(url: str = "https://example.edu/paper.html") -> SourceCandidate:
    return SourceCandidate(
        source_id="src-1",
        course_id="c-1",
        title="Question paper",
        url=url,
        document_type="question_paper",
        source_tier=1,
        reliability=0.9,
        provider="test",
    )


def _document(url: str, *, text: str = "paper", html: bool = False, links=None) -> FetchedDocument:
    return FetchedDocument(
        url=url,
        final_url=url,
        title="Source",
        content_type="text/html" if html else "application/pdf",
        text=text,
        sha256=("a" if "index" in url else "b") * 64,
        bytes_read=100,
        page_count=1,
        extraction_method="html_text" if html else "pymupdf_text",
        links=list(links or []),
    )


class _Fetcher:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    def fetch_document(self, url: str) -> FetchedDocument:
        self.calls.append(url)
        return self.documents[url]


def test_duckduckgo_parser_keeps_only_real_result_links() -> None:
    payload = """
    <div class="result">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgate.example.ac.in%2Fkey.pdf">
        Official answer key
      </a>
      <a class="result__snippet">Final key PDF</a>
    </div>
    """
    rows = DuckDuckGoSearch.parse_results(payload, limit=3)
    assert len(rows) == 1
    assert rows[0].url == "https://gate.example.ac.in/key.pdf"
    assert rows[0].provider == "duckduckgo"
    assert decode_duckduckgo_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.test%2Fa") == "https://x.test/a"


def test_answer_serialization_equivalence_is_bounded() -> None:
    assert answers_equivalent("A;B;C", "A,B,C")
    assert answers_equivalent("2 to 2", "2")
    assert answers_equivalent("2.75 to 2.75", "2.75")
    assert answers_equivalent("MTA", "MTA")
    assert not answers_equivalent("A;B", "A;C")
    assert not answers_equivalent("2 to 3", "2")


def test_retrieval_follows_only_explicit_document_links() -> None:
    index_url = "https://exam.example.edu/question-papers"
    pdf_url = "https://exam.example.edu/qp-2024.pdf"
    pdf_document = _document(
        pdf_url, text="1. What is the value of 2 + 2?\n2. Choose the correct option."
    )
    pdf_document.sha256 = "c" * 64
    fetcher = _Fetcher(
        {
            index_url: _document(index_url, html=True, links=[(pdf_url, "Question paper 2024 PDF")]),
            pdf_url: pdf_document,
        }
    )
    candidate = _candidate(index_url)
    rows = SourceRetriever(fetcher).retrieve([candidate], max_documents=2, follow_links=1)
    assert [item.fetched.url for item in rows] == [index_url, pdf_url]
    assert candidate.evidence["snippets_are_not_evidence"] is True
    assert fetcher.calls == [index_url, pdf_url]


def test_retrieval_cache_roundtrip_uses_persistent_repository() -> None:
    database = memory_database()
    try:
        cache = DatabaseRetrievalCache(database)
        payload = _document("https://exam.example.edu/key.pdf").cache_payload()
        payload.update({"status": "ok", "error": ""})
        cache.put(payload)
        loaded = cache.get("https://exam.example.edu/key.pdf")
        assert loaded is not None
        assert loaded["sha256"] == "b" * 64
        assert loaded["status"] == "ok"
        with database.session() as session:
            assert RecoveryRepository(session).cache_stats()["ok"] == 1
    finally:
        database.close()


def test_paper_question_and_passage_matching_is_evidence_bounded() -> None:
    text = (
        "Read the following passage\n"
        "A cell is the basic structural and functional unit of life. It contains many "
        "specialized organelles, a nucleus with genetic material, and a membrane that "
        "regulates the movement of substances into and out of the cell.\n"
        "1. Which organelle produces ATP in a eukaryotic cell?\n"
        "2. What carries genetic information in a cell?\n"
    )
    questions = extract_paper_questions(text)
    passages = extract_passage_groups(text)
    assert len(questions) == 2
    assert len(passages) == 1
    assert len(passages[0].children) == 2

    records = [
        SimpleNamespace(
            record_id="r1",
            repaired={"question_text": "Which organelle produces ATP in a eukaryotic cell?"},
        ),
        SimpleNamespace(
            record_id="r2",
            repaired={"question_text": "What carries genetic information in a cell?"},
        ),
        SimpleNamespace(record_id="r3", repaired={"question_text": "Unrelated question about geology."}),
    ]
    matches = match_paper_questions(records, questions, threshold=0.9)
    assert set(matches) == {"r1", "r2"}
    passage_matches = match_passage_children(passages, records, threshold=0.9)
    assert len(passage_matches) == 1
    assert passage_matches[0]["record_ids"] == ["r1", "r2"]


def test_paper_extraction_rejects_unheaded_question_like_text() -> None:
    assert extract_paper_questions("Some paragraph\nwithout numbered questions.") == []
    assert extract_passage_groups("A passage without a heading. Just prose.") == []
    assert PaperPassage(passage_text="short", children=[]).confidence == 0.0
    assert PaperQuestion(number="1", text="text").confidence > 0


def test_deterministic_solver_and_independent_recomputation() -> None:
    solved = solve_question("Calculate 12 * (3 + 2)")
    assert solved is not None
    assert solved.answer == "60"
    agrees, evidence = recompute_numeric("Calculate 12 * (3 + 2)", "60")
    assert agrees is True
    assert evidence["recomputed"] == 60
    outcome = verify_answer(
        question_text="Calculate 12 * (3 + 2)",
        options=[],
        answer_text="60",
        answer_type="numeric",
        candidate=SimpleNamespace(
            source_type="deterministic_solver",
            source_url=None,
            source_reference="row=2",                verification_status="review_required",
                evidence={},
                official=False,
            ),
        conflicting_evidence=False,
    )
    assert outcome.result.status == "independently_solved_verified"
    assert outcome.result.independent is True


def test_conflicting_and_blocking_evidence_cannot_be_released() -> None:
    released = release_assessment_for("official_verified", [])
    assert released["status"] == "released"
    blocked = release_assessment_for(
        "official_verified",
        [
            Issue(
                code="answer.conflict",
                category="answer_integrity",
                severity="error",
                message="conflict",
                blocking=True,
            )
        ],
    )
    assert blocked["status"] == "quarantined"
    assert "answer.conflict" in blocked["reasons"]
    unverified = release_assessment_for("source_stored_unverified", [])
    assert unverified["status"] == "review_required"
