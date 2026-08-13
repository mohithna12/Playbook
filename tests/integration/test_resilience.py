"""Circuit breaker and token bucket against a real Redis.

The unit tests cover the in-process fallbacks. What only a real Redis can show
is the property both primitives exist for: state is *shared*, so eight worker
pods count toward one budget rather than eight.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.circuit_breaker import BreakerState, CircuitBreaker
from app.core.errors import UpstreamError
from app.core.token_bucket import TokenBucket

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("redis_client")]


class TestSharedBreakerState:
    async def test_failures_from_separate_instances_accumulate(self) -> None:
        """Two instances stand in for two pods; the threshold is cluster-wide.

        Without this, eight pods each willing to burn five failures would send
        forty requests at an upstream that is already down.
        """
        pod_a = CircuitBreaker("shared_1", failure_threshold=3)
        pod_b = CircuitBreaker("shared_1", failure_threshold=3)
        await pod_a.reset()

        await pod_a.record_failure()
        await pod_b.record_failure()
        assert await pod_a.state() is BreakerState.CLOSED

        await pod_b.record_failure()  # third failure overall

        assert await pod_a.state() is BreakerState.OPEN
        assert await pod_b.state() is BreakerState.OPEN

    async def test_one_pod_opening_stops_the_others(self) -> None:
        opener = CircuitBreaker("shared_2", failure_threshold=1)
        other = CircuitBreaker("shared_2", failure_threshold=1)
        await opener.reset()

        await opener.record_failure()

        with pytest.raises(UpstreamError):
            await other.check()

    async def test_a_success_clears_the_shared_count(self) -> None:
        pod_a = CircuitBreaker("shared_3", failure_threshold=3)
        pod_b = CircuitBreaker("shared_3", failure_threshold=3)
        await pod_a.reset()

        await pod_a.record_failure()
        await pod_a.record_failure()
        await pod_b.record_success()
        await pod_b.record_failure()
        await pod_b.record_failure()

        assert await pod_a.state() is BreakerState.CLOSED

    async def test_breakers_with_different_names_are_independent(self) -> None:
        sleeper = CircuitBreaker("shared_sleeper", failure_threshold=1)
        odds = CircuitBreaker("shared_odds", failure_threshold=1)
        await sleeper.reset()
        await odds.reset()

        await sleeper.record_failure()

        assert await sleeper.state() is BreakerState.OPEN
        assert await odds.state() is BreakerState.CLOSED

    async def test_the_open_flag_expires_on_its_own(self) -> None:
        """The cooldown is a Redis TTL, so no sweeper is needed to close it."""
        breaker = CircuitBreaker("shared_ttl", failure_threshold=1, reset_seconds=1)
        await breaker.reset()

        await breaker.record_failure()
        assert await breaker.state() is BreakerState.OPEN

        await asyncio.sleep(1.2)

        assert await breaker.state() is BreakerState.CLOSED


class TestTokenBucket:
    async def test_a_fresh_bucket_starts_full(self) -> None:
        bucket = TokenBucket("tb_1", per_minute=600, capacity=5)
        await bucket.reset()

        for _ in range(5):
            assert await bucket.try_acquire() is True

    async def test_it_refuses_once_drained(self) -> None:
        bucket = TokenBucket("tb_2", per_minute=1, capacity=2)
        await bucket.reset()

        assert await bucket.try_acquire() is True
        assert await bucket.try_acquire() is True
        assert await bucket.try_acquire() is False

    async def test_the_budget_is_shared_across_instances(self) -> None:
        """The whole point: pods share one upstream budget."""
        pod_a = TokenBucket("tb_3", per_minute=1, capacity=2)
        pod_b = TokenBucket("tb_3", per_minute=1, capacity=2)
        await pod_a.reset()

        assert await pod_a.try_acquire() is True
        assert await pod_b.try_acquire() is True
        assert await pod_a.try_acquire() is False

    async def test_it_refills_over_time(self) -> None:
        bucket = TokenBucket("tb_4", per_minute=120, capacity=1)  # 2/second
        await bucket.reset()

        assert await bucket.try_acquire() is True
        assert await bucket.try_acquire() is False

        await asyncio.sleep(0.7)

        assert await bucket.try_acquire() is True

    async def test_refill_is_capped_at_capacity(self) -> None:
        """An idle bucket must not accumulate an unbounded burst."""
        bucket = TokenBucket("tb_5", per_minute=6000, capacity=3)
        await bucket.reset()
        await asyncio.sleep(0.3)  # would refill 30 tokens if uncapped

        granted = [await bucket.try_acquire() for _ in range(4)]

        assert granted[:3] == [True, True, True]
        assert granted[3] is False

    async def test_a_multi_token_take_is_all_or_nothing(self) -> None:
        """A partial grant would let a caller proceed under-budgeted."""
        bucket = TokenBucket("tb_6", per_minute=1, capacity=5)
        await bucket.reset()

        assert await bucket.try_acquire(4) is True
        assert await bucket.try_acquire(4) is False
        assert await bucket.try_acquire(1) is True

    async def test_acquire_waits_for_a_refill(self) -> None:
        bucket = TokenBucket("tb_7", per_minute=600, capacity=1)  # 10/second
        await bucket.reset()

        assert await bucket.try_acquire() is True
        assert await bucket.acquire(timeout=2.0) is True

    async def test_acquire_reports_expiry_rather_than_raising(self) -> None:
        """The caller decides whether to proceed unbudgeted; this only reports."""
        bucket = TokenBucket("tb_8", per_minute=1, capacity=1)
        await bucket.reset()

        assert await bucket.try_acquire() is True
        assert await bucket.acquire(timeout=0.2) is False

    async def test_concurrent_callers_cannot_overdraw(self) -> None:
        """The take is a Lua script precisely so this cannot happen.

        Read-then-write across three round trips would let every caller see
        the same token count and all take it.
        """
        bucket = TokenBucket("tb_9", per_minute=1, capacity=5)
        await bucket.reset()

        results = await asyncio.gather(*(bucket.try_acquire() for _ in range(20)))

        assert sum(results) == 5

    async def test_buckets_with_different_names_are_independent(self) -> None:
        sleeper = TokenBucket("tb_sleeper", per_minute=1, capacity=1)
        odds = TokenBucket("tb_odds", per_minute=1, capacity=1)
        await sleeper.reset()
        await odds.reset()

        assert await sleeper.try_acquire() is True
        assert await sleeper.try_acquire() is False
        assert await odds.try_acquire() is True
