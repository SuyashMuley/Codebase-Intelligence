"""Reading a provider's "try again at" hints, and building the platform's chat from settings."""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import LlmNotConfigured, LlmRateLimited
from app.providers.llm import factory
from app.providers.llm.fallback import FallbackChat
from app.providers.llm.openrouter import OpenRouterClient, retry_after_seconds

NOW = 1_800_000_000.0  # an arbitrary "now", in epoch seconds


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "30"}, 30),
        ({"retry-after": "0.4"}, 1),  # never less than a second
        ({"retry-after": "90000"}, 3600),  # never more than an hour
        ({"x-ratelimit-reset-tokens": "7.66s"}, 8),  # Groq: a duration
        ({"x-ratelimit-reset-requests": "2m59.56s"}, 180),
        ({"x-ratelimit-reset-requests": "1h2m"}, 3600),
        ({"x-ratelimit-reset-requests": "250ms"}, 1),
        (
            {"x-ratelimit-reset": str(int((NOW + 120) * 1000))},
            120,
        ),  # OpenRouter: epoch milliseconds
        ({"x-ratelimit-reset": str(int(NOW + 45))}, 45),  # or epoch seconds
        # Retry-After is the provider's own answer and wins over the reset headers, which describe
        # other limits (Groq: reset-requests is the DAILY limit, reset-tokens the per-minute one).
        ({"retry-after": "10", "x-ratelimit-reset-tokens": "40s"}, 10),
        ({"retry-after": "2", "x-ratelimit-reset-requests": "8m38.4s"}, 2),
        # with no Retry-After, a limit on either blocks the call: the longest
        ({"x-ratelimit-reset-requests": "20s", "x-ratelimit-reset-tokens": "40s"}, 40),
        (
            {"retry-after": "soon", "x-ratelimit-reset-tokens": "7s"},
            7,
        ),  # an unreadable one is skipped
        ({"retry-after": "soon"}, None),
        ({"retry-after": ""}, None),
        ({"x-ratelimit-reset": "5"}, None),  # not an absolute time
        ({"x-ratelimit-reset": "yesterday"}, None),
        ({"x-ratelimit-reset": str(int(NOW - 500))}, 1),  # a reset already in the past
    ],
)
def test_the_wait_a_provider_asks_for(headers: dict[str, str], expected: int | None) -> None:
    assert retry_after_seconds(headers, NOW) == expected


def call(status: int, headers: dict[str, str] | None = None, model: str = "m") -> LlmRateLimited:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers or {}, json={"error": {"message": "x"}})

    client = OpenRouterClient("k" * 12, model, None, transport=httpx.MockTransport(handler))
    with pytest.raises(LlmRateLimited) as exc:
        client.complete("s", "u")
    return exc.value


def test_a_429_carries_the_providers_own_wait() -> None:
    assert call(429, {"retry-after": "42"}).details["retry_after_seconds"] == 42
    assert call(429, {"x-ratelimit-reset-tokens": "1m5s"}).details["retry_after_seconds"] == 65
    assert "retry_after_seconds" not in call(429).details  # no hint: the caller picks a default


def test_used_up_credit_is_not_retried_in_a_minute() -> None:
    assert call(402).details["retry_after_seconds"] == 600


# --- building the platform's chat -------------------------------------------------------------


def settings(**update: Any) -> Settings:
    base: dict[str, Any] = {
        "openrouter_api_key": SecretStr("or-key-0123456789"),
        "groq_api_key": SecretStr("gsk-key-0123456789"),
    }
    return Settings().model_copy(update={**base, **update})


def test_both_providers_make_a_chain_in_the_configured_order() -> None:
    chat = factory.get_llm(settings())
    assert isinstance(chat, FallbackChat)
    assert list(chat.status()) == ["openrouter", "groq"]
    reversed_ = factory.get_llm(settings(llm_provider_order="groq,openrouter"))
    assert isinstance(reversed_, FallbackChat) and list(reversed_.status()) == [
        "groq",
        "openrouter",
    ]


def test_one_shared_chain_serves_every_request_so_it_remembers_what_it_learned() -> None:
    assert factory.get_llm(settings()) is factory.get_llm(settings())


def test_a_provider_without_a_key_is_skipped_and_one_provider_needs_no_chain() -> None:
    only_groq = factory.get_llm(settings(openrouter_api_key=None))
    assert isinstance(only_groq, OpenRouterClient) and only_groq.label == "groq"
    only_or = factory.get_llm(settings(groq_api_key=None))
    assert isinstance(only_or, OpenRouterClient) and only_or.label == "openrouter"


def test_with_no_key_at_all_the_error_names_both_settings() -> None:
    with pytest.raises(LlmNotConfigured) as exc:
        factory.get_llm(settings(openrouter_api_key=None, groq_api_key=None))
    assert "OPENROUTER_API_KEY" in exc.value.message and "GROQ_API_KEY" in exc.value.message


def test_a_provider_left_out_of_the_order_is_not_used_even_with_a_key() -> None:
    chat = factory.get_llm(settings(llm_provider_order="openrouter"))
    assert isinstance(chat, OpenRouterClient) and chat.label == "openrouter"


def test_the_groq_client_uses_groqs_address_and_none_of_openrouters_options() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    s = settings(groq_model="openai/gpt-oss-120b", groq_fallback_model="openai/gpt-oss-20b")
    client = factory._groq(
        "gsk-key-0123456789", s.groq_model, s.groq_fallback_model, s.groq_base_url, 25.0, 28.0
    )
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.complete("s", "u", json_mode=True) == "hi"
    assert sent[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert sent[0]["body"]["model"] == "openai/gpt-oss-120b"
    assert "reasoning" not in sent[0]["body"]  # OpenRouter's option, not sent to Groq
    assert sent[0]["body"]["response_format"] == {"type": "json_object"}
    assert client.fallback_model == "openai/gpt-oss-20b"


def test_a_users_own_chat_client_is_labelled_with_its_provider_for_metrics() -> None:
    import types
    from typing import cast

    from app.db.models import UserProvider
    from app.services.providers.build import build_chat

    for kind in ("groq", "openai", "together", "openrouter"):
        row = cast(UserProvider, types.SimpleNamespace(kind=kind, model=None))
        assert build_chat(row, "k" * 12, Settings()).label == kind


@pytest.mark.parametrize(
    "bad", ["", "openrouter,openrouter", "openrouter,claude", "groq;openrouter"]
)
def test_a_bad_provider_order_stops_startup_with_a_clear_message(bad: str) -> None:
    with pytest.raises(ValueError, match="LLM_PROVIDER_ORDER"):
        Settings(llm_provider_order=bad)


def test_the_order_is_normalised() -> None:
    assert (
        Settings(llm_provider_order=" Groq , OpenRouter ").llm_provider_order == "groq,openrouter"
    )


def test_a_blank_groq_key_in_the_environment_means_not_configured() -> None:
    s = Settings(groq_api_key="", groq_fallback_model="  ")
    assert s.groq_api_key is None and s.groq_fallback_model is None


# --- what Groq's documentation says to send ----------------------------------------------------


def groq_body(effort: str | None) -> dict[str, Any]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    client = factory._groq(
        f"gsk-key-{effort}-0123456789",
        "openai/gpt-oss-120b",
        None,
        "https://api.groq.com/openai/v1",
        25.0,
        28.0,
        effort,
    )
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client.complete("s", "u", json_mode=True, max_tokens=1500)
    return sent[0]


def test_groq_gets_its_own_flat_reasoning_fields_not_openrouters_nested_one() -> None:
    body = groq_body("low")
    assert body["reasoning_effort"] == "low"  # a flat field, as Groq's reasoning page says
    assert body["include_reasoning"] is False  # the thinking is not sent back
    assert "reasoning" not in body
    assert body["response_format"] == {"type": "json_object"}  # JSON mode: "all models" per Groq
    assert body["max_completion_tokens"] == 1500  # Groq deprecated max_tokens for this name
    assert "max_tokens" not in body


def test_with_no_reasoning_effort_neither_groq_field_is_sent() -> None:
    body = groq_body(None)
    assert "reasoning_effort" not in body and "include_reasoning" not in body


def test_openrouter_still_gets_its_nested_reasoning_option() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    client = OpenRouterClient(
        "k" * 12, "m", None, reasoning_effort="low", transport=httpx.MockTransport(handler)
    )
    client.complete("s", "u", max_tokens=700)
    assert sent[0]["reasoning"] == {"effort": "low"} and "reasoning_effort" not in sent[0]
    assert sent[0]["max_tokens"] == 700 and "max_completion_tokens" not in sent[0]


def test_the_defaults_come_from_groqs_published_table() -> None:
    s = Settings()
    assert s.groq_tokens_per_minute == 8000  # gpt-oss-120b, gpt-oss-20b and qwen3.8-27b
    assert s.groq_model == "openai/gpt-oss-120b" and s.groq_reasoning_effort == "low"
    assert Settings(groq_reasoning_effort="").groq_reasoning_effort is None  # blank: send neither


def test_the_chain_limits_groq_to_its_published_tokens_per_minute() -> None:
    chat = factory.get_llm(settings())
    assert isinstance(chat, FallbackChat)
    limits = {m.name: m.max_request_tokens for m in chat._members}
    assert limits == {"openrouter": None, "groq": 8000}
    off = factory.get_llm(settings(groq_tokens_per_minute=0))
    assert isinstance(off, FallbackChat) and {m.max_request_tokens for m in off._members} == {None}


def test_the_default_groq_model_offered_to_users_is_one_groq_lists() -> None:
    from app.services.providers.catalog import CHAT_PROVIDERS

    assert CHAT_PROVIDERS["groq"].default_model == "openai/gpt-oss-120b" == Settings().groq_model
