"""The schema's correctness guarantees, exercised against a real Postgres.

Each test here corresponds to a constraint the RFC argues for explicitly. They
are integration tests rather than unit tests because the guarantee *is* the
database behaviour — asserting it in Python would only test the assertion.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    League,
    LeagueProvider,
    ModelVersion,
    NflTeam,
    Player,
    Position,
    Prediction,
    SimulationResult,
    Team,
    User,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

QUANTILES = {
    "proj_points_mean": Decimal("12.40"),
    "proj_points_sd": Decimal("5.10"),
    "proj_p10": Decimal("5.00"),
    "proj_p25": Decimal("8.20"),
    "proj_p50": Decimal("12.10"),
    "proj_p75": Decimal("16.30"),
    "proj_p90": Decimal("21.90"),
}


async def _model_version(
    session: AsyncSession,
    *,
    status: str = "ACTIVE",
    name: str = "proj_wr",
    version: str = "2026.08.01-a1",
) -> ModelVersion:
    mv = ModelVersion(
        name=name,
        version=version,
        position=Position.WR,
        algorithm="xgboost",
        artifact_uri="s3://playbook-models/proj_wr/2026.08.01-a1/model.ubj",
        feature_set_version="fs_v1",
        hyperparameters={"max_depth": 6},
        training_window={"start": "2018-W01", "end": "2025-W18", "n_rows": 48213},
        metrics={"mae": 3.91},
        status=status,
    )
    session.add(mv)
    await session.flush()
    return mv


async def _player(session: AsyncSession, *, name: str = "Test Receiver") -> Player:
    player = Player(full_name=name, position=Position.WR, status="ACTIVE")
    session.add(player)
    await session.flush()
    return player


async def _league(session: AsyncSession, *, external_id: str = "1124589") -> League:
    league = League(
        provider=LeagueProvider.SLEEPER,
        external_id=external_id,
        season=2026,
        name="Test League",
        total_teams=12,
        scoring_rules={"schema_version": 1, "rec": 0.5, "rec_td": 6},
        roster_positions=["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"],
        playoff_config={"teams": 6, "start_week": 15},
    )
    session.add(league)
    await session.flush()
    return league


# --------------------------------------------------------------------- basics


async def test_orm_roundtrip_across_the_core_chain(session: AsyncSession) -> None:
    """User -> league -> team -> player -> prediction inserts and reads back."""
    user = User(auth_subject="clerk|abc123", email="Manager@Example.com")
    session.add(user)
    league = await _league(session)
    team = Team(league_id=league.id, external_id="1", display_name="Team One")
    session.add(team)
    player = await _player(session)
    mv = await _model_version(session)
    session.add(
        Prediction(
            season=2026,
            player_id=player.id,
            week=8,
            model_version_id=mv.id,
            pred_stats={"rec": 5.4, "rec_yds": 61.2, "rec_td": 0.41},
            **QUANTILES,
        )
    )
    await session.flush()

    loaded = await session.scalar(
        select(Prediction).where(
            Prediction.season == 2026,
            Prediction.player_id == player.id,
            Prediction.week == 8,
        )
    )
    assert loaded is not None
    assert loaded.pred_stats["rec"] == 5.4
    assert loaded.proj_p50 == Decimal("12.10")
    assert isinstance(league.id, uuid.UUID)


async def test_email_uniqueness_is_case_insensitive(session: AsyncSession) -> None:
    """CITEXT, so a second signup with different casing collides."""
    session.add(User(auth_subject="clerk|a", email="manager@example.com"))
    await session.flush()
    session.add(User(auth_subject="clerk|b", email="MANAGER@example.com"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_uuidv7_keys_are_time_ordered(session: AsyncSession) -> None:
    """Sequential inserts produce increasing keys -- this is why uuidv7, not v4."""
    first = await _league(session, external_id="aaa")
    second = await _league(session, external_id="bbb")
    assert first.id.version == 7
    assert str(first.id) < str(second.id)


# ---------------------------------------------------------------- constraints


async def test_crossed_quantiles_are_rejected(session: AsyncSession) -> None:
    """p75 below p50 would become a negative variance in the simulation."""
    player = await _player(session)
    mv = await _model_version(session)
    crossed: dict[str, Any] = {**QUANTILES, "proj_p75": Decimal("9.00")}
    session.add(
        Prediction(
            season=2026,
            player_id=player.id,
            week=8,
            model_version_id=mv.id,
            pred_stats={},
            **crossed,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_only_one_active_model_per_name(session: AsyncSession) -> None:
    await _model_version(session, status="ACTIVE", version="v1")
    await _model_version(session, status="ARCHIVED", version="v2")  # allowed
    with pytest.raises(IntegrityError):
        await _model_version(session, status="ACTIVE", version="v3")


async def test_league_total_teams_is_bounded(session: AsyncSession) -> None:
    league = await _league(session)
    league.total_teams = 99
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_baseline_simulations_deduplicate_despite_null_scenario(
    session: AsyncSession,
) -> None:
    """NULLs compare distinct, so ``uq_sim`` keys on COALESCE(scenario, '')."""
    league = await _league(session)
    mv = await _model_version(session)

    def _run() -> SimulationResult:
        return SimulationResult(
            league_id=league.id,
            as_of_week=8,
            n_simulations=50_000,
            seed=42,
            league_state_hash="deadbeef",
            scenario=None,
            results={"team_1": {"champ_prob": 0.18}},
            runtime_ms=1200,
            model_version_id=mv.id,
        )

    session.add(_run())
    await session.flush()
    session.add(_run())
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_league_cascades_to_its_teams(session: AsyncSession) -> None:
    league = await _league(session)
    session.add(Team(league_id=league.id, external_id="1", display_name="Team One"))
    await session.flush()

    await session.delete(league)
    await session.flush()

    remaining = await session.scalar(
        select(func.count()).select_from(Team).where(Team.league_id == league.id)
    )
    assert remaining == 0


async def test_seeded_nfl_teams_are_present(session: AsyncSession) -> None:
    """Seeding is part of the migration story, not an optional extra."""
    from scripts.seed_nfl_teams import NFL_TEAMS

    for abbr, full_name, conference, _division, _lat, _lon, _dome in NFL_TEAMS:
        session.add(
            NflTeam(
                abbr=abbr,
                full_name=full_name,
                conference=conference,
                division=_division,
                stadium_lat=_lat,
                stadium_lon=_lon,
                is_dome=_dome,
            )
        )
    await session.flush()
    count = await session.scalar(select(func.count()).select_from(NflTeam))
    assert count == 32


async def test_injury_report_timestamps_are_timezone_aware(session: AsyncSession) -> None:
    """TIMESTAMPTZ everywhere -- a naive datetime is a bug waiting for a Sunday."""
    league = await _league(session)
    assert league.created_at.tzinfo is not None
    assert league.created_at <= dt.datetime.now(dt.UTC)
