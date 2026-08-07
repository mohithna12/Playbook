"""Model registry and prediction tables. RFC 11.4 / 11.5."""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import POSITION, Position

QUANTILE_ORDER_CHECK = (
    "proj_p10 <= proj_p25 AND proj_p25 <= proj_p50 "
    "AND proj_p50 <= proj_p75 AND proj_p75 <= proj_p90"
)


class ModelVersion(Base):
    """One trained artifact. The registry is S3 for bytes, this table for metadata.

    A partial unique index guarantees at most one ``ACTIVE`` row per ``name``,
    which removes an entire class of "which model actually served this?"
    incidents (RFC 11.5).
    """

    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[Position] = mapped_column(POSITION, nullable=False)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False)
    feature_set_version: Mapped[str] = mapped_column(Text, nullable=False)
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    training_window: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    promoted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_model"),
        Index(
            "uq_active_model",
            "name",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class Prediction(Base):
    """Current projection for one (player, week, model_version). Upserted in place.

    Stat-space predictions live in ``pred_stats``; the ``proj_*`` columns are a
    half-PPR summary used only for ranking and UI defaults. League-specific
    points are derived at read time from the league's scoring rules.

    The quantile-ordering CHECK is load-bearing, not cosmetic: independently
    trained quantile heads do cross, and a crossed quantile becomes a negative
    variance inside the simulation. Failing the write turns a subtle
    statistical bug into a loud error (RFC 11.4).
    """

    __tablename__ = "predictions"
    __table_args__ = (
        CheckConstraint(QUANTILE_ORDER_CHECK, name="ck_quantile_order"),
        Index(
            "idx_pred_lookup",
            "season",
            "week",
            "player_id",
            postgresql_include=["proj_points_mean", "proj_points_sd"],
        ),
        Index("idx_pred_model", "model_version_id", text("generated_at DESC")),
        {"postgresql_partition_by": "RANGE (season)"},
    )

    # Postgres 16 does not allow identity columns on partitioned tables, so the
    # surrogate key uses an explicit sequence created in the migration.
    id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("nextval('predictions_id_seq')")
    )
    season: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", name="fk_pred_player"), primary_key=True
    )
    week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    model_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_versions.id", name="fk_pred_model_version"),
        primary_key=True,
    )
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    pred_stats: Mapped[dict[str, float]] = mapped_column(JSONB, nullable=False)
    proj_points_mean: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_points_sd: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p10: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p25: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p50: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p75: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p90: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    play_probability: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("1.0")
    )
    data_quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    feature_snapshot_id: Mapped[int | None] = mapped_column(BigInteger)


class PredictionHistory(Base):
    """Append-only log of every projection revision within a week.

    Same shape as ``predictions`` but never upserted, so "how did this
    projection move after Friday's injury report" stays answerable. Splitting
    it out is what keeps the hot read path a single-row lookup (RFC 11.4).
    Retention: one season.
    """

    __tablename__ = "prediction_history"
    __table_args__ = (
        CheckConstraint(QUANTILE_ORDER_CHECK, name="ck_hist_quantile_order"),
        Index("idx_predhist_lookup", "season", "week", "player_id", "generated_at"),
        {"postgresql_partition_by": "RANGE (season)"},
    )

    season: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        server_default=text("nextval('prediction_history_id_seq')"),
    )
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", name="fk_predhist_player"), nullable=False
    )
    week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    model_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_versions.id", name="fk_predhist_model_version"),
        nullable=False,
    )
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    pred_stats: Mapped[dict[str, float]] = mapped_column(JSONB, nullable=False)
    proj_points_mean: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_points_sd: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p10: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p25: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p50: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p75: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    proj_p90: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    play_probability: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("1.0")
    )
    data_quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


__all__ = ["QUANTILE_ORDER_CHECK", "ModelVersion", "Prediction", "PredictionHistory"]
