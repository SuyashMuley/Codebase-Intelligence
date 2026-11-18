"""Build the database a load test (and the restore drill) runs against.

    python -m loadtest.seed --files 600 --users 50 --out loadtest/fixture.json

It indexes the synthetic repository with the real pipeline (fetch, parse, embed, graph), creates
users with live sessions, and writes the session tokens, search queries and file paths a load
generator needs. The tokens are real credentials for that database: keep the file out of git.
"""

import argparse
import json
import time
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import IndexJob, Repository, Snapshot, User, UserRepository
from app.db.session import get_engine
from app.services.auth import sessions
from app.services.indexing import runner
from loadtest import synth
from tests.helpers import FakeGitHub


def index_job(db: Session, job: IndexJob, fake: FakeGitHub, settings, embedder=None):  # type: ignore[no-untyped-def]
    """Run a job through the real pipeline to its end. Returns (seconds per phase, total)."""
    timings: dict[str, float] = {}
    started = last = time.monotonic()
    phase = "fetch"
    while True:
        runner.tick(db, job.id, fake, settings, embedder)
        db.refresh(job)
        now = time.monotonic()
        timings[phase] = timings.get(phase, 0.0) + (now - last)
        last, phase = now, job.phase
        if job.phase in ("done", "failed"):
            break
    if job.phase == "failed":
        raise SystemExit(f"indexing failed: {job.error_code}: {job.error}")
    return timings, time.monotonic() - started


def seed(n_files: int, n_users: int, out: Path) -> dict:
    settings = get_settings().model_copy(update={"max_files": 5000, "max_chunks": 20_000})
    files = synth.generate(n_files)
    fake = FakeGitHub(files=files)
    with Session(get_engine(), expire_on_commit=False) as db:
        repo = Repository(owner="load", name="synthetic", url="https://github.com/load/synthetic")
        db.add(repo)
        db.flush()
        snap = Snapshot(repository_id=repo.id, commit_sha="a" * 40)
        db.add(snap)
        db.flush()
        job = IndexJob(snapshot_id=snap.id, phase="fetch", cursor={}, attempts=0)
        db.add(job)
        db.commit()
        timings, total = index_job(db, job, fake, settings)

        users, tokens = [], []
        for i in range(n_users):
            u = User(github_id=900_000 + i, login=f"load{i:03d}", avatar_url=None)
            db.add(u)
            db.flush()
            db.add(UserRepository(user_id=u.id, repository_id=repo.id))
            users.append(u)
        db.commit()
        for u in users:
            tokens.append(sessions.create_session(db, u.id, get_settings()))
        db.refresh(snap)
        fixture = {
            "repo_id": str(repo.id),
            "tokens": tokens,
            "queries": synth.queries(),
            "paths": [p for p in files if p.endswith(".py")][:: max(1, n_files // 100)],
            "files": n_files,
            "chunks": snap.chunk_count,
            "index_seconds": {k: round(v, 2) for k, v in timings.items()}
            | {"total": round(total, 2)},
        }
    out.write_text(json.dumps(fixture))
    return fixture


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=600)
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--out", type=Path, default=Path("loadtest/fixture.json"))
    a = ap.parse_args()
    f = seed(a.files, a.users, a.out)
    print({k: v for k, v in f.items() if k not in ("tokens", "queries", "paths")})
    print(uuid.UUID(f["repo_id"]))
