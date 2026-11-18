"""The background worker advances jobs without a browser, alongside other workers and tabs."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import EmbeddingUnavailable
from app.services.indexing import runner
from app.worker import Worker
from tests.helpers import FakeGitHub
from tests.integration.test_parse_phase import rows
from tests.integration.test_private_providers import (  # noqa: F401  (world is a fixture)
    CHAT_KEY,
    EMB_KEY,
    headers,
    save,
    world,  # noqa: F401  (a fixture: imported so pytest finds it)
)
from tests.integration.test_private_repos import private_repo
from tests.integration.test_search import FILES

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("world")]


@pytest.fixture
def fakes(request: pytest.FixtureRequest) -> Any:
    """The fake providers from the private-provider tests (the platform's forbidden)."""
    return request.getfixturevalue("world")


def make_worker(fake: FakeGitHub, **update: Any) -> Worker:
    settings = get_settings().model_copy(update={"worker_skip_seconds": 60.0, **update})
    return Worker(settings, github_for=lambda db, user_id: fake)


def drain(w: Worker, limit: int = 200) -> int:
    return w.run(threading.Event(), once=True, max_steps=limit)


def states(engine: Engine) -> dict[str, str]:
    return {r[0]: r[1] for r in rows(engine, "SELECT commit_sha, status FROM snapshots")}


def phase_of(engine: Engine, rid: str) -> str:
    return str(
        rows(
            engine,
            "SELECT j.phase FROM index_jobs j JOIN snapshots s ON s.id = j.snapshot_id "
            "WHERE s.repository_id = :r",
            r=uuid.UUID(rid),
        )[0][0]
    )


def start_public(c: TestClient, fake: FakeGitHub, name: str = "open") -> str:
    fake.files = dict(FILES)
    rid = c.post("/api/repositories", json={"url": f"https://github.com/acme/{name}"}).json()["id"]
    assert c.post(f"/api/repositories/{rid}/index").status_code == 202
    return str(rid)


def start_private(c: TestClient, fake: FakeGitHub, name: str = "secret") -> str:
    fake.files = dict(FILES)
    rid = private_repo(c, fake, f"https://github.com/acme/{name}")
    save(c, "embedding")
    save(c, "chat")
    assert c.post(f"/api/repositories/{rid}/index").status_code == 202
    return rid


# --- a public job -------------------------------------------------------------------------


def test_the_worker_finishes_a_job_with_no_browser_driving_it(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    _, platform = fakes
    rid = start_public(db_client, fake_gh)
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "pending"

    steps = drain(make_worker(fake_gh))
    assert steps >= 4  # fetch, parse, embed, graph
    status = db_client.get(f"/api/repositories/{rid}/status").json()
    assert status["state"] == "ready" and status["embed"]["done"] is True
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")[0][0] > 0
    assert "embed_documents" in platform.used  # a public repository uses the platform's embedder
    assert drain(make_worker(fake_gh)) == 0  # nothing left


def test_a_worker_and_a_browser_tick_can_share_a_job(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = start_public(db_client, fake_gh)
    w = make_worker(fake_gh)
    with Session(db_engine) as db:
        assert w.run_once(db) == "ran"  # the worker does a step
    assert (
        db_client.post(f"/api/repositories/{rid}/index/tick").json()["ran"] is True
    )  # a tab does one
    with Session(db_engine) as db:
        assert w.run_once(db) == "ran"
    drain(w)
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "ready"


# --- a private job ------------------------------------------------------------------------


def test_a_private_job_is_embedded_with_the_owners_stored_provider_only(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    w_, platform = fakes
    rid = start_private(db_client, fake_gh)
    drain(make_worker(fake_gh))
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "ready"
    models = {r[0] for r in rows(db_engine, "SELECT DISTINCT embedding_model FROM chunks")}
    assert models == {"user-embedder-v1"}
    assert ("embedding", EMB_KEY) in w_.keys  # the stored key, decrypted for the worker
    assert platform.used == []  # not once
    assert all(e.closed >= 1 for e in w_.embedders)  # clients are closed after each step


def test_a_browser_held_key_leaves_embedding_to_the_browser(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    w_, platform = fakes
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh, "https://github.com/acme/held")
    save(db_client, "embedding", mode="browser")
    save(db_client, "chat", mode="browser")
    assert db_client.post(f"/api/repositories/{rid}/index", headers=headers()).status_code == 202

    keys_before = list(w_.keys)  # starting the index itself checked both providers
    worker = make_worker(fake_gh)
    drain(worker)
    with Session(db_engine) as db:
        # ...and it said why, by the intended route (not by failing to read a key)
        assert worker.run_once(db) in ("idle", "skipped:needs_browser_or_provider")
    # It did the steps that send nothing to a provider, then stopped at embedding.
    assert phase_of(db_engine, rid) == "embed"
    assert w_.keys == keys_before and platform.used == []  # the worker never built a client
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "running"

    # The browser, which has the key, finishes it.
    for _ in range(10):
        r = db_client.post(f"/api/repositories/{rid}/index/tick", headers=headers())
        assert r.status_code == 200
        if r.json()["status"]["state"] == "ready":
            break
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "ready"
    assert ("embedding", EMB_KEY) in w_.keys and platform.used == []


def test_the_worker_can_use_a_stored_key_and_cannot_use_a_browser_held_one(
    db_client: TestClient, fake_gh: FakeGitHub, fakes: Any, db_engine: Engine, alice: Any
) -> None:
    w = make_worker(fake_gh)
    with Session(db_engine) as db:
        assert w._owner_embedder(db, alice.id) is None  # nothing set up
    save(db_client, "embedding")
    with Session(db_engine) as db:
        assert w._owner_embedder(db, alice.id) is not None  # a stored key is usable
    save(db_client, "embedding", mode="browser")
    with Session(db_engine) as db:
        assert w._owner_embedder(db, alice.id) is None  # the key is not on the server


def test_a_private_job_runs_as_its_owner_even_without_a_recorded_requester(
    db_client: TestClient, fake_gh: FakeGitHub, fakes: Any, db_engine: Engine
) -> None:
    rid = start_private(db_client, fake_gh)
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE snapshots SET requested_by = NULL"))  # as after an account change
    drain(make_worker(fake_gh))
    assert db_client.get(f"/api/repositories/{rid}/status").json()["state"] == "ready"


def test_a_public_job_with_no_recorded_requester_is_left_alone(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    start_public(db_client, fake_gh)
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE snapshots SET requested_by = NULL"))
    w = make_worker(fake_gh)
    with Session(db_engine) as db:
        assert w.run_once(db) == "skipped:no_user"  # no one's token to use


def test_a_private_job_without_an_embedding_provider_is_never_embedded_by_the_platform(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    _, platform = fakes
    rid = start_private(db_client, fake_gh)
    db_client.delete("/api/providers/embedding")  # removed after indexing started
    drain(make_worker(fake_gh))
    assert phase_of(db_engine, rid) == "embed"  # stopped there, untouched
    assert platform.used == []
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")[0][0] == 0


def test_a_private_job_whose_owner_lost_access_does_nothing(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    rid = start_private(db_client, fake_gh)
    fake_gh.hidden_names.add("acme/secret")
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE repositories SET access_checked_at = NULL"))
    before = (fake_gh.tarball_calls, fake_gh.resolve_calls)  # the API's own index start
    drain(make_worker(fake_gh))
    assert phase_of(db_engine, rid) == "fetch"  # not one step ran
    assert (fake_gh.tarball_calls, fake_gh.resolve_calls) == before


def test_the_embed_phase_itself_refuses_to_use_the_platform_for_a_private_repository(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    """Whoever calls the runner, a private repository is never embedded without its owner's
    embedder, even if the caller forgot to pass one."""
    _, platform = fakes
    rid = start_private(db_client, fake_gh)
    drain_until = make_worker(fake_gh)
    with Session(db_engine) as db:
        for _ in range(4):  # fetch, parse
            drain_until.run_once(db)
            if phase_of(db_engine, rid) == "embed":
                break
        job_id = rows(db_engine, "SELECT id FROM index_jobs")[0][0]
        result = runner.tick(db, job_id, fake_gh, get_settings(), embedder=None)
    assert result.reason == "failed:providers_required"
    assert platform.used == []
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")[0][0] == 0


# --- picking work -------------------------------------------------------------------------


def test_the_least_recently_worked_job_goes_first_and_unavailable_ones_are_skipped(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    ids = [start_public(db_client, fake_gh, f"r{i}") for i in range(4)]
    with db_engine.begin() as conn:
        # job 0 oldest ... job 3 newest; then make job 0 leased, job 1 finished
        for i, rid in enumerate(ids):
            conn.execute(
                text(
                    "UPDATE index_jobs SET updated_at = now() - make_interval(secs => :age) "
                    "WHERE snapshot_id IN (SELECT id FROM snapshots WHERE repository_id = :r)"
                ),
                {"age": 1000 - i * 100, "r": uuid.UUID(rid)},
            )
        conn.execute(
            text(
                "UPDATE index_jobs SET lease_until = now() + interval '1 hour' "
                "WHERE snapshot_id IN (SELECT id FROM snapshots WHERE repository_id = :r)"
            ),
            {"r": uuid.UUID(ids[0])},
        )
        conn.execute(
            text(
                "UPDATE index_jobs SET phase = 'done' WHERE snapshot_id IN "
                "(SELECT id FROM snapshots WHERE repository_id = :r)"
            ),
            {"r": uuid.UUID(ids[1])},
        )
    w = make_worker(fake_gh)
    with Session(db_engine) as db:
        picked = w.next_job(db)
        assert picked is not None and picked[2].id == uuid.UUID(ids[2])  # oldest that is free


def test_a_backing_off_job_is_not_retried_in_a_hot_loop(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    _, platform = fakes
    rid = start_public(db_client, fake_gh)
    w = make_worker(fake_gh)
    drain(w, limit=2)  # fetch and parse
    platform.embedder.error = EmbeddingUnavailable("busy")
    platform.embedder.fail_on_call = platform.embedder.calls + 1
    with Session(db_engine) as db:
        assert w.run_once(db) == "ran"  # the embed step fails and the job backs off
        assert w.run_once(db) == "idle"  # its lease is in the future: not picked again
    attempts = rows(db_engine, "SELECT attempts FROM index_jobs")[0][0]
    assert attempts == 1 and db_client.get(f"/api/repositories/{rid}/status").status_code == 200


def test_a_job_left_alone_is_looked_at_again_after_the_skip_window(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    fakes: Any,
    db_engine: Engine,
) -> None:
    rid = start_private(db_client, fake_gh)
    clock = {"t": 1000.0}
    w = make_worker(fake_gh, worker_skip_seconds=60.0)
    w.now = lambda: clock["t"]
    drain(w, limit=2)  # fetch and parse: the job is now waiting at embedding
    assert phase_of(db_engine, rid) == "embed"
    with Session(db_engine) as db:
        db_client.delete("/api/providers/embedding")
        assert w.run_once(db).startswith("skipped")
        assert w.run_once(db) == "idle"  # remembered: not picked again straight away
        clock["t"] += 61
        assert w.run_once(db).startswith("skipped")  # looked at again after the window
        save(db_client, "embedding")
        clock["t"] += 61
        assert w.run_once(db) == "ran"  # and runs once it can
    assert phase_of(db_engine, rid) in {"embed", "graph", "done"}


# --- the loop -----------------------------------------------------------------------------


def test_a_worker_that_is_told_to_stop_stops(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    start_public(db_client, fake_gh)
    stop = threading.Event()
    stop.set()
    assert make_worker(fake_gh).run(stop) == 0  # nothing started after the stop signal
    assert fake_gh.tarball_calls == 0


def test_a_step_in_progress_finishes_before_the_worker_stops(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_public(db_client, fake_gh)
    stop = threading.Event()
    w = make_worker(fake_gh)
    original = runner.tick

    def tick_then_stop(*a: Any, **k: Any) -> Any:
        result = original(*a, **k)
        stop.set()  # the signal arrives while a step is running
        return result

    monkeypatch.setattr(runner, "tick", tick_then_stop)
    steps = w.run(stop)
    assert steps == 1
    assert rows(db_engine, "SELECT phase, lease_until FROM index_jobs")[0][1] is None  # released


def test_a_crashing_step_does_not_kill_the_worker_and_is_not_retried_at_once(
    db_client: TestClient, fake_gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, db_engine: Engine
) -> None:
    start_public(db_client, fake_gh)
    w = make_worker(fake_gh)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("worker bug")

    monkeypatch.setattr("app.worker.runner.tick", boom)
    with Session(db_engine) as db:
        assert w.run_once(db) == "skipped:error"
        assert w.run_once(db) == "idle"


# --- several workers ----------------------------------------------------------------------


def test_two_workers_never_do_the_same_step_twice(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    ids = [start_public(db_client, fake_gh, f"p{i}") for i in range(6)]
    errors: list[BaseException] = []

    def work() -> None:
        try:
            w = make_worker(fake_gh)
            for _ in range(60):
                w.run(threading.Event(), once=True)
                if set(states(db_engine).values()) == {"ready"}:
                    break
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    assert all(
        db_client.get(f"/api/repositories/{r}/status").json()["state"] == "ready" for r in ids
    )
    assert fake_gh.tarball_calls == len(ids)  # one download per job, not two
    dupes = rows(
        db_engine,
        "SELECT count(*) FROM "
        "(SELECT snapshot_id, path FROM files GROUP BY 1, 2 HAVING count(*) > 1) d",
    )[0][0]
    assert dupes == 0


def test_the_cli_runs_once_and_exits(
    db_client: TestClient, fake_gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, db_engine: Engine
) -> None:
    import app.worker as worker_module

    start_public(db_client, fake_gh)
    monkeypatch.setattr(
        worker_module,
        "github_for_user",
        lambda user_id, db, settings, cipher, oauth: fake_gh,
    )
    assert worker_module.main(["--once"]) == 0
    assert set(states(db_engine).values()) == {"ready"}
    assert datetime.now(UTC) - timedelta(days=1) < datetime.now(UTC)


def test_the_worker_loop_beats_its_heartbeat(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    clock = {"t": 0.0}
    from app.worker_http import Heartbeat

    w = make_worker(fake_gh)
    w.heartbeat = Heartbeat(lambda: clock["t"])
    clock["t"] = 500.0
    assert w.heartbeat.age() == 500.0  # nothing has turned the loop yet
    w.run(threading.Event(), once=True)
    assert w.heartbeat.age() == 0.0  # the loop turned: it is alive
