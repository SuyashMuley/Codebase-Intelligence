# Load test and restore drill

Tools for measuring the API under load and for rehearsing a backup and restore. Results and what
they mean are in [docs/LOAD_TEST.md](../../docs/LOAD_TEST.md) and
[docs/RUNBOOK.md](../../docs/RUNBOOK.md). **Everything here runs on one machine**, so the load
generator, the API and Postgres compete for the same cores. The numbers compare configurations and
find the first bottleneck; they do not predict production.

You need Docker (for k6, and for Postgres 16 with pgvector), the project's compose database
running (`docker compose up -d db`), and `uv`.

## 1. A database to test against

```bash
cd backend
createdb_cmd='docker exec codebaseintelligenceplatform-db-1 psql -U cip -d cip -c'
$createdb_cmd "CREATE DATABASE cip_load"
export DATABASE_URL=postgresql+psycopg://cip:cip@localhost:5432/cip_load ALEMBIC_DATABASE_URL=
export EMBEDDING_PROVIDER=hash OPENROUTER_API_KEY= COOKIE_SECURE=false ENVIRONMENT=development
export GITHUB_APP_CLIENT_ID=cid GITHUB_APP_CLIENT_SECRET=csecret-value-123
export SESSION_SECRET=load-session-secret-0123456789abcdef
export TOKEN_ENCRYPTION_KEY=$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
uv run alembic upgrade head
uv run python -m loadtest.seed --files 1250 --users 50      # about 6,000 chunks, the app's cap
```

`seed` indexes a deterministic synthetic repository through the real pipeline and creates 50 users
with live sessions. It writes `loadtest/fixture.json`, which **contains real session tokens for
that database**: it is git-ignored, keep it that way.

## 2. The load test

The rate limits are raised so they are not what is being measured (they have their own tests):

```bash
export RATE_LIMIT_PER_MINUTE=10000000 IP_RATE_LIMIT_PER_MINUTE=10000000 SEARCH_RATE_LIMIT_PER_MINUTE=10000000
export CHAT_RATE_LIMIT_PER_MINUTE=10000000 AI_RATE_LIMIT_PER_MINUTE=10000000 CHAT_DAILY_LIMIT=100000000
uv run python -m loadtest.run_load --label pool-3+2 --pool 3 --overflow 2 --levels 1,5,10,25,50,100,200
```

It starts the real API (`loadtest/server.py`: the app with only the chat model replaced by a fast
fake, so the model's own 10 to 30 seconds is not measured), then for each level runs k6 for 20
seconds of a mixed workload (browse, view a file, search by meaning and by keyword, graph, impact,
ask) while sampling the API process and the database. Output: a table, and
`loadtest/results/<label>.json`.

## 3. The restore drill

**Never run.** The script and `infra/backup/*.sh` are untested drafts: nothing about them is verified.

```bash
uv run python -m loadtest.restore_drill      # needs the seeded cip_load database and the exports above
```

It backs the database up with `infra/backup/backup.sh`, restores it with `infra/backup/restore.sh`
into a new database in a new container, and checks the result field by field. See the script's
header for the nine steps.
