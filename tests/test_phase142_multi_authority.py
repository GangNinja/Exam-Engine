"""Deterministic multi-authority Phase 14.2 archive and session tests.

These tests never contact the network.  Live archive checks are recorded in the
Phase 14.2 report.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from exam_engine.models.recovery import AnswerCandidate, RecordResult, SourceCandidate
from exam_engine.recovery.archives import ArchiveSpec, OfficialArchiveDiscovery, load_archive_specs
from exam_engine.recovery import retrieval
from exam_engine.recovery.retrieval import ZipBundleFetcher
from exam_engine.recovery.service import DatasetRecoveryService
from exam_engine.recovery.sources import (
    AnswerKeyEntry,
    AnswerKeyIndex,
    extract_cbse_marking_scheme_entries,
)


class FakePage:
    def __init__(self, links: list[tuple[str, str]]) -> None:
        self.title = "Official archive"
        self.final_url = "https://official.example/archive"
        self.crawler = "test"
        self.links = links


def _cbse_spec() -> ArchiveSpec:
    return ArchiveSpec.from_mapping(
        {
            "id": "cbse-test",
            "provider": "html_archive",
            "exam_terms": ["cbse class 10"],
            "years": [2024],
            "page_urls": ["https://official.example/question-paper"],
            "class_code": "X",
            "set_aware": True,
            "subjects": {"science": ["SCIENCE", "Science"]},
        }
    )


def test_cbse_archive_discovers_set_aware_paper_and_marking_scheme() -> None:
    links = [
        ("https://official.example/question-paper/2024/X/SCIENCE.zip", "Download"),
        ("https://official.example/Marking-Scheme/2024/X/Science.zip", "Download"),
        ("https://official.example/question-paper/2023/X/SCIENCE.zip", "Download"),
        ("https://official.example/question-paper/2024/X/SCIENCE_Hindi.zip", "Download"),
    ]
    discovery = OfficialArchiveDiscovery(
        specs=[_cbse_spec()],
        page_fetch=lambda _url: FakePage(links),
    )
    found = discovery.discover(
        course_id="cbse-class-10-science",
        exam_name="CBSE Class 10",
        subject_name="Science",
        years=[2024],
        sets=["2"],
    )
    assert len(found) == 2
    assert {item.document_type for item in found} == {"question_paper", "marking_scheme"}
    assert all(item.url.endswith("#set=2") for item in found)
    assert all(item.evidence["identity"]["set"] == "2" for item in found)
    assert all(item.source_tier == 1 for item in found)


def test_jee_advanced_archive_discovers_both_english_papers() -> None:
    spec = ArchiveSpec.from_mapping(
        {
            "id": "jee-test",
            "provider": "html_archive",
            "exam_terms": ["jee advanced"],
            "years": [2023],
            "page_url": "https://official.example/archive",
            "subject_mode": "all_papers",
            "language": "English",
        }
    )
    links = [
        ("https://official.example/past_qps/2023_1_English.pdf", "Download"),
        ("https://official.example/past_qps/2023_2_English.pdf", "Download"),
        ("https://official.example/past_qps/2023_1_Hindi.pdf", "Download"),
        ("https://official.example/past_qps/2022_1_English.pdf", "Download"),
    ]
    discovery = OfficialArchiveDiscovery(
        specs=[spec],
        page_fetch=lambda _url: FakePage(links),
    )
    found = discovery.discover(
        course_id="jee-advanced-physics",
        exam_name="JEE Advanced",
        subject_name="Physics",
        years=[2023],
    )
    assert len(found) == 2
    assert {item.evidence["identity"]["paper"] for item in found} == {"1", "2"}
    assert all(item.document_type == "question_paper" for item in found)


def test_archive_year_order_can_prefer_oldest_cbse_year() -> None:
    spec = ArchiveSpec.from_mapping(
        {
            "id": "cbse-year-order-test",
            "provider": "html_archive",
            "exam_terms": ["cbse class 10"],
            "years": [2024, 2025],
            "year_order": "oldest",
            "page_url": "https://official.example/question-paper",
            "class_code": "X",
            "subjects": {"science": ["SCIENCE"]},
        }
    )
    links = [
        ("https://official.example/question-paper/2025/X/SCIENCE.zip", "Download"),
        ("https://official.example/question-paper/2024/X/SCIENCE.zip", "Download"),
    ]
    discovery = OfficialArchiveDiscovery(
        specs=[spec],
        page_fetch=lambda _url: FakePage(links),
    )
    found = discovery.discover(
        course_id="cbse-class-10-science",
        exam_name="CBSE Class 10",
        subject_name="Science",
        years=[2024, 2025],
    )
    assert len(found) == 2
    assert found[0].evidence["identity"]["year"] == 2024


def test_zip_member_set_codes_are_parsed_from_cbse_names() -> None:
    # The last component is the set; the middle component is the region.
    assert ZipBundleFetcher.member_set("PHYSICS/55_2_1_Physics.pdf") == "1"
    assert ZipBundleFetcher.member_set("Science/31_3_2_Science.pdf") == "2"
    assert ZipBundleFetcher.member_set("Physics/55-1-1,2,3 English Version.pdf") == "1"
    assert ZipBundleFetcher.member_sets("Physics/55-1-1,2,3 English Version.pdf") == {
        "1",
        "2",
        "3",
    }
    assert ZipBundleFetcher.member_code("MS 041_30-1-1 Mathematics 2023-24.pdf") == "30/1/1"
    assert ZipBundleFetcher.member_region("MS 041_30-1-1 Mathematics 2023-24.pdf") == "1"
    assert ZipBundleFetcher.member_set("Science/086 Science (English Medium)/paper.pdf") == ""


def test_zip_pdf_extraction_does_not_invoke_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page()
    payload = document.tobytes()
    document.close()

    def fail_ocr(_document: object) -> str:
        raise AssertionError("ZIP member extraction must not invoke OCR")

    monkeypatch.setattr(retrieval, "_ocr_pdf", fail_ocr)
    pages, text, method, warnings = retrieval._pdf_text(payload, allow_ocr=False)
    assert pages == 1
    assert text == ""
    assert method == "pymupdf_text"
    assert any("OCR disabled" in warning for warning in warnings)


CBSE_MARKING_SCHEME = """MARKING SCHEME : SCIENCE (086)
CODE :31/1/1
Q.NO.
VALUE POINT/EXPECTED ANSWERS
MARKS
TOTAL
MARKS
Section A
1.
(B) Zero
1
1
2.
(D) 5.0 x 10-2 J
1
1
3.
(A) Both assertion and reason are true
1
1
4.
(C) Shrink
1
1
5.
(B) 1000
1
1
6.
Explain the process of photosynthesis.
2
2
"""


def test_cbse_marking_scheme_extracts_only_explicit_mcq_answers() -> None:
    entries = extract_cbse_marking_scheme_entries(CBSE_MARKING_SCHEME)
    assert [entry.question_number for entry in entries] == ["1", "2", "3", "4", "5"]
    assert [entry.answer for entry in entries] == ["B", "D", "A", "C", "B"]
    assert all(entry.metadata["key_variant"] == "marking_scheme" for entry in entries)
    assert all(entry.metadata["question_type"] == "MCQ" for entry in entries)


def test_cbse_bare_number_layout_excludes_descriptive_subparts() -> None:
    text = """SECTION A
1.
(B) Zero
2.
(C) Five
3.
(A) True
4.
(D) None
5.
(B) 10
SECTION B
21.
(a) A descriptive value point is not an MCQ answer.
"""
    entries = extract_cbse_marking_scheme_entries(text)
    assert [(entry.question_number, entry.answer) for entry in entries] == [
        ("1", "B"),
        ("2", "C"),
        ("3", "A"),
        ("4", "D"),
        ("5", "B"),
    ]


def test_cbse_compact_table_layout_is_supported() -> None:
    text = "SECTION A\n1\nB\n2\nD\n3\nA\n4\nC\n5\nB\n"
    entries = extract_cbse_marking_scheme_entries(text)
    assert [entry.answer for entry in entries] == ["B", "D", "A", "C", "B"]


def test_cbse_solution_layout_and_first_set_boundary() -> None:
    text = """SECTION A
1.
Sol.
(b) 2
2.
Sol.
(d) 0.21
3.
Sol.
(c) 23
4.
Sol.
(c) 3
5.
Sol.
(d) 180
SECTION B
21.
This is descriptive.
SECTION A
1.
(a) 1
2.
(b) 2
3.
(c) 3
4.
(d) 4
5.
(a) 5
"""
    entries = extract_cbse_marking_scheme_entries(text)
    assert [(entry.question_number, entry.answer) for entry in entries] == [
        ("1", "B"),
        ("2", "D"),
        ("3", "C"),
        ("4", "C"),
        ("5", "D"),
    ]


def test_cbse_uppercase_alternative_outside_section_a_is_not_an_mcq() -> None:
    text = """SECTION A
1. (B) One
2. (D) Two
3. (A) Three
4. (C) Four
5. (B) Five
SECTION B
23. (A)
An descriptive alternative value point, not a key.
"""
    entries = extract_cbse_marking_scheme_entries(text)
    assert len(entries) == 5
    assert all(entry.question_number != "23" for entry in entries)


def test_retrieval_policy_spends_budget_on_paper_key_pairs_not_lone_keys() -> None:
    candidates = []
    for year in (2024, 2025, 2026):
        for document_type in ("question_paper", "answer_key"):
            candidates.append(
                SourceCandidate(
                    source_id=f"{document_type}-{year}",
                    course_id="gate-cs",
                    title=f"{document_type} {year}",
                    url=f"https://official.example/{document_type}-{year}.pdf",
                    document_type=document_type,
                    source_tier=1,
                    reliability=0.99,
                    evidence={
                        "document_variant": "final" if document_type == "answer_key" else "master",
                        "year_order": "newest",
                        "identity": {"year": year, "subject_code": "CS1"},
                    },
                )
            )
    selected = DatasetRecoveryService._select_retrieval_candidates(candidates, 6)
    assert {(item.document_type, item.evidence["identity"]["year"]) for item in selected} == {
        ("question_paper", 2024),
        ("answer_key", 2024),
        ("question_paper", 2025),
        ("answer_key", 2025),
        ("question_paper", 2026),
        ("answer_key", 2026),
    }


def test_gate_2025_2026_archives_are_official_paper_and_final_key_configs() -> None:
    specs = {spec.archive_id: spec for spec in load_archive_specs()}
    for year in (2025, 2026):
        spec = specs[f"gate-{year}-iitg-archive"]
        assert spec.provider == "document_urls"
        assert spec.matchable is True
        assert spec.page_url.startswith("https://gate2026.iitg.ac.in/")
        assert any(item.get("document_type") == "question_paper" for item in spec.documents)
        assert any(
            item.get("document_type") == "answer_key" and item.get("variant") == "final"
            for item in spec.documents
        )
        assert all(
            (item.get("identity") or {}).get("subject_code")
            for item in spec.documents
        )


def test_independent_answer_agreement_is_distinct_from_official_key() -> None:
    from exam_engine.recovery.verification import verify_answer

    outcome = verify_answer(
        question_text="Calculate 12 * (3 + 2)",
        options=[],
        answer_text="60",
        answer_type="numeric",
        candidate=AnswerCandidate(
            answer_text="60",
            source_type="structured_extraction",
            source_name="local row",
            verification_status="review_required",
            confidence=0.5,
            method="row_extraction",
            official=False,
        ),
        independent_answer="60",
    )
    assert outcome.result.status == "independently_solved_verified"
    assert outcome.result.independent is True
    assert outcome.result.method == "independent_answer_agreement"


def _gate_record() -> RecordResult:
    return RecordResult(
        record_id="r1",
        course_id="gate-civil-engineering",
        source_file="datasets/GATE/Civil_Engineering.csv",
        source_row=2,
        source_question_id="CE2024_Set1_Q01",
        original={
            "original_question_number": "1",
            "question_id": "CE2024_Set1_Q01",
        },
        normalized={},
        repaired={},
        structure={},
    )


def _key_source(code: str, answer: str = "A") -> dict:
    index = AnswerKeyIndex()
    index.add(
        AnswerKeyEntry(
            question_number="1",
            answer=answer,
            source_kind="tabular_answer_key",
            metadata={"question_type": "MCQ", "key_variant": "final"},
        )
    )
    return {
        "index": index,
        "source_name": f"{code}FinalAnswerKey",
        "reference": f"https://official.example/{code}FinalAnswerKey.pdf",
        "url": f"https://official.example/{code}FinalAnswerKey.pdf",
        "tier": 1,
        "entries": 1,
        "identity": {"subject_code": code, "year": 2024, "session": code[-1]},
        "session_ambiguous": True,
        "key_variant": "final",
    }


def test_gate_session_key_must_match_retrieved_paper_code() -> None:
    draft = SimpleNamespace(
        source_matches={
            "r1": {
                "question_number": "1",
                "source_identity": {"subject_code": "CE1", "year": 2024, "set": "1"},
            }
        },
        answer_keys=[],
        web_answer_keys=[_key_source("CE2", "B"), _key_source("CE1", "A")],
    )
    official = DatasetRecoveryService._official_answer(_gate_record(), draft)
    assert official is not None
    assert official["answer"] == "A"
    assert official["source_name"] == "CE1FinalAnswerKey"
    assert official["evidence"]["session"] == "1"


def test_gate_ambiguous_key_is_rejected_without_paper_code() -> None:
    draft = SimpleNamespace(
        source_matches={
            "r1": {
                "question_number": "1",
                "source_identity": {"subject_code": "", "year": 2024},
            }
        },
        answer_keys=[],
        web_answer_keys=[_key_source("CE1", "A")],
    )
    assert DatasetRecoveryService._official_answer(_gate_record(), draft) is None


def test_gate_prefixed_record_number_matches_numeric_key() -> None:
    record = _gate_record()
    record.course_id = "gate-aerospace-engineering"
    record.source_question_id = "AE2024_AE_Q19"
    record.original["original_question_number"] = "AE Q19"
    index = AnswerKeyIndex()
    index.add(
        AnswerKeyEntry(
            question_number="19",
            answer="A",
            source_kind="tabular_answer_key",
            metadata={"question_type": "MCQ", "key_variant": "final"},
        )
    )
    draft = SimpleNamespace(
        source_matches={
            "r1": {
                "question_number": "19",
                "source_identity": {"subject_code": "AE", "year": 2024},
            }
        },
        answer_keys=[],
        web_answer_keys=[
            {
                "index": index,
                "source_name": "AEFinalAnswerKey",
                "reference": "https://official.example/AEFinalAnswerKey.pdf",
                "url": "https://official.example/AEFinalAnswerKey.pdf",
                "tier": 1,
                "entries": 1,
                "identity": {"subject_code": "AE", "year": 2024},
                "session_ambiguous": False,
                "key_variant": "final",
            }
        ],
    )
    official = DatasetRecoveryService._official_answer(record, draft)
    assert official is not None
    assert official["answer"] == "A"


def test_archive_status_distinguishes_unavailable_and_unmatchable() -> None:
    statuses = {spec.archive_id: spec.status() for spec in load_archive_specs()}
    assert statuses["neet-nta-official"]["available"] is False
    assert statuses["neet-nta-official"]["coverage_state"] == "unavailable"
    assert statuses["nta-jee-main-2024-final-keys"]["matchable"] is False
    assert (
        statuses["nta-jee-main-2024-final-keys"]["coverage_state"]
        == "documented_unmatchable"
    )
    assert statuses["cbse-class-12-2024-2025"]["coverage_state"] == "configured"
