from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (and .env locally)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    # App connections go through Neon's pooled endpoint; keep the pool small.
    database_url: str = "postgresql+psycopg://cip:cip@localhost:5432/cip"
    # Migrations need Neon's direct (non-pooled) endpoint. Falls back to database_url.
    alembic_database_url: str | None = None
    db_pool_size: int = Field(default=3, ge=1, le=10)
    db_max_overflow: int = Field(default=2, ge=0, le=10)

    # Comma-separated list of allowed browser origins. No wildcard.
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
