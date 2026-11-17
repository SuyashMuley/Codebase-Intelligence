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
# token encryption key (Fernet); M3 adds the `cryptography` dependency, so run this after M3 lands
cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 4. Put them in `backend/.env`
```bash
GITHUB_APP_CLIENT_ID=...
GITHUB_APP_CLIENT_SECRET=...
SESSION_SECRET=...
TOKEN_ENCRYPTION_KEY=...
APP_BASE_URL=http://localhost:3000
```

These names are what M3 will read; `.env.example` will list them when M3 lands. If you rotate `TOKEN_ENCRYPTION_KEY`, users have to sign in again.

## Deployment notes (M8)
- Add the production callback URL to the app.
- Set the same variables as secrets on Render, with `APP_BASE_URL` set to your Vercel URL and cookies marked Secure.
- Render's free tier sleeps, so the first sign-in after idle can take 30 to 60 seconds. The login page will say so.
