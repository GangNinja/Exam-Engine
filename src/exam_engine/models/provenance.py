"""Provenance metadata for every dataset that enters the platform."""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from exam_engine.models.enums import DatasetType

SLUG_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,80}$"
_slug_re = re.compile(SLUG_PATTERN)


class DatasetSource(BaseModel):
    """Where a document came from, and under what terms it may be used.

    ``is_synthetic`` is derived from :attr:`dataset_type` so a synthetic set can
    never be silently relabelled as an official paper.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(..., description="Stable slug identifying this dataset.")
    name: str = Field(..., min_length=1, description="Human readable dataset name.")
    dataset_type: DatasetType
    retrieved_at: date = Field(..., description="Date this dataset was obtained.")
    license: str | None = Field(default=None, description="SPDX id or licence name.")
    license_url: str | None = None
    source_url: str | None = None
    publisher: str | None = None
    usage_terms: str | None = Field(
        default=None, description="Free-text usage terms when no formal licence exists."
    )
    notes: str | None = None
    external_dataset: bool = Field(
        default=False,
        description="True when the data originates outside this repository.",
    )

    @field_validator("source_id")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        if not _slug_re.match(value):
            raise ValueError("source_id must be a lowercase slug matching " + SLUG_PATTERN)
        return value

    @model_validator(mode="after")
    def _validate_terms(self) -> DatasetSource:
        if self.dataset_type is DatasetType.OPENLY_LICENSED and not self.license:
            raise ValueError(
                "openly_licensed datasets must record a licence in 'license' "
                "(public accessibility is not the same as a reuse licence)"
            )
        if self.dataset_type is DatasetType.SYNTHETIC and not (self.license or self.usage_terms):
            raise ValueError(
                "synthetic datasets must record their licence or usage terms "
                "so they cannot be mistaken for official material"
            )
        return self

    @property
    def is_synthetic(self) -> bool:
        return self.dataset_type is DatasetType.SYNTHETIC

    def provenance_warnings(self) -> list[str]:
        """Non-fatal gaps in provenance that callers surface as warnings."""
        warnings: list[str] = []
        if not self.license and not self.usage_terms:
            warnings.append(f"dataset '{self.source_id}' records no licence or usage terms")
        if self.dataset_type is DatasetType.OFFICIAL and self.external_dataset:
            warnings.append(
                f"dataset '{self.source_id}' is marked official but sourced externally; "
                "confirm the paper actually belongs to the stated course and year"
            )
        if self.dataset_type is DatasetType.SYNTHETIC:
            warnings.append(
                f"dataset '{self.source_id}' is synthetic: extracted questions are "
                "development fixtures, not official exam papers"
            )
        return warnings
