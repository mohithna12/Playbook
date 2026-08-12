"""Job kinds and their execution policy. RFC 7.2.

Pure data: the timeout, retry count, and idempotency of every job type, in one
place rather than spread across the enqueue call sites. The reaper reads the
same table the enqueuer does, so "how long may this run" has exactly one
answer.

Timeouts here are the *deadline* -- how long a job may take end to end,
including retries and queue wait. A job past its deadline is reaped.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobKind(StrEnum):
    """Every async job the system knows how to run."""

    ECHO = "echo"
    LEAGUE_FULL_IMPORT = "league.full_import"
    LEAGUE_INCREMENTAL_SYNC = "league.incremental_sync"
    SIMULATION_RUN = "simulation.run"
    TRADE_ANALYZE = "trade.analyze"
    EXPLANATION_GENERATE = "explanation.generate"
    CACHE_WARM_LEAGUE = "cache.warm_league"


@dataclass(frozen=True, slots=True)
class JobPolicy:
    """Execution policy for one job kind.

    ``idempotent`` records whether re-running the job is safe. It is what makes
    reaping a stale RUNNING job sound: the work is upsert-based, so a worker
    killed mid-flight can be replayed. A non-idempotent kind is failed rather
    than re-enqueued.
    """

    timeout_seconds: int
    max_attempts: int
    idempotent: bool
    estimated_seconds: int


# The table from RFC 7.2, verbatim. `explanation.generate` is the one
# non-idempotent kind -- it is deduplicated by prompt hash at the cache layer
# instead, so replaying it would spend tokens without changing the answer.
POLICIES: dict[JobKind, JobPolicy] = {
    JobKind.ECHO: JobPolicy(
        timeout_seconds=30, max_attempts=1, idempotent=True, estimated_seconds=1
    ),
    JobKind.LEAGUE_FULL_IMPORT: JobPolicy(
        timeout_seconds=180, max_attempts=3, idempotent=True, estimated_seconds=25
    ),
    JobKind.LEAGUE_INCREMENTAL_SYNC: JobPolicy(
        timeout_seconds=60, max_attempts=3, idempotent=True, estimated_seconds=3
    ),
    JobKind.SIMULATION_RUN: JobPolicy(
        timeout_seconds=30, max_attempts=1, idempotent=True, estimated_seconds=2
    ),
    JobKind.TRADE_ANALYZE: JobPolicy(
        timeout_seconds=60, max_attempts=1, idempotent=True, estimated_seconds=4
    ),
    JobKind.EXPLANATION_GENERATE: JobPolicy(
        timeout_seconds=45, max_attempts=2, idempotent=False, estimated_seconds=8
    ),
    JobKind.CACHE_WARM_LEAGUE: JobPolicy(
        timeout_seconds=120, max_attempts=2, idempotent=True, estimated_seconds=6
    ),
}

# A worker that stops heartbeating for this long is presumed dead -- a spot
# reclaim or an OOM kill, neither of which gets to run cleanup. Comfortably
# longer than the heartbeat interval so ordinary scheduling jitter does not
# look like death (RFC 7.2).
HEARTBEAT_INTERVAL_SECONDS = 15
HEARTBEAT_TIMEOUT_SECONDS = 90

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"})


def policy_for(kind: JobKind | str) -> JobPolicy:
    """The policy for ``kind``. Raises KeyError on an unregistered kind.

    Deliberately not a permissive default: a job type with no declared timeout
    would be invisible to the reaper and could hang forever.
    """
    return POLICIES[JobKind(kind)]


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TIMEOUT_SECONDS",
    "POLICIES",
    "TERMINAL_STATUSES",
    "JobKind",
    "JobPolicy",
    "is_terminal",
    "policy_for",
]
