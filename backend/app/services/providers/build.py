"""Turn a saved provider setting plus a key into a client. Addresses come only from the catalogue
and pass the SSRF guard first."""

from app.core.config import Settings
from app.db.models import UserProvider
from app.providers.embeddings.base import Embedder
from app.providers.embeddings.cloudflare import CloudflareEmbedder
from app.providers.embeddings.openai import OpenAIEmbedder
from app.providers.llm.openrouter import OpenRouterClient
from app.services.providers import ssrf
from app.services.providers.catalog import CHAT_PROVIDERS, EMBEDDING_DIM, EMBEDDING_PROVIDERS


def build_embedder(row: UserProvider, key: str, s: Settings) -> Embedder:
    spec = EMBEDDING_PROVIDERS[row.kind]
    ssrf.check_url(spec.base_url)
    model = row.model or spec.models[0]
    if row.kind == "cloudflare":
        assert row.account_id is not None
        return CloudflareEmbedder(
            row.account_id,
            key,
            model,
            s.cloudflare_pooling,
            EMBEDDING_DIM,
            s.embedding_batch_size,
            s.embedding_query_prefix,
            spec.base_url,
            s.embedding_timeout_seconds,
            credentials_hint="Check your Cloudflare token and account id in provider settings.",
        )
    return OpenAIEmbedder(
        key, model, EMBEDDING_DIM, api_base=spec.base_url, timeout=s.embedding_timeout_seconds
    )


def build_chat(row: UserProvider, key: str, s: Settings) -> OpenRouterClient:
    spec = CHAT_PROVIDERS[row.kind]
    ssrf.check_url(spec.base_url)
    return OpenRouterClient(
        key,
        row.model or spec.default_model,
        None,
        spec.base_url,
        s.llm_timeout_seconds,
        s.llm_budget_seconds,
        label=spec.id,
    )
