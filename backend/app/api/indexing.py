import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

import app.services.indexing  # noqa: F401  (registers phase handlers)
from app.api.deps import general_limit, get_client_key, get_github, index_limit
from app.core.config import Settings, get_settings
from app.core.errors import RepoNotFound
from app.db.models import IndexJob, Repository, Snapshot
from app.db.session import get_db
from app.schemas.indexing import IndexRequest, IndexStartOut, StatusOut, TickOut
from app.schemas.repository import ErrorEnvelope
from app.services.github.client import GitHubGateway
from app.services.indexing import runner, service

router = APIRouter(
    prefix="/api/repositories/{repository_id}",
    tags=["indexing"],
    dependencies=[Depends(general_limit)],
)

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Github = Annotated[GitHubGateway, Depends(get_github)]

_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorEnvelope, "description": "Repository or ref not found"},
    409: {"model": ErrorEnvelope, "description": "Conflicting indexing job"},
    429: {"model": ErrorEnvelope, "description": "Rate limit or quota exceeded"},
    503: {"model": ErrorEnvelope, "description": "GitHub rate limited or unavailable"},
}


def _repo(db: Session, repository_id: uuid.UUID) -> Repository:
    repo = db.get(Repository, repository_id)
    if repo is None:
        raise RepoNotFound(f"Repository {repository_id} not found.")
    return repo


@router.post(
    "/index",
    response_model=IndexStartOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(index_limit)],
    responses={200: {"model": IndexStartOut, "description": "Commit already known"}, **_ERRORS},
)
def start_index(
    repository_id: uuid.UUID,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    gh: Github,
    client_key: Annotated[str, Depends(get_client_key)],
    body: IndexRequest | None = None,
) -> IndexStartOut:
    """Pin a commit and queue it for indexing. Drive it forward with `POST .../index/tick`.

    Indexing the same commit twice returns the existing snapshot instead of repeating work;
    a failed one is restarted.
    """
    repo = _repo(db, repository_id)
    snapshot, job, created = service.start_indexing(
        db, repo, body.ref if body else None, client_key, gh, settings
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return IndexStartOut(
        created=created, status=service.build_status(snapshot, job, settings.max_attempts)
    )


@router.post("/index/tick", response_model=TickOut, responses=_ERRORS)
def tick_index(
    repository_id: uuid.UUID, db: DbSession, settings: AppSettings, gh: Github
) -> TickOut:
    """Run one bounded step of the active indexing job. Safe to call repeatedly or concurrently."""
    _repo(db, repository_id)
    job = service.active_job(db, repository_id)
    result = runner.tick(db, job.id, gh, settings)
    return TickOut(
        ran=result.ran,
        reason=result.reason,
        status=service.latest_status(db, repository_id, settings.max_attempts),
    )


@router.get("/status", response_model=StatusOut, responses=_ERRORS)
def get_status(repository_id: uuid.UUID, db: DbSession, settings: AppSettings) -> StatusOut:
    _repo(db, repository_id)
    return service.latest_status(db, repository_id, settings.max_attempts)


__all__ = ["IndexJob", "Snapshot", "router"]
