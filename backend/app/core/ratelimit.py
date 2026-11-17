"""Per-client rate limiting and client identity.

The limiter is in-process, which is correct for a single free-tier instance. If the API is
ever scaled out, swap this for a shared store; the interface (``hit``) stays the same.
"""

import hashlib
import threading
import time
from collections import deque
from collections.abc import Callable

from starlette.requests import Request


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 10_000, clock: Callable[[], float] = time.monotonic) -> None:
        self._events: dict[str, deque[float]] = {}
        self._max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: float = 60.0) -> float | None:
        """Record a request. Returns None if allowed, else seconds until a slot frees up."""
        now = self._clock()
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self._max_keys:
                    self._evict(now, window_seconds)
                events = self._events[key] = deque()
            while events and events[0] <= now - window_seconds:
                events.popleft()
            if len(events) >= limit:
                return max(0.01, events[0] + window_seconds - now)
            events.append(now)
            return None

    def _evict(self, now: float, window_seconds: float) -> None:
        stale = [k for k, ev in self._events.items() if not ev or ev[-1] <= now - window_seconds]
        for k in stale:
            del self._events[k]
        # Still full of active keys: drop the oldest-inserted so memory stays bounded.
        while len(self._events) >= self._max_keys:
            del self._events[next(iter(self._events))]


def client_ip(request: Request, forwarded_for_hops: int) -> str:
    """The client's IP. X-Forwarded-For is only consulted when proxies are configured, and
    entries are counted from the right because the left side is client-controlled."""
    peer = request.client.host if request.client else "unknown"
    if forwarded_for_hops <= 0:
        return peer
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) >= forwarded_for_hops:
        return parts[-forwarded_for_hops][:64]
    return peer


def client_key(ip: str, salt: str) -> str:
    """Stable pseudonymous key for quotas; raw IPs are never stored."""
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]
