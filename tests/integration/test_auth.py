"""End-to-end authentication and the fail-closed authorization posture.

The claim under test is RFC 20.1's: authorization is enforced in the
repository layer, so a query for another user's league returns nothing rather
than relying on a router remembering to check.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import CurrentUser
from app.core import auth as auth_module
from app.core.auth import ClerkVerifier
from app.core.db import get_session
from app.core.errors import PROBLEM_JSON, register_error_handlers
from app.models.league import League, LeagueMembership, Team
from app.repositories.league import LeagueRepository, TeamRepository
from app.repositories.user import UserRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ISSUER = "https://clerk.test.example"
AUDIENCE = "playbook"
KID = "integration-key"


@pytest.fixture(scope="module")
def signing_key() -> tuple[Any, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return private_key, jwk


def issue_token(private_key: Any, subject: str, email: str | None = None) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 60,
    }
    if email:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
async def api_client(
    session: AsyncSession,
    signing_key: tuple[Any, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """A probe app with one protected route, wired to the test session.

    ``/v1/leagues`` and friends do not exist until M5, so the guarantee under
    test -- that the auth dependency yields a real ``users`` row -- needs a
    route of its own.
    """
    _, jwk = signing_key

    verifier = ClerkVerifier(f"{ISSUER}/jwks", ISSUER, AUDIENCE)
    verifier._cache._keys = {KID: jwk}
    verifier._cache._fetched_at = time.monotonic()
    monkeypatch.setattr(auth_module, "_verifier", verifier)

    probe = FastAPI()
    register_error_handlers(probe)

    @probe.get("/v1/me")
    async def _me(user: CurrentUser) -> dict[str, str]:
        return {"user_id": str(user.id), "auth_subject": user.auth_subject}

    probe.dependency_overrides[get_session] = lambda: session

    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def make_league(session: AsyncSession, name: str) -> League:
    league = League(
        provider="SLEEPER",
        external_id=f"ext-{uuid.uuid4()}",
        season=2025,
        name=name,
        total_teams=12,
        scoring_rules={"schema_version": 1},
        roster_positions=["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DST"],
        playoff_config={"teams": 6},
    )
    session.add(league)
    await session.flush()
    return league


class TestUserProvisioning:
    async def test_first_sight_of_a_subject_creates_the_user(self, session: AsyncSession) -> None:
        repo = UserRepository(session)

        user = await repo.get_or_create("user_clerk_1", "a@example.com")

        assert user.id is not None
        assert user.auth_subject == "user_clerk_1"
        assert user.email == "a@example.com"

    async def test_the_same_subject_resolves_to_the_same_row(self, session: AsyncSession) -> None:
        repo = UserRepository(session)

        first = await repo.get_or_create("user_clerk_2", "b@example.com")
        second = await repo.get_or_create("user_clerk_2", "b@example.com")

        assert first.id == second.id

    async def test_a_token_without_email_still_provisions(self, session: AsyncSession) -> None:
        """`email` is NOT NULL; a Clerk template that omits it must not 500."""
        user = await UserRepository(session).get_or_create("user_clerk_3", None)

        assert user.email.endswith("@users.noreply.playbook.dev")
        assert "user_clerk_3" in user.email

    async def test_signing_in_again_reactivates_a_soft_deleted_account(
        self, session: AsyncSession
    ) -> None:
        repo = UserRepository(session)
        user = await repo.get_or_create("user_clerk_4", "d@example.com")

        await repo.soft_delete(user.id)
        assert await repo.get_by_auth_subject("user_clerk_4") is None

        revived = await repo.get_or_create("user_clerk_4", "d@example.com")

        assert revived.id == user.id
        assert revived.deleted_at is None

    async def test_a_soft_deleted_user_is_not_returned_by_lookups(
        self, session: AsyncSession
    ) -> None:
        repo = UserRepository(session)
        user = await repo.get_or_create("user_clerk_5", "e@example.com")

        await repo.soft_delete(user.id)

        assert await repo.get_by_id(user.id) is None


class TestFailClosedAuthorization:
    """Cross-user access returns empty results, not another user's data."""

    async def test_a_league_is_invisible_to_a_non_member(self, session: AsyncSession) -> None:
        users = UserRepository(session)
        owner = await users.get_or_create("owner_1", "owner1@example.com")
        stranger = await users.get_or_create("stranger_1", "stranger1@example.com")

        league = await make_league(session, "Owner's League")
        session.add(LeagueMembership(user_id=owner.id, league_id=league.id))
        await session.flush()

        leagues = LeagueRepository(session)

        assert await leagues.get(owner.id, league.id) is not None
        # The decisive assertion: no router check was involved.
        assert await leagues.get(stranger.id, league.id) is None

    async def test_listing_returns_only_the_callers_leagues(self, session: AsyncSession) -> None:
        users = UserRepository(session)
        alice = await users.get_or_create("alice_1", "alice1@example.com")
        bob = await users.get_or_create("bob_1", "bob1@example.com")

        alice_league = await make_league(session, "Alice League")
        bob_league = await make_league(session, "Bob League")
        session.add_all(
            [
                LeagueMembership(user_id=alice.id, league_id=alice_league.id),
                LeagueMembership(user_id=bob.id, league_id=bob_league.id),
            ]
        )
        await session.flush()

        visible = await LeagueRepository(session).list_for_user(alice.id)

        assert [league.id for league in visible] == [alice_league.id]

    async def test_teams_in_another_users_league_are_invisible(self, session: AsyncSession) -> None:
        users = UserRepository(session)
        owner = await users.get_or_create("owner_2", "owner2@example.com")
        stranger = await users.get_or_create("stranger_2", "stranger2@example.com")

        league = await make_league(session, "Team Scoping League")
        team = Team(league_id=league.id, external_id="1", display_name="The Team")
        session.add_all([team, LeagueMembership(user_id=owner.id, league_id=league.id)])
        await session.flush()

        teams = TeamRepository(session)

        assert await teams.get(owner.id, team.id) is not None
        assert await teams.get(stranger.id, team.id) is None
        assert await teams.list_for_league(stranger.id, league.id) == []

    async def test_membership_predicate_matches_the_scoped_query(
        self, session: AsyncSession
    ) -> None:
        users = UserRepository(session)
        member = await users.get_or_create("member_1", "member1@example.com")
        outsider = await users.get_or_create("outsider_1", "outsider1@example.com")

        league = await make_league(session, "Membership League")
        session.add(LeagueMembership(user_id=member.id, league_id=league.id))
        await session.flush()

        leagues = LeagueRepository(session)

        assert await leagues.is_member(member.id, league.id) is True
        assert await leagues.is_member(outsider.id, league.id) is False

    async def test_claimed_team_is_scoped_to_the_caller(self, session: AsyncSession) -> None:
        """Writes check the claimed team; it must not resolve for a stranger."""
        users = UserRepository(session)
        owner = await users.get_or_create("owner_3", "owner3@example.com")
        stranger = await users.get_or_create("stranger_3", "stranger3@example.com")

        league = await make_league(session, "Claim League")
        team = Team(league_id=league.id, external_id="7", display_name="Claimed")
        session.add(team)
        await session.flush()
        session.add(LeagueMembership(user_id=owner.id, league_id=league.id, team_id=team.id))
        await session.flush()

        leagues = LeagueRepository(session)

        assert await leagues.claimed_team_id(owner.id, league.id) == team.id
        assert await leagues.claimed_team_id(stranger.id, league.id) is None


class TestAuthenticatedRequests:
    """The HTTP surface of authentication: what a client actually observes."""

    async def test_no_credentials_is_401(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/v1/me")

        assert response.status_code == 401
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json()["type"] == "https://api.playbook.dev/errors/unauthorized"

    async def test_a_garbage_token_is_401(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/v1/me", headers={"Authorization": "Bearer not-a-jwt"})

        assert response.status_code == 401

    async def test_a_token_from_another_issuer_is_401(
        self, api_client: AsyncClient, signing_key: tuple[Any, dict[str, Any]]
    ) -> None:
        private_key, _ = signing_key
        now = int(time.time())
        foreign = jwt.encode(
            {
                "sub": "user_x",
                "iss": "https://evil.example",
                "aud": AUDIENCE,
                "iat": now,
                "exp": now + 60,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": KID},
        )

        response = await api_client.get("/v1/me", headers={"Authorization": f"Bearer {foreign}"})

        assert response.status_code == 401

    async def test_a_valid_token_resolves_to_a_user_row(
        self,
        api_client: AsyncClient,
        signing_key: tuple[Any, dict[str, Any]],
        session: AsyncSession,
    ) -> None:
        private_key, _ = signing_key
        token = issue_token(private_key, "user_http_1", "http1@example.com")

        response = await api_client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        body = response.json()
        assert body["auth_subject"] == "user_http_1"

        persisted = await UserRepository(session).get_by_auth_subject("user_http_1")
        assert persisted is not None
        assert str(persisted.id) == body["user_id"]

    async def test_repeat_requests_reuse_the_same_user(
        self, api_client: AsyncClient, signing_key: tuple[Any, dict[str, Any]]
    ) -> None:
        """Provisioning is idempotent; a second request must not create a row."""
        private_key, _ = signing_key
        headers = {"Authorization": f"Bearer {issue_token(private_key, 'user_http_2')}"}

        first = await api_client.get("/v1/me", headers=headers)
        second = await api_client.get("/v1/me", headers=headers)

        assert first.json()["user_id"] == second.json()["user_id"]

    async def test_two_subjects_get_two_users(
        self, api_client: AsyncClient, signing_key: tuple[Any, dict[str, Any]]
    ) -> None:
        private_key, _ = signing_key

        alice = await api_client.get(
            "/v1/me",
            headers={"Authorization": f"Bearer {issue_token(private_key, 'user_http_a')}"},
        )
        bob = await api_client.get(
            "/v1/me",
            headers={"Authorization": f"Bearer {issue_token(private_key, 'user_http_b')}"},
        )

        assert alice.json()["user_id"] != bob.json()["user_id"]
