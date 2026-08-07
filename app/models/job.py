"""Async job records and league sync diagnostics. RFC 11.5 / 7.2."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import JOB_STATUS, JobStatus


class Job(Base):
    """Durable record of an async job.

    Redis is the broker but never the record of truth: a row is written here at
    enqueue time and updated on every transition. If Redis loses data, a
    reconciler sweeps QUEUED/RUNNING rows past their ``deadline_at`` (or with a
    stale ``heartbeat_at``) and re-enqueues or fails them. One extra DB write
    per job eliminates an entire class of silent job loss (RFC 7.2).
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_jobs_user"),
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        JOB_STATUS, nullable=False, server_default=text("'QUEUED'")
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    progress_pct: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    progress_step: Mapped[str | None] = mapped_column(Text)
    result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "idempotency_key", name="uq_job_idem"),
        Index(
            "idx_jobs_reaper",
            "status",
            "heartbeat_at",
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
        Index("idx_jobs_user", "user_id", text("created_at DESC")),
    )


class SyncError(Base):
    """A non-fatal problem encountered during a league sync.

    Written rather than raised so a partial import still yields a usable league
    plus an explicit reconciliation report (FR-1.7).
    """

    __tablename__ = "sync_errors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    league_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_sync_errors_league"),
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL", name="fk_sync_errors_job"),
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_sync_errors_league", "league_id", text("created_at DESC")),
        Index("idx_sync_errors_job", "job_id"),
    )


__all__ = ["Job", "SyncError"]
