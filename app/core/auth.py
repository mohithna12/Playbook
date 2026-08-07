"""Clerk JWT verification against a cached JWKS.

Clerk issues short-lived (60 s) RS256 tokens; the frontend refreshes them
transparently. The backend never sees a password and stores no credential
(RFC 20.1). This module's whole job is: bytes on the wire -> a verified
:class:`AuthenticatedSubject`, or an exception.

The JWKS is cached for 24 h because Clerk rotates signing keys rarely and a
fetch on every request would put a third-party HTTP call in the hot path of
every authenticated endpoint. Two consequences are handled explicitly:

* An unknown ``kid`` forces one refresh (subject to a cooldown), so a rotation
  is picked up in seconds rather than at cache expiry. The cooldown is what
  keeps a stream of garbage ``kid`` values from becoming a request amplifier
  against Clerk.
* A refresh failure serves the stale cache rather than failing closed. A Clerk
  outage should not log out every user holding a token signed by a key we
  already have.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import structlog

from app.core.config import get_settings
from app.core.errors import UnauthorizedError

if TYPE_CHECKING:
    from jwt.types import Options as JWTOptions

logger = structlog.get_logger()

JWKS_TTL_SECONDS = 24 * 60 * 60
JWKS_REFRESH_COOLDOWN_SECONDS = 60
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
ALGORITHMS = ["RS256"]


@dataclass(frozen=True, slots=True)
class AuthenticatedSubject:
    """A verified token's claims. Not yet a FantasyAI user -- see the user repo.

    ``subject`` is Clerk's ``sub``, which is the stable join key to
    ``users.auth_subject``. Email is carried through when Clerk includes it in
    the token so first-time sign-in can create the row without a callback to
    Clerk's API.
    """

    subject: str
    email: str | None
    issued_at: int
    expires_at: int


class JWKSCache:
    """Time-bounded JWKS cache with rotation-triggered refresh."""

    def __init__(self, jwks_url: str, ttl_seconds: int = JWKS_TTL_SECONDS) -> None:
        self._jwks_url = jwks_url
        self._ttl = ttl_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._last_attempt_at = 0.0

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self._fetched_at >= self._ttl

    def _may_refetch(self) -> bool:
        return time.monotonic() - self._last_attempt_at >= JWKS_REFRESH_COOLDOWN_SECONDS

    async def _fetch(self) -> None:
        self._last_attempt_at = time.monotonic()
        async with httpx.AsyncClient(timeout=JWKS_FETCH_TIMEOUT_SECONDS) as client:
            response = await client.get(self._jwks_url)
            response.raise_for_status()
            document = response.json()

        keys = {k["kid"]: k for k in document.get("keys", []) if "kid" in k}
        if not keys:
            raise ValueError("JWKS document contained no usable keys")

        self._keys = keys
        self._fetched_at = time.monotonic()
        await logger.ainfo("jwks_refreshed", key_count=len(keys))

    async def _refresh(self, *, required: bool) -> None:
        """Fetch, tolerating failure when a usable cache is already in hand."""
        try:
            await self._fetch()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if required or not self._keys:
                await logger.aerror("jwks_fetch_failed", error=str(exc))
                raise
            await logger.awarning("jwks_refresh_failed_serving_stale", error=str(exc))

    async def get_key(self, kid: str) -> Any:
        """Return the signing key for ``kid``, refreshing on miss or expiry."""
        if not self._keys or self.is_stale:
            await self._refresh(required=not self._keys)

        if kid not in self._keys and self._may_refetch():
            await self._refresh(required=False)

        if kid not in self._keys:
            raise UnauthorizedError("Token signing key is not recognized")

        return self._keys[kid]


class ClerkVerifier:
    """Verifies Clerk-issued access tokens."""

    def __init__(
        self,
        jwks_url: str,
        issuer: str,
        audience: str | None,
        *,
        leeway_seconds: int = 10,
    ) -> None:
        self._cache = JWKSCache(jwks_url)
        self._issuer = issuer
        self._audience = audience or None
        # Clerk tokens live 60 s; a little leeway absorbs clock skew between
        # Clerk's issuer and our pods without meaningfully widening the window.
        self._leeway = leeway_seconds

    async def verify(self, token: str) -> AuthenticatedSubject:
        """Verify signature, expiry, issuer, and audience. Raises on any failure."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Malformed authentication token") from exc

        kid = header.get("kid")
        if not kid:
            raise UnauthorizedError("Token header is missing a key id")
        if header.get("alg") not in ALGORITHMS:
            # Reject before touching the key: this is the "alg: none" and
            # HS256-signed-with-the-public-key confusion class of attack.
            raise UnauthorizedError("Unsupported token signing algorithm")

        jwk = await self._cache.get_key(kid)
        key = jwt.PyJWK(jwk, algorithm="RS256").key

        # `require` makes the three claims mandatory rather than merely
        # checked-if-present; a token without `exp` would otherwise never
        # expire. verify_aud is disabled only when no audience is configured,
        # which production settings validation forbids.
        options: JWTOptions = {
            "require": ["exp", "iat", "sub"],
            "verify_aud": self._audience is not None,
        }

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options=options,
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError("Authentication token has expired") from exc
        except jwt.InvalidTokenError as exc:
            # One message for every verification failure. Distinguishing "bad
            # signature" from "wrong issuer" tells an attacker which knob to
            # turn next and tells a legitimate client nothing it can act on.
            raise UnauthorizedError("Authentication token is not valid") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise UnauthorizedError("Authentication token has no subject")

        email = claims.get("email")
        return AuthenticatedSubject(
            subject=subject,
            email=email if isinstance(email, str) else None,
            issued_at=int(claims["iat"]),
            expires_at=int(claims["exp"]),
        )


_verifier: ClerkVerifier | None = None


def get_verifier() -> ClerkVerifier:
    """Process-wide verifier, so the JWKS cache is shared across requests."""
    global _verifier
    if _verifier is None:
        settings = get_settings()
        if not settings.clerk_jwks_url or not settings.clerk_issuer:
            raise RuntimeError(
                "CLERK_JWKS_URL and CLERK_ISSUER must be set to verify authentication tokens"
            )
        _verifier = ClerkVerifier(
            jwks_url=settings.clerk_jwks_url,
            issuer=settings.clerk_issuer,
            audience=settings.clerk_audience,
        )
    return _verifier


def reset_verifier() -> None:
    """Drop the cached verifier. For tests and config reloads."""
    global _verifier
    _verifier = None


__all__ = [
    "AuthenticatedSubject",
    "ClerkVerifier",
    "JWKSCache",
    "get_verifier",
    "reset_verifier",
]
