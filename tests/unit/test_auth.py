"""Clerk JWT verification.

Tokens are signed with a locally generated RSA key and served through a fake
JWKS endpoint, so these tests exercise the real ``jwt.decode`` path -- signature
verification included -- without a network call or a Clerk account.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.auth import ClerkVerifier, JWKSCache
from app.core.errors import UnauthorizedError

ISSUER = "https://clerk.test.example"
AUDIENCE = "playbook"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
KID = "test-key-1"
OTHER_KID = "test-key-2"


@pytest.fixture(scope="module")
def keypair() -> tuple[Any, dict[str, Any]]:
    """An RSA key and its JWK, generated once for the module."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return private_key, jwk


class StubCache(JWKSCache):
    """A JWKS cache backed by an in-memory key set; counts refresh attempts."""

    def __init__(self, keys: dict[str, Any], *, fail: bool = False) -> None:
        super().__init__(JWKS_URL)
        self._source = keys
        self._fail = fail
        self.fetch_count = 0

    async def _fetch(self) -> None:
        self.fetch_count += 1
        self._last_attempt_at = time.monotonic()
        if self._fail:
            raise ValueError("JWKS unavailable")
        self._keys = dict(self._source)
        self._fetched_at = time.monotonic()


def make_verifier(
    jwk: dict[str, Any],
    *,
    audience: str | None = AUDIENCE,
    keys: dict[str, Any] | None = None,
) -> ClerkVerifier:
    verifier = ClerkVerifier(JWKS_URL, ISSUER, audience)
    verifier._cache = StubCache(keys if keys is not None else {KID: jwk})
    return verifier


def make_token(
    private_key: Any,
    *,
    subject: str = "user_2abc",
    issuer: str = ISSUER,
    audience: str | None = AUDIENCE,
    expires_in: int = 60,
    kid: str = KID,
    algorithm: str = "RS256",
    email: str | None = None,
    omit: set[str] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
    }
    if audience is not None:
        claims["aud"] = audience
    if email is not None:
        claims["email"] = email
    for key in omit or set():
        claims.pop(key, None)
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})


async def test_valid_token_yields_the_subject(keypair: tuple[Any, dict[str, Any]]) -> None:
    private_key, jwk = keypair
    verifier = make_verifier(jwk)

    result = await verifier.verify(make_token(private_key, email="a@example.com"))

    assert result.subject == "user_2abc"
    assert result.email == "a@example.com"
    assert result.expires_at > result.issued_at


async def test_token_without_email_claim_is_still_valid(
    keypair: tuple[Any, dict[str, Any]],
) -> None:
    """Clerk omits email unless the JWT template includes it."""
    private_key, jwk = keypair
    result = await make_verifier(jwk).verify(make_token(private_key))
    assert result.email is None


async def test_expired_token_is_rejected(keypair: tuple[Any, dict[str, Any]]) -> None:
    private_key, jwk = keypair
    token = make_token(private_key, expires_in=-3600)

    with pytest.raises(UnauthorizedError, match="expired"):
        await make_verifier(jwk).verify(token)


async def test_wrong_issuer_is_rejected(keypair: tuple[Any, dict[str, Any]]) -> None:
    private_key, jwk = keypair
    token = make_token(private_key, issuer="https://evil.example")

    with pytest.raises(UnauthorizedError):
        await make_verifier(jwk).verify(token)


async def test_wrong_audience_is_rejected(keypair: tuple[Any, dict[str, Any]]) -> None:
    private_key, jwk = keypair
    token = make_token(private_key, audience="some-other-app")

    with pytest.raises(UnauthorizedError):
        await make_verifier(jwk).verify(token)


async def test_token_signed_by_another_key_is_rejected(
    keypair: tuple[Any, dict[str, Any]],
) -> None:
    """The signature is actually verified, not merely decoded."""
    _, jwk = keypair
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = make_token(attacker_key)

    with pytest.raises(UnauthorizedError):
        await make_verifier(jwk).verify(forged)


async def test_unsigned_token_is_rejected(keypair: tuple[Any, dict[str, Any]]) -> None:
    """`alg: none` must be refused before any key lookup happens."""
    _, jwk = keypair
    now = int(time.time())
    unsigned = jwt.encode(
        {"sub": "user_2abc", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60},
        key=None,
        algorithm="none",
        headers={"kid": KID},
    )

    with pytest.raises(UnauthorizedError, match="algorithm"):
        await make_verifier(jwk).verify(unsigned)


async def test_missing_required_claim_is_rejected(keypair: tuple[Any, dict[str, Any]]) -> None:
    """A token with no `exp` would never expire, so it must not verify."""
    private_key, jwk = keypair
    token = make_token(private_key, omit={"exp"})

    with pytest.raises(UnauthorizedError):
        await make_verifier(jwk).verify(token)


async def test_garbage_token_is_rejected(keypair: tuple[Any, dict[str, Any]]) -> None:
    _, jwk = keypair
    with pytest.raises(UnauthorizedError, match="Malformed"):
        await make_verifier(jwk).verify("not-a-jwt")


async def test_unknown_kid_is_rejected(keypair: tuple[Any, dict[str, Any]]) -> None:
    private_key, jwk = keypair
    token = make_token(private_key, kid=OTHER_KID)

    with pytest.raises(UnauthorizedError, match="signing key"):
        await make_verifier(jwk).verify(token)


async def test_unknown_kids_do_not_amplify_into_upstream_fetches(
    keypair: tuple[Any, dict[str, Any]],
) -> None:
    """A stream of garbage kids must not become a request amplifier on Clerk.

    The cooldown is what bounds it: after one fetch, further misses are
    answered from the cache until the cooldown elapses.
    """
    private_key, jwk = keypair
    verifier = make_verifier(jwk)
    cache: StubCache = verifier._cache  # type: ignore[assignment]

    for _ in range(20):
        with pytest.raises(UnauthorizedError):
            await verifier.verify(make_token(private_key, kid=OTHER_KID))

    assert cache.fetch_count == 1


async def test_key_rotation_is_picked_up_before_the_ttl_expires(
    keypair: tuple[Any, dict[str, Any]],
) -> None:
    """A kid we have never seen forces a refresh once the cooldown has passed.

    Without this, a Clerk key rotation would reject every token for up to the
    24 h cache TTL.
    """
    private_key, jwk = keypair
    verifier = make_verifier(jwk)
    cache: StubCache = verifier._cache  # type: ignore[assignment]

    await verifier.verify(make_token(private_key))
    assert cache.fetch_count == 1

    rotated = dict(jwk)
    rotated["kid"] = OTHER_KID
    cache._source = {OTHER_KID: rotated}
    cache._last_attempt_at = 0.0  # cooldown elapsed

    result = await verifier.verify(make_token(private_key, kid=OTHER_KID))

    assert result.subject == "user_2abc"
    assert cache.fetch_count == 2


async def test_jwks_outage_serves_the_stale_cache(keypair: tuple[Any, dict[str, Any]]) -> None:
    """A Clerk outage must not log out users whose signing key we already hold."""
    private_key, jwk = keypair
    verifier = make_verifier(jwk)
    cache: StubCache = verifier._cache  # type: ignore[assignment]

    await verifier.verify(make_token(private_key))

    cache._fail = True
    cache._fetched_at = 0.0  # force staleness
    cache._last_attempt_at = 0.0

    result = await verifier.verify(make_token(private_key))
    assert result.subject == "user_2abc"


async def test_jwks_failure_with_empty_cache_propagates(
    keypair: tuple[Any, dict[str, Any]],
) -> None:
    """With nothing cached there is nothing to fall back to -- fail, don't allow."""
    private_key, jwk = keypair
    verifier = ClerkVerifier(JWKS_URL, ISSUER, AUDIENCE)
    verifier._cache = StubCache({KID: jwk}, fail=True)

    with pytest.raises(ValueError, match="unavailable"):
        await verifier.verify(make_token(private_key))


async def test_audience_verification_is_skipped_when_unconfigured(
    keypair: tuple[Any, dict[str, Any]],
) -> None:
    """Development runs without an audience; production settings forbid it."""
    private_key, jwk = keypair
    verifier = make_verifier(jwk, audience=None)

    result = await verifier.verify(make_token(private_key, audience=None))
    assert result.subject == "user_2abc"
