"""The providers a user may bring, and nothing else.

Users choose a provider by id; they never type a URL. Every address the server will ever call for
a user's key comes from this table, so a hostile "provider URL" has nowhere to go. (The SSRF
guard in `ssrf.py` is a second line of defence over these same addresses.)

Embeddings must have 384 dimensions because the database column is fixed: Cloudflare's
bge-small is 384 natively, and OpenAI's text-embedding-3 models are asked for 384.
"""

import re
from dataclasses import dataclass

EMBEDDING_DIM = 384
SLOTS = ("embedding", "chat")
MODES = ("stored", "browser")

# Model names are passed to the provider as data; keep them boring.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")
ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{32}$")  # a Cloudflare account id


@dataclass(frozen=True)
class ChatProvider:
    id: str
    label: str
    base_url: str
    default_model: str
    # The OpenRouter client sends OpenRouter-only options; others get a plain request.
    openrouter_options: bool = False


@dataclass(frozen=True)
class EmbeddingProvider:
    id: str
    label: str
    base_url: str
    models: tuple[str, ...]  # the first is the default
    needs_account_id: bool = False


# Default model names are suggestions the user can change; providers rename models over time.
CHAT_PROVIDERS: dict[str, ChatProvider] = {
    p.id: p
    for p in (
        ChatProvider(
            "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini", True
        ),
        ChatProvider("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
        ChatProvider(
            "together",
            "Together AI",
            "https://api.together.xyz/v1",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        ),
        # A model on Groq's published rate-limit table when this was written (2026-11-18); Groq
        # changes its models, so the user can type another.
        ChatProvider("groq", "Groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    )
}

EMBEDDING_PROVIDERS: dict[str, EmbeddingProvider] = {
    p.id: p
    for p in (
        EmbeddingProvider(
            "cloudflare",
            "Cloudflare Workers AI",
            "https://api.cloudflare.com/client/v4",
            ("@cf/baai/bge-small-en-v1.5",),
            needs_account_id=True,
        ),
        EmbeddingProvider(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            ("text-embedding-3-small", "text-embedding-3-large"),
        ),
    )
}


def allowed_hosts() -> frozenset[str]:
    """Every host the server may call with a user's key."""
    from urllib.parse import urlsplit

    urls = [p.base_url for p in CHAT_PROVIDERS.values()]
    urls += [p.base_url for p in EMBEDDING_PROVIDERS.values()]
    return frozenset(urlsplit(u).hostname or "" for u in urls)
