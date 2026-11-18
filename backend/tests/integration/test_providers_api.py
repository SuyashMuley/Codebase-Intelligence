"""Saving, showing and removing your own providers. The key must never come back out."""

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import User
from app.services.auth.crypto import TokenCipher
from tests.conftest import TEST_ENV, Persona

pytestmark = pytest.mark.db

KEY = "sk-live-SECRET-0123456789abcd"
ACCOUNT = "0123456789abcdef0123456789abcdef"


def put(c: TestClient, slot: str, **body: Any) -> Any:
    return c.put(f"/api/providers/{slot}", json=body)


def chat(**over: Any) -> dict[str, Any]:
    return {"kind": "openai", "mode": "stored", "api_key": KEY, **over}


def rows(engine: Engine, sql: str = "SELECT * FROM user_providers") -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql)))


def test_nothing_is_saved_at_first_and_the_catalogue_is_listed(db_client: TestClient) -> None:
    body = db_client.get("/api/providers").json()
    assert body["embedding"] is None and body["chat"] is None
    assert {p["id"] for p in body["chat_providers"]} == {"openrouter", "openai", "together", "groq"}
    cf = next(p for p in body["embedding_providers"] if p["id"] == "cloudflare")
    assert cf["needs_account_id"] is True and cf["models"] == ["@cf/baai/bge-small-en-v1.5"]


def test_a_stored_key_is_encrypted_and_never_returned(
    db_client: TestClient, db_engine: Engine, alice: Persona, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    r = put(db_client, "chat", **chat())
    assert r.status_code == 200
    out = r.json()
    assert (
        out["key_last4"] == KEY[-4:] and out["mode"] == "stored" and out["model"] == "gpt-4o-mini"
    )
    assert KEY not in r.text and "api_key" not in out

    row = rows(db_engine)[0]
    assert KEY not in "".join(str(x) for x in row)  # not in any column, in clear
    cipher = TokenCipher(TEST_ENV["TOKEN_ENCRYPTION_KEY"])
    assert cipher.decrypt(row.key_enc) == KEY  # recoverable only with the server's key

    got = db_client.get("/api/providers")
    assert KEY not in got.text and got.json()["chat"]["key_last4"] == KEY[-4:]
    assert KEY not in caplog.text  # nothing in any log line, including DEBUG
    assert KEY[:12] not in caplog.text


def test_a_rejected_request_does_not_echo_the_key(db_client: TestClient, db_engine: Engine) -> None:
    for body in (
        chat(kind="not-a-provider"),
        chat(model="bad model!"),
        {"mode": "stored", "api_key": KEY},  # no kind: fails validation before our code runs
        chat(mode="elsewhere"),
    ):
        r = put(db_client, "chat", **body)
        assert r.status_code == 422, r.text
        assert KEY not in r.text
    assert rows(db_engine) == []


def test_browser_mode_saves_the_choice_but_no_key(db_client: TestClient, db_engine: Engine) -> None:
    r = put(db_client, "chat", kind="openai", mode="browser")
    assert r.status_code == 200 and r.json()["key_last4"] is None
    row = rows(db_engine)[0]
    assert row.mode == "browser" and row.key_enc is None and row.key_last4 is None
    # Sending a key for saving in this mode is refused, and is not kept.
    refused = put(db_client, "chat", **chat(mode="browser"))
    assert refused.status_code == 422 and KEY not in refused.text
    assert rows(db_engine)[0].key_enc is None


def test_saving_again_replaces_the_provider_and_switching_mode_drops_the_key(
    db_client: TestClient, db_engine: Engine
) -> None:
    put(db_client, "chat", **chat())
    put(
        db_client,
        "chat",
        **chat(kind="groq", model="llama-3.3-70b-versatile", api_key="gsk_other_key_0123456789"),
    )
    assert len(rows(db_engine)) == 1
    assert db_client.get("/api/providers").json()["chat"]["kind"] == "groq"
    put(db_client, "chat", kind="groq", mode="browser")
    row = rows(db_engine)[0]
    assert row.key_enc is None and row.key_last4 is None  # the stored key was erased


def test_cloudflare_embeddings_need_an_account_id(db_client: TestClient) -> None:
    assert (
        put(db_client, "embedding", kind="cloudflare", mode="stored", api_key=KEY).status_code
        == 422
    )
    ok = put(
        db_client, "embedding", kind="cloudflare", mode="stored", api_key=KEY, account_id=ACCOUNT
    )
    assert ok.status_code == 200 and ok.json()["account_id"] == ACCOUNT
    # Only models that give 384 dimensions are accepted.
    bad = put(
        db_client,
        "embedding",
        kind="openai",
        mode="stored",
        api_key=KEY,
        model="text-embedding-ada-002",
    )
    assert bad.status_code == 422


def test_removing_a_provider_deletes_its_stored_key(
    db_client: TestClient, db_engine: Engine
) -> None:
    put(db_client, "chat", **chat())
    assert db_client.delete("/api/providers/chat").status_code == 204
    assert rows(db_engine) == []
    assert db_client.delete("/api/providers/chat").status_code == 204  # idempotent


def test_unknown_slots_are_refused(db_client: TestClient) -> None:
    assert put(db_client, "images", **chat()).status_code == 422
    assert db_client.delete("/api/providers/images").status_code == 422


def test_providers_belong_to_one_user(
    db_client: TestClient, other_client: TestClient, db_engine: Engine
) -> None:
    put(db_client, "chat", **chat())
    assert other_client.get("/api/providers").json()["chat"] is None
    other_client.delete("/api/providers/chat")  # bob "removing" alice's changes nothing
    assert len(rows(db_engine)) == 1
    put(other_client, "chat", **chat(kind="groq", api_key="gsk_bob_key_0123456789"))
    assert len(rows(db_engine)) == 2


def test_sign_in_and_same_origin_are_required(
    db_client: TestClient, anon_client: TestClient, db_engine: Engine
) -> None:
    assert anon_client.get("/api/providers").status_code == 401
    assert anon_client.put("/api/providers/chat", json=chat()).status_code == 401
    assert anon_client.delete("/api/providers/chat").status_code == 401
    cross = db_client.put(
        "/api/providers/chat", json=chat(), headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    assert rows(db_engine) == []


def test_deleting_a_user_deletes_their_providers(
    db_client: TestClient, db_engine: Engine, alice: Persona
) -> None:
    put(db_client, "chat", **chat())
    with Session(db_engine) as db:
        user = db.get(User, alice.id)
        assert user is not None
        db.delete(user)
        db.commit()
    assert rows(db_engine) == []


def test_the_database_itself_refuses_inconsistent_rows(
    db_client: TestClient, db_engine: Engine, alice: Persona
) -> None:
    def insert(mode: str, key: str | None, slot: str = "chat") -> None:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO user_providers (user_id, slot, kind, mode, key_enc) "
                    "VALUES (:u, :s, 'openai', :m, :k)"
                ),
                {"u": alice.id, "s": slot, "m": mode, "k": key},
            )

    with pytest.raises(IntegrityError):
        insert("stored", None)  # stored without a key
    with pytest.raises(IntegrityError):
        insert("browser", "secret")  # browser mode must hold no key
    with pytest.raises(IntegrityError):
        insert("stored", "x", slot="images")
    with pytest.raises(IntegrityError):
        insert("elsewhere", None)
