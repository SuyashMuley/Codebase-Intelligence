# Codebase Intelligence Platform: Plan and Tasks

Last updated: 2026-11-17

## 1. Scope and fixed decisions

The MVP is a web app that ingests one pinned commit of a public GitHub repo, parses it with Tree-sitter, builds a static dependency graph, and answers questions with validated file and line citations. It runs on free tiers with no always-on worker.

**In scope**
- Connect a public repo by URL, pin a commit SHA, ingest, and re-index when the default branch moves.
- Parse Python, JavaScript, TypeScript and Go; extract definitions, imports, and statically resolvable calls.
- Hybrid retrieval (Postgres full-text plus pgvector, fused with RRF) and graph expansion.
- Chat with validated citations, and change-impact analysis that separates static facts from AI suggestions.
- Dashboard: overview, ingestion progress, file explorer, React Flow graph, chat, impact panel, stats.

**Out of scope for the MVP**
- Private repos and OAuth, streaming chat, per-file incremental re-indexing, Go call edges, languages beyond the four.

**Fixed decisions**

| Concern | Decision |
| --- | --- |
| LLM | OpenRouter, OpenAI-compatible client, model ID in env, one fallback model |
| Embeddings | Hugging Face Inference API, `bge-small-en-v1.5` (384 dims), model ID in env |
| Tests and CI | Stub LLM and a deterministic hash embedder, so no network or keys |
| Background work | None. A resumable job state machine advanced by `tick` calls |
| Hosting | Vercel (frontend), Render free (API), Neon free (Postgres plus pgvector) |
| Never | Execute repo code, store detected secrets, trust repo text as instructions |

**Success criteria**
- A public repo up to the quota size indexes end to end on the free tiers.
- Every cited path and line range in an answer exists in the retrieved chunks (enforced and tested).
- A reproducible script reports Recall@k and MRR for keyword-only vs hybrid on a labeled set. Only measured numbers go in the README.

See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams, schema, API contracts, trade-offs and the security model.

## 2. Milestones and tasks

Seven milestones, each ending with a working, tested slice. After each one: what was built, how to run it, test results, known limits, next step. M1 to M4 give a fully tested backend with real benchmark numbers before any UI exists.

### M1. Skeleton and URL validation
- [ ] Monorepo layout, `pyproject.toml`, Ruff and mypy config
- [ ] `docker-compose.yml` with Postgres plus pgvector, backend container
- [ ] FastAPI app factory, structured JSON logging, request IDs, error envelope
- [ ] Settings via env, explicit CORS, `/health` and `/ready`
- [ ] Alembic setup and the first migration (all eight tables, extensions)
- [ ] `POST` and `GET /api/repositories` with GitHub URL validation
- [ ] Tests: URL validation (valid, malformed, non-GitHub, traversal tricks), error envelope, readiness
- [ ] GitHub Actions CI: lint, type check, tests against a Postgres service

**Exit:** `docker compose up` serves OpenAPI, a repo can be registered, CI is green.

### M2. Resumable ingestion
- [ ] GitHub client: resolve ref to SHA, download tarball, honor rate-limit headers, optional token
- [ ] Filters: size caps, binary detection, vendored and generated paths, language detection
- [ ] Snapshot uniqueness on (repo, commit); `index`, `index/tick`, `status` endpoints
- [ ] Job state machine with lease, cursor, attempts, and error capture
- [ ] Quotas: tarball size, file count, ingestions per client per day
- [ ] Tests: duplicate commit, oversize repo, interrupted tick then resume, expired lease, GitHub errors (mocked)

**Exit:** a small public repo reaches the parse phase, and killing the process mid-job loses nothing.

### M3. Parsing, chunking, secret handling
- [ ] Tree-sitter adapters for Python, JS, TS, Go: symbols, imports, call sites
- [ ] Syntax-aware chunker with a size fallback and line ranges
- [ ] Secret scanner and redaction applied before storage
- [ ] Fixture repos per language
- [ ] Tests: AST extraction, import extraction, chunk boundaries and line numbers, secret redaction

**Exit:** a snapshot has files, symbols and chunks with correct line ranges.

### M4. Embeddings, retrieval, benchmark
- [ ] `Embedder` interface, Hugging Face client (batching, retry, backoff), hash embedder
- [ ] Resumable embed phase with a cursor, HNSW index after bulk insert
- [ ] Keyword search, vector search, RRF fusion
- [ ] Labeled dataset of 30 to 50 questions on one pinned small repo
- [ ] `eval/run_eval.py`: keyword-only vs vector-only vs hybrid, Recall@k and MRR, results written to disk
- [ ] Tests: embedding storage and dimension check, retrieval on a fixture, eval script runs end to end

**Exit:** a reproducible eval report with measured numbers.

### M5. Graph and impact analysis
- [ ] Edge builder: imports, contains, and calls resolved through import bindings and same-module names
- [ ] Unresolved calls stored with `resolution = unresolved`, excluded from facts
- [ ] Graph and file endpoints; BFS for direct and transitive dependents
- [ ] Suggested tests by naming and import proximity, labeled as heuristic
- [ ] AI suggestion step, kept separate from static results, plus confidence and limitations
- [ ] Tests: graph construction on fixtures, no invented edges for dynamic calls, transitive depth, impact response shape

**Exit:** the impact endpoint returns static dependents with edge paths and clearly labeled suggestions.

### M6. Question answering
- [ ] OpenRouter client with timeout, retry, fallback model, and a stub
- [ ] LangGraph nodes: classify, scope, retrieve, expand, assemble, generate, validate
- [ ] Conditional edges: insufficient evidence, one regeneration on citation failure
- [ ] Prompt-injection hardening and a hostile-content fixture
- [ ] Chat quotas and rate limits
- [ ] Tests: citation correctness (every cited range exists), uncertainty path, injection fixture, validator rejects fabricated IDs

**Exit:** answers carry validated file and line citations, and weak evidence yields an honest "cannot determine".

### M7. Frontend and deployment
- [ ] Next.js, Tailwind dark theme, typed API client, TanStack Query
- [ ] Pages: repo list and connect, overview and stats, ingestion progress with tick driver
- [ ] File explorer with Monaco and citation jump-to-line
- [ ] React Flow graph with file and symbol levels, static vs unresolved styling
- [ ] Chat panel and impact panel; loading, error and empty states; waking-backend banner
- [ ] Dockerfiles, `vercel.json`, `render.yaml`, Neon setup notes, `.env.example`
- [ ] README, architecture docs with Mermaid, API docs, demo script

**Exit:** the deployed app indexes and queries a real repo, and the demo script runs start to finish.

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
