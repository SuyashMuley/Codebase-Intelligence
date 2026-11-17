import json
import math
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import (
    EmbeddingAuthFailed,
    EmbeddingBadResponse,
    EmbeddingNotConfigured,
    EmbeddingQuotaExceeded,
    EmbeddingUnavailable,
)
from app.providers.embeddings.cloudflare import CloudflareEmbedder
from app.providers.embeddings.factory import get_embedder
from app.providers.embeddings.hash import HashEmbedder, tokens

TOKEN = "cf-test-token-" + "z" * 24
ACCOUNT = "a" * 32
DIM = 8  # small vectors keep the fixtures readable


def vec(seed: int, dim: int = DIM) -> list[float]:
    return [float((seed + i) % 5 + 1) for i in range(dim)]  # deliberately NOT unit length


def make(handler: Any, **kw: Any) -> CloudflareEmbedder:
    sleeps: list[float] = []
    embedder = CloudflareEmbedder(
        ACCOUNT,
        TOKEN,
        dim=DIM,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kw,
    )
    embedder.slept = sleeps  # type: ignore[attr-defined]
    return embedder


def ok(texts: list[str], neurons: str = "0.5") -> httpx.Response:
    data = [vec(i) for i in range(len(texts))]
    return httpx.Response(
        200,
        json={"success": True, "result": {"shape": [len(data), DIM], "data": data}},
        headers={"cf-ai-neurons": neurons},
    )


def norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


# --- Cloudflare: the happy path ---------------------------------------------------------------


def test_request_shape_auth_and_model_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(json.loads(request.content)["text"])

    e = make(handler, model="@cf/baai/bge-small-en-v1.5", pooling="cls")
    e.embed_documents(["alpha", "beta"])
    req = seen[0]
    assert req.url.path == f"/client/v4/accounts/{ACCOUNT}/ai/run/@cf/baai/bge-small-en-v1.5"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content) == {"text": ["alpha", "beta"], "pooling": "cls"}
    assert e.model_id == "cloudflare:@cf/baai/bge-small-en-v1.5:cls"


def test_vectors_are_returned_in_order_and_unit_length() -> None:
    e = make(lambda r: ok(json.loads(r.content)["text"]))
    vectors = e.embed_documents(["a", "b", "c"])
    assert len(vectors) == 3 and all(len(v) == DIM for v in vectors)
    assert all(norm(v) == pytest.approx(1.0) for v in vectors)  # normalized even if the API did not
    raw = [vec(i) for i in range(3)]
    assert vectors[0] == pytest.approx([x / norm(raw[0]) for x in raw[0]])


def test_large_inputs_are_split_into_batches_without_losing_order() -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["text"]
        sizes.append(len(texts))
        # Echo something derived from each text so order can be checked afterwards.
        data = [[float(int(t.split("-")[1]) + 1)] + [0.0] * (DIM - 1) for t in texts]
        return httpx.Response(200, json={"result": {"data": data}})

    e = make(handler, batch_size=4)
    vectors = e.embed_documents([f"text-{i}" for i in range(10)])
    assert sizes == [4, 4, 2]
    assert [v[0] for v in vectors] == [1.0] * 10  # normalized to unit length, direction kept
    assert e.usage["requests"] == 3


def test_an_empty_list_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert make(handler).embed_documents([]) == []


def test_only_queries_get_the_instruction_prefix() -> None:
    bodies: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["text"]
        bodies.append(texts)
        return ok(texts)

    e = make(handler, query_prefix="Represent this query: ")
    e.embed_documents(["def f(): pass"])
    e.embed_query("where is f defined")
    assert bodies == [["def f(): pass"], ["Represent this query: where is f defined"]]


def test_neurons_and_requests_are_accumulated_for_cost_visibility() -> None:
    e = make(lambda r: ok(json.loads(r.content)["text"], neurons="1.25"), batch_size=1)
    e.embed_documents(["a", "b", "c"])
    assert e.usage == {"requests": 3.0, "neurons": pytest.approx(3.75)}


def test_a_missing_or_garbled_usage_header_does_not_break_embedding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        resp = ok(json.loads(request.content)["text"])
        resp.headers["cf-ai-neurons"] = "not-a-number"
        return resp

    e = make(handler)
    assert len(e.embed_documents(["a"])) == 1 and e.usage["neurons"] == 0.0


# --- Cloudflare: failures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error"), [(401, EmbeddingAuthFailed), (403, EmbeddingAuthFailed)]
)
def test_rejected_credentials_are_not_retried(status: int, error: type[Exception]) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            status, json={"errors": [{"code": 10000, "message": "Authentication error"}]}
        )

    with pytest.raises(error):
        make(handler).embed_documents(["x"])
    assert calls["n"] == 1


def test_payment_required_and_quota_messages_mean_the_allowance_is_used_up() -> None:
    with pytest.raises(EmbeddingQuotaExceeded):
        make(lambda r: httpx.Response(402, json={"errors": []})).embed_documents(["x"])
    quota = httpx.Response(
        429, json={"errors": [{"message": "you have exceeded your daily neuron quota"}]}
    )
    with pytest.raises(EmbeddingQuotaExceeded):
        make(lambda r: quota).embed_documents(["x"])


def test_rate_limits_and_server_errors_are_retried_then_reported_as_unavailable() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="busy")

    e = make(handler)
    with pytest.raises(EmbeddingUnavailable) as exc:
        e.embed_documents(["x"])
    assert calls["n"] == 3  # the first try plus two retries
    assert exc.value.retryable is True
    assert len(e.slept) == 2  # type: ignore[attr-defined]


def test_a_transient_failure_followed_by_success_is_invisible_to_the_caller() -> None:
    responses = [httpx.Response(503), httpx.Response(429, headers={"retry-after": "2"})]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0) if responses else ok(json.loads(request.content)["text"])

    e = make(handler)
    assert len(e.embed_documents(["x"])) == 1
    assert e.slept == [0.5, 2.0]  # type: ignore[attr-defined]  # exponential backoff, then Retry-After


def test_retry_after_is_capped() -> None:
    responses = [httpx.Response(429, headers={"retry-after": "9999"})]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0) if responses else ok(json.loads(request.content)["text"])

    e = make(handler)
    e.embed_documents(["x"])
    assert e.slept == [10.0]  # type: ignore[attr-defined]


def test_network_errors_are_retried_then_reported_as_unavailable() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(EmbeddingUnavailable):
        make(handler).embed_documents(["x"])
    assert calls["n"] == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"errors": [{"message": "Bad input"}]}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"success": True}),  # no result
        httpx.Response(200, json={"result": {"data": "nope"}}),
        httpx.Response(200, json={"result": {"data": [vec(0)]}}),  # one vector for two texts
        httpx.Response(200, json={"result": {"data": [vec(0, 5), vec(1, 5)]}}),  # wrong dimension
        httpx.Response(200, json={"result": {"data": [vec(0), "x"]}}),
    ],
)
def test_unexpected_answers_are_rejected_not_stored(response: httpx.Response) -> None:
    with pytest.raises(EmbeddingBadResponse):
        make(lambda r: response).embed_documents(["a", "b"])


def test_the_api_token_never_appears_in_an_error() -> None:
    for status in (401, 402, 429, 500, 400):
        with pytest.raises(Exception) as exc:  # noqa: PT011
            make(lambda r, s=status: httpx.Response(s, json={"errors": []})).embed_documents(["x"])
        assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)
        assert ACCOUNT not in str(exc.value)


# --- the offline hash embedder ----------------------------------------------------------------


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hash_embedder_gives_identical_vectors_in_separate_processes() -> None:
    """Vectors are stored in the database, so they must not depend on anything per-process.
    Python's built-in hash() is randomized per process; this embedder must not use it."""
    import subprocess
    import sys

    code = (
        "import json; from app.providers.embeddings.hash import HashEmbedder; "
        "print(json.dumps(HashEmbedder().embed_query('create session cookie')))"
    )

    def run(seed: str) -> list[float]:
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": ".", "PATH": ""},
        )
        return list(json.loads(out.stdout))

    here = HashEmbedder().embed_query("create session cookie")
    assert run("1") == run("2") == here
    assert HashEmbedder().embed_query("create session cookie") == here


def test_hash_vectors_have_the_right_size_and_unit_length() -> None:
    e = HashEmbedder(dim=384)
    v = e.embed_documents(["def create_session(user): return Session(user)"])[0]
    assert len(v) == 384 and norm(v) == pytest.approx(1.0)
    assert e.model_id == "hash-384-v1"


def test_texts_that_share_words_are_closer_than_unrelated_ones() -> None:
    e = HashEmbedder()
    q = e.embed_query("create a session cookie")
    related = e.embed_documents(["def create_session_cookie(user): ..."])[0]
    unrelated = e.embed_documents(["parse the command line arguments with argparse"])[0]
    assert cosine(q, related) > cosine(q, unrelated)


def test_camel_case_identifiers_are_also_matched_by_their_parts() -> None:
    assert tokens("sessionCookie") == ["sessioncookie", "session", "cookie"]
    e = HashEmbedder()
    q = e.embed_query("session cookie")
    assert cosine(q, e.embed_documents(["sessionCookie"])[0]) > cosine(
        q, e.embed_documents(["databaseConnection"])[0]
    )


def test_text_with_no_words_still_gets_a_usable_vector() -> None:
    e = HashEmbedder()
    v = e.embed_documents(["}"])[0]
    assert norm(v) == pytest.approx(1.0)  # never the zero vector (cosine distance would be NaN)
    assert e.embed_documents(["}"])[0] == v and e.embed_documents(["{"])[0] != v


# --- choosing the provider --------------------------------------------------------------------


def settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "cloudflare_account_id": None,
        "cloudflare_api_token": None,
        "embedding_provider": "cloudflare",
    }
    return Settings(**{**base, **kw})


def test_cloudflare_without_credentials_is_a_clear_error_not_a_silent_fallback() -> None:
    with pytest.raises(EmbeddingNotConfigured) as exc:
        get_embedder(settings())
    assert "CLOUDFLARE_API_TOKEN" in exc.value.message
    assert exc.value.retryable is False
    with pytest.raises(EmbeddingNotConfigured):
        get_embedder(settings(cloudflare_account_id=ACCOUNT))  # token still missing


def test_the_configured_provider_is_used() -> None:
    assert isinstance(get_embedder(settings(embedding_provider="hash")), HashEmbedder)
    e = get_embedder(
        settings(
            cloudflare_account_id=ACCOUNT, cloudflare_api_token=TOKEN, cloudflare_pooling="mean"
        )
    )
    assert isinstance(e, CloudflareEmbedder) and e.model_id.endswith(":mean")
    assert e.dim == 384


def test_the_same_settings_share_one_http_client() -> None:
    s = settings(cloudflare_account_id=ACCOUNT, cloudflare_api_token=TOKEN)
    assert get_embedder(s) is get_embedder(s)


def test_the_model_id_known_from_settings_matches_the_embedders_own() -> None:
    """The embed phase compares stored vectors against one id and stores another; they must be
    the same string for every real provider, or vectors would be discarded and redone."""
    from app.providers.embeddings.factory import embedding_model_id

    for s in (
        settings(embedding_provider="hash"),
        settings(cloudflare_account_id=ACCOUNT, cloudflare_api_token=TOKEN),
        settings(
            cloudflare_account_id=ACCOUNT, cloudflare_api_token=TOKEN, cloudflare_pooling="mean"
        ),
        settings(embedding_provider="hash", embedding_dim=128),
    ):
        assert embedding_model_id(s) == get_embedder(s).model_id
