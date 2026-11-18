"""Read APIs over an indexed snapshot: file explorer, file viewer, statistics."""

import uuid
from typing import Annotated, Any

from fastapi import Depends, Query

from app.api.deps import DbSession, OwnedRepo, csrf_guard, ip_backstop, user_general_limit
from app.core.dbrelease import ReleasingRouter
from app.schemas.files import FileDetail, FileList, StatsOut
from app.schemas.repository import ErrorEnvelope
from app.services.indexing import queries

router = ReleasingRouter(
    prefix="/api/repositories/{repository_id}",
    tags=["files"],
    dependencies=[Depends(ip_backstop), Depends(csrf_guard), Depends(user_general_limit)],
)

MAX_PATH_LENGTH = 500

SnapshotParam = Annotated[
    uuid.UUID | None,
    Query(description="A specific snapshot of this repository. Defaults to the newest."),
]

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in"},
    404: {"model": ErrorEnvelope, "description": "Repository, snapshot or file not found"},
    409: {"model": ErrorEnvelope, "description": "Nothing has been indexed yet"},
}


@router.get("/files", response_model=FileList, responses=_ERRORS)
def list_files(repo: OwnedRepo, db: DbSession, snapshot: SnapshotParam = None) -> FileList:
    """Every stored file of a snapshot: parsed source plus notable skips (too large, binary...)."""
    return queries.list_files(db, queries.resolve_snapshot(db, repo.id, snapshot))


@router.get("/files/{path:path}", response_model=FileDetail, responses=_ERRORS)
def get_file(
    path: str, repo: OwnedRepo, db: DbSession, snapshot: SnapshotParam = None
) -> FileDetail:
    """One file's text (secrets already redacted) and the symbols found in it."""
    snap = queries.resolve_snapshot(db, repo.id, snapshot)
    if len(path) > MAX_PATH_LENGTH or "\x00" in path:
        # Cannot match a stored path; answer exactly like any other missing file.
        queries.get_file(db, snap, "")
    return queries.get_file(db, snap, path)


@router.get("/stats", response_model=StatsOut, responses=_ERRORS)
def get_stats(repo: OwnedRepo, db: DbSession, snapshot: SnapshotParam = None) -> StatsOut:
    """Counts of what was indexed: files by language, symbols, chunks, references, redactions."""
    return queries.build_stats(db, queries.resolve_snapshot(db, repo.id, snapshot))
