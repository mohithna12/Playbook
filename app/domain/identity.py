"""The caller's identity, as the API layer sees it."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class AuthenticatedUser(BaseModel):
    """The current user. Frozen -- a handler must not mutate the caller."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    auth_subject: str
    email: str
    display_name: str | None = None
    timezone: str = "America/New_York"


__all__ = ["AuthenticatedUser"]
