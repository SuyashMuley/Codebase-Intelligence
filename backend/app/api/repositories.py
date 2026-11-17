from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    DbSession,
    OwnedRepo,
    csrf_guard,
    ip_backstop,
    user_general_limit,
)
from app.db.models import Repository, UserRepository
from app.schemas.repository import ErrorEnvelope, RepositoryCreate, RepositoryList, RepositoryOut
from app.services.github.url import parse_github_url

router = APIRouter(
    prefix="/api/repositories",
    tags=["repositories"],
    dependencies=[Depends(ip_backstop), Depends(csrf_guard), Depends(user_general_limit)],
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Invalid GitHub URL"},
    401: {"model": ErrorEnvelope, "description": "Not signed in"},
    422: {"model": ErrorEnvelope, "description": "Request validation failed"},
}


def _find_by_name(db: Session, owner: str, name: str) -> Repository | None:
    return db.scalar(
        select(Repository).where(
            func.lower(Repository.owner) == owner.lower(),
            func.lower(Repository.name) == name.lower(),
        )
    )


def _link(db: Session, user: CurrentUser, repo: Repository) -> bool:
    """Connect the repository to the user. Returns True if the link is new."""
    result = db.execute(
        pg_insert(UserRepository)
        .values(user_id=user.id, repository_id=repo.id)
        .on_conflict_do_nothing()
        .returning(UserRepository.user_id)
    )
    created = result.first() is not None
    db.commit()
    return created


@router.post(
    "",
    response_model=RepositoryOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RepositoryOut, "description": "Already connected"}, **_ERRORS},
)
def create_repository(
    body: RepositoryCreate, response: Response, db: DbSession, user: CurrentUser
) -> Repository:
    """Connect a public GitHub repository to your account. Idempotent.

    The repository record itself is shared between users (public data); what is yours is the
    connection. This validates and stores the URL only; indexing happens separately.
    """
    ref = parse_github_url(body.url)

    repo = _find_by_name(db, ref.owner, ref.name)
    if repo is None:
        repo = Repository(owner=ref.owner, name=ref.name, url=ref.url)
        db.add(repo)
        try:
            db.commit()
        except IntegrityError:
            # Lost a race with a concurrent identical request; use the winner's row.
            db.rollback()
            repo = _find_by_name(db, ref.owner, ref.name)
            if repo is None:
                raise

    if not _link(db, user, repo):
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
def get_repository(repo: OwnedRepo) -> Repository:
    return repo
