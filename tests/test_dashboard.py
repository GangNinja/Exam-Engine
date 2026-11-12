"""Phase 12 dashboard integration tests.

These tests ingest the labelled synthetic sample into a real file-backed SQLite
database, run the existing Phase 2, 3 and 5 services, and then exercise the
dashboard API over that stored data. They never assert that synthetic fixtures
prove educational accuracy; they only verify that the dashboard reports what
the backend actually stored.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest

from exam_engine.config import Settings
from exam_engine.dashboard.api import DashboardAPI
from exam_engine.dashboard.errors import ApiError
from exam_engine.dashboard.server import create_server
from exam_engine.dashboard.tutor_ai import _PartialAnswerExtractor
from exam_engine.graph.service import GraphService
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.nlp.service import NlpService
from exam_engine.storage.db import open_database
from exam_engine.understanding.service import UnderstandingService

CS = "demo-cs201-2024-v1"
PHY = "demo-phy101-2024-v1"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Settings]:
    directory = tmp_path_factory.mktemp("phase12-dashboard")
    path = directory / "dashboard.db"
    settings = Settings.from_env(
        {
            "EXAM_ENGINE_TUTOR_PROVIDER": "off",
            "EXAM_ENGINE_TUTOR_WEB_SEARCH": "off",
        }
    ).with_db(path)
    database = open_database(path)
    try:
        batch = IngestionPipeline(database, settings=settings).ingest_manifest(
            Path(__file__).resolve().parents[1] / "datasets" / "sample" / "manifest.json"
        )
        assert all(result.status.value != "failed" for result in batch.results)
        for course_id in (CS, PHY):
            NlpService(database, settings.nlp).analyze_course(course_id)
            UnderstandingService(database, settings.understanding).analyze_course(course_id)
            GraphService(database, settings.graph).build_course(course_id)
    finally:
        database.close()
    return path, settings


@pytest.fixture()
def api(corpus: tuple[Path, Settings]):
    path, settings = corpus
    instance = DashboardAPI(path, settings)
    try:
        yield instance
    finally:
        instance.close()


def call(api: DashboardAPI, method: str, path: str, body: dict | None = None):
    parsed = urlsplit(path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.dispatch(method, parsed.path, query, raw)


def test_health_and_capabilities_are_explicit(api: DashboardAPI) -> None:
    health = call(api, "GET", "/api/health")
    assert health.status == 200
    assert health.payload["authentication"]["enabled"] is False

    response = call(api, "GET", "/api/capabilities")
    assert response.status == 200
    payload = response.payload
    assert payload["phase"] == 13
    modules = {item["id"]: item for item in payload["modules"]}
    assert modules["grading"]["available"] is True
    assert "local" in modules["grading"]["detail"].lower()
    assert payload["artwork"]["hairline"]["license"] == "MIT"
    assert payload["artwork"]["tutor_avatar"]["studio_bundled"] is False
    assert payload["privacy"]["network_requests"] is False


def test_course_scope_and_document_isolation(api: DashboardAPI) -> None:
    listing = call(api, "GET", "/api/courses")
    assert listing.status == 200
    courses = {item["course_id"]: item for item in listing.payload["courses"]}
    assert set(courses) == {CS, PHY}
    assert courses[CS]["counts"]["documents"] == 4
    assert courses[PHY]["counts"]["documents"] == 3
    assert all(item["analysis"]["understanding"]["analyzed"] > 0 for item in courses.values())

    cs = call(api, "GET", f"/api/courses/{CS}")
    phy = call(api, "GET", f"/api/courses/{PHY}")
    assert cs.status == phy.status == 200
    cs_titles = {item["title"] for item in cs.payload["documents"]}
    phy_titles = {item["title"] for item in phy.payload["documents"]}
    assert cs_titles and phy_titles
    assert cs_titles.isdisjoint(phy_titles)

    documents = call(api, "GET", f"/api/courses/{PHY}/documents")
    assert documents.status == 200
    assert len(documents.payload["documents"]) == 3

    unknown = call(api, "GET", "/api/courses/not-a-course")
    assert unknown.status == 404
    assert unknown.payload["error"]["code"] == "unknown_course"

    bad_query = call(api, "GET", f"/api/courses/{CS}/questions?limit=9999")
    assert bad_query.status == 400
    assert bad_query.payload["error"]["code"] == "invalid_query"


def test_analysis_reports_are_real_and_course_scoped(api: DashboardAPI) -> None:
    coverage = call(api, "GET", f"/api/courses/{CS}/coverage?view=summary")
    assert coverage.status == 200
    assert coverage.payload["course"]["course_id"] == CS
    assert coverage.payload["summary"]["total_concepts"] > 0
    assert coverage.payload["syllabus"]["syllabus_id"]
    assert "unmapped_questions" in coverage.payload
    assert "question_assignments" in coverage.payload

    analytics = call(api, "GET", f"/api/courses/{CS}/analytics?view=summary")
    assert analytics.status == 200
    assert analytics.payload["course"]["course_id"] == CS
    assert analytics.payload["data_quality"]["questions"] > 0

    exam_dna = call(api, "GET", f"/api/courses/{CS}/exam-dna?view=summary")
    assert exam_dna.status == 200
    assert exam_dna.payload["course"]["course_id"] == CS
    assert exam_dna.payload["summary"]["questions"] > 0

    evolution = call(api, "GET", f"/api/courses/{CS}/evolution?view=summary")
    assert evolution.status == 200
    assert evolution.payload["course"]["course_id"] == CS

    graph = call(api, "GET", f"/api/courses/{CS}/graph?limit=5")
    assert graph.status == 200
    assert graph.payload["status"]["concepts"] > 0
    assert len(graph.payload["concepts"]) <= 5
    assert graph.payload["truncated"] is True

    foreign_graph = call(api, "GET", f"/api/courses/{PHY}/graph?limit=5")
    assert foreign_graph.status == 200
    foreign_ids = {item["node_id"] for item in foreign_graph.payload["concepts"]}
    local_ids = {item["node_id"] for item in graph.payload["concepts"]}
    assert foreign_ids.isdisjoint(local_ids)


def test_generation_scope_ids_are_accepted_by_generation(api: DashboardAPI) -> None:
    options = call(api, "GET", f"/api/courses/{CS}/generation/options")
    assert options.status == 200
    assert options.payload["syllabus_id"]
    assert options.payload["concept_count"] == len(options.payload["concepts"]) > 0
    assert options.payload["providers"]

    concept = options.payload["concepts"][0]
    generated = call(
        api,
        "POST",
        f"/api/courses/{CS}/generate",
        {
            "concept_ids": [concept["concept_id"]],
            "count": 1,
            "format": "short_answer",
            "provider": "template",
            "include_answer_key": True,
        },
    )
    assert generated.status == 201, generated.payload
    batch = generated.payload
    assert batch["batch_id"].startswith("gb_")
    assert batch["counts"]["persisted"] == 1
    question = batch["questions"][0]
    assert question["course_id"] == CS
    assert question["concept_ids"] == [concept["concept_id"]]
    assert question["review_status"] == "pending_review"
    assert question["validation"]["status"] in {"validated", "needs_review"}

    review = call(
        api,
        "POST",
        f"/api/courses/{CS}/generated/{question['question_id']}/review",
        {"status": "approved", "reviewer": "pytest", "expected_revision": question["revision"]},
    )
    assert review.status == 200
    assert review.payload["review_status"] == "approved"
    assert review.payload["question_id"] == question["question_id"]
    assert any(event["status"] == "approved" for event in review.payload["review_history"])

    listed = call(api, "GET", f"/api/courses/{CS}/generated?review_status=approved&limit=50")
    assert listed.status == 200
    assert question["question_id"] in {item["question_id"] for item in listed.payload["questions"]}

    wrong_course = call(
        api,
        "POST",
        f"/api/courses/{PHY}/generated/{question['question_id']}/review",
        {"status": "rejected"},
    )
    assert wrong_course.status == 404
    assert wrong_course.payload["error"]["code"] == "unknown_generated_question"


def test_advanced_assessment_is_exposed_and_course_scoped(api: DashboardAPI) -> None:
    options = call(api, "GET", f"/api/courses/{CS}/generation/options").payload
    concept = options["concepts"][-1]
    source_batch = call(
        api,
        "POST",
        f"/api/courses/{CS}/generate",
        {
            "concept_ids": [concept["concept_id"]],
            "count": 1,
            "format": "explanation",
            "provider": "template",
            "include_answer_key": True,
        },
    )
    assert source_batch.status == 201, source_batch.payload
    source = source_batch.payload["questions"][0]

    advanced = call(api, "GET", f"/api/courses/{CS}/advanced")
    assert advanced.status == 200
    assert advanced.payload["capabilities"]["mutation_strategies"]
    assert advanced.payload["capabilities"]["challenge_strategies"]
    assert advanced.payload["counts"]["mutation"] >= 0

    mutation = call(
        api,
        "POST",
        f"/api/courses/{CS}/advanced/mutate",
        {"question_id": source["question_id"], "strategies": ["rephrase"], "count": 1},
    )
    assert mutation.status == 201, mutation.payload
    variants = mutation.payload["questions"]
    assert variants
    assert all(item["origin"] == "mutation" for item in variants)
    assert all(item["source_question_id"] == source["question_id"] for item in variants)
    assert all(item["review_status"] == "pending_review" for item in variants)

    cross_course = call(
        api,
        "POST",
        f"/api/courses/{PHY}/advanced/mutate",
        {"question_id": source["question_id"], "count": 1},
    )
    assert cross_course.status == 409
    assert cross_course.payload["error"]["code"] == "course_mismatch"

    challenge = call(
        api,
        "POST",
        f"/api/courses/{CS}/advanced/adversarial",
        {"concept_ids": [concept["concept_id"]], "count": 1},
    )
    assert challenge.status == 201, challenge.payload
    assert all(item["origin"] == "adversarial" for item in challenge.payload["questions"])
    assert all(item["solution_steps"] for item in challenge.payload["questions"])

    stored = call(api, "GET", f"/api/courses/{CS}/generated?limit=200").payload
    stored_ids = {item["question_id"] for item in stored["questions"]}
    assert {item["question_id"] for item in variants} <= stored_ids
    assert {item["question_id"] for item in challenge.payload["questions"]} <= stored_ids

    invalid = call(api, "GET", "/api/courses/not-a-course/advanced")
    assert invalid.status == 404


def test_tutor_uses_stored_evidence_and_refuses_unimplemented_work(api: DashboardAPI) -> None:
    context = call(api, "GET", f"/api/courses/{CS}/tutor/context")
    assert context.status == 200
    assert context.payload["concept_count"] > 0
    assert context.payload["capabilities"]["answer_grading"]["available"] is True
    assert context.payload["capabilities"]["conversational_reasoning"]["available"] is True
    assert context.payload["capabilities"]["exam_focus"]["available"] is True
    suggestion = context.payload["suggestions"][0]

    answer = call(
        api,
        "POST",
        f"/api/courses/{CS}/tutor/ask",
        {"question": f"Explain {suggestion['label']}", "concept_id": suggestion["concept_id"]},
    )
    assert answer.status == 200
    assert answer.payload["status"] == "answered"
    assert answer.payload["sections"]
    citations = [
        citation
        for section in answer.payload["sections"]
        for citation in section.get("citations", [])
    ]
    assert citations, "an answered tutor response must cite stored evidence"
    assert answer.payload["course_id"] == CS
    headings = [section["heading"] for section in answer.payload["sections"]]
    assert "Explanation" in headings
    assert "What to learn" in headings
    assert "What the analysed corpus shows" not in headings

    focus = call(
        api,
        "POST",
        f"/api/courses/{CS}/tutor/ask",
        {"question": "What should I study for the exam?"},
    )
    assert focus.status == 200
    assert focus.payload["status"] == "answered"
    assert focus.payload["sections"][0]["heading"] == "Your revision focus"
    assert "not a prediction" in focus.payload["answer"].lower()

    grading = call(
        api, "POST", f"/api/courses/{CS}/tutor/ask", {"question": "Grade my answer please"}
    )
    assert grading.status == 200
    assert grading.payload["status"] == "unavailable"
    assert grading.payload["event"] == "tutor_unavailable"
    assert "does not grade" in grading.payload["answer"].lower()
    assert "practice" in grading.payload["answer"].lower()
    assert grading.payload["sections"]

    forecast = call(api, "POST", f"/api/courses/{CS}/tutor/ask", {"question": "Predict the exam"})
    assert forecast.payload["status"] == "unavailable"
    assert "forecast" in forecast.payload["answer"].lower()

    unknown = call(
        api,
        "POST",
        f"/api/courses/{CS}/tutor/ask",
        {"question": "Explain something absent from this corpus"},
    )
    assert unknown.payload["status"] == "insufficient_evidence"
    assert unknown.payload["suggestions"]

    phy_context = call(api, "GET", f"/api/courses/{PHY}/tutor/context").payload
    phy_concept = phy_context["suggestions"][0]
    cross_course = call(
        api,
        "POST",
        f"/api/courses/{CS}/tutor/ask",
        {"question": "Explain this concept", "concept_id": phy_concept["concept_id"]},
    )
    assert cross_course.payload["status"] == "insufficient_evidence"
    assert cross_course.payload["concept"] is None


def test_generated_batch_expands_to_distinct_concepts_and_answer_checks_work(
    api: DashboardAPI,
) -> None:
    options = call(api, "GET", f"/api/courses/{CS}/generation/options")
    concept = options.payload["concepts"][0]
    provider_names = [item["name"] for item in options.payload["providers"]]
    assert provider_names == ["template", "auto"]
    assert options.payload["providers"][1]["resolved_name"] == "template"
    numerical = next(item for item in options.payload["formats"] if item["format"] == "numerical")
    assert numerical["requires_model"] is True
    assert numerical["supported_by_provider"] == []
    generated = call(
        api,
        "POST",
        f"/api/courses/{CS}/generate",
        {
            "concept_ids": [concept["concept_id"]],
            "count": 3,
            "expand_batch": True,
            "format": "short_answer",
            "provider": "template",
            "include_answer_key": True,
        },
    )
    assert generated.status == 201, generated.payload
    batch = generated.payload
    assert batch["counts"]["persisted"] == 3, {
        "counts": batch["counts"],
        "rejections": batch["rejections"],
        "duplicates": batch["duplicates"],
        "concepts": [item["concept_labels"] for item in batch["questions"]],
    }
    assert len({item["text"] for item in batch["questions"]}) == 3
    assert len({item["question_id"] for item in batch["questions"]}) == 3
    assert any(item["code"] == "generation_batch_expanded" for item in batch["warnings"])

    scoped = call(
        api,
        "GET",
        f"/api/courses/{CS}/generated?batch_id={batch['batch_id']}&limit=100",
    )
    assert scoped.status == 200
    assert scoped.payload["total"] == 3
    assert {item["question_id"] for item in scoped.payload["questions"]} == {
        item["question_id"] for item in batch["questions"]
    }

    question = batch["questions"][0]
    answer = (
        f"{question['concept_labels'][0]} is the topic. It is explained in the "
        "stored course material and the reference gives the key ideas."
    )
    checked = call(
        api,
        "POST",
        f"/api/courses/{CS}/generated/{question['question_id']}/grade",
        {"answer": answer},
    )
    assert checked.status == 200, checked.payload
    assert checked.payload["question_id"] == question["question_id"]
    assert 0.0 <= checked.payload["score"] <= 1.0
    assert checked.payload["verdict"] in {"strong_match", "partial", "needs_more"}
    assert checked.payload["expected_points"]
    assert checked.payload["expected_outline"]
    assert checked.payload["coverage"]["key_ideas"]["total"] == len(
        checked.payload["expected_outline"]
    )
    assert all(
        item["status"] in {"covered", "partial", "missing"}
        for item in checked.payload["expected_outline"]
    )
    assert "awarded_score" not in checked.payload
    assert "max_score" not in checked.payload
    assert "not a grade" in checked.payload["feedback"].casefold()
    assert checked.payload["limitations"]

    partial = call(
        api,
        "POST",
        f"/api/courses/{CS}/generated/{question['question_id']}/grade",
        {"answer": "This topic is described in the stored material."},
    )
    assert partial.status == 200, partial.payload
    assert (
        partial.payload["coverage"]["key_ideas"]["missing"]
        or partial.payload["coverage"]["key_ideas"]["partial"]
    )
    assert any(item["status"] != "covered" for item in partial.payload["expected_outline"])

    empty = call(
        api,
        "POST",
        f"/api/courses/{CS}/generated/{question['question_id']}/grade",
        {"answer": "   "},
    )
    assert empty.status == 400
    assert empty.payload["error"]["code"] == "answer_required"

    wrong_course = call(
        api,
        "POST",
        f"/api/courses/{PHY}/generated/{question['question_id']}/grade",
        {"answer": "A stack is linear."},
    )
    assert wrong_course.status == 404
    assert wrong_course.payload["error"]["code"] == "unknown_generated_question"


def test_tutor_does_not_claim_graph_only_concepts_are_syllabus_placed(
    api: DashboardAPI,
) -> None:
    """A concept with no Phase 7 row must not be described as syllabus content."""

    # The sample's graph-only concepts rank after its syllabus/coverage
    # suggestions, so request enough candidates to exercise the contract.
    context = call(api, "GET", f"/api/courses/{CS}/tutor/context?limit=50").payload
    graph_only = next(
        (
            item
            for item in context["suggestions"]
            if not item["unit_labels"] and "stack" in item["label"].casefold()
        ),
        None,
    )
    assert graph_only is not None, "the sample should retain a graph-only stack concept"

    answer = call(
        api,
        "POST",
        f"/api/courses/{CS}/tutor/ask",
        {"question": graph_only["label"], "concept_id": graph_only["concept_id"]},
    )
    assert answer.status == 200
    assert answer.payload["status"] == "answered"
    text = answer.payload["answer"].lower()
    assert "knowledge graph" in text
    assert "stored syllabus" not in text
    assert "no syllabus placement" in text


def test_analysis_jobs_run_and_are_reported(api: DashboardAPI) -> None:
    started = call(api, "POST", f"/api/courses/{CS}/analysis/nlp")
    assert started.status == 202
    job = started.payload["job"]
    assert job["status"] in {"queued", "running"}

    settled = api.jobs.wait(job["job_id"], timeout=30)
    assert settled is not None
    assert settled.status == "succeeded", settled.error
    fetched = call(api, "GET", f"/api/jobs/{job['job_id']}")
    assert fetched.status == 200
    assert fetched.payload["status"] == "succeeded"
    assert fetched.payload["course_id"] == CS

    unknown = call(api, "POST", f"/api/courses/{CS}/analysis/not-a-job")
    assert unknown.status == 404
    assert unknown.payload["error"]["code"] == "unknown_analysis"


def test_upload_validation_and_course_isolation(api: DashboardAPI) -> None:
    before_cs = call(api, "GET", f"/api/courses/{CS}/documents").payload["total"]
    before_phy = call(api, "GET", f"/api/courses/{PHY}/documents").payload["total"]

    with pytest.raises(ApiError) as bad_suffix:
        api.ingest_upload(PHY, "paper.exe", b"MZ\x00", {"kind": "question_paper"})
    assert bad_suffix.value.code == "unsupported_format"

    with pytest.raises(ApiError) as empty:
        api.ingest_upload(PHY, "paper.txt", b"", {"kind": "question_paper"})
    assert empty.value.code == "empty_file"

    uploaded = api.ingest_upload(
        PHY,
        "../../escaped.txt",
        b"1. State Newton's second law. [5 marks]\n2. Define momentum. [3 marks]\n",
        {
            "kind": "question_paper",
            "dataset_type": "synthetic",
            "source_id": "phase12-pytest-upload",
            "license": "Generated for Phase 12 automated tests.",
        },
    )
    assert uploaded.status == 201, uploaded.payload
    result = uploaded.payload["result"]
    assert result["status"] == "ingested"
    assert result["source_path"] == "escaped.txt"
    assert result["question_count"] == 2

    after_cs = call(api, "GET", f"/api/courses/{CS}/documents").payload["total"]
    after_phy = call(api, "GET", f"/api/courses/{PHY}/documents").payload["total"]
    assert after_cs == before_cs
    assert after_phy == before_phy + 1


def test_create_course_validation_and_duplicate_offerings(api: DashboardAPI) -> None:
    payload = {
        "course_id": "phase12-api-course",
        "course_code": "P12",
        "title": "Phase 12 API Course",
        "institution_code": "phase12",
        "institution_name": "Phase 12 Institute",
        "academic_year": "2026-27",
        "syllabus_version": "v1",
    }
    created = call(api, "POST", "/api/courses", payload)
    assert created.status == 201
    assert created.payload["created"] is True
    again = call(api, "POST", "/api/courses", payload)
    assert again.status == 200
    assert again.payload["created"] is False

    conflict = call(
        api,
        "POST",
        "/api/courses",
        {**payload, "course_id": "phase12-api-course-conflict"},
    )
    assert conflict.status == 409
    assert conflict.payload["error"]["code"] == "course_conflict"

    options = call(api, "GET", "/api/courses/phase12-api-course/generation/options")
    assert options.status == 409
    assert options.payload["error"]["code"] == "no_syllabus"

    invalid = call(api, "POST", "/api/courses", {**payload, "course_id": "x"})
    assert invalid.status == 422
    assert invalid.payload["error"]["code"] == "invalid_request"


def test_streaming_answer_extractor_handles_split_json_escapes() -> None:
    extractor = _PartialAnswerExtractor()
    chunks = [
        '{"answer":"A stack uses ',
        'LIFO. \\"Top\\" is removed first.\\',
        'nNext line.", "key_points": []}',
    ]
    text = "".join(extractor.feed(chunk) for chunk in chunks)
    assert text == 'A stack uses LIFO. "Top" is removed first.\nNext line.'


def test_http_server_streams_tutor_progress_and_result(api: DashboardAPI) -> None:
    context = call(api, "GET", f"/api/courses/{CS}/tutor/context")
    suggestion = context.payload["suggestions"][0]
    server = create_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        request = Request(
            f"{base}/api/courses/{CS}/tutor/ask/stream",
            data=json.dumps(
                {
                    "question": f"Explain {suggestion['label']}",
                    "concept_id": suggestion["concept_id"],
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        with urlopen(request, timeout=10) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/event-stream")
            raw = response.read().decode("utf-8")
        frames: list[tuple[str, dict]] = []
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            event = "message"
            data: list[str] = []
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[len("event: ") :]
                elif line.startswith("data: "):
                    data.append(line[len("data: ") :])
            assert data, block
            frames.append((event, json.loads("\n".join(data))))
        names = [name for name, _ in frames]
        assert names[0] == "status"
        assert any(
            name == "status"
            and payload.get("stage") == "stored_evidence"
            and payload.get("message")
            for name, payload in frames
        )
        assert "result" in names
        assert names[-1] == "result"
        result = next(payload for name, payload in frames if name == "result")
        assert result["status"] == "answered"
        assert result["sections"]
    finally:
        server.shutdown()
        server.server_close()


def test_http_server_refuses_a_port_already_in_use(api: DashboardAPI) -> None:
    first = create_server(api, "127.0.0.1", 0)
    try:
        port = int(first.server_address[1])
        with pytest.raises(OSError):
            create_server(api, "127.0.0.1", port)
    finally:
        first.server_close()


def test_http_server_assets_csp_and_cross_origin(api: DashboardAPI) -> None:
    server = create_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        with urlopen(f"{base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            assert response.status == 200
            assert "text/html" in response.headers["Content-Type"]
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            assert "X-Frame-Options" in response.headers
        assert "skip-link" in html
        assert 'id="view"' in html

        with urlopen(f"{base}/assets/app.js", timeout=5) as response:
            script = response.read()
            assert b"createApi" in script
            assert "javascript" in response.headers["Content-Type"]
        with urlopen(f"{base}/assets/styles.css", timeout=5) as response:
            styles = response.read()
            assert b"prefers-reduced-motion" in styles
        with urlopen(f"{base}/assets/vendor/hairline/index.js", timeout=5) as response:
            assert response.status == 200
            assert len(response.read()) > 10_000
        with urlopen(f"{base}/api/health", timeout=5) as response:
            assert response.headers["Cache-Control"] == "no-store"

        try:
            urlopen(f"{base}/assets/../pyproject.toml", timeout=5)
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("path traversal must be rejected")

        request = Request(
            f"{base}/api/courses",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://attacker.invalid",
            },
        )
        try:
            urlopen(request, timeout=5)
        except HTTPError as exc:
            assert exc.code == 403
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["error"]["code"] == "cross_origin_rejected"
        else:
            raise AssertionError("cross-origin mutation must be rejected")
        time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
