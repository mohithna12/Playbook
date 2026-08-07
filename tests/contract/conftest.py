"""Contract-suite fixtures.

Schemathesis drives the real application, so the application needs a real
database -- a 500 here should mean "the handler is wrong", not "there was no
database".

Unlike the integration suite, this one does *not* share a single
transaction-bound session with the app. Hypothesis dispatches many examples per
operation and the ASGI call runs in its own event loop; one asyncpg connection
driven from two loops raises "another operation is in progress". The app gets
its own engine and a fresh session per request, which is what it does in
production anyway.

Redis is wired in as well, because ``/ready`` reports 503 without it and
Schemathesis -- correctly -- treats any 5xx as a failure. The contract suite
therefore needs a Docker daemon or ``TEST_REDIS_URL``; the unit and integration
suites cover the degraded paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import get_session
from tests.contract.app_under_test import contract_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.fixture(scope="session")
def contract_engine(migrated_url: str) -> AsyncEngine:
    """An engine of the app's own, against the migrated database.

    ``NullPool`` is required, not merely tidy. Hypothesis runs each example in
    its own event loop, and an asyncpg connection is bound to the loop that
    opened it -- a pooled connection reused from a second loop fails inside
    asyncpg's protocol. Not pooling means every request opens and closes its
    own connection, which is slower and correct.
    """
    # No teardown: disposing needs an await, and a session-scoped async
    # fixture would pin an event loop that pytest-asyncio recreates per test.
    # NullPool holds nothing open between requests, so there is nothing to
    # release.
    return create_async_engine(migrated_url, poolclass=NullPool)


@pytest.fixture(autouse=True)
def wire_app_dependencies(
    contract_engine: AsyncEngine,
    redis_client: object,
) -> Iterator[None]:
    """Point the app under test at the migrated database and a live Redis."""
    sessionmaker = async_sessionmaker(bind=contract_engine, expire_on_commit=False)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    contract_app.dependency_overrides[get_session] = _session_override
    try:
        yield
    finally:
        contract_app.dependency_overrides.clear()
