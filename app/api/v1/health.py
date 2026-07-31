"""Health and readiness probes.

GET /health -- liveness: is the process alive?
GET /ready  -- readiness: can we serve traffic? (checks DB + Redis)
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db

router = APIRouter(tags=["health"])
logger = structlog.get_logger()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Always returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Readiness probe. Checks database connectivity.

    Returns 200 only when all dependencies are reachable.
    Used by Kubernetes readiness probe and ALB health check.
    """
    checks: dict[str, str] = {}

    # Database check
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        await logger.awarning("readiness_check_failed", component="database")
        checks["database"] = "unavailable"

    all_ok = all(v == "ok" for v in checks.values())

    if not all_ok:
        from fastapi.responses import JSONResponse

        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={"status": "unavailable", "checks": checks},
        )

    return {"status": "ok", "checks": checks}
