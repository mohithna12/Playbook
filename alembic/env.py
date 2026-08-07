"""Alembic environment configuration.

The database URL always comes from application settings (which read
``DATABASE_URL``), never from ``alembic.ini`` — one source of truth for
connection config across the app, the workers, and migrations.

Autogenerate is a *drift check*, not the source of migrations: partition child
tables, the ``uuidv7()`` function, and expression indexes are invisible to it.
``include_object`` therefore filters out the season partitions so a clean
schema compares equal to the ORM metadata.
"""

from __future__ import annotations

import asyncio
import os
import re
from logging.config import fileConfig
from typing import TYPE_CHECKING, Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all models so Alembic can detect them for autogenerate
from app.core.config import get_settings
from app.models import *  # noqa: F403
from app.models import Base

if TYPE_CHECKING:
    from sqlalchemy.schema import SchemaItem

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    os.environ.get("DATABASE_URL") or get_settings().database_url,
)

target_metadata = Base.metadata

# Season partitions are named `<parent>_<season>` and are created by migrations
# and by the yearly Airflow task, not by the ORM.
_PARTITION_RE = re.compile(r"^(weekly_stats|feature_store|predictions|prediction_history)_\d{4}$")


def include_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    return not (type_ == "table" and name is not None and _PARTITION_RE.match(name))


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode -- generate SQL without connecting."""
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
