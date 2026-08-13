"""The full league import job (RFC 8.2).

Five stages, each reporting progress and each individually idempotent, so a
job killed anywhere can simply be run again -- which is exactly what the
reaper does with an interrupted import.

The stages after the league itself are *partial-tolerant*. A matchup week that
fails, or a player the crosswalk cannot resolve, is written to ``sync_errors``
and the import continues. A league that imports with a known reconciliation
item is far more useful than one that fails outright, and FR-1.7 asks for the
report rather than the failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from app.domain.jobs import JobKind
from app.repositories.league_import import LeagueImportRepository
from app.services.sleeper.adapter import SleeperAdapter
from app.services.sleeper.player_mapper import PlayerMapper
from app.workers.base import JobContext, register

if TYPE_CHECKING:
    import uuid

    from app.domain.provider import ProviderRoster

logger = structlog.get_logger()

PROVIDER = "SLEEPER"

# Progress checkpoints. Roughly proportional to wall-clock: the matchup sweep
# is 18 calls and dominates.
PCT_SETTINGS = 15
PCT_TEAMS = 45
PCT_ROSTERS = 60
PCT_MATCHUPS = 80
PCT_TRANSACTIONS = 95

# Below this, the import is reported as PARTIAL rather than clean: a roster
# missing a fifth of its players is not a usable league.
MIN_RESOLUTION_RATE = 0.8


@register(JobKind.LEAGUE_FULL_IMPORT)
async def import_league(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    """Import a whole league from its provider.

    ``params`` carries ``external_league_id``, the requesting ``user_id``, and
    optionally ``claim_team_external_id`` -- which team is the user's.
    """
    external_id = str(params["external_league_id"])
    user_id = params.get("user_id")
    claim_external_id = params.get("claim_team_external_id")

    repo = LeagueImportRepository(context.session)
    mapper = PlayerMapper(context.session)
    adapter = SleeperAdapter()

    unresolved_players: set[str] = set()

    try:
        # --- settings -----------------------------------------------------
        await context.check_cancelled()
        league = await adapter.fetch_league(external_id)
        league_id, created = await repo.upsert_league(PROVIDER, league)
        await context.session.commit()
        await context.report_progress(PCT_SETTINGS, "importing_settings")

        # --- teams --------------------------------------------------------
        await context.check_cancelled()
        teams = await adapter.fetch_teams(external_id)
        team_ids = await repo.upsert_teams(league_id, teams)

        if user_id is not None:
            claimed = team_ids.get(str(claim_external_id)) if claim_external_id else None
            await repo.claim_membership(user_id, league_id, claimed)
        await context.session.commit()
        await context.report_progress(PCT_TEAMS, "importing_teams")

        # --- rosters ------------------------------------------------------
        await context.check_cancelled()
        rosters = await adapter.fetch_rosters(external_id)
        unresolved_players |= await _import_rosters(repo, mapper, team_ids, rosters)
        await context.session.commit()
        await context.report_progress(PCT_ROSTERS, "importing_rosters")

        # --- matchups -----------------------------------------------------
        await context.check_cancelled()
        matchups = await adapter.fetch_all_matchups(external_id)
        matchup_count = await repo.upsert_matchups(league_id, matchups, team_ids)
        await context.session.commit()
        await context.report_progress(PCT_MATCHUPS, "importing_matchups")

        # --- transactions -------------------------------------------------
        await context.check_cancelled()
        transactions = await adapter.fetch_all_transactions(external_id)
        transaction_count = await repo.upsert_transactions(league_id, transactions)
        await context.session.commit()
        await context.report_progress(PCT_TRANSACTIONS, "importing_transactions")
    finally:
        await adapter.aclose()

    # --- reconciliation ---------------------------------------------------
    resolution_rate = _resolution_rate(rosters, unresolved_players)
    partial = bool(unresolved_players) and resolution_rate < MIN_RESOLUTION_RATE

    if unresolved_players:
        await repo.record_sync_error(
            league_id=league_id,
            job_id=context.job_id,
            severity="ERROR" if partial else "WARN",
            code="unmapped_players",
            detail={
                # Bounded: a crosswalk that is wholly empty would otherwise
                # write thousands of ids into one JSONB column.
                "player_external_ids": sorted(unresolved_players)[:100],
                "unmapped_count": len(unresolved_players),
                "resolution_rate": round(resolution_rate, 4),
            },
        )
        await logger.awarning(
            "league_import_unmapped_players",
            league_id=str(league_id),
            count=len(unresolved_players),
            resolution_rate=resolution_rate,
        )

    await repo.mark_synced(league_id, status="PARTIAL" if partial else "ACTIVE")
    await context.session.commit()

    return {
        "league_id": str(league_id),
        "created": created,
        "teams": len(team_ids),
        "matchups": matchup_count,
        "transactions": transaction_count,
        "unmapped_players": len(unresolved_players),
        "partial": partial,
    }


async def _import_rosters(
    repo: LeagueImportRepository,
    mapper: PlayerMapper,
    team_ids: dict[str, uuid.UUID],
    rosters: list[ProviderRoster],
) -> set[str]:
    """Resolve and write every roster. Returns the ids that did not resolve.

    Resolution is one batched lookup for the whole league rather than one per
    team: a 12-team league is ~200 players, and per-player round trips inside
    an import turn seconds into minutes.
    """
    every_player = {pid for roster in rosters for pid in roster.player_external_ids}
    mapping = await mapper.resolve_many(every_player)

    for roster in rosters:
        team_id = team_ids.get(roster.team_external_id)
        if team_id is None:
            # A roster for a team the provider did not list. Nothing to attach
            # it to; the missing team is the real problem.
            await logger.awarning(
                "roster_for_unknown_team", team_external_id=roster.team_external_id
            )
            continue

        resolved = [
            mapping.resolved[pid] for pid in roster.player_external_ids if pid in mapping.resolved
        ]
        await repo.replace_roster(team_id, resolved)

    return set(mapping.unresolved)


def _resolution_rate(rosters: list[ProviderRoster], unresolved: set[str]) -> float:
    total = len({pid for roster in rosters for pid in roster.player_external_ids})
    if not total:
        return 1.0
    return (total - len(unresolved)) / total


__all__ = ["MIN_RESOLUTION_RATE", "PROVIDER", "import_league"]
