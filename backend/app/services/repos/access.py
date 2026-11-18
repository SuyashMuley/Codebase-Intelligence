"""Who may use a repository: connecting it, and rechecking access to a private one.

A public repository is shared, so anyone who connected it may use it. A private repository
belongs to one user, and GitHub is asked again (not our own records) whether that user can
still see it, because access can be revoked after it was indexed. A check that cannot be
completed fails closed: private code is never served on a guess.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import RepoAccessLost, RepoNotFound
from app.db.models import Repository, User, UserRepository
from app.services import audit
from app.services.github.client import GitHubGateway, RepoMeta
from app.services.github.url import RepoRef


def find_repository(
    db: Session, ref: RepoRef, private_owner: uuid.UUID | None
) -> Repository | None:
    """The shared public row for this name, or the given user's private row."""
    owner_clause = (
        Repository.owner_user_id.is_(None)
        if private_owner is None
        else Repository.owner_user_id == private_owner
    )
    return db.scalar(
        select(Repository).where(
            func.lower(Repository.owner) == ref.owner.lower(),
            func.lower(Repository.name) == ref.name.lower(),
            owner_clause,
        )
    )


def connect(db: Session, user: User, ref: RepoRef, meta: RepoMeta) -> tuple[Repository, bool]:
    """Find or create the repository row GitHub's visibility calls for, and link the user.

    Returns (repository, newly_linked). Visibility comes from `meta` (read from GitHub with the
    user's own token), never from the request."""
    owner = user.id if meta.private else None
    repo = find_repository(db, ref, owner)
    if repo is None:
        repo = Repository(
            owner=ref.owner,
            name=ref.name,
            url=ref.url,
            is_private=meta.private,
            owner_user_id=owner,
            access_checked_at=datetime.now(UTC) if meta.private else None,
        )
        db.add(repo)
        try:
            db.commit()
        except Exception:
            # Lost a race with an identical concurrent request: use the winner's row.
            db.rollback()
            repo = find_repository(db, ref, owner)
            if repo is None:
                raise
    elif meta.private:
        repo.access_checked_at = datetime.now(UTC)
    result = db.execute(
        pg_insert(UserRepository)
        .values(user_id=user.id, repository_id=repo.id)
        .on_conflict_do_nothing()
        .returning(UserRepository.user_id)
    )
    created = result.first() is not None
    db.commit()
    return repo, created


def ensure_private_access(
    db: Session,
    repo: Repository,
    gh: GitHubGateway,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """For a private repository, confirm with GitHub that its owner can still see it.

    The answer is reused for `private_access_recheck_seconds`. If GitHub says no, the repository
    is unreachable (its data stays until the owner disconnects it). If GitHub cannot be asked,
    the error propagates and the request fails: it never falls back to "probably fine"."""
    if not repo.is_private:
        return
    now = now or datetime.now(UTC)
    checked = repo.access_checked_at
    window = timedelta(seconds=settings.private_access_recheck_seconds)
    if checked is not None and now - checked < window:
        return
    try:
        gh.get_repo(repo.owner, repo.name)
    except RepoNotFound:
        # Drop the cached confirmation so a later success cannot be mistaken for this one.
        repo.access_checked_at = None
        db.commit()
        audit.record(db, "private.access_lost", repo.owner_user_id, repo)
        raise RepoAccessLost(
            "GitHub no longer shows you this repository. Check that the GitHub App is still "
            "installed on it, or disconnect it to delete its data."
        ) from None
    repo.access_checked_at = now
    db.commit()
