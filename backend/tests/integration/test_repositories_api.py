import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.db


def test_ready_reports_migration_head(db_client: TestClient) -> None:
    r = db_client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["migration"]


def test_ready_fails_when_migrations_behind(db_client: TestClient, db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'stale'"))
    try:
        r = db_client.get("/ready")
        assert r.status_code == 503
        err = r.json()["error"]
        assert err["code"] == "not_ready"
        assert err["details"]["check"] == "migrations"
    finally:
        from app.api.health import _migration_head

        with db_engine.begin() as conn:
            conn.execute(
                text("UPDATE alembic_version SET version_num = :v"), {"v": _migration_head()}
            )


def test_create_and_get_repository(db_client: TestClient) -> None:
    r = db_client.post("/api/repositories", json={"url": "https://github.com/pallets/flask.git"})
    assert r.status_code == 201
    repo = r.json()
    assert repo["owner"] == "pallets"
    assert repo["name"] == "flask"
    assert repo["url"] == "https://github.com/pallets/flask"
    assert repo["default_branch"] is None

    got = db_client.get(f"/api/repositories/{repo['id']}")
    assert got.status_code == 200
    assert got.json() == repo


def test_create_is_idempotent_and_case_insensitive(db_client: TestClient) -> None:
    first = db_client.post("/api/repositories", json={"url": "https://github.com/pallets/flask"})
    again = db_client.post("/api/repositories", json={"url": "https://github.com/Pallets/FLASK/"})
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert db_client.get("/api/repositories").json()["total"] == 1


def test_list_pagination(db_client: TestClient) -> None:
    for name in ["a", "b", "c"]:
        assert (
            db_client.post("/api/repositories", json={"url": f"https://github.com/o/{name}"})
        ).status_code == 201
    page = db_client.get("/api/repositories", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    rest = db_client.get("/api/repositories", params={"limit": 2, "offset": 2}).json()
    assert len(rest["items"]) == 1
    seen = {i["name"] for i in page["items"]} | {i["name"] for i in rest["items"]}
    assert seen == {"a", "b", "c"}


def test_list_rejects_bad_paging(db_client: TestClient) -> None:
    assert db_client.get("/api/repositories", params={"limit": 0}).status_code == 422
    assert db_client.get("/api/repositories", params={"limit": 101}).status_code == 422
    assert db_client.get("/api/repositories", params={"offset": -1}).status_code == 422


def test_get_unknown_repository_is_404(db_client: TestClient) -> None:
    r = db_client.get(f"/api/repositories/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "repo_not_found"


def test_schema_has_all_tables_and_pgvector(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        ext = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
    assert {
        "repositories",
        "snapshots",
        "index_jobs",
        "files",
        "symbols",
        "chunks",
        "graph_edges",
        "chat_usage",
    } <= tables
    assert ext == 1


def test_duplicate_snapshot_commit_is_rejected_by_database(
    db_client: TestClient, db_engine: Engine
) -> None:
    repo_id = db_client.post("/api/repositories", json={"url": "https://github.com/o/r"}).json()[
        "id"
    ]
    sha = "a" * 40
    insert = text(
        "INSERT INTO snapshots (id, repository_id, commit_sha, status, file_count, chunk_count) "
        "VALUES (:id, :r, :sha, 'pending', 0, 0)"
    )
    with db_engine.begin() as conn:
        conn.execute(insert, {"id": uuid.uuid4(), "r": repo_id, "sha": sha})
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        conn.execute(insert, {"id": uuid.uuid4(), "r": repo_id, "sha": sha})
