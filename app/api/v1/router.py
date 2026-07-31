"""V1 API router -- aggregates all v1 sub-routers."""

from fastapi import APIRouter

from app.api.v1.health import router as health_router

v1_router = APIRouter()

# Health and readiness are mounted at root (not under /v1)
# so Kubernetes probes don't need the version prefix.
# All other routers will be mounted under /v1.

# Placeholder: additional routers will be added as features are built.
# from app.api.v1.leagues import router as leagues_router
# v1_router.include_router(leagues_router, prefix="/leagues")

__all__ = ["health_router", "v1_router"]
