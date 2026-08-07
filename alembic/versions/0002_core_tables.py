"""Core operational tables: dimensions, schedule, leagues, rosters.

Revision ID: 0002_core
Revises: 0001_extensions
Create Date: 2026-08-05

Table order follows FK dependency order. Indexes are created non-concurrently
because every table here is brand new and empty — ``CREATE INDEX CONCURRENTLY``
cannot run inside a transaction and buys nothing against zero rows. Index
creation on a *populated* table must use CONCURRENTLY in its own migration
(see ``docs/architecture/decisions/0002-migration-safety-rules.md``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_core"
down_revision = "0001_extensions"
branch_labels = None
depends_on = None

LEAGUE_PROVIDER = postgresql.ENUM(name="league_provider", create_type=False)
SYNC_STATUS = postgresql.ENUM(name="sync_status", create_type=False)
POSITION = postgresql.ENUM(name="player_position", create_type=False)

TABLES_IN_CREATE_ORDER = (
    "nfl_teams",
    "users",
    "players",
    "player_external_ids",
    "injury_reports",
    "games",
    "game_odds",
    "game_weather",
    "leagues",
    "teams",
    "league_memberships",
    "roster_entries",
    "lineups",
    "matchups",
    "transactions",
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    # ---------------------------------------------------------------- teams
    op.create_table(
        "nfl_teams",
        sa.Column("id", sa.SmallInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("abbr", sa.Text, nullable=False, unique=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("conference", sa.CHAR(3), nullable=False),
        sa.Column("division", sa.Text, nullable=False),
        sa.Column("stadium_lat", sa.REAL),
        sa.Column("stadium_lon", sa.REAL),
        sa.Column("is_dome", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )

    # ---------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column("auth_subject", sa.Text, nullable=False, unique=True),
        sa.Column("email", postgresql.CITEXT, nullable=False, unique=True),
        sa.Column("display_name", sa.Text),
        sa.Column(
            "timezone", sa.Text, nullable=False, server_default=sa.text("'America/New_York'")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "idx_users_auth_subject",
        "users",
        ["auth_subject"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # -------------------------------------------------------------- players
    op.create_table(
        "players",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("gsis_id", sa.Text, unique=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("position", POSITION, nullable=False),
        sa.Column(
            "nfl_team_id",
            sa.SmallInteger,
            sa.ForeignKey("nfl_teams.id", name="fk_players_nfl_team"),
        ),
        sa.Column("jersey_number", sa.SmallInteger),
        sa.Column("birth_date", sa.Date),
        sa.Column("height_in", sa.SmallInteger),
        sa.Column("weight_lb", sa.SmallInteger),
        sa.Column("rookie_year", sa.SmallInteger),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("depth_chart_order", sa.SmallInteger),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "idx_players_pos_team",
        "players",
        ["position", "nfl_team_id"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.execute("CREATE INDEX idx_players_name_trgm ON players USING gin (full_name gin_trgm_ops)")

    op.create_table(
        "player_external_ids",
        sa.Column("source", sa.Text, primary_key=True),
        sa.Column("external_id", sa.Text, primary_key=True),
        sa.Column(
            "player_id",
            sa.BigInteger,
            sa.ForeignKey("players.id", ondelete="CASCADE", name="fk_pxid_player"),
            nullable=False,
        ),
        sa.Column("confidence", sa.REAL, nullable=False, server_default=sa.text("1.0")),
        sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("idx_pxid_player", "player_external_ids", ["player_id"])

    op.create_table(
        "injury_reports",
        sa.Column(
            "player_id",
            sa.BigInteger,
            sa.ForeignKey("players.id", ondelete="CASCADE", name="fk_injury_player"),
            primary_key=True,
        ),
        sa.Column("season", sa.SmallInteger, primary_key=True),
        sa.Column("week", sa.SmallInteger, primary_key=True),
        sa.Column("designation", sa.Text),
        sa.Column("practice_status", sa.Text),
        sa.Column("body_part", sa.Text),
        sa.Column(
            "reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_injury_season_week", "injury_reports", ["season", "week"])

    # ---------------------------------------------------------------- games
    op.create_table(
        "games",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("nflverse_id", sa.Text, nullable=False, unique=True),
        sa.Column("season", sa.SmallInteger, nullable=False),
        sa.Column("week", sa.SmallInteger, nullable=False),
        sa.Column("season_type", sa.Text, nullable=False, server_default=sa.text("'REG'")),
        sa.Column(
            "home_team_id",
            sa.SmallInteger,
            sa.ForeignKey("nfl_teams.id", name="fk_games_home_team"),
            nullable=False,
        ),
        sa.Column(
            "away_team_id",
            sa.SmallInteger,
            sa.ForeignKey("nfl_teams.id", name="fk_games_away_team"),
            nullable=False,
        ),
        sa.Column("kickoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("home_score", sa.SmallInteger),
        sa.Column("away_score", sa.SmallInteger),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'SCHEDULED'")),
        sa.CheckConstraint("week BETWEEN 1 AND 22", name="ck_games_week_range"),
        sa.CheckConstraint("home_team_id <> away_team_id", name="ck_distinct_teams"),
    )
    op.create_index("idx_games_season_week", "games", ["season", "week"])
    op.create_index("idx_games_kickoff", "games", ["kickoff_at"])

    op.create_table(
        "game_odds",
        sa.Column(
            "game_id",
            sa.BigInteger,
            sa.ForeignKey("games.id", ondelete="CASCADE", name="fk_odds_game"),
            primary_key=True,
        ),
        sa.Column("book", sa.Text, primary_key=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), primary_key=True, server_default=sa.func.now()
        ),
        sa.Column("spread", sa.Numeric(5, 1)),
        sa.Column("total", sa.Numeric(5, 1)),
        sa.Column("home_implied_total", sa.Numeric(5, 2)),
        sa.Column("away_implied_total", sa.Numeric(5, 2)),
    )
    op.create_index("idx_odds_game_fetched", "game_odds", ["game_id", "fetched_at"])

    op.create_table(
        "game_weather",
        sa.Column(
            "game_id",
            sa.BigInteger,
            sa.ForeignKey("games.id", ondelete="CASCADE", name="fk_weather_game"),
            primary_key=True,
        ),
        sa.Column("forecast_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("temp_f", sa.SmallInteger),
        sa.Column("wind_mph", sa.SmallInteger),
        sa.Column("precip_prob", sa.REAL),
        sa.Column("is_dome", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )

    # -------------------------------------------------------------- leagues
    op.create_table(
        "leagues",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column("provider", LEAGUE_PROVIDER, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("season", sa.SmallInteger, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("total_teams", sa.SmallInteger, nullable=False),
        sa.Column("scoring_rules", postgresql.JSONB, nullable=False),
        sa.Column("roster_positions", postgresql.JSONB, nullable=False),
        sa.Column("playoff_config", postgresql.JSONB, nullable=False),
        sa.Column("waiver_config", postgresql.JSONB),
        sa.Column("settings_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("sync_status", SYNC_STATUS, nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("total_teams BETWEEN 2 AND 32", name="ck_leagues_total_teams_range"),
        sa.UniqueConstraint("provider", "external_id", "season", name="uq_league_provider_ext"),
    )
    op.create_index(
        "idx_leagues_sync",
        "leagues",
        ["sync_status", "last_synced_at"],
        postgresql_where=sa.text("sync_status IN ('ACTIVE','PARTIAL')"),
    )
    op.execute(
        "CREATE INDEX idx_leagues_scoring_gin ON leagues USING gin (scoring_rules jsonb_path_ops)"
    )

    op.create_table(
        "teams",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_teams_league"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("owner_name", sa.Text),
        sa.Column("wins", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("losses", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("ties", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("points_for", sa.Numeric(8, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("points_against", sa.Numeric(8, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("waiver_position", sa.SmallInteger),
        sa.Column("faab_remaining", sa.Integer),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("league_id", "external_id", name="uq_team_league_ext"),
    )
    op.create_index("idx_teams_league", "teams", ["league_id"])

    op.create_table(
        "league_memberships",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_membership_user"),
            primary_key=True,
        ),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_membership_league"),
            primary_key=True,
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="SET NULL", name="fk_membership_team"),
        ),
        sa.Column("role", sa.Text, nullable=False, server_default=sa.text("'OWNER'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_memberships_league", "league_memberships", ["league_id"])
    op.create_index("idx_memberships_team", "league_memberships", ["team_id"])

    op.create_table(
        "roster_entries",
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE", name="fk_roster_team"),
            primary_key=True,
        ),
        sa.Column(
            "player_id",
            sa.BigInteger,
            sa.ForeignKey("players.id", name="fk_roster_player"),
            primary_key=True,
        ),
        sa.Column("acquired_at", sa.DateTime(timezone=True)),
        sa.Column("acquired_via", sa.Text),
    )
    op.create_index("idx_roster_player", "roster_entries", ["player_id"])

    op.create_table(
        "lineups",
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE", name="fk_lineups_team"),
            primary_key=True,
        ),
        sa.Column("week", sa.SmallInteger, primary_key=True),
        sa.Column("source", sa.Text, primary_key=True),
        sa.Column("slots", postgresql.JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "matchups",
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_matchups_league"),
            primary_key=True,
        ),
        sa.Column("week", sa.SmallInteger, primary_key=True),
        sa.Column("matchup_index", sa.SmallInteger, primary_key=True),
        sa.Column(
            "home_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", name="fk_matchups_home_team"),
            nullable=False,
        ),
        sa.Column(
            "away_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", name="fk_matchups_away_team"),
        ),
        sa.Column("home_points", sa.Numeric(7, 2)),
        sa.Column("away_points", sa.Numeric(7, 2)),
        sa.Column("is_final", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("idx_matchups_team", "matchups", ["home_team_id", "week"])
    op.create_index("idx_matchups_away_team", "matchups", ["away_team_id"])

    op.create_table(
        "transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_txn_league"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("week", sa.SmallInteger, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.UniqueConstraint("league_id", "external_id", name="uq_txn"),
    )
    op.execute("CREATE INDEX idx_txn_league_week ON transactions (league_id, week DESC)")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    for table in reversed(TABLES_IN_CREATE_ORDER):
        op.drop_table(table)
