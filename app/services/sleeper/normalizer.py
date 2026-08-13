"""Sleeper's response shapes -> provider DTOs.

Kept apart from the client and the adapter because this is where the
provider's vocabulary stops. Everything downstream speaks the domain's
language, and adding ESPN means writing another one of these rather than
teaching the import worker a second dialect.

Two Sleeper quirks that are easy to get quietly wrong:

* **Points are split across two fields.** ``fpts`` holds the integer part and
  ``fpts_decimal`` the hundredths, so 1234.56 arrives as ``1234`` and ``56``.
  Reading only ``fpts`` loses the fraction, and every standings comparison is
  then wrong by up to a point.
* **Scoring is a ~60-key map, not a preset.** Collapsing it to
  Standard/Half-PPR/PPR would produce wrong numbers for a large fraction of
  real leagues, and wrong numbers are worse than none in a decision-support
  product (RFC 9.2).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import structlog

from app.domain.provider import (
    ProviderLeague,
    ProviderMatchup,
    ProviderPlayer,
    ProviderRoster,
    ProviderTeam,
    ProviderTransaction,
)

logger = structlog.get_logger()

# Bumped when the meaning of a stored ruleset changes, so a league imported
# under old semantics can be identified and re-normalized rather than silently
# scored under new ones (RFC 11.3).
SCORING_SCHEMA_VERSION = 1

MILLISECONDS_PER_SECOND = 1000
CENTS_PER_UNIT = Decimal(100)


def _points(settings: dict[str, Any], whole_key: str, decimal_key: str) -> Decimal:
    """Reassemble Sleeper's split point value.

    ``Decimal`` rather than float throughout: these feed standings and
    tiebreakers, and 0.1 + 0.2 is not 0.3 (RFC 11.1).
    """
    whole = settings.get(whole_key) or 0
    fraction = settings.get(decimal_key) or 0
    return Decimal(str(whole)) + (Decimal(str(fraction)) / CENTS_PER_UNIT)


def normalize_scoring(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and wrap Sleeper's ``scoring_settings``.

    The map is stored nearly as-is on purpose. Sleeper adds keys over time
    (new bonus categories, new positions), and a normalizer that allow-listed
    known keys would silently drop scoring a league actually uses -- the exact
    failure that makes a fantasy product untrustworthy. Instead every value is
    coerced to a number and anything non-numeric is dropped with a warning, so
    the ruleset stays evaluable without pretending to understand each key.

    Returns the stored JSONB shape: a versioned envelope around the rules.
    """
    rules: dict[str, float] = {}
    rejected: list[str] = []

    for key, value in (raw or {}).items():
        if isinstance(value, bool):
            # Sleeper uses booleans for a few toggles; they are not scoring
            # weights and multiplying by them would be meaningless.
            rejected.append(key)
            continue
        if isinstance(value, int | float):
            rules[key] = float(value)
        else:
            rejected.append(key)

    if rejected:
        logger.warning("sleeper_scoring_keys_dropped", keys=sorted(rejected))

    return {
        "schema_version": SCORING_SCHEMA_VERSION,
        "provider": "SLEEPER",
        "rules": rules,
    }


def normalize_playoff_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the playoff shape from Sleeper's flat settings blob."""
    settings = settings or {}
    return {
        "teams": settings.get("playoff_teams"),
        "start_week": settings.get("playoff_week_start"),
        "seed_type": settings.get("playoff_seed_type"),
        "round_type": settings.get("playoff_round_type"),
        "weeks_per_round": settings.get("playoff_type"),
    }


def normalize_waiver_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = settings or {}
    return {
        "type": settings.get("waiver_type"),
        "budget": settings.get("waiver_budget"),
        "clear_days": settings.get("waiver_clear_days"),
        "day_of_week": settings.get("waiver_day_of_week"),
    }


def normalize_league(payload: dict[str, Any]) -> ProviderLeague:
    """One Sleeper league object -> :class:`ProviderLeague`."""
    settings = payload.get("settings") or {}
    season_raw = payload.get("season")

    return ProviderLeague(
        external_id=str(payload["league_id"]),
        name=str(payload.get("name") or "Untitled league"),
        season=int(season_raw) if season_raw is not None else 0,
        # `total_rosters` is the authoritative team count; `settings.num_teams`
        # is absent on some older leagues.
        total_teams=int(payload.get("total_rosters") or settings.get("num_teams") or 0),
        scoring=normalize_scoring(payload.get("scoring_settings")),
        roster_positions=[str(p) for p in (payload.get("roster_positions") or [])],
        playoff_config=normalize_playoff_config(settings),
        waiver_config=normalize_waiver_config(settings),
        status=payload.get("status"),
        previous_league_id=payload.get("previous_league_id"),
    )


def normalize_teams(
    rosters: list[dict[str, Any]], users: list[dict[str, Any]]
) -> list[ProviderTeam]:
    """Join Sleeper's rosters and users into teams.

    Sleeper splits what is conceptually one team across two endpoints: the
    roster carries the record and the user carries the name. An orphaned
    roster (an abandoned team, which real leagues do have) still becomes a
    team, because its players and matchups matter.
    """
    users_by_id = {str(u["user_id"]): u for u in users if u.get("user_id")}
    teams: list[ProviderTeam] = []

    for roster in rosters:
        settings = roster.get("settings") or {}
        owner_id = roster.get("owner_id")
        user = users_by_id.get(str(owner_id)) if owner_id else None
        metadata = (user or {}).get("metadata") or {}

        display_name = (
            metadata.get("team_name")
            or (user or {}).get("display_name")
            or f"Team {roster.get('roster_id')}"
        )

        teams.append(
            ProviderTeam(
                external_id=str(roster["roster_id"]),
                display_name=str(display_name),
                owner_name=(user or {}).get("display_name"),
                owner_external_id=str(owner_id) if owner_id else None,
                wins=int(settings.get("wins") or 0),
                losses=int(settings.get("losses") or 0),
                ties=int(settings.get("ties") or 0),
                points_for=_points(settings, "fpts", "fpts_decimal"),
                points_against=_points(settings, "fpts_against", "fpts_against_decimal"),
                waiver_position=settings.get("waiver_position"),
                faab_remaining=_faab_remaining(settings),
            )
        )

    return teams


def _faab_remaining(settings: dict[str, Any]) -> int | None:
    """Sleeper reports budget *used*; what a manager cares about is left."""
    budget = settings.get("waiver_budget")
    used = settings.get("waiver_budget_used")
    if budget is None or used is None:
        return None
    return int(budget) - int(used)


def normalize_rosters(rosters: list[dict[str, Any]]) -> list[ProviderRoster]:
    """Roster payloads -> :class:`ProviderRoster`.

    Sleeper uses ``null`` inside ``starters`` for an empty slot, and those
    have to survive as gaps rather than be filtered out: the list is
    positional against ``roster_positions``, so dropping a null shifts every
    later player into the wrong slot.
    """
    return [
        ProviderRoster(
            team_external_id=str(roster["roster_id"]),
            player_external_ids=[str(p) for p in (roster.get("players") or []) if p],
            starter_external_ids=[str(p) if p else "" for p in (roster.get("starters") or [])],
            reserve_external_ids=[str(p) for p in (roster.get("reserve") or []) if p],
            taxi_external_ids=[str(p) for p in (roster.get("taxi") or []) if p],
        )
        for roster in rosters
    ]


def normalize_matchups(payload: list[dict[str, Any]], week: int) -> list[ProviderMatchup]:
    """Weekly matchup payloads -> :class:`ProviderMatchup`.

    ``custom_points`` wins when set: it is a commissioner's manual override,
    and the whole point of one is that it replaces the computed total.
    """
    matchups: list[ProviderMatchup] = []

    for entry in payload:
        custom = entry.get("custom_points")
        points_raw = custom if custom is not None else entry.get("points")
        matchup_id = entry.get("matchup_id")

        matchups.append(
            ProviderMatchup(
                week=week,
                # None is meaningful: a team on a bye has no opponent.
                matchup_id=str(matchup_id) if matchup_id is not None else None,
                team_external_id=str(entry["roster_id"]),
                points=Decimal(str(points_raw)) if points_raw is not None else None,
                starter_external_ids=[str(p) if p else "" for p in (entry.get("starters") or [])],
            )
        )

    return matchups


def normalize_transactions(payload: list[dict[str, Any]], week: int) -> list[ProviderTransaction]:
    """Transaction payloads -> :class:`ProviderTransaction`.

    ``adds``/``drops`` map a player id to the roster that gained or lost them,
    which is what makes a trade legible: one transaction, several rosters.
    """
    transactions: list[ProviderTransaction] = []

    for entry in payload:
        settings = entry.get("settings") or {}
        created = entry.get("created")

        transactions.append(
            ProviderTransaction(
                external_id=str(entry["transaction_id"]),
                # `leg` is Sleeper's week for the transaction; fall back to the
                # week we asked for when it is absent.
                week=int(entry.get("leg") or week),
                kind=str(entry.get("type") or "unknown"),
                status=str(entry.get("status") or "unknown"),
                created_at=_from_millis(created),
                team_external_ids=[str(r) for r in (entry.get("roster_ids") or [])],
                adds={str(k): str(v) for k, v in (entry.get("adds") or {}).items()},
                drops={str(k): str(v) for k, v in (entry.get("drops") or {}).items()},
                faab_spent=settings.get("waiver_bid"),
            )
        )

    return transactions


def _from_millis(value: Any) -> dt.datetime | None:
    """Sleeper timestamps are epoch milliseconds, and sometimes absent."""
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(int(value) / MILLISECONDS_PER_SECOND, tz=dt.UTC)
    except (TypeError, ValueError, OSError):
        logger.warning("sleeper_unparseable_timestamp", value=value)
        return None


def normalize_players(payload: dict[str, Any]) -> list[ProviderPlayer]:
    """Sleeper's whole-player export -> :class:`ProviderPlayer`.

    The export is a map of ~10k players keyed by id, several MB of JSON. Team
    defenses appear with the team abbreviation as their id and no first or
    last name, so the name is assembled defensively.
    """
    players: list[ProviderPlayer] = []

    for external_id, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        full_name = (
            entry.get("full_name")
            or " ".join(
                part for part in (entry.get("first_name"), entry.get("last_name")) if part
            ).strip()
            or str(external_id)
        )
        players.append(
            ProviderPlayer(
                external_id=str(external_id),
                full_name=full_name,
                position=entry.get("position"),
                team_abbr=entry.get("team"),
                status=entry.get("status"),
            )
        )

    return players


__all__ = [
    "SCORING_SCHEMA_VERSION",
    "normalize_league",
    "normalize_matchups",
    "normalize_players",
    "normalize_playoff_config",
    "normalize_rosters",
    "normalize_scoring",
    "normalize_teams",
    "normalize_transactions",
    "normalize_waiver_config",
]
