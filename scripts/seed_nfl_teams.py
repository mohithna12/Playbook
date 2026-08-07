#!/usr/bin/env python
"""Seed the 32 NFL franchises.

Idempotent: re-running upserts on ``abbr``. Stadium coordinates feed the
weather ingest (Open-Meteo takes lat/lon), and ``is_dome`` short-circuits it
entirely for the nine indoor venues.

Abbreviations follow nflverse conventions (LA = Rams, LAC = Chargers,
WAS = Commanders) because that is what the play-by-play join key uses.

Usage:
    uv run scripts/seed_nfl_teams.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy.dialects.postgresql import insert

from app.core.db import get_engine, get_sessionmaker
from app.models import NflTeam

# (abbr, full_name, conference, division, lat, lon, is_dome)
NFL_TEAMS: list[tuple[str, str, str, str, float, float, bool]] = [
    ("ARI", "Arizona Cardinals", "NFC", "West", 33.5277, -112.2626, True),
    ("ATL", "Atlanta Falcons", "NFC", "South", 33.7554, -84.4008, True),
    ("BAL", "Baltimore Ravens", "AFC", "North", 39.2780, -76.6227, False),
    ("BUF", "Buffalo Bills", "AFC", "East", 42.7738, -78.7870, False),
    ("CAR", "Carolina Panthers", "NFC", "South", 35.2258, -80.8528, False),
    ("CHI", "Chicago Bears", "NFC", "North", 41.8623, -87.6167, False),
    ("CIN", "Cincinnati Bengals", "AFC", "North", 39.0955, -84.5161, False),
    ("CLE", "Cleveland Browns", "AFC", "North", 41.5061, -81.6995, False),
    ("DAL", "Dallas Cowboys", "NFC", "East", 32.7473, -97.0945, True),
    ("DEN", "Denver Broncos", "AFC", "West", 39.7439, -105.0201, False),
    ("DET", "Detroit Lions", "NFC", "North", 42.3400, -83.0456, True),
    ("GB", "Green Bay Packers", "NFC", "North", 44.5013, -88.0622, False),
    ("HOU", "Houston Texans", "AFC", "South", 29.6847, -95.4107, True),
    ("IND", "Indianapolis Colts", "AFC", "South", 39.7601, -86.1639, True),
    ("JAX", "Jacksonville Jaguars", "AFC", "South", 30.3239, -81.6373, False),
    ("KC", "Kansas City Chiefs", "AFC", "West", 39.0489, -94.4839, False),
    ("LA", "Los Angeles Rams", "NFC", "West", 33.9535, -118.3392, True),
    ("LAC", "Los Angeles Chargers", "AFC", "West", 33.9535, -118.3392, True),
    ("LV", "Las Vegas Raiders", "AFC", "West", 36.0909, -115.1833, True),
    ("MIA", "Miami Dolphins", "AFC", "East", 25.9580, -80.2389, False),
    ("MIN", "Minnesota Vikings", "NFC", "North", 44.9738, -93.2578, True),
    ("NE", "New England Patriots", "AFC", "East", 42.0909, -71.2643, False),
    ("NO", "New Orleans Saints", "NFC", "South", 29.9511, -90.0812, True),
    ("NYG", "New York Giants", "NFC", "East", 40.8135, -74.0745, False),
    ("NYJ", "New York Jets", "AFC", "East", 40.8135, -74.0745, False),
    ("PHI", "Philadelphia Eagles", "NFC", "East", 39.9008, -75.1675, False),
    ("PIT", "Pittsburgh Steelers", "AFC", "North", 40.4468, -80.0158, False),
    ("SEA", "Seattle Seahawks", "NFC", "West", 47.5952, -122.3316, False),
    ("SF", "San Francisco 49ers", "NFC", "West", 37.4033, -121.9694, False),
    ("TB", "Tampa Bay Buccaneers", "NFC", "South", 27.9759, -82.5033, False),
    ("TEN", "Tennessee Titans", "AFC", "South", 36.1665, -86.7713, False),
    ("WAS", "Washington Commanders", "NFC", "East", 38.9077, -76.8645, False),
]


async def seed() -> int:
    """Upsert all franchises. Returns the number of rows written."""
    rows = [
        {
            "abbr": abbr,
            "full_name": full_name,
            "conference": conference,
            "division": division,
            "stadium_lat": lat,
            "stadium_lon": lon,
            "is_dome": is_dome,
        }
        for abbr, full_name, conference, division, lat, lon, is_dome in NFL_TEAMS
    ]

    stmt = insert(NflTeam).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[NflTeam.abbr],
        set_={
            "full_name": stmt.excluded.full_name,
            "conference": stmt.excluded.conference,
            "division": stmt.excluded.division,
            "stadium_lat": stmt.excluded.stadium_lat,
            "stadium_lon": stmt.excluded.stadium_lon,
            "is_dome": stmt.excluded.is_dome,
        },
    )

    async with get_sessionmaker()() as session:
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def main() -> None:
    count = await seed()
    print(f"Seeded {count} NFL teams.")
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
