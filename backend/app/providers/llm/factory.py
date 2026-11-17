from functools import lru_cache

from app.core.config import Settings
from app.core.errors import LlmNotConfigured
from app.providers.llm.openrouter import OpenRouterClient


@lru_cache
def _openrouter(
    key: str, model: str, fallback: str | None, base: str, timeout: float, budget: float
) -> OpenRouterClient:
    return OpenRouterClient(key, model, fallback, base, timeout, budget)


def get_llm(settings: Settings) -> OpenRouterClient:
    """The configured chat model. Raises LlmNotConfigured with the fix, never a vague failure."""
    if settings.openrouter_api_key is None:
        raise LlmNotConfigured(
            "The language model is not configured. Set OPENROUTER_API_KEY (see .env.example)."
        )
    return _openrouter(
        settings.openrouter_api_key.get_secret_value(),
        settings.openrouter_model,
        settings.openrouter_fallback_model,
        settings.openrouter_base_url,
        settings.llm_timeout_seconds,
        settings.llm_budget_seconds,
    )
