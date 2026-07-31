"""FastAPI dependency injection.

Provides database sessions, Redis clients, and the current authenticated user
to route handlers via Depends().
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=_settings.database_pool_size,
    max_overflow=_settings.database_max_overflow,
    echo=_settings.debug,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a scoped async database session."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
