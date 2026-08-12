"""FastAPI dependencies: database session, Redis, current user, rate limits.

``CurrentUser`` is the one every protected endpoint uses. It verifies the Clerk
token and resolves it to a ``users`` row in a single dependency, so a handler
can never accidentally hold a subject that has no user record.

The rate-limit dependencies are per-tier factories rather than middleware
because the tier is a property of the route, not the path -- ``POST /explain``
and ``POST /leagues/import`` differ, and middleware would have to re-derive
that from the URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.auth import AuthenticatedSubject, get_verifier
from app.core.db import get_session
from app.core.errors import RateLimitedError, UnauthorizedError
from app.core.rate_limit import LimitTier
from app.domain.identity import AuthenticatedUser
from app.services.identity import IdentityService

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

# These annotations are resolved at runtime -- FastAPI builds each handler's
# signature from them to decide what to inject. Deferring the imports into a
# TYPE_CHECKING block turns every dependency into an unresolvable forward
# reference, and FastAPI then treats the parameter as a request body: the
# symptom is a 422 on requests that should have been rejected as 401.

# auto_error=False so a missing header reaches our handler and returns a
# problem+json 401 like every other error, instead of Starlette's bare JSON.
_bearer = HTTPBearer(auto_error=False, scheme_name="ClerkJWT")

DbSession = Annotated[AsyncSession, Depends(get_session)]

BEARER_PREFIX = "bearer "


def _bearer_token(header: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not header or not header.lower().startswith(BEARER_PREFIX):
        return None
    return header[len(BEARER_PREFIX) :].strip() or None


async def get_auth_subject(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedSubject:
    """Verify the bearer token. Raises 401 on anything short of a valid one."""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Authentication required")
    return await get_verifier().verify(credentials.credentials)


AuthSubject = Annotated[AuthenticatedSubject, Depends(get_auth_subject)]


async def get_current_user(subject: AuthSubject, session: DbSession) -> AuthenticatedUser:
    """The caller's identity, provisioned on first sight of a Clerk subject."""
    return await IdentityService(session).resolve(subject)


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


def rate_limited(tier: LimitTier) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build a dependency enforcing ``tier``'s budget for the caller.

    Identity is the authenticated user when there is one and the client IP
    otherwise. The token is verified here rather than reusing ``CurrentUser``
    so that an unauthenticated flood is still shed -- and so limiting does not
    depend on a database round trip.
    """

    async def dependency(request: Request) -> None:
        # The header is read directly rather than through the HTTPBearer
        # security dependency. Declaring a security scheme here would put a
        # `security` requirement on every route that rate-limits -- including
        # the deliberately public ones -- and the OpenAPI spec would then
        # promise authentication those endpoints do not enforce.
        identity = f"ip:{request.client.host if request.client else 'unknown'}"
        token = _bearer_token(request.headers.get("authorization"))
        if token:
            try:
                subject = await get_verifier().verify(token)
                identity = f"user:{subject.subject}"
            except (UnauthorizedError, RuntimeError):
                # Leave the IP identity in place. If the endpoint requires
                # auth, its own dependency rejects the request; this one only
                # decides which bucket the attempt is counted against.
                pass

        result = await rate_limit.check(identity, tier)
        # Handed to the response middleware, which attaches RateLimit-* headers
        # to successful responses as well as rejected ones.
        request.state.rate_limit = result

        if not result.allowed:
            raise RateLimitedError(
                retry_after=result.reset_seconds,
                detail=f"Rate limit of {result.limit} requests per minute exceeded",
                headers=result.headers(),
            )

    return dependency


ReadRateLimit = Depends(rate_limited(LimitTier.READ))
WriteRateLimit = Depends(rate_limited(LimitTier.WRITE))
ExplainRateLimit = Depends(rate_limited(LimitTier.EXPLAIN))

# Long enough for a UUID or a hash, short enough that it cannot be used as a
# side channel to stash data on the job row.
IDEMPOTENCY_KEY_MAX_LENGTH = 255


async def get_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
            description=(
                "Replaying a key within 24h returns the original job instead of "
                "starting a second one."
            ),
        ),
    ] = None,
) -> str | None:
    """The caller's idempotency key, if any (RFC 12.1).

    Optional by design. A client that retries without one gets a second job,
    which is the correct default for a header it never sent -- guessing a key
    from the request body would silently collapse two deliberate submissions
    into one.
    """
    if idempotency_key is None:
        return None
    stripped = idempotency_key.strip()
    return stripped or None


IdempotencyKey = Annotated[str | None, Depends(get_idempotency_key)]


__all__ = [
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "AuthSubject",
    "CurrentUser",
    "DbSession",
    "ExplainRateLimit",
    "IdempotencyKey",
    "ReadRateLimit",
    "WriteRateLimit",
    "get_auth_subject",
    "get_current_user",
    "get_idempotency_key",
    "rate_limited",
]
