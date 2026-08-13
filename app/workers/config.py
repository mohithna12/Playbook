"""ARQ worker settings -- the entry points `arq` is pointed at.

Two worker classes rather than one. Simulation is CPU-bound NumPy that
saturates a core for seconds at a time; running it alongside I/O-bound imports
would let one long simulation stall a queue of fast syncs. Separate settings
mean separate deployments, separate scaling, and separate node pools (RFC
7.3, 18.1).

Importing this module registers every handler -- the imports below look
unused and are not.
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog
from arq import cron

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.domain.jobs import HEARTBEAT_INTERVAL_SECONDS
from app.workers import echo as _echo  # noqa: F401 - registers the echo handler
from app.workers import league_import as _league_import  # noqa: F401 - registers the import handler
from app.workers.base import run_job
from app.workers.queue import redis_settings
from app.workers.reaper import reap

logger = structlog.get_logger()

# Long enough that a slow import is not cut off, short enough that a wedged
# job does not hold a slot forever. Per-kind deadlines live in the jobs table
# and are enforced by the reaper; this is the crude backstop.
MAX_JOB_SECONDS = 300

# The reaper's own cadence. Faster than the heartbeat timeout would mean
# sweeping jobs that are merely between heartbeats.
REAPER_INTERVAL_SECONDS = 30


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(log_level=settings.log_level, json_output=settings.is_production)
    await logger.ainfo("worker_startup", environment=settings.environment)


async def shutdown(ctx: dict[str, Any]) -> None:
    from app.core.cache import close_redis
    from app.workers.queue import close_pool

    await close_pool()
    await close_redis()
    await logger.ainfo("worker_shutdown")


class WorkerSettings:
    """General-purpose worker: imports, syncs, trades, explanations."""

    functions: ClassVar[list[Any]] = [run_job]
    cron_jobs: ClassVar[list[Any]] = []
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = 10
    job_timeout = MAX_JOB_SECONDS
    keep_result = 0  # results live in the jobs table, not in Redis
    health_check_interval = HEARTBEAT_INTERVAL_SECONDS


class SimWorkerSettings(WorkerSettings):
    """Simulation worker: CPU-bound, so far fewer concurrent jobs.

    ``max_jobs`` is low on purpose. The work is vectorized NumPy that already
    uses the core it is on; oversubscribing would make every simulation slower
    without finishing any sooner.
    """

    max_jobs = 2


class ReaperSettings:
    """A tiny worker that only runs the reconciliation sweep.

    Its own deployment so a queue backlog cannot delay it -- the reaper is
    what recovers from a wedged queue, so it must not wait behind one.
    """

    functions: ClassVar[list[Any]] = [reap]
    # A plain list, not a method: ARQ reads this attribute directly, and a
    # callable here would be registered as a cron job of one, never called.
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            reap,
            second=set(range(0, 60, REAPER_INTERVAL_SECONDS)),
            run_at_startup=True,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = 1
    job_timeout = 60


__all__ = [
    "MAX_JOB_SECONDS",
    "REAPER_INTERVAL_SECONDS",
    "ReaperSettings",
    "SimWorkerSettings",
    "WorkerSettings",
]
