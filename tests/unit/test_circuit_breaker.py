"""Circuit breaker behaviour when Redis is unreachable.

These drive the in-process fallback deliberately: the point of that fallback
is that a Redis outage degrades breaking from cluster-wide to per-pod rather
than removing it. The shared-state path is covered against a real Redis in the
integration suite.
"""

from __future__ import annotations

import pytest
import redis.asyncio as redis

from app.core import circuit_breaker as cb_module
from app.core.circuit_breaker import BreakerState, CircuitBreaker
from app.core.errors import UpstreamError


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the in-process path by making every Redis call fail."""

    class Down:
        async def exists(self, *_a: object) -> int:
            raise redis.ConnectionError("down")

        async def delete(self, *_a: object) -> int:
            raise redis.ConnectionError("down")

        async def set(self, *_a: object, **_k: object) -> None:
            raise redis.ConnectionError("down")

        def pipeline(self, *_a: object, **_k: object) -> object:
            raise redis.ConnectionError("down")

    monkeypatch.setattr(cb_module, "get_redis", Down)


class TestClosedByDefault:
    async def test_a_new_breaker_is_closed(self) -> None:
        assert await CircuitBreaker("sleeper").state() is BreakerState.CLOSED

    async def test_check_passes_while_closed(self) -> None:
        await CircuitBreaker("sleeper").check()  # does not raise


class TestOpening:
    async def test_it_opens_on_the_threshold_failure(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=3)

        for _ in range(2):
            await breaker.record_failure()
        assert await breaker.state() is BreakerState.CLOSED

        await breaker.record_failure()
        assert await breaker.state() is BreakerState.OPEN

    async def test_an_open_breaker_rejects_without_calling(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=1)
        await breaker.record_failure()

        with pytest.raises(UpstreamError, match="circuit breaker is open"):
            await breaker.check()

    async def test_a_success_resets_the_streak(self) -> None:
        """*Consecutive* failures. A flaky upstream must not trip it."""
        breaker = CircuitBreaker("sleeper", failure_threshold=3)

        await breaker.record_failure()
        await breaker.record_failure()
        await breaker.record_success()
        await breaker.record_failure()
        await breaker.record_failure()

        assert await breaker.state() is BreakerState.CLOSED


class TestCooldown:
    async def test_it_closes_again_after_the_reset_window(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=1, reset_seconds=0)
        await breaker.record_failure()

        # reset_seconds=0 means the cooldown has already elapsed.
        assert await breaker.state() is BreakerState.CLOSED

    async def test_it_stays_open_inside_the_window(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=1, reset_seconds=3600)
        await breaker.record_failure()

        assert await breaker.state() is BreakerState.OPEN

    async def test_reset_forces_it_closed(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=1, reset_seconds=3600)
        await breaker.record_failure()

        await breaker.reset()

        assert await breaker.state() is BreakerState.CLOSED


class TestContextManager:
    async def test_a_successful_call_clears_the_streak(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=2)
        await breaker.record_failure()

        async with breaker:
            pass

        await breaker.record_failure()
        assert await breaker.state() is BreakerState.CLOSED

    async def test_an_upstream_error_counts_as_a_failure(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=1)

        with pytest.raises(UpstreamError):
            async with breaker:
                raise UpstreamError("502 from sleeper")

        assert await breaker.state() is BreakerState.OPEN

    async def test_other_exceptions_do_not_trip_it(self) -> None:
        """A 404 is our bad league id, not evidence the upstream is unhealthy.

        Counting it would take the whole integration down over one user's
        typo.
        """
        breaker = CircuitBreaker("sleeper", failure_threshold=1)

        for _ in range(5):
            with pytest.raises(ValueError, match="no such league"):
                async with breaker:
                    raise ValueError("no such league")

        assert await breaker.state() is BreakerState.CLOSED

    async def test_entering_an_open_breaker_raises(self) -> None:
        breaker = CircuitBreaker("sleeper", failure_threshold=1, reset_seconds=3600)
        await breaker.record_failure()

        with pytest.raises(UpstreamError):
            async with breaker:
                pytest.fail("the body must not run while the circuit is open")


class TestIsolation:
    async def test_breakers_are_independent(self) -> None:
        """A dead Sleeper must not stop us calling the odds API."""
        sleeper = CircuitBreaker("sleeper", failure_threshold=1, reset_seconds=3600)
        odds = CircuitBreaker("odds", failure_threshold=1, reset_seconds=3600)

        await sleeper.record_failure()

        assert await sleeper.state() is BreakerState.OPEN
        assert await odds.state() is BreakerState.CLOSED
