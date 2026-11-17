import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.config import Settings, get_settings
from app.core.errors import GithubRateLimited, GithubUnavailable, RepoNotFound, RepoTooLarge
from app.db.models import File
from app.services.indexing import fetch_phase, runner
from tests.helpers import DEFAULT_KEPT, FakeGitHub, make_tarball

pytestmark = pytest.mark.db


def register(c: TestClient, url: str = "https://github.com/o/r") -> str:
    r = c.post("/api/repositories", json={"url": url})
    assert r.status_code in (200, 201), r.text
    return str(r.json()["id"])


def start(c: TestClient, rid: str, **body: Any) -> Any:
    return c.post(f"/api/repositories/{rid}/index", json=body or None)


def tick(c: TestClient, rid: str) -> dict[str, Any]:
    r = c.post(f"/api/repositories/{rid}/index/tick")
    assert r.status_code == 200, r.text
    return dict(r.json())


def status(c: TestClient, rid: str) -> dict[str, Any]:
    r = c.get(f"/api/repositories/{rid}/status")
    assert r.status_code == 200, r.text
    return dict(r.json())


def expire_lease(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE index_jobs SET lease_until = now() - interval '1 second'"))


def file_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT path, language, skipped_reason, content FROM files ORDER BY path")
            )
        )


def override_settings(c: TestClient, **kw: Any) -> None:
    base = get_settings()
    c.app.dependency_overrides[get_settings] = lambda: base.model_copy(update=kw)  # type: ignore[attr-defined]


# --- starting ---------------------------------------------------------------------------


def test_status_before_any_index(db_client: TestClient) -> None:
    rid = register(db_client)
    s = status(db_client, rid)
    assert s["state"] == "not_indexed"
    assert s["snapshot_id"] is None


def test_start_creates_snapshot_and_job(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    rid = register(db_client)
    r = start(db_client, rid)
    assert r.status_code == 202
    body = r.json()
    assert body["created"] is True
    assert body["status"]["state"] == "pending"
    assert body["status"]["phase"] == "fetch"
    assert body["status"]["commit_sha"] == fake_gh.sha
    assert db_client.get(f"/api/repositories/{rid}").json()["default_branch"] == "main"


def test_same_commit_is_not_ingested_twice(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client)
    first = start(db_client, rid)
    again = start(db_client, rid)
    assert first.status_code == 202
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["status"]["snapshot_id"] == first.json()["status"]["snapshot_id"]
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM snapshots")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM index_jobs")).scalar() == 1


def test_ref_is_validated_and_passed_through(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    rid = register(db_client)
    for bad in ["../x", "a b", "x;rm", "/abs", "trail/", ""]:
        r = start(db_client, rid, ref=bad)
        assert r.status_code == 422, bad
    assert start(db_client, rid, ref="feature/x-1.2").status_code == 202
    assert fake_gh.last_ref == "feature/x-1.2"


def test_different_commit_while_one_is_active_is_rejected(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = register(db_client)
    assert start(db_client, rid).status_code == 202
    fake_gh.sha = "c" * 40
    r = start(db_client, rid)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "job_in_progress"


def test_unknown_repository_and_github_errors(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    assert start(db_client, str(uuid.uuid4())).status_code == 404
    rid = register(db_client)
    fake_gh.resolve_error = RepoNotFound("nope")
    r = start(db_client, rid)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "repo_not_found"
    fake_gh.resolve_error = GithubRateLimited("slow", details={"retry_after_seconds": 30})
    r = start(db_client, rid)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "github_rate_limited"


def test_daily_quota_counts_new_snapshots_only(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    override_settings(db_client, ingestions_per_client_per_day=2)
    rid = register(db_client)
    shas = ["1" * 40, "2" * 40, "3" * 40]
    for sha in shas[:2]:
        fake_gh.sha = sha
        assert start(db_client, rid).status_code == 202
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE snapshots SET status = 'ready'"))
    fake_gh.sha = shas[2]
    r = start(db_client, rid)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota_exceeded"
    # An already-indexed commit is not new work, so it is still served.
    fake_gh.sha = shas[0]
    assert start(db_client, rid).status_code == 200


def test_index_endpoint_is_rate_limited(db_client: TestClient) -> None:
    rid = register(db_client)
    codes = [start(db_client, rid).status_code for _ in range(11)]
    assert codes[:10].count(429) == 0
    assert codes[10] == 429
    r = start(db_client, rid)
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1


# --- ticking ----------------------------------------------------------------------------


def test_tick_without_job_is_conflict(db_client: TestClient) -> None:
    rid = register(db_client)
    r = db_client.post(f"/api/repositories/{rid}/index/tick")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_active_job"


def test_tick_fetches_source_and_reaches_parse(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client)
    start(db_client, rid)
    t = tick(db_client, rid)
    assert t["ran"] is True
    s = t["status"]
    assert s["state"] == "running"
    assert s["phase"] == "parse"
    assert s["error"] is None and s["attempts"] == 0
    assert s["progress"]["files_kept"] == len(DEFAULT_KEPT)
    assert s["progress"]["skipped"] == {
        "secret_file": 1,
        "too_large": 1,
        "unsupported": 2,
        "vendored": 1,
    }

    rows = {r.path: r for r in file_rows(db_engine)}
    assert {p for p, r in rows.items() if r.skipped_reason is None} == DEFAULT_KEPT
    assert rows["src/app.py"].language == "python"
    assert "def main" in rows["src/app.py"].content
    assert rows["src/big.py"].skipped_reason == "too_large" and rows["src/big.py"].content is None
    assert ".env" not in rows and "node_modules/lib/index.js" not in rows
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT file_count FROM snapshots")).scalar() == len(DEFAULT_KEPT)


def test_unimplemented_phase_is_left_untouched(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.indexing.runner import HANDLERS

    monkeypatch.delitem(HANDLERS, "graph")  # simulate a phase that has no handler
    rid = register(db_client)
    start(db_client, rid)
    tick(db_client, rid)  # fetch
    tick(db_client, rid)  # parse
    tick(db_client, rid)  # embed
    t = tick(db_client, rid)
    assert t["ran"] is False
    assert t["reason"] == "phase_not_implemented:graph"
    assert t["status"]["phase"] == "graph" and t["status"]["state"] == "running"
    # The lease is released, so it does not look "busy" to later phases.
    assert tick(db_client, rid)["reason"] == "phase_not_implemented:graph"


def test_the_graph_phase_finishes_the_job(db_client: TestClient) -> None:
    rid = register(db_client)
    start(db_client, rid)
    for _ in range(3):
        tick(db_client, rid)  # fetch, parse, embed
    t = tick(db_client, rid)  # graph
    assert t["ran"] is True
    assert t["status"]["state"] == "ready" and t["status"]["phase"] == "done"
    assert t["status"]["graph"]["done"] is True


# --- failure and recovery ---------------------------------------------------------------


def test_transient_failure_backs_off_then_recovers(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client)
    start(db_client, rid)
    fake_gh.fail_next = [GithubUnavailable("GitHub is down.")]

    t = tick(db_client, rid)
    assert t["ran"] is True and t["reason"] == "failed:github_unavailable"
    s = t["status"]
    assert s["state"] == "running" and s["phase"] == "fetch"
    assert s["attempts"] == 1
    assert s["error"] == {"code": "github_unavailable", "message": "GitHub is down."}
    assert s["retry_at"] is not None and s["can_resume"] is False
    assert file_rows(db_engine) == []

    # Backing off: another tick does nothing and does not call GitHub.
    calls = fake_gh.tarball_calls
    t = tick(db_client, rid)
    assert t["ran"] is False
    assert fake_gh.tarball_calls == calls

    expire_lease(db_engine)
    t = tick(db_client, rid)
    assert t["ran"] is True
    s = t["status"]
    assert s["phase"] == "parse" and s["attempts"] == 0 and s["error"] is None
    assert s["retry_at"] is None
    assert len(file_rows(db_engine)) == len(DEFAULT_KEPT) + 1  # + big.py (recorded skip)


def test_job_fails_after_max_attempts_and_can_be_restarted(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    override_settings(db_client, max_attempts=2)
    rid = register(db_client)
    start(db_client, rid)
    fake_gh.fail_next = [GithubUnavailable("down"), GithubUnavailable("still down")]
    tick(db_client, rid)
    expire_lease(db_engine)
    s = tick(db_client, rid)["status"]

    assert s["state"] == "failed" and s["phase"] == "failed"
    assert s["can_resume"] is True and s["attempts"] == 2
    assert s["error"]["code"] == "github_unavailable"
    assert s["retry_at"] is None
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT error FROM snapshots")).scalar() == "still down"
    assert tick_status_code(db_client, rid) == 409  # nothing active to tick

    r = start(db_client, rid)  # restart the failed job, same commit
    assert r.status_code == 200 and r.json()["created"] is False
    assert r.json()["status"]["state"] == "pending"
    assert r.json()["status"]["phase"] == "fetch"
    assert r.json()["status"]["attempts"] == 0 and r.json()["status"]["error"] is None
    assert tick(db_client, rid)["status"]["phase"] == "parse"


def tick_status_code(c: TestClient, rid: str) -> int:
    return int(c.post(f"/api/repositories/{rid}/index/tick").status_code)


def test_non_retryable_failure_fails_immediately(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = register(db_client)
    start(db_client, rid)
    fake_gh.fail_next = [RepoTooLarge("Repository has more than 2000 source files.")]
    s = tick(db_client, rid)["status"]
    assert s["state"] == "failed" and s["attempts"] == 1
    assert s["error"]["code"] == "repo_too_large"


def test_upstream_rate_limit_waits_without_burning_attempts(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = register(db_client)
    start(db_client, rid)
    fake_gh.fail_next = [GithubRateLimited("limit", details={"retry_after_seconds": 120})]
    s = tick(db_client, rid)["status"]
    assert s["state"] == "running" and s["attempts"] == 0
    assert s["error"]["code"] == "github_rate_limited"
    assert s["retry_at"] is not None


def test_failed_fetch_leaves_no_partial_files(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Many files, flushed one row at a time, then the connection dies halfway through.
    fake_gh.files = {f"pkg/m{i}.py": f"value_{i} = {i}\n" * 400 for i in range(40)}
    monkeypatch.setattr(fetch_phase, "BATCH_ROWS", 1)
    inserts = {"n": 0}
    from sqlalchemy import insert as real_insert

    def counting_insert(*a: Any, **k: Any) -> Any:
        inserts["n"] += 1
        return real_insert(*a, **k)

    monkeypatch.setattr("app.services.indexing.fetch_phase.insert", counting_insert)
    fake_gh.fail_after_fraction = 0.5

    rid = register(db_client)
    start(db_client, rid)
    s = tick(db_client, rid)["status"]

    assert inserts["n"] > 0  # rows really were written before the failure...
    assert file_rows(db_engine) == []  # ...and rolled back with it
    assert s["error"]["code"] == "github_unavailable"
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT file_count FROM snapshots")).scalar() == 0

    # The next attempt starts clean and succeeds.
    fake_gh.fail_after_fraction = None
    expire_lease(db_engine)
    s = tick(db_client, rid)["status"]
    assert s["phase"] == "parse"
    assert len(file_rows(db_engine)) == 40


def test_refetch_replaces_existing_files(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = register(db_client)
    start(db_client, rid)
    tick(db_client, rid)
    before = len(file_rows(db_engine))
    # Force the fetch phase to run again against the same snapshot.
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE index_jobs SET phase = 'fetch'"))
    tick(db_client, rid)
    assert len(file_rows(db_engine)) == before


# --- leases -----------------------------------------------------------------------------


def test_active_lease_blocks_other_ticks_until_it_expires(
    db_client: TestClient, db_engine: Engine
) -> None:
    rid = register(db_client)
    start(db_client, rid)
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE index_jobs SET lease_until = now() + interval '1 hour'"))
    t = tick(db_client, rid)
    assert t["ran"] is False and t["reason"] == "busy_or_backing_off_or_finished"
    assert t["status"]["phase"] == "fetch" and file_rows(db_engine) == []

    expire_lease(db_engine)  # the other worker "died"
    assert tick(db_client, rid)["status"]["phase"] == "parse"


def test_stale_tick_cannot_overwrite_a_newer_lease_holder(
    db_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_handler(ctx: runner.PhaseContext) -> runner.PhaseResult:
        ctx.db.add(File(snapshot_id=ctx.snapshot.id, path="x.py", size_bytes=1, sha256="0" * 64))
        ctx.db.flush()
        # While we work, our lease "expires" and another worker takes the job over.
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE index_jobs SET lease_until = now() + interval '1 hour'"))
        return runner.PhaseResult(next_phase="parse")

    monkeypatch.setitem(runner.HANDLERS, "fetch", slow_handler)
    rid = register(db_client)
    start(db_client, rid)
    t = tick(db_client, rid)
    assert t["ran"] is False and t["reason"] == "lease_lost"
    assert file_rows(db_engine) == []  # the stale tick's writes were rolled back
    assert t["status"]["phase"] == "fetch"


def test_multi_step_phase_resumes_from_committed_cursor(
    db_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: FakeGitHub,
) -> None:
    """The generic contract later phases (parse, embed) rely on: progress is committed per
    tick, a crash loses only the in-flight step, and the next tick resumes where it left off."""
    crash = {"on_call": 2, "calls": 0}

    def handler(ctx: runner.PhaseContext) -> runner.PhaseResult:
        crash["calls"] += 1
        if crash["calls"] == crash["on_call"]:
            raise RuntimeError("worker crashed mid-step")
        done = int((ctx.job.cursor or {}).get("items_done", 0))
        done = min(done + 2, 5)
        return runner.PhaseResult(
            next_phase="embed" if done == 5 else None, cursor={"items_done": done}
        )

    monkeypatch.setitem(runner.HANDLERS, "parse", handler)
    rid = register(db_client)
    start(db_client, rid)
    tick(db_client, rid)  # fetch -> parse

    def cursor() -> dict[str, Any]:
        with db_engine.connect() as conn:
            return dict(conn.execute(text("SELECT cursor FROM index_jobs")).scalar_one())

    tick(db_client, rid)  # parse step 1
    assert cursor()["items_done"] == 2
    s = tick(db_client, rid)["status"]  # parse step 2 crashes
    assert s["error"]["code"] == "internal_error" and s["attempts"] == 1
    assert "worker crashed" not in str(s)  # internals are not leaked
    assert cursor()["items_done"] == 2  # committed progress survived the crash

    expire_lease(db_engine)
    tick(db_client, rid)  # resumes from 2, not from 0
    assert cursor()["items_done"] == 4
    s = tick(db_client, rid)["status"]
    assert cursor()["items_done"] == 5
    assert s["phase"] == "embed" and s["attempts"] == 0 and s["error"] is None


def test_completing_the_last_phase_marks_snapshot_ready(
    db_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(runner.HANDLERS, "parse", lambda ctx: runner.PhaseResult(next_phase="done"))
    rid = register(db_client)
    start(db_client, rid)
    tick(db_client, rid)
    s = tick(db_client, rid)["status"]
    assert s["state"] == "ready" and s["phase"] == "done"
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT indexed_at IS NOT NULL FROM snapshots")).scalar() is True
    # A finished job is no longer "active".
    assert tick_status_code(db_client, rid) == 409
    # And asking to index the same commit again just reports it.
    again = start(db_client, rid)
    assert again.status_code == 200 and again.json()["status"]["state"] == "ready"


def test_tarball_with_hostile_members_cannot_escape(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    import io
    import tarfile

    def extras(tf: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("owner-repo-abc123/../../etc/evil.py")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"x = 1"))

    fake_gh.files = {"ok.py": "x = 1\n"}
    fake_gh.open_tarball = lambda *a, **k: _ctx(make_tarball(fake_gh.files, extras=extras))  # type: ignore[method-assign]
    rid = register(db_client)
    start(db_client, rid)
    s = tick(db_client, rid)["status"]
    assert [r.path for r in file_rows(db_engine)] == ["ok.py"]
    assert s["progress"]["skipped"]["unsafe_path"] == 1


def _ctx(data: bytes) -> Any:
    import io
    from contextlib import contextmanager

    @contextmanager
    def cm() -> Any:
        yield io.BytesIO(data)

    return cm()


def test_settings_override_helper_keeps_other_settings(db_client: TestClient) -> None:
    override_settings(db_client, max_attempts=7)
    s = db_client.app.dependency_overrides[get_settings]()  # type: ignore[attr-defined]
    assert isinstance(s, Settings) and s.max_attempts == 7 and s.lease_seconds == 90
