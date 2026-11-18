"""Metrics: right numbers, a guarded endpoint, and never any content."""

import threading
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from app import metrics
from app.core.config import Settings, get_settings
from app.core.errors import AppError, RepoTooLarge
from app.providers.embeddings.cloudflare import CloudflareEmbedder
from app.providers.embeddings.openai import OpenAIEmbedder
from app.providers.llm.openrouter import OpenRouterClient
from app.worker import Worker
from tests.conftest import Persona
from tests.helpers import FakeGitHub
from tests.integration.test_parse_phase import rows
from tests.integration.test_private_providers import (  # noqa: F401  (world is a fixture)
    CHAT_KEY,
    EMB_KEY,
    run_index,
    save,
    world,
)
from tests.integration.test_private_repos import private_repo
from tests.integration.test_search import FILES

pytestmark = pytest.mark.db

TOKEN = "s3cret-metrics-token"
CANARY = "CANARY_METRIC_5b2e91"


def value(name: str, **labels: str) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def configure(c: TestClient, token: str | None = TOKEN, **update: Any) -> None:
    c.app.dependency_overrides[get_settings] = lambda: Settings().model_copy(  # type: ignore[attr-defined]
        update={"metrics_token": SecretStr(token) if token else None, **update}
    )


def scrape(c: TestClient) -> str:
    r = c.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    return r.text


def start_public(c: TestClient, fake: FakeGitHub, name: str = "open") -> str:
    fake.files = dict(FILES)
    rid = c.post("/api/repositories", json={"url": f"https://github.com/acme/{name}"}).json()["id"]
    assert c.post(f"/api/repositories/{rid}/index").status_code == 202
    return str(rid)


def drain(fake: FakeGitHub) -> None:
    w = Worker(get_settings(), github_for=lambda db, uid: fake)
    w.run(threading.Event(), once=True, max_steps=50)


# --- the endpoint ------------------------------------------------------------------------


def test_the_endpoint_does_not_exist_until_a_token_is_configured(anon_client: TestClient) -> None:
    configure(anon_client, None)
    for headers in ({}, {"Authorization": f"Bearer {TOKEN}"}):
        assert anon_client.get("/metrics", headers=headers).status_code == 404


def test_the_endpoint_needs_the_token_and_no_session(
    db_client: TestClient, anon_client: TestClient
) -> None:
    configure(anon_client)
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN + "x"}):
        r = anon_client.get("/metrics", headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "metrics_unauthorized"
    configure(db_client)  # a signed-in user's cookie is not a credential for it
    assert db_client.get("/metrics").status_code == 401
    ok = anon_client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert (
        ok.headers["content-type"].startswith("text/plain")
        and "version=0.0.4" in ok.headers["content-type"]
    )
    assert TOKEN not in ok.text
    assert "/metrics" not in anon_client.get("/openapi.json").text  # not advertised


# --- HTTP metrics ------------------------------------------------------------------------


def test_requests_are_counted_by_route_template_never_by_path(
    db_client: TestClient, anon_client: TestClient
) -> None:
    configure(anon_client)
    rid = uuid.uuid4()
    before = value(
        "cip_http_requests_total",
        method="GET",
        route="/api/repositories/{repository_id}/files/{path:path}",
        status="404",
    )
    db_client.get(f"/api/repositories/{rid}/files/{CANARY}/deep.py")
    db_client.get(f"/nowhere/{CANARY}")
    anon_client.request("BREW", f"/{CANARY}")

    text_ = scrape(anon_client)
    assert (
        value(
            "cip_http_requests_total",
            method="GET",
            route="/api/repositories/{repository_id}/files/{path:path}",
            status="404",
        )
        == before + 1
    )
    assert value("cip_http_requests_total", method="GET", route="unmatched", status="404") >= 1
    assert (
        value("cip_http_requests_total", method="OTHER", route="unmatched", status="405")
        + value("cip_http_requests_total", method="OTHER", route="unmatched", status="404")
        >= 1
    )
    assert CANARY not in text_ and str(rid) not in text_  # no path, no id, no method text
    assert "BREW" not in text_
    assert "cip_http_request_duration_seconds_bucket" in text_


# --- indexing metrics --------------------------------------------------------------------


def test_job_steps_durations_and_failures_are_counted(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    ok = {
        p: value("cip_index_steps_total", phase=p, outcome="ok")
        for p in ("fetch", "parse", "embed", "graph")
    }
    start_public(db_client, fake_gh)
    drain(fake_gh)
    for p in ok:
        assert value("cip_index_steps_total", phase=p, outcome="ok") > ok[p], p
    assert value("cip_index_step_duration_seconds_count", phase="fetch") >= 1
    assert value("cip_index_step_duration_seconds_sum", phase="fetch") > 0

    # a job that fails for good is counted once, by error code
    failures = value("cip_index_job_failures_total", code="repo_too_large")
    errors = value("cip_index_steps_total", phase="fetch", outcome="error")
    start_public(db_client, fake_gh, "huge")
    fake_gh.fail_next = [RepoTooLarge("too big")]
    drain(fake_gh)
    assert value("cip_index_job_failures_total", code="repo_too_large") == failures + 1
    assert value("cip_index_steps_total", phase="fetch", outcome="error") == errors + 1


def test_a_busy_job_is_counted_as_busy(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = start_public(db_client, fake_gh)
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE index_jobs SET lease_until = now() + interval '1 hour'"))
    before = value("cip_index_steps_total", phase="none", outcome="busy")
    assert db_client.post(f"/api/repositories/{rid}/index/tick").json()["ran"] is False
    assert value("cip_index_steps_total", phase="none", outcome="busy") == before + 1


# --- the queue gauges, read from the database --------------------------------------------


def test_queue_gauges_show_depth_state_lag_and_failures(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    metrics.install_queue_collector()
    ids = [start_public(db_client, fake_gh, f"q{i}") for i in range(5)]
    snaps = [r[0] for r in rows(db_engine, "SELECT id FROM snapshots ORDER BY created_at")]
    assert len(snaps) == len(ids)
    with db_engine.begin() as conn:

        def job(i: int, sql: str) -> None:
            conn.execute(
                text(f"UPDATE index_jobs SET {sql} WHERE snapshot_id = :s"), {"s": snaps[i]}
            )

        job(0, "updated_at = now() - interval '200 seconds'")  # waiting, the oldest
        # running, and older than anything waiting: it must not count towards the queue lag
        job(
            1,
            "lease_until = now() + interval '1 hour', updated_at = now() - interval '3000 seconds'",
        )
        job(
            2, "lease_until = now() + interval '1 hour', error_code = 'github_unavailable'"
        )  # backing off
        job(3, "phase = 'embed'")  # waiting, in another phase
        job(4, "phase = 'failed'")

    def n(phase: str, state: str) -> float:
        return value("cip_index_jobs", phase=phase, state=state)

    assert (
        n("fetch", "waiting") == 1 and n("fetch", "running") == 1 and n("fetch", "backing_off") == 1
    )
    assert n("embed", "waiting") == 1 and n("parse", "waiting") == 0
    assert value("cip_index_failed_jobs") == 1
    assert 190 <= value("cip_index_oldest_waiting_seconds") < 400  # the queue lag
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM snapshots"))
    assert value("cip_index_oldest_waiting_seconds") == 0 and n("fetch", "waiting") == 0


def test_a_scrape_survives_a_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise RuntimeError("database down")

    collector = metrics.QueueCollector(broken)
    assert list(collector.collect()) == []  # no exception, simply no queue gauges


# --- provider metrics --------------------------------------------------------------------


def test_provider_calls_are_counted_by_provider_operation_and_error_code() -> None:
    vec = [0.0] * 383 + [1.0]

    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in request.headers["authorization"]:
            return httpx.Response(401, json={"error": {"message": f"key {CANARY}"}})
        if "embeddings" in str(request.url):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": vec}]})
        if "/ai/run/" in str(request.url):
            return httpx.Response(200, json={"result": {"data": [vec]}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}], "usage": {}})

    t = httpx.MockTransport(handler)

    def delta(provider: str, op: str, outcome: str, fn: Any) -> float:
        before = value(
            "cip_provider_requests_total", provider=provider, operation=op, outcome=outcome
        )
        try:
            fn()
        except AppError:
            pass
        return (
            value("cip_provider_requests_total", provider=provider, operation=op, outcome=outcome)
            - before
        )

    sleep = lambda _s: None  # noqa: E731
    assert (
        delta(
            "openai",
            "embed",
            "ok",
            lambda: OpenAIEmbedder("good-key-01234", transport=t, sleep=sleep).embed_query("x"),
        )
        == 1
    )
    assert (
        delta(
            "openai",
            "embed",
            "embedding_auth_failed",
            lambda: OpenAIEmbedder("bad-key-01234", transport=t, sleep=sleep).embed_query("x"),
        )
        == 1
    )
    assert (
        delta(
            "cloudflare",
            "embed",
            "ok",
            lambda: CloudflareEmbedder(
                "0" * 32, "good-key-01234", transport=t, sleep=sleep
            ).embed_query("x"),
        )
        == 1
    )
    assert (
        delta(
            "cloudflare",
            "embed",
            "embedding_auth_failed",
            lambda: CloudflareEmbedder(
                "0" * 32, "bad-key-01234", transport=t, sleep=sleep
            ).embed_query("x"),
        )
        == 1
    )
    assert (
        delta(
            "groq",
            "chat",
            "ok",
            lambda: OpenRouterClient(
                "good-key-01234", "m", None, transport=t, label="groq"
            ).complete("s", "u"),
        )
        == 1
    )
    assert (
        delta(
            "groq",
            "chat",
            "llm_auth_failed",
            lambda: OpenRouterClient(
                "bad-key-01234", "m", None, transport=t, label="groq"
            ).complete("s", "u"),
        )
        == 1
    )
    assert (
        value("cip_provider_request_duration_seconds_count", provider="groq", operation="chat") >= 2
    )
    assert CANARY not in metrics.render().decode()  # the provider's message never becomes a label


def test_an_unexpected_exception_is_counted_as_error() -> None:
    before = value("cip_provider_requests_total", provider="x", operation="y", outcome="error")
    with pytest.raises(ValueError), metrics.observe_provider("x", "y"):
        raise ValueError("boom")
    assert (
        value("cip_provider_requests_total", provider="x", operation="y", outcome="error")
        == before + 1
    )


# --- never any content -------------------------------------------------------------------


def test_metrics_hold_no_names_ids_questions_or_keys(
    db_client: TestClient,
    anon_client: TestClient,
    fake_gh: FakeGitHub,
    world: Any,  # noqa: F811
    alice: Persona,
    db_engine: Engine,
) -> None:
    fake_gh.files = dict(FILES) | {"billing/ledger.py": f'NOTE = "{CANARY}_SOURCE"\n'}
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    run_index(db_client, rid)
    db_client.post(f"/api/repositories/{rid}/search", json={"query": f"{CANARY}_QUESTION"})
    db_client.post(f"/api/repositories/{rid}/chat", json={"question": f"{CANARY}_QUESTION2"})
    configure(anon_client)
    out = scrape(anon_client)
    for forbidden in (
        CANARY,
        "acme/secret",
        "secret",
        rid,
        str(alice.id),
        alice.login,
        EMB_KEY,
        CHAT_KEY,
        "billing/ledger.py",
    ):
        assert forbidden not in out, forbidden
    assert "cip_http_requests_total" in out and "cip_index_steps_total" in out
