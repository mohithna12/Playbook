"""Resolving a verified token to the caller's identity.

This exists so the API layer never holds an ORM entity. Routers get an
``AuthenticatedUser`` -- an immutable value with the two fields a handler
actually needs -- and the ``User`` row stays behind the repository, which is
what the layering rule in CLAUDE.md asks for (api -> services ->
repositories -> models).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.identity import AuthenticatedUser
from app.repositories.user import UserRepository

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.auth import AuthenticatedSubject


class IdentityService:
    def __init__(self, session: AsyncSession) -> None:
        self._users = UserRepository(session)

    async def resolve(self, subject: AuthenticatedSubject) -> AuthenticatedUser:
        """Map a verified token to a user, provisioning on first sight."""
        user = await self._users.get_or_create(subject.subject, subject.email)
        return AuthenticatedUser(
            id=user.id,
            auth_subject=user.auth_subject,
            email=user.email,
            display_name=user.display_name,
            timezone=user.timezone,
        )

    async def get(self, user_id: uuid.UUID) -> AuthenticatedUser | None:
        user = await self._users.get_by_id(user_id)
        if user is None:
            return None
        return AuthenticatedUser(
            id=user.id,
            auth_subject=user.auth_subject,
            email=user.email,
            display_name=user.display_name,
            timezone=user.timezone,
        )


__all__ = ["IdentityService"]
