"""Cross-cutting middleware: ETags, trace context, rate-limit headers.

The ETag cases are regression tests. Starlette's ``BaseHTTPMiddleware`` hands
every response to the next layer as a *streaming* response, even when the route
returned a plain one -- so the obvious implementation (read ``response.body``)
silently never fires, and every response ships without an ETag. Nothing errors;
the feature is just absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from app.api.middleware import (
    API_VERSION,
    ETagMiddleware,
    RateLimitHeaderMiddleware,
    TraceContextMiddleware,
)
from app.core.rate_limit import RateLimitResult

LARGE_BODY_ITEMS = 100


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.add_middleware(ETagMiddleware)
    app.add_middleware(RateLimitHeaderMiddleware)
    app.add_middleware(TraceContextMiddleware)

    @app.get("/json")
    async def _json() -> dict[str, str]:
        return {"hello": "world"}

    @app.get("/other")
    async def _other() -> dict[str, str]:
        return {"hello": "elsewhere"}

    @app.get("/cached")
    async def _cached() -> dict[str, str]:
        return {"cached": "yes"}

    @app.post("/write")
    async def _write() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/error")
    async def _error() -> dict[str, str]:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="nope")

    @app.get("/stream")
    async def _stream() -> StreamingResponse:
        async def events() -> AsyncIterator[bytes]:
            yield b"data: one\n\n"
            yield b"data: two\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/big")
    async def _big() -> list[str]:
        return ["x" * 32_000] * LARGE_BODY_ITEMS

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestETag:
    async def test_get_responses_carry_an_etag(self, client: AsyncClient) -> None:
        response = await client.get("/json")

        assert response.status_code == 200
        assert response.headers["etag"].startswith('"')
        assert response.json() == {"hello": "world"}

    async def test_the_body_survives_being_hashed(self, client: AsyncClient) -> None:
        """Draining the iterator to hash it must not consume the response."""
        response = await client.get("/json")

        assert response.content == b'{"hello":"world"}'
        assert int(response.headers["content-length"]) == len(response.content)

    async def test_the_etag_is_stable_across_identical_responses(self, client: AsyncClient) -> None:
        first = await client.get("/json")
        second = await client.get("/json")

        assert first.headers["etag"] == second.headers["etag"]

    async def test_different_bodies_get_different_etags(self, client: AsyncClient) -> None:
        json_etag = (await client.get("/json")).headers["etag"]
        other_etag = (await client.get("/other")).headers["etag"]

        assert json_etag != other_etag

    async def test_matching_if_none_match_returns_304(self, client: AsyncClient) -> None:
        etag = (await client.get("/cached")).headers["etag"]

        response = await client.get("/cached", headers={"If-None-Match": etag})

        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["etag"] == etag

    async def test_304_preserves_the_headers_a_200_would_have_sent(
        self, client: AsyncClient
    ) -> None:
        """RFC 9110: a revalidating cache must not lose headers on a 304."""
        etag = (await client.get("/cached")).headers["etag"]

        response = await client.get("/cached", headers={"If-None-Match": etag})

        assert response.headers["x-api-version"] == API_VERSION

    async def test_stale_if_none_match_returns_the_body(self, client: AsyncClient) -> None:
        response = await client.get("/cached", headers={"If-None-Match": '"stale"'})

        assert response.status_code == 200
        assert response.json() == {"cached": "yes"}

    async def test_non_get_requests_are_not_tagged(self, client: AsyncClient) -> None:
        """A POST response is not a cacheable representation."""
        response = await client.post("/write")

        assert response.status_code == 200
        assert "etag" not in response.headers

    async def test_error_responses_are_not_tagged(self, client: AsyncClient) -> None:
        response = await client.get("/error")

        assert response.status_code == 404
        assert "etag" not in response.headers

    async def test_event_streams_are_passed_through_untouched(self, client: AsyncClient) -> None:
        """Buffering an SSE stream to hash it would defeat the endpoint."""
        response = await client.get("/stream")

        assert response.status_code == 200
        assert "etag" not in response.headers
        assert response.content == b"data: one\n\ndata: two\n\n"

    async def test_oversized_bodies_are_delivered_without_an_etag(
        self, client: AsyncClient
    ) -> None:
        """The size guard must not truncate what it declines to hash."""
        response = await client.get("/big")

        assert response.status_code == 200
        assert "etag" not in response.headers
        assert len(response.json()) == LARGE_BODY_ITEMS


class TestTraceContext:
    async def test_api_version_is_always_advertised(self, client: AsyncClient) -> None:
        assert (await client.get("/json")).headers["x-api-version"] == API_VERSION

    async def test_no_trace_id_header_without_a_span(self, client: AsyncClient) -> None:
        """Absent beats fabricated.

        This probe app has no OpenTelemetry instrumentation, so there is no
        span and no real trace ID. Emitting one anyway would send whoever is
        debugging after a trace that was never recorded. The populated case is
        covered end to end against the real app, where instrumentation runs.
        """
        response = await client.get("/json")

        assert "x-trace-id" not in response.headers


class TestRateLimitHeaders:
    async def test_headers_are_copied_from_request_state(self) -> None:
        app = FastAPI()
        app.add_middleware(RateLimitHeaderMiddleware)

        @app.get("/limited")
        async def _limited(request: Request) -> dict[str, str]:
            request.state.rate_limit = RateLimitResult(
                allowed=True, limit=120, remaining=119, reset_seconds=60
            )
            return {"ok": "yes"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/limited")

        assert response.headers["ratelimit-limit"] == "120"
        assert response.headers["ratelimit-remaining"] == "119"
        assert response.headers["ratelimit-reset"] == "60"

    async def test_no_headers_when_the_route_does_not_rate_limit(self) -> None:
        app = FastAPI()
        app.add_middleware(RateLimitHeaderMiddleware)

        @app.get("/open")
        async def _open() -> dict[str, str]:
            return {"ok": "yes"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/open")

        assert "ratelimit-limit" not in response.headers
