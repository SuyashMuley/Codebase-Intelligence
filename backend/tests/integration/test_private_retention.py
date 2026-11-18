"""Private data expires, and deleting a private repository leaves nothing behind."""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Repository
from app.services.repos import retention
from tests.conftest import Persona
from tests.helpers import FakeGitHub
from tests.integration.test_indexing_api import override_settings
from tests.integration.test_parse_phase import rows
from tests.integration.test_private_providers import (  # noqa: F401  (world is a fixture)
    run_index,
    save,
    world,
)
from tests.integration.test_private_repos import private_repo
from tests.integration.test_search import FILES

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("world")]


def backdate(engine: Engine, days: float, where: str = "TRUE") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE snapshots SET created_at = now() - make_interval(secs => :s) WHERE {where}"
            ),
            {"s": days * 86400},
        )


def count(engine: Engine, table: str) -> int:
    return int(rows(engine, f"SELECT count(*) FROM {table}")[0][0])


def seed(
    engine: Engine, owner: uuid.UUID, private: bool, status: str, age_days: float
) -> uuid.UUID:
    """A repository (private for `owner`, or public), one snapshot of that age, one file."""
    repo, snap = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO repositories (id, owner, name, url, is_private, owner_user_id) "
                "VALUES (:r, 'acme', :n, 'https://github.com/acme/x', :p, :o)"
            ),
            {"r": repo, "n": f"r-{repo.hex[:8]}", "p": private, "o": owner if private else None},
        )
        conn.execute(
            text(
                "INSERT INTO snapshots "
                "(id, repository_id, commit_sha, status, file_count, chunk_count, created_at) "
                "VALUES (:s, :r, :c, :st, 1, 0, now() - make_interval(secs => :age))"
            ),
            {"s": snap, "r": repo, "c": "a" * 40, "st": status, "age": age_days * 86400},
        )
        conn.execute(
            text(
                "INSERT INTO files (snapshot_id, path, size_bytes, sha256) "
                "VALUES (:s, 'a.py', 1, :h)"
            ),
            {"s": snap, "h": "0" * 64},
        )
    return snap


def snapshots_left(engine: Engine) -> set[uuid.UUID]:
    return {r[0] for r in rows(engine, "SELECT id FROM snapshots")}


# --- the purge itself --------------------------------------------------------------------


def test_only_expired_finished_private_snapshots_are_deleted(
    db_clean: Engine, alice: Persona, db_engine: Engine
) -> None:
    old_ready = seed(db_engine, alice.id, True, "ready", 31)
    old_failed = seed(db_engine, alice.id, True, "failed", 45)
    young = seed(db_engine, alice.id, True, "ready", 29)
    running = seed(db_engine, alice.id, True, "running", 90)  # never deleted under its job
    pending = seed(db_engine, alice.id, True, "pending", 90)
    public_old = seed(db_engine, alice.id, False, "ready", 400)  # public data is never expired

    settings = get_settings().model_copy(update={"private_retention_days": 30})
    with Session(db_engine) as db:
        assert retention.purge_expired(db, settings) == 2
        assert retention.purge_expired(db, settings) == 0  # idempotent
    assert snapshots_left(db_engine) == {young, running, pending, public_old}
    assert old_ready not in snapshots_left(db_engine) and old_failed not in snapshots_left(
        db_engine
    )
    assert count(db_engine, "files") == 4  # the deleted snapshots' files went with them
    assert count(db_engine, "repositories") == 6  # the repositories stay connected


def test_a_retention_of_zero_keeps_everything(
    db_clean: Engine, alice: Persona, db_engine: Engine
) -> None:
    seed(db_engine, alice.id, True, "ready", 5000)
    settings = get_settings().model_copy(update={"private_retention_days": 0})
    with Session(db_engine) as db:
        assert retention.purge_expired(db, settings) == 0
    assert count(db_engine, "snapshots") == 1
    private = Repository(owner="a", name="b", url="u", is_private=True)
    assert retention.retention_days(private, settings) is None


def test_the_boundary_is_the_configured_number_of_days(
    db_clean: Engine, alice: Persona, db_engine: Engine
) -> None:
    keep = seed(db_engine, alice.id, True, "ready", 6.9)
    drop = seed(db_engine, alice.id, True, "ready", 7.1)
    settings = get_settings().model_copy(update={"private_retention_days": 7})
    with Session(db_engine) as db:
        retention.purge_expired(db, settings)
    assert snapshots_left(db_engine) == {keep}
    assert drop not in snapshots_left(db_engine)


# --- enforced when a repository is used --------------------------------------------------


def indexed_private(c: TestClient, fake: FakeGitHub, engine: Engine) -> str:
    fake.files = dict(FILES)
    rid = private_repo(c, fake)
    save(c, "embedding")
    save(c, "chat")
    run_index(c, rid)
    assert count(engine, "chunks") > 0
    return rid


def test_expired_private_data_is_gone_before_anything_can_read_it(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,  # noqa: F811
) -> None:
    rid = indexed_private(db_client, fake_gh, db_engine)
    base = f"/api/repositories/{rid}"
    assert db_client.get(f"{base}/files").status_code == 200

    backdate(db_engine, get_settings().private_retention_days + 1)  # no scheduler has run
    r = db_client.get(f"{base}/files")
    assert r.status_code == 409  # nothing indexed any more, not a stale answer
    for table in ("snapshots", "files", "chunks", "symbols", "graph_edges", "code_refs"):
        assert count(db_engine, table) == 0, table
    assert db_client.get(f"{base}/status").json()["state"] == "not_indexed"
    assert db_client.get(base).status_code == 200  # still connected: it can be indexed again
    for url, body in [
        (f"{base}/search", {"query": "user login sessions"}),
        (f"{base}/chat", {"question": "how do users log in with a session"}),
    ]:
        assert db_client.post(url, json=body).status_code == 409


def test_a_private_repository_can_be_indexed_again_after_it_expires(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,  # noqa: F811
) -> None:
    rid = indexed_private(db_client, fake_gh, db_engine)
    backdate(db_engine, get_settings().private_retention_days + 1)
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "not_indexed"
    run_index(db_client, rid)
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "ready"
    assert count(db_engine, "chunks") > 0


def test_the_status_says_when_private_data_will_be_deleted(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,  # noqa: F811
) -> None:
    rid = indexed_private(db_client, fake_gh, db_engine)
    status = db_client.get(f"/api/repositories/{rid}/status").json()
    created = rows(db_engine, "SELECT created_at FROM snapshots")[0][0]
    expires = datetime.fromisoformat(status["expires_at"])
    assert expires - created == timedelta(days=get_settings().private_retention_days)

    override_settings(db_client, private_retention_days=0)
    assert db_client.get(f"/api/repositories/{rid}/status").json()["expires_at"] is None


def test_public_repositories_never_expire(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = db_client.post("/api/repositories", json={"url": "https://github.com/acme/open"}).json()[
        "id"
    ]
    assert db_client.post(f"/api/repositories/{rid}/index").status_code == 202
    backdate(db_engine, 10_000)
    status = db_client.get(f"/api/repositories/{rid}/status").json()
    assert status["state"] != "not_indexed" and status["expires_at"] is None
    assert count(db_engine, "snapshots") == 1


def test_using_one_repository_expires_only_that_repositorys_data(
    db_clean: Engine, db_client: TestClient, alice: Persona, db_engine: Engine, fake_gh: FakeGitHub
) -> None:
    mine = seed(db_engine, alice.id, True, "ready", 400)
    other = seed(db_engine, alice.id, True, "ready", 400)
    repo = rows(db_engine, "SELECT repository_id FROM snapshots WHERE id = :s", s=mine)[0][0]
    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO user_repositories (user_id, repository_id) VALUES (:u, :r)"),
            {"u": alice.id, "r": repo},
        )
    assert db_client.get(f"/api/repositories/{repo}/status").status_code == 200
    assert snapshots_left(db_engine) == {other}  # only the repository in use was cleaned up


# --- the scheduler's endpoint ------------------------------------------------------------


def with_token(c: TestClient, token: str | None) -> None:
    c.app.dependency_overrides[get_settings] = lambda: Settings().model_copy(  # type: ignore[attr-defined]
        update={"maintenance_token": SecretStr(token) if token else None}
    )


def test_the_purge_endpoint_does_not_exist_until_a_token_is_configured(
    anon_client: TestClient,
) -> None:
    with_token(anon_client, None)
    for headers in ({}, {"Authorization": "Bearer anything"}):
        r = anon_client.post("/api/internal/purge-expired", headers=headers)
        assert r.status_code == 404


def test_the_purge_endpoint_needs_the_right_token_and_no_session(
    db_clean: Engine, anon_client: TestClient, alice: Persona, db_engine: Engine
) -> None:
    with_token(anon_client, "s3cret-maintenance-token")
    old = seed(db_engine, alice.id, True, "ready", 400)
    url = "/api/internal/purge-expired"
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer "}):
        r = anon_client.post(url, headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "maintenance_unauthorized"
    assert old in snapshots_left(db_engine)  # nothing happened

    ok = anon_client.post(url, headers={"Authorization": "Bearer s3cret-maintenance-token"})
    assert ok.status_code == 200 and ok.json() == {
        "deleted_snapshots": 1,
        "deleted_audit_events": 0,
    }
    assert old not in snapshots_left(db_engine)
    again = anon_client.post(url, headers={"Authorization": "Bearer s3cret-maintenance-token"})
    assert again.json() == {"deleted_snapshots": 0, "deleted_audit_events": 0}


def test_a_signed_in_user_cannot_use_their_session_instead_of_the_token(
    db_client: TestClient, alice: Persona, db_engine: Engine
) -> None:
    with_token(db_client, "s3cret-maintenance-token")
    seed(db_engine, alice.id, True, "ready", 400)
    r = db_client.post("/api/internal/purge-expired")
    assert r.status_code == 401
    assert count(db_engine, "snapshots") == 1


def test_the_endpoint_is_rate_limited_per_address(
    anon_client: TestClient,
) -> None:
    with_token(anon_client, "s3cret-maintenance-token")
    codes = [anon_client.post("/api/internal/purge-expired").status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and 429 in codes[10:]


# --- deleting a repository leaves nothing ------------------------------------------------


def test_deleting_a_private_repository_removes_every_derived_row(
    db_client: TestClient,
    other_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    alice: Persona,
) -> None:
    rid = indexed_private(db_client, fake_gh, db_engine)
    db_client.post(
        f"/api/repositories/{rid}/chat", json={"question": "how do users log in with a session"}
    )
    assert count(db_engine, "chat_usage") > 0

    # Another user's repository with data must be untouched.
    other_public = other_client.post(
        "/api/repositories", json={"url": "https://github.com/acme/open"}
    ).json()["id"]
    other_client.post(f"/api/repositories/{other_public}/index")
    keep = {t: count(db_engine, t) for t in ("repositories", "snapshots")}
    snapshot_ids = [
        r[0]
        for r in rows(
            db_engine, "SELECT id FROM snapshots WHERE repository_id = :r", r=uuid.UUID(rid)
        )
    ]
    assert snapshot_ids

    assert db_client.delete(f"/api/repositories/{rid}").status_code == 204

    # Every table that hangs off a repository or a snapshot, found from the schema itself so a
    # table added later cannot be forgotten.
    by_snapshot = _tables_with(db_engine, "snapshot_id")
    # The audit log is meant to outlive what it describes (it holds no content).
    by_repository = _tables_with(db_engine, "repository_id") - {"snapshots", "audit_events"}
    assert {"files", "chunks", "symbols", "graph_edges", "index_jobs"} <= by_snapshot
    assert {"chat_usage", "user_repositories"} <= by_repository
    for table in sorted(by_snapshot):
        n = rows(
            db_engine,
            f"SELECT count(*) FROM {table} WHERE snapshot_id = ANY(:ids)",
            ids=snapshot_ids,
        )[0][0]
        assert n == 0, table
    for table in sorted(by_repository):
        n = rows(
            db_engine, f"SELECT count(*) FROM {table} WHERE repository_id = :r", r=uuid.UUID(rid)
        )[0][0]
        assert n == 0, table
    assert count(db_engine, "repositories") == keep["repositories"] - 1
    assert count(db_engine, "snapshots") == keep["snapshots"] - 1  # the other user's is untouched
    # The user's provider settings belong to the user, not to the repository.
    assert count(db_engine, "user_providers") == 2
    assert count(db_engine, "users") >= 1


def _tables_with(engine: Engine, column: str) -> set[str]:
    return {
        r[0]
        for r in rows(
            engine,
            "SELECT DISTINCT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = :c",
            c=column,
        )
    }
