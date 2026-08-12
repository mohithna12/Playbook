"""The worker runtime: one entry point that every job kind goes through.

A handler is an async function that takes a :class:`JobContext` and the job's
params and returns a ``result_ref``. Everything around it -- claiming the job,
heartbeating, publishing progress, recording the outcome -- happens here, once,
so a new job kind cannot forget to do any of it.

Three properties this is built to hold:

* **A job is claimed exactly once.** QUEUED -> RUNNING is a guarded UPDATE, so
  a duplicate delivery finds the row already claimed and returns instead of
  running the work twice.
* **A dead worker is detectable.** A background task heartbeats while the
  handler runs; if the pod is reclaimed mid-job the heartbeat stops and the
  reaper takes over.
* **Cancellation is observed.** Handlers call ``check_cancelled()`` at their
  own checkpoints; the runtime also checks before starting.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace

from app.core import sse
from app.core.db import get_sessionmaker
from app.domain.jobs import HEARTBEAT_INTERVAL_SECONDS, JobKind
from app.repositories.job import JobRepository
from app.schemas.job import JobCompleteEvent, JobProgressEvent

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)

PROGRESS_MAX = 100


class JobCancelled(Exception):  # noqa: N818 - control flow, not an error
    """Raised inside a handler when the user cancelled the job.

    Deliberately not ``JobCancelledError``: cancellation is a normal outcome
    the user asked for, and naming it an error would invite handlers to treat
    it as a failure to be logged and retried.
    """


@dataclass(slots=True)
class JobContext:
    """What a handler is given: identity, a session, and progress reporting."""

    job_id: uuid.UUID
    kind: JobKind
    session: AsyncSession
    _jobs: JobRepository

    async def report_progress(self, pct: int, step: str | None = None) -> None:
        """Record progress on the row and publish it to any live SSE stream.

        Both, not either. The row is what a polling client and a reconnecting
        client read; the publish is what makes an already-connected client see
        it now. Publishing alone would lose progress on reconnect.
        """
        bounded = max(0, min(int(pct), PROGRESS_MAX))
        await self._jobs.report_progress(self.job_id, bounded, step)
        await self.session.commit()
        await sse.publish(
            self.job_id, "progress", JobProgressEvent(pct=bounded, step=step).model_dump()
        )

    async def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` if the user cancelled. Call at checkpoints.

        Cooperative rather than pre-emptive: killing a task mid-write could
        leave a half-imported league, so handlers decide where it is safe to
        stop.
        """
        await self.session.rollback()  # see the cancellation the API committed
        if await self._jobs.is_cancelled(self.job_id):
            raise JobCancelled


Handler = "Callable[[JobContext, dict[str, Any]], Awaitable[dict[str, Any] | None]]"

_HANDLERS: dict[JobKind, Any] = {}


def register(kind: JobKind) -> Callable[[Any], Any]:
    """Register a handler for a job kind."""

    def decorator(fn: Any) -> Any:
        _HANDLERS[kind] = fn
        return fn

    return decorator


def handler_for(kind: JobKind) -> Any:
    return _HANDLERS[kind]


async def _heartbeat_loop(job_id: uuid.UUID, sessionmaker: Any) -> None:
    """Refresh the heartbeat until cancelled.

    Its own session, because the handler's is busy inside a transaction --
    sharing one would mean the heartbeat only lands when the handler happens
    to commit, which is exactly when it is least needed.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            async with sessionmaker() as session:
                await JobRepository(session).heartbeat(job_id)
                await session.commit()
        except Exception as exc:
            await logger.awarning("heartbeat_failed", job_id=str(job_id), error=str(exc))


async def run_job(
    ctx: dict[str, Any],
    job_id_str: str,
    kind_str: str,
    params: dict[str, Any],
    trace_id: str | None = None,
) -> str:
    """ARQ entry point. Returns the terminal status as a string.

    Never raises: a handler failure is recorded on the row and reported to the
    client. Letting it propagate would make ARQ retry outside our own retry
    accounting, and the row would disagree with reality.
    """
    import uuid as _uuid

    job_id = _uuid.UUID(job_id_str)
    kind = JobKind(kind_str)
    sessionmaker = get_sessionmaker()

    # Bind the originating trace so the async half of the request is the same
    # trace as the half the user waited on.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        job_id=job_id_str, job_kind=kind_str, trace_id=trace_id or ""
    )

    with tracer.start_as_current_span(f"job.{kind_str}"):
        async with sessionmaker() as session:
            jobs = JobRepository(session)

            if not await jobs.mark_running(job_id):
                # Cancelled while queued, or already claimed by another worker.
                await session.rollback()
                await logger.ainfo("job_not_claimable")
                return "SKIPPED"
            await session.commit()

            context = JobContext(job_id=job_id, kind=kind, session=session, _jobs=jobs)
            heartbeat = asyncio.create_task(_heartbeat_loop(job_id, sessionmaker))

            status = "FAILED"
            result_ref: dict[str, Any] | None = None
            error: dict[str, Any] | None = None

            try:
                await logger.ainfo("job_started")
                result_ref = await handler_for(kind)(context, params)
                status = "SUCCEEDED"
            except JobCancelled:
                status = "CANCELLED"
                await logger.ainfo("job_cancelled")
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
                await logger.aerror("job_failed", exc_info=exc, error=error)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

            await session.rollback()
            recorded = await jobs.finish(job_id, status=status, result_ref=result_ref, error=error)
            await session.commit()

            if recorded:
                await sse.publish(
                    job_id,
                    "complete",
                    JobCompleteEvent(
                        status=status,
                        result_ref=result_ref,
                        error=error,
                    ).model_dump(),
                )
            await logger.ainfo("job_finished", status=status)
            return status


__all__ = [
    "JobCancelled",
    "JobContext",
    "handler_for",
    "register",
    "run_job",
]
