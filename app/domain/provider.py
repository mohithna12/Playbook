"""The league-provider contract and its provider-shaped DTOs (RFC 9.4).

Each provider returns *provider-shaped* data which a normalizer converts into
these types. Keeping that boundary explicit is what makes adding ESPN or Yahoo
a matter of writing one adapter rather than threading a second data shape
through the import worker.

The genuinely hard part of a second provider is not the HTTP client -- it is
``map_player``, because every provider has its own player id space. The
crosswalk table (RFC 11.3) is what makes that tractable, and it is why these
DTOs carry the provider's own id rather than resolving it here.

Pure data: no I/O, no SQLAlchemy. The protocol lives in the domain so both the
service layer and the adapters can depend on it without depending on each
other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import datetime as dt


@dataclass(frozen=True, slots=True)
class ProviderLeague:
    """A league's settings as the provider describes them.

    ``scoring`` is the raw provider map, not a preset name. Sleeper exposes
    ~60 keys and modelling them as Standard/Half-PPR/PPR would be wrong for a
    large fraction of real leagues -- and wrong numbers are worse than no
    numbers in a decision-support product (RFC 9.2).
    """

    external_id: str
    name: str
    season: int
    total_teams: int
    scoring: dict[str, Any]
    roster_positions: list[str]
    playoff_config: dict[str, Any]
    waiver_config: dict[str, Any] | None = None
    status: str | None = None
    previous_league_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderTeam:
    """One fantasy team, plus the provider's id for its owner."""

    external_id: str
    display_name: str
    owner_name: str | None = None
    owner_external_id: str | None = None
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: Decimal = Decimal(0)
    points_against: Decimal = Decimal(0)
    waiver_position: int | None = None
    faab_remaining: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderRoster:
    """A team's players, in the provider's id space.

    ``starters`` is ordered and positional -- its index maps to the league's
    roster_positions -- so it cannot be collapsed into a set.
    """

    team_external_id: str
    player_external_ids: list[str] = field(default_factory=list)
    starter_external_ids: list[str] = field(default_factory=list)
    reserve_external_ids: list[str] = field(default_factory=list)
    taxi_external_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProviderMatchup:
    """One team's side of a weekly matchup.

    Providers report matchups per team rather than per pairing, joined by an
    opaque ``matchup_id``. Modelling it the same way avoids inventing a
    pairing that the provider may not agree with when a league has an odd
    number of teams or a bye.
    """

    week: int
    matchup_id: str | None
    team_external_id: str
    points: Decimal | None = None
    starter_external_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProviderTransaction:
    """A roster move: add, drop, trade, or waiver claim."""

    external_id: str
    week: int
    kind: str
    status: str
    created_at: dt.datetime | None = None
    team_external_ids: list[str] = field(default_factory=list)
    adds: dict[str, str] = field(default_factory=dict)
    drops: dict[str, str] = field(default_factory=dict)
    faab_spent: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderPlayer:
    """A player as the provider knows them, for crosswalk resolution."""

    external_id: str
    full_name: str
    position: str | None = None
    team_abbr: str | None = None
    status: str | None = None


@runtime_checkable
class LeagueProvider(Protocol):
    """What every provider adapter must implement (RFC 9.4).

    A Protocol rather than an ABC so an adapter does not have to import this
    module to satisfy it, and so test doubles are structural rather than
    inherited.
    """

    name: str

    async def fetch_league(self, external_id: str) -> ProviderLeague: ...

    async def fetch_teams(self, external_id: str) -> list[ProviderTeam]: ...

    async def fetch_rosters(self, external_id: str) -> list[ProviderRoster]: ...

    async def fetch_matchups(self, external_id: str, week: int) -> list[ProviderMatchup]: ...

    async def fetch_transactions(
        self, external_id: str, week: int
    ) -> list[ProviderTransaction]: ...


__all__ = [
    "LeagueProvider",
    "ProviderLeague",
    "ProviderMatchup",
    "ProviderPlayer",
    "ProviderRoster",
    "ProviderTeam",
    "ProviderTransaction",
]
