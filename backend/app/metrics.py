"""Prometheus metrics: how the service is doing, never what anyone asked or stored.

Every label comes from a small fixed set (route templates, phase names, provider kinds, error
codes). No label ever holds a user id, repository name, path, question or key, so the output is
safe to hand to a monitoring system and cannot leak content; a test pins that.

The API serves these at `/metrics` (bearer `METRICS_TOKEN`, absent unless configured) and the
worker serves them from its own small HTTP server, because the two are separate processes.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import IndexJob

# The classic text format: understood by every Prometheus version (the library's default now
# declares 1.0.0, which older servers do not expect). The output itself is compatible.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
REGISTRY = CollectorRegistry()

_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
_SLOW = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60)

HTTP_REQUESTS = Counter(
    "cip_http_requests_total",
    "HTTP requests by method, route template and status.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "cip_http_request_duration_seconds",
    "HTTP request duration by method and route template.",
    ["method", "route"],
    buckets=_FAST,
    registry=REGISTRY,
)
INDEX_STEPS = Counter(
    "cip_index_steps_total",
    "Indexing job steps by phase and outcome (ok, error, lease_lost, busy).",
    ["phase", "outcome"],
    registry=REGISTRY,
)
INDEX_STEP_DURATION = Histogram(
    "cip_index_step_duration_seconds",
    "Duration of one indexing job step by phase.",
    ["phase"],
    buckets=_SLOW,
    registry=REGISTRY,
)
INDEX_JOB_FAILURES = Counter(
    "cip_index_job_failures_total",
    "Indexing jobs that failed for good, by error code.",
    ["code"],
    registry=REGISTRY,
)
PROVIDER_REQUESTS = Counter(
    "cip_provider_requests_total",
    "Calls to embedding and chat providers by provider, operation and outcome (ok or error code).",
    ["provider", "operation", "outcome"],
    registry=REGISTRY,
)
PROVIDER_DURATION = Histogram(
    "cip_provider_request_duration_seconds",
    "Duration of calls to embedding and chat providers.",
    ["provider", "operation"],
    buckets=_SLOW,
    registry=REGISTRY,
)

_ACTIVE_PHASES = ("fetch", "parse", "embed", "graph")


@contextmanager
def observe_provider(provider: str, operation: str) -> Iterator[None]:
    """Time one call to a provider and count its outcome. The outcome label is "ok" or the typed
    error code (a small fixed set), never the provider's message."""
    started = time.monotonic()
    outcome = "ok"
    try:
        yield
    except AppError as exc:
        outcome = exc.code
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        PROVIDER_REQUESTS.labels(provider, operation, outcome).inc()
        PROVIDER_DURATION.labels(provider, operation).observe(time.monotonic() - started)


class QueueCollector(Collector):
    """Gauges read from the database at scrape time, so they are right whichever process (API or
    worker) is scraped and survive restarts.

    `cip_index_jobs{phase,state}`: waiting (due now, nobody holds it), running (a lease is held and
    no error is recorded) and backing_off (waiting out a retry delay).
    `cip_index_oldest_waiting_seconds`: how long the job that has waited longest has been waiting
    (the queue lag). `cip_index_failed_jobs`: how many jobs are in the failed state."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def collect(self) -> Iterator[Metric]:
        try:
            with self._session_factory() as db:
                counts, lag, n_failed = _queue_numbers(db)
        except Exception:  # noqa: BLE001 - a scrape must survive a database blip
            return
        jobs = GaugeMetricFamily(
            "cip_index_jobs", "Active indexing jobs by phase and state.", labels=["phase", "state"]
        )
        for phase in _ACTIVE_PHASES:
            for state in ("waiting", "running", "backing_off"):
                jobs.add_metric([phase, state], counts.get((phase, state), 0))
        oldest = GaugeMetricFamily(
            "cip_index_oldest_waiting_seconds",
            "Age of the longest-waiting job that is due to run (queue lag).",
        )
        oldest.add_metric([], lag)
        failed = GaugeMetricFamily("cip_index_failed_jobs", "Indexing jobs in the failed state.")
        failed.add_metric([], n_failed)
        yield jobs
        yield oldest
        yield failed


def _queue_numbers(db: Session) -> tuple[dict[tuple[str, str], int], float, int]:
    now = func.now()
    state = case(
        (IndexJob.lease_until.is_(None), "waiting"),
        (IndexJob.lease_until <= now, "waiting"),
        (IndexJob.error_code.is_not(None), "backing_off"),
        else_="running",
    )
    rows = db.execute(
        select(IndexJob.phase, state, func.count())
        .where(IndexJob.phase.in_(_ACTIVE_PHASES))
        .group_by(IndexJob.phase, state)
    ).all()
    counts = {(r[0], r[1]): int(r[2]) for r in rows}
    lag = db.scalar(
        select(func.max(func.extract("epoch", now - IndexJob.updated_at))).where(
            IndexJob.phase.in_(_ACTIVE_PHASES),
            (IndexJob.lease_until.is_(None)) | (IndexJob.lease_until <= now),
        )
    )
    n_failed = db.scalar(select(func.count()).where(IndexJob.phase == "failed")) or 0
    return counts, float(lag or 0.0), int(n_failed)


def install_queue_collector() -> None:
    """Register the database-backed gauges once (idempotent)."""
    from app.db.session import get_engine

    if getattr(install_queue_collector, "_done", False):
        return

    def make_session() -> Session:
        return Session(get_engine())

    REGISTRY.register(QueueCollector(make_session))
    install_queue_collector._done = True  # type: ignore[attr-defined]


def render() -> bytes:
    install_queue_collector()
    return generate_latest(REGISTRY)
