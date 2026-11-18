"""Private snapshots do not live forever.

A private repository's indexed data (files, chunks, vectors, graph) is deleted
`private_retention_days` after it was indexed. The repository itself stays connected, so the owner
can index it again. Expiry is enforced two ways, because the free tier has no always-on worker:

- whenever a private repository is used (lazily), so expired data is never served even if no
  scheduler ever ran;
- by `purge_expired`, which a scheduler can call through the maintenance endpoint to clear data
  nobody is touching.

Only finished snapshots (ready or failed) are removed: a running one is never deleted from under
its job. Public snapshots are never touched.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AuditEvent, Repository, Snapshot

FINISHED = ("ready", "failed")


def retention_days(repo: Repository, settings: Settings) -> int | None:
    """Days a snapshot of this repository is kept, or None if it is not expired automatically."""
    if not repo.is_private or settings.private_retention_days <= 0:
        return None
    return settings.private_retention_days


def expires_at(created_at: datetime, days: int | None) -> datetime | None:
    return None if days is None else created_at + timedelta(days=days)


def purge_repository(
    db: Session, repo: Repository, settings: Settings, now: datetime | None = None
) -> int:
    """Delete this private repository's expired snapshots. Returns how many were deleted."""
    days = retention_days(repo, settings)
    if days is None:
        return 0
    return _delete(db, repo.id, (now or datetime.now(UTC)) - timedelta(days=days))


def purge_expired(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """Delete every expired private snapshot. Returns how many were deleted."""
    if settings.private_retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.private_retention_days)
    return _delete(db, None, cutoff)


def purge_audit(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """Delete audit events older than the audit retention. Returns how many were deleted."""
    if settings.audit_retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.audit_retention_days)
    result = db.execute(delete(AuditEvent).where(AuditEvent.at < cutoff))
    db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _delete(db: Session, repo_id: uuid.UUID | None, cutoff: datetime) -> int:
    private_repos = select(Repository.id).where(Repository.is_private.is_(True))
    if repo_id is not None:
        private_repos = private_repos.where(Repository.id == repo_id)
    result = db.execute(
        delete(Snapshot).where(
            Snapshot.repository_id.in_(private_repos),
            Snapshot.status.in_(FINISHED),
            Snapshot.created_at < cutoff,
        )
    )
    db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
