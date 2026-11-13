"""Deterministic Phase 14.2 Crawl4AI, table-key and archive tests.

These tests never contact the network.  Live public-source checks are recorded
separately in the Phase 14.2 report.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from exam_engine.models.recovery import SourceCandidate
from exam_engine.recovery.archives import OfficialArchiveDiscovery, load_archive_specs
from exam_engine.recovery.crawl4ai_adapter import result_to_page
from exam_engine.recovery.providers import DuckDuckGoSearch
from exam_engine.recovery.papers import (
    extract_paper_questions,
    match_question_text,
    match_paper_questions,
)
from exam_engine.recovery.retrieval import (
    DocumentFetcher,
    FetchedDocument,
    SourceRetriever,
    _safe_url,
)
from exam_engine.recovery.sources import extract_answer_key_entries


GATE_KEY = """Q. No.
Session
Question 
Type
Section
Key/Range 
Mark 
1
1
MCQ
GA
D
1
2
1
MCQ
GA
A
1
11
1
MCQ
CY
MTA
1
30
1
NAT
CY
25920 to 25920
1
31
1
MSQ
CY
A;C
1
Page 2 of 2
"""

GATE_PAPER = """Q.1
If the meaning of [sick -> infirm] is analogous to [silly -> daft], choose the option.
(A) frown (B) fawn (C) vein (D) vain
Chemistry (CY)
Page 7 of 42
Q.2
The number of metal-metal bonds in the complex is x. The value of x is _____.
(A) 1 (B) 2 (C) 3 (D) 4
Page 8 of 42
Q.3
The correct statement(s) is (are):
(A) one (B) two (C) three (D) four
"""


def test_tabular_key_preserves_mta_and_ranges() -> None:
    entries = extract_answer_key_entries(GATE_KEY)
    assert len(entries) == 5
    by_number = {entry.question_number: entry for entry in entries}
    assert by_number["1"].answer == "D"
    assert by_number["11"].answer == "MTA"
    assert by_number["11"].metadata["marks_to_all"] is True
    assert by_number["30"].metadata["question_type"] == "NAT"
    assert by_number["30"].answer == "25920 to 25920"
    assert by_number["31"].metadata["question_type"] == "MSQ"


def test_q_marker_paper_extraction_and_stem_boundary() -> None:
    questions = extract_paper_questions(GATE_PAPER)
    numbers = [question.number for question in questions]
    assert numbers == ["1", "2", "3"]
    assert questions[0].text.startswith("If the meaning")
    assert "(A) frown" not in questions[0].text
    assert "sick -> infirm" in questions[0].text


def test_token_containment_matches_clean_local_stem() -> None:
    source = "A question stem with several distinctive words and a numerical value 42. (A) one (B) two"
    record = "A question stem with several distinctive words and a numerical value 42."
    assert match_question_text(source, record, threshold=0.94) == 1.0


def test_question_number_alone_does_not_match() -> None:
    class Record:
        record_id = "r1"
        original = {"original_question_number": "1"}
        repaired = {"question_text": "A completely different question about organic synthesis."}

    class Question:
        number = "1"
        text = "A question about quantum mechanics and spectroscopy."
        passage = None
        confidence = 0.8
        evidence = {}

    assert match_paper_questions([Record()], [Question()], threshold=0.94) == {}


def test_archive_discovery_uses_configured_official_media_api() -> None:
    payload = [
        {
            "id": 1,
            "title": {"rendered": "CYFinalAnswerKey"},
            "source_url": "https://official.example/2024/CYFinalAnswerKey.pdf",
            "mime_type": "application/pdf",
        },
        {
            "id": 2,
            "title": {"rendered": "CY24S1"},
            "source_url": "https://official.example/2024/CY24S1.pdf",
            "mime_type": "application/pdf",
        },
        {
            "id": 3,
            "title": {"rendered": "CYkey"},
            "source_url": "https://official.example/2024/CYkey.pdf",
            "mime_type": "application/pdf",
        },
    ]
    specs = load_archive_specs()
    assert specs
    discovery = OfficialArchiveDiscovery(
        specs=specs,
        media_fetch=lambda _url: payload,
        browser=None,
    )
    found = discovery.discover(
        course_id="gate-chemistry",
        exam_name="GATE",
        subject_name="Chemistry",
        years=[2024],
    )
    assert [item.document_type for item in found] == [
        "answer_key",
        "question_paper",
        "answer_key",
    ]
    assert found[0].evidence["document_variant"] == "final"
    assert found[0].provider == "official_archive"
    assert found[0].source_tier == 1


def test_crawl4ai_result_conversion_keeps_markdown_links_tables() -> None:
    result = SimpleNamespace(
        success=True,
        status_code=200,
        url="https://example.com/page",
        redirected_url="https://example.com/page",
        error_message="",
        metadata={"title": "Example", "canonical": "https://example.com/canonical"},
        markdown="# Title\n\nBody",
        html="<h1>Title</h1><p>Body</p>",
        links={
            "internal": [{"href": "https://example.com/a", "text": "A"}],
            "external": [{"href": "https://official.example/paper.pdf", "text": "Paper"}],
        },
        tables=[{"headers": ["Q", "A"], "rows": [["1", "D"]]}],
    )
    page = result_to_page(result, url="https://example.com/page", elapsed_ms=12)
    assert page.success is True
    assert page.title == "Example"
    assert page.canonical_url == "https://example.com/canonical"
    assert page.markdown.startswith("# Title")
    assert len(page.links) == 2
    assert page.tables[0]["rows"] == [["1", "D"]]


def test_fetched_document_payload_includes_crawl_fields() -> None:
    document = FetchedDocument(
        url="https://example.com",
        final_url="https://example.com",
        title="T",
        content_type="text/html",
        text="Body",
        sha256="abc",
        bytes_read=4,
        markdown="# Body",
        links=[("https://example.com/a", "A")],
        tables=[{"headers": ["x"]}],
        metadata={"canonical": "https://example.com"},
        canonical_url="https://example.com",
        crawler="crawl4ai",
        browser_ms=25,
    )
    payload = document.cache_payload()
    assert payload["markdown"] == "# Body"
    assert payload["links"][0]["url"] == "https://example.com/a"
    assert payload["crawler"] == "crawl4ai"
    assert payload["browser_ms"] == 25


def test_retriever_prefers_browser_for_html_and_http_for_pdf() -> None:
    class FakeBrowser:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def fetch_document(self, url: str) -> FetchedDocument:
            self.urls.append(url)
            return FetchedDocument(
                url=url,
                final_url=url,
                title="Rendered",
                content_type="text/html",
                text="rendered text",
                sha256="html",
                bytes_read=8,
                crawler="crawl4ai",
            )

    class FakeHttp:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def fetch_document(self, url: str) -> FetchedDocument:
            self.urls.append(url)
            return FetchedDocument(
                url=url,
                final_url=url,
                title="PDF",
                content_type="application/pdf",
                text="pdf text",
                sha256="pdf",
                bytes_read=7,
                page_count=1,
            )

    browser = FakeBrowser()
    http = FakeHttp()
    retriever = SourceRetriever(http, browser_fetcher=browser)  # type: ignore[arg-type]
    html_candidate = SourceCandidate(
        source_id="s1",
        course_id="c",
        title="Paper page",
        url="https://example.com/question-paper",
        document_type="question_paper",
        source_tier=1,
        reliability=0.9,
        provider="test",
    )
    pdf_candidate = SourceCandidate(
        source_id="s2",
        course_id="c",
        title="Paper PDF",
        url="https://example.com/paper.pdf",
        document_type="question_paper",
        source_tier=1,
        reliability=0.8,
        provider="test",
    )
    results = retriever.retrieve([html_candidate, pdf_candidate], max_documents=2, follow_links=0)
    assert len(results) == 2
    assert browser.urls == ["https://example.com/question-paper"]
    assert http.urls == ["https://example.com/paper.pdf"]


def test_duckduckgo_202_is_an_explicit_provider_failure(monkeypatch) -> None:
    class Response:
        status = 202

        def read(self, _limit: int) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    provider = DuckDuckGoSearch(delay_seconds=0.0, retries=0)
    assert provider.search("GATE Chemistry 2024 answer key", limit=3) == []
    assert "202" in (provider.last_error or "")
    assert provider.last_status == 202


def test_ssrf_private_and_loopback_are_rejected() -> None:
    with pytest.raises(ValueError):
        _safe_url("http://127.0.0.1/paper.pdf")
    with pytest.raises(ValueError):
        _safe_url("http://10.0.0.1/paper.pdf")
    with pytest.raises(ValueError):
        _safe_url("file:///etc/passwd")
    assert DocumentFetcher is not None
