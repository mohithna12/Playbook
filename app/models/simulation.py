"""Monte Carlo simulation results. RFC 11.5."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SimulationResult(Base):
    """One completed simulation run for a league.

    The unique constraint on ``(league_state_hash, n_simulations, seed,
    scenario)`` is a durable memoization layer: an identical request finds this
    row instead of burning ~3 CPU-seconds. Redis caches the same result for hot
    reads; Postgres retains it across cache eviction (RFC 11.5).

    ``scenario`` is NULL for the baseline run and a counterfactual descriptor
    otherwise. Because NULLs are distinct in a unique index, the constraint is
    built on the expression ``COALESCE(scenario::text, '')``.
    """

    __tablename__ = "simulation_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_sim_league"),
        nullable=False,
    )
    as_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    n_simulations: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    league_state_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    runtime_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_versions.id", name="fk_sim_model_version"),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "uq_sim",
            "league_state_hash",
            "n_simulations",
            "seed",
            text("COALESCE(scenario::text, '')"),
            unique=True,
        ),
        Index(
            "idx_sim_league",
            "league_id",
            text("as_of_week DESC"),
            text("created_at DESC"),
        ),
        Index("idx_sim_model_version", "model_version_id"),
    )


__all__ = ["SimulationResult"]
