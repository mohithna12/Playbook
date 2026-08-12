"""The job reaper: reconciles the jobs table against reality.

This is what makes "Redis is the broker, Postgres is the record of truth" more
than a slogan. Three failures it recovers from, none of which the job itself
can report:

* An ElastiCache failover drops queued messages. The rows are still QUEUED and
  nothing will ever run them.
* A worker pod is reclaimed mid-job. The row is RUNNING with a heartbeat that
  stopped, and no one will ever write its terminal state.
* A job genuinely exceeds its kind's timeout, because something upstream is
  wedged.

An idempotent kind is re-enqueued with a fresh deadline, up to its attempt
limit; a non-idempotent one is failed immediately, because replaying it could
double-charge or double-send. Past the attempt limit, everything fails -- a job
that has already died twice is a job that will die again (RFC 7.2).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.db import get_sessionmaker
from app.domain.jobs import JobKind, policy_for
from app.repositories.job import JobRepository
from app.services.job import deadline_for

logger = structlog.get_logger()

SWEEP_LIMIT = 100


async def reap(ctx: dict[str, Any]) -> dict[str, int]:
    """ARQ cron entry point. Signature is ARQ's; the logic is in `sweep`."""
    return await sweep()


async def sweep() -> dict[str, int]:
    """One sweep. Returns counts, so the cron log says what it did.

    Safe to run concurrently on several workers: every transition is a guarded
    UPDATE, so two reapers racing on the same job means one of them gets a
    row count of zero and moves on.
    """
    sessionmaker = get_sessionmaker()
    requeued = 0
    failed = 0

    async with sessionmaker() as session:
        jobs = JobRepository(session)
        stale = await jobs.find_stale(limit=SWEEP_LIMIT)

        for job in stale:
            try:
                kind = JobKind(job.kind)
            except ValueError:
                # A kind this build does not know about -- a rollback with an
                # older image, say. Leave it: a newer worker may still own it,
                # and failing another version's job is worse than waiting.
                await logger.awarning(
                    "reaper_skipped_unknown_kind", job_id=str(job.id), kind=job.kind
                )
                continue

            policy = policy_for(kind)
            retriable = policy.idempotent and job.attempts < policy.max_attempts

            if retriable:
                if await jobs.requeue(job.id, deadline_at=deadline_for(kind)):
                    requeued += 1
                    await _resubmit(job.id, kind, job.params, job.trace_id)
                    await logger.ainfo(
                        "reaper_requeued",
                        job_id=str(job.id),
                        kind=job.kind,
                        attempts=job.attempts,
                    )
            elif await jobs.finish(
                job.id,
                status="FAILED",
                error={
                    "type": "JobExpired",
                    "message": (
                        "Job exceeded its deadline or stopped reporting progress, "
                        f"after {job.attempts} attempt(s)."
                    ),
                },
            ):
                failed += 1
                await logger.awarning(
                    "reaper_failed_job",
                    job_id=str(job.id),
                    kind=job.kind,
                    attempts=job.attempts,
                    idempotent=policy.idempotent,
                )

        await session.commit()

    if requeued or failed:
        await logger.ainfo("reaper_sweep", requeued=requeued, failed=failed)
    return {"requeued": requeued, "failed": failed}


async def _resubmit(
    job_id: Any, kind: JobKind, params: dict[str, Any], trace_id: str | None
) -> None:
    """Put a requeued job back on the broker. Logged, never raised.

    If this fails the row is QUEUED again with a fresh deadline, so the next
    sweep will retry it. A broker that is still down should not take the
    reaper down with it.
    """
    from app.workers.queue import enqueue_job

    try:
        await enqueue_job(job_id=job_id, kind=kind, params=params, trace_id=trace_id)
    except Exception as exc:
        await logger.aerror("reaper_resubmit_failed", job_id=str(job_id), error=str(exc))


__all__ = ["SWEEP_LIMIT", "reap"]
