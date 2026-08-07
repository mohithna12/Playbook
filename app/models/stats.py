"""Partitioned time-series tables: raw weekly stats and the feature store.

Both are ``PARTITION BY RANGE (season)``. At ~34K rows/season partitioning buys
nothing for point queries — it is done for training-time partition pruning,
one-statement season archival to S3, and bounded per-partition VACUUM
(RFC 11.4). The cost is that ``season`` must appear in every primary key.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    ARRAY,
    REAL,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WeeklyStats(Base):
    """One row per player per game, raw counting stats only.

    No fantasy points are stored here. Points are derived per-league at read
    time from ``scoring_rules`` — storing them would mean one row per
    (player, week, league), a ~200x write amplification at MVP scale, and would
    make "what is this player worth in a TE-premium league" unanswerable
    (RFC 11.1 principle 2).
    """

    __tablename__ = "weekly_stats"
    __table_args__ = (
        Index("idx_ws_player_season", "player_id", "season", "week"),
        Index("idx_ws_game", "game_id"),
        Index("idx_ws_opponent", "opponent_team_id", "season", "week"),
        {"postgresql_partition_by": "RANGE (season)"},
    )

    season: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", name="fk_ws_player"), primary_key=True
    )
    week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", name="fk_ws_game"), nullable=False)
    nfl_team_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("nfl_teams.id", name="fk_ws_team"), nullable=False
    )
    opponent_team_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("nfl_teams.id", name="fk_ws_opponent"), nullable=False
    )
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # -- Usage / opportunity --
    offense_snaps: Mapped[int | None] = mapped_column(SmallInteger)
    team_offense_snaps: Mapped[int | None] = mapped_column(SmallInteger)
    snap_pct: Mapped[float | None] = mapped_column(REAL)
    routes_run: Mapped[int | None] = mapped_column(SmallInteger)
    team_dropbacks: Mapped[int | None] = mapped_column(SmallInteger)
    route_participation: Mapped[float | None] = mapped_column(REAL)

    # -- Passing --
    pass_att: Mapped[int | None] = mapped_column(SmallInteger)
    pass_cmp: Mapped[int | None] = mapped_column(SmallInteger)
    pass_yds: Mapped[int | None] = mapped_column(SmallInteger)
    pass_td: Mapped[int | None] = mapped_column(SmallInteger)
    pass_int: Mapped[int | None] = mapped_column(SmallInteger)
    sacks_taken: Mapped[int | None] = mapped_column(SmallInteger)

    # -- Rushing --
    rush_att: Mapped[int | None] = mapped_column(SmallInteger)
    rush_yds: Mapped[int | None] = mapped_column(SmallInteger)
    rush_td: Mapped[int | None] = mapped_column(SmallInteger)

    # -- Receiving --
    targets: Mapped[int | None] = mapped_column(SmallInteger)
    receptions: Mapped[int | None] = mapped_column(SmallInteger)
    rec_yds: Mapped[int | None] = mapped_column(SmallInteger)
    rec_td: Mapped[int | None] = mapped_column(SmallInteger)
    air_yards: Mapped[int | None] = mapped_column(SmallInteger)
    yac: Mapped[int | None] = mapped_column(SmallInteger)

    # -- High-leverage volume --
    rz_targets: Mapped[int | None] = mapped_column(SmallInteger)
    rz_carries: Mapped[int | None] = mapped_column(SmallInteger)
    inside_5_carries: Mapped[int | None] = mapped_column(SmallInteger)

    # -- Misc scoring events --
    fumbles_lost: Mapped[int | None] = mapped_column(SmallInteger)
    two_pt_conv: Mapped[int | None] = mapped_column(SmallInteger)

    # -- Kicking --
    fg_made_0_39: Mapped[int | None] = mapped_column(SmallInteger)
    fg_made_40_49: Mapped[int | None] = mapped_column(SmallInteger)
    fg_made_50p: Mapped[int | None] = mapped_column(SmallInteger)
    fg_missed: Mapped[int | None] = mapped_column(SmallInteger)
    xp_made: Mapped[int | None] = mapped_column(SmallInteger)
    xp_missed: Mapped[int | None] = mapped_column(SmallInteger)

    # -- Derived shares (stored because they are expensive to recompute) --
    target_share: Mapped[float | None] = mapped_column(REAL)
    air_yards_share: Mapped[float | None] = mapped_column(REAL)
    wopr: Mapped[float | None] = mapped_column(REAL)

    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source_version: Mapped[str] = mapped_column(Text, nullable=False)


class FeatureStore(Base):
    """Point-in-time-correct model inputs for one (player, week).

    ``as_of`` is the temporal boundary: no value in ``features`` may depend on
    information published after it. This is the schema half of the leakage
    guard; the query half lives in the feature builder (RFC 14.1).

    Both ``features`` (JSONB, debuggable, consumed by the explanation engine)
    and ``features_vec`` (dense REAL[] ordered by the feature-set manifest, fed
    to the model) are stored. The 2x storage cost buys fast inference *and* the
    ability to answer "what was this player's target share when we predicted"
    without reverse-engineering an array index (RFC 11.4).
    """

    __tablename__ = "feature_store"
    __table_args__ = (
        Index("idx_fs_lookup", "season", "week", "feature_set_version"),
        {"postgresql_partition_by": "RANGE (season)"},
    )

    season: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", name="fk_fs_player"), primary_key=True
    )
    week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    feature_set_version: Mapped[str] = mapped_column(Text, primary_key=True)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    features_vec: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["FeatureStore", "WeeklyStats"]
