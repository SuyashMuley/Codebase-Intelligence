from functools import lru_cache

from pydantic import Field, SecretStr
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

    # --- GitHub access (token optional: unauthenticated allows ~60 API calls/hour/IP) ---
    github_token: SecretStr | None = None
    github_api_base: str = "https://api.github.com"
    github_timeout_seconds: float = 15.0

    # --- Ingestion limits (free-tier friendly; all overridable by env) ---
    max_tarball_bytes: int = 25 * 1024 * 1024  # compressed download
    max_uncompressed_bytes: int = 150 * 1024 * 1024  # sum of archive member sizes
    max_tar_members: int = 60_000
    max_file_bytes: int = 256 * 1024
    max_files: int = 2_000  # kept source files
    max_total_source_bytes: int = 5 * 1024 * 1024  # kept source text
    fetch_timeout_seconds: float = 40.0

    # --- Job execution ---
    lease_seconds: int = 90  # must exceed fetch_timeout_seconds
    max_attempts: int = 3
    ingestions_per_client_per_day: int = 10  # rolling 24h

    # --- Rate limiting (per client IP, in-process, single instance) ---
    rate_limit_per_minute: int = 120
    index_rate_limit_per_minute: int = 10
    # How many trusted proxies sit in front of the app. 0 = use the socket peer address.
    # Behind Render's proxy set 1; never trust X-Forwarded-For when this is 0.
    forwarded_for_hops: int = Field(default=0, ge=0, le=5)
    # Salt for hashing client IPs into quota keys. Change in production.
    client_key_salt: str = "dev-salt-change-me"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
