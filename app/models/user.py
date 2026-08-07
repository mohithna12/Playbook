"""User identity. RFC Section 11.3."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """A FantasyAI account, keyed to a Clerk subject.

    Soft-deleted (``deleted_at``) rather than removed, because league
    memberships and jobs reference users and an account deletion should not
    cascade into the audit trail.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("public.uuidv7()")
    )
    auth_subject: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'America/New_York'")
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "idx_users_auth_subject",
            "auth_subject",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
