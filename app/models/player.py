"""NFL team and player dimensions plus the provider ID crosswalk. RFC 11.3."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CHAR,
    REAL,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import POSITION, Position


class NflTeam(Base):
    """The 32 NFL franchises. Seeded by ``scripts/seed_nfl_teams.py``."""

    __tablename__ = "nfl_teams"

    id: Mapped[int] = mapped_column(SmallInteger, Identity(), primary_key=True)
    abbr: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    conference: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    division: Mapped[str] = mapped_column(Text, nullable=False)
    stadium_lat: Mapped[float | None] = mapped_column(REAL)
    stadium_lon: Mapped[float | None] = mapped_column(REAL)
    is_dome: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class Player(Base):
    """Canonical, provider-independent player dimension.

    Players are never deleted — a departed player is marked
    ``status='RETIRED'`` — because ``weekly_stats`` and ``predictions``
    reference them and history must stay reconstructible (RFC 11.6).
    """

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    gsis_id: Mapped[str | None] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[Position] = mapped_column(POSITION, nullable=False)
    nfl_team_id: Mapped[int | None] = mapped_column(
        SmallInteger, ForeignKey("nfl_teams.id", name="fk_players_nfl_team")
    )
    jersey_number: Mapped[int | None] = mapped_column(SmallInteger)
    birth_date: Mapped[dt.date | None] = mapped_column(Date)
    height_in: Mapped[int | None] = mapped_column(SmallInteger)
    weight_lb: Mapped[int | None] = mapped_column(SmallInteger)
    rookie_year: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ACTIVE'"))
    depth_chart_order: Mapped[int | None] = mapped_column(SmallInteger)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "idx_players_pos_team",
            "position",
            "nfl_team_id",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        # The operator class goes in postgresql_ops, not inside the expression.
        # Baked into the text(), Alembic cannot compare the index against the
        # deployed one and warns on every autogenerate/comparison run.
        Index(
            "idx_players_name_trgm",
            "full_name",
            postgresql_using="gin",
            postgresql_ops={"full_name": "gin_trgm_ops"},
        ),
    )


class PlayerExternalId(Base):
    """Crosswalk from a provider's player ID to our canonical ``player_id``.

    Primary key is ``(source, external_id)`` so a provider ID resolves in one
    index lookup during import. ``confidence`` below 1.0 means the mapping came
    from fuzzy name matching and is pending review — an unmapped player is a
    silently missing roster slot, so these surface in a reconciliation report
    rather than being dropped (FR-1.7).
    """

    __tablename__ = "player_external_ids"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE", name="fk_pxid_player"),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, server_default=text("1.0"))
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (Index("idx_pxid_player", "player_id"),)


class InjuryReport(Base):
    """Weekly injury designation and practice participation. RFC 11.5."""

    __tablename__ = "injury_reports"

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE", name="fk_injury_player"),
        primary_key=True,
    )
    season: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    designation: Mapped[str | None] = mapped_column(Text)
    practice_status: Mapped[str | None] = mapped_column(Text)
    body_part: Mapped[str | None] = mapped_column(Text)
    reported_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_injury_season_week", "season", "week"),)


__all__ = [
    "InjuryReport",
    "NflTeam",
    "Player",
    "PlayerExternalId",
]
