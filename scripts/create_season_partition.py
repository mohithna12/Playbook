#!/usr/bin/env python
"""Create season partitions for the four partitioned tables.

Run every August before the new season's data starts arriving. No DEFAULT
partition exists by design (see migration ``0003``), so a missing partition
means inserts fail loudly rather than landing somewhere they cannot later be
detached from.

Idempotent: uses ``CREATE TABLE IF NOT EXISTS``, so a re-run is a no-op.

Usage:
    uv run scripts/create_season_partition.py 2027
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from app.core.db import get_engine

PARTITIONED_TABLES = (
    "weekly_stats",
    "feature_store",
    "predictions",
    "prediction_history",
)


async def create_partitions(season: int) -> list[str]:
    """Create one partition per partitioned table. Returns the names created."""
    created: list[str] = []
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("SET lock_timeout = '3s'"))
        for table in PARTITIONED_TABLES:
            name = f"{table}_{season}"
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
                    f"FOR VALUES FROM ({season}) TO ({season + 1})"
                )
            )
            created.append(name)
    return created


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("season", type=int, help="Season year, e.g. 2027")
    args = parser.parse_args()

    created = await create_partitions(args.season)
    for name in created:
        print(f"ensured partition {name}")
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
