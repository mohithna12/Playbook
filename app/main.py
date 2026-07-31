"""FastAPI application factory.

Creates and configures the FantasyAI API application.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.v1.health import router as health_router
from app.api.v1.router import v1_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import setup_logging

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan -- startup and shutdown logic."""
    settings = get_settings()
    setup_logging(
        log_level=settings.log_level,
        json_output=settings.is_production,
    )
    await logger.ainfo(
        "application_startup",
        environment=settings.environment,
        debug=settings.debug,
    )
    yield
    await logger.ainfo("application_shutdown")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="FantasyAI",
        description="ML-backed fantasy football decision copilot",
        version="0.1.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        lifespan=lifespan,
    )

    # Error handlers (RFC 7807)
    register_error_handlers(app)

    # Health probes at root (no /v1 prefix -- Kubernetes probes)
    app.include_router(health_router)

    # Versioned API
    app.include_router(v1_router, prefix="/v1")

    return app


app = create_app()
