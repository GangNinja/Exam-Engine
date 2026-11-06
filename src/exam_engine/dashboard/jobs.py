"""Small in-process job runner for long-running analysis operations.

The CLI is synchronous by design. The dashboard must stay responsive while NLP,
question understanding, graph construction or prerequisite discovery runs, so
those operations are submitted to a bounded worker pool and their progress is
polled through the API.

Jobs are deliberately ephemeral: they are not persisted, because Phase 1-11
never persisted analysis *commands* and inventing a durable queue would be a
new architectural subsystem. A restart clears job history; the data the job
wrote is already durable.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exam_engine.storage.db import Database, open_database

logger = logging.getLogger(__name__)

JobOperation = Callable[[Database], dict[str, Any]]

_ACTIVE = frozenset({"queued", "running"})


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    job_id: str
    course_id: str
    kind: str
    label: str
    status: str = "queued"
    message: str = "queued"
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> dict[str, Any]:
        finished = self.finished_at
        duration_ms = (
            int((finished - self.started_at).total_seconds() * 1000)
            if finished is not None and self.started_at is not None
            else None
        )
        return {
            "job_id": self.job_id,
            "course_id": self.course_id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": finished.isoformat() if finished else None,
            "duration_ms": duration_ms,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    """Bounded background executor with de-duplicated active work per course."""

    def __init__(self, db_path: Path | str, *, max_workers: int = 2) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="exam-dashboard",
        )
        self._closed = False

    # -- public API --------------------------------------------------------
    def submit(
        self,
        *,
        course_id: str,
        kind: str,
        label: str,
        operation: JobOperation,
    ) -> tuple[Job, bool]:
        """Submit work, returning the job and whether it was newly created.

        Submitting the same kind for the same course while a job is still active
        returns that job instead of starting duplicate work.
        """

        with self._lock:
            for job in self._jobs.values():
                if job.course_id == course_id and job.kind == kind and job.status in _ACTIVE:
                    return job, False
            job = Job(
                job_id=f"job_{uuid.uuid4().hex[:12]}",
                course_id=course_id,
                kind=kind,
                label=label,
            )
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._closed = False
            self._executor.submit(self._run, job, operation)
            return job, True

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, *, course_id: str | None = None, limit: int = 20) -> list[Job]:
        with self._lock:
            items = [self._jobs[key] for key in reversed(self._order)]
        if course_id is not None:
            items = [job for job in items if job.course_id == course_id]
        return items[: max(0, limit)]

    def wait(self, job_id: str, timeout: float = 30.0) -> Job | None:
        """Testing/CLI helper: block until a job settles."""

        job = self.get(job_id)
        if job is None:
            return None
        job._done.wait(timeout)
        return job

    def close(self, *, wait: bool = False) -> None:
        self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)

    # -- worker ------------------------------------------------------------
    def _run(self, job: Job, operation: JobOperation) -> None:
        with self._lock:
            job.status = "running"
            job.message = "running"
            job.started_at = _now()
        try:
            database = open_database(self.db_path, migrate_schema=False)
            try:
                result = operation(database)
            finally:
                database.close()
            with self._lock:
                job.status = "succeeded"
                job.message = "completed"
                job.result = result
        except Exception as exc:  # noqa: BLE001 - job fault boundary
            logger.exception("dashboard job %s (%s) failed", job.job_id, job.kind)
            with self._lock:
                job.status = "failed"
                job.message = str(exc)
                job.error = {
                    "code": getattr(exc, "code", "job_failed"),
                    "message": getattr(exc, "message", str(exc)) or "the job failed",
                }
        finally:
            with self._lock:
                job.finished_at = _now()
            job._done.set()


__all__ = ["Job", "JobManager"]
