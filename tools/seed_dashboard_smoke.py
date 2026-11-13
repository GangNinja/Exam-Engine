"""Seed an isolated database for the Practice browser smoke test.

The browser test must exercise real persisted services without touching the
operator's dashboard database or requiring a running Ollama model. This helper
ingests the small synthetic sample, runs the local analyses, and creates one
pending short-answer question through the same dashboard API used by the UI.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from exam_engine.config import Settings
from exam_engine.dashboard.api import DashboardAPI
from exam_engine.graph.service import GraphService
from exam_engine.ingest.pipeline import IngestionPipeline
from exam_engine.nlp.service import NlpService
from exam_engine.storage.db import open_database
from exam_engine.understanding.service import UnderstandingService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MANIFEST = PROJECT_ROOT / "datasets" / "sample" / "manifest.json"
COURSE_ID = "demo-cs201-2024-v1"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path", type=Path)
    args = parser.parse_args()
    db_path: Path = args.db_path
    if db_path.exists():
        raise SystemExit(f"refusing to overwrite an existing database: {db_path}")

    settings = Settings.from_env(
        {
            "EXAM_ENGINE_TUTOR_PROVIDER": "off",
            "EXAM_ENGINE_TUTOR_WEB_SEARCH": "off",
        }
    ).with_db(db_path)
    database = open_database(db_path)
    try:
        batch = IngestionPipeline(database, settings=settings).ingest_manifest(SAMPLE_MANIFEST)
        failed = [result for result in batch.results if result.status.value == "failed"]
        if failed:
            raise SystemExit(f"sample ingestion failed: {failed[0].detail}")
        NlpService(database, settings.nlp).analyze_course(COURSE_ID)
        UnderstandingService(database, settings.understanding).analyze_course(COURSE_ID)
        GraphService(database, settings.graph).build_course(COURSE_ID)
    finally:
        database.close()

    api = DashboardAPI(db_path, settings)
    try:
        options = api.dispatch(
            "GET",
            f"/api/courses/{COURSE_ID}/generation/options",
            {},
        )
        if options.status != 200:
            raise SystemExit(f"generation options failed: {options.payload}")
        options_body = options.payload
        concepts = options_body.get("concepts") or []
        if not concepts:
            raise SystemExit("the sample course has no generation-ready concepts")
        concept = concepts[0]
        generated = api.dispatch(
            "POST",
            f"/api/courses/{COURSE_ID}/generate",
            {},
            json.dumps(
                {
                    "syllabus_id": options_body.get("syllabus_id"),
                    "concept_ids": [concept["concept_id"]],
                    "count": 1,
                    "format": "short_answer",
                    "provider": "template",
                    "include_answer_key": True,
                }
            ).encode("utf-8"),
        )
        if generated.status != 201:
            raise SystemExit(f"question generation failed: {generated.payload}")
        batch_payload = generated.payload
        questions = batch_payload.get("questions") or []
        if len(questions) != 1:
            raise SystemExit(f"expected one generated question, got {len(questions)}")
        question = questions[0]
        if question.get("review_status") != "pending_review":
            raise SystemExit("the smoke question was not created as a Draft")

        question_id = question["question_id"]
        grade = api.dispatch(
            "POST",
            f"/api/courses/{COURSE_ID}/generated/{question_id}/grade",
            {},
            json.dumps({"answer": "I do not know this topic yet."}).encode("utf-8"),
        )
        if grade.status != 200:
            raise SystemExit(f"answer check failed: {grade.payload}")
        outline = grade.payload.get("expected_outline") or []
        if not outline:
            raise SystemExit("the generated question has no answer-check outline")

        print(
            json.dumps(
                {
                    "course_id": COURSE_ID,
                    "syllabus_id": options_body.get("syllabus_id"),
                    "question_id": question_id,
                    "review_status": question["review_status"],
                    "outline_items": len(outline),
                },
                ensure_ascii=False,
            )
        )
    finally:
        api.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    sys.stdout.reconfigure(encoding="utf-8")
    main()
