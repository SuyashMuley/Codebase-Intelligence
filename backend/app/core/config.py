from functools import lru_cache
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Look for .env in both the repo root and backend/, independent of the directory the API is
# started from. Later files win, so backend/.env overrides the root one. Real environment
# variables override both.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILES = (_BACKEND_DIR.parent / ".env", _BACKEND_DIR / ".env")


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (and .env locally)."""

    # hide_input_in_errors: a bad secret must never be echoed in a startup traceback.
    model_config = SettingsConfigDict(env_file=ENV_FILES, extra="ignore", hide_input_in_errors=True)

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

    # --- Embeddings (Cloudflare Workers AI, or an offline hash embedder) ---
    # "cloudflare" needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN. "hash" is a
    # deterministic offline embedder for tests and demos (lexical, not semantic).
    embedding_provider: Literal["cloudflare", "hash"] = "cloudflare"
    cloudflare_account_id: str | None = None
    cloudflare_api_token: SecretStr | None = None
    cloudflare_api_base: str = "https://api.cloudflare.com/client/v4"
    cloudflare_embedding_model: str = "@cf/baai/bge-small-en-v1.5"
    # bge models are trained with CLS pooling; "mean" is only kept for experiments.
    cloudflare_pooling: Literal["cls", "mean"] = "cls"
    embedding_dim: int = 384  # fixed by the database column; the model must output this size
    embedding_batch_size: int = 32  # texts per request
    # bge's documented instruction for search queries (documents get none).
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embedding_timeout_seconds: float = 30.0
    embedding_budget_seconds: float = 20.0  # one tick embeds for at most this long

    # --- Chat model (OpenRouter). Free models change often: set OPENROUTER_MODEL to pick one ---
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    # Free models come and go and are often slow, empty or rate limited. These two answered
    # correctly in a side-by-side check on this project's real prompts (docs/PLAN.md, M7); check
    # again if answers get slow. "openrouter/free" picks whichever free model is answering.
    openrouter_fallback_model: str | None = "baidu/ernie-5.5-flash-preview:free"
    # "low" asks reasoning models to think briefly (faster, fewer empty replies); blank to omit.
    openrouter_reasoning_effort: str | None = "low"
    llm_timeout_seconds: float = 25.0  # one model attempt
    llm_budget_seconds: float = 28.0  # a whole call including the fallback; see OpenRouterClient
    ai_rate_limit_per_minute: int = 5  # per user; each call is a model request

    # --- Question answering (defaults measured with docs/PLAN.md's M7 evaluation) ---
    # Any question may be asked. By default nothing is refused for being "too far" from the code:
    # the model may decline, and every claim must quote the retrieved code or it is dropped. Set
    # this above 0 to refuse, without a model call, questions whose closest chunk is less similar
    # than this (and that name no existing symbol). bge-small-en-v1.5 scores unrelated questions
    # 0.45 to 0.73, so a useful value is high and also refuses real questions (docs/PLAN.md, M7).
    qa_min_similarity: float = 0.0
    qa_high_similarity: float = 0.80  # at or above this the retrieval is called strong
    qa_retrieve_k: int = 10  # chunks retrieved by search
    qa_context_chunks: int = 6  # most excerpts shown to the model (98% as good as 8, cheaper)
    qa_context_chars: int = 12_000  # most characters of code shown to the model
    # Graph neighbours are off: measured no gain, and a loss when they take a search hit's slot.
    qa_expand_max: int = 0
    chat_budget_seconds: float = 28.0  # whole question, retries included (proxy gives up at ~30)
    chat_rate_limit_per_minute: int = 5  # per user
    chat_daily_limit: int = 25  # model-backed questions per user per repository per day

    # --- Parsing and chunking ---
    chunk_max_chars: int = 1800  # about 450 tokens: fits small embedding models
    chunk_merge_below: int = 500  # neighbouring chunks smaller than this are merged
    max_chunks: int = 6_000  # per snapshot; bounds embedding cost and database size
    parse_batch_files: int = 20  # files read from the database at a time
    parse_budget_seconds: float = 15.0  # one tick parses files for at most this long

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

    # --- Sign in with GitHub (GitHub App user authorization) ---
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    github_oauth_base: str = "https://github.com"
    # Public URL of the FRONTEND. The OAuth callback lives on it (it proxies /api to this API).
    app_base_url: str = "http://localhost:3000"
    # Signs and encrypts the short-lived login state cookie.
    session_secret: SecretStr | None = None
    # Encrypts GitHub tokens at rest: 32 random bytes, url-safe base64 (a Fernet key).
    token_encryption_key: SecretStr | None = None
    cookie_secure: bool = True  # false only for plain-http local development
    session_absolute_days: int = 14
    session_idle_hours: int = 24
    login_state_ttl_seconds: int = 600
    ip_rate_limit_per_minute: int = 600  # backstop per IP on all /api routes
    auth_rate_limit_per_minute: int = 30  # per IP on the sign-in endpoints
    search_rate_limit_per_minute: int = 20  # per user; each search embeds the query

    @field_validator(
        "github_token",
        "github_app_client_id",
        "github_app_client_secret",
        "session_secret",
        "token_encryption_key",
        "alembic_database_url",
        "cloudflare_account_id",
        "cloudflare_api_token",
        "openrouter_api_key",
        "openrouter_fallback_model",
        "openrouter_reasoning_effort",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        # docker compose passes unset variables as empty strings.
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _validate_auth(self) -> "Settings":
        if self.token_encryption_key is not None:
            try:
                Fernet(self.token_encryption_key.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError(
                    "TOKEN_ENCRYPTION_KEY must be 32 random bytes, url-safe base64 encoded."
                ) from None
        if self.environment == "production":
            problems = []
            if not self.auth_configured:
                problems.append("GitHub App credentials, SESSION_SECRET and TOKEN_ENCRYPTION_KEY")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE=true")
            if not self.app_base_url.startswith("https://"):
                problems.append("an https APP_BASE_URL")
            if self.client_key_salt == "dev-salt-change-me":
                problems.append("a non-default CLIENT_KEY_SALT")
            if problems:
                raise ValueError("Production requires " + ", ".join(problems) + ".")
        return self

    @property
    def auth_configured(self) -> bool:
        return bool(
            self.github_app_client_id
            and self.github_app_client_secret
            and self.session_secret
            and self.token_encryption_key
        )

    @property
    def callback_url(self) -> str:
        return self.app_base_url.rstrip("/") + "/api/auth/github/callback"

    @property
    def allowed_origins(self) -> set[str]:
        return {o.rstrip("/") for o in [self.app_base_url, *self.cors_origin_list]}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
