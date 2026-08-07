"""PostgreSQL enum types, mirrored as Python enums.

The database types are created once, explicitly, in migration ``0001``. Every
ORM column therefore binds with ``create_type=False`` — otherwise SQLAlchemy
would try to ``CREATE TYPE`` again on the first table that references it.

Enum names here must match the ``CREATE TYPE`` names in the migration exactly.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class LeagueProvider(StrEnum):
    SLEEPER = "SLEEPER"
    ESPN = "ESPN"
    YAHOO = "YAHOO"


class SyncStatus(StrEnum):
    PENDING = "PENDING"
    SYNCING = "SYNCING"
    ACTIVE = "ACTIVE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class Position(StrEnum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "DST"
    OL = "OL"
    DL = "DL"
    LB = "LB"
    DB = "DB"


class RecommendationKind(StrEnum):
    LINEUP = "LINEUP"
    TRADE = "TRADE"
    WAIVER = "WAIVER"
    START_SIT = "START_SIT"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"


class ModelStatus(StrEnum):
    """Not a PG enum — stored as TEXT so a new lifecycle state does not need DDL."""

    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    ROLLED_BACK = "ROLLED_BACK"


def pg_enum(python_enum: type[StrEnum], name: str) -> ENUM:
    """Bind a Python enum to an existing PostgreSQL enum type."""
    return ENUM(
        python_enum,
        name=name,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


LEAGUE_PROVIDER = pg_enum(LeagueProvider, "league_provider")
SYNC_STATUS = pg_enum(SyncStatus, "sync_status")
POSITION = pg_enum(Position, "player_position")
RECOMMENDATION_KIND = pg_enum(RecommendationKind, "recommendation_kind")
JOB_STATUS = pg_enum(JobStatus, "job_status")

__all__ = [
    "JOB_STATUS",
    "LEAGUE_PROVIDER",
    "POSITION",
    "RECOMMENDATION_KIND",
    "SYNC_STATUS",
    "JobStatus",
    "LeagueProvider",
    "ModelStatus",
    "Position",
    "RecommendationKind",
    "SyncStatus",
    "pg_enum",
]
