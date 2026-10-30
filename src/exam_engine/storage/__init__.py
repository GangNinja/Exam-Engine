"""Persistence layer for Phase 1."""

from exam_engine.storage.db import Database, memory_database, open_database
from exam_engine.storage.migrator import MigrationError, migrate
from exam_engine.storage.repositories import (
    CourseRepository,
    DatasetSourceRepository,
    DocumentRepository,
    QuestionRepository,
    RunRepository,
    SyllabusRepository,
)

__all__ = [
    "CourseRepository",
    "Database",
    "DatasetSourceRepository",
    "DocumentRepository",
    "MigrationError",
    "QuestionRepository",
    "RunRepository",
    "SyllabusRepository",
    "memory_database",
    "migrate",
    "open_database",
]
