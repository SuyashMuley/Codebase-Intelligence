"""Starting, restarting and reporting on indexing jobs."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import JobInProgress, NoActiveJob, QuotaExceeded
from app.db.models import IndexJob, Repository, Snapshot
from app.schemas.indexing import FetchProgress, JobError, StatusOut
from app.services.github.client import GitHubGateway

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "running")


def _snapshot_with_job(db: Session, repo_id: Any, sha: str) -> tuple[Snapshot, IndexJob] | None:
    row = db.execute(
        select(Snapshot, IndexJob)
        .join(IndexJob, IndexJob.snapshot_id == Snapshot.id)
        .where(Snapshot.repository_id == repo_id, Snapshot.commit_sha == sha)
    ).first()
    return (row[0], row[1]) if row else None


def restart_failed(db: Session, snapshot: Snapshot, job: IndexJob) -> bool:
    """Put a failed job back at the phase it failed in. Returns True if it was restarted."""
    cursor = dict(job.cursor or {})
    phase = str(cursor.pop("failed_phase", "fetch"))
    res = db.execute(
        update(IndexJob)
        .where(IndexJob.id == job.id, IndexJob.phase == "failed")
        .values(
            phase=phase, cursor=cursor, attempts=0, error=None, error_code=None, lease_until=None
        )
    )
    restarted = bool(res.rowcount == 1)  # type: ignore[attr-defined]
    if restarted:
        db.execute(
            update(Snapshot).where(Snapshot.id == snapshot.id).values(status="pending", error=None)
        )
    db.commit()
    db.refresh(snapshot)
    db.refresh(job)
    return restarted


def start_indexing(
    db: Session,
    repo: Repository,
    ref: str | None,
    requested_by: UUID,
    gh: GitHubGateway,
    settings: Settings,
) -> tuple[Snapshot, IndexJob, bool]:
    """Pin a commit and create (or reuse) its snapshot and job. Returns (snapshot, job, created)."""
    info = gh.resolve_commit(repo.owner, repo.name, ref)
    if repo.default_branch != info.default_branch:
        repo.default_branch = info.default_branch
        db.commit()

    existing = _snapshot_with_job(db, repo.id, info.sha)
    if existing:
        snapshot, job = existing
        if job.phase == "failed":
            restart_failed(db, snapshot, job)
        return snapshot, job, False

    in_progress = db.scalar(
        select(func.count())
        .select_from(Snapshot)
        .where(Snapshot.repository_id == repo.id, Snapshot.status.in_(ACTIVE_STATUSES))
    )
    if in_progress:
        raise JobInProgress(
            "Another snapshot of this repository is still being indexed. "
            "Let it finish before indexing a different commit."
        )

    recent = db.scalar(
        select(func.count())
        .select_from(Snapshot)
        .where(
            Snapshot.requested_by == requested_by,
            Snapshot.created_at >= func.now() - timedelta(hours=24),
        )
    )
    if (recent or 0) >= settings.ingestions_per_client_per_day:
        raise QuotaExceeded(
            f"Daily indexing quota reached ({settings.ingestions_per_client_per_day} "
            "new snapshots per 24 hours).",
            details={"limit": settings.ingestions_per_client_per_day},
        )

    snapshot = Snapshot(repository_id=repo.id, commit_sha=info.sha, requested_by=requested_by)
    db.add(snapshot)
    try:
        db.flush()
        job = IndexJob(snapshot_id=snapshot.id, phase="fetch", cursor={}, attempts=0)
        db.add(job)
        db.commit()
    except IntegrityError:
        # Lost a race with an identical concurrent request: return the winner's snapshot.
        db.rollback()
        winner = _snapshot_with_job(db, repo.id, info.sha)
        if winner is None:
            raise
        return winner[0], winner[1], False
    return snapshot, job, True


def active_job(db: Session, repo_id: Any) -> IndexJob:
    job = db.scalar(
        select(IndexJob)
        .join(Snapshot, IndexJob.snapshot_id == Snapshot.id)
        .where(Snapshot.repository_id == repo_id, Snapshot.status.in_(ACTIVE_STATUSES))
        .order_by(Snapshot.created_at.desc())
        .limit(1)
    )
    if job is None:
        raise NoActiveJob("There is no indexing job to run for this repository.")
    return job


def latest_status(db: Session, repo_id: Any, max_attempts: int) -> StatusOut:
    row = db.execute(
        select(Snapshot, IndexJob)
        .join(IndexJob, IndexJob.snapshot_id == Snapshot.id)
        .where(Snapshot.repository_id == repo_id)
        .order_by(Snapshot.created_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return StatusOut(state="not_indexed", attempts=0, max_attempts=max_attempts)
    return build_status(row[0], row[1], max_attempts)


def build_status(snapshot: Snapshot, job: IndexJob, max_attempts: int) -> StatusOut:
    cursor = job.cursor or {}
    fetch = cursor.get("fetch")
    progress = (
        FetchProgress(
            files_kept=int(fetch.get("files_kept", 0)),
            bytes_kept=int(fetch.get("bytes_kept", 0)),
            skipped={str(k): int(v) for k, v in fetch.get("skipped", {}).items()},
        )
        if isinstance(fetch, dict)
        else None
    )
    error = (
        JobError(code=job.error_code or "error", message=job.error)
        if job.error and snapshot.status != "ready"
        else None
    )
    retry_at = None
    if error and job.phase != "failed" and job.lease_until:
        if job.lease_until > datetime.now(UTC):
            retry_at = job.lease_until
    return StatusOut(
        state=snapshot.status,
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        phase=job.phase,
        error=error,
        attempts=job.attempts,
        max_attempts=max_attempts,
        retry_at=retry_at,
        can_resume=job.phase == "failed",
        progress=progress,
        updated_at=job.updated_at,
    )
