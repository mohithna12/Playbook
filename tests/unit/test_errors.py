"""Tests for RFC 7807 error handling."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.errors import AppError, NotFoundError
from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_app_error_attributes() -> None:
    err = AppError(404, "Not Found", "Thing not found")
    assert err.status_code == 404
    assert err.title == "Not Found"
    assert err.detail == "Thing not found"


def test_not_found_error() -> None:
    err = NotFoundError("League 123 not found")
    assert err.status_code == 404
    assert err.detail == "League 123 not found"


async def test_unknown_route_returns_404(client: AsyncClient) -> None:
    response = await client.get("/v1/nonexistent")
    assert response.status_code in (404, 405)
