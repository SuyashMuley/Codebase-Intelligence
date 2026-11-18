"""Several chat providers behind one: use the first that works, and leave a struggling one alone
until it should have recovered.

The platform's free chat models are rate limited and sometimes down. With more than one provider
configured (OpenRouter, then Groq by default), a request that the first cannot serve goes to the
next within the same time budget, and the first is then skipped for as long as it asked (its own
reset hint) or a sensible default, instead of being tried and failing for every request.

This is only for the platform's own chat, used for public repositories. A private repository's
code is only ever sent to its owner's own provider; it never reaches this chain.
"""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.errors import (
    AppError,
    LlmAuthFailed,
    LlmBadResponse,
    LlmRateLimited,
    LlmRequestTooLarge,
    LlmUnavailable,
)
from app.metrics import PROVIDER_FAILOVERS


class Chat(Protocol):
    last_model: str | None

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = ...,
        json_mode: bool = ...,
        temperature: float = ...,
        budget_seconds: float | None = ...,
    ) -> str: ...


# How long a provider is left alone after each kind of failure, when it gave no hint of its own.
RATE_LIMITED_SECONDS = 60.0
UNAVAILABLE_SECONDS = 30.0
AUTH_FAILED_SECONDS = 600.0  # a bad or exhausted key needs a person, not a retry every minute
MIN_ATTEMPT_SECONDS = 3.0  # not worth starting another provider with less time than this


# Characters per token used to estimate a request's size. Code averages somewhat more than three
# characters a token; three over-counts a little, which is the safe side for a limit.
CHARS_PER_TOKEN = 3.0


def estimate_tokens(system: str, user: str, max_tokens: int) -> int:
    """A cautious size for a request against a tokens-per-minute limit: the prompt as estimated,
    plus the whole reply allowance (providers differ on whether they count what you only allow)."""
    return int((len(system) + len(user)) / CHARS_PER_TOKEN) + max_tokens


@dataclass
class _Member:
    name: str
    client: Chat
    skip_until: float = 0.0  # monotonic time before which this provider is not tried
    max_request_tokens: int | None = None  # a request estimated above this can never succeed here


class FallbackChat:
    def __init__(
        self,
        members: list[tuple[str, Chat]],
        budget_seconds: float = 28.0,
        clock: Callable[[], float] = time.monotonic,
        max_request_tokens: Mapping[str, int] | None = None,
    ) -> None:
        if len(members) < 2:
            raise ValueError("a fallback chain needs at least two providers")
        limits = max_request_tokens or {}
        self._members = [
            _Member(name, client, max_request_tokens=limits.get(name)) for name, client in members
        ]
        self._budget = budget_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self.last_model: str | None = None
        self.last_provider: str | None = None

    # --- state -------------------------------------------------------------------------------

    def status(self) -> dict[str, float]:
        """Seconds each provider will still be left alone (0 means it is being tried)."""
        now = self._clock()
        with self._lock:
            return {m.name: max(0.0, round(m.skip_until - now, 1)) for m in self._members}

    def _order(self, candidates: list[_Member]) -> list[_Member]:
        """Of the providers that can take this request, those worth trying now, in preference
        order; if every one is being left alone, all of them, soonest-to-recover first (a hint
        can be pessimistic, and failing at once is worse than trying)."""
        now = self._clock()
        with self._lock:
            ready = [m for m in candidates if m.skip_until <= now]
            if ready:
                return ready
            return sorted(candidates, key=lambda m: m.skip_until)

    def _leave_alone(self, member: _Member, seconds: float) -> None:
        with self._lock:
            member.skip_until = max(member.skip_until, self._clock() + seconds)

    def _recovered(self, member: _Member) -> None:
        with self._lock:
            member.skip_until = 0.0

    # --- the call ----------------------------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 900,
        json_mode: bool = False,
        temperature: float = 0.2,
        budget_seconds: float | None = None,
    ) -> str:
        started = self._clock()
        total = self._budget if budget_seconds is None else budget_seconds
        errors: list[AppError] = []
        # A request that cannot fit a provider's limit can never succeed there: it is not a
        # failure and has nothing to wait out, so that provider is not a candidate at all (and a
        # rate-limited provider that can take it is not ruled out by one that cannot).
        size = estimate_tokens(system, user, max_tokens)
        fitting = [
            m for m in self._members if m.max_request_tokens is None or size <= m.max_request_tokens
        ]
        if not fitting:
            raise LlmRequestTooLarge(
                "This question and the code it needs are too large for the language models "
                "available. Try a more specific question."
            )
        order = self._order(fitting)
        for index, member in enumerate(order):
            remaining = total - (self._clock() - started)
            if remaining < MIN_ATTEMPT_SECONDS:
                if not errors:
                    errors.append(LlmUnavailable("The language model took too long. Try again."))
                break
            try:
                text = member.client.complete(
                    system,
                    user,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                    temperature=temperature,
                    budget_seconds=remaining,
                )
            except LlmBadResponse as exc:
                # An empty or unreadable answer is about this prompt, not the provider's health.
                errors.append(exc)
                self._note_failover(order, index, "bad_response")
                continue
            except LlmRateLimited as exc:
                hint = exc.details.get("retry_after_seconds")
                self._leave_alone(member, float(hint) if hint else RATE_LIMITED_SECONDS)
                errors.append(exc)
                self._note_failover(order, index, "rate_limited")
                continue
            except LlmUnavailable as exc:
                self._leave_alone(member, UNAVAILABLE_SECONDS)
                errors.append(exc)
                self._note_failover(order, index, "unavailable")
                continue
            except LlmAuthFailed as exc:
                self._leave_alone(member, AUTH_FAILED_SECONDS)
                errors.append(exc)
                self._note_failover(order, index, "auth_failed")
                continue
            self._recovered(member)
            self.last_model = member.client.last_model
            self.last_provider = member.name
            return text
        raise self._final_error(errors)

    def _note_failover(self, order: list[_Member], index: int, reason: str) -> None:
        if index + 1 < len(order):
            PROVIDER_FAILOVERS.labels(order[index].name, order[index + 1].name, reason).inc()

    def _final_error(self, errors: list[AppError]) -> AppError:
        """What the caller sees when nothing answered: the most informative of what happened.
        If everything was rate limited, say when the soonest provider should be back."""
        limited = [e for e in errors if isinstance(e, LlmRateLimited)]
        if limited and len(limited) == len(errors):
            waits = [e.details.get("retry_after_seconds") for e in limited]
            known = [float(w) for w in waits if w]
            err = LlmRateLimited(limited[-1].message)
            if known:
                err.details = {"retry_after_seconds": int(min(known))}
            return err
        return errors[-1]
