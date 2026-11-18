"""A backup and restore drill, run for real against a populated database.

STATUS: WRITTEN BUT NEVER RUN. Nothing below has been verified. Run it, read every failure, and fix
the script or the backup tooling before trusting either.

    python -m loadtest.restore_drill

What it does, in order:
 1. fingerprint the source database (row counts, content checksums, schema, extensions);
 2. back it up with infra/backup/backup.sh (in a throwaway container with the Postgres 16 tools);
 3. delete a private repository *after* the backup, to see what a restore does to it;
 4. restore with infra/backup/restore.sh into a brand-new database in a brand-new container;
 5. fingerprint the restore and compare with step 1, field by field;
 6. start the real API on the restored database and compare its answers with the source's;
 7. check the encrypted provider key with the right and the wrong TOKEN_ENCRYPTION_KEY;
 8. restore an older-schema backup and migrate it forward;
 9. try a truncated and a tampered backup, which must fail safely.

It needs Docker, and the source database from `loadtest.seed` (database cip_load in the compose
Postgres container). Results are printed and written to loadtest/results/restore-drill.json.
"""

import hashlib
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BACKEND = HERE.parent
ROOT = BACKEND.parent
SRC_CONTAINER = "codebaseintelligenceplatform-db-1"
DST_CONTAINER = "cip-restore-target"
IMAGE = "pgvector/pgvector:pg16"
WORK = Path("/tmp/restore-drill")
SRC_DB = "cip_load"
DST_PORT = 55432
checks: list[tuple[str, bool, str]] = []


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def psql(container: str, db: str, sql: str) -> str:
    r = sh(["docker", "exec", container, "psql", "-U", "cip", "-d", db, "-tAc", sql])
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)


def tool(script: str, env: dict[str, str], *args: str) -> tuple[subprocess.CompletedProcess, float]:  # type: ignore[type-arg]
    """Run one of infra/backup/*.sh in a throwaway container that has the Postgres 16 tools."""
    cmd = [
        "docker",
        "run",
        "--rm",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-v",
        f"{ROOT / 'infra/backup'}:/b:ro",
        "-v",
        f"{WORK}:/out",
    ]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE, "bash", f"/b/{script}", *args]
    started = time.monotonic()
    r = sh(cmd)
    return r, time.monotonic() - started


def fingerprint(container: str, db: str) -> dict[str, object]:
    tables = psql(
        container,
        db,
        "select table_name from information_schema.tables "
        "where table_schema='public' and table_type='BASE TABLE' order by 1",
    ).split()
    counts = {t: int(psql(container, db, f"select count(*) from {t}")) for t in tables}
    sums = {
        "files.content": "select md5(coalesce(string_agg(md5(coalesce(content,'')), '' order by id),'')) from files",
        "chunks.text": "select md5(coalesce(string_agg(md5(text), '' order by id),'')) from chunks",
        "chunks.embedding": "select md5(coalesce(string_agg(embedding::text, '' order by id),'')) from chunks",
        "symbols": "select md5(coalesce(string_agg(qualified_name || start_line::text, '' order by id),'')) from symbols",
        "graph_edges": "select md5(coalesce(string_agg(kind || resolution || source_file_id::text, '' order by id),'')) from graph_edges",
        "users": "select md5(coalesce(string_agg(login || github_id::text, '' order by login),'')) from users",
        "sessions": "select md5(coalesce(string_agg(id_hash, '' order by id_hash),'')) from user_sessions",
        "provider_keys": "select md5(coalesce(string_agg(coalesce(key_enc,''), '' order by user_id, slot),'')) from user_providers",
        "audit": "select md5(coalesce(string_agg(action || coalesce(repository,''), '' order by id),'')) from audit_events",
    }
    return {
        "counts": counts,
        "checksums": {k: psql(container, db, q) for k, q in sums.items()},
        "alembic": psql(container, db, "select version_num from alembic_version"),
        "extensions": psql(
            container,
            db,
            "select string_agg(extname || ' ' || extversion, ',' order by extname) from pg_extension",
        ),
        "indexes": psql(
            container,
            db,
            "select md5(string_agg(indexdef, '' order by indexname)) from pg_indexes where schemaname='public'",
        ),
        "constraints": psql(
            container,
            db,
            "select md5(string_agg(conname || contype, '' order by conname)) from pg_constraint where connamespace='public'::regnamespace",
        ),
        "tsv_populated": psql(container, db, "select count(*) from chunks where tsv is not null"),
    }


def api(
    url: str, token: str, body: dict | None = None, method: str | None = None
) -> tuple[int, dict]:  # type: ignore[type-arg]
    req = urllib.request.Request(
        url,
        method=method or ("POST" if body is not None else "GET"),
        data=json.dumps(body).encode() if body is not None else None,
    )
    req.add_header("Cookie", f"cip_session={token}")
    req.add_header("Origin", "http://localhost:3000")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


def start_api(db_url: str, port: int, env_extra: dict[str, str] | None = None) -> subprocess.Popen:  # type: ignore[type-arg]
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ALEMBIC_DATABASE_URL": "",
        "EMBEDDING_PROVIDER": "hash",
        "LOG_LEVEL": "WARNING",
        **(env_extra or {}),
    }
    p = subprocess.Popen(
        [
            str(BACKEND / ".venv/bin/uvicorn"),
            "loadtest.server:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/health", timeout=1)
            return p
        except Exception:
            time.sleep(0.5)
    raise SystemExit("API did not start")


def stop(p: subprocess.Popen) -> None:  # type: ignore[type-arg]
    os.killpg(p.pid, signal.SIGTERM)
    p.wait(timeout=20)


def main() -> None:
    env = os.environ
    enc_key = env["TOKEN_ENCRYPTION_KEY"]
    fixture = json.loads((HERE / "fixture.json").read_text())
    repo_id, token = fixture["repo_id"], fixture["tokens"][0]
    WORK.mkdir(exist_ok=True)
    for f in WORK.glob("*"):
        f.unlink()
    sh(["docker", "rm", "-f", DST_CONTAINER])
    timings: dict[str, float] = {}

    # --- 0. add what a real deployment has beyond the seeded public repository
    print("0. preparing the source: a private repository, a stored provider key, audit events")
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.core.config import get_settings
    from app.db.models import IndexJob, Repository, Snapshot, User, UserRepository
    from app.db.session import get_engine
    from app.services import audit
    from app.services.auth.crypto import TokenCipher
    from app.services.providers import store
    from loadtest import synth
    from loadtest.seed import index_job
    from tests.helpers import FakeGitHub

    settings = get_settings().model_copy(update={"max_files": 5000, "max_chunks": 20_000})
    with Session(get_engine(), expire_on_commit=False) as db:
        owner = db.scalar(select(User).where(User.login == "load000"))
        assert owner is not None
        if db.scalar(select(Repository).where(Repository.name == "drill-private")) is None:
            repo = Repository(
                owner="load",
                name="drill-private",
                url="https://github.com/load/drill-private",
                is_private=True,
                owner_user_id=owner.id,
            )
            db.add(repo)
            db.flush()
            snap = Snapshot(repository_id=repo.id, commit_sha="b" * 40)
            db.add(snap)
            db.flush()
            job = IndexJob(snapshot_id=snap.id, phase="fetch", cursor={}, attempts=0)
            db.add(job)
            db.add(UserRepository(user_id=owner.id, repository_id=repo.id))
            db.commit()
            index_job(db, job, FakeGitHub(files=synth.generate(60, seed=99)), settings)
            audit.record(db, "private.connect", owner.id, repo, {"new": True})
        private_id = str(db.scalar(select(Repository.id).where(Repository.name == "drill-private")))
        cipher = TokenCipher(enc_key)
        cfg = store.validate("chat", "openai", "stored", None, None, "sk-drill-SECRET-0123456789")
        store.save(db, owner.id, "chat", cfg, "sk-drill-SECRET-0123456789", cipher)
        owner_id = str(owner.id)
    owner_token = fixture["tokens"][0]

    # --- 1. fingerprint the source and capture what the source API answers
    print("1. fingerprinting the source")
    src_fp = fingerprint(SRC_CONTAINER, SRC_DB)
    src_url = f"postgresql+psycopg://cip:cip@localhost:5432/{SRC_DB}"
    src_api = start_api(src_url, 8201)
    queries = fixture["queries"][:8]
    src_answers = {}
    try:
        for q in queries:
            code, body = api(
                f"http://localhost:8201/api/repositories/{repo_id}/search",
                token,
                {"query": q, "mode": "vector", "limit": 10},
            )
            src_answers[q] = [h["chunk_id"] for h in body.get("hits", [])] if code == 200 else code
        code, chat_body = api(
            f"http://localhost:8201/api/repositories/{repo_id}/chat",
            token,
            {"question": queries[0]},
        )
        src_chat_cites = [c["path"] + str(c["start_line"]) for c in chat_body.get("citations", [])]
    finally:
        stop(src_api)
    total_rows = sum(src_fp["counts"].values())  # type: ignore[union-attr]
    print(f"   {total_rows:,} rows in {len(src_fp['counts'])} tables")  # type: ignore[arg-type]

    # --- 2. back up
    print("2. backing up with infra/backup/backup.sh")
    r, secs = tool(
        "backup.sh",
        {"DATABASE_URL": f"postgresql://cip:cip@host.docker.internal:5432/{SRC_DB}"},
        "/out/drill.dump",
    )
    timings["backup_seconds"] = round(secs, 2)
    dump = WORK / "drill.dump"
    check(
        "backup script succeeded",
        r.returncode == 0,
        r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr[:200],
    )
    check("a checksum file was written", (WORK / "drill.dump.sha256").exists())
    size_mb = round(dump.stat().st_size / 1e6, 1)
    timings["dump_size_mb"] = size_mb
    db_size = psql(SRC_CONTAINER, SRC_DB, f"select pg_database_size('{SRC_DB}')/1000000.0")
    print(f"   dump {size_mb} MB from a {float(db_size):.0f} MB database in {secs:.1f}s")

    # --- 3. something deleted after the backup
    print("3. deleting the private repository AFTER the backup")
    src_api = start_api(src_url, 8201)
    try:
        code, _ = api(
            f"http://localhost:8201/api/repositories/{private_id}", owner_token, method="DELETE"
        )
    finally:
        stop(src_api)
    gone = psql(
        SRC_CONTAINER, SRC_DB, f"select count(*) from repositories where id = '{private_id}'"
    )
    check(
        "the private repository was deleted from the source after the backup",
        code == 204 and gone == "0",
    )

    # --- 4. restore into a brand-new database on a brand-new server
    print("4. restoring into a new database in a new container with infra/backup/restore.sh")
    sh(
        [
            "docker",
            "run",
            "-d",
            "--name",
            DST_CONTAINER,
            "-e",
            "POSTGRES_USER=cip",
            "-e",
            "POSTGRES_PASSWORD=cip",
            "-e",
            "POSTGRES_DB=postgres",
            "-p",
            f"{DST_PORT}:5432",
            IMAGE,
        ]
    )
    for _ in range(60):
        if sh(["docker", "exec", DST_CONTAINER, "pg_isready", "-U", "cip"]).returncode == 0:
            break
        time.sleep(1)
    time.sleep(2)
    psql(DST_CONTAINER, "postgres", "create database cip_restored")
    dst_url_tool = f"postgresql://cip:cip@host.docker.internal:{DST_PORT}/cip_restored"
    r, secs = tool("restore.sh", {"TARGET_DATABASE_URL": dst_url_tool}, "/out/drill.dump")
    timings["restore_seconds"] = round(secs, 2)
    check(
        "restore script succeeded",
        r.returncode == 0 and "restore finished" in r.stdout,
        f"{secs:.1f}s" if r.returncode == 0 else (r.stderr or r.stdout)[-300:],
    )

    # --- 5. compare
    print("5. comparing the restore with the source as it was at backup time")
    dst_fp = fingerprint(DST_CONTAINER, "cip_restored")
    d_counts, s_counts = dst_fp["counts"], src_fp["counts"]  # type: ignore[assignment]
    check("same tables", set(d_counts) == set(s_counts))  # type: ignore[arg-type]
    diffs = {t: (s_counts[t], d_counts.get(t)) for t in s_counts if s_counts[t] != d_counts.get(t)}  # type: ignore[index, union-attr]
    check(
        "every table has the same row count",
        not diffs,
        str(diffs) if diffs else f"{total_rows:,} rows",
    )
    cs_diff = [k for k in src_fp["checksums"] if src_fp["checksums"][k] != dst_fp["checksums"][k]]  # type: ignore[index, union-attr]
    check(
        "content checksums match (files, chunks, vectors, symbols, graph, users, sessions, keys, audit)",
        not cs_diff,
        f"differ: {cs_diff}" if cs_diff else "all 9 equal",
    )
    check(
        "schema version is the same", src_fp["alembic"] == dst_fp["alembic"], str(dst_fp["alembic"])
    )
    check(
        "the vector extension came back with its version",
        src_fp["extensions"] == dst_fp["extensions"],
        str(dst_fp["extensions"]),
    )
    check("indexes are identical", src_fp["indexes"] == dst_fp["indexes"])
    check("constraints are identical", src_fp["constraints"] == dst_fp["constraints"])
    check(
        "the full-text column is populated for every chunk",
        dst_fp["tsv_populated"] == src_fp["tsv_populated"],
        str(dst_fp["tsv_populated"]),
    )
    resurrected = psql(
        DST_CONTAINER,
        "cip_restored",
        f"select count(*) from repositories where id = '{private_id}'",
    )
    check(
        "KNOWN BEHAVIOUR: the repository deleted after the backup is back in the restore",
        resurrected == "1",
        "a restore resurrects deleted data",
    )

    # --- 6. run the real API on the restored data
    print("6. running the API on the restored database")
    dst_url = f"postgresql+psycopg://cip:cip@localhost:{DST_PORT}/cip_restored"
    out = sh(
        ["uv", "run", "alembic", "current"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": dst_url, "ALEMBIC_DATABASE_URL": ""},
    )
    check(
        "alembic reports the head revision on the restored database",
        "(head)" in (out.stdout + out.stderr),
    )
    out = sh(
        ["uv", "run", "alembic", "check"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": dst_url, "ALEMBIC_DATABASE_URL": ""},
    )
    check(
        "the models and the restored schema agree (alembic check)",
        "No new upgrade operations" in (out.stdout + out.stderr),
    )
    dst_api = start_api(dst_url, 8211)
    try:
        code, me = api("http://localhost:8211/api/me", token)
        check("a session from before the backup still signs in", code == 200, me.get("login", ""))
        code, status = api(f"http://localhost:8211/api/repositories/{repo_id}/status", token)
        check("the indexed repository is ready", code == 200 and status.get("state") == "ready")
        same = 0
        for q in queries:
            code, body = api(
                f"http://localhost:8211/api/repositories/{repo_id}/search",
                token,
                {"query": q, "mode": "vector", "limit": 10},
            )
            same += code == 200 and [h["chunk_id"] for h in body["hits"]] == src_answers[q]
        check(
            "vector search returns the same results as the source",
            same == len(queries),
            f"{same}/{len(queries)} queries identical",
        )
        code, chat_body = api(
            f"http://localhost:8211/api/repositories/{repo_id}/chat",
            token,
            {"question": queries[0]},
        )
        check(
            "a question gets a validated, cited answer",
            code == 200
            and [c["path"] + str(c["start_line"]) for c in chat_body.get("citations", [])]
            == src_chat_cites,
        )
        code, _ = api(f"http://localhost:8211/api/repositories/{private_id}/files", owner_token)
        check("KNOWN BEHAVIOUR: the resurrected private repository is served again", code == 200)
    finally:
        stop(dst_api)

    # --- 7. the encrypted provider key
    print("7. the encrypted provider key")
    from sqlalchemy import create_engine

    from app.core.errors import ProvidersRequired

    with Session(create_engine(dst_url)) as db:
        row = store.get(db, __import__("uuid").UUID(owner_id), "chat")
        assert row is not None
        check(
            "the right TOKEN_ENCRYPTION_KEY reads the stored provider key back",
            store.read_key(row, TokenCipher(enc_key), None) == "sk-drill-SECRET-0123456789",
        )
        other = TokenCipher(__import__("base64").urlsafe_b64encode(os.urandom(32)).decode())
        try:
            store.read_key(row, other, None)
            unreadable = False
        except ProvidersRequired as exc:
            unreadable = "Save it again" in exc.message
        check("a different key cannot read it, and the user is told to save it again", unreadable)

    # --- 8. an older backup, migrated forward
    print("8. an older-schema backup restored and migrated forward")
    sh(
        [
            "docker",
            "exec",
            SRC_CONTAINER,
            "psql",
            "-U",
            "cip",
            "-d",
            "postgres",
            "-c",
            "drop database if exists cip_old",
        ]
    )
    psql(SRC_CONTAINER, "postgres", "create database cip_old")
    old_url = "postgresql+psycopg://cip:cip@localhost:5432/cip_old"
    sh(
        ["uv", "run", "alembic", "upgrade", "0005"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": old_url, "ALEMBIC_DATABASE_URL": ""},
    )
    psql(
        SRC_CONTAINER,
        "cip_old",
        "insert into repositories (id, owner, name, url) values (gen_random_uuid(), 'old', 'repo', 'https://github.com/old/repo')",
    )
    psql(
        SRC_CONTAINER,
        "cip_old",
        "insert into snapshots (id, repository_id, commit_sha, status, file_count, chunk_count) "
        "select gen_random_uuid(), id, repeat('c',40), 'ready', 1, 0 from repositories",
    )
    psql(
        SRC_CONTAINER,
        "cip_old",
        "insert into files (snapshot_id, path, size_bytes, sha256, content) "
        "select id, 'a.py', 10, repeat('0',64), 'print(1)' from snapshots",
    )
    old_before = psql(
        SRC_CONTAINER, "cip_old", "select md5(string_agg(path || content, '')) from files"
    )
    r, _ = tool(
        "backup.sh",
        {"DATABASE_URL": "postgresql://cip:cip@host.docker.internal:5432/cip_old"},
        "/out/old.dump",
    )
    check("backup of the older schema", r.returncode == 0)
    psql(DST_CONTAINER, "postgres", "create database cip_old_restored")
    r, _ = tool(
        "restore.sh",
        {
            "TARGET_DATABASE_URL": f"postgresql://cip:cip@host.docker.internal:{DST_PORT}/cip_old_restored"
        },
        "/out/old.dump",
    )
    check("restore of the older schema", r.returncode == 0)
    new_url = f"postgresql+psycopg://cip:cip@localhost:{DST_PORT}/cip_old_restored"
    up = sh(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": new_url, "ALEMBIC_DATABASE_URL": ""},
    )
    check(
        "alembic upgrade head runs on the restored older database",
        up.returncode == 0,
        (up.stderr or "")[-200:] if up.returncode else "",
    )
    after = psql(
        DST_CONTAINER, "cip_old_restored", "select md5(string_agg(path || content, '')) from files"
    )
    check("the data survived the migration", after == old_before)
    check(
        "new columns got their defaults",
        psql(DST_CONTAINER, "cip_old_restored", "select is_private from repositories") == "f",
    )
    check(
        "it is at the head revision",
        "(head)"
        in sh(
            ["uv", "run", "alembic", "current"],
            cwd=BACKEND,
            env={**os.environ, "DATABASE_URL": new_url, "ALEMBIC_DATABASE_URL": ""},
        ).stdout
        + "",
    )

    # --- 9. bad backups must fail safely
    print("9. a truncated and a tampered backup")
    data = dump.read_bytes()
    (WORK / "truncated.dump").write_bytes(data[: len(data) // 2])
    (WORK / "truncated.dump.sha256").write_text(
        hashlib.sha256(data).hexdigest() + "  /out/truncated.dump\n"
    )
    psql(DST_CONTAINER, "postgres", "create database cip_bad1")
    r, _ = tool(
        "restore.sh",
        {"TARGET_DATABASE_URL": f"postgresql://cip:cip@host.docker.internal:{DST_PORT}/cip_bad1"},
        "/out/truncated.dump",
    )
    tables = psql(
        DST_CONTAINER,
        "cip_bad1",
        "select count(*) from information_schema.tables where table_schema='public'",
    )
    check(
        "a truncated backup is refused",
        r.returncode != 0,
        "checksum mismatch caught first" if "FAILED" in r.stdout + r.stderr else "",
    )
    check("and the target database is left empty", tables == "0")
    tampered = bytearray(data)
    tampered[len(tampered) // 2] ^= 0xFF
    (WORK / "tampered.dump").write_bytes(bytes(tampered))
    (WORK / "tampered.dump.sha256").write_text(
        hashlib.sha256(data).hexdigest() + "  /out/tampered.dump\n"
    )
    psql(DST_CONTAINER, "postgres", "create database cip_bad2")
    r, _ = tool(
        "restore.sh",
        {"TARGET_DATABASE_URL": f"postgresql://cip:cip@host.docker.internal:{DST_PORT}/cip_bad2"},
        "/out/tampered.dump",
    )
    tables = psql(
        DST_CONTAINER,
        "cip_bad2",
        "select count(*) from information_schema.tables where table_schema='public'",
    )
    check(
        "a tampered backup is refused by its checksum",
        r.returncode != 0 and "FAILED" in r.stdout + r.stderr,
    )
    check("and that target is left empty too", tables == "0")
    # without a checksum file the structure check alone must still catch a truncated archive
    (WORK / "nosum.dump").write_bytes(data[: len(data) // 2])
    psql(DST_CONTAINER, "postgres", "create database cip_bad3")
    r, _ = tool(
        "restore.sh",
        {"TARGET_DATABASE_URL": f"postgresql://cip:cip@host.docker.internal:{DST_PORT}/cip_bad3"},
        "/out/nosum.dump",
    )
    tables = psql(
        DST_CONTAINER,
        "cip_bad3",
        "select count(*) from information_schema.tables where table_schema='public'",
    )
    check(
        "a truncated backup with no checksum file is still refused (and a warning is printed)",
        r.returncode != 0 and tables == "0" and "integrity is unchecked" in r.stderr,
    )

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "restore-drill.json").write_text(
        json.dumps(
            {
                "rows": total_rows,
                "tables": len(src_fp["counts"]),
                **timings,  # type: ignore[arg-type]
                "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in checks],
            },
            indent=1,
        )
    )
    sh(["docker", "rm", "-f", DST_CONTAINER])
    sh(
        [
            "docker",
            "exec",
            SRC_CONTAINER,
            "psql",
            "-U",
            "cip",
            "-d",
            "postgres",
            "-c",
            "drop database if exists cip_old",
        ]
    )
    raise SystemExit(0 if passed == len(checks) else 1)


if __name__ == "__main__":
    main()
