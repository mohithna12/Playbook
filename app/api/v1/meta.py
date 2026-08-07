"""Metadata endpoints: current NFL state and the active model registry.

Both are public. Which model produced a projection is part of that
projection's provenance (RFC 13.4), and season state is not user-specific.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.deps import DbSession, ReadRateLimit
from app.core.cache import TTL_NFL_STATE
from app.schemas.common import ModelVersionSummary, NflState
from app.services.meta import MetaService

router = APIRouter(tags=["meta"], dependencies=[ReadRateLimit])


@router.get("/meta/nfl-state", summary="Current season and week")
async def nfl_state(response: Response, session: DbSession) -> NflState:
    """The season, week, and next kickoff the rest of the API defaults to."""
    response.headers["Cache-Control"] = f"public, max-age={TTL_NFL_STATE}"
    return await MetaService(session).nfl_state()


@router.get("/meta/models", summary="Active model versions and their metrics")
async def active_models(session: DbSession) -> list[ModelVersionSummary]:
    """The models currently serving predictions, with their eval metrics."""
    return await MetaService(session).active_models()


__all__ = ["router"]
