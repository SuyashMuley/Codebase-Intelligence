"""OpenRouter chat completions (OpenAI-compatible), used for AI suggestions and Q&A.

One request in, one text out. The caller owns the prompt and the validation of the answer: the
model is treated as an untrusted source whatever it says.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.core.errors import (
    LlmAuthFailed,
    LlmBadResponse,
    LlmRateLimited,
    LlmUnavailable,
)

_FALLBACK_STATUSES = {400, 404, 408, 409, 500, 502, 503, 504}
_MAX_RESPONSE_BYTES = 1_000_000
_MIN_ATTEMPT_SECONDS = 3.0  # not worth starting another model attempt with less time than this


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_model: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 60.0,
        budget_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model
        self.fallback_model = fallback_model if fallback_model != model else None
        self.usage: dict[str, int] = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.last_model: str | None = None  # which model actually answered
        # The whole call, fallback included, must finish inside this: the browser reaches the API
        # through a proxy that gives up after about 30 seconds, and a clean, retryable error
        # beats a connection that just hangs up.
        self._budget = budget_seconds
        self._timeout = timeout
        self._clock = clock
        self._reasoning_effort = reasoning_effort
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
            follow_redirects=False,
        )

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 900,
        json_mode: bool = False,
        temperature: float = 0.2,
        budget_seconds: float | None = None,
    ) -> str:
        """The model's text answer. Falls back once to the fallback model when the main one is
        unavailable; rate limits and bad credentials are reported as they are."""
        models = [self.model] + ([self.fallback_model] if self.fallback_model else [])
        last_error: LlmUnavailable | LlmBadResponse | LlmRateLimited | None = None
        started = self._clock()
        # A caller that has already spent part of a larger time limit passes what is left.
        budget = self._budget if budget_seconds is None else budget_seconds
        for model in models:
            remaining = (
                self._timeout
                if budget is None
                else min(self._timeout, budget - (self._clock() - started))
            )
            if remaining < _MIN_ATTEMPT_SECONDS:
                if last_error is None:  # no time at all, e.g. a tiny budget
                    last_error = LlmUnavailable("The language model took too long. Try again.")
                break
            try:
                text = self._request(
                    model, system, user, max_tokens, json_mode, temperature, remaining
                )
            except (LlmUnavailable, LlmBadResponse, LlmRateLimited) as exc:
                # A free model is often rate limited upstream while another still answers.
                if isinstance(exc, LlmRateLimited) and not exc.details.get("model_specific"):
                    raise
                last_error = exc
                continue
            self.last_model = model
            return text
        assert last_error is not None
        raise last_error

    def _request(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        json_mode: bool,
        temperature: float,
        timeout: float,
    ) -> str:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self._reasoning_effort:
            # Reasoning models spend tokens thinking before they answer; on a free tier that
            # means slow replies and sometimes an empty one when the token limit runs out.
            body["reasoning"] = {"effort": self._reasoning_effort}
        deadline = self._clock() + timeout
        try:
            # Streamed so that the deadline is a wall-clock limit. A plain read timeout only
            # fires when no bytes arrive, and a busy upstream can keep the connection alive
            # with filler for far longer than we are willing to wait.
            with self._http.stream(
                "POST",
                self._url,
                json=body,
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            ) as resp:
                status = resp.status_code
                parts: list[bytes] = []
                for chunk in resp.iter_bytes():
                    if self._clock() > deadline:
                        raise LlmUnavailable("The language model took too long. Try again.")
                    parts.append(chunk)
                    if sum(len(p) for p in parts) > _MAX_RESPONSE_BYTES:
                        raise LlmBadResponse("The language model's answer was too large.")
        except httpx.TimeoutException:
            raise LlmUnavailable("The language model took too long. Try again.") from None
        except httpx.HTTPError:
            raise LlmUnavailable("Could not reach the language model service.") from None
        if self._clock() > deadline:
            raise LlmUnavailable("The language model took too long. Try again.")

        if status in (401, 403):
            raise LlmAuthFailed("The language model service rejected the API key.")
        if status == 402:
            raise LlmRateLimited("The language model allowance is used up. Try again later.")
        if status == 429:
            raise LlmRateLimited(
                "The language model service is rate limiting requests.",
                details={"model_specific": True},
            )
        if status in _FALLBACK_STATUSES:
            raise LlmUnavailable(f"The language model is unavailable (HTTP {status}).")
        if status != 200:
            raise LlmBadResponse(f"The language model service returned HTTP {status}.")
        try:
            payload = json.loads(b"".join(parts))
            text = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmBadResponse("The language model returned an unreadable answer.") from None
        if not isinstance(text, str) or not text.strip():
            raise LlmBadResponse("The language model returned an empty answer.")
        usage = payload.get("usage") or {}
        self.usage["requests"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                self.usage[key] += value
        return text
