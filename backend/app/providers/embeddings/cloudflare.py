"""Cloudflare Workers AI embeddings (default model: bge-small-en-v1.5, 384 dimensions)."""

import time
from typing import Any

import httpx

from app.core.errors import (
    EmbeddingAuthFailed,
    EmbeddingBadResponse,
    EmbeddingQuotaExceeded,
    EmbeddingUnavailable,
)
from app.metrics import observe_provider
from app.providers.embeddings.base import l2_normalize

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_IN_REQUEST_RETRIES = 2
_MAX_RETRY_WAIT = 10.0


def cloudflare_model_id(model: str, pooling: str) -> str:
    return f"cloudflare:{model}:{pooling}"


class CloudflareEmbedder:
    def __init__(
        self,
        account_id: str,
        api_token: str,
        model: str = "@cf/baai/bge-small-en-v1.5",
        pooling: str = "cls",
        dim: int = 384,
        batch_size: int = 32,
        query_prefix: str = "",
        api_base: str = "https://api.cloudflare.com/client/v4",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
        credentials_hint: str = "Check CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID.",
    ) -> None:
        self._credentials_hint = credentials_hint
        self.dim = dim
        self.batch_size = batch_size
        self.model_id = cloudflare_model_id(model, pooling)
        self.usage: dict[str, float] = {"requests": 0.0, "neurons": 0.0}
        self._pooling = pooling
        self._query_prefix = query_prefix
        self._url = f"{api_base.rstrip('/')}/accounts/{account_id}/ai/run/{model}"
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_token}"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    # --- one request ----------------------------------------------------------------------

    def _post(self, texts: list[str]) -> httpx.Response:
        body = {"text": texts, "pooling": self._pooling}
        for attempt in range(_MAX_IN_REQUEST_RETRIES + 1):
            try:
                resp = self._http.post(self._url, json=body)
            except httpx.HTTPError:
                if attempt == _MAX_IN_REQUEST_RETRIES:
                    raise EmbeddingUnavailable("Could not reach the embedding service.") from None
                self._sleep(0.5 * 2**attempt)
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_IN_REQUEST_RETRIES:
                wait = _retry_after(resp) or 0.5 * 2**attempt
                self._sleep(min(wait, _MAX_RETRY_WAIT))
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        with observe_provider("cloudflare", "embed"):
            return self._embed_batch_unobserved(texts)

    def _embed_batch_unobserved(self, texts: list[str]) -> list[list[float]]:
        resp = self._post(texts)
        status = resp.status_code
        if status in (401, 403):
            raise EmbeddingAuthFailed(
                f"The embedding service rejected the credentials. {self._credentials_hint}"
            )
        if status == 402 or (status == 429 and _looks_like_quota(resp)):
            raise EmbeddingQuotaExceeded(
                "The daily free embedding allowance is used up. Try again later."
            )
        if status == 429 or status >= 500:
            raise EmbeddingUnavailable(f"The embedding service is busy (HTTP {status}).")
        if status != 200:
            raise EmbeddingBadResponse(f"The embedding service returned HTTP {status}.")

        try:
            payload = resp.json()
            data = payload["result"]["data"]
        except (ValueError, KeyError, TypeError):
            raise EmbeddingBadResponse(
                "The embedding service returned an unreadable answer."
            ) from None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingBadResponse(
                "The embedding service returned the wrong number of vectors."
            )
        vectors: list[list[float]] = []
        for row in data:
            if not isinstance(row, list) or len(row) != self.dim:
                raise EmbeddingBadResponse(
                    f"Expected {self.dim}-dimensional vectors from the embedding model."
                )
            vectors.append(l2_normalize([float(x) for x in row]))

        self.usage["requests"] += 1
        try:
            self.usage["neurons"] += float(resp.headers.get("cf-ai-neurons", 0))
        except ValueError:
            pass
        return vectors

    # --- Embedder interface ------------------------------------------------------------------

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors += self._embed_batch(texts[start : start + self.batch_size])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([self._query_prefix + text])[0]


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _looks_like_quota(resp: httpx.Response) -> bool:
    try:
        text = str(resp.json().get("errors", "")).lower()
    except (ValueError, AttributeError):
        return False
    return any(w in text for w in ("quota", "neuron", "daily", "limit exceeded"))
