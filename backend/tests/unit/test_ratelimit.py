from starlette.requests import Request

from app.core.ratelimit import SlidingWindowLimiter, client_ip, client_key


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_limit_then_blocks_with_retry_after() -> None:
    clock = Clock()
    lim = SlidingWindowLimiter(clock=clock)
    assert all(lim.hit("k", 3) is None for _ in range(3))
    retry = lim.hit("k", 3)
    assert retry is not None and 59 < retry <= 60


def test_window_slides() -> None:
    clock = Clock()
    lim = SlidingWindowLimiter(clock=clock)
    for _ in range(3):
        lim.hit("k", 3)
        clock.now += 10
    assert lim.hit("k", 3) is not None  # 30s in, oldest hit still inside the window
    clock.now += 31  # oldest (t=0) is now 61s old
    assert lim.hit("k", 3) is None


def test_keys_are_independent() -> None:
    lim = SlidingWindowLimiter(clock=Clock())
    assert lim.hit("a", 1) is None
    assert lim.hit("a", 1) is not None
    assert lim.hit("b", 1) is None


def test_memory_is_bounded() -> None:
    lim = SlidingWindowLimiter(max_keys=50, clock=Clock())
    for i in range(500):
        lim.hit(f"k{i}", 5)
    assert len(lim._events) <= 50


def make_request(xff: str | None, peer: str | None = "9.9.9.9") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    scope = {"type": "http", "headers": headers, "client": (peer, 1234) if peer else None}
    return Request(scope)


def test_forwarded_for_ignored_when_no_trusted_proxy() -> None:
    assert client_ip(make_request("1.1.1.1"), forwarded_for_hops=0) == "9.9.9.9"


def test_forwarded_for_counts_from_the_right() -> None:
    # The client controls the left side; the trusted proxy appends the real peer on the right.
    req = make_request("6.6.6.6, 1.1.1.1, 2.2.2.2")
    assert client_ip(req, forwarded_for_hops=1) == "2.2.2.2"
    assert client_ip(req, forwarded_for_hops=2) == "1.1.1.1"


def test_forwarded_for_falls_back_to_peer() -> None:
    assert client_ip(make_request(None), forwarded_for_hops=1) == "9.9.9.9"
    assert client_ip(make_request("1.1.1.1"), forwarded_for_hops=3) == "9.9.9.9"
    assert client_ip(make_request(None, peer=None), forwarded_for_hops=0) == "unknown"


def test_client_key_is_stable_salted_and_does_not_contain_ip() -> None:
    a = client_key("1.2.3.4", "salt")
    assert a == client_key("1.2.3.4", "salt")
    assert a != client_key("1.2.3.4", "other")
    assert a != client_key("1.2.3.5", "salt")
    assert "1.2.3.4" not in a and len(a) == 32
