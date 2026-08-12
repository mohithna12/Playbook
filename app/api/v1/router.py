"""The ``/v1`` router. Feature routers mount here as milestones land."""

from fastapi import APIRouter

from app.api.v1.jobs import router as jobs_router
from app.api.v1.meta import router as meta_router

v1_router = APIRouter()

# Health and readiness mount at the root, not under /v1: Kubernetes probes and
# the ALB health check should not have to track an API version.
v1_router.include_router(meta_router)
v1_router.include_router(jobs_router)

__all__ = ["v1_router"]
