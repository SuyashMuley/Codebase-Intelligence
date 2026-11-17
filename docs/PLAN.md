# Codebase Intelligence Platform: Plan and Tasks

Last updated: 2026-11-17

## 1. Scope and fixed decisions

The MVP is a web app that ingests one pinned commit of a public GitHub repo, parses it with Tree-sitter, builds a static dependency graph, and answers questions with validated file and line citations. It runs on free tiers with no always-on worker.

**In scope**
- Connect a public repo by URL, pin a commit SHA, ingest, and re-index when the default branch moves.
- Parse Python, JavaScript, TypeScript and Go; extract definitions, imports, and statically resolvable calls.
- Hybrid retrieval (Postgres full-text plus pgvector, fused with RRF) and graph expansion.
- Chat with validated citations, and change-impact analysis that separates static facts from AI suggestions.
- Sign in with GitHub (GitHub App user authorization). Each user sees and queries only the repositories they connected.
- Dashboard: overview, ingestion progress, file explorer, React Flow graph, chat, impact panel, stats. Each feature milestone ships its own UI, so there is a working end-to-end flow from M3 onward.

**Out of scope for the MVP**
- Private repos (planned in M9), multi-instance scale and workers (planned in M10), streaming chat, per-file incremental re-indexing, Go call edges, languages beyond the four.

**Fixed decisions**

| Concern | Decision |
| --- | --- |
| LLM | OpenRouter, OpenAI-compatible client, model ID in env, one fallback model |
| Embeddings | Hugging Face Inference API, `bge-small-en-v1.5` (384 dims), model ID in env |
| Tests and CI | Stub LLM and a deterministic hash embedder, so no network or keys |
| Background work | None. A resumable job state machine advanced by `tick` calls |
| Hosting | Vercel (frontend), Render free (API), Neon free (Postgres plus pgvector) |
| Auth | GitHub App user authorization, server-side sessions in an HttpOnly cookie, GitHub tokens encrypted at rest. Public repos only until M9. |
| Never | Execute repo code, store detected secrets, trust repo text as instructions |

**Success criteria**
- A public repo up to the quota size indexes end to end on the free tiers.
- Every cited path and line range in an answer exists in the retrieved chunks (enforced and tested).
- A reproducible script reports Recall@k and MRR for keyword-only vs hybrid on a labeled set. Only measured numbers go in the README.

See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams, schema, API contracts, trade-offs and the security model.

## 2. Milestones and tasks

Eight MVP milestones (M1 to M8) and two post-MVP ones (M9, M10), each ending with a working, tested slice. After each one: what was built, how to run it, test results, known limits, next step. M1 to M5 give a fully tested backend with real benchmark numbers; the UI grows with each milestone from M3.

### M1. Skeleton and URL validation
- [x] Monorepo layout, `pyproject.toml`, Ruff and mypy config
- [x] `docker-compose.yml` with Postgres plus pgvector, backend container
- [x] FastAPI app factory, structured JSON logging, request IDs, error envelope
- [x] Settings via env, explicit CORS, `/health` and `/ready`
- [x] Alembic setup and the first migration (all eight tables, extensions)
- [x] `POST` and `GET /api/repositories` with GitHub URL validation
- [x] Tests: URL validation (valid, malformed, non-GitHub, traversal tricks), error envelope, readiness
- [x] GitHub Actions CI: lint, type check, tests against a Postgres service

**Exit:** `docker compose up` serves OpenAPI, a repo can be registered, CI is green.

**Status: done locally (59 tests, ruff, mypy strict, Docker run verified).** The CI workflow has not yet run on GitHub, so "CI is green" is unconfirmed.

**Deviations:** added `GET /api/repositories/{id}`; re-registering an existing repo returns 200 instead of 201; per-IP rate limiting moved to M2 with the ingestion quotas; the pgvector HNSW index moves to M5.

### M2. Resumable ingestion
- [x] GitHub client: resolve ref to SHA, download tarball, honor rate-limit headers, optional token
- [x] Filters: size caps, binary detection, vendored and generated paths, language detection
- [x] Snapshot uniqueness on (repo, commit); `index`, `index/tick`, `status` endpoints
- [x] Job state machine with lease, cursor, attempts, and error capture
- [x] Quotas: tarball size, file count, ingestions per client per day
- [x] Tests: duplicate commit, oversize repo, interrupted tick then resume, expired lease, GitHub errors (mocked)

**Exit:** a small public repo reaches the parse phase, and killing the process mid-job loses nothing.

**Status: done locally** (170 tests total, ruff, mypy strict). Also verified by hand against real GitHub: `octocat/Hello-World` (README only, 0 source files kept) and `pallets/markupsafe` (12 Python files, 29 KB kept, 35 files counted as unsupported). There is no automated test that hits the network, and CI has still not run on GitHub.

**Deviations from the plan**
- The fetch phase is atomic per tick: the tarball is streamed and all rows are written in one transaction, and a failure rolls everything back and retries from the start. Cursor-based partial progress is not used for fetch (a tarball can't be resumed mid-stream). The generic cursor and resume machinery is implemented and tested for the later multi-step phases (parse, embed) with a test handler.
- Leases use compare-and-set on `lease_until` instead of `SELECT ... FOR UPDATE SKIP LOCKED`, so no row lock is held while a tick runs. A tick's result is only written if it still owns its lease.
- Migration `0002` adds `snapshots.client_key` (hashed IP, for the quota) and `index_jobs.error_code`.
- Only files in supported languages are stored. Other files are counted in the skip statistics but get no row. Vendored, unsafe-path and secret files are counted, never stored.
- Quotas are a rolling 24 hours, not calendar days. Rate limiting is in-process per client IP, which is correct for a single instance only.
- Unexpected exceptions are retried like transient errors, bounded by `max_attempts`.

**Known limitations**
- Phases after fetch have no handlers yet, so a job stops in `parse` and `tick` reports `phase_not_implemented:parse`.
- Secret scanning of file contents is M4. M2 only skips secret-looking file names.


### M3. Sign-in with GitHub, ownership, and the first end-to-end UI
Design: [Authentication and ownership](ARCHITECTURE.md#authentication-and-ownership-m3). One-time setup you do yourself: [GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md). The backend work is built and tested against a fake GitHub, so it does not wait on the setup; the real login is verified last.

Backend
- [x] Migration: `users`, `sessions`, `github_credentials`, `user_repositories`; `snapshots.requested_by`
- [x] GitHub App user-authorization flow: login (state and PKCE), callback (code exchange, profile fetch, user upsert), logout, `GET /api/me`
- [x] Server-side sessions: random 256-bit ID stored hashed, absolute and idle expiry, HttpOnly Secure SameSite=Lax cookie, new session ID at login
- [x] CSRF defense on state-changing routes: Origin check on top of SameSite
- [x] GitHub tokens encrypted at rest; refreshed before expiry; a revoked or invalid token returns 401 `reauth_required`
- [x] Auth dependency on every `/api` route except the auth endpoints; routes scoped through `user_repositories`, with 404 for repositories the caller has not connected
- [x] `POST /api/repositories` links the repository to the caller; `GET /api/repositories` lists only theirs
- [x] GitHub client takes the caller's token through a token provider; the platform `GITHUB_TOKEN` stays an optional fallback
- [x] Quotas and the index rate limit keyed by user instead of IP
- [x] Tests: unauthenticated requests denied on every protected route; a second user gets 404 on every repository route; login with a fake GitHub (state mismatch, replayed code, expired session, session fixation); CSRF rejection; token refresh and revocation; tokens never in responses or logs

Frontend (thin vertical slice)
- [x] Next.js, TypeScript, Tailwind dark theme, typed API client with credentials, TanStack Query; `/api` proxied to the backend by Next rewrites so the browser sees one origin
- [x] Pages: sign in, repository list with connect form, repository page with an Index button, tick driver, progress and error display, and a "waking backend" state
- [x] Frontend tests for the auth gate and the tick driver; lint and typecheck added to CI

**Exit:** you can sign in with GitHub, connect a public repo, index it, and watch fetch progress in the browser; a second account cannot see or tick it.

**Status: built and verified locally, except the real GitHub login.**
- Backend: 278 tests (ruff, mypy strict). Nine security properties were mutation-tested, each broken in turn and caught by a test: ownership check, CSRF guard, OAuth state check, session rotation at login, open-redirect guard, token deletion at logout, idle timeout, absolute expiry, and token encryption at rest.
- Frontend: 88 tests, lint, typecheck and a production build pass, with a CI job added.
- End to end, through the real Next proxy and a headless browser, against a local fake of GitHub's OAuth endpoints: sign-in (with PKCE), HttpOnly session cookie, `/api/me`, CSRF enforced through the proxy, connecting a repository with the form (a real browser's `Origin` header passes), listing, error states, sign-out, and a phone-width layout.
- Not verified: a login against the real GitHub App (it needs your credentials; see GITHUB_APP_SETUP.md), and indexing from the browser after a real sign-in. Indexing itself was verified against real GitHub in M2, but the fake provider cannot serve tarballs.

**Deviations from the plan**
- Migration `0003` drops `snapshots.client_key` (added in M2) in favour of `snapshots.requested_by`, a foreign key to the user.
- Rate limits are per user for repository routes, with a per-IP backstop on every `/api` route and a stricter per-IP limit on the sign-in endpoints.
- Logout deletes the user's stored GitHub tokens as well as the session, so the server holds no tokens for signed-out users.
- Added a "Sign in again" notice for the case where GitHub revokes a token while the session is still valid.
- Added a GitHub repository picker (`GET /api/github/repos` and a "From your GitHub account" list with one-click Connect). It lists only public repositories the user owns, using the public listing endpoint, so it needs no extra permissions and cannot return private repositories. Organization repositories are not listed; they can still be connected by URL.
- Sessions, GitHub tokens and ownership are enforced as planned; the `__Host-` cookie prefix is used whenever cookies are Secure.

**Known limitations**
- The sign-in flow depends on GitHub honoring PKCE parameters and on a GitHub App user token reading public repositories with no extra permissions. Both are expected but unconfirmed until a real login.
- Refresh tokens are single-use, so refreshes are serialized with a row lock. This is tested at the service level, not under real concurrent load.
- The frontend has no indexing-progress end-to-end test against a real job yet; the tick driver is unit-tested with a mocked API.

**Still to verify with a real login:** that a GitHub App user token with no extra permissions can read public repositories and download their tarballs. I expect it can but have not confirmed it.

### M4. Parsing, chunking, secret handling, and the file explorer
- [x] Tree-sitter extractors for Python, JS, TS (and TSX/JSX) and Go: symbols, imports, call sites
- [x] Syntax-aware chunker with a size fallback and exact line ranges
- [x] Secret scanner; secrets are redacted before anything is stored
- [x] Resumable `parse` phase (bounded per tick, idempotent), chunk-count quota
- [x] Fixture repos per language, plus this project's own source as a real-code fixture
- [x] Tests: AST extraction, import extraction, chunk boundaries and line numbers, secret redaction, resumability
- [x] API: file list, file detail with symbols, indexing statistics
- [x] UI: file explorer, Monaco viewer with symbol outline and line highlighting, statistics panel

**Exit:** a snapshot has files, symbols and chunks with correct line ranges.

**Status: built and verified.**
- Backend: 922 tests; frontend: 167 tests; ruff, mypy strict, lint, typecheck and production builds pass; the Docker image builds and parses all four languages on Linux.
- Chunking guarantees are property-tested on 300 random inputs and on real code: every non-blank line is in exactly one chunk, chunk text is the exact cited lines, chunks start and end on code, and sizes are bounded.
- Python symbols are cross-checked against the standard library's `ast` for every Python file in this repository and in four public repositories (the only difference: Tree-sitter keeps an indented trailing comment inside the function it follows).
- Run on 8 public repositories (551 files; Python: click, requests, flask, httpx; JavaScript: express; TypeScript: ky; Go: uuid, mux) with zero violations of the guarantees, no syntax errors on valid code, and parsing at roughly 400 to 1,400 files per second.
- 12 safeguards were mutation-tested (each broken in turn and caught by a test), including redaction at fetch, redaction of legacy rows, chunk-text exactness, idempotent re-parse, parent links, the chunk cap, snapshot scoping and the ownership check.
- In a real browser against the real backend (only the GitHub network layer faked), indexing this project's own source (105 files, 430 symbols, 409 chunks, 2,241 references): the tick driver finished the job, statistics were fresh immediately, Monaco rendered with syntax highlighting, outline clicks highlighted the right lines and wrote them into the URL, a reload restored the highlight, redaction, skipped-file and syntax-error panels showed, Tab reached the explorer, and the page has no horizontal scroll at 390 px. The browser contacted only localhost, the fake provider and the avatar host, so nothing is loaded from a CDN.

**Bugs found by testing real code and fixed in this milestone**
- A bulk update linking child symbols to their parents was rejected by SQLAlchemy (hidden until a fixture had nested symbols).
- File listings used the database's locale ordering, which differs between machines; they now sort bytewise.
- The generic secret rule redacted test fixtures like `token: 'invalid-token'` (31 false positives across five repositories); it now requires random-looking values and ignores words and slugs. URL passwords like `user:pass@host` are ignored too.
- Files named `credentials.py` or `secrets.ts` were skipped as "secret files"; only data files (`credentials.json`, `secrets.yaml`, `.env`, keys) are now.
- The statistics panel showed zeros for up to five seconds after indexing finished; it now refreshes when a phase ends. It and the status panel also disagreed on the file count.
- Monaco 0.57 hard-pins a DOMPurify with critical XSS advisories; an npm override pins the patched release.

**Deviations from the plan**
- Added a `code_refs` table (imports and calls, resolved in M6), `symbols.parent_id` and `signature`, `files.redactions` and `parse_status` (migration `0004`).
- Redaction happens in the fetch step, so a raw secret is never written to the database; the parse phase redacts again as a safety net for rows stored by older versions. Line endings are normalized to LF and a leading BOM is dropped (line numbers are unchanged).
- Neighbouring small chunks are merged (up to 1,800 characters) so files full of tiny functions do not explode the chunk count. A merged chunk's `symbol_id` is its first symbol, not all of them.
- The HNSW vector index is still deferred to M5.
- The URL carries the open tab, file and highlighted lines (`?tab=files&file=...&line=12-20`). M7 citations will link through it.

**Known limitations**
- Calls are recorded by callee name and receiver only. Nothing is resolved to a target until M6, and dynamic calls are never guessed.
- Secret scanning is pattern based. It will miss unusual formats, and it still redacts a few harmless values: odd fake URL passwords (`p%40ssword`), the RFC 2617 example password, and this project's own default dev database URL. Redaction is judged the safer error for a privacy tool.
- After a syntax error Tree-sitter may swallow later declarations in that file, so a broken file can have an incomplete symbol list (flagged in the UI).
- Not symbols: Go constants and variables, Python nested functions (their calls are credited to the enclosing function), object-literal methods in JavaScript. `.d.ts` files are treated as generated and skipped.
- The browser run is a manual script, not part of CI. The Monaco bundle is large but loads only when a file is opened.
- 17 dev-tooling advisories remain, all one `picomatch` ReDoS with no patched release (4.0.7 is latest). They affect eslint, vite and vitest only and do not ship to users; CI audits production dependencies only.

### M5. Embeddings, retrieval, benchmark
- [ ] `Embedder` interface, Hugging Face client (batching, retry, backoff), hash embedder
- [ ] Resumable embed phase with a cursor, HNSW index after bulk insert
- [ ] Keyword search, vector search, RRF fusion
- [ ] Labeled dataset of 30 to 50 questions on one pinned small repo
- [ ] `eval/run_eval.py`: keyword-only vs vector-only vs hybrid, Recall@k and MRR, results written to disk
- [ ] Tests: embedding storage and dimension check, retrieval on a fixture, eval script runs end to end

**Exit:** a reproducible eval report with measured numbers.

### M6. Graph and impact analysis
- [ ] Edge builder: imports, contains, and calls resolved through import bindings and same-module names
- [ ] Unresolved calls stored with `resolution = unresolved`, excluded from facts
- [ ] Graph and file endpoints; BFS for direct and transitive dependents
- [ ] Suggested tests by naming and import proximity, labeled as heuristic
- [ ] AI suggestion step, kept separate from static results, plus confidence and limitations
- [ ] Tests: graph construction on fixtures, no invented edges for dynamic calls, transitive depth, impact response shape
- [ ] UI: React Flow graph (file and symbol levels, static vs unresolved styling) and the impact panel

**Exit:** the impact endpoint returns static dependents with edge paths and clearly labeled suggestions.

### M7. Question answering
- [ ] OpenRouter client with timeout, retry, fallback model, and a stub
- [ ] LangGraph nodes: classify, scope, retrieve, expand, assemble, generate, validate
- [ ] Conditional edges: insufficient evidence, one regeneration on citation failure
- [ ] Prompt-injection hardening and a hostile-content fixture
- [ ] Chat quotas and rate limits
- [ ] Tests: citation correctness (every cited range exists), uncertainty path, injection fixture, validator rejects fabricated IDs
- [ ] UI: chat panel whose citations jump to the cited lines in the Monaco viewer

**Exit:** answers carry validated file and line citations, and weak evidence yields an honest "cannot determine".

### M8. Polish and deployment
- [ ] Responsive layouts and consistent loading, empty and error states across every page
- [ ] Dockerfiles, `vercel.json` (including the `/api` rewrite), `render.yaml`, Neon setup notes, `.env.example`
- [ ] Production callback URL and secrets for the GitHub App documented
- [ ] README, architecture docs with Mermaid, API docs, demo script

**Exit:** the deployed app lets a new user sign in, index a real repo, and query it, and the demo script runs start to finish.

### M9. Private repos with user-supplied providers (post-MVP)
Design: [Production architecture](ARCHITECTURE.md#production-architecture-post-mvp-planned-in-m9-and-m10). Sign-in and ownership exist from M3. Public repos keep using the platform's providers; private repos require the user's own embedding and chat providers.

- [ ] Add "Contents: read" to the GitHub App and the install-on-selected-repositories flow
- [ ] Token provider for short-lived installation tokens; private visibility read from GitHub, never from the client
- [ ] Private repos linked only to users GitHub says have access; access rechecked on index and on query
- [ ] Provider settings model with three modes: platform, user key (stored encrypted), browser-held (zero retention)
- [ ] Provider URL allowlist and SSRF guard (private and link-local ranges blocked, no redirects)
- [ ] Policy gate: a private repo cannot be indexed until both embedding and chat providers are user-supplied
- [ ] Embedding choice for user providers limited to a supported 384-dimension list
- [ ] Private snapshots owner-scoped, never shared or deduplicated; retention TTL job; delete-repository endpoint that removes all derived data
- [ ] Frontend: provider settings (key entry, masked display, remove) and a notice of what is sent to which provider before connecting a private repo
- [ ] Tests: private repo blocked without providers; SSRF guard rejects internal addresses and redirects; keys never in logs or responses; deletion leaves no rows; revoked access stops queries; the wrong-user matrix still holds for private repos

**Exit:** two users cannot see or query each other's private repos on any route, and a private repo cannot be indexed through the platform's providers.

### M10. Production hardening and scale (post-MVP)
Needs paid hosting tiers. Numbers in the README come from measurements, not estimates.

- [ ] Separate worker process running the same runner; queue claim reuses the lease logic
- [ ] Redis-backed rate limits and quotas
- [ ] Object storage for file contents, with a migration from `files.content`
- [ ] KMS envelope encryption for stored provider keys, with rotation tooling
- [ ] Postgres row-level security policies as defense in depth
- [ ] Sandboxed parsing workers (container limits, no network except an allowlist)
- [ ] Audit log of private-repo access; a test that logs contain no file contents, prompts, or keys
- [ ] Metrics, tracing, and alerts on queue depth and job failure rate
- [ ] Load test with recorded results; backup and restore drill
- [ ] Dependency and container scanning in CI; written threat-model review

**Exit:** a load test with recorded numbers, a successful restore drill, and sandbox limits shown to hold against a hostile archive.

## 3. Testing, deployment, risks

**Testing and evaluation.** Unit tests use fixtures and the stub providers, so CI needs no keys. Integration tests run against a real Postgres with pgvector. The benchmark uses one pinned small repo and a hand-labeled question set, and reports Recall@k and MRR for keyword-only, vector-only and hybrid. The README will state the dataset size and its limits, and quote only numbers the script produced.

**Deployment**
- **Neon:** create a project, enable `vector`, copy the pooled URL for the app and the direct URL for Alembic.
- **Render:** one free web service from `render.yaml`, migrations on deploy, secrets in the dashboard (`DATABASE_URL`, `OPENROUTER_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`).
- **Vercel:** the `frontend` directory, with `NEXT_PUBLIC_API_URL` pointing at Render. CORS on the API allows only that origin.
- **Indexing on free tiers:** the browser drives `tick` while the page is open. A scheduled GitHub Actions workflow can also call `tick` so a job finishes after the tab closes.
- **Cold starts:** the frontend shows a waking state and retries `/health`. Neon connections use pre-ping and a retry on first connect.

**Risks**

| Risk | Effect | Mitigation |
| --- | --- | --- |
| HF free credits run out | Embedding phase stalls | Small repo quotas, batching, a clear `quota_exceeded` error, resumable cursor |
| OpenRouter free models are limited or vanish | Chat fails | Fallback model, retries, a configurable model ID, honest error |
| Open models misformat answers | Failed citations | Chunk-ID citations, one regeneration, then drop the claim |
| Render free sleeps mid-job | Job pauses | Leases expire, the next tick resumes |
| Neon 0.5 GB fills | Writes fail | Per-repo caps, delete old snapshots, show usage in stats |
| Call resolution is partial | Impact misses dependents | Report it in `limitations`, never guess |

**Open questions**
- Which small public repo should be the benchmark target? A repo you know well makes labeling faster and more accurate.
- Should the app require a `GITHUB_TOKEN`, or work unauthenticated at 60 requests per hour for the demo?
- Is Docker and Python 3.12 available locally? M1 assumes both.
- For M9: should user-supplied provider keys default to browser-held (zero retention) or encrypted on the server? The design supports both; the default affects the UI and how background indexing works for private repos.
