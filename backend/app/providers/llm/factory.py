from functools import lru_cache

from app.core.config import Settings
from app.core.errors import LlmNotConfigured
from app.providers.llm.fallback import Chat, FallbackChat
from app.providers.llm.openrouter import OpenRouterClient


@lru_cache
def _openrouter(
    key: str,
    model: str,
    fallback: str | None,
    base: str,
    timeout: float,
    budget: float,
    reasoning: str | None,
) -> OpenRouterClient:
    return OpenRouterClient(key, model, fallback, base, timeout, budget, reasoning_effort=reasoning)


@lru_cache
def _groq(
    key: str,
    model: str,
    fallback: str | None,
    base: str,
    timeout: float,
    budget: float,
    reasoning: str | None = None,
) -> OpenRouterClient:
    # Same OpenAI-compatible protocol. OpenRouter's nested `reasoning` option is not sent: it is
    # OpenRouter's. Groq takes a flat `reasoning_effort` and `include_reasoning` (its docs).
    extra = {"reasoning_effort": reasoning, "include_reasoning": False} if reasoning else None
    return OpenRouterClient(
        key,
        model,
        fallback,
        base,
        timeout,
        budget,
        label="groq",
        extra_body=extra,
        max_tokens_field="max_completion_tokens",  # `max_tokens` is deprecated on Groq
    )


@lru_cache
def _chain(
    clients: tuple[OpenRouterClient, ...],
    names: tuple[str, ...],
    budget: float,
    groq_tokens_per_minute: int,
) -> FallbackChat:
    # One shared chain, so what it learns about a provider's limits outlasts a single request.
    limits = {"groq": groq_tokens_per_minute} if groq_tokens_per_minute else {}
    return FallbackChat(
        list(zip(names, clients, strict=True)), budget_seconds=budget, max_request_tokens=limits
    )


def get_llm(settings: Settings) -> Chat:
    """The platform's chat model: OpenRouter, Groq, or both with failover, in the configured
    order. Raises LlmNotConfigured with the fix, never a vague failure."""
    members: list[tuple[str, OpenRouterClient]] = []
    for name in settings.llm_provider_order.split(","):
        if name == "openrouter" and settings.openrouter_api_key is not None:
            members.append(
                (
                    "openrouter",
                    _openrouter(
                        settings.openrouter_api_key.get_secret_value(),
                        settings.openrouter_model,
                        settings.openrouter_fallback_model,
                        settings.openrouter_base_url,
                        settings.llm_timeout_seconds,
                        settings.llm_budget_seconds,
                        settings.openrouter_reasoning_effort,
                    ),
                )
            )
        elif name == "groq" and settings.groq_api_key is not None:
            members.append(
                (
                    "groq",
                    _groq(
                        settings.groq_api_key.get_secret_value(),
                        settings.groq_model,
                        settings.groq_fallback_model,
                        settings.groq_base_url,
                        settings.llm_timeout_seconds,
                        settings.llm_budget_seconds,
                        settings.groq_reasoning_effort,
                    ),
                )
            )
    if not members:
        raise LlmNotConfigured(
            "The language model is not configured. Set OPENROUTER_API_KEY or GROQ_API_KEY "
            "(see .env.example)."
        )
    if len(members) == 1:
        return members[0][1]
    return _chain(
        tuple(c for _, c in members),
        tuple(n for n, _ in members),
        settings.llm_budget_seconds,
        settings.groq_tokens_per_minute,
    )
