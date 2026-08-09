"""Application configuration via environment variables.

Uses pydantic-settings to validate and type all config at startup.
Secrets come from AWS Secrets Manager in production, .env locally.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application --
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_host: str = "0.0.0.0"  # noqa: S104
    api_port: int = 8000
    api_workers: int = 1
    service_version: str = "0.1.0"
    # Empty disables CORS entirely, which is correct for a same-origin deploy
    # behind the ALB. The dev frontend origin goes here. Accepts a
    # comma-separated string -- pydantic-settings otherwise demands JSON for
    # list fields, and `["http://localhost:3000"]` in a .env file is a trap.
    cors_allow_origins: Annotated[list[str], NoDecode] = []

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    # -- Telemetry --
    # Head sampling ratio in production only; see app.core.telemetry.
    otel_sample_ratio: float = 0.1

    # -- Database --
    database_url: str = "postgresql+asyncpg://playbook:playbook@localhost:5432/playbook"
    database_pool_size: int = 10
    database_max_overflow: int = 5
    database_statement_timeout_ms: int = 5000

    # -- Redis --
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_db: int = 1
    redis_rate_limit_db: int = 2
    redis_lock_db: int = 3
    redis_socket_timeout_seconds: float = 2.0

    # -- Auth (Clerk) --
    clerk_jwks_url: str = ""
    clerk_issuer: str = ""
    # Clerk's `azp` claim. Empty skips audience verification, which is only
    # acceptable in development -- see the production validator below.
    clerk_audience: str = ""

    # -- AWS --
    aws_region: str = "us-west-2"
    s3_raw_bucket: str = "playbook-raw"
    s3_models_bucket: str = "playbook-models"
    s3_features_bucket: str = "playbook-features"
    s3_endpoint_url: str = ""  # Set to http://localhost:4566 for LocalStack

    # -- External APIs --
    odds_api_key: str = ""

    # -- LLM --
    anthropic_api_key: str = ""
    # Alias, not a dated snapshot: the explanation engine has no pinned-version
    # requirement, and a dated ID silently retires. The prior default
    # ("claude-sonnet-4-5-20241022") was not a real model ID at all.
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.3
    llm_daily_spend_cap_usd: float = 5.00

    # -- Simulation --
    default_n_simulations: int = 50_000
    max_n_simulations: int = 100_000

    # -- Rate Limiting --
    rate_limit_reads_per_min: int = 120
    rate_limit_writes_per_min: int = 20
    rate_limit_explain_per_min: int = 10

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @model_validator(mode="after")
    def _require_auth_config_in_production(self) -> "Settings":
        """Refuse to boot a production pod that cannot verify tokens.

        Every auth setting has a permissive default so tests and local runs
        need no configuration. That convenience is exactly what would let a
        misconfigured production deploy come up serving unauthenticated
        traffic, so the defaults are rejected here instead of at first request.
        """
        if self.environment != "production":
            return self
        missing = [
            name
            for name in ("clerk_jwks_url", "clerk_issuer", "clerk_audience")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(
                f"Production requires authentication settings: {', '.join(sorted(missing))}"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton -- settings are read once at startup."""
    return Settings()
