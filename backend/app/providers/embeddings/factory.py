from functools import lru_cache

from app.core.config import Settings
from app.core.errors import EmbeddingNotConfigured
from app.providers.embeddings.base import Embedder
from app.providers.embeddings.cloudflare import CloudflareEmbedder, cloudflare_model_id
from app.providers.embeddings.hash import HashEmbedder


@lru_cache
def _cloudflare(
    account_id: str,
    token: str,
    model: str,
    pooling: str,
    dim: int,
    batch: int,
    prefix: str,
    base: str,
    timeout: float,
) -> CloudflareEmbedder:
    return CloudflareEmbedder(account_id, token, model, pooling, dim, batch, prefix, base, timeout)


def embedding_model_id(settings: Settings) -> str:
    """The identifier stored with vectors, known without credentials or a network client."""
    if settings.embedding_provider == "hash":
        return f"hash-{settings.embedding_dim}-v1"
    return cloudflare_model_id(settings.cloudflare_embedding_model, settings.cloudflare_pooling)


def get_embedder(settings: Settings) -> Embedder:
    """The configured embedder. Raises EmbeddingNotConfigured (never silently falls back, so a
    missing key is a clear error rather than quietly worse search)."""
    if settings.embedding_provider == "hash":
        return HashEmbedder(settings.embedding_dim, settings.embedding_batch_size)
    if not settings.cloudflare_account_id or settings.cloudflare_api_token is None:
        raise EmbeddingNotConfigured(
            "Embeddings are not configured. Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN "
            "(see .env.example), or EMBEDDING_PROVIDER=hash for an offline demo."
        )
    return _cloudflare(
        settings.cloudflare_account_id,
        settings.cloudflare_api_token.get_secret_value(),
        settings.cloudflare_embedding_model,
        settings.cloudflare_pooling,
        settings.embedding_dim,
        settings.embedding_batch_size,
        settings.embedding_query_prefix,
        settings.cloudflare_api_base,
        settings.embedding_timeout_seconds,
    )
