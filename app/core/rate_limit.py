"""Sliding-window rate limiting on Redis sorted sets.

A fixed window is trivially cheaper but lets a caller send two full windows'
worth of traffic across a boundary -- 240 requests in two seconds against a
120/min limit. The window here slides: each request is a member of a sorted set
scored by timestamp, entries older than the window are dropped, and the
remaining cardinality is the count (RFC 12.1).

Buckets are per user *and* per tier, so a burst of writes cannot exhaust a
user's read budget. Anonymous callers are limited by client IP, which is
weaker (shared NAT, spoofable via headers we do not trust) but is only load
shedding, not authorization.

Fails **open**: if Redis is unavailable the request proceeds. A rate limiter is
there to protect capacity, and turning a Redis outage into a total API outage
inverts that.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum

import redis.asyncio as redis
import structlog

from app.core.cache import get_redis
from app.core.config import get_settings

logger = structlog.get_logger()

WINDOW_SECONDS = 60


class LimitTier(StrEnum):
    """Which budget a request draws from."""

    READ = "read"
    WRITE = "write"
    EXPLAIN = "explain"


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        """Draft ``RateLimit-*`` response headers (RFC 12.1)."""
        return {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(self.reset_seconds),
        }


def limit_for(tier: LimitTier) -> int:
    settings = get_settings()
    return {
        LimitTier.READ: settings.rate_limit_reads_per_min,
        LimitTier.WRITE: settings.rate_limit_writes_per_min,
        LimitTier.EXPLAIN: settings.rate_limit_explain_per_min,
    }[tier]


def _bucket_key(identity: str, tier: LimitTier) -> str:
    return f"fai:rl:{tier}:{identity}"


async def check(identity: str, tier: LimitTier) -> RateLimitResult:
    """Consume one unit from ``identity``'s budget for ``tier``.

    The four commands run in a single pipeline round trip. They are not atomic
    against concurrent requests from the same identity, which can let a
    small burst slightly overshoot the limit; a Lua script would close that,
    at the cost of a script every request. Overshoot of a few requests on a
    120/min budget is not worth the complexity.
    """
    limit = limit_for(tier)
    key = _bucket_key(identity, tier)
    now = time.time()
    cutoff = now - WINDOW_SECONDS

    try:
        client = get_redis()
        async with client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, cutoff)
            # A unique member per request: two requests in the same millisecond
            # must count twice, so the score cannot double as the member.
            pipe.zadd(key, {str(uuid.uuid4()): now})
            pipe.zcard(key)
            pipe.expire(key, WINDOW_SECONDS)
            results = await pipe.execute()
        count = int(results[2])
    except redis.RedisError as exc:
        await logger.awarning("rate_limit_unavailable_failing_open", tier=str(tier), error=str(exc))
        return RateLimitResult(
            allowed=True, limit=limit, remaining=limit, reset_seconds=WINDOW_SECONDS
        )

    return RateLimitResult(
        allowed=count <= limit,
        limit=limit,
        remaining=limit - count,
        reset_seconds=WINDOW_SECONDS,
    )


async def reset(identity: str, tier: LimitTier) -> None:
    """Clear one bucket. For tests and operator intervention."""
    with contextlib.suppress(redis.RedisError):
        await get_redis().delete(_bucket_key(identity, tier))


__all__ = ["WINDOW_SECONDS", "LimitTier", "RateLimitResult", "check", "limit_for", "reset"]
