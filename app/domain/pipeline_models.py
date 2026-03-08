"""
app/domain/pipeline_models.py

Pydantic models specific to the GitLab pipeline (deploy) domain.

Kept separate from models.py to avoid bloating the auth models file.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.models import ApiResponse


# ──────────────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────────────

class PipelineVariable(BaseModel):
    """A single key-value variable passed to a GitLab pipeline."""

    key: str
    value: str


class TriggerPipelineRequest(BaseModel):
    """Body for POST /api/v1/deploy/stage."""

    variables: list[PipelineVariable] = Field(
        default_factory=list,
        description="Additional variables to inject into the pipeline.",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Data payloads (returned inside ApiResponse[T])
# ──────────────────────────────────────────────────────────────────────────────

class JobData(BaseModel):
    """Job summary returned inside PipelineData."""

    id: int
    name: str
    status: str


class PipelineData(BaseModel):
    """Pipeline summary returned by all deploy endpoints."""

    id: int
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tag_list: list[str] = Field(
        default_factory=list,
        description="Unique runner tags across all pipeline jobs.",
    )
    variables: list[PipelineVariable] = Field(
        default_factory=list,
        description="All variables the pipeline was triggered with.",
    )
    jobs: list[JobData] = Field(
        default_factory=list,
        description="All jobs associated with this pipeline.",
    )
    ref_name: str = ""
    web_url: str = ""


class CancelRetryData(BaseModel):
    """Minimal acknowledgement returned by cancel / retry."""

    pipeline_id: int
    status: str
    message: str


class RunningPipelinesData(BaseModel):
    """Response for POST /api/v1/deploy/stage/check-running.

    Returns all active pipelines whose ref AND variables match the query.
    ``has_running`` is a convenience flag so callers don't have to inspect
    the list to know if a conflict exists.
    """

    has_running: bool
    count: int
    pipelines: list[PipelineData]
