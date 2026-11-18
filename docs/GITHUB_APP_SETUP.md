# GitHub App setup (one time, about 10 minutes)

The app is used only to sign users in and give each user their own GitHub rate limit. It needs **no repository permissions** until private repos arrive in M9. You do this yourself because it needs your GitHub account; the backend and frontend are built and tested without it, and the real login is the last thing verified in M3.

Never paste the client secret or any key into a chat, an issue, or a commit. They go only in `backend/.env`, which is gitignored.

## 1. Create the app
GitHub, Settings, Developer settings, GitHub Apps, **New GitHub App**.

| Field | Value |
| --- | --- |
| GitHub App name | Any unique name |
| Homepage URL | `http://localhost:3000` for now |
| Callback URL | `http://localhost:3000/api/auth/github/callback` |
| Expire user authorization tokens | **Checked** (this gives you refresh tokens) |
| Request user authorization (OAuth) during installation | Unchecked |
| Webhook, Active | **Unchecked** |
| Repository permissions | None |
| Account permissions | None (we do not request email) |
| Where can this GitHub App be installed? | **Any account**, so other people can sign in |

The callback goes through the frontend (`:3000`) because the frontend proxies `/api` to the backend. You can add more callback URLs to the same app later, for example `https://<your-vercel-domain>/api/auth/github/callback` at deployment (M8).

## 2. Collect the values
On the app's settings page:
- Copy the **Client ID**.
- Under "Client secrets", click **Generate a new client secret** and copy it immediately (it is shown once).

## 3. Generate two local secrets
```bash
# session signing secret
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# token encryption key (32 random bytes, url-safe base64; needs no extra packages)
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

## 4. Put them in `backend/.env`
```bash
GITHUB_APP_CLIENT_ID=...
GITHUB_APP_CLIENT_SECRET=...
SESSION_SECRET=...
TOKEN_ENCRYPTION_KEY=...
APP_BASE_URL=http://localhost:3000
COOKIE_SECURE=false
```

Put them in the repo-root `.env` (docker compose reads that one too) or in `backend/.env`; the API reads both, and `backend/.env` wins if a name is in both. Restart the API after editing. `.env.example` lists every name. If you rotate `TOKEN_ENCRYPTION_KEY`, users have to sign in again.

## 5. Run it locally
Three terminals (the frontend is not in `docker-compose.yml` yet):

```bash
docker compose up -d db                                   # Postgres with pgvector
cd backend && uv run alembic upgrade head && uv run uvicorn app.main:app --reload   # API on :8000
cd frontend && npm install && npm run dev                 # app on :3000
```

Open http://localhost:3000, choose **Continue with GitHub**, approve, and you land on your repositories. Connect a public repo and press **Index latest commit**; the page drives the job forward and shows progress.

If sign-in fails:
- `auth_not_configured` on the login page: one of the four secrets is missing from `backend/.env`.
- GitHub says the redirect URI is not valid: the Callback URL in the app settings must be exactly `http://localhost:3000/api/auth/github/callback`.
- You land back on the login page with "link expired": the state cookie did not survive. Make sure you start at `http://localhost:3000` (not `127.0.0.1`) and that `APP_BASE_URL` matches.
- Indexing says "authorization expired": GitHub did not accept the user token for that call; sign in again. If it keeps happening for public repos, tell me: it would mean a GitHub App user token needs a permission I assumed it did not.

## Production
The full deployment steps are in [DEPLOYMENT.md](DEPLOYMENT.md). For the GitHub App specifically:
- Add `https://<your-vercel-domain>/api/auth/github/callback` as a second Callback URL (keep the localhost one) and set the Homepage URL to the Vercel URL.
- Put the same Client ID and secret on Render as secrets, with `APP_BASE_URL` set to your Vercel URL and `COOKIE_SECURE=true`. The API will not start in production without them.
- Render's free tier sleeps, so the first sign-in after idle can take 30 to 60 seconds; the app says it is waking the server.
