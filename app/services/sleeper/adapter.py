"""The Sleeper implementation of :class:`~app.domain.provider.LeagueProvider`.

Thin by design: it knows Sleeper's URL shapes and nothing else. Transport
concerns live in the client, shape concerns in the normalizer, and this is the
seam that satisfies the protocol.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from app.services.sleeper import normalizer
from app.services.sleeper.client import SleeperClient

if TYPE_CHECKING:
    from app.domain.provider import (
        ProviderLeague,
        ProviderMatchup,
        ProviderPlayer,
        ProviderRoster,
        ProviderTeam,
        ProviderTransaction,
    )

logger = structlog.get_logger()

# The regular season plus Sleeper's longest playoff configuration. Fetching
# past the league's actual end is cheap -- an empty list -- and avoids
# depending on a playoff_week_start that some leagues leave unset.
MAX_WEEK = 18


class SleeperAdapter:
    """Reads a Sleeper league. Satisfies ``LeagueProvider`` structurally."""

    name = "SLEEPER"

    def __init__(self, client: SleeperClient | None = None) -> None:
        self._client = client or SleeperClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_league(self, external_id: str) -> ProviderLeague:
        payload = await self._client.get(f"/league/{external_id}")
        return normalizer.normalize_league(payload)

    async def fetch_teams(self, external_id: str) -> list[ProviderTeam]:
        """Teams need two endpoints joined; fetch them concurrently.

        Sleeper keeps the record on the roster and the name on the user, so
        neither call alone produces a team.
        """
        rosters, users = await asyncio.gather(
            self._client.get(f"/league/{external_id}/rosters"),
            self._client.get(f"/league/{external_id}/users"),
        )
        return normalizer.normalize_teams(rosters or [], users or [])

    async def fetch_rosters(self, external_id: str) -> list[ProviderRoster]:
        payload = await self._client.get(f"/league/{external_id}/rosters")
        return normalizer.normalize_rosters(payload or [])

    async def fetch_matchups(self, external_id: str, week: int) -> list[ProviderMatchup]:
        payload = await self._client.get(f"/league/{external_id}/matchups/{week}")
        return normalizer.normalize_matchups(payload or [], week)

    async def fetch_transactions(self, external_id: str, week: int) -> list[ProviderTransaction]:
        payload = await self._client.get(f"/league/{external_id}/transactions/{week}")
        return normalizer.normalize_transactions(payload or [], week)

    async def fetch_all_matchups(
        self, external_id: str, *, through_week: int = MAX_WEEK
    ) -> list[ProviderMatchup]:
        """Every week's matchups.

        Concurrency is bounded inside the client's semaphore rather than here,
        so a wide gather cannot outrun the shared rate budget. Failures are
        per-week: one bad week yields a partial import with a sync error
        rather than losing the other seventeen.
        """
        results = await asyncio.gather(
            *(self.fetch_matchups(external_id, week) for week in range(1, through_week + 1)),
            return_exceptions=True,
        )

        matchups: list[ProviderMatchup] = []
        for week, result in enumerate(results, start=1):
            if isinstance(result, BaseException):
                await logger.awarning(
                    "sleeper_week_fetch_failed",
                    league=external_id,
                    week=week,
                    error=str(result),
                )
                continue
            matchups.extend(result)
        return matchups

    async def fetch_all_transactions(
        self, external_id: str, *, through_week: int = MAX_WEEK
    ) -> list[ProviderTransaction]:
        """Every week's transactions, with the same per-week failure policy."""
        results = await asyncio.gather(
            *(self.fetch_transactions(external_id, week) for week in range(1, through_week + 1)),
            return_exceptions=True,
        )

        transactions: list[ProviderTransaction] = []
        for week, result in enumerate(results, start=1):
            if isinstance(result, BaseException):
                await logger.awarning(
                    "sleeper_week_fetch_failed",
                    league=external_id,
                    week=week,
                    error=str(result),
                )
                continue
            transactions.extend(result)
        return transactions

    async def fetch_players(self) -> list[ProviderPlayer]:
        """Sleeper's whole-player export.

        Several megabytes and rarely changing, so this is a seeding path, not
        something an import calls.
        """
        payload: dict[str, Any] = await self._client.get("/players/nfl")
        return normalizer.normalize_players(payload or {})

    async def fetch_nfl_state(self) -> dict[str, Any]:
        """Sleeper's view of the current season and week."""
        payload: dict[str, Any] = await self._client.get("/state/nfl")
        return payload


__all__ = ["MAX_WEEK", "SleeperAdapter"]
