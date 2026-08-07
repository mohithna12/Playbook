"""Liveness and readiness probes.

``/health`` answers "is this process alive?" and must never touch a dependency:
a probe that checks the database restarts healthy pods during a database
blip, turning a degraded system into an outage.

``/ready`` answers "should this pod receive traffic?" and does check
dependencies, because a pod that cannot reach Postgres should be pulled from
the load balancer while it stays running.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from app.core import cache
from app.core.db import get_session

router = APIRouter(tags=["health"])
logger = structlog.get_logger()

OK = "ok"
UNAVAILABLE = "unavailable"


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """200 whenever the process is running. Checks nothing else, by design."""
    return {"status": OK}


@router.get(
    "/ready",
    summary="Readiness probe",
    responses={
        HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "A dependency is unreachable; do not route traffic here.",
        }
    },
)
async def ready(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """200 only when Postgres and Redis both answer."""
    checks: dict[str, str] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = OK
    except Exception as exc:
        await logger.awarning("readiness_check_failed", component="database", error=str(exc))
        checks["database"] = UNAVAILABLE

    checks["redis"] = OK if await cache.ping() else UNAVAILABLE

    if any(v != OK for v in checks.values()):
        response.status_code = HTTP_503_SERVICE_UNAVAILABLE
        return {"status": UNAVAILABLE, "checks": checks}

    return {"status": OK, "checks": checks}


__all__ = ["router"]
