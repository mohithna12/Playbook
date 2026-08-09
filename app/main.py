"""FastAPI application factory.

Middleware order is the load-bearing detail here. Starlette applies middleware
in reverse registration order on the way in, so the last one registered is the
outermost. Trace context is registered last and therefore runs first, which is
what lets error handlers and every log line downstream carry a trace ID.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI

from app.api.middleware import (
    ETagMiddleware,
    RateLimitHeaderMiddleware,
    TraceContextMiddleware,
)
from app.api.v1.health import router as health_router
from app.api.v1.router import v1_router
from app.core.cache import close_redis
from app.core.config import get_settings
from app.core.errors import PROBLEM_JSON, register_error_handlers
from app.core.json import ORJSONResponse
from app.core.logging import setup_logging
from app.core.telemetry import instrument_app, setup_telemetry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger()

# Documented on every operation so generated clients model the error shape
# once instead of per endpoint.
COMMON_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"description": "Bad request", "content": {PROBLEM_JSON: {}}},
    401: {"description": "Missing or invalid credentials", "content": {PROBLEM_JSON: {}}},
    422: {"description": "Validation failed", "content": {PROBLEM_JSON: {}}},
    429: {"description": "Rate limit exceeded", "content": {PROBLEM_JSON: {}}},
    500: {"description": "Internal error", "content": {PROBLEM_JSON: {}}},
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(log_level=settings.log_level, json_output=settings.is_production)

    await logger.ainfo(
        "application_startup",
        environment=settings.environment,
        version=settings.service_version,
    )
    try:
        yield
    finally:
        await close_redis()
        await logger.ainfo("application_shutdown")


def create_app() -> FastAPI:
    """Build the configured FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Playbook",
        description="ML-backed fantasy football decision copilot",
        version=settings.service_version,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json",
        default_response_class=ORJSONResponse,
        responses=COMMON_RESPONSES,
        lifespan=lifespan,
    )

    register_error_handlers(app)

    # Registered inner-to-outer -- the last one added is the outermost. ETag
    # sees the final body, rate-limit headers are attached around it, and trace
    # context wraps both.
    app.add_middleware(ETagMiddleware)
    app.add_middleware(RateLimitHeaderMiddleware)
    app.add_middleware(TraceContextMiddleware)

    # Must come after TraceContextMiddleware so the OpenTelemetry ASGI
    # middleware ends up outermost and the span exists by the time the trace
    # context is read. Installed here rather than in the lifespan because the
    # middleware stack is already frozen by the time startup runs -- doing it
    # there fails quietly, and every trace_id is an empty string.
    setup_telemetry()
    instrument_app(app)

    if settings.cors_allow_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-None-Match"],
            expose_headers=[
                "ETag",
                "X-Trace-Id",
                "X-API-Version",
                "RateLimit-Limit",
                "RateLimit-Remaining",
                "RateLimit-Reset",
                "Retry-After",
            ],
        )

    app.include_router(health_router)
    app.include_router(v1_router, prefix="/v1")

    return app


app = create_app()
