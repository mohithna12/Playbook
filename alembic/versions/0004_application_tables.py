"""Application tables: recommendations, trades, simulations, jobs, explanations.

Revision ID: 0004_application
Revises: 0003_timeseries
Create Date: 2026-08-05

Two deviations from RFC Section 11.5, both forced by Postgres semantics:

1. ``uq_sim`` is a unique *index* on ``COALESCE(scenario::text, '')`` rather
   than a table constraint on ``(scenario::text)``. Constraints cannot contain
   expressions, and NULLs compare distinct — without the COALESCE, two baseline
   runs (``scenario IS NULL``) with identical state, seed, and n_sims would
   both insert, defeating the memoization the constraint exists to provide.

2. ``idx_recs_expiry`` has no ``WHERE expires_at > now()`` predicate. Index
   predicates must be immutable; ``now()`` is not. The unfiltered index serves
   the expiry sweep just as well at this row count.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_application"
down_revision = "0003_timeseries"
branch_labels = None
depends_on = None

RECOMMENDATION_KIND = postgresql.ENUM(name="recommendation_kind", create_type=False)
JOB_STATUS = postgresql.ENUM(name="job_status", create_type=False)

TABLES_IN_CREATE_ORDER = (
    "recommendations",
    "trade_analyses",
    "simulation_results",
    "jobs",
    "sync_errors",
    "explanations",
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    op.create_table(
        "recommendations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE", name="fk_recs_team"),
            nullable=False,
        ),
        sa.Column("kind", RECOMMENDATION_KIND, nullable=False),
        sa.Column("week", sa.SmallInteger),
        sa.Column(
            "model_version_id",
            sa.Integer,
            sa.ForeignKey("model_versions.id", name="fk_recs_model_version"),
        ),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("confidence", sa.REAL),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_recs_confidence_range"),
    )
    op.execute(
        "CREATE INDEX idx_recs_team_kind ON recommendations "
        "(team_id, kind, week DESC, generated_at DESC)"
    )
    op.create_index("idx_recs_expiry", "recommendations", ["expires_at"])
    op.create_index("idx_recs_model_version", "recommendations", ["model_version_id"])

    op.create_table(
        "trade_analyses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_trades_league"),
            nullable=False,
        ),
        sa.Column(
            "proposing_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", name="fk_trades_proposing_team"),
            nullable=False,
        ),
        sa.Column(
            "receiving_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", name="fk_trades_receiving_team"),
        ),
        sa.Column("players_out", postgresql.ARRAY(sa.BigInteger), nullable=False),
        sa.Column("players_in", postgresql.ARRAY(sa.BigInteger), nullable=False),
        sa.Column("score", sa.Numeric(5, 2), nullable=False),
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("breakdown", postgresql.JSONB, nullable=False),
        sa.Column("champ_prob_delta", sa.REAL),
        sa.Column(
            "model_version_id",
            sa.Integer,
            sa.ForeignKey("model_versions.id", name="fk_trades_model_version"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("score BETWEEN -100 AND 100", name="ck_trades_score_range"),
    )
    op.execute(
        "CREATE INDEX idx_trades_team ON trade_analyses (proposing_team_id, created_at DESC)"
    )
    op.create_index("idx_trades_league", "trade_analyses", ["league_id"])

    op.create_table(
        "simulation_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_sim_league"),
            nullable=False,
        ),
        sa.Column("as_of_week", sa.SmallInteger, nullable=False),
        sa.Column("n_simulations", sa.Integer, nullable=False),
        sa.Column("seed", sa.BigInteger, nullable=False),
        sa.Column("league_state_hash", sa.Text, nullable=False),
        sa.Column("scenario", postgresql.JSONB),
        sa.Column("results", postgresql.JSONB, nullable=False),
        sa.Column("runtime_ms", sa.Integer, nullable=False),
        sa.Column(
            "model_version_id",
            sa.Integer,
            sa.ForeignKey("model_versions.id", name="fk_sim_model_version"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sim ON simulation_results "
        "(league_state_hash, n_simulations, seed, COALESCE(scenario::text, ''))"
    )
    op.execute(
        "CREATE INDEX idx_sim_league ON simulation_results "
        "(league_id, as_of_week DESC, created_at DESC)"
    )
    op.create_index("idx_sim_model_version", "simulation_results", ["model_version_id"])

    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_jobs_user"),
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", JOB_STATUS, nullable=False, server_default=sa.text("'QUEUED'")),
        sa.Column("idempotency_key", sa.Text),
        sa.Column("params", postgresql.JSONB, nullable=False),
        sa.Column("progress_pct", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("progress_step", sa.Text),
        sa.Column("result_ref", postgresql.JSONB),
        sa.Column("error", postgresql.JSONB),
        sa.Column("attempts", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "kind", "idempotency_key", name="uq_job_idem"),
    )
    # The reaper scans exactly this predicate every 30s.
    op.create_index(
        "idx_jobs_reaper",
        "jobs",
        ["status", "heartbeat_at"],
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"),
    )
    op.execute("CREATE INDEX idx_jobs_user ON jobs (user_id, created_at DESC)")

    op.create_table(
        "sync_errors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_sync_errors_league"),
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL", name="fk_sync_errors_job"),
        ),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("detail", postgresql.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("CREATE INDEX idx_sync_errors_league ON sync_errors (league_id, created_at DESC)")
    op.create_index("idx_sync_errors_job", "sync_errors", ["job_id"])

    op.create_table(
        "explanations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.uuidv7()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_explanations_user"),
        ),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id", ondelete="CASCADE", name="fk_explanations_league"),
        ),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("context_hash", sa.Text, nullable=False),
        sa.Column("response", postgresql.JSONB, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("tokens_in", sa.Integer),
        sa.Column("tokens_out", sa.Integer),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("validation_status", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        "CREATE INDEX idx_explanations_context ON explanations (context_hash, created_at DESC)"
    )
    op.execute("CREATE INDEX idx_explanations_user ON explanations (user_id, created_at DESC)")
    op.create_index("idx_explanations_league", "explanations", ["league_id"])


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    for table in reversed(TABLES_IN_CREATE_ORDER):
        op.drop_table(table)
