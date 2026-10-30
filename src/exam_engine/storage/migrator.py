"""Minimal, checksum-verified SQL migration runner.

Migrations are plain ``.sql`` files applied once, in filename order. Checksums
are recorded so an edited migration is detected instead of silently skipped.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATIONS_TABLE = "schema_migrations"


class MigrationError(RuntimeError):
    """Raised when migrations cannot be applied safely."""


def split_sql_statements(script: str) -> list[str]:
    """Split a DDL script on statement boundaries.

    ``sqlite3.complete_statement`` understands quoted strings and comments, which
    avoids the classic naive ``split(';')`` failure inside string literals.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = _strip_leading_comments(buffer)
            if statement:
                statements.append(statement)
            buffer = ""
    leftover = _strip_leading_comments(buffer)
    if leftover:
        raise MigrationError(f"unterminated SQL statement: {leftover[:120]!r}")
    return statements


def _strip_leading_comments(statement: str) -> str:
    """Drop leading comment/blank lines without discarding the DDL that follows."""
    lines = statement.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines).strip()


def discover_migrations(directory: Path | None = None) -> list[Path]:
    directory = directory or MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    return sorted(p for p in directory.glob("*.sql") if p.is_file())


def applied_migrations(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (MIGRATIONS_TABLE,),
        ).first()
        if not exists:
            return {}
        rows = conn.exec_driver_sql(
            f"SELECT version, checksum FROM {MIGRATIONS_TABLE} ORDER BY version"
        ).all()
    return {row[0]: row[1] for row in rows}


def migrate(engine: Engine, directory: Path | None = None) -> list[str]:
    """Apply pending migrations. Returns the versions applied in this call."""
    paths = discover_migrations(directory)
    already = applied_migrations(engine)
    applied_now: list[str] = []

    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
                version    TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                checksum   TEXT NOT NULL
            )
            """
        )

    for path in paths:
        version = path.name
        script = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if version in already:
            if already[version] != checksum:
                raise MigrationError(
                    f"migration {version} was modified after being applied "
                    f"(recorded {already[version][:12]}, file {checksum[:12]}); "
                    "add a new migration instead"
                )
            continue

        statements = split_sql_statements(script)
        try:
            with engine.begin() as conn:
                for statement in statements:
                    conn.exec_driver_sql(statement)
                conn.exec_driver_sql(
                    f"INSERT INTO {MIGRATIONS_TABLE} "
                    "(version, applied_at, checksum) VALUES (?,?,?)",
                    (version, datetime.now(UTC).isoformat(), checksum),
                )
        except Exception as exc:  # noqa: BLE001 - surface as migration failure
            raise MigrationError(f"migration {version} failed: {exc}") from exc
        applied_now.append(version)

    return applied_now


def pending_migrations(engine: Engine, directory: Path | None = None) -> Iterable[str]:
    already = applied_migrations(engine)
    return [p.name for p in discover_migrations(directory) if p.name not in already]
