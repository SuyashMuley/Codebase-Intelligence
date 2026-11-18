"""The rule that stops the API stalling under load: no connection is held when a task ends."""

import asyncio
import importlib
import inspect
import pkgutil
import threading
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api as api_package
from app.core.dbrelease import ReleasingRouter, release_after
from app.db import session as session_module


class FakeSession(Session):
    """Records what happens to the transaction, without a database."""

    def __init__(self) -> None:  # no engine: nothing here talks to a database
        self.events: list[str] = []
        self.active = True

    def commit(self) -> None:
        self.events.append("commit")
        self.active = False

    def rollback(self) -> None:
        self.events.append("rollback")
        self.active = False

    def in_transaction(self) -> bool:
        return self.active

    def close(self) -> None:
        self.events.append(f"close:{threading.current_thread().name}")


def test_a_finished_endpoint_ends_its_transaction() -> None:
    db = FakeSession()

    @release_after
    def endpoint(db: Session, n: int) -> int:
        return n + 1

    assert endpoint(db=db, n=1) == 2
    assert db.events == ["commit"]


def test_a_failing_endpoint_rolls_back_and_the_error_still_propagates() -> None:
    db = FakeSession()

    @release_after
    def endpoint(db: Session) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        endpoint(db=db)
    assert db.events == ["rollback"]  # never a commit for something that failed


def test_nothing_is_committed_when_no_transaction_was_started() -> None:
    db = FakeSession()
    db.active = False

    @release_after
    def endpoint(db: Session) -> str:
        return "ok"

    assert endpoint(db=db) == "ok" and db.events == []


def test_endpoints_without_a_session_and_async_endpoints_are_left_alone() -> None:
    @release_after
    def plain(x: int) -> int:
        return x

    async def coroutine() -> int:
        return 1

    assert plain(x=3) == 3
    assert release_after(coroutine) is coroutine  # runs on the event loop: no thread hop to guard


def test_the_signature_fastapi_reads_is_preserved() -> None:
    def endpoint(db: Session, limit: int = 20, *, q: str | None = None) -> int:
        return limit

    wrapped = release_after(endpoint)
    assert inspect.signature(wrapped) == inspect.signature(endpoint)
    assert wrapped.__name__ == "endpoint"  # the audit log names routes by this


def test_a_router_wraps_the_endpoints_registered_through_every_decorator_style() -> None:
    router = ReleasingRouter(prefix="/t")
    sessions: list[FakeSession] = []

    def fake_db() -> FakeSession:
        s = FakeSession()
        sessions.append(s)
        return s

    @router.get("/a")
    def a(db: Annotated[Session, Depends(fake_db)]) -> dict[str, int]:
        return {"n": 1}

    def b(db: Annotated[Session, Depends(fake_db)]) -> dict[str, int]:
        return {"n": 2}

    router.add_api_route("/b", b, methods=["GET"])
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/t/a").json() == {"n": 1} and client.get("/t/b").json() == {"n": 2}
    assert [s.events for s in sessions] == [["commit"], ["commit"]]


def test_every_router_in_the_app_releases_its_connections() -> None:
    """A plain APIRouter added later would silently bring the stall back."""
    found = 0
    for mod in pkgutil.iter_modules(api_package.__path__):
        module = importlib.import_module(f"app.api.{mod.name}")
        for value in vars(module).values():
            if isinstance(value, ReleasingRouter):
                found += 1
            else:
                assert type(value).__name__ != "APIRouter", (
                    f"app.api.{mod.name} uses a plain APIRouter"
                )
    assert found >= 12


def test_the_session_is_closed_on_its_own_executor_not_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSession()
    monkeypatch.setattr(session_module, "_session_factory", lambda: lambda: fake)

    async def run() -> None:
        gen: Any = (
            session_module.get_db()
        )  # an async generator: opened, then closed like FastAPI does
        got = await gen.__anext__()
        assert got is fake
        await gen.aclose()

    asyncio.run(run())
    assert len(fake.events) == 1 and fake.events[0].startswith("close:db-close")
