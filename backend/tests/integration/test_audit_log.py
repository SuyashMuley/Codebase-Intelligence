"""The audit log records that a private repository was used, never what was in it."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import AuditEvent, User
from app.services import audit
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
from tests.integration.test_private_repos import (
    PUBLIC,
    age_the_check,
    connect,
    private_repo,
)
from tests.integration.test_search import FILES

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("world")]


def events(engine: Engine, where: str = "TRUE") -> list[Any]:
    return rows(engine, f"SELECT * FROM audit_events WHERE {where} ORDER BY id")


def actions(engine: Engine, where: str = "TRUE") -> list[str]:
    return [e.action for e in events(engine, where)]


# --- what is recorded --------------------------------------------------------------------


def test_connecting_reading_and_disconnecting_a_private_repository_are_recorded(
    db_client: TestClient, fake_gh: FakeGitHub, alice: Persona, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    db_client.get(f"/api/repositories/{rid}/files")
    db_client.get(f"/api/repositories/{rid}/stats")
    db_client.delete(f"/api/repositories/{rid}")

    got = events(db_engine)
    assert [e.action for e in got] == [
        "private.connect",
        "private.list_files",
        "private.get_stats",
        "private.disconnect",
    ]
    for e in got:
        assert e.user_id == alice.id and e.repository == "acme/secret"
        assert str(e.repository_id) == rid and e.request_id  # each ties back to a request log line
    assert got[0].detail == {"new": True}


def test_status_polling_is_not_recorded_but_other_reads_are(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    before = len(events(db_engine))
    for _ in range(5):
        db_client.get(f"/api/repositories/{rid}/status")
    assert len(events(db_engine)) == before
    db_client.get(f"/api/repositories/{rid}/files")
    assert len(events(db_engine)) == before + 1


def test_public_repositories_are_not_audited(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = connect(db_client, PUBLIC).json()["id"]
    db_client.get(f"/api/repositories/{rid}/files")
    db_client.delete(f"/api/repositories/{rid}")
    assert events(db_engine) == []


def test_provider_changes_are_recorded_without_the_key(
    db_client: TestClient, alice: Persona, db_engine: Engine
) -> None:
    save(db_client, "chat")
    save(db_client, "embedding", mode="browser")
    db_client.delete("/api/providers/chat")
    got = events(db_engine)
    assert [e.action for e in got] == ["provider.save", "provider.save", "provider.remove"]
    assert got[0].detail == {"slot": "chat", "kind": "openai", "mode": "stored"}
    assert got[1].detail["mode"] == "browser" and got[0].repository_id is None
    assert all(e.user_id == alice.id for e in got)
    flat = "".join(str(tuple(e)) for e in got)
    assert CHAT_KEY not in flat and EMB_KEY not in flat and CHAT_KEY[-4:] not in flat


def test_lost_access_and_retention_purges_are_recorded(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    fake_gh.hidden_names.add("acme/secret")
    age_the_check(db_engine)
    assert db_client.get(f"/api/repositories/{rid}/files").status_code == 403
    assert "private.access_lost" in actions(db_engine)
    # and no "access" event for a request that was refused
    assert "private.list_files" not in actions(db_engine)


def test_a_retention_purge_is_recorded_with_its_count(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    run_index(db_client, rid)
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE snapshots SET created_at = now() - interval '400 days'"))
    db_client.get(f"/api/repositories/{rid}/files")
    purge = [e for e in events(db_engine) if e.action == "private.retention_purge"]
    assert len(purge) == 1 and purge[0].detail == {"snapshots": 1}


# --- the guard on what may be written ----------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"q": "x" * 61},  # long: could be a question or a code line
        {"q": "two\nlines"},
        {"q": ["a", "b"]},
        {"q": {"nested": "x"}},
        {"q": b"bytes"},
    ],
)
def test_only_short_plain_values_can_be_written(
    db_clean: Engine, alice: Persona, db_engine: Engine, bad: dict[str, Any]
) -> None:
    with Session(db_engine) as db, pytest.raises(ValueError):
        audit.record(db, "x", alice.id, None, bad)
    assert events(db_engine) == []


def test_a_failed_audit_write_stops_the_private_read(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = private_repo(db_client, fake_gh)

    def broken(*a: Any, **k: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "record", broken)
    r = db_client.get(f"/api/repositories/{rid}/files")
    assert r.status_code == 500  # nothing is served that could not be recorded


# --- reading it --------------------------------------------------------------------------


def test_you_can_read_your_own_trail_and_only_yours(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    db_client.get(f"/api/repositories/{rid}/files")
    private_repo(other_client, fake_gh, "https://github.com/acme/bobs")
    other_client.get("/api/providers")

    mine = db_client.get("/api/audit").json()["items"]
    assert [e["action"] for e in mine] == ["private.list_files", "private.connect"]  # newest first
    assert {e["repository"] for e in mine} == {"acme/secret"}
    bobs = other_client.get("/api/audit").json()["items"]
    assert {e["repository"] for e in bobs} == {"acme/bobs"}
    assert len(db_client.get("/api/audit?limit=1").json()["items"]) == 1
    assert db_client.get("/api/audit?limit=0").status_code == 422
    assert db_client.get("/api/audit?limit=201").status_code == 422


def test_the_trail_needs_sign_in(anon_client: TestClient) -> None:
    assert anon_client.get("/api/audit").status_code == 401


# --- it outlives what it describes -------------------------------------------------------


def test_events_survive_deleting_the_repository_and_the_account(
    db_client: TestClient, fake_gh: FakeGitHub, alice: Persona, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    db_client.delete(f"/api/repositories/{rid}")
    assert rows(db_engine, "SELECT count(*) FROM repositories")[0][0] == 0
    n = len(events(db_engine))
    with Session(db_engine) as db:
        user = db.get(User, alice.id)
        assert user is not None
        db.delete(user)
        db.commit()
    assert len(events(db_engine)) == n and n >= 2


# --- its own retention -------------------------------------------------------------------


def with_token(c: TestClient, **update: Any) -> None:
    from pydantic import SecretStr

    c.app.dependency_overrides[get_settings] = lambda: Settings().model_copy(  # type: ignore[attr-defined]
        update={"maintenance_token": SecretStr("s3cret-maintenance-token"), **update}
    )


def test_old_audit_events_are_purged_and_zero_keeps_them(
    db_clean: Engine, anon_client: TestClient, alice: Persona, db_engine: Engine
) -> None:
    with Session(db_engine) as db:
        db.add(AuditEvent(user_id=alice.id, action="old", detail={}))
        db.add(AuditEvent(user_id=alice.id, action="new", detail={}))
        db.commit()
    with db_engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_events SET at = now() - interval '400 days' WHERE action = 'old'")
        )
    headers = {"Authorization": "Bearer s3cret-maintenance-token"}

    with_token(anon_client, audit_retention_days=0)
    assert (
        anon_client.post("/api/internal/purge-expired", headers=headers).json()[
            "deleted_audit_events"
        ]
        == 0
    )
    assert actions(db_engine) == ["old", "new"]

    with_token(anon_client, audit_retention_days=365)
    assert (
        anon_client.post("/api/internal/purge-expired", headers=headers).json()[
            "deleted_audit_events"
        ]
        == 1
    )
    assert actions(db_engine) == ["new"]
    assert uuid.UUID(str(alice.id))
