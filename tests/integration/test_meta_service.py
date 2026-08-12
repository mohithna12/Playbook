"""Deriving the current NFL season and week.

Every screen defaults its week selector to this, so getting it wrong is
visible everywhere at once. The subtlety: a season's whole schedule is
ingested before week 1, so "current" cannot mean "the latest week with rows".
It is the earliest week that still has a game left to play.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pytest

from app.models.game import Game
from app.services.meta import MetaService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SEASON = 2026


async def nfl_team_ids(session: AsyncSession) -> tuple[int, int]:
    """Two team ids, created if the table is empty.

    ``games`` has FKs to ``nfl_teams``, and the schema fixtures run migrations
    but not the seed script. Creating the rows here rather than skipping keeps
    the test meaningful on any database -- a skipped test that looks green is
    worse than no test.
    """
    from sqlalchemy import select

    from app.models.player import NflTeam

    result = await session.execute(select(NflTeam.id).order_by(NflTeam.id).limit(2))
    ids = list(result.scalars().all())
    if len(ids) >= 2:
        return ids[0], ids[1]

    session.add_all(
        [
            NflTeam(abbr="TS1", full_name="Test Team One", conference="AFC", division="AFC East"),
            NflTeam(abbr="TS2", full_name="Test Team Two", conference="NFC", division="NFC East"),
        ]
    )
    await session.flush()
    result = await session.execute(
        select(NflTeam.id).where(NflTeam.abbr.in_(("TS1", "TS2"))).order_by(NflTeam.id)
    )
    created = list(result.scalars().all())
    return created[0], created[1]


async def add_game(
    session: AsyncSession,
    *,
    week: int,
    status: str,
    kickoff: dt.datetime,
    season: int = SEASON,
    season_type: str = "REG",
) -> None:
    home, away = await nfl_team_ids(session)
    session.add(
        Game(
            nflverse_id=f"{season}_{week:02d}_{status}_{kickoff.timestamp()}",
            season=season,
            week=week,
            season_type=season_type,
            home_team_id=home,
            away_team_id=away,
            kickoff_at=kickoff,
            status=status,
        )
    )
    await session.flush()


@pytest.fixture(autouse=True)
def no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the cache so each test sees its own fixture data.

    The service caches nfl-state for 60s, which is right in production and
    would make these tests read each other's results.
    """
    from app.core import cache

    async def _miss(_key: str) -> None:
        return None

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(cache, "get_json", _miss)
    monkeypatch.setattr(cache, "set_json", _noop)


class TestCurrentWeek:
    async def test_the_current_week_is_the_earliest_unfinished_one(
        self, session: AsyncSession
    ) -> None:
        """Not the latest week with rows -- the whole season is loaded up front."""
        now = dt.datetime.now(dt.UTC)
        await add_game(session, week=1, status="FINAL", kickoff=now - dt.timedelta(days=14))
        await add_game(session, week=2, status="FINAL", kickoff=now - dt.timedelta(days=7))
        await add_game(session, week=3, status="SCHEDULED", kickoff=now + dt.timedelta(days=1))
        await add_game(session, week=4, status="SCHEDULED", kickoff=now + dt.timedelta(days=8))
        await add_game(session, week=18, status="SCHEDULED", kickoff=now + dt.timedelta(days=100))

        state = await MetaService(session).nfl_state()

        assert state.season == SEASON
        assert state.week == 3

    async def test_a_week_in_progress_is_still_the_current_week(
        self, session: AsyncSession
    ) -> None:
        """Sunday afternoon: some games done, one live. The week has not moved on."""
        now = dt.datetime.now(dt.UTC)
        await add_game(session, week=5, status="FINAL", kickoff=now - dt.timedelta(hours=6))
        await add_game(session, week=5, status="IN_PROGRESS", kickoff=now - dt.timedelta(hours=1))
        await add_game(session, week=6, status="SCHEDULED", kickoff=now + dt.timedelta(days=7))

        state = await MetaService(session).nfl_state()

        assert state.week == 5

    async def test_lock_at_is_the_first_kickoff_of_the_week(self, session: AsyncSession) -> None:
        """Lineups lock per game; the first kickoff is the one that matters first."""
        now = dt.datetime.now(dt.UTC)
        first = now + dt.timedelta(days=1)
        await add_game(session, week=7, status="SCHEDULED", kickoff=now + dt.timedelta(days=3))
        await add_game(session, week=7, status="SCHEDULED", kickoff=first)

        state = await MetaService(session).nfl_state()

        assert state.week == 7
        assert state.lock_at is not None
        assert abs((state.lock_at - first).total_seconds()) < 1
        assert state.next_kickoff_at == state.lock_at


class TestFallbacks:
    async def test_an_empty_schedule_reports_unknown_rather_than_failing(
        self, session: AsyncSession
    ) -> None:
        """Before the first ingest there is nothing to report, and that is fine."""
        state = await MetaService(session).nfl_state()

        assert state.season == 0
        assert state.week == 0
        assert state.season_type == "UNKNOWN"

    async def test_a_finished_season_falls_back_to_the_last_known_week(
        self, session: AsyncSession
    ) -> None:
        """After the Super Bowl every game is FINAL; clients still need a default."""
        now = dt.datetime.now(dt.UTC)
        await add_game(session, week=21, status="FINAL", kickoff=now - dt.timedelta(days=14))
        await add_game(session, week=22, status="FINAL", kickoff=now - dt.timedelta(days=7))

        state = await MetaService(session).nfl_state()

        assert state.season == SEASON
        assert state.week == 22
        assert state.next_kickoff_at is None


class TestActiveModels:
    async def test_no_models_returns_an_empty_list(self, session: AsyncSession) -> None:
        assert await MetaService(session).active_models() == []

    async def test_only_active_models_are_listed(self, session: AsyncSession) -> None:
        """Which model served a projection is provenance; a retired one is noise."""
        from app.models.prediction import ModelVersion

        common = {
            "position": "RB",
            "algorithm": "xgboost",
            "artifact_uri": "s3://models/x",
            "feature_set_version": "v1",
            "hyperparameters": {},
            "training_window": {},
        }
        session.add_all(
            [
                ModelVersion(
                    name="rb_points",
                    version="3",
                    status="ACTIVE",
                    metrics={"mae": 4.2, "note": "text"},
                    **common,
                ),
                ModelVersion(
                    name="rb_points_old", version="2", status="ARCHIVED", metrics={}, **common
                ),
            ]
        )
        await session.flush()

        models = await MetaService(session).active_models()

        assert [m.name for m in models] == ["rb_points"]
        # Non-numeric metric values are dropped rather than crashing the response.
        assert models[0].metrics == {"mae": 4.2}
