"""RFC 7807 error rendering and the exception taxonomy."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    PROBLEM_JSON,
    AppError,
    ConflictError,
    ForbiddenError,
    LeagueNotSyncedError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    UpstreamError,
    register_error_handlers,
)
from app.core.errors import (
    TimeoutError as GatewayTimeoutError,
)
from app.main import app
from app.schemas.common import Week


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def error_client() -> AsyncIterator[AsyncClient]:
    """An app whose routes exist only to raise, one per error class."""
    probe = FastAPI()
    register_error_handlers(probe)

    @probe.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError("League 123 not found")

    @probe.get("/unauthorized")
    async def _unauthorized() -> None:
        raise UnauthorizedError

    @probe.get("/rate-limited")
    async def _rate_limited() -> None:
        raise RateLimitedError(retry_after=42)

    @probe.get("/not-synced")
    async def _not_synced() -> None:
        raise LeagueNotSyncedError("League 1124589 is in state PENDING.")

    @probe.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("database on fire")

    @probe.get("/bounded")
    async def _bounded(week: Week) -> dict[str, int]:
        return {"week": week}

    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_status_codes_match_the_taxonomy() -> None:
    """RFC 9.3's table, asserted so a renamed class cannot silently remap."""
    assert NotFoundError().status_code == 404
    assert UnauthorizedError().status_code == 401
    assert ForbiddenError().status_code == 403
    assert ConflictError().status_code == 409
    assert RateLimitedError(retry_after=1).status_code == 429
    assert UpstreamError().status_code == 502
    assert GatewayTimeoutError().status_code == 504


def test_detail_defaults_to_title() -> None:
    assert NotFoundError().detail == "Not Found"
    assert NotFoundError("League 123 not found").detail == "League 123 not found"


def test_type_uri_is_stable_and_namespaced() -> None:
    assert NotFoundError().type_uri == "https://api.fantasyai.dev/errors/not-found"
    assert LeagueNotSyncedError().type_uri == "https://api.fantasyai.dev/errors/league-not-synced"


def test_subclass_keeps_parent_status_with_its_own_slug() -> None:
    error = LeagueNotSyncedError()
    assert error.status_code == ConflictError.status_code
    assert error.slug != ConflictError.slug


def test_unauthorized_sets_www_authenticate() -> None:
    assert UnauthorizedError().headers["WWW-Authenticate"] == "Bearer"


def test_rate_limited_sets_retry_after() -> None:
    assert RateLimitedError(retry_after=42).headers["Retry-After"] == "42"


def test_base_error_is_a_500() -> None:
    assert AppError().status_code == 500


async def test_problem_json_shape(error_client: AsyncClient) -> None:
    response = await error_client.get("/not-found")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)

    body = response.json()
    assert body["type"] == "https://api.fantasyai.dev/errors/not-found"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "League 123 not found"
    assert body["instance"] == "/not-found"
    assert body["errors"] == []
    assert "trace_id" in body


async def test_error_headers_reach_the_response(error_client: AsyncClient) -> None:
    unauthorized = await error_client.get("/unauthorized")
    assert unauthorized.headers["WWW-Authenticate"] == "Bearer"

    limited = await error_client.get("/rate-limited")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "42"


async def test_unhandled_exception_becomes_problem_json(error_client: AsyncClient) -> None:
    """An unhandled error must not leak its message, but must stay renderable."""
    response = await error_client.get("/boom")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)

    body = response.json()
    assert body["type"] == "https://api.fantasyai.dev/errors/internal"
    assert "database on fire" not in body["detail"]


async def test_unknown_route_is_problem_json(client: AsyncClient) -> None:
    """Starlette's own routing errors go through the same renderer."""
    response = await client.get("/v1/nonexistent")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["type"] == "https://api.fantasyai.dev/errors/not-found"


async def test_validation_error_populates_field_errors(error_client: AsyncClient) -> None:
    """Pydantic failures become the `errors[]` array, not a bare 422 body."""
    response = await error_client.get("/bounded", params={"week": 99})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)

    body = response.json()
    assert body["type"] == "https://api.fantasyai.dev/errors/validation-failed"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["loc"] == ["query", "week"]
