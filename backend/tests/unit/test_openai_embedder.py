import json
import math
from typing import Any

import httpx
import pytest

from app.core.errors import (
    EmbeddingAuthFailed,
    EmbeddingBadResponse,
    EmbeddingQuotaExceeded,
    EmbeddingUnavailable,
)
from app.providers.embeddings.openai import OpenAIEmbedder

KEY = "sk-test-key-0123456789"


def make(handler: Any, **kw: Any) -> tuple[OpenAIEmbedder, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request, len(seen))
        return response

    return (
        OpenAIEmbedder(KEY, transport=httpx.MockTransport(wrapped), sleep=lambda _s: None, **kw),
        seen,
    )


def vec(value: float, dim: int = 384) -> list[float]:
    return [value] + [0.0] * (dim - 1)


def ok(vectors: list[list[float]], tokens: int = 7) -> httpx.Response:
    data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    return httpx.Response(200, json={"data": data, "usage": {"total_tokens": tokens}})


def test_it_asks_for_384_dimensions_with_the_key_in_the_header_only() -> None:
    e, seen = make(lambda req, n: ok([vec(3.0)]))
    out = e.embed_documents(["hello"])
    body = json.loads(seen[0].content)
    assert body == {"model": "text-embedding-3-small", "input": ["hello"], "dimensions": 384}
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in seen[0].content.decode() and KEY not in str(seen[0].url)
    assert str(seen[0].url) == "https://api.openai.com/v1/embeddings"
    assert math.isclose(math.sqrt(sum(x * x for x in out[0])), 1.0)  # normalized again
    assert e.model_id == "openai:text-embedding-3-small:384" and e.usage["tokens"] == 7


def test_vectors_come_back_in_input_order_even_if_the_service_shuffles_them() -> None:
    def handler(req: httpx.Request, n: int) -> httpx.Response:
        data = [{"index": 1, "embedding": vec(2.0)}, {"index": 0, "embedding": vec(1.0)}]
        return httpx.Response(200, json={"data": data})

    e, _ = make(handler)
    a, b = e.embed_documents(["a", "b"])
    assert a[0] == 1.0 and b[0] == 1.0  # both normalized to a unit vector on axis 0


def test_a_large_input_is_sent_in_batches() -> None:
    e, seen = make(
        lambda req, n: ok([vec(1.0)] * len(json.loads(req.content)["input"])), batch_size=3
    )
    assert len(e.embed_documents([str(i) for i in range(7)])) == 7
    assert [len(json.loads(r.content)["input"]) for r in seen] == [3, 3, 1]


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_reported_without_echoing_it(status: int) -> None:
    e, _ = make(lambda req, n: httpx.Response(status, json={"error": {"message": KEY}}))
    with pytest.raises(EmbeddingAuthFailed) as exc:
        e.embed_query("x")
    assert KEY not in str(exc.value)


def test_no_quota_is_not_retried_and_a_busy_service_is() -> None:
    quota = {"error": {"code": "insufficient_quota", "type": "insufficient_quota"}}
    e, seen = make(lambda req, n: httpx.Response(429, json=quota))
    with pytest.raises(EmbeddingQuotaExceeded):
        e.embed_query("x")

    e, seen = make(lambda req, n: ok([vec(1.0)]) if n == 3 else httpx.Response(503))
    assert e.embed_query("x")[0] == 1.0
    assert len(seen) == 3  # two retries, then success

    e, _ = make(lambda req, n: httpx.Response(503))
    with pytest.raises(EmbeddingUnavailable):
        e.embed_query("x")


def test_unreadable_or_wrongly_shaped_answers_are_rejected() -> None:
    for response in (
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"nope": 1}),
        ok([vec(1.0, 10)]),  # wrong dimension
        ok([vec(1.0), vec(1.0)]),  # wrong count for one input
        httpx.Response(418),
    ):
        e, _ = make(lambda req, n, r=response: r)
        with pytest.raises(EmbeddingBadResponse):
            e.embed_documents(["only one"])


def test_the_client_never_follows_redirects() -> None:
    e, seen = make(
        lambda req, n: httpx.Response(302, headers={"location": "https://evil.example/"})
    )
    with pytest.raises(EmbeddingBadResponse):
        e.embed_query("x")
    assert len(seen) == 1
