"""NFL schedule and per-game market/environment context. RFC 11.3 / 11.5."""

from __future__ import annotations

import datetime as dt
import decimal

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Game(Base):
    """One NFL game. ``nflverse_id`` (e.g. ``2025_08_KC_LV``) is the natural key."""

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    nflverse_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    season: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    season_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'REG'"))
    home_team_id: Mapped[int] = mapped_column(
        SmallInteger,
        ForeignKey("nfl_teams.id", name="fk_games_home_team"),
        nullable=False,
    )
    away_team_id: Mapped[int] = mapped_column(
        SmallInteger,
        ForeignKey("nfl_teams.id", name="fk_games_away_team"),
        nullable=False,
    )
    kickoff_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    home_score: Mapped[int | None] = mapped_column(SmallInteger)
    away_score: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'SCHEDULED'"))

    __table_args__ = (
        CheckConstraint("week BETWEEN 1 AND 22", name="ck_games_week_range"),
        CheckConstraint("home_team_id <> away_team_id", name="ck_distinct_teams"),
        Index("idx_games_season_week", "season", "week"),
        Index("idx_games_kickoff", "kickoff_at"),
    )


class GameOdds(Base):
    """Betting lines per game per book.

    Implied team totals are the model-facing features; spread and total are
    kept so they can be recomputed if the derivation changes.
    """

    __tablename__ = "game_odds"

    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE", name="fk_odds_game"),
        primary_key=True,
    )
    book: Mapped[str] = mapped_column(Text, primary_key=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    spread: Mapped[decimal.Decimal | None] = mapped_column(Numeric(5, 1))
    total: Mapped[decimal.Decimal | None] = mapped_column(Numeric(5, 1))
    home_implied_total: Mapped[decimal.Decimal | None] = mapped_column(Numeric(5, 2))
    away_implied_total: Mapped[decimal.Decimal | None] = mapped_column(Numeric(5, 2))

    __table_args__ = (Index("idx_odds_game_fetched", "game_id", "fetched_at"),)


class GameWeather(Base):
    """Stadium forecast, keyed by game and forecast issue time.

    ``forecast_at`` is part of the primary key so every fetch is retained rather
    than overwritten. The point-in-time feature builder selects the latest
    forecast issued *before* its ``as_of`` boundary; overwriting in place would
    leak a Sunday-morning forecast into a Wednesday prediction (RFC 14.1).
    """

    __tablename__ = "game_weather"

    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE", name="fk_weather_game"),
        primary_key=True,
    )
    forecast_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    temp_f: Mapped[int | None] = mapped_column(SmallInteger)
    wind_mph: Mapped[int | None] = mapped_column(SmallInteger)
    precip_prob: Mapped[float | None] = mapped_column(REAL)
    is_dome: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


__all__ = ["Game", "GameOdds", "GameWeather"]
