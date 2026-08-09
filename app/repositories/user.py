"""User lookup and just-in-time provisioning from a Clerk subject.

There is no signup endpoint. The first authenticated request from a subject we
have never seen creates the row. Clerk already owns the identity; a separate
registration step would only add a state where a valid token has no user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.user import User
from app.repositories.base import Repository

if TYPE_CHECKING:
    import uuid


class UserRepository(Repository[User]):
    model = User

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self.session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_auth_subject(self, auth_subject: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.auth_subject == auth_subject, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_or_create(self, auth_subject: str, email: str | None) -> User:
        """Resolve a Clerk subject to a user row, creating it on first sight.

        The insert is ``ON CONFLICT DO UPDATE`` on ``auth_subject`` rather than
        read-then-insert: a client that fires several requests in parallel
        right after sign-in would otherwise race, and one of them would get a
        unique-violation 500 on what is a successful sign-in.

        The conflict branch clears ``deleted_at``. Signing in again with the
        same Clerk identity reactivates a soft-deleted account, which is the
        only sensible reading -- the alternative is a valid token that can
        never resolve to a user.
        """
        existing = await self.get_by_auth_subject(auth_subject)
        if existing is not None:
            return existing

        # ``email`` is NOT NULL and unique. Clerk omits it from the token
        # unless the JWT template includes it, so synthesize a stable
        # placeholder; a later profile sync overwrites it.
        resolved_email = email or f"{auth_subject}@users.noreply.playbook.dev"

        statement = (
            insert(User)
            .values(auth_subject=auth_subject, email=resolved_email)
            .on_conflict_do_update(
                index_elements=[User.auth_subject],
                set_={"deleted_at": None, "updated_at": func.now()},
            )
            .returning(User)
        )
        result = await self.session.execute(statement)
        await self.session.flush()
        return result.scalar_one()

    async def update_email(self, user_id: uuid.UUID, email: str) -> None:
        await self.session.execute(
            update(User).where(User.id == user_id).values(email=email, updated_at=func.now())
        )

    async def soft_delete(self, user_id: uuid.UUID) -> None:
        """Mark an account deleted. Memberships and jobs keep referencing it."""
        await self.session.execute(
            update(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .values(deleted_at=func.now())
        )


__all__ = ["UserRepository"]
