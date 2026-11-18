"""Resumable job runner.

Contract: one ``tick`` performs at most one bounded step of the job's current phase, inside a
single database transaction. A tick must claim the job's lease first, and its result is only
written if it still owns that lease (compare-and-set on ``lease_until``), so a slow or
crashed tick can never overwrite a newer one. A failed step rolls back completely.

Phases without a registered handler are not yet implemented; ticks leave those jobs untouched.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, InternalError
from app.db.models import IndexJob, Repository, Snapshot
from app.metrics import INDEX_JOB_FAILURES, INDEX_STEP_DURATION, INDEX_STEPS
from app.providers.embeddings.base import Embedder
from app.services.github.client import GitHubGateway

logger = logging.getLogger(__name__)

PHASES = ("fetch", "parse", "embed", "graph", "done")
TERMINAL_PHASES = ("done", "failed")
_MAX_BACKOFF_SECONDS = 300


@dataclass
class PhaseContext:
    db: Session
    job: IndexJob
    snapshot: Snapshot
    repository: Repository
    gh: GitHubGateway
    settings: Settings
    deadline: float  # time.monotonic() value; handlers must stop before it
    # The embedder to use instead of the platform's: set for a private repository, whose code may
    # only go to its owner's provider.
    embedder: Embedder | None = None


@dataclass
class PhaseResult:
    next_phase: str | None = None  # None = stay in the current phase (more work remains)
    cursor: dict[str, Any] | None = None  # merged into the job cursor


Handler = Callable[[PhaseContext], PhaseResult]
HANDLERS: dict[str, Handler] = {}


@dataclass
class TickResult:
    ran: bool
    reason: str | None = None


def register(phase: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[phase] = fn
        return fn

    return deco


def _claim(db: Session, job_id: Any, lease_seconds: int) -> IndexJob | None:
    """Atomically take the lease if the job is active and nobody holds an unexpired lease."""
    stmt = (
        update(IndexJob)
        .where(
            IndexJob.id == job_id,
            IndexJob.phase.not_in(TERMINAL_PHASES),
            or_(IndexJob.lease_until.is_(None), IndexJob.lease_until <= func.now()),
        )
        .values(lease_until=func.now() + timedelta(seconds=lease_seconds))
        .returning(IndexJob)
        .execution_options(populate_existing=True)
    )
    job = db.execute(stmt).scalar_one_or_none()
    db.commit()
    return job


def tick(
    db: Session,
    job_id: Any,
    gh: GitHubGateway,
    settings: Settings,
    embedder: Embedder | None = None,
) -> TickResult:
    job = _claim(db, job_id, settings.lease_seconds)
    if job is None:
        INDEX_STEPS.labels("none", "busy").inc()
        return TickResult(False, "busy_or_backing_off_or_finished")
    started = time.monotonic()

    token = job.lease_until
    job_pk = job.id
    phase = job.phase
    attempts = job.attempts
    cursor = dict(job.cursor or {})

    handler = HANDLERS.get(phase)
    snapshot = db.get(Snapshot, job.snapshot_id)
    assert snapshot is not None
    if handler is None:
        db.execute(update(IndexJob).where(IndexJob.id == job_pk).values(lease_until=None))
        db.commit()
        return TickResult(False, f"phase_not_implemented:{phase}")

    if snapshot.status == "pending":
        snapshot.status = "running"
        db.commit()
    repository = db.get(Repository, snapshot.repository_id)
    assert repository is not None

    ctx = PhaseContext(
        db=db,
        job=job,
        snapshot=snapshot,
        repository=repository,
        gh=gh,
        settings=settings,
        deadline=time.monotonic() + settings.fetch_timeout_seconds,
        embedder=embedder,
    )
    try:
        result = handler(ctx)
        next_phase = result.next_phase or phase
        merged = {**cursor, **(result.cursor or {})}
        done = db.execute(
            update(IndexJob)
            .where(IndexJob.id == job_pk, IndexJob.lease_until == token)
            .values(
                phase=next_phase,
                cursor=merged,
                lease_until=None,
                attempts=0,
                error=None,
                error_code=None,
            )
        )
        if done.rowcount != 1:  # type: ignore[attr-defined]
            db.rollback()
            logger.warning("lease_lost", extra={"job_id": str(job_pk), "phase": phase})
            _observe_step(phase, "lease_lost", started)
            return TickResult(False, "lease_lost")
        if next_phase == "done":
            snapshot.status = "ready"
            snapshot.indexed_at = func.now()
        db.commit()
        _observe_step(phase, "ok", started)
        return TickResult(True, None)
    except AppError as exc:
        db.rollback()
        _record_failure(db, job_pk, token, phase, attempts, cursor, exc, settings)
        _observe_step(phase, "error", started)
        return TickResult(True, f"failed:{exc.code}")
    except Exception:
        db.rollback()
        logger.exception("tick_failed", extra={"job_id": str(job_pk), "phase": phase})
        err = InternalError("Unexpected error while indexing.")
        _record_failure(db, job_pk, token, phase, attempts, cursor, err, settings)
        _observe_step(phase, "error", started)
        return TickResult(True, "failed:internal_error")


def _observe_step(phase: str, outcome: str, started: float) -> None:
    INDEX_STEPS.labels(phase, outcome).inc()
    INDEX_STEP_DURATION.labels(phase).observe(time.monotonic() - started)


def _record_failure(
    db: Session,
    job_id: Any,
    token: Any,
    phase: str,
    attempts: int,
    cursor: dict[str, Any],
    exc: AppError,
    settings: Settings,
) -> None:
    upstream_rate_limit = exc.code == "github_rate_limited"
    new_attempts = attempts if upstream_rate_limit else attempts + 1
    terminal = not exc.retryable or new_attempts >= settings.max_attempts
    values: dict[str, Any] = {
        "attempts": new_attempts,
        "error": exc.message,
        "error_code": exc.code,
    }
    if terminal:
        values.update(phase="failed", cursor={**cursor, "failed_phase": phase}, lease_until=None)
        INDEX_JOB_FAILURES.labels(exc.code).inc()
    else:
        wait = exc.details.get("retry_after_seconds") or min(
            _MAX_BACKOFF_SECONDS, 5 * 2**new_attempts
        )
        # While in the future, lease_until doubles as "do not retry before".
        values["lease_until"] = func.now() + timedelta(seconds=int(wait))
    res = db.execute(
        update(IndexJob)
        .where(IndexJob.id == job_id, IndexJob.lease_until == token)
        .values(**values)
    )
    if res.rowcount == 1 and terminal:  # type: ignore[attr-defined]
        job = db.get(IndexJob, job_id)
        assert job is not None
        db.execute(
            update(Snapshot)
            .where(Snapshot.id == job.snapshot_id)
            .values(status="failed", error=exc.message)
        )
    db.commit()
    logger.warning(
        "tick_failure",
        extra={"job_id": str(job_id), "phase": phase, "code": exc.code, "terminal": terminal},
    )
