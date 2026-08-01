"""Application configuration via environment variables.

Uses pydantic-settings to validate and type all config at startup.
Secrets come from AWS Secrets Manager in production, .env locally.
"""

from functools import lru_cache
from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # -- Database --
    database_url: str = "postgresql+asyncpg://fantasyai:fantasyai@localhost:5432/fantasyai"
    database_pool_size: int = 10
    database_max_overflow: int = 5
    database_statement_timeout_ms: int = 5000

    # -- Redis --
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_db: int = 1
    redis_rate_limit_db: int = 2
    redis_lock_db: int = 3

    # -- Auth (Clerk) --
    clerk_jwks_url: str = ""
    clerk_issuer: str = ""
    clerk_audience: str = ""

    # -- AWS --
    aws_region: str = "us-west-2"
    s3_raw_bucket: str = "fantasyai-raw"
    s3_models_bucket: str = "fantasyai-models"
    s3_features_bucket: str = "fantasyai-features"
    s3_endpoint_url: str = ""  # Set to http://localhost:4566 for LocalStack

    # -- External APIs --
    odds_api_key: str = ""

    # -- LLM --
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-5-20241022"
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton -- settings are read once at startup."""
    return Settings()
