"""OpenAI embeddings (`/embeddings`), asked for 384 dimensions so they fit the database column.

Used only with a key the user supplied. `text-embedding-3-*` accept a `dimensions` parameter that
shortens the vector; the result is normalized again here, so cosine distance behaves.
"""

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


def openai_model_id(model: str, dim: int) -> str:
    return f"openai:{model}:{dim}"


class OpenAIEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dim: int = 384,
        batch_size: int = 64,
        api_base: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.dim = dim
        self.batch_size = batch_size
        self.model_id = openai_model_id(model, dim)
        self.usage: dict[str, float] = {"requests": 0.0, "tokens": 0.0}
        self._model = model
        self._url = f"{api_base.rstrip('/')}/embeddings"
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def _post(self, texts: list[str]) -> httpx.Response:
        body = {"model": self._model, "input": texts, "dimensions": self.dim}
        for attempt in range(_MAX_IN_REQUEST_RETRIES + 1):
            try:
                resp = self._http.post(self._url, json=body)
            except httpx.HTTPError:
                if attempt == _MAX_IN_REQUEST_RETRIES:
                    raise EmbeddingUnavailable("Could not reach the embedding service.") from None
                self._sleep(0.5 * 2**attempt)
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_IN_REQUEST_RETRIES:
                self._sleep(min(_retry_after(resp) or 0.5 * 2**attempt, _MAX_RETRY_WAIT))
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        with observe_provider("openai", "embed"):
            return self._embed_batch_unobserved(texts)

    def _embed_batch_unobserved(self, texts: list[str]) -> list[list[float]]:
        resp = self._post(texts)
        status = resp.status_code
        if status in (401, 403):
            raise EmbeddingAuthFailed(
                "OpenAI rejected your embedding key. Check it in your provider settings."
            )
        if status == 429 and _is_quota(resp):
            raise EmbeddingQuotaExceeded(
                "Your OpenAI account has no embedding quota left. Check its billing."
            )
        if status == 429 or status >= 500:
            raise EmbeddingUnavailable(f"The embedding service is busy (HTTP {status}).")
        if status != 200:
            raise EmbeddingBadResponse(f"The embedding service returned HTTP {status}.")
        try:
            payload = resp.json()
            data = sorted(payload["data"], key=lambda d: d["index"])
            vectors = [d["embedding"] for d in data]
        except (ValueError, KeyError, TypeError):
            raise EmbeddingBadResponse(
                "The embedding service returned an unreadable answer."
            ) from None
        if len(vectors) != len(texts):
            raise EmbeddingBadResponse(
                "The embedding service returned the wrong number of vectors."
            )
        out: list[list[float]] = []
        for row in vectors:
            if not isinstance(row, list) or len(row) != self.dim:
                raise EmbeddingBadResponse(
                    f"Expected {self.dim}-dimensional vectors from the embedding model."
                )
            out.append(l2_normalize([float(x) for x in row]))
        self.usage["requests"] += 1
        try:
            self.usage["tokens"] += float(payload["usage"]["total_tokens"])
        except (KeyError, TypeError, ValueError):
            pass
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors += self._embed_batch(texts[start : start + self.batch_size])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([text])[0]


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        value = resp.headers.get("retry-after")
        return float(value) if value else None
    except ValueError:
        return None


def _is_quota(resp: httpx.Response) -> bool:
    try:
        err = resp.json().get("error", {})
        return "quota" in str(err.get("code", "")) + str(err.get("type", "")).lower()
    except (ValueError, AttributeError):
        return False
