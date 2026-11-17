import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import RepoNotFound
from app.db.models import Repository
from app.db.session import get_db
from app.schemas.repository import ErrorEnvelope, RepositoryCreate, RepositoryList, RepositoryOut
from app.services.github.url import parse_github_url

router = APIRouter(prefix="/api/repositories", tags=["repositories"])

DbSession = Annotated[Session, Depends(get_db)]

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Invalid GitHub URL"},
    422: {"model": ErrorEnvelope, "description": "Request validation failed"},
}


def _find_by_name(db: Session, owner: str, name: str) -> Repository | None:
    return db.scalar(
        select(Repository).where(
            func.lower(Repository.owner) == owner.lower(),
            func.lower(Repository.name) == name.lower(),
        )
    )


@router.post(
    "",
    response_model=RepositoryOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RepositoryOut, "description": "Already registered"}, **_ERRORS},
)
def create_repository(body: RepositoryCreate, response: Response, db: DbSession) -> Repository:
    """Register a public GitHub repository. Idempotent: re-registering returns the existing one.

    This validates and stores the URL only; fetching and indexing happen separately.
    """
    ref = parse_github_url(body.url)

    existing = _find_by_name(db, ref.owner, ref.name)
    if existing:
        response.status_code = status.HTTP_200_OK
        return existing

    repo = Repository(owner=ref.owner, name=ref.name, url=ref.url)
    db.add(repo)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent identical request; return the winner.
        db.rollback()
        winner = _find_by_name(db, ref.owner, ref.name)
        if winner is None:
            raise
        response.status_code = status.HTTP_200_OK
        return winner
    return repo


@router.get("", response_model=RepositoryList)
def list_repositories(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RepositoryList:
    total = db.scalar(select(func.count()).select_from(Repository)) or 0
    rows = db.scalars(
        select(Repository)
        .order_by(Repository.created_at.desc(), Repository.id)
        .limit(limit)
        .offset(offset)
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
    responses={404: {"model": ErrorEnvelope, "description": "Repository not found"}},
)
def get_repository(repository_id: uuid.UUID, db: DbSession) -> Repository:
    repo = db.get(Repository, repository_id)
    if repo is None:
        raise RepoNotFound(f"Repository {repository_id} not found.")
    return repo
