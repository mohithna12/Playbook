"""Sleeper player ids -> canonical player ids.

The hard part of supporting a second provider (RFC 9.4). Every provider has
its own player id space, and a fantasy roster is meaningless until those ids
resolve to the players our stats and projections are keyed by.

Resolution is deliberately conservative, in this order:

1. **The crosswalk table.** An exact ``(source, external_id)`` match is the
   only fully trustworthy answer, and once written it is permanent.
2. **Exact name plus position.** Used once, to bootstrap the crosswalk, and
   only when it identifies exactly one player. Two players with the same name
   and position is a real occurrence, and guessing between them would put
   someone else's projections on a user's roster.

There is no fuzzy fallback. An unresolved player becomes a ``sync_errors`` row
and a visible reconciliation item -- surfacing it is the mitigation for R12,
because silently dropping a rostered player is indistinguishable from the user
not having them, and silently guessing is worse than both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.models.player import Player, PlayerExternalId

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.provider import ProviderPlayer

logger = structlog.get_logger()

SOURCE = "SLEEPER"


@dataclass(slots=True)
class MappingResult:
    """What a batch resolution produced, including what it could not."""

    resolved: dict[str, int] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    @property
    def resolution_rate(self) -> float:
        total = len(self.resolved) + len(self.unresolved)
        return len(self.resolved) / total if total else 1.0


class PlayerMapper:
    """Resolves provider player ids, caching within one import."""

    def __init__(self, session: AsyncSession, *, source: str = SOURCE) -> None:
        self._session = session
        self._source = source
        # One import touches the same players across rosters, matchups, and
        # transactions; without this that is hundreds of identical lookups.
        self._cache: dict[str, int] = {}

    async def resolve_many(self, external_ids: Iterable[str]) -> MappingResult:
        """Resolve a batch, reporting what could not be resolved.

        Batched rather than per-player: a 12-team league's rosters are ~200
        players, and 200 round trips inside an import is the difference
        between seconds and minutes.
        """
        wanted = {external_id for external_id in external_ids if external_id}
        result = MappingResult()

        pending = set()
        for external_id in wanted:
            cached = self._cache.get(external_id)
            if cached is not None:
                result.resolved[external_id] = cached
            else:
                pending.add(external_id)

        if not pending:
            return result

        rows = await self._session.execute(
            select(PlayerExternalId.external_id, PlayerExternalId.player_id).where(
                PlayerExternalId.source == self._source,
                PlayerExternalId.external_id.in_(pending),
            )
        )
        for external_id, player_id in rows.all():
            self._cache[external_id] = player_id
            result.resolved[external_id] = player_id
            pending.discard(external_id)

        result.unresolved = sorted(pending)
        return result

    async def resolve(self, external_id: str) -> int | None:
        """Resolve one player id, or None."""
        return (await self.resolve_many([external_id])).resolved.get(external_id)

    async def link(self, external_id: str, player_id: int, *, confidence: float = 1.0) -> None:
        """Record a crosswalk entry.

        ``ON CONFLICT DO NOTHING``: the first mapping wins. A later import
        must not silently repoint an id at a different player, because every
        historical stat row already joined through the original.
        """
        await self._session.execute(
            insert(PlayerExternalId)
            .values(
                source=self._source,
                external_id=external_id,
                player_id=player_id,
                confidence=confidence,
            )
            .on_conflict_do_nothing(index_elements=["source", "external_id"])
        )
        self._cache[external_id] = player_id

    async def link_from_export(self, players: Sequence[ProviderPlayer]) -> MappingResult:
        """Bootstrap the crosswalk from a provider's player export.

        Matches on exact name and position, and only when that identifies
        exactly one player. An ambiguous match is left unresolved rather than
        guessed: two active players sharing a name and position is rare but
        real, and picking wrong puts another player's projections on someone's
        roster with no visible sign of it.
        """
        result = MappingResult()
        if not players:
            return result

        already = await self.resolve_many([p.external_id for p in players])
        result.resolved.update(already.resolved)
        remaining = [p for p in players if p.external_id in set(already.unresolved)]
        if not remaining:
            return result

        # One query for every candidate name, rather than one per player.
        names = {p.full_name.lower() for p in remaining if p.full_name}
        rows = await self._session.execute(
            select(Player.id, func.lower(Player.full_name), Player.position).where(
                func.lower(Player.full_name).in_(names)
            )
        )

        by_name_position: dict[tuple[str, str | None], list[int]] = {}
        for player_id, lowered, position in rows.all():
            by_name_position.setdefault((lowered, position), []).append(player_id)

        for provider_player in remaining:
            key = (provider_player.full_name.lower(), provider_player.position)
            candidates = by_name_position.get(key, [])

            if len(candidates) == 1:
                await self.link(provider_player.external_id, candidates[0], confidence=0.9)
                result.resolved[provider_player.external_id] = candidates[0]
                continue

            if len(candidates) > 1:
                await logger.awarning(
                    "player_mapping_ambiguous",
                    source=self._source,
                    external_id=provider_player.external_id,
                    name=provider_player.full_name,
                    position=provider_player.position,
                    candidates=len(candidates),
                )
            result.unresolved.append(provider_player.external_id)

        return result


__all__ = ["SOURCE", "MappingResult", "PlayerMapper"]
