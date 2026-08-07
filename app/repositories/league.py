"""League reads, all scoped to the caller's memberships.

Every method takes ``user_id`` first. That is the convention the fail-closed
authorization posture depends on -- see :mod:`app.repositories.base`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.league import League, LeagueMembership, Team
from app.repositories.base import Repository, ScopedRepository, clamp_page_size

if TYPE_CHECKING:
    import uuid


class LeagueRepository(ScopedRepository[League]):
    model = League
    league_id_attr = "id"

    async def list_for_user(self, user_id: uuid.UUID, *, limit: int | None = None) -> list[League]:
        result = await self.session.execute(
            self.scoped(user_id).order_by(League.created_at.desc()).limit(clamp_page_size(limit))
        )
        return list(result.scalars().all())

    async def get(self, user_id: uuid.UUID, league_id: uuid.UUID) -> League | None:
        """A league the caller is a member of, or None.

        None covers both "no such league" and "not yours", and the router turns
        both into 404. A 403 would confirm the league exists, which leaks
        membership of other people's leagues to anyone guessing ids.
        """
        result = await self.session.execute(self.scoped(user_id).where(League.id == league_id))
        return result.scalar_one_or_none()

    async def is_member(self, user_id: uuid.UUID, league_id: uuid.UUID) -> bool:
        result = await self.session.execute(
            select(LeagueMembership.league_id).where(
                LeagueMembership.user_id == user_id,
                LeagueMembership.league_id == league_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def claimed_team_id(self, user_id: uuid.UUID, league_id: uuid.UUID) -> uuid.UUID | None:
        """The team the user owns in this league, if they claimed one.

        Write operations (lineup drafts) check this; reads do not, because
        fantasy leagues are not private among their members (RFC 20.1).
        """
        result = await self.session.execute(
            select(LeagueMembership.team_id).where(
                LeagueMembership.user_id == user_id,
                LeagueMembership.league_id == league_id,
            )
        )
        return result.scalar_one_or_none()


class TeamRepository(ScopedRepository[Team]):
    model = Team
    league_id_attr = "league_id"

    async def list_for_league(self, user_id: uuid.UUID, league_id: uuid.UUID) -> list[Team]:
        result = await self.session.execute(
            self.scoped(user_id)
            .where(Team.league_id == league_id)
            .order_by(Team.wins.desc(), Team.points_for.desc())
        )
        return list(result.scalars().all())

    async def get(self, user_id: uuid.UUID, team_id: uuid.UUID) -> Team | None:
        result = await self.session.execute(self.scoped(user_id).where(Team.id == team_id))
        return result.scalar_one_or_none()


class MembershipRepository(Repository[LeagueMembership]):
    model = LeagueMembership

    async def add(
        self,
        user_id: uuid.UUID,
        league_id: uuid.UUID,
        team_id: uuid.UUID | None = None,
    ) -> LeagueMembership:
        membership = LeagueMembership(user_id=user_id, league_id=league_id, team_id=team_id)
        self.session.add(membership)
        await self.session.flush()
        return membership


__all__ = ["LeagueRepository", "MembershipRepository", "TeamRepository"]
