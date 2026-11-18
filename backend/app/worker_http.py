"""The worker's own small HTTP server: `/health` for an orchestrator, `/metrics` for Prometheus.

The worker is a separate process from the API, so it cannot use the API's endpoints. `/health`
says whether the worker's loop is still turning (it reports unhealthy when it has not for longer
than a step could take, so a hung worker gets restarted). `/metrics` needs the bearer token and
does not exist without one, as on the API. Nothing else is served.
"""

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.metrics import CONTENT_TYPE, render


class Heartbeat:
    """When the worker's loop last turned. Thread-safe."""

    def __init__(self, clock=time.monotonic) -> None:  # type: ignore[no-untyped-def]
        self._clock = clock
        self._lock = threading.Lock()
        self._last = clock()

    def beat(self) -> None:
        with self._lock:
            self._last = self._clock()

    def age(self) -> float:
        with self._lock:
            return float(self._clock() - self._last)


def make_server(
    host: str,
    port: int,
    heartbeat: Heartbeat,
    stale_seconds: float,
    metrics_token: str | None,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:  # no access log: nothing to leak
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            path = self.path.split("?", 1)[0]
            if path == "/health":
                age = heartbeat.age()
                ok = age <= stale_seconds
                body = json.dumps(
                    {"status": "ok" if ok else "stale", "seconds_since_last_loop": round(age, 1)}
                ).encode()
                self._send(200 if ok else 503, body, "application/json")
            elif path == "/metrics" and metrics_token is not None:
                presented = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                if hmac.compare_digest(presented.encode(), metrics_token.encode()):
                    self._send(200, render(), CONTENT_TYPE)
                else:
                    self._send(401, b'{"error":"unauthorized"}', "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

    return ThreadingHTTPServer((host, port), Handler)
