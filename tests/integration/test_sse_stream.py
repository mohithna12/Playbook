"""The SSE job stream, end to end over real Redis pub/sub.

The stream has to hold three properties that only a live broker can show:

* a job that is already finished when the client connects gets its terminal
  event immediately, rather than an open connection that never speaks;
* progress published by a worker on one connection reaches a subscriber on
  another, which is what lets the connection land on any API pod;
* the stream ends on the terminal event rather than hanging.

These need Redis. The rest of the job suite deliberately does not.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.core import sse
from app.domain.jobs import JobKind
from app.repositories.job import JobRepository
from app.repositories.user import UserRepository
from app.services.job import JobService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("redis_client")]

SUBJECT_PREFIX = "sse_"
# Generous: the stream's idle tick is 1s and its status re-poll is 2s, so a
# tighter bound would make this flaky on a loaded CI runner rather than
# catching a real hang.
STREAM_TIMEOUT_SECONDS = 15


@pytest.fixture
async def sse_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A committing session. The stream re-reads rows on fresh transactions."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
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


async def make_job(session: AsyncSession, subject: str) -> tuple[uuid.UUID, uuid.UUID]:
    name = f"{SUBJECT_PREFIX}{subject}"
    user = await UserRepository(session).get_or_create(name, f"{name}@example.com")
    job, _ = await JobRepository(session).create(user_id=user.id, kind=JobKind.ECHO, params={})
    await session.commit()
    return job.id, user.id


async def collect(stream: AsyncIterator[str], until: str = "event: complete") -> list[str]:
    """Drain a stream until a frame contains ``until``. Fails rather than hangs."""
    frames: list[str] = []

    async def _drain() -> None:
        async for frame in stream:
            frames.append(frame)
            if until in frame:
                return

    await asyncio.wait_for(_drain(), timeout=STREAM_TIMEOUT_SECONDS)
    return frames


class TestAlreadyFinished:
    async def test_a_finished_job_completes_the_stream_immediately(
        self, sse_session: AsyncSession
    ) -> None:
        """A client reconnecting to a fast job must not get a silent connection."""
        job_id, user_id = await make_job(sse_session, "done_1")
        repo = JobRepository(sse_session)
        await repo.mark_running(job_id)
        await repo.finish(job_id, status="SUCCEEDED", result_ref={"league_id": "abc"})
        await sse_session.commit()

        frames = await collect(JobService(sse_session).stream(job_id, user_id))

        assert len(frames) == 1
        assert "event: complete" in frames[0]
        assert '"status":"SUCCEEDED"' in frames[0]
        assert '"league_id":"abc"' in frames[0]

    async def test_a_failed_job_reports_its_error(self, sse_session: AsyncSession) -> None:
        job_id, user_id = await make_job(sse_session, "done_2")
        repo = JobRepository(sse_session)
        await repo.mark_running(job_id)
        await repo.finish(job_id, status="FAILED", error={"type": "X", "message": "boom"})
        await sse_session.commit()

        frames = await collect(JobService(sse_session).stream(job_id, user_id))

        assert '"status":"FAILED"' in frames[0]
        assert "boom" in frames[0]


class TestLiveProgress:
    async def test_progress_published_elsewhere_reaches_the_stream(
        self, sse_session: AsyncSession
    ) -> None:
        """The cross-process hop: a worker publishes, this connection receives.

        This is what lets the SSE connection land on any API pod rather than
        the one that happens to share a process with the worker.
        """
        job_id, user_id = await make_job(sse_session, "live_1")
        await JobRepository(sse_session).mark_running(job_id)
        await sse_session.commit()

        stream = JobService(sse_session).stream(job_id, user_id)

        async def publish_later() -> None:
            await asyncio.sleep(0.3)
            await sse.publish(job_id, "progress", {"pct": 45, "step": "importing_rosters"})
            await asyncio.sleep(0.3)
            await sse.publish(job_id, "complete", {"status": "SUCCEEDED", "result_ref": None})

        publisher = asyncio.create_task(publish_later())
        try:
            frames = await collect(stream)
        finally:
            await publisher

        body = "".join(frames)
        assert "importing_rosters" in body
        assert '"pct":45' in body
        assert "event: complete" in body

    async def test_the_stream_opens_with_current_progress(self, sse_session: AsyncSession) -> None:
        """A late subscriber must not wait for the next event to learn where it is."""
        job_id, user_id = await make_job(sse_session, "live_2")
        repo = JobRepository(sse_session)
        await repo.mark_running(job_id)
        await repo.report_progress(job_id, 30, "already_underway")
        await sse_session.commit()

        stream = JobService(sse_session).stream(job_id, user_id)

        async def finish_later() -> None:
            await asyncio.sleep(0.3)
            await sse.publish(job_id, "complete", {"status": "SUCCEEDED", "result_ref": None})

        finisher = asyncio.create_task(finish_later())
        try:
            frames = await collect(stream)
        finally:
            await finisher

        assert "already_underway" in frames[0]
        assert '"pct":30' in frames[0]


class TestFallback:
    async def test_a_job_finishing_without_a_publish_still_ends_the_stream(
        self, sse_session: AsyncSession
    ) -> None:
        """Covers a publish lost to a Redis restart: the row is the backstop.

        Without the status re-poll the client would hold an open connection
        forever on a job that is already done.
        """
        job_id, user_id = await make_job(sse_session, "fallback_1")
        await JobRepository(sse_session).mark_running(job_id)
        await sse_session.commit()

        stream = JobService(sse_session).stream(job_id, user_id)

        async def finish_silently() -> None:
            # Terminal state written, nothing published -- the message is lost.
            await asyncio.sleep(0.3)
            from sqlalchemy.ext.asyncio import async_sessionmaker

            maker = async_sessionmaker(bind=sse_session.get_bind(), expire_on_commit=False)
            async with maker() as other:
                await JobRepository(other).finish(job_id, status="PARTIAL")
                await other.commit()

        finisher = asyncio.create_task(finish_silently())
        try:
            frames = await collect(stream)
        finally:
            await finisher

        body = "".join(frames)
        assert "event: complete" in body
        assert '"status":"PARTIAL"' in body

    async def test_heartbeats_are_emitted_while_waiting(self, sse_session: AsyncSession) -> None:
        """Without these the ALB closes an idle connection at 120s."""
        job_id, user_id = await make_job(sse_session, "fallback_2")
        await JobRepository(sse_session).mark_running(job_id)
        await sse_session.commit()

        stream = JobService(sse_session).stream(job_id, user_id)

        async def finish_later() -> None:
            await asyncio.sleep(2.5)
            await sse.publish(job_id, "complete", {"status": "SUCCEEDED", "result_ref": None})

        finisher = asyncio.create_task(finish_later())
        try:
            frames = await collect(stream)
        finally:
            await finisher

        assert sse.heartbeat() in frames


class TestScoping:
    async def test_a_stranger_cannot_stream_another_users_job(
        self, sse_session: AsyncSession
    ) -> None:
        from app.core.errors import NotFoundError

        job_id, _ = await make_job(sse_session, "scope_1")
        stranger = await UserRepository(sse_session).get_or_create(
            f"{SUBJECT_PREFIX}scope_stranger", f"{SUBJECT_PREFIX}stranger@example.com"
        )
        await sse_session.commit()

        stream = JobService(sse_session).stream(job_id, stranger.id)

        with pytest.raises(NotFoundError):
            await anext(stream)


class TestCancellation:
    async def test_cancelling_publishes_a_terminal_event(self, sse_session: AsyncSession) -> None:
        """A watching client should see the cancellation, not just stop hearing."""
        job_id, user_id = await make_job(sse_session, "cancel_1")
        await JobRepository(sse_session).mark_running(job_id)
        await sse_session.commit()

        service = JobService(sse_session)
        stream = service.stream(job_id, user_id)

        async def cancel_later() -> None:
            await asyncio.sleep(0.3)
            from sqlalchemy.ext.asyncio import async_sessionmaker

            maker = async_sessionmaker(bind=sse_session.get_bind(), expire_on_commit=False)
            async with maker() as other:
                await JobService(other).cancel(job_id, user_id)

        canceller = asyncio.create_task(cancel_later())
        try:
            frames = await collect(stream)
        finally:
            await canceller

        assert '"status":"CANCELLED"' in "".join(frames)


class TestPublishFailsOpen:
    async def test_publishing_to_a_dead_redis_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broker blip costs a live update, never the job."""
        import redis.asyncio as redis

        class Broken:
            async def publish(self, *_a: Any, **_k: Any) -> None:
                raise redis.ConnectionError("down")

        monkeypatch.setattr(sse, "get_redis", Broken)

        await sse.publish(uuid.uuid4(), "progress", {"pct": 1})
