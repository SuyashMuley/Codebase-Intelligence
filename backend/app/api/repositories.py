from typing import Annotated, Any

from fastapi import Depends, Query, Response, status
from sqlalchemy import delete, func, select

from app.api.deps import (
    CurrentUser,
    DbSession,
    OwnedRepoUnchecked,
    csrf_guard,
    get_github,
    ip_backstop,
    user_general_limit,
)
from app.core.dbrelease import ReleasingRouter
from app.db.models import Repository, UserRepository
from app.schemas.repository import ErrorEnvelope, RepositoryCreate, RepositoryList, RepositoryOut
from app.services import audit
from app.services.github.client import GitHubGateway
from app.services.github.url import parse_github_url
from app.services.repos import access

router = ReleasingRouter(
    prefix="/api/repositories",
    tags=["repositories"],
    dependencies=[Depends(ip_backstop), Depends(csrf_guard), Depends(user_general_limit)],
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Invalid GitHub URL"},
    401: {"model": ErrorEnvelope, "description": "Not signed in"},
    422: {"model": ErrorEnvelope, "description": "Request validation failed"},
}


@router.post(
    "",
    response_model=RepositoryOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RepositoryOut, "description": "Already connected"}, **_ERRORS},
)
def create_repository(
    body: RepositoryCreate,
    response: Response,
    db: DbSession,
    user: CurrentUser,
    gh: Annotated[GitHubGateway, Depends(get_github)],
) -> Repository:
    """Connect a GitHub repository to your account. Idempotent.

    GitHub is asked, with your own token, whether the repository is private. A public one is a
    shared record (what is yours is the connection). A private one is a record only you have:
    it exists only if GitHub shows it to you, which needs the GitHub App installed on it.
    Indexing happens separately.
    """
    ref = parse_github_url(body.url)
    meta = gh.get_repo(ref.owner, ref.name)
    repo, created = access.connect(db, user, ref, meta)
    if repo.is_private:
        audit.record(db, "private.connect", user.id, repo, {"new": created})
    if not created:
        response.status_code = status.HTTP_200_OK
    return repo


@router.get("", response_model=RepositoryList, responses={401: _ERRORS[401]})
def list_repositories(
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RepositoryList:
    """Repositories you connected, newest first."""
    mine = (
        select(Repository)
        .join(UserRepository, UserRepository.repository_id == Repository.id)
        .where(UserRepository.user_id == user.id)
    )
    total = db.scalar(select(func.count()).select_from(mine.subquery())) or 0
    rows = db.scalars(
        mine.order_by(UserRepository.created_at.desc(), Repository.id).limit(limit).offset(offset)
    ).all()
    return RepositoryList(
        items=[RepositoryOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{repository_id}",
    response_model=RepositoryOut,
    responses={
        401: _ERRORS[401],
        404: {"model": ErrorEnvelope, "description": "Repository not found"},
    },
)
def get_repository(repo: OwnedRepoUnchecked) -> Repository:
    return repo


@router.delete(
    "/{repository_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: _ERRORS[401],
        404: {"model": ErrorEnvelope, "description": "Repository not found"},
    },
)
def disconnect_repository(repo: OwnedRepoUnchecked, db: DbSession, user: CurrentUser) -> Response:
    """Disconnect a repository from your account.

    The repository's data (snapshots, files, chunks, embeddings, graph, chat counters) is public
    source shared between everyone who connected it, so it is deleted when the last person
    disconnects; while others are still connected only your connection is removed.
    """
    # Lock the repository row so two people disconnecting at once cannot both see the other
    # still connected, and so nobody connects in between the count and the delete.
    if repo.is_private:
        audit.record(db, "private.disconnect", user.id, repo)
    locked = db.scalar(select(Repository).where(Repository.id == repo.id).with_for_update())
    if locked is None:  # already gone
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    db.execute(
        delete(UserRepository).where(
            UserRepository.user_id == user.id, UserRepository.repository_id == repo.id
        )
    )
    remaining = db.scalar(
        select(func.count())
        .select_from(UserRepository)
        .where(UserRepository.repository_id == repo.id)
    )
    if not remaining:
        db.delete(locked)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
