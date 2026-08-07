"""Response envelopes shared across endpoints: pagination, jobs, NFL state."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ItemT = TypeVar("ItemT")

WEEK_MIN = 1
WEEK_MAX = 22
SEASON_MIN = 2018
SEASON_MAX = 2030

# Bounded at the type level so every endpoint that takes a week or season
# inherits the same validation, and the bounds appear in the OpenAPI schema
# rather than living in handler bodies (RFC 20.2).
Week = Annotated[int, Field(ge=WEEK_MIN, le=WEEK_MAX, description="NFL week, 1-22")]
Season = Annotated[int, Field(ge=SEASON_MIN, le=SEASON_MAX, description="NFL season year")]


class Page(BaseModel, Generic[ItemT]):
    """Keyset-paginated collection (RFC 12.1).

    ``next_cursor`` is opaque: clients pass it back verbatim and must not
    parse it. Keeping it opaque is what lets the underlying sort key change
    without breaking every stored cursor.
    """

    data: list[ItemT]
    next_cursor: str | None = None
    has_more: bool = False


class JobHandle(BaseModel):
    """The 202 body of every async endpoint.

    Async endpoints return a handle, never a partial result. A client polls
    ``status_url`` or subscribes to ``stream_url``; both are absolute paths so
    it never has to build a URL from a template (RFC 12.3).
    """

    job_id: str
    status: str
    status_url: str
    stream_url: str
    estimated_seconds: int | None = None


class NflState(BaseModel):
    """Where the NFL season currently is. Drives week defaults everywhere."""

    model_config = ConfigDict(from_attributes=True)

    season: int
    week: int
    season_type: str
    next_kickoff_at: dt.datetime | None = None
    lock_at: dt.datetime | None = Field(
        default=None,
        description="Kickoff of the first game of the current week; lineups lock per game.",
    )


class ModelVersionSummary(BaseModel):
    """One active model and its headline evaluation metrics."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    name: str
    version: str
    position: str | None = None
    trained_at: dt.datetime
    metrics: dict[str, float] = Field(default_factory=dict)


__all__ = [
    "JobHandle",
    "ModelVersionSummary",
    "NflState",
    "Page",
    "Season",
    "Week",
]
