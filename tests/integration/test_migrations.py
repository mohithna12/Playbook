"""Migrations apply, reverse, and re-apply cleanly, and match the ORM metadata.

A tested downgrade is a hard rule for this project (RFC 11.6): a migration you
cannot reverse is a migration you cannot deploy on a Friday.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.models import Base

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.schema import SchemaItem

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
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
    "model_versions",
    "weekly_stats",
    "feature_store",
    "predictions",
    "prediction_history",
    "recommendations",
    "trade_analyses",
    "simulation_results",
    "jobs",
    "sync_errors",
    "explanations",
}

PARTITIONED_TABLES = ("weekly_stats", "feature_store", "predictions", "prediction_history")
SEASONS = range(2018, 2027)

_PARTITION_RE = re.compile(rf"^({'|'.join(PARTITIONED_TABLES)})_\d{{4}}$")

_TABLE_QUERY = text(
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
)


def _include_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    """Mirror of ``alembic/env.py`` -- season partitions are not ORM tables."""
    return not (type_ == "table" and name is not None and _PARTITION_RE.match(name))


async def _table_names(conn: AsyncConnection) -> set[str]:
    result = await conn.execute(_TABLE_QUERY)
    return {row[0] for row in result}


async def test_upgrade_creates_every_expected_table(connection: AsyncConnection) -> None:
    assert await _table_names(connection) >= EXPECTED_TABLES


async def test_partitions_exist_for_every_season(connection: AsyncConnection) -> None:
    expected = {f"{t}_{s}" for t in PARTITIONED_TABLES for s in SEASONS}
    assert expected <= await _table_names(connection)


async def test_no_default_partition_exists(connection: AsyncConnection) -> None:
    """A default partition would silently absorb an unprovisioned season."""
    result = await connection.execute(
        text("SELECT c.relname FROM pg_class c JOIN pg_partitioned_table p ON p.partdefid = c.oid")
    )
    assert result.first() is None


async def test_orm_metadata_matches_migrated_schema(connection: AsyncConnection) -> None:
    """Autogenerate finds no drift: the ORM and the migrations agree."""

    def _diff(sync_conn: Connection) -> list[object]:
        context = MigrationContext.configure(
            sync_conn,
            opts={"include_object": _include_object, "compare_type": True},
        )
        return compare_metadata(context, Base.metadata)

    diffs = await connection.run_sync(_diff)
    assert diffs == [], f"ORM/schema drift detected: {diffs}"


def test_downgrade_to_base_then_upgrade_again(migrated_url: str, alembic_config: Config) -> None:
    """Every downgrade is exercised, then the whole chain is replayed.

    Synchronous by necessity: ``env.py`` drives migrations with
    ``asyncio.run()``, which cannot be called from inside a running loop.
    """

    async def _remaining_tables() -> set[str]:
        engine = create_async_engine(migrated_url)
        try:
            async with engine.connect() as conn:
                return await _table_names(conn)
        finally:
            await engine.dispose()

    command.downgrade(alembic_config, "base")
    remaining = asyncio.run(_remaining_tables())
    assert remaining == set(), f"downgrade left tables behind: {remaining}"

    command.upgrade(alembic_config, "head")
