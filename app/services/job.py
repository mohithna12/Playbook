"""Job orchestration: enqueue, poll, stream, cancel.

The enqueue path is a dual write -- a row in Postgres and a message in Redis --
and the order is load-bearing. The row goes first, so a Redis failure leaves a
QUEUED job the reaper will find. Enqueueing first would risk the mirror image:
a worker running a job with no durable record, invisible to every status query
(RFC 7.2).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import TYPE_CHECKING, Any

import structlog

from app.core import sse
from app.core.errors import NotFoundError
from app.core.telemetry import current_trace_id
from app.domain.jobs import JobKind, is_terminal, policy_for
from app.repositories.job import JobRepository
from app.schemas.job import JobCompleteEvent, JobProgressEvent, JobStatusResponse

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

# How often the SSE stream re-reads the job row. Progress arrives by pub/sub;
# this is the backstop that notices a job that finished while Redis was
# unavailable, so a stream cannot hang forever on a job that is already done.
STATUS_POLL_SECONDS = 2.0


class JobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = JobRepository(session)

    async def enqueue(
        self,
        *,
        user_id: uuid.UUID | None,
        kind: JobKind,
        params: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[JobStatusResponse, bool]:
        """Create and enqueue a job. Returns ``(job, created)``.

        A replayed idempotency key returns the original job without enqueueing
        again -- the contract is that replaying a key within 24h returns the
        original job, not that it starts a second one (RFC 12.1).
        """
        job, created = await self._jobs.create(
            user_id=user_id,
            kind=kind,
            params=params,
            idempotency_key=idempotency_key,
            # Captured at enqueue and restored in the worker, so the async
            # boundary does not split one user action into two unrelated
            # traces.
            trace_id=current_trace_id() or None,
        )

        if created:
            await self._session.commit()
            await self._submit(job.id, kind, params, job.trace_id)

        return JobStatusResponse.model_validate(job), created

    async def _submit(
        self,
        job_id: uuid.UUID,
        kind: JobKind,
        params: dict[str, Any],
        trace_id: str | None,
    ) -> None:
        """Hand the job to ARQ. A failure here is recoverable, so it is logged.

        The row is already committed as QUEUED. If the broker is unreachable
        the reaper re-enqueues it once the deadline passes, which is slower
        than failing loudly but strictly better than losing the work.
        """
        from app.workers.queue import enqueue_job

        try:
            await enqueue_job(job_id=job_id, kind=kind, params=params, trace_id=trace_id)
        except Exception as exc:
            await logger.aerror(
                "job_enqueue_failed_will_be_reaped",
                job_id=str(job_id),
                kind=str(kind),
                error=str(exc),
            )

    async def get(self, job_id: uuid.UUID, user_id: uuid.UUID | None) -> JobStatusResponse:
        job = await self._jobs.get(job_id, user_id=user_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found")
        return JobStatusResponse.model_validate(job)

    async def cancel(self, job_id: uuid.UUID, user_id: uuid.UUID | None) -> JobStatusResponse:
        """Cancel a job. 404 if it is not the caller's, 409 if already finished."""
        job = await self._jobs.get(job_id, user_id=user_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found")

        cancelled = await self._jobs.cancel(job_id, user_id=user_id)
        if not cancelled:
            from app.core.errors import ConflictError

            raise ConflictError(f"Job {job_id} has already finished with status {job.status}")

        await self._session.commit()
        await sse.publish(job_id, "complete", JobCompleteEvent(status="CANCELLED").model_dump())

        refreshed = await self._jobs.get(job_id, user_id=user_id)
        assert refreshed is not None
        return JobStatusResponse.model_validate(refreshed)

    async def stream(self, job_id: uuid.UUID, user_id: uuid.UUID | None) -> AsyncIterator[str]:
        """Yield SSE frames for a job until it reaches a terminal state.

        A job that is already finished when the client connects gets its
        terminal event immediately rather than an open connection that never
        speaks -- the common case for a fast job whose client reconnects.
        """
        job = await self._jobs.get(job_id, user_id=user_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found")

        if is_terminal(str(job.status)):
            yield sse.format_event("complete", _complete_event(job).model_dump())
            return

        yield sse.format_event(
            "progress",
            JobProgressEvent(pct=job.progress_pct, step=job.progress_step).model_dump(),
        )

        last_poll = asyncio.get_running_loop().time()

        async for event, data in sse.subscribe(job_id):
            now = asyncio.get_running_loop().time()

            if event == "_idle":
                yield sse.heartbeat()
                # The pub/sub message may have been published while this
                # connection was being set up, or dropped entirely if Redis
                # restarted. Re-reading the row bounds how long that costs.
                if now - last_poll >= STATUS_POLL_SECONDS:
                    last_poll = now
                    current = await self._refresh(job_id, user_id)
                    if current is not None and is_terminal(str(current.status)):
                        yield sse.format_event("complete", _complete_event(current).model_dump())
                        return
                continue

            yield sse.format_event(event, data)
            if event == "complete":
                return

        # The subscription ended without a terminal event, which means Redis
        # was unavailable. Fall back to the row so the client still learns the
        # outcome instead of hanging.
        final = await self._refresh(job_id, user_id)
        if final is not None and is_terminal(str(final.status)):
            yield sse.format_event("complete", _complete_event(final).model_dump())

    async def _refresh(self, job_id: uuid.UUID, user_id: uuid.UUID | None) -> Any:
        """Re-read a job on a fresh transaction so it sees other writers' commits."""
        await self._session.rollback()
        return await self._jobs.get(job_id, user_id=user_id)


def _complete_event(job: Any) -> JobCompleteEvent:
    return JobCompleteEvent(
        status=str(job.status),
        result_ref=job.result_ref,
        error=job.error,
    )


def deadline_for(kind: JobKind) -> dt.datetime:
    """A fresh deadline for a requeued job."""
    return dt.datetime.now(dt.UTC) + dt.timedelta(seconds=policy_for(kind).timeout_seconds)


__all__ = ["STATUS_POLL_SECONDS", "JobService", "deadline_for"]
