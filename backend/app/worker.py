"""A background worker: advances indexing jobs without a browser tab open.

It runs the same `runner.tick` the API's tick endpoint runs, so there is one implementation of a
job step. The lease compare-and-set in `runner._claim` is what makes it safe to run several
workers, and a worker alongside browser tabs: whoever holds an unexpired lease runs the step and
everyone else sees "busy".

Whose credentials a step uses:
- GitHub: the job owner's own token (a private repository's owner; for a public one, whoever
  started the index), refreshed as needed.
- Embeddings: a public repository uses the platform's embedder, as ticking does. A private
  repository uses its owner's own embedding provider, which a worker can use only if the key is
  *stored*. A browser-held key does not exist on the server, so such a job is left for the browser
  to drive; a job whose owner has lost access to the repository, or has no embedding provider, is
  left alone too. Nothing private is ever embedded with the platform's key.

Run it with `python -m app.worker`. `--once` drains the work available now and exits.
"""

import argparse
import logging
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_cipher_or_none, get_oauth_or_none, github_for_user
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ReauthRequired
from app.core.logging import configure_logging
from app.db.models import IndexJob, Repository, Snapshot
from app.db.session import get_engine
from app.providers.embeddings.base import Embedder
from app.services.github.client import GitHubGateway
from app.services.indexing import runner
from app.services.providers import build, store
from app.services.repos.access import ensure_private_access
from app.worker_http import Heartbeat, make_server

logger = logging.getLogger("app.worker")

GitHubFor = Callable[[Session, uuid.UUID], GitHubGateway]


@dataclass
class Worker:
    settings: Settings
    github_for: GitHubFor | None = None
    now: Callable[[], float] = time.monotonic
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    # jobs this worker decided to leave alone for a while: job id -> monotonic time to look again
    _skipped: dict[uuid.UUID, float] = field(default_factory=dict)

    # --- picking work ---------------------------------------------------------------------

    def next_job(self, db: Session) -> tuple[IndexJob, Snapshot, Repository] | None:
        """The least recently worked job that is active, not leased and not being left alone."""
        now = self.now()
        self._skipped = {k: v for k, v in self._skipped.items() if v > now}
        stmt = (
            select(IndexJob, Snapshot, Repository)
            .join(Snapshot, Snapshot.id == IndexJob.snapshot_id)
            .join(Repository, Repository.id == Snapshot.repository_id)
            .where(
                IndexJob.phase.not_in(runner.TERMINAL_PHASES),
                (IndexJob.lease_until.is_(None)) | (IndexJob.lease_until <= func.now()),
            )
            .order_by(IndexJob.updated_at.asc(), IndexJob.id)
            .limit(20)
        )
        for job, snapshot, repo in db.execute(stmt).all():
            if job.id not in self._skipped:
                return job, snapshot, repo
        return None

    def _leave_alone(self, job_id: uuid.UUID, why: str) -> str:
        self._skipped[job_id] = self.now() + self.settings.worker_skip_seconds
        logger.info("job_skipped", extra={"job_id": str(job_id), "why": why})
        return f"skipped:{why}"

    # --- one step -------------------------------------------------------------------------

    def run_once(self, db: Session) -> str:
        """Do at most one job step. Returns 'idle', 'ran', 'busy' or 'skipped:<why>'."""
        picked = self.next_job(db)
        if picked is None:
            return "idle"
        job, snapshot, repo = picked
        job_id = job.id
        user_id = repo.owner_user_id if repo.is_private else snapshot.requested_by
        if user_id is None:
            return self._leave_alone(job_id, "no_user")

        embedder: Embedder | None = None
        try:
            gh = self._github(db, user_id)
            if repo.is_private:
                ensure_private_access(db, repo, gh, self.settings)  # lost access: refuse
                # Fetching, parsing and the graph send nothing to a provider; only embedding does.
                if job.phase == "embed":
                    embedder = self._owner_embedder(db, user_id)
                    if embedder is None:
                        return self._leave_alone(job_id, "needs_browser_or_provider")
            result = runner.tick(db, job_id, gh, self.settings, embedder)
        except ReauthRequired:
            return self._leave_alone(job_id, "reauth_required")
        except AppError as exc:  # access lost, GitHub unreachable, address refused, ...
            return self._leave_alone(job_id, exc.code)
        except Exception:  # noqa: BLE001 - leave this job alone rather than retry it in a loop
            db.rollback()
            logger.exception("worker_step_failed", extra={"job_id": str(job_id)})
            return self._leave_alone(job_id, "error")
        finally:
            close = getattr(embedder, "close", None)
            if callable(close):
                close()
        return "ran" if result.ran else "busy"

    def _github(self, db: Session, user_id: uuid.UUID) -> GitHubGateway:
        if self.github_for is not None:
            return self.github_for(db, user_id)
        return github_for_user(
            user_id,
            db,
            self.settings,
            get_cipher_or_none(self.settings),
            get_oauth_or_none(self.settings),
        )

    def _owner_embedder(self, db: Session, user_id: uuid.UUID) -> Embedder | None:
        """The owner's embedding client, or None when a worker cannot use it (no provider, or the
        key is held in a browser)."""
        row = store.get(db, user_id, "embedding")
        if row is None or row.mode != "stored":
            return None
        key = store.read_key(row, get_cipher_or_none(self.settings), None)
        return build.build_embedder(row, key, self.settings)

    # --- the loop -------------------------------------------------------------------------

    def run(
        self,
        stop: threading.Event,
        once: bool = False,
        max_steps: int | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> int:
        """Work until `stop` is set (or, with `once`, until nothing is left). Returns the number of
        steps run. A step in progress always finishes: a lease is never abandoned half-way."""
        wait = sleep or stop.wait
        steps = 0
        while not stop.is_set():
            self.heartbeat.beat()  # the loop is turning
            with Session(get_engine(), expire_on_commit=False) as db:
                try:
                    outcome = self.run_once(db)
                except Exception:  # noqa: BLE001 - one bad step must not kill the worker
                    db.rollback()
                    logger.exception("worker_step_failed")
                    outcome = "error"
            if outcome == "ran":
                steps += 1
                if max_steps is not None and steps >= max_steps:
                    break
                continue  # more work is probably waiting: no sleep
            if outcome.startswith("skipped"):
                continue  # left alone for a while: look at the next job straight away
            if once:
                break  # idle, busy (another worker has it) or error: nothing more to drain
            wait(self.settings.worker_poll_seconds)
        return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advance indexing jobs in the background.")
    parser.add_argument("--once", action="store_true", help="drain available work, then exit")
    parser.add_argument("--max-steps", type=int, default=None, help="stop after this many steps")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    worker = Worker(settings)
    server = None
    if settings.worker_http_port is not None:
        server = make_server(
            settings.worker_http_host,
            settings.worker_http_port,
            worker.heartbeat,
            settings.worker_health_stale_seconds,
            settings.metrics_token.get_secret_value() if settings.metrics_token else None,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(
        "worker_started",
        extra={"once": args.once, "http_port": server.server_address[1] if server else None},
    )
    try:
        steps = worker.run(stop, once=args.once, max_steps=args.max_steps)
    finally:
        if server:
            server.shutdown()
            server.server_close()
    logger.info("worker_stopped", extra={"steps": steps})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
