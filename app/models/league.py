"""Fantasy league state: leagues, teams, rosters, lineups, matchups. RFC 11.3/11.5."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import LEAGUE_PROVIDER, SYNC_STATUS, LeagueProvider, SyncStatus


class League(Base, TimestampMixin):
    """An imported fantasy league.

    ``scoring_rules``, ``roster_positions``, and ``playoff_config`` are JSONB
    because their shape is provider-defined. They are validated on write by
    Pydantic and carry a ``schema_version`` (RFC 11.3). Scoring is stored as an
    evaluable ruleset, never a preset name — half-PPR and TE-premium change
    every downstream number (FR-1.6).
    """

    __tablename__ = "leagues"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    provider: Mapped[LeagueProvider] = mapped_column(LEAGUE_PROVIDER, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    season: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    total_teams: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    scoring_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    roster_positions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    playoff_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    waiver_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    sync_status: Mapped[SyncStatus] = mapped_column(
        SYNC_STATUS, nullable=False, server_default=text("'PENDING'")
    )
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("total_teams BETWEEN 2 AND 32", name="ck_leagues_total_teams_range"),
        UniqueConstraint("provider", "external_id", "season", name="uq_league_provider_ext"),
        Index(
            "idx_leagues_sync",
            "sync_status",
            "last_synced_at",
            postgresql_where=text("sync_status IN ('ACTIVE','PARTIAL')"),
        ),
        Index(
            "idx_leagues_scoring_gin",
            "scoring_rules",
            postgresql_using="gin",
            postgresql_ops={"scoring_rules": "jsonb_path_ops"},
        ),
    )


class Team(Base):
    """A fantasy team within a league."""

    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_teams_league"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_name: Mapped[str | None] = mapped_column(Text)
    wins: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    losses: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    ties: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    points_for: Mapped[decimal.Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, server_default=text("0")
    )
    points_against: Mapped[decimal.Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, server_default=text("0")
    )
    waiver_position: Mapped[int | None] = mapped_column(SmallInteger)
    faab_remaining: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("league_id", "external_id", name="uq_team_league_ext"),
        Index("idx_teams_league", "league_id"),
    )


class LeagueMembership(Base):
    """Many-to-many between users and leagues; ``team_id`` is the user's own team."""

    __tablename__ = "league_memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_membership_user"),
        primary_key=True,
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_membership_league"),
        primary_key=True,
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="SET NULL", name="fk_membership_team"),
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'OWNER'"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_memberships_league", "league_id"),
        Index("idx_memberships_team", "team_id"),
    )


class RosterEntry(Base):
    """A player currently rostered by a fantasy team."""

    __tablename__ = "roster_entries"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_roster_team"),
        primary_key=True,
    )
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", name="fk_roster_player"), primary_key=True
    )
    acquired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acquired_via: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_roster_player", "player_id"),)


class Lineup(Base):
    """A weekly slot assignment.

    ``source`` is part of the key so the provider's actual lineup and our
    recommended lineup coexist — FR-3.6 requires explaining every *change* from
    what the user currently has set, which needs both.
    """

    __tablename__ = "lineups"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_lineups_team"),
        primary_key=True,
    )
    week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    slots: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Matchup(Base):
    """A head-to-head pairing. ``away_team_id`` is NULL for a bye."""

    __tablename__ = "matchups"

    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_matchups_league"),
        primary_key=True,
    )
    week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    matchup_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    home_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", name="fk_matchups_home_team"),
        nullable=False,
    )
    away_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", name="fk_matchups_away_team")
    )
    home_points: Mapped[decimal.Decimal | None] = mapped_column(Numeric(7, 2))
    away_points: Mapped[decimal.Decimal | None] = mapped_column(Numeric(7, 2))
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        Index("idx_matchups_team", "home_team_id", "week"),
        Index("idx_matchups_away_team", "away_team_id"),
    )


class Transaction(Base):
    """Provider transaction log entry (trade, waiver, free agent, commissioner)."""

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE", name="fk_txn_league"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("league_id", "external_id", name="uq_txn"),
        Index("idx_txn_league_week", "league_id", text("week DESC")),
    )


__all__ = [
    "League",
    "LeagueMembership",
    "Lineup",
    "Matchup",
    "RosterEntry",
    "Team",
    "Transaction",
]
