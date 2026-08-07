"""Root conftest -- Postgres and Redis fixtures shared by every suite.

The schema under test is always built by running the actual migrations,
never by ``metadata.create_all()``. Creating tables from ORM metadata would
test a schema that no environment ever runs: partitions, the
``public.uuidv7()`` function, expression indexes, and the JSONB defaults all
come from migrations.

Containers are session-scoped (starting Postgres costs ~2s); each test gets a
fresh session and rolls back, so tests stay order-independent without paying
for a container per test.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic_config(url: str) -> Config:
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(PROJECT_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start Postgres 16 and return an asyncpg URL. Schema is *not* created.

    ``TEST_DATABASE_URL`` bypasses the container and points the suite at an
    already-running server. CI uses the container; the escape hatch is for
    machines without a Docker daemon. The database it names is dropped to and
    recreated from ``base`` by the migration fixtures, so never aim it at
    anything you care about.
    """
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = external
        try:
            yield external
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
        return

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        url = container.get_connection_url()
        # env.py prefers DATABASE_URL, which is how alembic reaches the container
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        try:
            yield url
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def migrated_url(postgres_url: str) -> str:
    """Postgres with all migrations applied."""
    command.upgrade(_alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_url, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection wrapped in a transaction that is always rolled back."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """An ORM session bound to the rolled-back transaction.

    ``join_transaction_mode="create_savepoint"`` matters for the constraint
    tests: a failed flush rolls the session back to its savepoint instead of
    killing the outer transaction, so the fixture can still tear down cleanly
    after an expected ``IntegrityError``.
    """
    async with AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as sess:
        yield sess


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    return _alembic_config(postgres_url)


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """A Redis endpoint. ``TEST_REDIS_URL`` bypasses the container, as above."""
    external = os.environ.get("TEST_REDIS_URL")
    if external:
        yield external
        return

    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    """A Redis client wired into ``app.core.cache``'s process-wide singleton.

    Rate limiting and caching both reach for that singleton rather than taking
    a client argument, so the fixture swaps it out and flushes between tests to
    keep them order-independent.
    """
    from app.core import cache

    client: Redis = redis_from_url(redis_url, decode_responses=False)
    previous = cache._client
    cache._client = client
    try:
        await client.flushdb()
        yield client
    finally:
        cache._client = previous
        await client.aclose()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
