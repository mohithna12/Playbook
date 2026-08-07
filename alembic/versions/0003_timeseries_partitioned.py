"""Model registry and season-partitioned time-series tables.

Revision ID: 0003_timeseries
Revises: 0002_core
Create Date: 2026-08-05

``weekly_stats``, ``feature_store``, ``predictions``, and ``prediction_history``
are all ``PARTITION BY RANGE (season)``. Partitions for 2018-2026 are created
here; ``scripts/create_season_partition.py`` creates each new season and is
invoked by an Airflow task every August.

No DEFAULT partition is created, deliberately. A default partition would
silently absorb rows for a season nobody provisioned — and then block attaching
that season's real partition later without a full scan. Failing the insert
loudly is the better trade for a pipeline that runs on a known calendar.

``predictions`` and ``prediction_history`` take their surrogate key from an
explicit sequence rather than an identity column: Postgres 16 rejects identity
columns on partitioned tables (allowed only from 17).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_timeseries"
down_revision = "0002_core"
branch_labels = None
depends_on = None

POSITION = postgresql.ENUM(name="player_position", create_type=False)

FIRST_SEASON = 2018
LAST_SEASON = 2026

PARTITIONED_TABLES = (
    "weekly_stats",
    "feature_store",
    "predictions",
    "prediction_history",
)

QUANTILE_ORDER = (
    "proj_p10 <= proj_p25 AND proj_p25 <= proj_p50 "
    "AND proj_p50 <= proj_p75 AND proj_p75 <= proj_p90"
)


def _projection_columns() -> list[sa.Column]:
    """Columns shared by ``predictions`` and ``prediction_history``."""
    return [
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("pred_stats", postgresql.JSONB, nullable=False),
        sa.Column("proj_points_mean", sa.Numeric(6, 2), nullable=False),
        sa.Column("proj_points_sd", sa.Numeric(6, 2), nullable=False),
        sa.Column("proj_p10", sa.Numeric(6, 2), nullable=False),
        sa.Column("proj_p25", sa.Numeric(6, 2), nullable=False),
        sa.Column("proj_p50", sa.Numeric(6, 2), nullable=False),
        sa.Column("proj_p75", sa.Numeric(6, 2), nullable=False),
        sa.Column("proj_p90", sa.Numeric(6, 2), nullable=False),
        sa.Column("play_probability", sa.REAL, nullable=False, server_default=sa.text("1.0")),
        sa.Column("data_quality", postgresql.JSONB),
    ]


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    # ------------------------------------------------------- model registry
    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer, sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("position", POSITION, nullable=False),
        sa.Column("algorithm", sa.Text, nullable=False),
        sa.Column("artifact_uri", sa.Text, nullable=False),
        sa.Column("feature_set_version", sa.Text, nullable=False),
        sa.Column("hyperparameters", postgresql.JSONB, nullable=False),
        sa.Column("training_window", postgresql.JSONB, nullable=False),
        sa.Column("metrics", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("name", "version", name="uq_model"),
    )
    # At most one ACTIVE model per name -- removes "which model served this?"
    op.create_index(
        "uq_active_model",
        "model_versions",
        ["name"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # --------------------------------------------------------- weekly_stats
    op.create_table(
        "weekly_stats",
        sa.Column("season", sa.SmallInteger, primary_key=True),
        sa.Column(
            "player_id",
            sa.BigInteger,
            sa.ForeignKey("players.id", name="fk_ws_player"),
            primary_key=True,
        ),
        sa.Column("week", sa.SmallInteger, primary_key=True),
        sa.Column(
            "game_id", sa.BigInteger, sa.ForeignKey("games.id", name="fk_ws_game"), nullable=False
        ),
        sa.Column(
            "nfl_team_id",
            sa.SmallInteger,
            sa.ForeignKey("nfl_teams.id", name="fk_ws_team"),
            nullable=False,
        ),
        sa.Column(
            "opponent_team_id",
            sa.SmallInteger,
            sa.ForeignKey("nfl_teams.id", name="fk_ws_opponent"),
            nullable=False,
        ),
        sa.Column("is_home", sa.Boolean, nullable=False),
        sa.Column("offense_snaps", sa.SmallInteger),
        sa.Column("team_offense_snaps", sa.SmallInteger),
        sa.Column("snap_pct", sa.REAL),
        sa.Column("routes_run", sa.SmallInteger),
        sa.Column("team_dropbacks", sa.SmallInteger),
        sa.Column("route_participation", sa.REAL),
        sa.Column("pass_att", sa.SmallInteger),
        sa.Column("pass_cmp", sa.SmallInteger),
        sa.Column("pass_yds", sa.SmallInteger),
        sa.Column("pass_td", sa.SmallInteger),
        sa.Column("pass_int", sa.SmallInteger),
        sa.Column("sacks_taken", sa.SmallInteger),
        sa.Column("rush_att", sa.SmallInteger),
        sa.Column("rush_yds", sa.SmallInteger),
        sa.Column("rush_td", sa.SmallInteger),
        sa.Column("targets", sa.SmallInteger),
        sa.Column("receptions", sa.SmallInteger),
        sa.Column("rec_yds", sa.SmallInteger),
        sa.Column("rec_td", sa.SmallInteger),
        sa.Column("air_yards", sa.SmallInteger),
        sa.Column("yac", sa.SmallInteger),
        sa.Column("rz_targets", sa.SmallInteger),
        sa.Column("rz_carries", sa.SmallInteger),
        sa.Column("inside_5_carries", sa.SmallInteger),
        sa.Column("fumbles_lost", sa.SmallInteger),
        sa.Column("two_pt_conv", sa.SmallInteger),
        sa.Column("fg_made_0_39", sa.SmallInteger),
        sa.Column("fg_made_40_49", sa.SmallInteger),
        sa.Column("fg_made_50p", sa.SmallInteger),
        sa.Column("fg_missed", sa.SmallInteger),
        sa.Column("xp_made", sa.SmallInteger),
        sa.Column("xp_missed", sa.SmallInteger),
        sa.Column("target_share", sa.REAL),
        sa.Column("air_yards_share", sa.REAL),
        sa.Column("wopr", sa.REAL),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("source_version", sa.Text, nullable=False),
        postgresql_partition_by="RANGE (season)",
    )
    op.create_index("idx_ws_player_season", "weekly_stats", ["player_id", "season", "week"])
    op.create_index("idx_ws_game", "weekly_stats", ["game_id"])
    op.create_index("idx_ws_opponent", "weekly_stats", ["opponent_team_id", "season", "week"])

    # -------------------------------------------------------- feature_store
    op.create_table(
        "feature_store",
        sa.Column("season", sa.SmallInteger, primary_key=True),
        sa.Column(
            "player_id",
            sa.BigInteger,
            sa.ForeignKey("players.id", name="fk_fs_player"),
            primary_key=True,
        ),
        sa.Column("week", sa.SmallInteger, primary_key=True),
        sa.Column("feature_set_version", sa.Text, primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", postgresql.JSONB, nullable=False),
        sa.Column("features_vec", postgresql.ARRAY(sa.REAL)),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        postgresql_partition_by="RANGE (season)",
    )
    op.create_index("idx_fs_lookup", "feature_store", ["season", "week", "feature_set_version"])

    # ----------------------------------------------------------- prediction
    op.execute("CREATE SEQUENCE predictions_id_seq AS BIGINT")
    op.create_table(
        "predictions",
        sa.Column(
            "id",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("nextval('predictions_id_seq')"),
        ),
        sa.Column("season", sa.SmallInteger, primary_key=True),
        sa.Column(
            "player_id",
            sa.BigInteger,
            sa.ForeignKey("players.id", name="fk_pred_player"),
            primary_key=True,
        ),
        sa.Column("week", sa.SmallInteger, primary_key=True),
        sa.Column(
            "model_version_id",
            sa.Integer,
            sa.ForeignKey("model_versions.id", name="fk_pred_model_version"),
            primary_key=True,
        ),
        *_projection_columns(),
        sa.Column("feature_snapshot_id", sa.BigInteger),
        # Crossed quantiles become a negative variance downstream. Fail the
        # write instead of poisoning the simulation.
        sa.CheckConstraint(QUANTILE_ORDER, name="ck_quantile_order"),
        postgresql_partition_by="RANGE (season)",
    )
    op.execute("ALTER SEQUENCE predictions_id_seq OWNED BY predictions.id")
    op.create_index(
        "idx_pred_lookup",
        "predictions",
        ["season", "week", "player_id"],
        postgresql_include=["proj_points_mean", "proj_points_sd"],
    )
    op.execute("CREATE INDEX idx_pred_model ON predictions (model_version_id, generated_at DESC)")

    # --------------------------------------------------- prediction_history
    op.execute("CREATE SEQUENCE prediction_history_id_seq AS BIGINT")
    op.create_table(
        "prediction_history",
        sa.Column("season", sa.SmallInteger, primary_key=True),
        sa.Column(
            "id",
            sa.BigInteger,
            primary_key=True,
            server_default=sa.text("nextval('prediction_history_id_seq')"),
        ),
        sa.Column(
            "player_id",
            sa.BigInteger,
            sa.ForeignKey("players.id", name="fk_predhist_player"),
            nullable=False,
        ),
        sa.Column("week", sa.SmallInteger, nullable=False),
        sa.Column(
            "model_version_id",
            sa.Integer,
            sa.ForeignKey("model_versions.id", name="fk_predhist_model_version"),
            nullable=False,
        ),
        *_projection_columns(),
        sa.CheckConstraint(QUANTILE_ORDER, name="ck_hist_quantile_order"),
        postgresql_partition_by="RANGE (season)",
    )
    op.execute("ALTER SEQUENCE prediction_history_id_seq OWNED BY prediction_history.id")
    op.create_index(
        "idx_predhist_lookup",
        "prediction_history",
        ["season", "week", "player_id", "generated_at"],
    )

    # ------------------------------------------------------------ partitions
    for table in PARTITIONED_TABLES:
        for season in range(FIRST_SEASON, LAST_SEASON + 1):
            op.execute(
                f"CREATE TABLE {table}_{season} PARTITION OF {table} "
                f"FOR VALUES FROM ({season}) TO ({season + 1})"
            )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    # Dropping the parent drops its partitions.
    for table in reversed(PARTITIONED_TABLES):
        op.drop_table(table)

    op.execute("DROP SEQUENCE IF EXISTS prediction_history_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS predictions_id_seq")
    op.drop_table("model_versions")
