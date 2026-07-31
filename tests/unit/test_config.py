"""Tests for application configuration."""

from app.core.config import Settings


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
    settings = Settings(environment="production")
    assert settings.is_production is True
    assert settings.is_development is False
