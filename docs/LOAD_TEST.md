# Load test

Measured 2026-11-18 on one Apple M4 (10 cores, 24 GB), with the load generator (k6 in Docker), the API
(one `uvicorn` process) and PostgreSQL 16 (Docker, default settings) **all on that machine**. They compete for
the same cores, so these numbers are for comparing configurations and finding the first bottleneck. **They do
not predict production**: Render's free instance has a fraction of one core and Neon adds network latency.

## What was measured

The real API against a synthetic repository sized to the app's own cap (1,250 files, 5,980 chunks of the
6,000 allowed), 50 users with live sessions, and a closed-loop mixed workload (users send the next request as
soon as the last one finishes) held for 20 seconds per level: browse (list, status, files, one file, stats),
graph, impact, search by meaning (about 12%), search by keyword, and ask (8%). Three things are **not** the
real thing:
- **The chat model is a fast fake** (it quotes the first usable line of the first excerpt), so questions measure
  our retrieval, prompt building and quote validation, not a model's 10 to 30 seconds.
- **Embeddings are the offline hash embedder**, so search measures the database, not Cloudflare's 150 to 270 ms.
- **The rate limits were raised** so they were not what was measured (they have their own tests).

Tools and procedure: [backend/loadtest/README.md](../backend/loadtest/README.md). Raw results are in
`backend/loadtest/results/`.

## The finding: the API stalled completely at 50 concurrent users

With the shipped settings (a pool of 3 connections plus 2 spare), throughput was steady at about 120 requests
a second up to 25 users, then at 50 users **the API stopped serving**: 1.7 requests a second, every response
taking 30 seconds, 78% of requests failing, while the API process sat at 3% CPU and the database at 4%.

| Concurrent users | Before the fix | After the fix |
| --- | --- | --- |
| 1 | 49 req/s, p95 101 ms | 48 req/s, p95 71 ms |
| 5 | 123 req/s, p95 103 ms | 108 req/s, p95 126 ms |
| 10 | 122 req/s, p95 172 ms | 116 req/s, p95 179 ms |
| 25 | 116 req/s, p95 380 ms | 113 req/s, p95 369 ms |
| **50** | **1.7 req/s, 78% errors, 30 s responses** | **112 req/s, 0% errors, p95 692 ms** |
| **100** | **0.8 req/s, 98% errors** | **109 req/s, 0% errors, p95 1.3 s** |
| 200 | not run | 104 req/s, 0% errors, p95 2.6 s |

(Shipped pool of 3+2 in both columns. A pool of 10+10 survived 50 users before the fix and stalled at 100, so
the failure point moved with the pool size, as expected of a starvation deadlock.)

**Cause.** FastAPI runs each synchronous dependency, each synchronous endpoint and the validation of the response
as separate tasks on a shared pool of 40 worker threads. A request kept its database connection between those
tasks, so it held a connection while waiting for a thread. Once enough requests did, every connection was held
by a request waiting for a thread and every thread was waiting for a connection: nothing moved until the pool
timed out (80 pool-timeout errors in the log). The database was idle throughout, which is how it was recognised.
The same would have happened in production with a burst of a few dozen simultaneous requests (a page load sends
several), not only in a synthetic test.

**Fix.** A connection is never held when a worker-thread task ends: dependencies that only read end their
transaction (`release()` in `app/api/deps.py`), every synchronous endpoint is wrapped so its transaction ends
before the response is validated (`ReleasingRouter` in `app/core/dbrelease.py`; every router uses it, and a test
checks that none is a plain `APIRouter`), and a session is closed on its own small executor so closing can never
queue behind request code (`app/db/session.py`). The pool timeout dropped from 30 to 10 seconds.
**Regression test:** `tests/integration/test_pool_starvation.py` (a pool of 2, four worker threads, 32 clients
at once). Without the fix it fails after 74 to 86 seconds of pool timeouts; with it, it passes in under a second.
Undoing either of the first two parts of the fix makes it fail; the third part (the separate close executor) is
a safety net the test does not exercise, and is pinned by its own unit test.

## What else the numbers show

- **A single API process is CPU-bound at roughly 110 to 125 requests a second here** (the process sits at about
  100% of one core). That is Python's one-core limit for this workload on a fast machine, not the database
  (peak database CPU was a small fraction of the cores it could use). More capacity would need more processes,
  and the in-memory rate limiter assumes one (see the threat model).
- **Past that point latency grows with the number of users and nothing breaks**: with a closed loop, 200 users
  means about 2 seconds per request at the same throughput. There is no load shedding: requests queue instead of
  being refused.
- **Memory was steady at about 185 to 195 MB** for the API process throughout.

## What this does not tell you

- **Not production numbers** (see the top). Real latency adds the network to the database, to GitHub and to the
  model providers, and the free host is much slower than this machine.
- **Only one machine, one run per level, 20 seconds each**: no confidence intervals, no repeats. Treat differences
  of a few percent as noise.
- **A pool of 10+10 was measured only before the fix** (the run after it was stopped). The shipped pool of 3+2
  is the configuration that matters and is fully measured on both sides of the fix.
- **Indexing speed was not load-tested**: seeding the 5,980-chunk repository took 13.3 seconds with the offline
  embedder (fetch 0.8 s, parse 8.8 s, embed 1.5 s, graph 2.3 s); with a real embedding service the embed phase
  is dominated by its latency and quota (see the M5 notes).
- **Long model calls were not exercised**: a real chat request holds a worker thread for up to 28 seconds, so many
  at once could still use up the 40 threads and starve everything else. They are limited per user (5 a minute,
  25 a day) but nothing limits them across users.
