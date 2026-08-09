"""Database engine and session management.

Pool sizing is deliberately small (10 + 5 overflow per pod). With 10 API pods
that is 150 potential connections against a ``db.t4g.medium`` whose
``max_connections`` is ~340, leaving headroom for workers, Airflow, and
migrations. PgBouncer in transaction-pooling mode sits in front in production
(RFC 7.1).

``statement_timeout`` is set per-connection rather than per-query: a runaway
analytical query must not be able to saturate the pool, and the guarantee
should not depend on every call site remembering to ask for it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        echo=False,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.database_statement_timeout_ms),
                "application_name": "playbook",
            }
        },
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that commits on success."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = ["get_engine", "get_session", "get_sessionmaker"]
