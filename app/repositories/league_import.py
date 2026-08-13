"""Idempotent upserts for an imported league.

Re-importing a league must converge on the same rows, not accumulate copies:
the reaper replays interrupted imports, users re-import deliberately, and the
incremental sync runs the same writes on a schedule. Every method here is
therefore an upsert keyed on the natural identity the provider supplies, never
an insert.

That property is also what makes reaping a half-finished import safe (RFC
7.2): a job killed between rosters and matchups can simply be run again.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.models.job import SyncError
from app.models.league import League, LeagueMembership, Matchup, RosterEntry, Team, Transaction
from app.repositories.base import Repository

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from app.domain.provider import (
        ProviderLeague,
        ProviderMatchup,
        ProviderTeam,
        ProviderTransaction,
    )

logger = structlog.get_logger()


class LeagueImportRepository(Repository[League]):
    """Writes one provider's view of a league into the schema."""

    model = League

    async def upsert_league(self, provider: str, league: ProviderLeague) -> tuple[uuid.UUID, bool]:
        """Create or update the league row. Returns ``(id, created)``.

        Keyed on ``(provider, external_id, season)``: the same Sleeper league
        in a new season is a different league, with its own rosters and
        standings, and Sleeper models that as a distinct id chained by
        ``previous_league_id``.
        """
        values: dict[str, Any] = {
            "provider": provider,
            "external_id": league.external_id,
            "season": league.season,
            "name": league.name,
            "total_teams": league.total_teams,
            "scoring_rules": league.scoring,
            "roster_positions": league.roster_positions,
            "playoff_config": league.playoff_config,
            "waiver_config": league.waiver_config,
            "sync_status": "SYNCING",
        }

        statement = (
            insert(League)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_league_provider_ext",
                # Settings genuinely change mid-season -- a commissioner edits
                # scoring, adds a roster slot -- so a re-import must pick them
                # up rather than preserve a stale copy.
                set_={
                    "name": values["name"],
                    "total_teams": values["total_teams"],
                    "scoring_rules": values["scoring_rules"],
                    "roster_positions": values["roster_positions"],
                    "playoff_config": values["playoff_config"],
                    "waiver_config": values["waiver_config"],
                    "sync_status": "SYNCING",
                    # Set explicitly: `onupdate` is a SQLAlchemy-side default
                    # that fires for update() statements, not for the DO UPDATE
                    # arm of an INSERT, so without this the timestamp would
                    # stay at the original insert time forever.
                    "updated_at": func.now(),
                },
            )
            # `xmax = 0` is Postgres's own answer to "did this INSERT actually
            # insert?" -- the row's delete-transaction id is zero only for a
            # tuple that was inserted rather than updated. Comparing
            # created_at to updated_at cannot work here: the DO UPDATE arm
            # leaves created_at alone, so they match on both paths.
            .returning(League.id, text("xmax = 0 AS inserted"))
        )
        league_id, inserted = (await self.session.execute(statement)).one()
        return league_id, bool(inserted)

    async def upsert_teams(
        self, league_id: uuid.UUID, teams: Sequence[ProviderTeam]
    ) -> dict[str, uuid.UUID]:
        """Upsert teams and return the provider id -> our id map.

        That map is what every later stage joins through, so it is returned
        rather than re-queried.
        """
        if not teams:
            return {}

        rows = [
            {
                "league_id": league_id,
                "external_id": team.external_id,
                "display_name": team.display_name,
                "owner_name": team.owner_name,
                "wins": team.wins,
                "losses": team.losses,
                "ties": team.ties,
                "points_for": team.points_for,
                "points_against": team.points_against,
                "waiver_position": team.waiver_position,
                "faab_remaining": team.faab_remaining,
            }
            for team in teams
        ]

        statement = (
            insert(Team)
            .values(rows)
            .on_conflict_do_update(
                constraint="uq_team_league_ext",
                set_={
                    "display_name": insert(Team).excluded.display_name,
                    "owner_name": insert(Team).excluded.owner_name,
                    "wins": insert(Team).excluded.wins,
                    "losses": insert(Team).excluded.losses,
                    "ties": insert(Team).excluded.ties,
                    "points_for": insert(Team).excluded.points_for,
                    "points_against": insert(Team).excluded.points_against,
                    "waiver_position": insert(Team).excluded.waiver_position,
                    "faab_remaining": insert(Team).excluded.faab_remaining,
                },
            )
            .returning(Team.id, Team.external_id)
        )
        result = await self.session.execute(statement)
        return {external_id: team_id for team_id, external_id in result.all()}

    async def claim_membership(
        self, user_id: uuid.UUID, league_id: uuid.UUID, team_id: uuid.UUID | None
    ) -> None:
        """Record that a user has imported this league.

        This row is what every league-scoped query joins through, so the
        import is not usable until it exists (RFC 20.1).
        """
        await self.session.execute(
            insert(LeagueMembership)
            .values(user_id=user_id, league_id=league_id, team_id=team_id)
            .on_conflict_do_update(
                index_elements=["user_id", "league_id"],
                # A user can re-import and claim a different team.
                set_={"team_id": team_id},
            )
        )

    async def replace_roster(self, team_id: uuid.UUID, player_ids: Sequence[int]) -> None:
        """Set a team's roster to exactly ``player_ids``.

        Replace rather than merge: a roster is a snapshot, and a player who
        was dropped has to disappear. Upserting alone would leave them
        attached forever, so a team that churned its bench would accumulate
        every player it ever held.
        """
        wanted = set(player_ids)

        # Two statements rather than one with a conditional predicate: an
        # empty roster means delete everything, and `notin_(())` would delete
        # nothing.
        removal = delete(RosterEntry).where(RosterEntry.team_id == team_id)
        if wanted:
            removal = removal.where(RosterEntry.player_id.notin_(wanted))
        await self.session.execute(removal)

        if not wanted:
            return

        await self.session.execute(
            insert(RosterEntry)
            .values([{"team_id": team_id, "player_id": pid} for pid in sorted(wanted)])
            .on_conflict_do_nothing(index_elements=["team_id", "player_id"])
        )

    async def upsert_matchups(
        self,
        league_id: uuid.UUID,
        matchups: Sequence[ProviderMatchup],
        team_ids: dict[str, uuid.UUID],
    ) -> int:
        """Pair per-team matchup rows into home/away and upsert them.

        Providers report a matchup once per team, joined by an opaque id; the
        schema stores one row per pairing. A team with no ``matchup_id`` is on
        a bye and is stored with a null away side rather than dropped -- its
        points still count toward standings.
        """
        if not matchups:
            return 0

        by_week: dict[int, dict[str | None, list[ProviderMatchup]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for entry in matchups:
            by_week[entry.week][entry.matchup_id].append(entry)

        rows: list[dict[str, Any]] = []
        for week, groups in by_week.items():
            index = 0
            for matchup_id, sides in sorted(
                groups.items(), key=lambda item: (item[0] is None, item[0] or "")
            ):
                if matchup_id is None:
                    # Byes: each is its own single-sided row.
                    for side in sides:
                        row = self._matchup_row(league_id, week, index, side, None, team_ids)
                        if row is not None:
                            rows.append(row)
                            index += 1
                    continue

                # Deterministic sides, so a re-import does not swap home and
                # away and rewrite history.
                ordered = sorted(sides, key=lambda side: side.team_external_id)
                home = ordered[0]
                away = ordered[1] if len(ordered) > 1 else None
                row = self._matchup_row(league_id, week, index, home, away, team_ids)
                if row is not None:
                    rows.append(row)
                    index += 1

        if not rows:
            return 0

        await self.session.execute(
            insert(Matchup)
            .values(rows)
            .on_conflict_do_update(
                index_elements=["league_id", "week", "matchup_index"],
                set_={
                    "home_team_id": insert(Matchup).excluded.home_team_id,
                    "away_team_id": insert(Matchup).excluded.away_team_id,
                    "home_points": insert(Matchup).excluded.home_points,
                    "away_points": insert(Matchup).excluded.away_points,
                    "is_final": insert(Matchup).excluded.is_final,
                },
            )
        )
        return len(rows)

    @staticmethod
    def _matchup_row(
        league_id: uuid.UUID,
        week: int,
        index: int,
        home: ProviderMatchup,
        away: ProviderMatchup | None,
        team_ids: dict[str, uuid.UUID],
    ) -> dict[str, Any] | None:
        home_id = team_ids.get(home.team_external_id)
        if home_id is None:
            # A matchup naming a team we did not import. Skipping is right:
            # the FK would fail, and the team is the missing thing, not this.
            return None

        away_id = team_ids.get(away.team_external_id) if away else None
        return {
            "league_id": league_id,
            "week": week,
            "matchup_index": index,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_points": home.points,
            "away_points": away.points if away else None,
            # Points recorded means the week has been scored.
            "is_final": home.points is not None and home.points > 0,
        }

    async def upsert_transactions(
        self, league_id: uuid.UUID, transactions: Sequence[ProviderTransaction]
    ) -> int:
        """Upsert transactions, keyed on the provider's own id.

        The provider payload is kept in ``payload`` rather than shredded into
        columns: transaction shapes differ per provider and per type, and a
        trade with draft picks has no natural column layout worth inventing
        before anything reads it.
        """
        if not transactions:
            return 0

        rows = [
            {
                "league_id": league_id,
                "external_id": entry.external_id,
                "type": entry.kind,
                "week": entry.week,
                "status": entry.status,
                "executed_at": entry.created_at,
                "payload": {
                    "adds": entry.adds,
                    "drops": entry.drops,
                    "roster_ids": entry.team_external_ids,
                    "faab_spent": entry.faab_spent,
                },
            }
            for entry in transactions
        ]

        await self.session.execute(
            insert(Transaction)
            .values(rows)
            .on_conflict_do_update(
                constraint="uq_txn",
                set_={
                    # Status changes: a pending waiver becomes complete or
                    # failed, and the row must follow.
                    "status": insert(Transaction).excluded.status,
                    "executed_at": insert(Transaction).excluded.executed_at,
                    "payload": insert(Transaction).excluded.payload,
                },
            )
        )
        return len(rows)

    async def record_sync_error(
        self,
        *,
        league_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
        severity: str,
        code: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Write a non-fatal import problem.

        Written rather than raised so a partial import still yields a usable
        league plus an explicit reconciliation report (FR-1.7). An unmapped
        player that vanishes silently is indistinguishable from a player the
        user never had.
        """
        self.session.add(
            SyncError(
                league_id=league_id,
                job_id=job_id,
                severity=severity,
                code=code,
                detail=detail,
            )
        )

    async def mark_synced(self, league_id: uuid.UUID, *, status: str = "ACTIVE") -> None:
        """Finish the import, stamping when it completed."""
        from sqlalchemy import update

        await self.session.execute(
            update(League)
            .where(League.id == league_id)
            .values(sync_status=status, last_synced_at=dt.datetime.now(dt.UTC))
        )

    async def find_league(self, provider: str, external_id: str, season: int) -> uuid.UUID | None:
        result = await self.session.execute(
            select(League.id).where(
                League.provider == provider,
                League.external_id == external_id,
                League.season == season,
            )
        )
        return result.scalar_one_or_none()


__all__ = ["LeagueImportRepository"]
