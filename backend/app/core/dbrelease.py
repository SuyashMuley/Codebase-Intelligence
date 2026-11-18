"""Never hold a database connection across a worker-thread hop.

FastAPI runs every synchronous dependency, every synchronous endpoint and the validation of the
response as separate tasks on one shared pool of worker threads. A request that keeps its
connection between two of those tasks is holding a connection while it waits for a free thread.
Under load every connection is then held by a request waiting for a thread while every thread
waits for a connection, and nothing moves until the pool times out. The load test found exactly
that (docs/LOAD_TEST.md): 50 concurrent users stalled the API completely.

The rule that prevents it: when one of these tasks ends, its connection goes back to the pool.
Dependencies that read call `release()` (app/api/deps.py), the session is closed on its own
executor (app/db/session.py), and this wraps every synchronous endpoint so the transaction ends
before the response is serialised. Objects the endpoint loaded stay usable, because sessions here
do not expire them on commit.
"""

import functools
import inspect
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from sqlalchemy.orm import Session


def release_after(call: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a synchronous endpoint so its transaction ends before it returns."""
    if inspect.iscoroutinefunction(call):
        return call  # async endpoints run on the event loop and do not hop between threads

    @functools.wraps(call)  # keeps the signature FastAPI reads its parameters from
    def endpoint(*args: Any, **kwargs: Any) -> Any:
        db = next((v for v in kwargs.values() if isinstance(v, Session)), None)
        try:
            result = call(*args, **kwargs)
        except BaseException:
            if db is not None:
                db.rollback()  # an endpoint that failed must not persist anything
            raise
        if db is not None and db.in_transaction():
            db.commit()  # ends the transaction and returns the connection (objects stay loaded)
        return result

    return endpoint


class ReleasingRouter(APIRouter):
    """An APIRouter whose synchronous endpoints release their connection when they finish.

    Every router in the app is one of these (a test checks that none is a plain APIRouter)."""

    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        super().add_api_route(path, release_after(endpoint), **kwargs)
