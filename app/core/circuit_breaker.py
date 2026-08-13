"""Circuit breaker for outbound calls to third-party APIs.

Two things it buys, and they are different. It stops us hammering an upstream
that is already failing, and it stops *us* spending 30 seconds per request
waiting on something that will not answer -- a queue of import jobs blocked on
a dead Sleeper is a queue that never drains (RFC 7.2).

State is shared through Redis so all worker pods count toward one budget:
eight pods each willing to burn five failures is forty requests at a
struggling upstream. When Redis is unreachable the breaker falls back to an
in-process counter, so the behaviour degrades from cluster-wide to per-pod
rather than disappearing.

Deliberately no half-open probe queue: after the cooldown the next caller is
simply let through. One speculative request against a recovered API is cheap,
and a proper half-open state needs coordination this does not warrant.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import redis.asyncio as redis
import structlog

from app.core.cache import get_redis
from app.core.errors import UpstreamError

if TYPE_CHECKING:
    from types import TracebackType

logger = structlog.get_logger()

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_RESET_SECONDS = 60


class BreakerState(StrEnum):
    CLOSED = "closed"  # calls pass through
    OPEN = "open"  # calls are rejected without being attempted


@dataclass
class _LocalState:
    """Per-process fallback, used only while Redis is unreachable."""

    consecutive_failures: int = 0
    opened_at: float | None = None
    lock: None = field(default=None, repr=False)


class CircuitBreaker:
    """Trips after ``failure_threshold`` consecutive failures.

    *Consecutive* is the important word: a single success resets the count, so
    an upstream that is merely flaky never trips it. What trips it is an
    upstream that is down.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_seconds: int = DEFAULT_RESET_SECONDS,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._local = _LocalState()

    @property
    def _failures_key(self) -> str:
        return f"fai:cb:{self.name}:failures"

    @property
    def _open_key(self) -> str:
        return f"fai:cb:{self.name}:open"

    async def state(self) -> BreakerState:
        """Whether calls are currently allowed through."""
        try:
            is_open = await get_redis().exists(self._open_key)
        except redis.RedisError:
            return self._local_state()
        return BreakerState.OPEN if is_open else BreakerState.CLOSED

    def _local_state(self) -> BreakerState:
        if self._local.opened_at is None:
            return BreakerState.CLOSED
        if time.monotonic() - self._local.opened_at >= self.reset_seconds:
            # Cooldown elapsed: let the next caller probe.
            self._local.opened_at = None
            self._local.consecutive_failures = 0
            return BreakerState.CLOSED
        return BreakerState.OPEN

    async def check(self) -> None:
        """Raise :class:`UpstreamError` if the circuit is open."""
        if await self.state() is BreakerState.OPEN:
            raise UpstreamError(
                f"{self.name} is temporarily unavailable; the circuit breaker is open"
            )

    async def record_success(self) -> None:
        """Reset the failure count. One success clears the streak."""
        self._local.consecutive_failures = 0
        self._local.opened_at = None
        with contextlib.suppress(redis.RedisError):
            await get_redis().delete(self._failures_key)

    async def record_failure(self) -> None:
        """Count a failure and open the circuit if the streak is long enough."""
        self._local.consecutive_failures += 1
        if self._local.consecutive_failures >= self.failure_threshold:
            self._local.opened_at = time.monotonic()

        try:
            client = get_redis()
            async with client.pipeline(transaction=True) as pipe:
                pipe.incr(self._failures_key)
                # The counter expires on its own, so an old failure cannot
                # combine with a new one much later to trip the breaker.
                pipe.expire(self._failures_key, self.reset_seconds)
                results = await pipe.execute()
            failures = int(results[0])
        except redis.RedisError:
            return  # the local counter above already handled it

        if failures >= self.failure_threshold:
            try:
                await client.set(self._open_key, "1", ex=self.reset_seconds)
            except redis.RedisError:
                return
            await logger.awarning(
                "circuit_breaker_opened",
                breaker=self.name,
                failures=failures,
                reset_seconds=self.reset_seconds,
            )

    async def reset(self) -> None:
        """Force the circuit closed. For tests and operator intervention."""
        self._local = _LocalState()
        with contextlib.suppress(redis.RedisError):
            await get_redis().delete(self._failures_key, self._open_key)

    async def __aenter__(self) -> CircuitBreaker:
        await self.check()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Record the outcome.

        Only counts failures that say something about the upstream's health.
        A 404 means we asked for a league that does not exist -- that is our
        bug or the user's typo, and letting it trip the breaker would take the
        whole integration down over one bad league id.
        """
        if exc is None:
            await self.record_success()
        elif isinstance(exc, UpstreamError):
            await self.record_failure()


__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_RESET_SECONDS",
    "BreakerState",
    "CircuitBreaker",
]
