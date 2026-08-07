"""Rate-limit behaviour that does not need a live Redis.

The sliding-window arithmetic itself is exercised against a real Redis in
``tests/integration/test_rate_limit.py``; what matters here is the tier
configuration and the fail-open contract.
"""

from __future__ import annotations

from typing import Any

import pytest
import redis.asyncio as redis

from app.core import rate_limit
from app.core.config import get_settings
from app.core.rate_limit import LimitTier, RateLimitResult


def test_each_tier_reads_its_own_budget() -> None:
    """A write burst must not be able to spend a user's read budget."""
    settings = get_settings()
    assert rate_limit.limit_for(LimitTier.READ) == settings.rate_limit_reads_per_min
    assert rate_limit.limit_for(LimitTier.WRITE) == settings.rate_limit_writes_per_min
    assert rate_limit.limit_for(LimitTier.EXPLAIN) == settings.rate_limit_explain_per_min


def test_rfc_headers_are_rendered() -> None:
    result = RateLimitResult(allowed=True, limit=120, remaining=7, reset_seconds=60)
    assert result.headers() == {
        "RateLimit-Limit": "120",
        "RateLimit-Remaining": "7",
        "RateLimit-Reset": "60",
    }


def test_remaining_never_renders_negative() -> None:
    """Overshoot is possible under concurrency; the header must stay sane."""
    result = RateLimitResult(allowed=False, limit=120, remaining=-3, reset_seconds=60)
    assert result.headers()["RateLimit-Remaining"] == "0"


async def test_redis_outage_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A limiter outage must not become an API outage.

    Rate limiting protects capacity; refusing all traffic when the counter is
    unreachable inverts what it is for.
    """

    class BrokenRedis:
        def pipeline(self, *_args: Any, **_kwargs: Any) -> Any:
            raise redis.ConnectionError("redis is down")

    monkeypatch.setattr(rate_limit, "get_redis", BrokenRedis)

    result = await rate_limit.check("user:abc", LimitTier.READ)

    assert result.allowed is True
    assert result.remaining == result.limit
