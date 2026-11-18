"""Under load, a request must never hold a database connection while it waits for a worker thread.

The load test (docs/LOAD_TEST.md) found that with the default pool, 50 concurrent users brought the
API to a standstill: every connection was held by a request waiting for a worker thread, while
every worker thread waited for a connection, and nothing moved until the pool timed out. This
reproduces it in miniature (a pool of 2, four worker threads, 32 clients) so it cannot come back.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import anyio.to_thread
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from tests.conftest import TEST_DATABASE_URL, Persona, make_client
from tests.helpers import FakeGitHub

pytestmark = pytest.mark.db

POOL = 2
THREADS = 4
CLIENTS = 32
REQUESTS = 160


@pytest.fixture
def tiny_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL, pool_size=POOL, max_overflow=0, pool_timeout=4)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._session_factory", lambda: factory)
    yield engine
    engine.dispose()


def hammer(db_app: FastAPI, alice: Persona, fake: FakeGitHub) -> tuple[list[int], int]:
    """Fire many concurrent requests at routes that all use the user and ownership dependencies
    and then the database. Returns the status codes and the highest number of connections that
    were checked out at once."""
    base = get_settings()
    # The rate limits are tested elsewhere; here they must not be what stops the requests.
    db_app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={
            "rate_limit_per_minute": 10**9,
            "ip_rate_limit_per_minute": 10**9,
            "search_rate_limit_per_minute": 10**9,
        }
    )
    with make_client(db_app, alice) as client:

        async def shrink_workers() -> None:
            anyio.to_thread.current_default_thread_limiter().total_tokens = THREADS

        client.portal.call(shrink_workers)  # type: ignore[union-attr]
        rid = client.post("/api/repositories", json={"url": "https://github.com/acme/open"}).json()[
            "id"
        ]
        routes: list[tuple[str, str, Any]] = [
            ("GET", "/api/repositories", None),
            ("GET", f"/api/repositories/{rid}", None),
            ("GET", f"/api/repositories/{rid}/status", None),
            ("GET", f"/api/repositories/{rid}/files", None),
            ("POST", f"/api/repositories/{rid}/search", {"query": "x", "mode": "keyword"}),
        ]

        def one(i: int) -> int:
            method, url, body = routes[i % len(routes)]
            return client.request(method, url, json=body).status_code

        with ThreadPoolExecutor(CLIENTS) as pool:
            codes = list(pool.map(one, range(REQUESTS), timeout=60))
    return codes, POOL


def test_a_pool_smaller_than_the_load_does_not_stall_the_api(
    tiny_pool: Engine,
    db_app: FastAPI,
    alice: Persona,
    fake_gh: FakeGitHub,
) -> None:
    codes, _ = hammer(db_app, alice, fake_gh)
    server_errors = [c for c in codes if c >= 500]
    # 409 is an honest answer (nothing is indexed yet); a 500 here is a connection that could not
    # be had within the pool timeout, which is the stall.
    assert not server_errors, (
        f"{len(server_errors)} of {len(codes)} requests failed: {set(server_errors)}"
    )
    assert set(codes) <= {200, 409}
    assert cast(Any, tiny_pool.pool).checkedout() == 0  # every connection came back
