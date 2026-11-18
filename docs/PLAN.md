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
| Embeddings | Cloudflare Workers AI, `bge-small-en-v1.5` (384 dims), model ID in env |
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
- [x] `Embedder` interface, Cloudflare Workers AI client (batching, retry, backoff), offline hash embedder
- [x] Resumable embed phase with a cursor
- [x] Keyword search (Postgres full-text), exact vector search, RRF fusion
- [x] Labeled datasets: 40 questions on psf/requests and 20 held-out questions on pallets/click, both pinned to a commit
- [x] `eval/run_eval.py`: keyword vs vector vs hybrid, ablations, Recall@k, MRR with bootstrap intervals, paired comparisons, results written to `backend/eval/results/`
- [x] Tests: storage and dimension checks, stale-vector reset, fallbacks, snapshot scoping, retrieval on a fixture, eval harness end to end
- [x] API `POST /api/repositories/{id}/search`; UI: Search tab, embed progress

**Exit:** a reproducible eval report with measured numbers. Met.

**Measured results** (Cloudflare `bge-small-en-v1.5`, CLS pooling; MRR, 95% bootstrap interval in brackets for the headline rows)

| Method | psf/requests, 40 questions, 523 chunks | pallets/click, 20 questions (held out), 1,214 chunks |
| --- | --- | --- |
| keyword | 0.577 (0.45 to 0.71) | 0.462 (0.28 to 0.65) |
| keyword, text and path only (no symbol signal) | 0.412 | 0.213 |
| vector | **0.826** (0.73 to 0.92) | **0.571** (0.38 to 0.78) |
| hybrid, equal weights | 0.721 | 0.505 |
| hybrid, vector weight 3 | 0.753 | 0.502 |

- Vector search had the best MRR on both repositories, so it is the default. Hybrid never beat it. Against vector on click, hybrid was 5 better, 5 worse, 10 tied. Weighting the vector side up closed part of the gap on requests and none on click.
- Keyword search is better for exact identifiers (click identifier Recall@5 1.00 vs 0.83 for vector); vector is better for concept questions (requests Recall@5 0.88 vs 0.56). Hybrid remains selectable for identifier-heavy use.
- The click intervals are wide with 20 questions, so click alone does not prove vector is better; the evidence is the consistent direction across both sets.
- Honesty notes: the symbol-name "jump to definition" signal was added after seeing keyword failures on requests (identifier MRR 0.56 to 0.92 there; it also helped on click, 0.21 to 0.46 overall). The fusion weights were tried on requests and then checked on click. Click's labels were fixed before it was run.
- The bge query instruction prefix made no measurable difference (vector MRR 0.826 with, 0.822 without), so it is kept only because the model documents it.
- Cost: about 0.9 Workers AI neurons per chunk (523 chunks used 455, 1,214 used 1,083). The free allowance is 10,000 neurons a day, so roughly 11,000 chunks a day; a repository at the 6,000-chunk cap costs about 5,200.
- Latency (median): keyword 7 to 15 ms; vector and hybrid 150 to 270 ms, almost all of it the query-embedding request.

**Deviations from the plan**
- Cloudflare Workers AI replaced Hugging Face: HF's Inference API returned 402 (credits required). The model and its 384 dimensions are unchanged.
- No HNSW index. Approximate indexes combined with the snapshot filter can return fewer rows than asked; an exact scan over at most 6,000 rows is correct and fast enough. Migration `0005` only rebuilds the full-text column (plain words plus camelCase-split words).
- Default search mode is `vector`, not `hybrid` (see results). If embeddings are missing or the embedding service fails, search falls back to keyword and says so in `warnings`.
- The embedder's own model id is stored per chunk and used for stale-vector detection, so changing the model re-embeds instead of mixing vector spaces. A missing Cloudflare credential raises `embedding_not_configured`; it never silently switches to the hash embedder.

**Known limitations**
- Two Python repositories and 60 questions: this is evidence, not a general result. The hash embedder is lexical and is for tests and demos only.
- Measured since: exact vector search over 6,000 random 384-dimension rows takes 1.8 ms median locally (random vectors, so a rough figure), which is why there is no HNSW index. Not measured: a pooling ablation (`--pooling mean`).
- The local fastembed alternative was not installed (the environment denied the install); only Cloudflare and hash embedders exist.

### M6. Graph and impact analysis
- [x] Edge builder: imports resolved to files; calls resolved through import bindings, re-exports, `self`/`this`, same-module names and Go package scope
- [x] Unresolved calls and imports stored with `resolution = unresolved` and a reason, never used as facts
- [x] Graph and impact endpoints; BFS for direct and transitive dependents with the edge path to each
- [x] Suggested tests: static dependents plus a naming heuristic, each labelled
- [x] AI review step, kept separate from static results, with validated citations, calibrated confidence and limitations
- [x] Tests: graph construction on fixtures, no invented edges, transitive depth, impact response shape, ownership, snapshot scoping
- [x] UI: React Flow graph (files and symbols, static vs unresolved), impact panel, file finder, AI review

**Exit:** the impact endpoint returns static dependents with edge paths and clearly labeled suggestions. Met.

**Status: built and verified.**
- Backend 1,334 tests, frontend 218; ruff, mypy strict, lint, typecheck, build and `alembic check` pass; `npm audit --omit=dev` finds nothing.
- The rule that shapes the builder: an edge is `static` only when the code proves it. `obj.method()` on a variable, inherited methods, dynamic imports, path aliases and name collisions are `unresolved` with a reason (`dynamic_receiver`, `inherited_or_dynamic`, `not_found`, `external_or_unresolved`, `package_or_alias`, `external_package`). The builder never matches by name across files.
- Python imports resolve against package roots (the first ancestor directory without `__init__.py`), so `src/` layouts work; a name that matches two roots is left unresolved. JS and TS resolve relative specifiers with extensions and index files. Go resolves imports under `github.com/<owner>/<repo>` (taken from the repository URL) to the package's non-test files.
- Checks on real repositories (psf/requests, pallets/click, sindresorhus/ky, gorilla/mux, expressjs/express): no static call edge whose target name differs from the callee name; every cross-file static call in requests, click and ky is backed by a chain of static imports (the direct import edge is sometimes to a re-exporting `__init__`).
- **Manual audit** (by reading the call-site line next to the target's definition, not an independent tool; installing `jedi` as an oracle was denied by the environment): click 40 of 40 correct, ky 20 of 20, mux 20 of 20, requests 12 of 12 in an earlier sample. express 18 of 20: two were wrong (below), and after the fix all 56 remaining express edges were read and are correct. The audit is by one reader on samples, so it is evidence, not a guarantee.
- How much resolves (static calls out of all call sites): requests 29%, click 30%, mux 19%, ky 13%, express 3%. Most call sites are method calls on variables, builtins and library calls, which cannot be resolved without type information. Precision was the goal; recall is limited and shown honestly (unresolved edges are drawn dashed, counted, and listed as unverified callers in impact).
- 12 safeguards were mutation-tested. Nine were caught at once; three gaps (ambiguous duplicate definitions, `self` scoping, import folding) got new tests and are now caught; two other survivors are equivalent mutants (a removed filter that another guard makes redundant).
- In a real browser against the real backend (only GitHub faked) on this project's own source: the tick driver indexed through the graph phase to ready; the file graph rendered; selecting a file narrowed the graph to its neighbourhood and showed the impact panel; the symbol view, URL state, reload restore, phone width (no horizontal scroll) and the AI review all worked, with no page errors and no external hosts beyond localhost, the fake provider and the avatar host.

**Bugs found by dogfooding and fixed in this milestone**
- A JavaScript member assignment such as `req.accepts = function () {...}` is recorded as a symbol, and bare-name lookup treated it as a name in scope, so `accepts(this)` (the npm package) linked to it. 12 of express's 68 static call edges were wrong. Declarations are now lexical, `exports.x = ...` is exported but not in scope, and other member assignments are neither.
- `import '../..'` (the repository root's index file) was not found because `normpath` returns `.`.
- The whole-repository graph (114 files) and the symbol graph (75 nodes, 95 of 141 edges unresolved) were unreadable. The overview now draws only the 60 most connected files, selecting a file shows its neighbourhood by default, unresolved calls are hidden in the symbol view until asked for, and a file finder reaches any file.
- A model call could outlast the frontend proxy's roughly 30 second limit and surface as a dropped connection. The LLM client now has a whole-call time budget (28 s, fallback included) and reports a retryable "took too long".
- The test environment inherited the real `OPENROUTER_API_KEY`, so a "not configured" test would have made a real model call. Tests now blank it.
- The impact card had an `aria-label` but no landmark role.
- The model's own summary once said "7 direct dependents" when the proven number was 9, which is why the AI review is labelled unverified and kept out of the static list.

**Deviations from the plan**
- `contains` edges are not stored: containment is `symbols.parent_id`, so a copy would only add rows. `graph_edges` gained `target_name`, `reason`, `line` and `count` (migration `0006`).
- Imports are file to file; a file's repeated imports of one file fold into one edge with a count and the first line.
- The first default chat model was OpenRouter's `openrouter/free` router (replaced in M7 by named models) because individual free models are often rate limited upstream (both Gemma free models returned 429 when tested) and the free list changes. The answer reports which model replied. Set `OPENROUTER_MODEL` to choose.
- The AI review is `POST .../impact/review` (5 per minute per user). No daily model budget yet; that comes with chat in M7. The database connection is released before the model call.

**Known limitations**
- Not resolved: inheritance (a subclass calling an inherited method), type-based method calls, TypeScript `paths` aliases and package workspaces, Go vanity import paths (anything not under `github.com/<owner>/<repo>`) and Go methods, dynamic imports with computed specifiers, `export default` targets (the file is linked, the symbol is not guessed).
- A symbol's impact follows its callers only; code that imports it without calling it appears in the file-level view. A call at the top level of a file is attributed to the file.
- Two definitions of one name in a file (conditional definitions) are treated as ambiguous and left unresolved.
- Impact loads the snapshot's edges for each request. That is fine at the edge cap (150,000, with unresolved edges dropped first) but is not built for much larger graphs.
- The overview graph is capped at 60 files and a neighbourhood at 300; a very large repository needs the finder.
- The AI review depends on a free model, so it can be slow, busy or wrong; it can only cite files the analysis surfaced.

### M7. Question answering
- [x] OpenRouter client with a whole-call time budget, fallback model, retry on empty replies, and an injectable transport for tests
- [x] LangGraph nodes: classify, scope, retrieve, expand, assemble, generate, finalize (validation is mechanical and runs inside generate)
- [x] Conditional edges: insufficient evidence skips the model; one regeneration when a citation fails
- [x] Prompt-injection hardening and a hostile-content fixture
- [x] Chat quotas and rate limits (5 per minute and 25 model-backed questions per day per user per repository)
- [x] Tests: every cited range exists, uncertainty path, injection fixture, validator rejects fabricated ids and quotes
- [x] UI: Ask tab whose citations open the Files tab at the cited lines

**Exit:** answers carry validated file and line citations, and weak evidence yields an honest "cannot determine". Met in tests; see the limits below for what is not yet shown with a real model.

**Status: built; backend 1,430 tests and frontend 234 passed, ruff, mypy strict, lint, build and `alembic check` clean at the last full run.**
- **Workflow.** classify, scope, retrieve, then either `cannot_determine` (no model call, free) or expand, assemble, generate, finalize. At most two model attempts inside one 28 s budget. The database connection is released before the first model call.
- **Evidence gate.** Enough evidence means hits exist and either a symbol named in the question exists or the best cosine similarity is at least `QA_MIN_SIMILARITY`. That setting is now 0 (off): any question is sent to the model, which may decline, and the quote validator rejects anything it cannot tie to the code (see below).
- **Validation, not trust.** The model returns 3 to 5 claims, each with excerpt ids and a verbatim quote. A claim is kept only if the id exists and the quote is found word for word in a cited excerpt; the quote also fixes the exact cited lines. Failed claims are dropped and the answer says how many. Verified claims beat a later refusal.
- **Injection defences.** Retrieved code sits in a block delimited by a per-request random nonce, the question is outside it, and the validator, not the model, decides what is cited.
- **Quota.** Declined questions cost nothing; a model-service failure is refunded.

**Measured (retrieval only, Cloudflare bge-small, no chat model)**
- Placeholder threshold 0.5 was badly wrong: unrelated questions score 0.45 to 0.73 with this embedder.
- psf/requests (calibration): answerable 0.716 to 0.857, unanswerable 0.453 to 0.734. At 0.725 it answers 98% and declines 93%; at 0.75, 92% and 100%.
- pallets/click (held out): answerable 0.693 to 0.868, unanswerable 0.473 to 0.69. At 0.75, 90% and 100%; best band 0.70 to 0.725 (95% and 100%).
- **Gate turned off after manual testing.** On the user's own repository (nl2pod), "where are the prompt templates?" scored 0.714 against the 0.725 threshold and was refused, and "what is the repo about?" was refused too. The threshold was tuned to decline off-topic questions for free, but it also refused real questions, which is the worse failure, and the user wants to be able to ask anything. `QA_MIN_SIMILARITY` now defaults to 0 (set it above 0 to bring the free refusal back). Real-model check on psf/requests (`python -m eval.qa_live --dataset eval/datasets/requests.json --unanswerable`): of the 15 unanswerable questions, 14 reached the model and all 14 were declined by it, none produced an answer; the 15th hit a model-service failure. Cost: every question now uses a model request and counts toward the daily limit.
- **Overview questions.** Questions about the whole project ("what is this repo about?", "overview", "tech stack", "how is it structured") are classified `overview` by rules, and the README plus the first manifest chunk are put first in the context. To make that possible, README files (`README`, `.md`, `.markdown`, `.rst`, `.txt`) are now stored as language `markdown`, chunked as plain text, with no symbols and no graph edges. Existing indexed repositories must be re-indexed to get this. Real model on psf/requests: both overview questions were answered with claims citing `README.md`, `setup.py` and `docs/conf.py`.
- The unanswerable questions were written before measuring. The earlier 0.725 was nudged after seeing click's sweep, so click is not a fully clean held-out check.
- Labelled code was in the top 10 for 98% of requests questions but only 80% of click's (75% within the context budget).
- Graph expansion gave no gain (neighbours added after hits: 0 gained, 0 lost; neighbours replacing hits lost recall, e.g. 92% to 75% at 3 excerpts), so `QA_EXPAND_MAX=0`. Six excerpts matched eight (98%).
- Model side-by-side on the real prompts: nemotron-3-ultra answered in 9 to 16 s and is the default, with ernie-5.5-flash as the fallback; Gemma and particle returned 429, ling-3.2-flash was slow and empty, nemotron-lightning returned empty.

**Deviations from the plan**
- No separate `validate` node: validation runs inside `generate` so the retry decision sees it.
- No global daily model budget, only per-user limits. Chat history is sent by the browser (at most 6 turns) and is not stored.
- Default models changed from the M6 note: `openrouter/free` was replaced by the two named models above, because the router's choice varied between runs.

**Known limitations**
- **Real-model evidence is small.** The live run (`python -m eval.qa_live --dataset eval/datasets/requests.json --per-type 4`, psf/requests, nemotron-3-ultra) asked 12 questions. 5 failed because the free model service was unavailable (`llm_unavailable`, not counted against quota). The other 7 all answered: 21 claims kept and 2 dropped by the validator, every answer cited the labelled code, 6 needed one attempt and 1 needed a retry, median 11.7 s (range 3.7 to 24.8). Confidence: 3 high, 3 medium, 1 low (the retry that lost 2 claims). That is 7 answers on one repository: it shows the validator and prompt work with a real model, not how often answers are right. "Cites the labelled code" was checked mechanically; claim correctness was not graded. The live set has no unanswerable questions, so real-model declines are untested (the no-model decline path is covered by the retrieval measurements and tests). The Ask tab has not been driven in a real browser.
- The gate catches out-of-scope questions but not in-scope questions where retrieval missed the right code; those can get an answer built from the wrong excerpts (every claim still quotes real code, but it may not be the relevant code).
- With the gate off, a model that answers an off-topic question is held back only by the quote validator and the prompt; 14 of 14 declined in the check above, which is a small sample.
- A citation proves the quote exists in the cited lines, not that the claim follows from it.
- Free models are slow, rate limited and sometimes empty; answers take 10 to 30 s and can fail with a retryable error.

**Added after manual testing of M7:** a Re-index button (`POST .../index` with `{"force": true}`, behind a confirmation) and a Disconnect button (`DELETE /api/repositories/{id}`, behind a confirmation). Re-index replaces a same-commit snapshot and counts toward the daily indexing quota. Disconnect removes the user's link and deletes the repository's data when nobody else is connected. 9 backend and 5 frontend tests. Limitations: disconnecting and connecting again resets the per-repository daily question count (it lives with the repository's data), though it costs a full re-index, which has its own daily limit; another person viewing a shared repository while it is re-indexed sees it as indexing for a few minutes; a person connecting at the exact moment the last other person disconnects can get an error and should retry.

### M8. Polish and deployment
- [x] Responsive layouts and consistent loading, empty and error states across every page
- [x] Dockerfile checked, `render.yaml`, Neon setup notes, `.env.example` for the backend and the frontend (no `vercel.json`: see deviations)
- [x] Production callback URL and secrets for the GitHub App documented
- [x] README, architecture docs with Mermaid (already present), API docs (FastAPI's `/docs`), demo script

**Exit:** the deployed app lets a new user sign in, index a real repo, and query it, and the demo script runs start to finish. **Not yet met: nothing has been deployed.** The files and steps exist; the first real deploy is the test of them.

**Status: files and docs written; deployment itself not done.**
- **Verified here:** the backend image builds (435 MB); started with production settings against a fresh pgvector database it ran all six migrations and answered `/health` and `/ready`. A real browser (Playwright, the real backend with only GitHub faked) loaded login, repositories, and every tab of an indexed repository (153 files) at 375, 768 and 1280 px wide.
- **Found and fixed by that check:** at 375 px every repository page was 15 px wider than the screen (the five tabs did not fit; the tab bar now scrolls); the Ask input was squeezed to about 100 px next to its buttons (it now takes its own row on a phone); long file paths in search results made the page 415 px wide (they now wrap; the same fix is on the Ask sources list, which could not be exercised in a browser without a model key). After the fixes there was no horizontal overflow on any page at 375 px, and no page errors.
- **Error states seen in the browser:** "not set up on this server" for Ask without a key, "too many requests" when the rate limit tripped (it did, from the audit's own traffic), repository not found. The cold-start message ("Waking up the server") already existed from M3.
- **Not verified:** Neon, Render and Vercel themselves (no accounts here), `render.yaml` as an applied blueprint, the Vercel build, and the production GitHub callback. [DEPLOYMENT.md](DEPLOYMENT.md) is a checklist written from the documented behaviour of those services and from this project's code; expect to correct it on first use.

**Deviations from the plan**
- No `vercel.json`. The `/api` rewrite already lives in `next.config.ts` and reads `BACKEND_URL` at build time, and local development exercises it. A second copy could only disagree with it.
- No scheduled GitHub Actions `tick` workflow. `tick` requires the owner's session, so an unauthenticated cron cannot call it; indexing therefore advances only while the repository page is open, and the docs say so.
- The graph on a phone is small and needs zooming; it is usable but not designed for a phone.

**Known limitations**
- On a phone the code viewer scrolls sideways inside its own box (intended), and the graph canvas is small.
- Search always returns the nearest chunks, even for a nonsense query; it shows their scores but does not say "nothing relevant".

### M9. Private repos with user-supplied providers (post-MVP)
Design: [Production architecture](ARCHITECTURE.md#production-architecture-post-mvp-planned-in-m9-and-m10). Sign-in and ownership exist from M3. Public repos keep using the platform's providers; private repos require the user's own embedding and chat providers.

- [x] Add "Contents: read" to the GitHub App and the install-on-selected-repositories flow (done in the GitHub account; steps in GITHUB_APP_SETUP.md)
- [x] Private visibility read from GitHub with the user's own token, never from the client (deviation: user-to-server tokens instead of installation tokens, see below)
- [x] Private repos linked only to users GitHub shows them to; access rechecked on every route that reads or changes data
- [x] Provider settings model with three modes: platform, user key (stored encrypted), browser-held (zero retention)
- [x] Provider allowlist and SSRF guard (private and link-local ranges blocked, no redirects)
- [x] Policy gate: a private repo cannot be indexed until both embedding and chat providers are user-supplied
- [x] Embedding choice for user providers limited to a supported 384-dimension list
- [x] Private snapshots owner-scoped, never shared or deduplicated; retention TTL; delete-repository endpoint that removes all derived data
- [x] Frontend: provider settings (key entry, masked display, remove) and a notice of what is sent to which provider before connecting a private repo
- [x] Tests: private repo blocked without providers; SSRF guard rejects internal addresses and redirects; keys never in logs or responses; deletion leaves no rows; revoked access stops queries; the wrong-user matrix still holds for private repos

**Exit:** two users cannot see or query each other's private repos on any route, and a private repo cannot be indexed through the platform's providers.

**Progress: every task in the list is built and tested. What remains is a run with real provider keys and a real private repository, which has not been done.** A private repository can now be connected, indexed, searched and asked about, but only with providers its owner set up: the platform's providers are never used for it, even when they are configured and the owner's are missing (that is a `409 providers_required`, not a fallback).

**Step 2 design and results (provider settings and the policy gate)**
- **Providers are chosen, not typed.** A fixed catalogue lists what a user can bring: chat through OpenRouter, OpenAI, Together or Groq (all OpenAI-compatible); embeddings through the user's own Cloudflare Workers AI account (bge-small, 384 dimensions natively) or OpenAI `text-embedding-3-small` or `-large` asked for 384 dimensions. A user picks a provider id and a model; there is no URL field, so every address the server calls for a user's key comes from the catalogue. Default chat model names are suggestions and may be out of date; the user can change them.
- **SSRF guard.** Before a client is built, its address must be https, on the catalogue's host list, with no credentials or odd port, and every IP its name resolves to must be public (loopback, private, link-local including the cloud metadata address, carrier-grade NAT, IPv6 unique-local and multicast are refused; a failed lookup fails closed). The HTTP clients also never follow redirects. Tested with look-alike hosts, the `user@host` trick, IP literals and a resolver that returns internal addresses.
- **Key storage, two modes.** *Stored*: encrypted with the server's `TOKEN_ENCRYPTION_KEY` (Fernet), decrypted only for the request that needs it; the API returns only the last four characters; a database constraint ties the mode to the presence of a key. *Browser only*: nothing secret is saved; the browser sends `X-Embedding-Key` / `X-Chat-Key` with each repository request, kept in `sessionStorage` (gone when the tab closes) and attached only to `/api/repositories/...` calls. Keys travel as `SecretStr`, are never in a validation error, a response or a log line (tested at DEBUG level for both modes).
- **Resolution per repository.** Every route that needs a provider gets it from one dependency: public repository, the platform's; private repository, only the owner's. A missing provider, a missing or malformed browser-held key and an unreadable stored key each give a specific `409 providers_required`. Clients built for a user are closed when the request ends. Indexing a private repository needs both slots; each tick needs the embedding provider.
- **Mixed vector spaces.** Search and Ask compare a query vector with stored vectors only if every stored vector came from the same model; otherwise search falls back to keywords with the warning `embeddings_model_changed` (and the next tick re-embeds). This also protects public repositories if the platform's model changes.
- **Tests:** 3 unit files (47 tests for the guard and the OpenAI embedder, 23 for settings validation) and 24 integration tests (the settings API, and the full private flow with fake providers that record which key they were given, with the platform's providers wired up but forbidden). 20 safeguards were mutation-tested; 17 were caught at once, one more after a new test, and two survivors are equivalent mutants (an unreachable return, and an IPv4-mapped-address check that Python 3.13 already performs).

**Step 3 design and results (retention and deletion)**
- **Retention.** A private repository's indexed data (files, chunks, vectors, graph) is deleted `PRIVATE_RETENTION_DAYS` (default 30; 0 keeps it) after it was indexed. The repository stays connected, so it can be indexed again; public snapshots never expire; a running or pending snapshot is never deleted from under its job. The status shows `expires_at` and the Overview says so.
- **Enforced two ways, because there is no always-on worker.** Whenever a private repository is used, its expired snapshots are removed first, so expired data is never served even if no scheduler ever ran (and using one repository cleans up only that repository). For data nobody touches, `POST /api/internal/purge-expired` (bearer `MAINTENANCE_TOKEN`, constant-time compare, per-IP limited, 404 when no token is configured, no session accepted) deletes every expired private snapshot, and `.github/workflows/purge-private-data.yml` calls it daily when the `API_URL` and `MAINTENANCE_TOKEN` repository secrets exist.
- **Deleting a private repository** (`DELETE /api/repositories/{id}`, from M7) removes every derived row. The test finds the tables from the database schema (every table with a `snapshot_id` or `repository_id` column), so a table added later cannot be forgotten, and checks another user's data and the user's provider settings are untouched.
- **Tests:** 14 backend and 1 frontend. 12 safeguards were mutation-tested; one survivor (one repository's use purging every repository) got a new test and is now caught.
- **Limits:** retention counts from when a snapshot was created, not from when it was last used. Backups of the database (if the operator keeps any) are outside this: deleting rows does not erase them from a Neon backup or branch, and the docs say so.

**Step 4 design and results (the privacy notice)**
- A plain-language notice, specific to the user: it says who can see the repository (only the owner; never shared; this app's own providers are never used for it), what GitHub is asked, what this app's database stores and for how long (the configured retention, from the API; "until you Disconnect" when retention is off), which **named** embedding provider and model receive every code chunk and search question, which chat provider receives questions and retrieved excerpts, how the user's keys are held (only the modes actually in use are described), that each provider's own terms apply, and that backups are not erased by deletion. A provider that is not set up is called out in amber with the consequence (indexing stays blocked) instead of being hidden.
- **Before connecting:** the private repositories list shows it in full and keeps every Connect button disabled until the box "I understand where my private code goes" is ticked. **After connecting:** a private repository's Overview shows it folded under "Where this repository's data goes".
- **Limits:** the acknowledgement is a UI step, not a security control: pasting a private repository's URL into the connect form also connects it (the server decides it is private), without the checkbox. Nothing leaves for a provider until the owner has set one up and started indexing, and the notice is on the repository page. The text describes this app's behaviour as built and tested (for example, request logs here record only the path, status and timing); it makes no promise about what a provider does. Not viewed in a real browser: the layout is covered by component tests only.
- **Tests:** 9 component tests for the notice (named providers, not-set-up warning, key modes, retention wording, backups, fixed facts while loading or failing, acknowledgement, folding), plus picker and repository-page tests; 1 backend test for the retention value the listing reports.

**Step 2 limitations**
- **Not tried with real provider keys.** The provider clients are tested against mocked HTTP only. The first real run (an OpenAI or Cloudflare key, a chat key) is the true test, including whether a given provider accepts the request shape (for example `dimensions`, and JSON mode on each chat provider).
- Keys in `sessionStorage` are readable by any script running in the page. The app serves no third-party scripts, but this is weaker than keeping keys only on the server; the stored mode is the alternative.
- A key typed on the settings page is sent to the server once in stored mode (over TLS) and is then encrypted; nothing re-validates it against the provider when saving, so a wrong key shows up at first use.
- The user's own chat and embedding usage is not metered here; the daily question limit still applies to private repositories.

**Step 1 design and results**
- **Visibility is read from GitHub.** Connecting a repository now calls `GET /repos/{owner}/{name}` with the caller's own GitHub token; `private` in that answer decides which row is used. Anything the browser sends about visibility is ignored (tested). Connecting a repository GitHub does not show you now fails with 404 instead of being stored and failing later; for a private repository that means the GitHub App must be installed on it.
- **Private rows belong to one user.** `repositories.is_private`, `owner_user_id` (cascade on user delete) and `access_checked_at` (migration `0007`). A check constraint ties the two together; the unique name index is now per owner, so two users with access to `acme/secret` each get their own row, snapshots and data, never shared or deduplicated. Public repositories are unchanged and still shared.
- **Access is rechecked.** Every route that reads or changes repository data goes through one dependency (`OwnedRepo`). For a private repository it asks GitHub again, at most once per `PRIVATE_ACCESS_RECHECK_SECONDS` (300) per repository. If GitHub says no: `403 repo_access_lost` on every route except reading the record and disconnecting, so the owner can still delete the data. If GitHub cannot be reached the request fails (it never assumes access). The window is the longest a revoked repository stays reachable.
- **Private repositories picker.** `GET /api/github/private-repos` lists, through `/user/installations`, the private repositories the user can see and the app is installed on, with an install link when `GITHUB_APP_SLUG` is set. The Repositories page has a Private repositories section and a "private" badge.
- **Tests:** 17 backend tests (connecting, client cannot claim visibility, two owners, public still shared, a 13-route wrong-user matrix, provider gate on index, tick, meaning and hybrid search, chat and AI review, recheck caching and expiry, revoked access with disconnect still working, fail-closed, database constraints, user deletion) and 6 frontend. 12 safeguards were mutation-tested and all were caught.

**Deviation:** user-to-server tokens are used instead of installation tokens. A GitHub App user token can reach a repository only if the user has access to it and the app is installed on it, so GitHub itself computes the intersection and no installation token needs minting. The existing sign-in token, its encryption at rest and its refresh are reused. Cost: it relies on the user's token staying valid (a reauth prompt otherwise, as for public repositories).

**Known limitations (step 1)**
- A public repository that later becomes private is not noticed for users already connected to it: public rows are not rechecked, so data indexed while it was public stays visible to them.
- Connecting any repository now needs a GitHub token for the user (sign-in); previously it only validated the URL.

### M10. Production hardening and scale (post-MVP)
Needs paid hosting tiers. Numbers in the README come from measurements, not estimates.

- [x] Separate worker process running the same runner; queue claim reuses the lease logic
- [ ] Redis-backed rate limits and quotas
- [ ] Object storage for file contents, with a migration from `files.content`
- [ ] KMS envelope encryption for stored provider keys, with rotation tooling
- [ ] Postgres row-level security policies as defense in depth
- [ ] Sandboxed parsing workers (container limits, no network except an allowlist)
- [x] Audit log of private-repo access; a test that logs contain no file contents, prompts, or keys
- [~] Metrics and alerts on queue depth and job failure rate (built and checked, thresholds untuned); tracing is not built
- [~] Load test with recorded results (done, one machine, see LOAD_TEST.md); backup and restore drill (**not done**: the scripts exist but have never been run)
- [x] Dependency and container scanning in CI; written threat-model review (see [THREAT_MODEL.md](THREAT_MODEL.md); it is the developers' own review, not an independent one)

**Exit:** a load test with recorded numbers, a successful restore drill, and sandbox limits shown to hold against a hostile archive.

**Progress: 3 of 10 tasks done (audit log and log hygiene; worker process; scanning and threat model) and 2 partly (metrics and alerts, without tracing; load test done, but the backup and restore drill has never been run). Redis-backed limits were deliberately skipped for now. Object storage, KMS, row-level security and sandboxed parsing are not started.** M10 is mostly infrastructure that this project cannot verify from a laptop and a free tier. The honest order of work is: what can be built and tested locally first (worker process, Redis-backed limits, metrics, CI scanning, a written threat model, a load test and restore drill on local numbers), then what needs real services (object storage, KMS, row-level security with a non-owner database role, sandboxed workers). Numbers measured locally will be labelled as local; none will be presented as production figures.

**Worker process (done)**
- `python -m app.worker` (`--once` drains the work available now and exits; otherwise it runs until SIGTERM or SIGINT, finishing the step in progress first). It runs the same `runner.tick` as the API's tick endpoint, so there is one implementation of a job step. The lease compare-and-set already in the runner is what makes several workers, and workers next to browser tabs, safe: whoever holds an unexpired lease runs the step, the rest see "busy". Verified with two workers on six jobs: one download per job, no duplicate rows, all ready.
- **Picking work:** the least recently worked active job that is not leased and not being left alone, so jobs share the worker fairly. A job that failed transiently keeps its backoff (its lease is in the future), so the worker does not retry it in a hot loop.
- **Credentials (the part that needed care):** GitHub calls use the job owner's own token (a private repository's owner; for a public one, whoever started the index) and refresh it as needed. Embedding for a public repository uses the platform's embedder, as ticking does. A private repository is embedded only with its owner's own provider, and a worker can use that only if the key is *stored*; a browser-held key does not exist on the server, so the worker still fetches, parses and builds the graph (none of which sends anything to a provider) and then leaves the embedding step for the browser. A job whose owner has no provider, or has lost access to the repository, is left alone for `WORKER_SKIP_SECONDS` and looked at again. The embed phase itself also refuses to run for a private repository without the owner's embedder, so no caller can fall back to the platform's key by mistake.
- **A crash in one step** is logged (with no content, see the log hygiene test), the job is left alone for the skip window, and the worker keeps going.
- **Deploy:** `docker compose --profile worker up -d worker` locally; a commented `cip-worker` block in `render.yaml` for a paid Render plan (Render's free plan has no background workers).
- **Tests:** 18 integration tests (a job finishing with no browser, sharing a job with a tab, private jobs with stored and browser-held keys, a removed provider, lost access, the embed-phase guard, ordering and eligibility, backoff, skip memory, stop signals, a crashing step, two concurrent workers, the CLI). 15 safeguards were mutation-tested; all are caught (2 needed new tests; one is the difference between "skipped because the key is in a browser" and "skipped because reading it failed", which is the same safe behaviour, now pinned directly).
- **Found while testing:** the first mutation run left an orphaned test process spinning against the shared test database (a mutant that made the worker loop forever; the harness killed the wrapper but not the child), which made unrelated tests fail. The harness now kills the whole process group.
- **Not covered:** the worker is not audited per step (it would flood the log; a private job's start is audited when the owner starts it); there is no separate health endpoint or metrics for the worker yet (a later M10 task); a worker restarted mid-step loses at most that step, as with a tab.

**Load test (done, on one machine) and a real bug it found**
- Results, method and limits are in [LOAD_TEST.md](LOAD_TEST.md). The headline: **the API stalled completely at 50 concurrent users** with the shipped settings (1.7 requests a second, 78% errors, 30-second responses, while the API and the database were nearly idle). It was a connection-pool deadlock: requests held database connections while waiting for worker threads, and all threads waited for connections. Fixed (connections are released at the end of every worker-thread task; the session closes on its own executor; the pool timeout is 10 s), with a regression test that fails in 74 to 86 seconds without the fix and passes in under one second with it. After the fix the shipped pool served 50, 100 and 200 users at 104 to 112 requests a second with no errors. A single process is CPU-bound at roughly 110 to 125 requests a second on this machine.
- Added to the repository: `backend/loadtest/` (synthetic-repository generator, seeding, load script, runner, and the restore-drill script) and the `infra/backup/` scripts.
- **Not done, and said plainly:** the backup and restore drill. `infra/backup/backup.sh`, `infra/backup/restore.sh` and `backend/loadtest/restore_drill.py` were written but **have never been run**: the run was stopped, so nothing about their behaviour is verified, and there is no runbook. Treat them as untested drafts. The pool of 10+10 was measured only before the fix; the numbers are single 20-second runs, not repeats.

**CI scanning and the threat model (done)**
- **CI** (`.github/workflows/security.yml`, on every change and weekly, because new advisories appear without any code change; the existing `ci.yml` is unchanged except for a least-privilege token): `pip-audit` on the locked Python dependencies; `bandit` on the application code; `npm audit --omit=dev` (must be clean) and an audit of everything with an explicit, expiring exception list (`frontend/scripts/audit-policy.mjs`, unit-tested: a new high advisory fails, an expired exception fails, moderate does not); `trivy` on the freshly built API image (high and critical findings that have a fix) and on the Dockerfile, compose file and workflows; `gitleaks` over the whole git history. Dependabot opens weekly update PRs for Python, npm, the Dockerfile and the actions.
- **Every gate was shown to fail when it should**: a planted SQL injection (bandit), a planted real-looking secret (gitleaks), a vulnerable image (trivy) and a new or expired advisory (the npm policy). Each was also shown to pass on the current code.
- **What the scans found, and what was done:** `trivy` found 6 fixable high findings in the API image, of which the Python ones were inside `pip`'s vendored libraries in the base image; the Dockerfile now upgrades OS packages and removes `pip` (the app never runs it), and the image rescans clean. `bandit`'s 14 findings are resolved (11 `assert` skipped by policy, 3 fixed or annotated with a reason). `gitleaks` found 2 synthetic test fixtures in history, allowlisted by file and rule. `npm audit` shows 17 high findings that are all one unpatched `picomatch` advisory in development tools, accepted until 2026-12-31 with a reason. `pip-audit` and the production npm audit were already clean.
- **Found while writing the threat model:** there was no Content-Security-Policy header. `Permissions-Policy` and `Cross-Origin-Opener-Policy` were added at once (tested, and confirmed on a built server); the CSP followed, see the next section.
- **The threat model** (`docs/THREAT_MODEL.md`) walks each trust boundary and lists 32 threats with the defence, the test that pins it (all 27 cited tests were checked to exist) and a status, then lists the 12 open risks in order of importance. The most important: no CSP; parsing is unsandboxed and in-process; private code is stored in clear with the encryption key beside it; no row-level security; availability controls are single-instance with no global provider budget.
- **Limits:** this is the developers' own review, not independent and not a penetration test; scanners cannot find logic flaws such as a missing ownership check; the scanner images are pulled as `latest` (fresh rules, not reproducible); GitHub Actions are referenced by tag, not commit hash; Trivy's database download can be rate limited in CI and would show as a failed job; the new workflow has not yet run on GitHub.

**Content-Security-Policy (done, with stated gaps)**
- **Design:** a nonce-based policy from `src/proxy.ts` (Next 16's renamed middleware), built by a tested function in `src/lib/csp.ts`. Next 16's own guide says a nonce needs every page rendered per request and is incompatible with Partial Prerendering, so `cacheComponents` and `partialPrefetching` were switched off and the root layout is `force-dynamic` (the app uses none of the caching features; its pages are thin shells around client components). Policy: scripts only with this request's nonce plus `'strict-dynamic'`; no `'unsafe-inline'` or `'unsafe-eval'` for scripts in production; `connect-src 'self'`; Monaco's worker via `worker-src 'self' blob:`; images only from the app, `data:`, `blob:` and GitHub's avatar host; no foreign frames, objects or base URI; upgrade of insecure requests only when the request was https (over plain http, as in a local production run, it would break every asset). `style-src` keeps `'unsafe-inline'` (Monaco and React Flow create styles at run time): the one deliberate hole. `CSP_MODE=report-only` reports without blocking, for rolling a change out safely; `off` disables it; any other value is ignored, so a typo cannot silently turn it off.
- **Checked in a real browser** (Chromium, the production build and the dev server, a real backend against a scratch database): normal use of login, repositories, the real indexing tick loop, Files (Monaco rendered), Search, Graph (React Flow rendered), Ask and provider settings produced **no violations and no console or page errors**; all 13 script tags of a page carried a nonce, and it differed between loads. Seven attack-shaped controls were blocked: an injected inline script, an injected foreign script, `eval` called from a trusted script, an injected event handler, a cross-origin `fetch`, a foreign frame and a `<base>` tag. The development server needs `'unsafe-eval'` (React's debugging) and the hot-reload websocket, which it gets only in development.
- **A flaw found in my own first test:** the first attempt ran its "inline script" and `eval` attacks through Playwright's `page.evaluate`, which goes through DevTools and is exempt from CSP, so they appeared to succeed. They proved nothing; the attacks were redone by injecting the markup into the served HTML, which is how a stored XSS delivers it.
- **A setup slip, also mine:** `BACKEND_URL` is read at build time, and a first build pointed the browser test at the developer's own backend instead of the scratch one (harmless: it only reached a login redirect). The README and `docs/DEPLOYMENT.md` already say this; the test script now builds with the right value.
- **Tests:** 20 unit tests for the policy and 7 for the proxy (nonce per request and forwarded for Next, report-only and off modes, https detection behind a proxy, which paths it covers). **The browser check is manual and is not in CI.**
- **Remaining gaps** are listed under O1 in `docs/THREAT_MODEL.md`: `style-src 'unsafe-inline'`; `'strict-dynamic'` trusts scripts created by trusted scripts; no violation reporting; per-request rendering; no CSP on API responses.

**Metrics, health and alerts (done except tracing)**
- **Metrics** (`prometheus-client`, `GET /metrics`, bearer `METRICS_TOKEN`, 404 unless configured; the worker serves the same on `WORKER_HTTP_PORT`): HTTP requests and durations by *route template*; indexing steps by phase and outcome, step durations, and jobs that failed for good by error code; calls to embedding and chat providers by provider, operation and outcome (an error code, never the provider's message) with latency; and gauges read from the database at scrape time, so they are right in either process and survive restarts: `cip_index_jobs{phase,state}` (waiting, running, backing_off), `cip_index_oldest_waiting_seconds` (queue lag) and `cip_index_failed_jobs`. Every label comes from a small fixed set (unknown routes share one label; unusual HTTP methods become `OTHER`).
- **No content in metrics, by test:** after a private repository is indexed, searched and asked about with marker strings in the code and the questions, the output contains none of them, nor the repository name, ids, user name or keys. A provider error body that echoes the key is not turned into a label. 15 safeguards were mutation-tested; one gap (a leased job older than any waiting job was counted in the queue lag) was found and closed.
- **Worker health:** `/health` on the worker's own port reports unhealthy when its loop has not turned for `WORKER_HEALTH_STALE_SECONDS` (default 120, above the longest step), so an orchestrator can restart a hung worker; `/metrics` needs the token and does not exist without one; nothing else is served and there is no access log. The API's existing `/health` and `/ready` are unchanged.
- **Alerts** (`infra/prometheus/alerts.yml`): queue stuck, jobs failing for good, indexing step errors, 5xx rate, ordinary-route latency (questions and AI reviews are excluded because a free model takes 10 to 30 seconds), provider error rate, and a target down. Checked with Prometheus's own `promtool check rules`, and ten `promtool test rules` scenarios feed them synthetic series (each alert fires when it should, and a short wait, one failed job among many, 4xx responses and slow chat requests do not trip them); deliberately breaking two rules made those tests fail. Both run in CI. **The thresholds are starting guesses, not tuned values.**
- **Checked for real, locally:** a real API and worker against a scratch database, scraped by a real Prometheus container: both targets `up`, the queue gauges arrived, all seven rules loaded, and killing the worker turned its `up` to 0 (the signal the target-down alert watches).
- **Not built:** distributed tracing (OpenTelemetry); a dashboard; a hosted Prometheus or Grafana setup; a firing alert has never been delivered anywhere (no Alertmanager or notification route is configured); the alert thresholds have never seen production traffic.

**Audit log and log hygiene (done)**
- **What is recorded:** private repository connect, disconnect, every read or query (named by the route, such as `private.list_files` or `private.ask`), lost access, retention purges, and provider saves and removals (slot, kind and mode only). Status polling and job ticks are not recorded (they run every few seconds and would bury real events). Public repositories are not audited.
- **What is never recorded:** file contents, questions, answers, prompts or keys. `audit.record` accepts only short single-line plain values in `detail` and raises otherwise (tested), so a content leak by accident fails loudly.
- **Fails closed:** if the audit write fails, the private read is not served (tested).
- **Design:** table `audit_events` (migration `0009`), no foreign keys so events outlive the repository or account they describe. Users can read their own trail at `GET /api/audit`. Events are deleted after `AUDIT_RETENTION_DAYS` (365; 0 keeps them) by the same scheduled purge as private data. The application has no code that edits or deletes an event other than that purge.
- **Not tamper-proof:** a database administrator can change rows; this is a record for the user and the operator, not evidence that survives a hostile operator. A trigger or an external append-only sink would be the next step and is not built.
- **Log hygiene test:** a private repository is indexed, searched, asked about and AI-reviewed, including failure paths (unreadable, empty, unverifiable and refusing model replies, crashes in the model client and the embedder, missing browser-held keys), with marker strings planted in the code, a credential-like value, the questions, the model replies and the keys. Every record from every logger at DEBUG, including extra fields and tracebacks, is searched for them. A second test drives the real provider clients over mocked HTTP with error bodies that echo the key and prompt back. A control test proves the check fails when a leak is present. 7 leaks planted in the code (question, prompt, request headers, chunk text, model reply, browser key, stored key) were all caught; one gap (the real client builders were replaced by fakes in the flow tests) was found this way and closed.
- **Limit:** the test shows today's code paths do not log these values; it cannot prove no future code will, which is what the planted-leak checks are for. Provider and GitHub error messages are passed on to users but not logged.

## 3. Testing, deployment, risks

**Testing and evaluation.** Unit tests use fixtures and the stub providers, so CI needs no keys. Integration tests run against a real Postgres with pgvector. The benchmark uses one pinned small repo and a hand-labeled question set, and reports Recall@k and MRR for keyword-only, vector-only and hybrid. The README will state the dataset size and its limits, and quote only numbers the script produced.

**Deployment**
- **Neon:** create a project, enable `vector`, copy the pooled URL for the app and the direct URL for Alembic.
- **Render:** one free web service from `render.yaml`, migrations on deploy, secrets in the dashboard (`DATABASE_URL`, `OPENROUTER_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `GITHUB_TOKEN`).
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
