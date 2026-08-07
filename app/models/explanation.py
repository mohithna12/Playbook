"""LLM explanation audit log. RFC 11.5 / 17."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Explanation(Base):
    """One generated explanation, with its grounding and validation outcome.

    ``validation_status`` records what the numeric validator concluded
    (``PASSED`` / ``REGENERATED`` / ``TEMPLATE_FALLBACK``). Persisting it is
    what makes the "zero fabricated statistics" claim in G6 auditable after the
    fact rather than only at review time.

    ``context_hash`` doubles as the cache key, so an identical question against
    unchanged data reuses the stored response. Retention: 90 days.
    """

    __tablename__ = "explanations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_explanations_user"),
    )
    league_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_explanations_league"),
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_explanations_context", "context_hash", text("created_at DESC")),
        Index("idx_explanations_user", "user_id", text("created_at DESC")),
        Index("idx_explanations_league", "league_id"),
    )


__all__ = ["Explanation"]
