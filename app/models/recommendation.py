"""Optimizer and trade-analyzer outputs. RFC 11.5."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    REAL,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import RECOMMENDATION_KIND, RecommendationKind


class Recommendation(Base):
    """Unified store for lineup / trade / waiver / start-sit outputs.

    One table rather than one per kind so the explanation engine has exactly
    one place to retrieve "what did we recommend and why" (RFC 11.5). The
    kind-specific shape lives in ``payload`` and carries a ``schema_version``.
    """

    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_recs_team"),
        nullable=False,
    )
    kind: Mapped[RecommendationKind] = mapped_column(RECOMMENDATION_KIND, nullable=False)
    week: Mapped[int | None] = mapped_column(SmallInteger)
    model_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("model_versions.id", name="fk_recs_model_version")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(REAL)
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_recs_confidence_range"),
        Index(
            "idx_recs_team_kind",
            "team_id",
            "kind",
            text("week DESC"),
            text("generated_at DESC"),
        ),
        Index("idx_recs_expiry", "expires_at"),
        Index("idx_recs_model_version", "model_version_id"),
    )


class TradeAnalysis(Base):
    """A scored trade proposal, including its championship-probability delta."""

    __tablename__ = "trade_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_trades_league"),
        nullable=False,
    )
    proposing_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", name="fk_trades_proposing_team"),
        nullable=False,
    )
    receiving_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", name="fk_trades_receiving_team")
    )
    players_out: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    players_in: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    score: Mapped[decimal.Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    champ_prob_delta: Mapped[float | None] = mapped_column(REAL)
    model_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_versions.id", name="fk_trades_model_version"),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("score BETWEEN -100 AND 100", name="ck_trades_score_range"),
        Index("idx_trades_team", "proposing_team_id", text("created_at DESC")),
        Index("idx_trades_league", "league_id"),
    )


__all__ = ["Recommendation", "TradeAnalysis"]
