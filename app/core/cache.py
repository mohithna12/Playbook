"""Redis client and cache-key conventions.

Redis holds three unrelated kinds of state, separated by logical database so
that flushing one never touches the others: the ARQ job queue, rate-limit
counters, and this cache (RFC 7.4).

Key convention: ``fai:<version>:<namespace>:<identifier>``. ``CACHE_VERSION``
is a global invalidation lever -- bumping it retires every cached value at once,
which is what a change to a projection's serialization needs. Reaching for
``FLUSHDB`` instead would take the queue and rate limits with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import redis.asyncio as redis
import structlog

from app.core.config import get_settings
from app.core.json import dumps

if TYPE_CHECKING:
    from collections.abc import Awaitable

import orjson

logger = structlog.get_logger()

CACHE_VERSION = "v1"
KEY_PREFIX = "fai"

# TTLs from the RFC 22.2 caching table. Projections are refreshed by a batch
# job, so a short TTL bounds staleness after an out-of-band refresh; league
# settings change only on sync.
TTL_PROJECTIONS = 300
TTL_LEAGUE_SETTINGS = 3600
TTL_NFL_STATE = 60
TTL_PLAYER_SEARCH = 600
TTL_EXPLANATION = 86400

_client: redis.Redis[bytes] | None = None


def cache_key(namespace: str, *parts: Any) -> str:
    """Build a namespaced, version-prefixed cache key."""
    rendered = ":".join(str(p) for p in parts)
    return f"{KEY_PREFIX}:{CACHE_VERSION}:{namespace}:{rendered}"


def get_redis() -> redis.Redis[bytes]:
    """Process-wide Redis client. Connections are pooled and reused."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            health_check_interval=30,
        )
    return _client


async def close_redis() -> None:
    """Close the shared client. Called from the application lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()  # type: ignore[attr-defined]  # present in redis>=5, absent from stubs
        _client = None


async def get_json(key: str) -> Any | None:
    """Read and decode a cached value. A cache failure is never fatal.

    The cache is an optimization; if Redis is down the request should be slow,
    not broken. Every read swallows connection errors and reports a miss.
    """
    try:
        raw = await get_redis().get(key)
    except redis.RedisError as exc:
        await logger.awarning("cache_read_failed", key=key, error=str(exc))
        return None

    if raw is None:
        return None
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        # A value we cannot decode is a value we wrote under a different
        # schema. Drop it rather than serving or repeatedly failing on it.
        await logger.awarning("cache_value_undecodable", key=key)
        await delete(key)
        return None


async def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    """Write a value with a TTL. Failures are logged, never raised."""
    try:
        await get_redis().set(key, dumps(value), ex=ttl_seconds)
    except redis.RedisError as exc:
        await logger.awarning("cache_write_failed", key=key, error=str(exc))


async def delete(*keys: str) -> None:
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except redis.RedisError as exc:
        await logger.awarning("cache_delete_failed", error=str(exc))


async def ping() -> bool:
    """Readiness check. True if Redis answers."""
    try:
        result: Awaitable[Any] | Any = get_redis().ping()
        await result
    except redis.RedisError:
        return False
    return True


__all__ = [
    "CACHE_VERSION",
    "TTL_EXPLANATION",
    "TTL_LEAGUE_SETTINGS",
    "TTL_NFL_STATE",
    "TTL_PLAYER_SEARCH",
    "TTL_PROJECTIONS",
    "cache_key",
    "close_redis",
    "delete",
    "get_json",
    "get_redis",
    "ping",
    "set_json",
]
