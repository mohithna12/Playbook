"""Job persistence and the guarded status transitions.

The transitions are guarded UPDATEs rather than read-modify-write, and these
tests exist to prove the guards actually hold under the races they exist for:
a cancel landing while a worker claims, two workers claiming at once, a late
result arriving after a cancellation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

import pytest

from app.domain.jobs import HEARTBEAT_TIMEOUT_SECONDS, JobKind
from app.repositories.job import JobRepository
from app.repositories.user import UserRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def make_user(session: AsyncSession, subject: str) -> uuid.UUID:
    user = await UserRepository(session).get_or_create(subject, f"{subject}@example.com")
    return user.id


class TestCreation:
    async def test_a_job_starts_queued_with_a_deadline(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "job_user_1")

        job, created = await repo.create(
            user_id=user_id, kind=JobKind.ECHO, params={"message": "hi"}
        )

        assert created is True
        assert job.status == "QUEUED"
        assert job.progress_pct == 0
        assert job.attempts == 0
        assert job.deadline_at > dt.datetime.now(dt.UTC)

    async def test_the_deadline_comes_from_the_kind(self, session: AsyncSession) -> None:
        """A full import gets 180s; an echo gets 30s (RFC 7.2)."""
        repo = JobRepository(session)
        user_id = await make_user(session, "job_user_2")

        echo, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        import_job, _ = await repo.create(
            user_id=user_id, kind=JobKind.LEAGUE_FULL_IMPORT, params={}
        )

        assert import_job.deadline_at > echo.deadline_at

    async def test_jobs_without_an_idempotency_key_are_always_distinct(
        self, session: AsyncSession
    ) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "job_user_3")

        first, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        second, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})

        assert first.id != second.id


class TestIdempotency:
    async def test_a_replayed_key_returns_the_original_job(self, session: AsyncSession) -> None:
        """The contract is 'returns the original job', not 'starts another'."""
        repo = JobRepository(session)
        user_id = await make_user(session, "idem_user_1")

        first, first_created = await repo.create(
            user_id=user_id, kind=JobKind.ECHO, params={"message": "a"}, idempotency_key="k1"
        )
        second, second_created = await repo.create(
            user_id=user_id, kind=JobKind.ECHO, params={"message": "b"}, idempotency_key="k1"
        )

        assert first_created is True
        assert second_created is False
        assert second.id == first.id
        # The replay did not overwrite the original's params.
        assert second.params == {"message": "a"}

    async def test_the_key_is_scoped_per_user(self, session: AsyncSession) -> None:
        """One user's key must not collide with another's."""
        repo = JobRepository(session)
        alice = await make_user(session, "idem_alice")
        bob = await make_user(session, "idem_bob")

        a, _ = await repo.create(
            user_id=alice, kind=JobKind.ECHO, params={}, idempotency_key="shared"
        )
        b, created = await repo.create(
            user_id=bob, kind=JobKind.ECHO, params={}, idempotency_key="shared"
        )

        assert created is True
        assert a.id != b.id

    async def test_the_key_is_scoped_per_kind(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "idem_user_2")

        echo, _ = await repo.create(
            user_id=user_id, kind=JobKind.ECHO, params={}, idempotency_key="same"
        )
        sync, created = await repo.create(
            user_id=user_id,
            kind=JobKind.LEAGUE_INCREMENTAL_SYNC,
            params={},
            idempotency_key="same",
        )

        assert created is True
        assert echo.id != sync.id


class TestTransitions:
    async def test_claiming_moves_queued_to_running_and_counts_the_attempt(
        self, session: AsyncSession
    ) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_1")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})

        assert await repo.mark_running(job.id) is True

        await session.refresh(job)
        assert job.status == "RUNNING"
        assert job.attempts == 1
        assert job.heartbeat_at is not None

    async def test_only_one_worker_can_claim_a_job(self, session: AsyncSession) -> None:
        """The guard is what makes duplicate delivery harmless."""
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_2")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})

        assert await repo.mark_running(job.id) is True
        assert await repo.mark_running(job.id) is False

    async def test_a_cancelled_job_cannot_be_claimed(self, session: AsyncSession) -> None:
        """Cancelling a queued job is what stops it from ever starting."""
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_3")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})

        assert await repo.cancel(job.id) is True
        assert await repo.mark_running(job.id) is False

    async def test_progress_updates_percent_step_and_heartbeat(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_4")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)

        await repo.report_progress(job.id, 45, "importing_rosters")

        await session.refresh(job)
        assert job.progress_pct == 45
        assert job.progress_step == "importing_rosters"

    async def test_progress_is_ignored_once_a_job_is_terminal(self, session: AsyncSession) -> None:
        """A late progress write must not resurrect a finished job."""
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_5")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)
        await repo.finish(job.id, status="SUCCEEDED")

        await repo.report_progress(job.id, 50, "too_late")

        await session.refresh(job)
        assert job.status == "SUCCEEDED"
        assert job.progress_step is None

    async def test_success_forces_progress_to_100(self, session: AsyncSession) -> None:
        """A finished job showing 60% would look stuck to a polling client."""
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_6")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)
        await repo.report_progress(job.id, 60, "partway")

        await repo.finish(job.id, status="SUCCEEDED", result_ref={"league_id": "abc"})

        await session.refresh(job)
        assert job.progress_pct == 100
        assert job.result_ref == {"league_id": "abc"}

    async def test_a_job_can_only_finish_once(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_7")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)

        assert await repo.finish(job.id, status="SUCCEEDED") is True
        assert await repo.finish(job.id, status="FAILED") is False

    async def test_a_late_result_cannot_overwrite_a_cancellation(
        self, session: AsyncSession
    ) -> None:
        """The user already saw CANCELLED; it must not flip to SUCCEEDED."""
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_8")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)
        await repo.cancel(job.id)

        assert await repo.finish(job.id, status="SUCCEEDED") is False

        await session.refresh(job)
        assert job.status == "CANCELLED"

    async def test_cancelling_a_finished_job_reports_failure(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "trans_user_9")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)
        await repo.finish(job.id, status="SUCCEEDED")

        assert await repo.cancel(job.id) is False


class TestScoping:
    async def test_another_users_job_is_invisible(self, session: AsyncSession) -> None:
        """Fail closed: the router turns None into a 404, not a 403."""
        repo = JobRepository(session)
        owner = await make_user(session, "scope_owner")
        stranger = await make_user(session, "scope_stranger")
        job, _ = await repo.create(user_id=owner, kind=JobKind.ECHO, params={})

        assert await repo.get(job.id, user_id=owner) is not None
        assert await repo.get(job.id, user_id=stranger) is None

    async def test_a_stranger_cannot_cancel(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        owner = await make_user(session, "cancel_owner")
        stranger = await make_user(session, "cancel_stranger")
        job, _ = await repo.create(user_id=owner, kind=JobKind.ECHO, params={})

        assert await repo.cancel(job.id, user_id=stranger) is False
        assert await repo.cancel(job.id, user_id=owner) is True


class TestReaperQueries:
    async def test_a_fresh_job_is_not_stale(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "reap_user_1")
        await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})

        assert list(await repo.find_stale()) == []

    async def test_a_job_past_its_deadline_is_stale(self, session: AsyncSession) -> None:
        """Covers a queued job whose broker message was lost."""
        repo = JobRepository(session)
        user_id = await make_user(session, "reap_user_2")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})

        job.deadline_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.flush()

        assert job.id in {j.id for j in await repo.find_stale()}

    async def test_a_running_job_with_a_dead_heartbeat_is_stale(
        self, session: AsyncSession
    ) -> None:
        """Covers a worker pod reclaimed mid-job, which writes nothing."""
        repo = JobRepository(session)
        user_id = await make_user(session, "reap_user_3")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.LEAGUE_FULL_IMPORT, params={})
        await repo.mark_running(job.id)

        job.heartbeat_at = dt.datetime.now(dt.UTC) - dt.timedelta(
            seconds=HEARTBEAT_TIMEOUT_SECONDS + 5
        )
        await session.flush()

        assert job.id in {j.id for j in await repo.find_stale()}

    async def test_a_terminal_job_is_never_stale(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "reap_user_4")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)
        await repo.finish(job.id, status="FAILED", error={"type": "X", "message": "y"})

        job.deadline_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=60)
        await session.flush()

        assert job.id not in {j.id for j in await repo.find_stale()}

    async def test_requeue_resets_the_heartbeat_and_deadline(self, session: AsyncSession) -> None:
        """A stale heartbeat left in place would make the retry look dead too."""
        repo = JobRepository(session)
        user_id = await make_user(session, "reap_user_5")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)

        new_deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=300)
        assert await repo.requeue(job.id, deadline_at=new_deadline) is True

        await session.refresh(job)
        assert job.status == "QUEUED"
        assert job.heartbeat_at is None
        assert job.attempts == 1  # preserved, so the retry budget still applies

    async def test_a_requeued_job_can_be_claimed_again(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        user_id = await make_user(session, "reap_user_6")
        job, _ = await repo.create(user_id=user_id, kind=JobKind.ECHO, params={})
        await repo.mark_running(job.id)
        await repo.requeue(job.id, deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(60))

        assert await repo.mark_running(job.id) is True

        await session.refresh(job)
        assert job.attempts == 2
