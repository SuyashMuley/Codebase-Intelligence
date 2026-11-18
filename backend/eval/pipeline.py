"""Index a repository through the real production pipeline, for evaluation.

It uses the same runner, phases and database as the application (no parallel implementation),
so what is measured is what ships. The temporary repository is always deleted afterwards.
"""

import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import IndexJob, Repository, Snapshot
from app.db.session import get_engine
from app.services.github.client import (
    ByteSource,
    CommitInfo,
    GitHubGateway,
    GithubRepoInfo,
    RepoMeta,
)
from app.services.indexing import runner

MAX_TICKS = 400


class _RealRepository:
    """Downloads always use the real owner and name, whatever the temporary row is called."""

    def __init__(self, inner: GitHubGateway, owner: str, name: str) -> None:
        self._inner, self._owner, self._name = inner, owner, name

    def get_repo(self, owner: str, name: str) -> RepoMeta:
        return self._inner.get_repo(self._owner, self._name)

    def resolve_commit(
        self, owner: str, name: str, ref: str | None = None, allow_private: bool = False
    ) -> CommitInfo:
        return self._inner.resolve_commit(self._owner, self._name, ref, allow_private)

    def list_private_repos(self, limit: int = 100) -> list[GithubRepoInfo]:
        return self._inner.list_private_repos(limit)

    def list_public_repos(self, login: str, limit: int = 100) -> list[GithubRepoInfo]:
        return self._inner.list_public_repos(login, limit)

    def open_tarball(
        self, owner: str, name: str, sha: str, max_bytes: int
    ) -> AbstractContextManager[ByteSource]:
        return self._inner.open_tarball(self._owner, self._name, sha, max_bytes)


class IndexingFailed(RuntimeError):
    pass


@contextmanager
def indexed_snapshot(
    owner: str,
    name: str,
    commit: str,
    settings: Settings,
    gh: GitHubGateway,
    through: str,
) -> Iterator[tuple[Session, uuid.UUID, dict[str, Any]]]:
    """Fetch, parse (and, for through='embed', embed) one pinned commit.

    Yields (session, snapshot id, the job's final cursor). `through` is the last phase to run:
    'parse' stops before embedding, 'embed' runs it. The temporary rows are removed on exit.
    """
    stop_at = {"parse": "embed", "embed": "graph", "graph": "done"}[through]
    db = Session(get_engine(), expire_on_commit=False)
    repo = Repository(
        owner=f"eval-{owner}"[:39],
        name=f"{name}-{uuid.uuid4().hex[:8]}"[:100],
        url=f"https://github.com/{owner}/{name}",
    )
    try:
        db.add(repo)
        db.flush()
        snapshot = Snapshot(repository_id=repo.id, commit_sha=commit)
        db.add(snapshot)
        db.flush()
        job = IndexJob(snapshot_id=snapshot.id, phase="fetch", cursor={}, attempts=0)
        db.add(job)
        db.commit()

        for _ in range(MAX_TICKS):
            result = runner.tick(db, job.id, _RealRepository(gh, owner, name), settings)
            db.refresh(job)
            if job.phase == "failed":
                raise IndexingFailed(f"{job.error_code}: {job.error}")
            if job.phase == stop_at:
                break
            if not result.ran and result.reason and "backing_off" in result.reason:
                raise IndexingFailed("the job is backing off after an error; try again later")
        else:
            raise IndexingFailed("indexing did not finish within the tick limit")
        yield db, snapshot.id, dict(job.cursor or {})
    finally:
        db.rollback()
        stale = db.scalar(select(Repository).where(Repository.id == repo.id))
        if stale is not None:
            db.delete(stale)  # cascades to snapshots, files, symbols, chunks and refs
            db.commit()
        db.close()
