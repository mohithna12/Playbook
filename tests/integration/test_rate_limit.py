"""Sliding-window rate limiting against a real Redis."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core import rate_limit
from app.core.rate_limit import WINDOW_SECONDS, LimitTier

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("redis_client")]


async def test_requests_under_the_limit_are_allowed() -> None:
    limit = rate_limit.limit_for(LimitTier.EXPLAIN)

    for expected_remaining in range(limit - 1, -1, -1):
        result = await rate_limit.check("user:under", LimitTier.EXPLAIN)
        assert result.allowed is True
        assert result.remaining == expected_remaining


async def test_the_request_past_the_limit_is_rejected() -> None:
    limit = rate_limit.limit_for(LimitTier.EXPLAIN)

    for _ in range(limit):
        assert (await rate_limit.check("user:over", LimitTier.EXPLAIN)).allowed is True

    rejected = await rate_limit.check("user:over", LimitTier.EXPLAIN)
    assert rejected.allowed is False
    assert rejected.reset_seconds == WINDOW_SECONDS


async def test_tiers_have_independent_budgets() -> None:
    """Exhausting the explain budget must leave reads untouched (RFC 12.1)."""
    for _ in range(rate_limit.limit_for(LimitTier.EXPLAIN) + 1):
        await rate_limit.check("user:tiers", LimitTier.EXPLAIN)

    assert (await rate_limit.check("user:tiers", LimitTier.EXPLAIN)).allowed is False
    assert (await rate_limit.check("user:tiers", LimitTier.READ)).allowed is True


async def test_identities_have_independent_budgets() -> None:
    for _ in range(rate_limit.limit_for(LimitTier.EXPLAIN) + 1):
        await rate_limit.check("user:noisy", LimitTier.EXPLAIN)

    assert (await rate_limit.check("user:noisy", LimitTier.EXPLAIN)).allowed is False
    assert (await rate_limit.check("user:quiet", LimitTier.EXPLAIN)).allowed is True


async def test_the_window_slides(redis_client) -> None:
    """Entries older than the window stop counting.

    Time is not mocked -- the entries are backdated directly in the sorted set,
    which is what the implementation actually reads. Sleeping through a 60 s
    window in a test suite is not an option.
    """
    identity = "user:sliding"
    limit = rate_limit.limit_for(LimitTier.EXPLAIN)
    key = f"fai:rl:{LimitTier.EXPLAIN}:{identity}"

    for _ in range(limit):
        await rate_limit.check(identity, LimitTier.EXPLAIN)
    assert (await rate_limit.check(identity, LimitTier.EXPLAIN)).allowed is False

    # Age every recorded request out of the window.
    stale = time.time() - WINDOW_SECONDS - 1
    members = await redis_client.zrange(key, 0, -1)
    await redis_client.zadd(key, {m: stale for m in members})

    assert (await rate_limit.check(identity, LimitTier.EXPLAIN)).allowed is True


async def test_reset_clears_a_bucket() -> None:
    for _ in range(rate_limit.limit_for(LimitTier.EXPLAIN) + 1):
        await rate_limit.check("user:reset", LimitTier.EXPLAIN)
    assert (await rate_limit.check("user:reset", LimitTier.EXPLAIN)).allowed is False

    await rate_limit.reset("user:reset", LimitTier.EXPLAIN)

    assert (await rate_limit.check("user:reset", LimitTier.EXPLAIN)).allowed is True


async def test_concurrent_requests_are_all_counted() -> None:
    """No request may go uncounted, even when they arrive together.

    Overshoot is accepted (the check is not atomic against concurrent callers,
    see the module docstring); undercounting is not, because that is the bug
    that lets a burst bypass the limit entirely.
    """
    identity = "user:concurrent"
    burst = 20

    results = await asyncio.gather(
        *(rate_limit.check(identity, LimitTier.READ) for _ in range(burst))
    )

    limit = rate_limit.limit_for(LimitTier.READ)
    assert sorted(r.remaining for r in results) == sorted(range(limit - burst, limit))
