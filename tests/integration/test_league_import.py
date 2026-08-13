"""The league import, end to end against a real database.

The adapter is stubbed with the recorded fixtures rather than hitting
Sleeper: what is under test is the import -- upserts, roster replacement,
matchup pairing, player reconciliation -- not the HTTP client, which has its
own suite.

The property that matters most here is idempotence. The reaper replays
interrupted imports, users re-import, and the incremental sync runs the same
writes on a schedule, so importing twice must converge rather than duplicate.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from app.domain.provider import ProviderPlayer
from app.models.job import SyncError
from app.models.league import League, LeagueMembership, Matchup, RosterEntry, Team, Transaction
from app.models.player import Player, PlayerExternalId
from app.repositories.league_import import LeagueImportRepository
from app.services.sleeper import normalizer
from app.services.sleeper.player_mapper import PlayerMapper
from app.workers import league_import

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "sleeper"


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class StubAdapter:
    """Serves the recorded fixtures. Structurally a LeagueProvider."""

    name = "SLEEPER"

    def __init__(self, *, weeks: int = 1) -> None:
        self._weeks = weeks
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def fetch_league(self, _external_id: str) -> Any:
        return normalizer.normalize_league(load("league"))

    async def fetch_teams(self, _external_id: str) -> Any:
        return normalizer.normalize_teams(load("rosters"), load("users"))

    async def fetch_rosters(self, _external_id: str) -> Any:
        return normalizer.normalize_rosters(load("rosters"))

    async def fetch_all_matchups(self, _external_id: str, **_kw: Any) -> Any:
        return normalizer.normalize_matchups(load("matchups_week_8"), week=8)

    async def fetch_all_transactions(self, _external_id: str, **_kw: Any) -> Any:
        return normalizer.normalize_transactions(load("transactions_week_8"), week=8)


class FakeContext:
    """The slice of JobContext the import uses."""

    def __init__(self, session: AsyncSession, job_id: uuid.UUID) -> None:
        self.session = session
        # A real jobs row, not an invented id: sync_errors has an FK to it, so
        # a fabricated one turns the reconciliation path into a foreign-key
        # violation rather than testing it.
        self.job_id = job_id
        self.progress: list[tuple[int, str | None]] = []
        self.cancel_after: int | None = None
        self._checks = 0

    async def report_progress(self, pct: int, step: str | None = None) -> None:
        self.progress.append((pct, step))

    async def check_cancelled(self) -> None:
        self._checks += 1
        if self.cancel_after is not None and self._checks > self.cancel_after:
            from app.workers.base import JobCancelled

            raise JobCancelled


@pytest.fixture
def stub_adapter(monkeypatch: pytest.MonkeyPatch) -> StubAdapter:
    adapter = StubAdapter()
    monkeypatch.setattr(league_import, "SleeperAdapter", lambda: adapter)
    return adapter


async def seed_players(session: AsyncSession, sleeper_ids: list[str]) -> dict[str, int]:
    """Create canonical players and crosswalk them, as the seeder would."""
    mapping: dict[str, int] = {}
    for index, external_id in enumerate(sleeper_ids):
        player = Player(full_name=f"Player {external_id}", position="RB")
        session.add(player)
        await session.flush()
        session.add(
            PlayerExternalId(source="SLEEPER", external_id=external_id, player_id=player.id)
        )
        mapping[external_id] = player.id
        del index
    await session.flush()
    return mapping


ALL_FIXTURE_PLAYERS = ["4034", "6794", "5849", "1234", "2222", "3333"]


async def make_job_row(session: AsyncSession) -> uuid.UUID:
    """A real jobs row for the import to attach sync errors to."""
    from app.domain.jobs import JobKind
    from app.repositories.job import JobRepository

    job, _ = await JobRepository(session).create(
        user_id=None, kind=JobKind.LEAGUE_FULL_IMPORT, params={}
    )
    await session.flush()
    return job.id


async def run_import(
    session: AsyncSession, *, user_id: uuid.UUID | None = None, claim: str | None = None
) -> tuple[dict[str, Any], FakeContext]:
    context = FakeContext(session, await make_job_row(session))
    params: dict[str, Any] = {"external_league_id": "1124589302838374400"}
    if user_id is not None:
        params["user_id"] = user_id
    if claim is not None:
        params["claim_team_external_id"] = claim
    result = await league_import.import_league(context, params)  # type: ignore[arg-type]
    return result, context


class TestFullImport:
    async def test_a_league_imports(self, session: AsyncSession, stub_adapter: StubAdapter) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        result, _ = await run_import(session)

        league = (await session.execute(select(League))).scalar_one()
        assert league.name == "Sunday Scaries"
        assert league.season == 2026
        assert league.total_teams == 12
        assert result["created"] is True

    async def test_the_scoring_ruleset_is_stored_whole(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """Not a preset -- every key the league scores must survive the trip."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        league = (await session.execute(select(League))).scalar_one()
        rules = league.scoring_rules["rules"]
        assert rules["bonus_rec_te"] == 0.5
        assert rules["pass_int"] == -2
        assert len(rules) == len(load("league")["scoring_settings"])

    async def test_teams_are_imported_with_reassembled_points(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        teams = (await session.execute(select(Team).order_by(Team.external_id))).scalars().all()
        assert len(teams) == 2
        assert teams[0].display_name == "Kelce's Kitchen"
        assert float(teams[0].points_for) == 1234.56

    async def test_rosters_are_written(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        count = (await session.execute(select(func.count()).select_from(RosterEntry))).scalar_one()
        assert count == 6  # 4 on roster 1, 2 on roster 2

    async def test_progress_is_reported_at_each_stage(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        _, context = await run_import(session)

        steps = [step for _, step in context.progress]
        assert steps == [
            "importing_settings",
            "importing_teams",
            "importing_rosters",
            "importing_matchups",
            "importing_transactions",
        ]
        assert [pct for pct, _ in context.progress] == sorted(pct for pct, _ in context.progress)

    async def test_the_league_ends_active(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        league = (await session.execute(select(League))).scalar_one()
        assert league.sync_status == "ACTIVE"
        assert league.last_synced_at is not None

    async def test_the_adapter_is_closed(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """A leaked connection pool per import would exhaust the worker."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        assert stub_adapter.closed is True


class TestIdempotence:
    async def test_re_importing_does_not_duplicate(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """The property the reaper depends on to replay an interrupted import."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        first, _ = await run_import(session)
        second, _ = await run_import(session)

        assert first["league_id"] == second["league_id"]
        assert first["created"] is True
        assert second["created"] is False

        for model in (League, Team, Matchup, Transaction):
            count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
            assert count == _expected_count(model), f"{model.__name__} duplicated"

    async def test_roster_changes_replace_rather_than_accumulate(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """A dropped player must disappear, not linger forever."""
        players = await seed_players(session, ALL_FIXTURE_PLAYERS)
        await run_import(session)

        team = (await session.execute(select(Team).where(Team.external_id == "1"))).scalar_one()
        repo = LeagueImportRepository(session)

        await repo.replace_roster(team.id, [players["4034"]])
        await session.flush()

        remaining = (
            (
                await session.execute(
                    select(RosterEntry.player_id).where(RosterEntry.team_id == team.id)
                )
            )
            .scalars()
            .all()
        )
        assert list(remaining) == [players["4034"]]

    async def test_an_emptied_roster_is_cleared(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)
        await run_import(session)

        team = (await session.execute(select(Team).where(Team.external_id == "1"))).scalar_one()
        await LeagueImportRepository(session).replace_roster(team.id, [])
        await session.flush()

        count = (
            await session.execute(
                select(func.count()).select_from(RosterEntry).where(RosterEntry.team_id == team.id)
            )
        ).scalar_one()
        assert count == 0

    async def test_updated_settings_are_picked_up(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """A commissioner editing scoring mid-season must not be ignored."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)
        await run_import(session)

        changed = {**load("league"), "name": "Renamed League"}

        async def fetch_changed(_external_id: str) -> Any:
            return normalizer.normalize_league(changed)

        stub_adapter.fetch_league = fetch_changed  # type: ignore[method-assign]
        await run_import(session)

        league = (await session.execute(select(League))).scalar_one()
        assert league.name == "Renamed League"


class TestMatchupPairing:
    async def test_per_team_rows_are_paired(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """Sleeper reports a matchup once per team; the schema stores pairings."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        matchups = (await session.execute(select(Matchup))).scalars().all()
        paired = [m for m in matchups if m.away_team_id is not None]
        assert len(paired) == 1
        assert paired[0].home_points is not None
        assert paired[0].away_points is not None

    async def test_a_commissioner_override_is_the_stored_score(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        matchup = (
            await session.execute(select(Matchup).where(Matchup.away_team_id.isnot(None)))
        ).scalar_one()
        assert float(matchup.away_points) == 100.0  # override, not 96.5

    async def test_a_bye_is_stored_with_no_away_side(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """A team on a bye still scores points that count toward standings."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)
        await run_import(session)

        league = (await session.execute(select(League))).scalar_one()
        team = (await session.execute(select(Team).where(Team.external_id == "1"))).scalar_one()

        from app.domain.provider import ProviderMatchup

        count = await LeagueImportRepository(session).upsert_matchups(
            league.id,
            [
                ProviderMatchup(
                    week=9, matchup_id=None, team_external_id="1", points=Decimal("88.5")
                )
            ],
            {"1": team.id},
        )
        await session.flush()

        assert count == 1
        bye = (await session.execute(select(Matchup).where(Matchup.week == 9))).scalar_one()
        assert bye.away_team_id is None
        assert float(bye.home_points) == 88.5

    async def test_a_matchup_naming_an_unknown_team_is_skipped(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """Roster 3 appears in matchups but not in the roster fixture."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        team_ids = set((await session.execute(select(Team.id))).scalars().all())
        matchups = (await session.execute(select(Matchup))).scalars().all()
        for matchup in matchups:
            assert matchup.home_team_id in team_ids


class TestTransactions:
    async def test_transactions_are_imported(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        transactions = (
            (await session.execute(select(Transaction).order_by(Transaction.external_id)))
            .scalars()
            .all()
        )
        assert len(transactions) == 3
        assert transactions[0].type == "waiver"
        assert transactions[0].payload["faab_spent"] == 17

    async def test_a_status_change_updates_in_place(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """A pending waiver becoming complete is an update, not a new row."""
        await seed_players(session, ALL_FIXTURE_PLAYERS)
        await run_import(session)

        payload = load("transactions_week_8")
        payload[0]["status"] = "failed"

        async def fetch_changed(_external_id: str, **_kw: Any) -> Any:
            return normalizer.normalize_transactions(payload, week=8)

        stub_adapter.fetch_all_transactions = fetch_changed  # type: ignore[method-assign]
        await run_import(session)

        transactions = (
            (
                await session.execute(
                    select(Transaction).where(Transaction.external_id == "1124589302838374401")
                )
            )
            .scalars()
            .all()
        )
        assert len(transactions) == 1
        assert transactions[0].status == "failed"


class TestPlayerReconciliation:
    async def test_unmapped_players_are_recorded_not_dropped_silently(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """R12's mitigation: an unresolved player must be visible."""
        await seed_players(session, ["4034", "6794"])  # 4 of 6 left unmapped

        result, _ = await run_import(session)

        assert result["unmapped_players"] == 4
        errors = (await session.execute(select(SyncError))).scalars().all()
        assert len(errors) == 1
        assert errors[0].code == "unmapped_players"
        assert errors[0].detail["unmapped_count"] == 4

    async def test_a_low_resolution_rate_marks_the_league_partial(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """A roster missing most of its players is not a usable league."""
        await seed_players(session, ["4034"])  # 1 of 6

        result, _ = await run_import(session)

        assert result["partial"] is True
        league = (await session.execute(select(League))).scalar_one()
        assert league.sync_status == "PARTIAL"

    async def test_a_high_resolution_rate_stays_active(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """One missing player is a warning, not a failed import."""
        await seed_players(session, ALL_FIXTURE_PLAYERS[:5])  # 5 of 6

        result, _ = await run_import(session)

        assert result["partial"] is False
        league = (await session.execute(select(League))).scalar_one()
        assert league.sync_status == "ACTIVE"
        errors = (await session.execute(select(SyncError))).scalars().all()
        assert errors[0].severity == "WARN"

    async def test_a_clean_import_records_no_sync_errors(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        await seed_players(session, ALL_FIXTURE_PLAYERS)

        await run_import(session)

        errors = (await session.execute(select(SyncError))).scalars().all()
        assert errors == []


class TestMembership:
    async def test_the_importing_user_gets_a_membership(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """Without this row the league is invisible to every scoped query."""
        from app.repositories.user import UserRepository

        await seed_players(session, ALL_FIXTURE_PLAYERS)
        user = await UserRepository(session).get_or_create("importer", "i@example.com")

        await run_import(session, user_id=user.id, claim="1")

        membership = (await session.execute(select(LeagueMembership))).scalar_one()
        assert membership.user_id == user.id
        assert membership.team_id is not None

    async def test_re_importing_can_reclaim_a_different_team(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        from app.repositories.user import UserRepository

        await seed_players(session, ALL_FIXTURE_PLAYERS)
        user = await UserRepository(session).get_or_create("reclaimer", "r@example.com")

        await run_import(session, user_id=user.id, claim="1")
        await run_import(session, user_id=user.id, claim="2")

        memberships = (await session.execute(select(LeagueMembership))).scalars().all()
        assert len(memberships) == 1
        team = (
            await session.execute(select(Team).where(Team.id == memberships[0].team_id))
        ).scalar_one()
        assert team.external_id == "2"

    async def test_an_unclaimed_import_still_creates_a_membership(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """A user may import a league they do not play in."""
        from app.repositories.user import UserRepository

        await seed_players(session, ALL_FIXTURE_PLAYERS)
        user = await UserRepository(session).get_or_create("watcher", "w@example.com")

        await run_import(session, user_id=user.id)

        membership = (await session.execute(select(LeagueMembership))).scalar_one()
        assert membership.team_id is None


class TestCancellation:
    async def test_cancelling_stops_at_a_checkpoint(
        self, session: AsyncSession, stub_adapter: StubAdapter
    ) -> None:
        """Cooperative: the league is written, later stages are not."""
        from app.workers.base import JobCancelled

        await seed_players(session, ALL_FIXTURE_PLAYERS)
        context = FakeContext(session, await make_job_row(session))
        context.cancel_after = 2  # after settings and teams

        with pytest.raises(JobCancelled):
            await league_import.import_league(  # type: ignore[arg-type]
                context, {"external_league_id": "1124589302838374400"}
            )

        assert (await session.execute(select(func.count()).select_from(Team))).scalar_one() == 2
        assert (await session.execute(select(func.count()).select_from(Matchup))).scalar_one() == 0


class TestPlayerMapper:
    async def test_the_crosswalk_resolves_ids(self, session: AsyncSession) -> None:
        mapping = await seed_players(session, ["4034", "6794"])
        mapper = PlayerMapper(session)

        result = await mapper.resolve_many(["4034", "6794", "unknown"])

        assert result.resolved == {"4034": mapping["4034"], "6794": mapping["6794"]}
        assert result.unresolved == ["unknown"]

    async def test_bootstrapping_links_an_unambiguous_name(self, session: AsyncSession) -> None:
        session.add(Player(full_name="Christian McCaffrey", position="RB"))
        await session.flush()
        mapper = PlayerMapper(session)

        result = await mapper.link_from_export(
            [ProviderPlayer(external_id="4034", full_name="Christian McCaffrey", position="RB")]
        )

        assert "4034" in result.resolved
        assert await mapper.resolve("4034") is not None

    async def test_an_ambiguous_name_is_left_unresolved(self, session: AsyncSession) -> None:
        """Guessing would put another player's projections on a user's roster."""
        session.add_all(
            [
                Player(full_name="Mike Williams", position="WR"),
                Player(full_name="Mike Williams", position="WR"),
            ]
        )
        await session.flush()
        mapper = PlayerMapper(session)

        result = await mapper.link_from_export(
            [ProviderPlayer(external_id="9001", full_name="Mike Williams", position="WR")]
        )

        assert result.unresolved == ["9001"]
        assert await mapper.resolve("9001") is None

    async def test_position_disambiguates_a_shared_name(self, session: AsyncSession) -> None:
        session.add_all(
            [
                Player(full_name="Josh Allen", position="QB"),
                Player(full_name="Josh Allen", position="LB"),
            ]
        )
        await session.flush()
        mapper = PlayerMapper(session)

        result = await mapper.link_from_export(
            [ProviderPlayer(external_id="4984", full_name="Josh Allen", position="QB")]
        )

        assert "4984" in result.resolved

    async def test_an_existing_link_is_never_repointed(self, session: AsyncSession) -> None:
        """Every historical stat row already joined through the original."""
        first = Player(full_name="First", position="RB")
        second = Player(full_name="Second", position="RB")
        session.add_all([first, second])
        await session.flush()

        mapper = PlayerMapper(session)
        await mapper.link("4034", first.id)
        await session.flush()

        fresh = PlayerMapper(session)  # a new mapper, so no cached answer
        await fresh.link("4034", second.id)
        await session.flush()

        assert await PlayerMapper(session).resolve("4034") == first.id


def _expected_count(model: Any) -> int:
    return {
        League.__name__: 1,
        Team.__name__: 2,
        # One pairing. The fixture's third entry is a bye for a roster that
        # is not in the league's team list, and is correctly skipped -- see
        # test_a_matchup_naming_an_unknown_team_is_skipped.
        Matchup.__name__: 1,
        Transaction.__name__: 3,
    }[model.__name__]
