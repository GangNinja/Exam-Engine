"""SQLite engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from exam_engine.storage.migrator import migrate


@dataclass
class Database:
    """A migrated database plus its session factory."""

    engine: Engine
    session_factory: sessionmaker[Session]
    path: Path | None = None
    applied_migrations: list[str] = field(default_factory=list)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Session scoped to one commit-or-rollback unit."""
        with self.session_factory.begin() as session:
            yield session

    def migrate(self) -> list[str]:
        applied = migrate(self.engine)
        self.applied_migrations.extend(applied)
        return applied

    def close(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


def _build_engine(url: str, *, echo: bool, memory: bool = False) -> Engine:
    kwargs: dict[str, object] = {"echo": echo, "future": True}
    if memory:
        kwargs.update(
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    engine = create_engine(url, **kwargs)
    _install_sqlite_pragmas(engine)
    return engine


def open_database(
    path: Path | str,
    *,
    migrate_schema: bool = True,
    echo: bool = False,
) -> Database:
    """Open (and by default migrate) a file-backed SQLite database."""
    db_path = Path(path)
    if db_path.parent and str(db_path.parent) not in ("", "."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = _build_engine(f"sqlite+pysqlite:///{db_path.as_posix()}", echo=echo)
    database = Database(
        engine=engine,
        session_factory=sessionmaker(bind=engine, expire_on_commit=False, future=True),
        path=db_path,
    )
    if migrate_schema:
        database.migrate()
    return database


def memory_database(*, echo: bool = False) -> Database:
    """In-memory database for tests; keeps one connection alive via StaticPool."""
    engine = _build_engine("sqlite+pysqlite://", echo=echo, memory=True)
    database = Database(
        engine=engine,
        session_factory=sessionmaker(bind=engine, expire_on_commit=False, future=True),
    )
    database.migrate()
    return database
