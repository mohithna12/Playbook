"""Liveness and readiness probes.

The split matters operationally: ``/health`` must never check a dependency,
because a probe that does will restart healthy pods during a database blip and
turn a degraded system into an outage. ``/ready`` must check them, so a pod
that cannot serve is pulled from the load balancer while staying alive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.health import router as health_router
from app.core.db import get_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(health_router)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestLiveness:
    async def test_health_is_200_and_checks_nothing(self, client: AsyncClient) -> None:
        """No dependency check, by design -- see the module docstring."""
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_health_does_not_report_dependencies(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with Redis down, liveness is unaffected."""
        from app.core import cache

        async def _down() -> bool:
            return False

        monkeypatch.setattr(cache, "ping", _down)

        assert (await client.get("/health")).status_code == 200


class TestReadiness:
    async def test_ready_is_200_when_both_dependencies_answer(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core import cache

        async def _up() -> bool:
            return True

        monkeypatch.setattr(cache, "ping", _up)

        response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}

    async def test_ready_is_503_when_redis_is_down(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """503 pulls the pod from the load balancer without restarting it."""
        from app.core import cache

        async def _down() -> bool:
            return False

        monkeypatch.setattr(cache, "ping", _down)

        response = await client.get("/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["checks"] == {"database": "ok", "redis": "unavailable"}

    async def test_ready_names_the_failing_dependency(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which one is down is the first question during an incident."""
        from app.core import cache
        from app.core.db import get_session as real_get_session

        async def _up() -> bool:
            return True

        monkeypatch.setattr(cache, "ping", _up)

        # Swap the session for one whose execute() fails, standing in for an
        # unreachable database.
        class BrokenSession:
            async def execute(self, *_a: object, **_k: object) -> object:
                raise RuntimeError("connection refused")

        app = client._transport.app  # type: ignore[attr-defined]

        async def _broken() -> AsyncIterator[object]:
            yield BrokenSession()

        app.dependency_overrides[real_get_session] = _broken

        response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "unavailable"
