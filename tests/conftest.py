from __future__ import annotations

from pathlib import Path

import pytest

from exam_engine.storage.db import Database, memory_database
from tests.support import make_course, make_source

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MANIFEST = PROJECT_ROOT / "datasets" / "sample" / "manifest.json"


@pytest.fixture()
def database() -> Database:
    db = memory_database()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def course():
    return make_course()


@pytest.fixture()
def source():
    return make_source()


@pytest.fixture()
def sample_manifest() -> Path:
    return SAMPLE_MANIFEST
