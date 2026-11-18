"""Indexing a commit again from scratch, and disconnecting a repository."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.config import get_settings
from tests.helpers import FakeGitHub
from tests.integration.test_indexing_api import override_settings, register, start, tick

pytestmark = pytest.mark.db

URL = "https://github.com/o/shared"


def finish(c: TestClient, rid: str) -> dict[str, Any]:
    for _ in range(60):
        status = tick(c, rid)["status"]
        if status["state"] in ("ready", "failed"):
            return dict(status)
    raise AssertionError("never finished")


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def snapshot_ids(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [str(r[0]) for r in conn.execute(text("SELECT id FROM snapshots ORDER BY id"))]


# --- re-index ---------------------------------------------------------------------------


def test_force_replaces_a_finished_snapshot_of_the_same_commit(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client)
    assert start(db_client, rid).status_code == 202
    assert finish(db_client, rid)["state"] == "ready"
    first = snapshot_ids(db_engine)
    files_before = count(db_engine, "files")
    assert files_before > 0

    # a plain request reuses it; a forced one starts over
    assert start(db_client, rid).json()["created"] is False
    forced = start(db_client, rid, force=True)
    assert forced.status_code == 202 and forced.json()["created"] is True
    assert forced.json()["status"]["state"] in ("pending", "running")
    assert snapshot_ids(db_engine) != first and count(db_engine, "snapshots") == 1
    assert count(db_engine, "files") == 0  # the old rows went with the old snapshot

    assert finish(db_client, rid)["state"] == "ready"
    assert count(db_engine, "files") == files_before  # same commit, same content again


def test_force_on_a_running_snapshot_changes_nothing(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client)
    first = start(db_client, rid).json()["status"]["snapshot_id"]
    again = start(db_client, rid, force=True)
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["status"]["snapshot_id"] == first
    assert count(db_engine, "snapshots") == 1


def test_force_that_hits_the_daily_quota_keeps_the_existing_data(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    override_settings(db_client, ingestions_per_client_per_day=1)
    rid = register(db_client)
    start(db_client, rid)
    assert finish(db_client, rid)["state"] == "ready"
    before = snapshot_ids(db_engine)
    r = start(db_client, rid, force=True)
    assert r.status_code == 429
    assert snapshot_ids(db_engine) == before  # nothing was deleted
    assert count(db_engine, "files") > 0
    assert get_settings().ingestions_per_client_per_day != 1  # the override was local


def test_force_must_be_a_boolean(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    rid = register(db_client)
    assert start(db_client, rid, force="yes please").status_code == 422


# --- disconnect -------------------------------------------------------------------------


def test_disconnecting_the_only_connection_deletes_everything(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client, URL)
    start(db_client, rid)
    assert finish(db_client, rid)["state"] == "ready"
    db_client.post(f"/api/repositories/{rid}/index")  # idempotent, nothing new
    assert count(db_engine, "chunks") > 0 and count(db_engine, "symbols") > 0

    r = db_client.delete(f"/api/repositories/{rid}")
    assert r.status_code == 204 and r.content == b""
    for table in (
        "repositories",
        "user_repositories",
        "snapshots",
        "index_jobs",
        "files",
        "chunks",
        "symbols",
        "graph_edges",
        "chat_usage",
    ):
        assert count(db_engine, table) == 0, table
    assert count(db_engine, "users") == 1  # the account itself stays
    assert db_client.get(f"/api/repositories/{rid}").status_code == 404
    assert db_client.get("/api/repositories").json()["total"] == 0


def test_disconnecting_while_someone_else_is_connected_keeps_the_shared_data(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client, URL)
    assert other_client.post("/api/repositories", json={"url": URL}).status_code == 201
    start(db_client, rid)
    assert finish(db_client, rid)["state"] == "ready"
    files = count(db_engine, "files")

    assert db_client.delete(f"/api/repositories/{rid}").status_code == 204
    assert db_client.get(f"/api/repositories/{rid}").status_code == 404  # gone for alice
    assert other_client.get(f"/api/repositories/{rid}").status_code == 200  # not for bob
    assert count(db_engine, "files") == files and count(db_engine, "snapshots") == 1
    assert count(db_engine, "user_repositories") == 1

    assert other_client.delete(f"/api/repositories/{rid}").status_code == 204
    assert count(db_engine, "repositories") == 0 and count(db_engine, "files") == 0


def test_disconnecting_a_repository_that_is_not_yours_is_a_404_and_changes_nothing(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client, URL)
    r = other_client.delete(f"/api/repositories/{rid}")
    assert r.status_code == 404
    assert count(db_engine, "repositories") == 1 and count(db_engine, "user_repositories") == 1


def test_disconnect_needs_sign_in_and_a_same_origin_request(
    db_client: TestClient, anon_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client, URL)
    assert anon_client.delete(f"/api/repositories/{rid}").status_code == 401
    cross = db_client.delete(f"/api/repositories/{rid}", headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403
    assert count(db_engine, "repositories") == 1


def test_a_repository_can_be_connected_and_indexed_again_after_disconnecting(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client, URL)
    start(db_client, rid)
    assert finish(db_client, rid)["state"] == "ready"
    assert db_client.delete(f"/api/repositories/{rid}").status_code == 204
    again = register(db_client, URL)
    assert start(db_client, again).status_code == 202
    assert finish(db_client, again)["state"] == "ready"
