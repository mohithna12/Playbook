"""The ARQ connection used to enqueue work from the API process.

Kept apart from the worker runtime so the API never imports handler code. The
API's job is to put a message on a queue; what executes it lives in the worker
image and may not even be installed here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

if TYPE_CHECKING:
    import uuid

    from arq.connections import ArqRedis

    from app.domain.jobs import JobKind

TASK_NAME = "run_job"

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    """ARQ's Redis config, derived from the same URL everything else uses.

    ARQ takes its own settings object rather than a client, so this is the one
    place the URL is translated; the queue lives in its own logical database so
    flushing the cache can never drop queued work.
    """
    settings = get_settings()
    parsed = RedisSettings.from_dsn(settings.redis_url)
    parsed.database = settings.redis_queue_db
    return parsed


async def get_pool() -> ArqRedis:
    """Process-wide ARQ pool. Created lazily so importing this costs nothing."""
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()  # type: ignore[attr-defined]  # runtime has it; stubs lag
        _pool = None


async def enqueue_job(
    *,
    job_id: uuid.UUID,
    kind: JobKind,
    params: dict[str, Any],
    trace_id: str | None = None,
) -> None:
    """Put a job on the queue.

    ``_job_id`` is ARQ's own deduplication key and is set to our job id, so a
    retried enqueue of the same row cannot produce two concurrent executions.
    That is belt and braces on top of the QUEUED -> RUNNING guard, which is
    what actually makes double execution impossible.
    """
    pool = await get_pool()
    await pool.enqueue_job(
        TASK_NAME,
        str(job_id),
        str(kind),
        params,
        trace_id,
        _job_id=str(job_id),
    )


__all__ = ["TASK_NAME", "close_pool", "enqueue_job", "get_pool", "redis_settings"]
