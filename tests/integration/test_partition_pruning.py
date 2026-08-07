"""Partition pruning actually happens.

Partitioning is only worth its DDL complexity if the planner prunes. These
tests read the plan rather than trusting the DDL: a query that filters on
``season`` must scan one partition, and a query that does not must scan all of
them (which is the loud signal that a caller forgot the partition key).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

pytestmark = pytest.mark.integration

SEASONS = 9  # 2018-2026, created by migration 0003


async def _plan(conn: AsyncConnection, sql: str) -> str:
    result = await conn.execute(text(f"EXPLAIN {sql}"))
    return "\n".join(row[0] for row in result)


async def test_weekly_stats_query_prunes_to_one_partition(
    connection: AsyncConnection,
) -> None:
    plan = await _plan(
        connection,
        "SELECT * FROM weekly_stats WHERE season = 2025 AND player_id = 1 AND week = 8",
    )
    assert "weekly_stats_2025" in plan
    assert "weekly_stats_2024" not in plan


async def test_predictions_lookup_prunes_to_one_partition(
    connection: AsyncConnection,
) -> None:
    plan = await _plan(
        connection,
        "SELECT proj_points_mean FROM predictions "
        "WHERE season = 2026 AND week = 8 AND player_id = 1",
    )
    assert "predictions_2026" in plan
    assert "predictions_2018" not in plan


async def test_feature_store_training_scan_spans_requested_seasons_only(
    connection: AsyncConnection,
) -> None:
    """A backfill over 2023-2024 must not touch 2018."""
    plan = await _plan(
        connection,
        "SELECT * FROM feature_store WHERE season BETWEEN 2023 AND 2024",
    )
    assert "feature_store_2023" in plan
    assert "feature_store_2024" in plan
    assert "feature_store_2018" not in plan


async def test_query_without_season_scans_every_partition(
    connection: AsyncConnection,
) -> None:
    """Documents the cost of forgetting the partition key."""
    plan = await _plan(connection, "SELECT * FROM predictions WHERE player_id = 1")
    scanned = sum(1 for season in range(2018, 2027) if f"predictions_{season}" in plan)
    assert scanned == SEASONS
