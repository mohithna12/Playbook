"""Job persistence: creation with idempotency, transitions, and reaper sweeps.

The `jobs` table -- not Redis -- is the record of truth for job state (RFC
7.2). Redis is a broker that may lose data on an ElastiCache failover; a row
here is written before anything is enqueued, so a lost message becomes a job
the reaper can find rather than a job nobody ever hears about again.

Status transitions are guarded in SQL rather than read-modify-write. Two
workers racing on the same job, or a cancel arriving while a worker starts,
must not both win: each transition is an UPDATE with the expected current
status in its WHERE clause, and the row count says whether it applied.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.domain.jobs import HEARTBEAT_TIMEOUT_SECONDS, JobKind, policy_for
from app.models.job import Job
from app.repositories.base import Repository, rows_affected

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

ACTIVE_STATUSES = ("QUEUED", "RUNNING")


class JobRepository(Repository[Job]):
    model = Job

    async def create(
        self,
        *,
        user_id: uuid.UUID | None,
        kind: JobKind,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[Job, bool]:
        """Create a job row. Returns ``(job, created)``.

        ``created`` is False when an idempotency key replayed an existing job,
        which is what lets the caller skip enqueueing a duplicate.

        The uniqueness check is the database's, not a prior SELECT: two
        requests carrying the same key can arrive concurrently, and a
        check-then-insert would let both through. ON CONFLICT DO NOTHING plus a
        follow-up read makes the loser observe the winner's row.
        """
        policy = policy_for(kind)
        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=policy.timeout_seconds)

        values: dict[str, Any] = {
            "user_id": user_id,
            "kind": str(kind),
            "params": params,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
            "deadline_at": deadline,
        }

        if idempotency_key is None:
            job = Job(**values)
            self.session.add(job)
            await self.session.flush()
            return job, True

        statement = (
            insert(Job)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_job_idem")
            .returning(Job)
        )
        result = await self.session.execute(statement)
        created = result.scalar_one_or_none()
        if created is not None:
            await self.session.flush()
            return created, True

        existing = await self.get_by_idempotency_key(user_id, kind, idempotency_key)
        if existing is None:  # pragma: no cover - the row is guaranteed by the conflict
            raise RuntimeError("idempotency conflict resolved to no row")
        return existing, False

    async def get_by_idempotency_key(
        self, user_id: uuid.UUID | None, kind: JobKind, idempotency_key: str
    ) -> Job | None:
        result = await self.session.execute(
            select(Job).where(
                Job.user_id == user_id,
                Job.kind == str(kind),
                Job.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def get(self, job_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Job | None:
        """Fetch a job, scoped to its owner when one is given.

        Jobs are per-user, so the scoping is a direct ``user_id`` predicate
        rather than a membership join -- but the fail-closed rule is the same:
        another user's job id resolves to None, and the router turns that into
        a 404 rather than confirming the job exists.
        """
        statement = select(Job).where(Job.id == job_id)
        if user_id is not None:
            statement = statement.where(Job.user_id == user_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def mark_running(self, job_id: uuid.UUID) -> bool:
        """QUEUED -> RUNNING. False if the job was cancelled or already taken.

        The guard is what makes cancellation meaningful: a job cancelled while
        sitting in the queue must not start when a worker finally picks it up.
        """
        result = await self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "QUEUED")
            .values(
                status="RUNNING",
                attempts=Job.attempts + 1,
                heartbeat_at=func.now(),
                updated_at=func.now(),
            )
        )
        return rows_affected(result) == 1

    async def report_progress(self, job_id: uuid.UUID, pct: int, step: str | None = None) -> None:
        """Record progress and refresh the heartbeat in one write.

        Coupling them means a job that is visibly progressing cannot be reaped
        for a stale heartbeat, without the worker having to remember two calls.
        """
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "RUNNING")
            .values(
                progress_pct=pct,
                progress_step=step,
                heartbeat_at=func.now(),
                updated_at=func.now(),
            )
        )

    async def heartbeat(self, job_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "RUNNING")
            .values(heartbeat_at=func.now(), updated_at=func.now())
        )

    async def finish(
        self,
        job_id: uuid.UUID,
        *,
        status: str,
        result_ref: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        """Move a job to a terminal status. False if it was already terminal.

        Only an active job may finish, so a late-arriving worker result cannot
        overwrite a cancellation the user already saw.
        """
        values: dict[str, Any] = {
            "status": status,
            "result_ref": result_ref,
            "error": error,
            "updated_at": func.now(),
        }
        if status == "SUCCEEDED":
            values["progress_pct"] = 100

        result = await self.session.execute(
            update(Job).where(Job.id == job_id, Job.status.in_(ACTIVE_STATUSES)).values(**values)
        )
        return rows_affected(result) == 1

    async def cancel(self, job_id: uuid.UUID, user_id: uuid.UUID | None = None) -> bool:
        """Request cancellation. False if the job already finished.

        Cancelling a QUEUED job is immediate -- the worker's mark_running guard
        will refuse to start it. Cancelling a RUNNING job marks the intent; the
        worker notices at its next checkpoint.
        """
        statement = update(Job).where(Job.id == job_id, Job.status.in_(ACTIVE_STATUSES))
        if user_id is not None:
            statement = statement.where(Job.user_id == user_id)
        result = await self.session.execute(
            statement.values(status="CANCELLED", updated_at=func.now())
        )
        return rows_affected(result) == 1

    async def is_cancelled(self, job_id: uuid.UUID) -> bool:
        """Whether a worker should abandon this job at its next checkpoint."""
        result = await self.session.execute(select(Job.status).where(Job.id == job_id))
        return result.scalar_one_or_none() == "CANCELLED"

    async def find_stale(self, limit: int = 100) -> Sequence[Job]:
        """Active jobs that are past their deadline or have stopped heartbeating.

        Two separate conditions, because they catch different failures. A
        passed deadline means the job took longer than its kind allows -- a
        wedged upstream, say. A stale heartbeat means the worker died without
        getting to write anything, which is what a spot reclaim looks like.
        """
        now = dt.datetime.now(dt.UTC)
        heartbeat_cutoff = now - dt.timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)

        result = await self.session.execute(
            select(Job)
            .where(
                Job.status.in_(ACTIVE_STATUSES),
                (Job.deadline_at < now)
                | ((Job.status == "RUNNING") & (Job.heartbeat_at < heartbeat_cutoff)),
            )
            .order_by(Job.created_at)
            .limit(limit)
        )
        return result.scalars().all()

    async def requeue(self, job_id: uuid.UUID, deadline_at: dt.datetime) -> bool:
        """RUNNING -> QUEUED for a retry, with a fresh deadline.

        Clearing the heartbeat matters: leaving the dead worker's timestamp
        would make the requeued job look stale the moment it is picked up.
        """
        result = await self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(ACTIVE_STATUSES))
            .values(
                status="QUEUED",
                heartbeat_at=None,
                deadline_at=deadline_at,
                updated_at=func.now(),
            )
        )
        return rows_affected(result) == 1


__all__ = ["ACTIVE_STATUSES", "JobRepository"]
