"""Dataset intelligence, repair and answer-recovery subsystem."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "DatasetRecoveryService":
        from exam_engine.recovery.service import DatasetRecoveryService

        return DatasetRecoveryService
    raise AttributeError(name)


__all__ = ["DatasetRecoveryService"]
