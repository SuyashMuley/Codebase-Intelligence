import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        # Neon suspends idle compute and drops connections; verify before reuse.
        pool_pre_ping=True,
        pool_recycle=300,
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


# Closing a session hands its connection back to the pool. That must never have to queue behind
# request code for a worker thread: under load every worker can be waiting for a connection, and
# the connections would all be waiting to be handed back (a deadlock found by the load test, see
# docs/LOAD_TEST.md). Closing therefore has its own small executor, which nothing else uses and
# whose threads never wait for a connection.
_CLOSER = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db-close")


async def get_db() -> AsyncIterator[Session]:
    session = _session_factory()()
    try:
        yield session
    finally:
        await asyncio.get_running_loop().run_in_executor(_CLOSER, session.close)
