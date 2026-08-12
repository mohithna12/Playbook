"""Base repository and the scoping rule that carries authorization.

Authorization lives here, not in routers (RFC 20.1). Every league-scoped query
joins through ``league_memberships`` on the caller's ``user_id``. The property
that buys is *fail-closed*: a new endpoint whose author forgets an ownership
check returns an empty result set, not another user's league. A check placed in
a handler is a check that gets forgotten when the next handler is written.

Concretely, that means no repository method reachable from the API takes a
league or team id without also taking a ``user_id``, and no method builds a
league-scoped statement except through :class:`ScopedRepository`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from sqlalchemy import Select, select

from app.models.league import LeagueMembership

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.base import Base

ModelT = TypeVar("ModelT", bound="Base")

# Keyset pagination caps (RFC 12.1). Offset pagination is not offered: both
# paginated collections are insert-heavy, and offset paging over an
# insert-heavy table skips and duplicates rows between pages.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class Repository(Generic[ModelT]):
    """Holds a session. Repositories are cheap; construct one per request."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session


class ScopedRepository(Repository[ModelT]):
    """A repository whose rows are reachable only via a league membership.

    Subclasses name the attribute on ``model`` that points at ``leagues.id``
    and get :meth:`scoped` for free.
    """

    # The attribute *name*, not the column object. Assigning an
    # InstrumentedAttribute to a plain class would make every access go through
    # SQLAlchemy's descriptor protocol, which then tries to read ORM state off
    # the repository instance and raises UnmappedInstanceError.
    league_id_attr: str

    @property
    def league_id_column(self) -> Any:
        return getattr(self.model, self.league_id_attr)

    def scoped(self, user_id: uuid.UUID) -> Select[tuple[ModelT]]:
        """A SELECT over ``model`` restricted to leagues ``user_id`` belongs to."""
        return (
            select(self.model)
            .join(
                LeagueMembership,
                LeagueMembership.league_id == self.league_id_column,
            )
            .where(LeagueMembership.user_id == user_id)
        )


def rows_affected(result: Any) -> int:
    """The row count of a DML statement.

    ``session.execute`` is typed as returning ``Result``, but an UPDATE or
    DELETE actually yields a ``CursorResult``, which is the only one carrying
    ``rowcount``. Guarded transitions read that count to find out whether they
    won a race, so this narrowing happens in one place rather than at each of
    them.
    """
    return int(result.rowcount)


def clamp_page_size(limit: int | None) -> int:
    """Bound a caller-supplied page size. Absent means the default."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Repository",
    "ScopedRepository",
    "clamp_page_size",
]
