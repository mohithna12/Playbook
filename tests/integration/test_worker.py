"""The worker runtime, driven end to end.

``run_job`` is invoked directly rather than through an ARQ worker process.
What matters is the contract between the runtime and the jobs table -- claim,
progress, terminal state -- and starting a real worker would test ARQ's
scheduler rather than any of that.

Redis is not required: publishing fails open, so a job runs to completion with
no broker. The SSE side of that is covered separately.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.domain.jobs import JobKind
from app.repositories.job import JobRepository
from app.repositories.user import UserRepository
from app.workers import echo as _echo  # noqa: F401 - registers the echo handler
from app.workers.base import JobCancelled, JobContext, register, run_job

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration


SUBJECT_PREFIX = "wrk_"


@pytest.fixture
async def worker_session(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    """A session that really commits, plus the same sessionmaker for the worker.

    The suite's usual ``session`` fixture is bound to a transaction that is
    rolled back, which is what keeps tests independent -- but it cannot be used
    here. ``run_job`` opens its *own* connection and commits between the claim
    and the handler, so a row written inside an uncommitted transaction is
    invisible to it, and every job looks unclaimable.

    So these tests commit for real and clean up after themselves. Rows are
    namespaced by ``SUBJECT_PREFIX`` so the teardown cannot touch anything
    another test created.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.workers import base

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(base, "get_sessionmaker", lambda: sessionmaker)

    async with sessionmaker() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(
                text(
                    "DELETE FROM jobs WHERE user_id IN "
                    "(SELECT id FROM users WHERE auth_subject LIKE :prefix)"
                ),
                {"prefix": f"{SUBJECT_PREFIX}%"},
            )
            await session.execute(
                text("DELETE FROM users WHERE auth_subject LIKE :prefix"),
                {"prefix": f"{SUBJECT_PREFIX}%"},
            )
            await session.commit()


async def make_job(
    session: AsyncSession, subject: str, kind: JobKind = JobKind.ECHO, **params: Any
) -> uuid.UUID:
    name = f"{SUBJECT_PREFIX}{subject}"
    user = await UserRepository(session).get_or_create(name, f"{name}@example.com")
    job, _ = await JobRepository(session).create(user_id=user.id, kind=kind, params=params or {})
    await session.commit()
    return job.id


async def read_job(session: AsyncSession, job_id: uuid.UUID) -> Any:
    await session.rollback()  # start a fresh transaction to see the worker's commits
    return await JobRepository(session).get(job_id)


class TestSuccessfulRun:
    async def test_a_job_runs_to_succeeded(self, worker_session: AsyncSession) -> None:
        job_id = await make_job(worker_session, "worker_ok_1", message="hello", steps=2)

        status = await run_job({}, str(job_id), str(JobKind.ECHO), {"message": "hello", "steps": 2})

        assert status == "SUCCEEDED"
        job = await read_job(worker_session, job_id)
        assert job.status == "SUCCEEDED"
        assert job.progress_pct == 100
        assert job.result_ref == {"message": "hello", "steps": 2}
        assert job.attempts == 1

    async def test_progress_lands_on_the_row(self, worker_session: AsyncSession) -> None:
        """A polling client and a reconnecting one both read the row, not Redis."""
        job_id = await make_job(worker_session, "worker_ok_2", steps=4)

        await run_job({}, str(job_id), str(JobKind.ECHO), {"steps": 4})

        job = await read_job(worker_session, job_id)
        assert job.progress_step == "step_4_of_4"

    async def test_the_error_column_stays_empty_on_success(
        self, worker_session: AsyncSession
    ) -> None:
        job_id = await make_job(worker_session, "worker_ok_3")

        await run_job({}, str(job_id), str(JobKind.ECHO), {})

        assert (await read_job(worker_session, job_id)).error is None


class TestFailure:
    async def test_a_raising_handler_fails_the_job_rather_than_the_worker(
        self, worker_session: AsyncSession
    ) -> None:
        """A handler exception is an outcome to record, not a crash to propagate."""

        @register(JobKind.CACHE_WARM_LEAGUE)
        async def _boom(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("upstream exploded")

        job_id = await make_job(worker_session, "worker_fail_1", kind=JobKind.CACHE_WARM_LEAGUE)

        status = await run_job({}, str(job_id), str(JobKind.CACHE_WARM_LEAGUE), {})

        assert status == "FAILED"
        job = await read_job(worker_session, job_id)
        assert job.status == "FAILED"
        assert job.error == {"type": "ValueError", "message": "upstream exploded"}

    async def test_a_failed_job_records_its_attempt(self, worker_session: AsyncSession) -> None:
        """The retry budget only works if failures count against it."""

        @register(JobKind.TRADE_ANALYZE)
        async def _boom(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("nope")

        job_id = await make_job(worker_session, "worker_fail_2", kind=JobKind.TRADE_ANALYZE)

        await run_job({}, str(job_id), str(JobKind.TRADE_ANALYZE), {})

        assert (await read_job(worker_session, job_id)).attempts == 1


class TestClaiming:
    async def test_a_cancelled_job_is_never_started(self, worker_session: AsyncSession) -> None:
        """Cancelling while queued must actually prevent the work."""
        ran = False

        @register(JobKind.SIMULATION_RUN)
        async def _track(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal ran
            ran = True
            return {}

        job_id = await make_job(worker_session, "worker_claim_1", kind=JobKind.SIMULATION_RUN)
        await JobRepository(worker_session).cancel(job_id)
        await worker_session.commit()

        status = await run_job({}, str(job_id), str(JobKind.SIMULATION_RUN), {})

        assert status == "SKIPPED"
        assert ran is False

    async def test_a_duplicate_delivery_does_not_run_twice(
        self, worker_session: AsyncSession
    ) -> None:
        """The QUEUED -> RUNNING guard is what makes at-least-once safe."""
        runs = 0

        @register(JobKind.LEAGUE_INCREMENTAL_SYNC)
        async def _count(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal runs
            runs += 1
            return {}

        job_id = await make_job(
            worker_session, "worker_claim_2", kind=JobKind.LEAGUE_INCREMENTAL_SYNC
        )
        kind = str(JobKind.LEAGUE_INCREMENTAL_SYNC)

        assert await run_job({}, str(job_id), kind, {}) == "SUCCEEDED"
        assert await run_job({}, str(job_id), kind, {}) == "SKIPPED"
        assert runs == 1


class TestCancellationDuringRun:
    async def test_a_handler_checkpoint_observes_cancellation(
        self, worker_session: AsyncSession
    ) -> None:
        """Cooperative cancellation: the handler stops where it chose to."""
        steps_completed = 0

        @register(JobKind.LEAGUE_FULL_IMPORT)
        async def _cancellable(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal steps_completed
            for _ in range(5):
                await context.check_cancelled()
                steps_completed += 1
                if steps_completed == 2:
                    # Stand in for the user's DELETE landing mid-run.
                    repo = JobRepository(context.session)
                    await repo.cancel(context.job_id)
                    await context.session.commit()
            return {}

        job_id = await make_job(worker_session, "worker_cancel_1", kind=JobKind.LEAGUE_FULL_IMPORT)

        status = await run_job({}, str(job_id), str(JobKind.LEAGUE_FULL_IMPORT), {})

        assert status == "CANCELLED"
        assert steps_completed == 2  # stopped at the next checkpoint, not later
        assert (await read_job(worker_session, job_id)).status == "CANCELLED"

    async def test_job_cancelled_is_not_recorded_as_an_error(
        self, worker_session: AsyncSession
    ) -> None:
        """A user-requested stop is a normal outcome, not a failure to debug."""

        @register(JobKind.EXPLANATION_GENERATE)
        async def _immediate(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
            raise JobCancelled

        job_id = await make_job(
            worker_session, "worker_cancel_2", kind=JobKind.EXPLANATION_GENERATE
        )

        await run_job({}, str(job_id), str(JobKind.EXPLANATION_GENERATE), {})

        job = await read_job(worker_session, job_id)
        assert job.status == "CANCELLED"
        assert job.error is None


class TestProgressBounds:
    async def test_progress_is_clamped(self, worker_session: AsyncSession) -> None:
        """A handler's arithmetic must not be able to write an invalid percent."""

        @register(JobKind.CACHE_WARM_LEAGUE)
        async def _overshoot(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
            await context.report_progress(150, "over")
            return {}

        job_id = await make_job(worker_session, "worker_bounds_1", kind=JobKind.CACHE_WARM_LEAGUE)

        await run_job({}, str(job_id), str(JobKind.CACHE_WARM_LEAGUE), {})

        assert (await read_job(worker_session, job_id)).progress_pct == 100
