"""Job wire contracts."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.jobs import JobKind

PROGRESS_MIN = 0
PROGRESS_MAX = 100


class JobStatusResponse(BaseModel):
    """What ``GET /v1/jobs/{id}`` returns.

    ``result_ref`` is a pointer, not the result: a reference to where the work
    landed (a league id, a simulation id). Async endpoints return handles and
    clients fetch the real resource, so a job row never has to grow to hold a
    payload.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: JobKind
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"]
    progress_pct: int = Field(ge=PROGRESS_MIN, le=PROGRESS_MAX)
    progress_step: str | None = None
    result_ref: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    attempts: int
    created_at: dt.datetime
    updated_at: dt.datetime
    deadline_at: dt.datetime


class JobProgressEvent(BaseModel):
    """The ``progress`` SSE event payload (RFC 12.4)."""

    pct: int = Field(ge=PROGRESS_MIN, le=PROGRESS_MAX)
    step: str | None = None


class JobCompleteEvent(BaseModel):
    """The ``complete`` SSE event payload -- the last event on a stream."""

    status: Literal["SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"]
    result_ref: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


__all__ = [
    "JobCompleteEvent",
    "JobProgressEvent",
    "JobStatusResponse",
]
