"""Run the mixed workload at several concurrency levels against one server configuration.

    python -m loadtest.run_load --label default --pool 3 --overflow 2 --levels 1,5,10,25,50,100

Starts the real API (`loadtest.server`), warms it up, then for each level runs k6 (in Docker) for a
fixed time while sampling the API process and the database. Writes loadtest/results/<label>.json.
Everything runs on one machine, so the load generator, the API and Postgres compete for the same
cores: the numbers are for comparing configurations and finding the first bottleneck, not for
predicting production.
"""

import argparse
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BACKEND = HERE.parent
DB_CONTAINER = "codebaseintelligenceplatform-db-1"
PORT = 8200


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def start_server(env_extra: dict[str, str]) -> subprocess.Popen:
    env = {**os.environ, **env_extra}
    log = open("/tmp/load-api.log", "w")
    proc = subprocess.Popen(
        [
            str(BACKEND / ".venv/bin/uvicorn"),
            "loadtest.server:app",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    raise SystemExit("server did not start")


def stop(proc: subprocess.Popen) -> None:
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=20)


class Sampler(threading.Thread):
    """CPU and memory of the API process, database connections, and the database container."""

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid, self.stop_flag = pid, threading.Event()
        self.api_cpu, self.api_rss, self.db_conns, self.db_cpu = [], [], [], []

    def run(self) -> None:
        tick = 0
        while not self.stop_flag.is_set():
            out = sh(["ps", "-o", "%cpu=,rss=", "-p", str(self.pid)]).stdout.split()
            if len(out) == 2:
                self.api_cpu.append(float(out[0]))
                self.api_rss.append(int(out[1]) / 1024)
            if tick % 2 == 0:
                c = sh(
                    [
                        "docker",
                        "exec",
                        DB_CONTAINER,
                        "psql",
                        "-U",
                        "cip",
                        "-d",
                        "cip_load",
                        "-tAc",
                        "select count(*) from pg_stat_activity where datname='cip_load' and state is not null",
                    ]
                ).stdout.strip()
                if c.isdigit():
                    self.db_conns.append(int(c))
                s = (
                    sh(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", DB_CONTAINER])
                    .stdout.strip()
                    .rstrip("%")
                )
                try:
                    self.db_cpu.append(float(s))
                except ValueError:
                    pass
            tick += 1
            self.stop_flag.wait(1.0)


def k6(vus: int, duration: int, label: str) -> dict:
    out = Path("/tmp/k6out")
    out.mkdir(exist_ok=True)
    summary = out / f"{label}.json"
    summary.unlink(missing_ok=True)
    sh(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{HERE / 'fixture.json'}:/fixture.json:ro",
            "-v",
            f"{HERE / 'load.js'}:/load.js:ro",
            "-v",
            f"{out}:/out",
            "-e",
            f"VUS={vus}",
            "-e",
            f"DURATION={duration}s",
            "grafana/k6:latest",
            "run",
            "--summary-export",
            f"/out/{label}.json",
            "--quiet",
            "/load.js",
        ]
    )
    return json.loads(summary.read_text())


def trend(m: dict, key: str) -> dict:
    t = m.get(key) or {}
    return {k: round(t.get(k, 0), 1) for k in ("med", "p(95)", "p(99)", "max")}


def level(vus: int, duration: int, pid: int, label: str) -> dict:
    sampler = Sampler(pid)
    sampler.start()
    raw = k6(vus, duration, f"{label}-{vus}")
    sampler.stop_flag.set()
    sampler.join(timeout=5)
    m = raw["metrics"]
    endpoints = [
        "list",
        "status",
        "files",
        "file",
        "stats",
        "graph",
        "impact",
        "search_vector",
        "search_keyword",
        "chat",
    ]
    reqs = m["http_reqs"]["count"]
    return {
        "vus": vus,
        "requests": reqs,
        "rps": round(m["http_reqs"]["rate"], 1),
        "failed_requests": int(round(m["http_req_failed"]["value"] * reqs)),
        "error_rate_pct": round(m["http_req_failed"]["value"] * 100, 2),
        "overall_ms": trend(m, "http_req_duration"),
        "per_endpoint_ms": {e: trend(m, f"http_req_duration{{name:{e}}}") for e in endpoints},
        "api_cpu_pct_avg": round(sum(sampler.api_cpu) / max(1, len(sampler.api_cpu)), 1),
        "api_cpu_pct_peak": round(max(sampler.api_cpu or [0]), 1),
        "api_rss_mb_peak": round(max(sampler.api_rss or [0]), 1),
        "db_connections_peak": max(sampler.db_conns or [0]),
        "db_cpu_pct_peak": round(max(sampler.db_cpu or [0]), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--pool", type=int, default=3)
    ap.add_argument("--overflow", type=int, default=2)
    ap.add_argument("--levels", default="1,5,10,25,50,100")
    ap.add_argument("--duration", type=int, default=20)
    a = ap.parse_args()
    env = {"DB_POOL_SIZE": str(a.pool), "DB_MAX_OVERFLOW": str(a.overflow)}
    proc = start_server(env)
    try:
        k6(4, 6, f"{a.label}-warmup")
        results = []
        for vus in [int(v) for v in a.levels.split(",")]:
            r = level(vus, a.duration, proc.pid, a.label)
            results.append(r)
            print(
                f"{a.label:>10} vus={vus:>3} rps={r['rps']:>7} p50={r['overall_ms']['med']:>7}ms p95={r['overall_ms']['p(95)']:>8}ms "
                f"err={r['error_rate_pct']:>5}% api_cpu={r['api_cpu_pct_avg']:>5}% db_conn={r['db_connections_peak']}",
                flush=True,
            )
        log = Path("/tmp/load-api.log").read_text()
        out = {
            "label": a.label,
            "pool": a.pool,
            "overflow": a.overflow,
            "duration_s_per_level": a.duration,
            "server_log_errors": log.count("Traceback"),
            "pool_timeouts": log.count("QueuePool limit"),
            "levels": results,
        }
        (HERE / "results").mkdir(exist_ok=True)
        (HERE / "results" / f"{a.label}.json").write_text(json.dumps(out, indent=1))
    finally:
        stop(proc)


if __name__ == "__main__":
    main()
