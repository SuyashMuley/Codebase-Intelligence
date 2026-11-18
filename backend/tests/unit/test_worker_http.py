"""The worker's own /health and /metrics server."""

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from app.worker_http import Heartbeat, make_server

TOKEN = "s3cret-metrics-token"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def get(url: str, token: str | None = None) -> tuple[int, bytes, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


@pytest.fixture
def server() -> Iterator[tuple[str, Clock, Heartbeat]]:
    clock = Clock()
    beat = Heartbeat(clock)
    srv = make_server("127.0.0.1", 0, beat, stale_seconds=120.0, metrics_token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", clock, beat
    srv.shutdown()
    srv.server_close()


def test_health_is_ok_while_the_loop_turns_and_stale_when_it_stops(
    server: tuple[str, Clock, Heartbeat],
) -> None:
    base, clock, beat = server
    status, body, headers = get(f"{base}/health")
    assert status == 200 and json.loads(body)["status"] == "ok"
    assert headers["Cache-Control"] == "no-store"

    clock.t += 119
    assert get(f"{base}/health")[0] == 200  # a long step is not a hang
    clock.t += 5
    status, body, _ = get(f"{base}/health")
    assert status == 503 and json.loads(body)["status"] == "stale"
    assert json.loads(body)["seconds_since_last_loop"] == 124.0

    beat.beat()  # the loop turns again
    assert get(f"{base}/health")[0] == 200


def test_metrics_need_the_bearer_token(server: tuple[str, Clock, Heartbeat]) -> None:
    base, _, _ = server
    assert get(f"{base}/metrics")[0] == 401
    assert get(f"{base}/metrics", "wrong")[0] == 401
    status, body, headers = get(f"{base}/metrics", TOKEN)
    assert status == 200 and b"cip_index_steps_total" in body
    assert headers["Content-Type"].startswith("text/plain")
    assert TOKEN.encode() not in body


def test_nothing_else_is_served(server: tuple[str, Clock, Heartbeat]) -> None:
    base, _, _ = server
    for path in ("/", "/ready", "/api/repositories", "/metrics/../health2", "/openapi.json"):
        assert get(f"{base}{path}", TOKEN)[0] == 404, path
    assert get(f"{base}/health?x=1")[0] == 200  # a query string does not change the route
    req = urllib.request.Request(f"{base}/health", method="POST", data=b"x")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 501  # no other method is implemented


def test_metrics_do_not_exist_without_a_token() -> None:
    srv = make_server("127.0.0.1", 0, Heartbeat(), 120.0, metrics_token=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert get(f"{base}/metrics")[0] == 404
        assert get(f"{base}/metrics", TOKEN)[0] == 404
        assert get(f"{base}/health")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_server_binds_only_where_it_is_told() -> None:
    srv = make_server("127.0.0.1", 0, Heartbeat(), 120.0, None)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()
