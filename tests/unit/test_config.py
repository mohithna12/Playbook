"""Tests for application configuration."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

PRODUCTION_AUTH = {
    "clerk_jwks_url": "https://clerk.example.com/.well-known/jwks.json",
    "clerk_issuer": "https://clerk.example.com",
    "clerk_audience": "fantasyai",
}


def test_default_settings() -> None:
    """Settings should load with defaults when no env vars are set."""
    settings = Settings()
    assert settings.environment == "development"
    assert settings.is_development is True
    assert settings.is_production is False
    assert settings.api_port == 8000
    assert settings.database_pool_size == 10
    assert settings.default_n_simulations == 50_000
    assert settings.max_n_simulations == 100_000


def test_production_flag() -> None:
    settings = Settings(environment="production", **PRODUCTION_AUTH)
    assert settings.is_production is True
    assert settings.is_development is False


def test_production_requires_auth_settings() -> None:
    """A production deploy without Clerk config must fail at construction.

    The permissive defaults exist for tests and local runs; without this guard
    they would let a misconfigured production pod boot and serve traffic it
    cannot authenticate.
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(environment="production")

    message = str(excinfo.value)
    assert "clerk_jwks_url" in message
    assert "clerk_issuer" in message
    assert "clerk_audience" in message


def test_non_production_tolerates_missing_auth_settings() -> None:
    for environment in ("development", "staging"):
        assert Settings(environment=environment).clerk_issuer == ""


def test_cors_origins_parse_from_a_comma_separated_string() -> None:
    """`.env` files hold plain strings; pydantic-settings wants JSON for lists."""
    settings = Settings(cors_allow_origins="http://localhost:3000, https://app.example.com")

    assert settings.cors_allow_origins == [
        "http://localhost:3000",
        "https://app.example.com",
    ]


def test_cors_origins_default_to_empty() -> None:
    """Empty means CORS is off, which is correct for a same-origin deploy."""
    assert Settings().cors_allow_origins == []


def test_cors_origins_accept_a_list_unchanged() -> None:
    assert Settings(cors_allow_origins=["https://a.example"]).cors_allow_origins == [
        "https://a.example"
    ]
