"""The reconciliation sweep.

The reaper is the reason "Redis is the broker, Postgres is the record of
truth" is more than a slogan, so these tests drive the three failures it
exists for: a lost broker message, a worker pod that died mid-job, and a
genuine timeout. Each is simulated by writing the state that failure leaves
behind, because the failures themselves cannot be produced on demand.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.domain.jobs import HEARTBEAT_TIMEOUT_SECONDS, JobKind, policy_for
from app.repositories.job import JobRepository
from app.repositories.user import UserRepository
from app.workers.reaper import sweep

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration

SUBJECT_PREFIX = "reap_"


@pytest.fixture
async def reaper_session(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    """A committing session, shared with the reaper. See tests/integration/test_worker.py."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.workers import reaper

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(reaper, "get_sessionmaker", lambda: sessionmaker)

    # The broker is not running in tests; resubmission is exercised by
    # asserting the row's state, not by inspecting Redis.
    async def _noop_resubmit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(reaper, "_resubmit", _noop_resubmit)

    async with sessionmaker() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(
                text(
                    "DELETE FROM jobs WHERE user_id IN "
                    "(SELECT id FROM users WHERE auth_subject LIKE :p)"
                ),
                {"p": f"{SUBJECT_PREFIX}%"},
            )
            await session.execute(
                text("DELETE FROM users WHERE auth_subject LIKE :p"),
                {"p": f"{SUBJECT_PREFIX}%"},
            )
            await session.commit()


async def make_job(session: AsyncSession, subject: str, kind: JobKind = JobKind.ECHO) -> uuid.UUID:
    name = f"{SUBJECT_PREFIX}{subject}"
    user = await UserRepository(session).get_or_create(name, f"{name}@example.com")
    job, _ = await JobRepository(session).create(user_id=user.id, kind=kind, params={})
    await session.commit()
    return job.id


async def expire(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Backdate the deadline: what a lost broker message leaves behind."""
    job = await JobRepository(session).get(job_id)
    assert job is not None
    job.deadline_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    await session.commit()


async def kill_worker(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Backdate the heartbeat: what a reclaimed pod leaves behind."""
    job = await JobRepository(session).get(job_id)
    assert job is not None
    job.heartbeat_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS + 5)
    await session.commit()


async def reload(session: AsyncSession, job_id: uuid.UUID) -> Any:
    await session.rollback()
    return await JobRepository(session).get(job_id)


class TestHealthyJobs:
    async def test_a_fresh_job_is_left_alone(self, reaper_session: AsyncSession) -> None:
        await make_job(reaper_session, "healthy_1")

        assert await sweep() == {"requeued": 0, "failed": 0}

    async def test_a_heartbeating_job_is_left_alone(self, reaper_session: AsyncSession) -> None:
        """A job that is visibly working must never be reaped."""
        job_id = await make_job(reaper_session, "healthy_2", kind=JobKind.LEAGUE_FULL_IMPORT)
        await JobRepository(reaper_session).mark_running(job_id)
        await reaper_session.commit()

        assert await sweep() == {"requeued": 0, "failed": 0}

    async def test_a_finished_job_is_left_alone(self, reaper_session: AsyncSession) -> None:
        job_id = await make_job(reaper_session, "healthy_3")
        repo = JobRepository(reaper_session)
        await repo.mark_running(job_id)
        await repo.finish(job_id, status="SUCCEEDED")
        await reaper_session.commit()
        await expire(reaper_session, job_id)

        assert await sweep() == {"requeued": 0, "failed": 0}


class TestLostBrokerMessage:
    async def test_an_expired_queued_job_is_requeued(self, reaper_session: AsyncSession) -> None:
        """The ElastiCache-failover case: the row is QUEUED and nothing will run it."""
        job_id = await make_job(reaper_session, "lost_1", kind=JobKind.LEAGUE_FULL_IMPORT)
        await expire(reaper_session, job_id)

        assert await sweep() == {"requeued": 1, "failed": 0}

        job = await reload(reaper_session, job_id)
        assert job.status == "QUEUED"
        assert job.deadline_at > dt.datetime.now(dt.UTC)

    async def test_the_new_deadline_matches_the_kind(self, reaper_session: AsyncSession) -> None:
        job_id = await make_job(reaper_session, "lost_2", kind=JobKind.LEAGUE_FULL_IMPORT)
        await expire(reaper_session, job_id)

        await sweep()

        job = await reload(reaper_session, job_id)
        timeout = policy_for(JobKind.LEAGUE_FULL_IMPORT).timeout_seconds
        remaining = (job.deadline_at - dt.datetime.now(dt.UTC)).total_seconds()
        assert timeout - 30 < remaining <= timeout


class TestDeadWorker:
    async def test_a_stale_heartbeat_requeues_an_idempotent_job(
        self, reaper_session: AsyncSession
    ) -> None:
        """The spot-reclaim case. Safe because the work is upsert-based."""
        job_id = await make_job(reaper_session, "dead_1", kind=JobKind.LEAGUE_FULL_IMPORT)
        await JobRepository(reaper_session).mark_running(job_id)
        await reaper_session.commit()
        await kill_worker(reaper_session, job_id)

        assert await sweep() == {"requeued": 1, "failed": 0}

        job = await reload(reaper_session, job_id)
        assert job.status == "QUEUED"
        assert job.heartbeat_at is None
        assert job.attempts == 1  # the dead attempt still counts

    async def test_a_non_idempotent_job_is_failed_not_replayed(
        self, reaper_session: AsyncSession
    ) -> None:
        """Replaying an explanation would spend tokens without changing anything."""
        job_id = await make_job(reaper_session, "dead_2", kind=JobKind.EXPLANATION_GENERATE)
        await JobRepository(reaper_session).mark_running(job_id)
        await reaper_session.commit()
        await kill_worker(reaper_session, job_id)

        assert await sweep() == {"requeued": 0, "failed": 1}

        job = await reload(reaper_session, job_id)
        assert job.status == "FAILED"
        assert job.error is not None
        assert job.error["type"] == "JobExpired"


class TestRetryBudget:
    async def test_a_job_at_its_attempt_limit_is_failed(self, reaper_session: AsyncSession) -> None:
        """A job that has already died as often as its kind allows stays dead."""
        job_id = await make_job(reaper_session, "budget_1", kind=JobKind.SIMULATION_RUN)
        repo = JobRepository(reaper_session)
        await repo.mark_running(job_id)  # simulation.run allows 1 attempt
        await reaper_session.commit()
        await kill_worker(reaper_session, job_id)

        assert await sweep() == {"requeued": 0, "failed": 1}
        assert (await reload(reaper_session, job_id)).status == "FAILED"

    async def test_a_job_under_its_limit_is_requeued(self, reaper_session: AsyncSession) -> None:
        job_id = await make_job(reaper_session, "budget_2", kind=JobKind.LEAGUE_FULL_IMPORT)
        repo = JobRepository(reaper_session)
        await repo.mark_running(job_id)  # 1 of 3 attempts used
        await reaper_session.commit()
        await kill_worker(reaper_session, job_id)

        assert await sweep() == {"requeued": 1, "failed": 0}

    async def test_repeated_reaping_eventually_gives_up(self, reaper_session: AsyncSession) -> None:
        """A job that keeps dying must not be retried forever.

        Each cycle is one worker claiming the job and dying. The sweep
        requeues while the attempt budget lasts and fails on the attempt that
        exhausts it -- so the last cycle is where it gives up, not some later
        sweep.
        """
        max_attempts = policy_for(JobKind.LEAGUE_FULL_IMPORT).max_attempts
        job_id = await make_job(reaper_session, "budget_3", kind=JobKind.LEAGUE_FULL_IMPORT)
        repo = JobRepository(reaper_session)

        outcomes = []
        for _ in range(max_attempts):
            await repo.mark_running(job_id)
            await reaper_session.commit()
            await kill_worker(reaper_session, job_id)
            outcomes.append(await sweep())

        assert outcomes[:-1] == [{"requeued": 1, "failed": 0}] * (max_attempts - 1)
        assert outcomes[-1] == {"requeued": 0, "failed": 1}

        job = await reload(reaper_session, job_id)
        assert job.status == "FAILED"
        assert job.attempts == max_attempts

        # And it stays failed -- a later sweep must not pick it up again.
        assert await sweep() == {"requeued": 0, "failed": 0}


class TestIdempotenceOfTheSweep:
    async def test_a_second_sweep_finds_nothing(self, reaper_session: AsyncSession) -> None:
        """Concurrent reapers must not double-handle a job."""
        job_id = await make_job(reaper_session, "sweep_1", kind=JobKind.LEAGUE_FULL_IMPORT)
        await expire(reaper_session, job_id)

        assert await sweep() == {"requeued": 1, "failed": 0}
        assert await sweep() == {"requeued": 0, "failed": 0}

    async def test_an_unknown_kind_is_skipped_rather_than_failed(
        self, reaper_session: AsyncSession
    ) -> None:
        """A rollback to an older image must not let it fail a newer build's jobs."""
        job_id = await make_job(reaper_session, "sweep_2")
        job = await JobRepository(reaper_session).get(job_id)
        assert job is not None
        job.kind = "some.future.kind"
        await reaper_session.commit()
        await expire(reaper_session, job_id)

        assert await sweep() == {"requeued": 0, "failed": 0}
        assert (await reload(reaper_session, job_id)).status == "QUEUED"
