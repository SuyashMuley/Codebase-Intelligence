import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.errors import LlmAuthFailed, LlmBadResponse, LlmRateLimited, LlmUnavailable
from app.providers.llm.openrouter import OpenRouterClient

KEY = "sk-or-v1-secret-key-value-123"


def ok(text: str = "hello", model_usage: bool = True) -> httpx.Response:
    body: dict[str, object] = {"choices": [{"message": {"content": text}}]}
    if model_usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7}
    return httpx.Response(200, json=body)


def client(
    handler: Callable[[httpx.Request, int], httpx.Response], fallback: str | None = "fallback/model"
) -> tuple[OpenRouterClient, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        return handler(request, len(seen))

    return (
        OpenRouterClient(KEY, "main/model", fallback, transport=httpx.MockTransport(wrapped)),
        seen,
    )


def test_a_request_carries_the_key_the_prompt_and_the_json_mode_flag() -> None:
    c, seen = client(lambda r, n: ok('{"a": 1}'))
    assert c.complete("be brief", "question", max_tokens=50, json_mode=True) == '{"a": 1}'
    sent = seen[0]
    assert sent["headers"]["authorization"] == f"Bearer {KEY}"
    assert sent["body"]["model"] == "main/model"
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "question"},
    ]
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert sent["body"]["max_tokens"] == 50
    assert c.last_model == "main/model"
    assert c.usage == {"requests": 1, "prompt_tokens": 11, "completion_tokens": 7}


def test_json_mode_is_off_unless_asked_for() -> None:
    c, seen = client(lambda r, n: ok())
    c.complete("s", "u")
    assert "response_format" not in seen[0]["body"]


def test_an_unavailable_model_falls_back_once() -> None:
    c, seen = client(lambda r, n: httpx.Response(503) if n == 1 else ok("from fallback"))
    assert c.complete("s", "u") == "from fallback"
    assert [s["body"]["model"] for s in seen] == ["main/model", "fallback/model"]
    assert c.last_model == "fallback/model"


def test_a_model_rate_limited_upstream_falls_back_too() -> None:
    c, _ = client(lambda r, n: httpx.Response(429) if n == 1 else ok("later"))
    assert c.complete("s", "u") == "later"


def test_when_every_model_is_rate_limited_the_error_says_so() -> None:
    c, seen = client(lambda r, n: httpx.Response(429))
    with pytest.raises(LlmRateLimited) as exc:
        c.complete("s", "u")
    assert len(seen) == 2 and exc.value.retryable


def test_a_bad_key_stops_immediately_without_trying_another_model() -> None:
    c, seen = client(lambda r, n: httpx.Response(401))
    with pytest.raises(LlmAuthFailed) as exc:
        c.complete("s", "u")
    assert len(seen) == 1
    assert KEY not in str(exc.value)


def test_exhausted_credit_is_not_retried_on_another_model() -> None:
    c, seen = client(lambda r, n: httpx.Response(402))
    with pytest.raises(LlmRateLimited):
        c.complete("s", "u")
    assert len(seen) == 1


def test_network_failure_is_reported_as_unavailable_after_trying_the_fallback() -> None:
    def boom(request: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ConnectError("down")

    c, seen = client(boom)
    with pytest.raises(LlmUnavailable) as exc:
        c.complete("s", "u")
    assert len(seen) == 2 and exc.value.retryable
    assert KEY not in str(exc.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
        httpx.Response(418),
    ],
)
def test_unusable_answers_are_bad_responses(response: httpx.Response) -> None:
    c, _ = client(lambda r, n: response, fallback=None)
    with pytest.raises((LlmBadResponse, LlmUnavailable)):
        c.complete("s", "u")


def test_the_fallback_is_ignored_when_it_is_the_same_model() -> None:
    c, seen = client(lambda r, n: httpx.Response(503), fallback="main/model")
    with pytest.raises(LlmUnavailable):
        c.complete("s", "u")
    assert len(seen) == 1


def test_token_usage_accumulates_and_tolerates_missing_usage() -> None:
    c, _ = client(lambda r, n: ok(model_usage=n == 1))
    c.complete("s", "u")
    c.complete("s", "u")
    assert c.usage == {"requests": 2, "prompt_tokens": 11, "completion_tokens": 7}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def budgeted(
    handler: Callable[[httpx.Request, int], httpx.Response], clock: Clock, budget: float
) -> tuple[OpenRouterClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    c = OpenRouterClient(
        KEY,
        "main/model",
        "fallback/model",
        timeout=25.0,
        budget_seconds=budget,
        transport=httpx.MockTransport(wrapped),
        clock=clock,
    )
    return c, seen


def test_each_attempt_is_given_only_the_time_that_is_left() -> None:
    clock = Clock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        clock.now += 20.0  # the first attempt burns 20 of the 28 seconds, then fails
        return httpx.Response(503) if n == 1 else ok("late but in time")

    c, seen = budgeted(handler, clock, budget=28.0)
    assert c.complete("s", "u") == "late but in time"
    assert len(seen) == 2
    assert seen[0].extensions["timeout"]["read"] == 25.0  # capped by the per-attempt timeout
    assert seen[1].extensions["timeout"]["read"] == pytest.approx(8.0)  # 28 - 20 left


def test_no_fallback_is_started_when_almost_no_time_is_left() -> None:
    clock = Clock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        clock.now += 26.5
        return httpx.Response(503)

    c, seen = budgeted(handler, clock, budget=28.0)
    with pytest.raises(LlmUnavailable) as exc:
        c.complete("s", "u")
    assert len(seen) == 1 and exc.value.retryable


def test_a_timeout_is_reported_as_taking_too_long_and_is_retryable() -> None:
    def slow(request: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    c, _ = budgeted(slow, Clock(), budget=28.0)
    with pytest.raises(LlmUnavailable) as exc:
        c.complete("s", "u")
    assert "too long" in exc.value.message and exc.value.retryable
