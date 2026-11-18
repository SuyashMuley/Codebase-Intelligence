"""Logs never contain file contents, questions, model output or keys.

A private repository is indexed, searched, asked about and reviewed, including the failure paths,
with distinctive marker strings planted in the code, the questions, the model's replies and the
keys. Every log record from every logger (at DEBUG) is then searched for them.
"""

import json
import logging
import traceback
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.providers.embeddings.base import Embedder
from app.services.providers import build
from app.services.providers.build import build_chat as real_build_chat
from app.services.providers.build import build_embedder as real_build_embedder
from tests.helpers import FakeGitHub
from tests.integration.test_chat_api import FakeChat
from tests.integration.test_private_providers import (  # noqa: F401  (world is a fixture)
    CHAT_KEY,
    EMB_KEY,
    run_index,
    save,
    world,
)
from tests.integration.test_private_repos import private_repo
from tests.integration.test_search import FILES

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("world")]

SOURCE = "CANARY_SOURCE_7f3a9c"
QUESTION = "CANARY_QUESTION_91bc3d"
REPLY = "CANARY_REPLY_55de01"
SECRET_VALUE = "CANARY_SECRET_c0ffee42"  # looks like a credential in the code

CANARIES = {
    "source": SOURCE,
    "question": QUESTION,
    "model reply": REPLY,
    "embedding key": EMB_KEY,
    "chat key": CHAT_KEY,
    "key (middle)": EMB_KEY[4:20],
    "chat key (middle)": CHAT_KEY[4:20],
    "secret value": SECRET_VALUE,
}

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def flatten(record: logging.LogRecord) -> str:
    """Everything a log line could carry: message, extra fields and any traceback."""
    parts = [record.getMessage(), record.name]
    parts += [repr(v) for k, v in record.__dict__.items() if k not in _RESERVED]
    if record.exc_info:
        parts.append("".join(traceback.format_exception(*record.exc_info)))
    parts.append(record.exc_text or "")
    return "\n".join(parts)


def assert_clean(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "nothing was logged: the capture is not working"
    blob = "\n".join(flatten(r) for r in caplog.records)
    for name, canary in CANARIES.items():
        assert canary not in blob, f"the {name} appears in the logs"


def reply(text: str) -> FakeChat:
    return FakeChat(text)


@pytest.fixture
def repo(db_client: TestClient, fake_gh: FakeGitHub) -> str:
    files = dict(FILES)
    files["billing/ledger.py"] = (
        f'LEDGER_NOTE = "{SOURCE}"\n'
        f'api_key = "{SECRET_VALUE}0123456789abcdef"\n\n\n'
        "def ledger_total(items):\n    return sum(items)\n"
    )
    fake_gh.files = files
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    return rid


def test_the_whole_private_flow_logs_no_contents_prompts_or_keys(
    db_client: TestClient,
    repo: str,
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    w, _ = world
    base = f"/api/repositories/{repo}"
    run_index(db_client, repo)

    for r in (
        db_client.get(f"{base}/files"),
        db_client.get(f"{base}/files/billing/ledger.py"),
        db_client.get(f"{base}/stats"),
        db_client.get(f"{base}/graph"),
        db_client.post(f"{base}/search", json={"query": f"ledger {QUESTION}"}),
        db_client.post(f"{base}/search", json={"query": QUESTION, "mode": "keyword"}),
        db_client.post(f"{base}/chat", json={"question": f"What is the ledger? {QUESTION}"}),
    ):
        assert r.status_code == 200, r.text
    # the planted values really did reach the code paths being tested
    assert SOURCE in db_client.get(f"{base}/files/billing/ledger.py").text
    assert any(QUESTION in p for c in w.chats for p in c.prompts)
    assert SECRET_VALUE not in db_client.get(f"{base}/files/billing/ledger.py").text  # redacted

    assert_clean(caplog)


def test_failure_paths_do_not_log_contents_either(
    db_client: TestClient,
    repo: str,
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    base = f"/api/repositories/{repo}"
    run_index(db_client, repo)

    answers = [
        f"this is not json {REPLY}",  # unreadable reply
        json.dumps(
            {
                "cannot_determine": False,
                "claims": [{"text": REPLY, "cites": ["C1"], "quote": REPLY}],
                "missing": REPLY,
            }
        ),
        json.dumps({"cannot_determine": True, "claims": [], "missing": REPLY}),
        "",  # empty reply
    ]
    for text in answers:
        monkeypatch.setattr(build, "build_chat", lambda row, key, s, t=text: reply(t))
        r = db_client.post(f"{base}/chat", json={"question": f"{QUESTION} what is it?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (
            REPLY not in json.dumps(body["claims"]) or body["claims"] == []
        )  # unverified text is not shown

    # an unexpected crash inside the model client
    class Exploding(FakeChat):
        def complete(self, system: str, user: str, **kw: Any) -> str:
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(build, "build_chat", lambda row, key, s: Exploding("x"))
    assert db_client.post(f"{base}/chat", json={"question": QUESTION}).status_code == 500
    assert (
        db_client.post(f"{base}/impact/review", json={"file": "billing/ledger.py"}).status_code
        == 500
    )

    # an unexpected crash while embedding a search question
    def boom(row: Any, key: str, s: Any) -> Any:
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr(build, "build_embedder", boom)
    assert db_client.post(f"{base}/search", json={"query": QUESTION}).status_code == 500

    assert any(r.exc_info for r in caplog.records)  # the crashes were logged, with tracebacks
    assert_clean(caplog)


def test_browser_held_keys_sent_in_headers_are_not_logged(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    world: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding", mode="browser")
    save(db_client, "chat", mode="browser")
    headers = {"X-Embedding-Key": EMB_KEY, "X-Chat-Key": CHAT_KEY}
    run_index(db_client, rid, headers=headers)
    db_client.post(f"/api/repositories/{rid}/search", json={"query": QUESTION}, headers=headers)
    db_client.post(f"/api/repositories/{rid}/chat", json={"question": QUESTION}, headers=headers)
    db_client.post(f"/api/repositories/{rid}/search", json={"query": QUESTION})  # missing keys: 409
    assert_clean(caplog)


def test_the_check_would_notice_a_leak(caplog: pytest.LogCaptureFixture) -> None:
    """A guard that cannot fail proves nothing."""
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("app.test")
    leaks: list[Callable[[], None]] = [
        lambda: log.info(f"asked: {QUESTION}"),
        lambda: log.info("event", extra={"excerpt": SOURCE}),
        lambda: log.error("x", exc_info=_raised(f"bad key {EMB_KEY}")),
    ]
    for leak in leaks:
        caplog.clear()
        leak()
        with pytest.raises(AssertionError):
            assert_clean(caplog)


def _raised(message: str) -> tuple[Any, Any, Any]:
    import sys

    try:
        raise ValueError(message)
    except ValueError:
        return sys.exc_info()


def test_the_real_provider_clients_log_nothing_sensitive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The flow tests use fake providers; this drives the real clients, over mocked HTTP, on
    success and on errors whose bodies echo the key and the prompt back."""
    import types

    import httpx

    from app.core.config import Settings
    from app.core.errors import AppError
    from app.providers.embeddings.cloudflare import CloudflareEmbedder
    from app.providers.embeddings.openai import OpenAIEmbedder
    from app.providers.llm.openrouter import OpenRouterClient

    caplog.set_level(logging.DEBUG)
    echo = {"error": {"message": f"bad key {CHAT_KEY} for prompt {QUESTION} {SOURCE}"}}

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=echo)

    def succeeding(request: httpx.Request) -> httpx.Response:
        vec = [0.0] * 383 + [1.0]
        if "embeddings" in str(request.url):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": vec}]})
        if "/ai/run/" in str(request.url):
            return httpx.Response(200, json={"result": {"data": [vec]}})
        return httpx.Response(200, json={"choices": [{"message": {"content": REPLY}}], "usage": {}})

    for handler in (succeeding, failing):
        transport = httpx.MockTransport(handler)
        clients: list[Embedder] = [
            OpenAIEmbedder(EMB_KEY, transport=transport, sleep=lambda _s: None),
            CloudflareEmbedder("0" * 32, EMB_KEY, transport=transport, sleep=lambda _s: None),
        ]
        for emb in clients:
            try:
                emb.embed_documents([f"{SOURCE} {QUESTION}"])
                emb.embed_query(QUESTION)
            except AppError as exc:
                assert (
                    EMB_KEY not in str(exc) and CHAT_KEY not in str(exc) and SOURCE not in str(exc)
                )
        llm = OpenRouterClient(CHAT_KEY, "m", None, transport=transport)
        try:
            llm.complete(f"system {SOURCE}", f"user {QUESTION}")
        except AppError as exc:
            assert CHAT_KEY not in str(exc) and QUESTION not in str(exc) and SOURCE not in str(exc)

    # The builders themselves (these were replaced by fakes in the flow tests).
    s = Settings()
    chat_row = types.SimpleNamespace(kind="openai", model="gpt-4o-mini")
    emb_row = types.SimpleNamespace(kind="cloudflare", model=None, account_id="0" * 32)
    # (the world fixture replaced these on the module; the originals were imported above)
    real_build_chat(chat_row, CHAT_KEY, s)  # type: ignore[arg-type]
    real_build_embedder(emb_row, EMB_KEY, s)  # type: ignore[arg-type]

    assert_clean(caplog) if caplog.records else None
