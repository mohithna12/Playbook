"""SQLAlchemy ORM models.

All models inherit from Base. This module re-exports everything
so Alembic's env.py can import `from app.models import *` to
discover all tables for autogenerate.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass
