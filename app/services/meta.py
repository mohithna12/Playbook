"""Season state and model registry reads.

The week-derivation rule lives here rather than in the router: it is a
judgement about the domain (which week is "current" when Thursday's game has
finished and Sunday's has not), and it is about to be needed by the batch
inference job as well.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core import cache
from app.models.game import Game
from app.models.prediction import ModelVersion
from app.schemas.common import ModelVersionSummary, NflState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# A season's full schedule is loaded before week 1, so "current" cannot mean
# "the latest week with rows". It is the earliest week that still has a game
# left to play.
UNFINISHED_STATUSES = ("SCHEDULED", "IN_PROGRESS")

UNKNOWN_STATE = NflState(season=0, week=0, season_type="UNKNOWN")


class MetaService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def nfl_state(self) -> NflState:
        """The current season and week, cached for a minute.

        Read on nearly every page load to seed week selectors, and it changes
        at most once a week -- so a short TTL removes almost all of the query
        volume while keeping the window of staleness after a game ends small.
        """
        key = cache.cache_key("meta", "nfl-state")
        cached = await cache.get_json(key)
        if cached is not None:
            return NflState.model_validate(cached)

        state = await self._compute_nfl_state()
        await cache.set_json(key, state.model_dump(mode="json"), cache.TTL_NFL_STATE)
        return state

    async def _compute_nfl_state(self) -> NflState:
        result = await self._session.execute(
            select(
                Game.season,
                Game.week,
                Game.season_type,
                func.min(Game.kickoff_at).label("first_kickoff"),
            )
            .where(Game.status.in_(UNFINISHED_STATUSES))
            .group_by(Game.season, Game.week, Game.season_type)
            .order_by(func.min(Game.kickoff_at))
            .limit(1)
        )
        row = result.one_or_none()

        if row is not None:
            season, week, season_type, kickoff = row
            return NflState(
                season=season,
                week=week,
                season_type=season_type,
                next_kickoff_at=kickoff,
                lock_at=kickoff,
            )

        # Either the season is over or the schedule has not been ingested yet.
        # Reporting the latest known week keeps clients on a sane default
        # instead of turning an empty table into an error on every page.
        fallback = await self._session.execute(
            select(Game.season, Game.week, Game.season_type)
            .order_by(Game.season.desc(), Game.week.desc())
            .limit(1)
        )
        latest = fallback.one_or_none()
        if latest is None:
            return UNKNOWN_STATE
        return NflState(season=latest[0], week=latest[1], season_type=latest[2])

    async def active_models(self) -> list[ModelVersionSummary]:
        """Models currently serving predictions, with their eval metrics."""
        result = await self._session.execute(
            select(ModelVersion).where(ModelVersion.status == "ACTIVE").order_by(ModelVersion.name)
        )
        return [
            ModelVersionSummary(
                name=model.name,
                version=model.version,
                position=str(model.position),
                trained_at=model.promoted_at or model.created_at,
                metrics={
                    k: float(v)
                    for k, v in model.metrics.items()
                    if isinstance(v, int | float) and not isinstance(v, bool)
                },
            )
            for model in result.scalars().all()
        ]


__all__ = ["MetaService"]
