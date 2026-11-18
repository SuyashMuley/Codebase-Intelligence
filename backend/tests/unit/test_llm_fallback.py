"""Failing over between chat providers, and leaving a struggling one alone until it recovers."""

from typing import Any

import pytest

from app import metrics
from app.core.errors import (
    LlmAuthFailed,
    LlmBadResponse,
    LlmRateLimited,
    LlmUnavailable,
)
from app.providers.llm.fallback import FallbackChat


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Fake:
    """A chat client that plays a script: an exception is raised, a string is returned. The last
    entry repeats. It records every call, including the time budget it was given."""

    def __init__(
        self, model: str, *script: Any, clock: Clock | None = None, takes: float = 0.0
    ) -> None:
        self.last_model: str | None = None
        self.model = model
        self.script = list(script) or ["ok"]
        self.calls: list[dict[str, Any]] = []
        self.clock, self.takes = clock, takes

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 900,
        json_mode: bool = False,
        temperature: float = 0.2,
        budget_seconds: float | None = None,
    ) -> str:
        self.calls.append(
            {
                "max_tokens": max_tokens,
                "json_mode": json_mode,
                "temperature": temperature,
                "budget_seconds": budget_seconds,
            }
        )
        if self.clock:
            self.clock.t += self.takes
        entry = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(entry, Exception):
            raise entry
        self.last_model = self.model
        return str(entry)


def chain(primary: Fake, secondary: Fake, clock: Clock, budget: float = 28.0) -> FallbackChat:
    return FallbackChat(
        [("openrouter", primary), ("groq", secondary)], budget_seconds=budget, clock=clock
    )


def limited(seconds: int | None = None) -> LlmRateLimited:
    return LlmRateLimited("slow down", details={"retry_after_seconds": seconds} if seconds else {})


def ask(c: FallbackChat) -> str:
    return c.complete("sys", "user", max_tokens=100, json_mode=True, temperature=0.1)


def test_a_healthy_first_provider_is_used_and_the_second_is_never_called() -> None:
    clock = Clock()
    a, b = Fake("m-a", "from a"), Fake("m-b", "from b")
    c = chain(a, b, clock)
    assert ask(c) == "from a" and ask(c) == "from a"
    assert len(a.calls) == 2 and b.calls == []
    assert (c.last_provider, c.last_model) == ("openrouter", "m-a")


def test_the_request_options_reach_the_provider_unchanged() -> None:
    clock = Clock()
    a = Fake("m-a")
    ask(chain(a, Fake("m-b"), clock))
    assert a.calls[0]["max_tokens"] == 100 and a.calls[0]["json_mode"] is True
    assert a.calls[0]["temperature"] == 0.1


def test_a_rate_limited_first_provider_hands_over_and_is_then_left_alone_for_the_hint() -> None:
    clock = Clock()
    a, b = Fake("m-a", limited(300)), Fake("m-b", "from b")
    c = chain(a, b, clock)
    assert ask(c) == "from b"
    assert (c.last_provider, c.last_model) == ("groq", "m-b")
    assert c.status()["openrouter"] == 300.0 and c.status()["groq"] == 0.0

    for _ in range(5):  # every following request goes straight to the second provider
        clock.t += 10
        assert ask(c) == "from b"
    assert len(a.calls) == 1  # the first was not tried again while it was cooling down
    assert len(b.calls) == 6


def test_the_first_provider_is_tried_again_once_its_time_is_up_and_takes_over_when_it_works() -> (
    None
):
    clock = Clock()
    a, b = Fake("m-a", limited(120), "back again"), Fake("m-b", "from b")
    c = chain(a, b, clock)
    assert ask(c) == "from b"
    clock.t += 119
    assert ask(c) == "from b" and len(a.calls) == 1  # one second early: still left alone
    clock.t += 2
    assert ask(c) == "back again" and len(a.calls) == 2  # tried, and answered
    assert c.status() == {"openrouter": 0.0, "groq": 0.0}
    assert ask(c) == "back again"  # and it is the first choice again


def test_a_provider_still_limited_when_retried_is_left_alone_again_and_the_user_is_not_failed() -> (
    None
):
    clock = Clock()
    a, b = Fake("m-a", limited(60)), Fake("m-b", "from b")
    c = chain(a, b, clock)
    ask(c)
    clock.t += 61
    assert ask(c) == "from b"  # the retry of a failed, b answered in the same request
    assert len(a.calls) == 2 and c.status()["openrouter"] == 60.0


def test_without_a_hint_a_rate_limit_costs_a_minute() -> None:
    clock = Clock()
    c = chain(Fake("m-a", limited()), Fake("m-b"), clock)
    ask(c)
    assert c.status()["openrouter"] == 60.0  # a literal: a change to the default must be deliberate


def test_server_errors_and_timeouts_are_left_alone_briefly() -> None:
    clock = Clock()
    c = chain(Fake("m-a", LlmUnavailable("down")), Fake("m-b"), clock)
    ask(c)
    assert c.status()["openrouter"] == 30.0


def test_a_rejected_key_or_used_up_credit_is_left_alone_for_a_long_time() -> None:
    clock = Clock()
    c = chain(Fake("m-a", LlmAuthFailed("bad key")), Fake("m-b", "from b"), clock)
    assert ask(c) == "from b"
    assert c.status()["openrouter"] >= 300.0  # a bad key is not retried every minute


def test_an_unreadable_answer_moves_on_but_is_not_held_against_the_provider() -> None:
    clock = Clock()
    a, b = Fake("m-a", LlmBadResponse("empty"), "fine"), Fake("m-b", "from b")
    c = chain(a, b, clock)
    assert ask(c) == "from b"  # this prompt got an empty reply from a: try b
    assert c.status() == {"openrouter": 0.0, "groq": 0.0}  # a's health is untouched
    assert ask(c) == "fine"  # and a is simply tried first next time


def test_when_every_provider_fails_the_last_error_surfaces() -> None:
    clock = Clock()
    c = chain(Fake("m-a", LlmUnavailable("a down")), Fake("m-b", LlmUnavailable("b down")), clock)
    with pytest.raises(LlmUnavailable) as exc:
        ask(c)
    assert "b down" in exc.value.message


def test_when_everything_is_rate_limited_the_error_says_when_the_soonest_is_back() -> None:
    clock = Clock()
    c = chain(Fake("m-a", limited(900)), Fake("m-b", limited(120)), clock)
    with pytest.raises(LlmRateLimited) as exc:
        ask(c)
    assert exc.value.details["retry_after_seconds"] == 120 and exc.value.retryable


def test_when_both_are_being_left_alone_the_one_due_back_soonest_is_tried_first_not_nobody() -> (
    None
):
    clock = Clock()
    a, b = Fake("m-a", limited(900), "a is back"), Fake("m-b", limited(100), "b is back")
    c = chain(a, b, clock)
    with pytest.raises(LlmRateLimited):
        ask(c)
    clock.t += 5  # both still cooling down: b is due back first
    assert ask(c) == "b is back" and len(a.calls) == 1 and len(b.calls) == 2
    assert (
        c.status()["groq"] == 0.0
    )  # it answered, so it is forgiven at once, not when its time was up


def test_the_second_provider_gets_only_the_time_that_is_left() -> None:
    clock = Clock()
    a = Fake("m-a", LlmUnavailable("slow"), clock=clock, takes=20.0)
    b = Fake("m-b", "from b", clock=clock, takes=1.0)
    c = chain(a, b, clock, budget=28.0)
    assert ask(c) == "from b"
    assert a.calls[0]["budget_seconds"] == 28.0
    assert b.calls[0]["budget_seconds"] == 8.0  # 28 minus the 20 a used


def test_no_second_attempt_is_started_with_too_little_time() -> None:
    clock = Clock()
    a = Fake("m-a", LlmUnavailable("slow"), clock=clock, takes=26.0)
    b = Fake("m-b", "from b")
    with pytest.raises(LlmUnavailable):
        ask(chain(a, b, clock, budget=28.0))
    assert b.calls == []  # 2 seconds left is not enough to be worth trying


def test_the_callers_own_budget_overrides_the_default() -> None:
    clock = Clock()
    a = Fake("m-a")
    chain(a, Fake("m-b"), clock).complete("s", "u", budget_seconds=9.0)
    assert a.calls[0]["budget_seconds"] == 9.0


def test_failovers_are_counted_by_provider_and_reason() -> None:
    def n(reason: str) -> float:
        return (
            metrics.REGISTRY.get_sample_value(
                "cip_provider_failovers_total",
                {"from_provider": "openrouter", "to_provider": "groq", "reason": reason},
            )
            or 0.0
        )

    before = {r: n(r) for r in ("rate_limited", "unavailable", "auth_failed", "bad_response")}
    for err in (limited(60), LlmUnavailable("x"), LlmAuthFailed("x"), LlmBadResponse("x")):
        clock = Clock()
        ask(chain(Fake("m-a", err), Fake("m-b"), clock))
    assert {r: n(r) - before[r] for r in before} == {
        "rate_limited": 1.0,
        "unavailable": 1.0,
        "auth_failed": 1.0,
        "bad_response": 1.0,
    }


def test_the_last_provider_failing_is_not_counted_as_a_failover() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "cip_provider_failovers_total",
        {"from_provider": "groq", "to_provider": "none", "reason": "unavailable"},
    )
    clock = Clock()
    with pytest.raises(LlmUnavailable):
        ask(chain(Fake("m-a", LlmUnavailable("x")), Fake("m-b", LlmUnavailable("y")), clock))
    assert before is None  # there was nowhere to fail over to from the last provider


def test_a_chain_needs_two_providers() -> None:
    with pytest.raises(ValueError):
        FallbackChat([("openrouter", Fake("m"))])


# --- request size against a provider's tokens-per-minute limit ----------------------------------


def sized(primary: Fake, secondary: Fake, clock: Clock, **limits: int) -> FallbackChat:
    return FallbackChat(
        [("openrouter", primary), ("groq", secondary)], clock=clock, max_request_tokens=limits
    )


BIG = "x" * 30_000  # about 10,000 tokens at three characters each


def test_the_size_estimate_counts_the_prompt_and_the_whole_reply_allowance() -> None:
    from app.providers.llm.fallback import estimate_tokens

    assert estimate_tokens("a" * 300, "b" * 300, 100) == 300  # 600 characters / 3, plus 100
    assert estimate_tokens("", "", 1500) == 1500


def test_a_request_too_big_for_the_second_provider_is_never_sent_there_as_a_fallback() -> None:
    clock = Clock()
    a, b = Fake("m-a", limited(300)), Fake("m-b", "from b")
    c = sized(a, b, clock, groq=8000)
    with pytest.raises(LlmRateLimited):  # a is limited and b cannot take it: the honest answer
        c.complete("sys", BIG, max_tokens=1500)
    assert b.calls == []


def test_a_request_too_big_for_the_first_goes_to_the_second_at_no_cost_to_the_first() -> None:
    clock = Clock()
    a, b = Fake("m-a", "from a"), Fake("m-b", "from b")
    c = sized(a, b, clock, openrouter=500)
    assert c.complete("sys", BIG, max_tokens=100) == "from b"
    assert a.calls == [] and c.status() == {"openrouter": 0.0, "groq": 0.0}  # not held against it


def test_a_rate_limited_provider_that_can_take_the_request_is_tried_even_if_the_other_cannot() -> (
    None
):
    clock = Clock()
    a, b = Fake("m-a", limited(300), "a answers"), Fake("m-b", "from b")
    c = sized(a, b, clock, groq=8000)
    c.complete("sys", "small", max_tokens=100)  # a is limited, b takes it
    assert c.status()["openrouter"] == 300.0
    # a big request: only a fits, and a is cooling down: it is still tried, not refused
    assert c.complete("sys", BIG, max_tokens=100) == "a answers"
    assert b.calls and len(b.calls) == 1


def test_a_request_too_big_for_every_provider_is_refused_outright_with_a_clear_error() -> None:
    from app.core.errors import LlmRequestTooLarge

    clock = Clock()
    a, b = Fake("m-a"), Fake("m-b")
    c = sized(a, b, clock, openrouter=500, groq=800)
    with pytest.raises(LlmRequestTooLarge) as exc:
        c.complete("sys", BIG, max_tokens=100)
    assert exc.value.status_code == 413 and exc.value.code == "llm_request_too_large"
    assert not exc.value.retryable and "more specific" in exc.value.message
    assert a.calls == [] and b.calls == []


def test_the_limit_itself_is_allowed_and_one_token_over_is_not() -> None:
    clock = Clock()
    prompt = "x" * 2997  # 999 tokens at three characters each
    ok = sized(Fake("m-a"), Fake("m-b", "from b"), clock, openrouter=1099)  # 999 + 100 = 1099
    assert ok.complete("", prompt, max_tokens=100) == "ok"
    over = sized(Fake("m-a"), Fake("m-b", "from b"), clock, openrouter=1098)
    assert over.complete("", prompt, max_tokens=100) == "from b"


def test_no_limit_means_no_check() -> None:
    clock = Clock()
    a = Fake("m-a")
    assert chain(a, Fake("m-b"), clock).complete("sys", BIG, max_tokens=100) == "ok"
