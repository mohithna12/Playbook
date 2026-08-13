"""A Redis token bucket for outbound call budgets.

Distinct from ``app.core.rate_limit``, which sheds *inbound* load per user.
This one paces our own calls to a third party whose budget we share across
every pod: Sleeper allows roughly 1000 requests/minute per IP, and eight
worker pods each running a 25-call import will exceed that without a shared
counter (RFC 8.2).

A token bucket rather than the sliding window used for inbound limits,
because the goals differ. Inbound, an over-limit request should be rejected --
the caller can retry. Outbound, the request still needs to happen; it should
just *wait*. So this blocks until a token is available rather than raising.

Fails **open**. If Redis is unreachable we lose the shared count, and the
choice is between not importing anything and briefly risking the upstream's
rate limit -- which it will answer with a 429 that the caller already handles.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import redis.asyncio as redis
import structlog

from app.core.cache import get_redis

logger = structlog.get_logger()

# Refill and capacity are separate: capacity is how much burst is allowed,
# refill is the sustained rate. A full import wants to spend its ~25 calls
# quickly, then wait.
DEFAULT_CAPACITY = 100

# How long a caller will wait for a token before giving up and proceeding.
# Longer than this and the job's own deadline becomes the better guard.
MAX_WAIT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.05

# Redis has no atomic "take a token if one is available" for this shape, and
# doing it in three round trips would let concurrent callers over-draw. This
# runs server-side so the read, refill, and take are one atomic step.
_TAKE_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens = tonumber(bucket[1])
local updated_at = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  updated_at = now
end

-- Refill for the elapsed time, capped at capacity.
local elapsed = math.max(0, now - updated_at)
tokens = math.min(capacity, tokens + elapsed * refill_per_second)

local granted = 0
if tokens >= requested then
  tokens = tokens - requested
  granted = 1
end

redis.call('HSET', key, 'tokens', tokens, 'updated_at', now)
-- Expire an idle bucket; a fresh one starts full, which is correct.
redis.call('EXPIRE', key, 3600)

return {granted, tostring(tokens)}
"""


class TokenBucket:
    """A shared, refilling budget for calls to one upstream."""

    def __init__(
        self,
        name: str,
        *,
        per_minute: int,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        self.name = name
        self.per_minute = per_minute
        self.capacity = capacity
        self._refill_per_second = per_minute / 60.0
        self._script_sha: str | None = None

    @property
    def key(self) -> str:
        return f"fai:tb:{self.name}"

    async def try_acquire(self, tokens: int = 1) -> bool:
        """Take ``tokens`` if available. True if granted.

        Returns True when Redis is unreachable: see the module docstring on
        failing open.
        """
        try:
            client = get_redis()
            # redis-py's `eval` is untyped in the stubs; the Lua contract is
            # the one that matters and it is pinned by the tests.
            result = await client.eval(  # type: ignore[no-untyped-call]
                _TAKE_SCRIPT,
                1,
                self.key,
                str(self.capacity),
                str(self._refill_per_second),
                str(time.time()),
                str(tokens),
            )
        except redis.RedisError as exc:
            await logger.awarning(
                "token_bucket_unavailable_failing_open", bucket=self.name, error=str(exc)
            )
            return True

        return bool(int(result[0]))

    async def acquire(
        self,
        tokens: int = 1,
        *,
        timeout: float = MAX_WAIT_SECONDS,  # noqa: ASYNC109 - reports expiry, not cancellation
    ) -> bool:
        """Wait for ``tokens``, up to ``timeout``. True if acquired.

        Polls rather than using a blocking Redis primitive: there is no
        server-side wait for this shape, and a 50ms poll on a bucket that
        refills continuously is close enough. False means the caller waited
        the full timeout, and it should decide whether to proceed anyway.

        Not ``asyncio.timeout``, which ASYNC109 suggests: that cancels the
        caller, and the contract here is to *report* expiry so the caller can
        choose between waiting longer and proceeding without a token.
        """
        deadline = time.monotonic() + timeout
        while True:
            if await self.try_acquire(tokens):
                return True
            if time.monotonic() >= deadline:
                await logger.awarning(
                    "token_bucket_wait_timed_out", bucket=self.name, waited_seconds=timeout
                )
                return False
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def reset(self) -> None:
        """Refill the bucket. For tests and operator intervention."""
        with contextlib.suppress(redis.RedisError):
            await get_redis().delete(self.key)


__all__ = ["MAX_WAIT_SECONDS", "TokenBucket"]
