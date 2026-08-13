"""The Sleeper HTTP client's failure handling.

Driven through an ``httpx.MockTransport`` rather than the network, so every
failure mode the roadmap calls for -- 429, 5xx, timeout, malformed JSON,
missing resource -- is producible on demand rather than waited for.

The distinction these exist to protect is between "Sleeper is unhealthy" and
"we asked for something that does not exist". Only the first should count
toward the circuit breaker; conflating them means one user's typo can take the
integration down for everyone.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core import circuit_breaker as cb_module
from app.core import token_bucket as tb_module
from app.core.errors import NotFoundError, UpstreamError
from app.services.sleeper import client as client_module
from app.services.sleeper.client import SleeperClient


@pytest.fixture(autouse=True)
def fast_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Redis, no sleeping.

    Redis absence exercises the fail-open paths in both the bucket and the
    breaker, which is the state these tests want anyway. Zeroing the backoff
    keeps three attempts from costing seconds.
    """
    import redis.asyncio as redis

    class Down:
        def __getattr__(self, _name: str) -> Any:
            async def _fail(*_a: object, **_k: object) -> Any:
                raise redis.ConnectionError("down")

            return _fail

        def pipeline(self, *_a: object, **_k: object) -> Any:
            raise redis.ConnectionError("down")

    monkeypatch.setattr(cb_module, "get_redis", Down)
    monkeypatch.setattr(tb_module, "get_redis", Down)
    monkeypatch.setattr(client_module, "BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(client_module, "BACKOFF_MAX_SECONDS", 0.0)


def make_client(handler: Any) -> SleeperClient:
    return SleeperClient(transport=httpx.MockTransport(handler))


class TestHappyPath:
    async def test_a_200_returns_decoded_json(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"league_id": "123"})

        async with make_client(handler) as client:
            assert await client.get("/league/123") == {"league_id": "123"}

    async def test_an_empty_list_is_a_valid_body(self) -> None:
        """Sleeper returns [] for a week that has not happened yet."""

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        async with make_client(handler) as client:
            assert await client.get("/league/123/matchups/18") == []

    async def test_a_null_body_is_returned_not_retried(self) -> None:
        """`null` is Sleeper's answer for an empty transactions week.

        The retry sentinel is a distinct object precisely so a legitimate
        ``None`` is not mistaken for "try again".
        """
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200, content=b"null", headers={"content-type": "application/json"}
            )

        async with make_client(handler) as client:
            assert await client.get("/league/123/transactions/1") is None
        assert calls == 1


class TestMissingResource:
    async def test_a_404_raises_not_found(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with make_client(handler) as client:
            with pytest.raises(NotFoundError, match="no resource"):
                await client.get("/league/nope")

    async def test_a_404_is_not_retried(self) -> None:
        """Retrying a resource that does not exist wastes the rate budget."""
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(404)

        async with make_client(handler) as client:
            with pytest.raises(NotFoundError):
                await client.get("/league/nope")
        assert calls == 1

    async def test_a_404_does_not_trip_the_breaker(self) -> None:
        """One mistyped league id must not take the integration down."""

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with make_client(handler) as client:
            for _ in range(10):
                with pytest.raises(NotFoundError):
                    await client.get("/league/nope")

            assert await client._breaker.state() is cb_module.BreakerState.CLOSED


class TestServerErrors:
    async def test_a_500_is_retried_then_raises_upstream(self) -> None:
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        async with make_client(handler) as client:
            with pytest.raises(UpstreamError):
                await client.get("/league/123")

        assert calls == client_module.MAX_ATTEMPTS

    async def test_a_transient_500_recovers(self) -> None:
        """The retry exists for exactly this: one bad response, then fine."""
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        async with make_client(handler) as client:
            assert await client.get("/league/123") == {"ok": True}
        assert calls == 2

    async def test_repeated_failures_open_the_breaker(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        async with make_client(handler) as client:
            for _ in range(2):  # 2 calls x 3 attempts = 6 failures, threshold 5
                with pytest.raises(UpstreamError):
                    await client.get("/league/123")

            assert await client._breaker.state() is cb_module.BreakerState.OPEN

    async def test_an_open_breaker_rejects_without_calling(self) -> None:
        """The point: stop queueing behind an upstream that is down."""
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        async with make_client(handler) as client:
            for _ in range(2):
                with pytest.raises(UpstreamError):
                    await client.get("/league/123")
            calls_before = calls

            with pytest.raises(UpstreamError, match="circuit breaker is open"):
                await client.get("/league/123")

            assert calls == calls_before  # nothing was sent


class TestRateLimiting:
    async def test_a_429_is_retried(self) -> None:
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, json={"ok": True})

        async with make_client(handler) as client:
            assert await client.get("/league/123") == {"ok": True}
        assert calls == 2

    async def test_a_persistent_429_raises_upstream(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "0"})

        async with make_client(handler) as client:
            with pytest.raises(UpstreamError):
                await client.get("/league/123")

    async def test_an_http_date_retry_after_does_not_crash(self) -> None:
        """The header may be an HTTP-date; our own backoff covers that case."""

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})

        async with make_client(handler) as client:
            with pytest.raises(UpstreamError):
                await client.get("/league/123")


class TestTransportFailures:
    async def test_a_timeout_is_retried_then_raises(self) -> None:
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("too slow")

        async with make_client(handler) as client:
            with pytest.raises(UpstreamError):
                await client.get("/league/123")

        assert calls == client_module.MAX_ATTEMPTS

    async def test_a_connection_error_is_retried(self) -> None:
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"ok": True})

        async with make_client(handler) as client:
            assert await client.get("/league/123") == {"ok": True}


class TestMalformedResponses:
    async def test_a_200_with_html_raises_upstream(self) -> None:
        """Sleeper has served HTML error pages under load.

        A 200 whose body is not JSON is a broken upstream, not a broken
        request, so it counts toward the breaker and is retried.
        """

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>502 Bad Gateway</html>")

        async with make_client(handler) as client:
            with pytest.raises(UpstreamError, match="non-JSON"):
                await client.get("/league/123")

    async def test_truncated_json_raises_upstream(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b'{"league_id": "123"',
                headers={"content-type": "application/json"},
            )

        async with make_client(handler) as client:
            with pytest.raises(UpstreamError):
                await client.get("/league/123")

    async def test_a_schema_change_is_not_the_clients_problem(self) -> None:
        """Valid JSON of an unexpected shape passes through.

        The client's job is transport. A shape change is the normalizer's to
        notice, and failing here would hide it behind a transport error.
        """

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"totally": "different"})

        async with make_client(handler) as client:
            assert await client.get("/league/123") == {"totally": "different"}


class TestOtherClientErrors:
    async def test_a_400_raises_without_retrying(self) -> None:
        """Retrying a request the upstream rejected will not fix it."""
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(400)

        async with make_client(handler) as client:
            with pytest.raises(UpstreamError):
                await client.get("/league/123")
        assert calls == 1


class TestRequestShape:
    async def test_the_base_url_is_applied(self) -> None:
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={})

        async with make_client(handler) as client:
            await client.get("/league/123")

        assert seen == [f"{client_module.BASE_URL}/league/123"]

    async def test_a_user_agent_identifies_us(self) -> None:
        """An unauthenticated API's only way to contact us about our traffic."""
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("user-agent", ""))
            return httpx.Response(200, json={})

        async with make_client(handler) as client:
            await client.get("/league/123")

        assert "playbook" in seen[0].lower()


class TestConcurrency:
    async def test_concurrent_calls_are_bounded_by_the_semaphore(self) -> None:
        """A wide gather must not outrun the per-league concurrency limit."""
        import asyncio

        in_flight = 0
        peak = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(200, json={})

        client = SleeperClient(transport=httpx.MockTransport(handler), concurrency=3)
        async with client:
            await asyncio.gather(*(client.get(f"/league/{i}") for i in range(12)))

        assert peak <= 3


def test_fixtures_are_valid_json() -> None:
    """The recorded responses must stay loadable as the schema evolves."""
    from pathlib import Path

    fixtures = Path(__file__).resolve().parent.parent / "fixtures" / "sleeper"
    files = sorted(fixtures.glob("*.json"))

    assert files, "no recorded Sleeper fixtures found"
    for path in files:
        json.loads(path.read_text())
