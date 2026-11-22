# Remote control-plane setup and limitations

The remote-access feature is a bounded local prototype, not a production cloud deployment. It keeps device/control data in a separate SQLite file and currently supports one owner account and two read-only agent operations. The owner session protects remote-access and control-plane operations only; local chat, document, memory, task, and agent APIs do not require that login and are intended for the existing localhost-only single-user application. Keep the application local; `CONTROL_PLANE_ONLY=true` hides local chat/document routes but does not make the prototype safe for public deployment.

## Backend configuration

The backend reads `backend/.env` relative to the backend source, not the current shell directory. Create it from the example if needed:

```powershell
Copy-Item .\backend\.env.example .\backend\.env
```

Set these values in `backend\.env` for local control-plane validation:

```dotenv
FRONTEND_ORIGIN=http://localhost:3000
AUTH_COOKIE_SECURE=false
AUTH_SESSION_HOURS=12
CONTROL_PLANE_ONLY=false
```

Owner sign-in requires a private `AUTH_JWT_SECRET` of at least 32 bytes. Generate it once from the backend environment and store it only in `backend/.env`:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set `AUTH_JWT_SECRET` to the generated value. Keep it stable to preserve active sessions; changing it invalidates existing JWTs. Do not commit the value or place it in frontend variables.

`AUTH_COOKIE_SECURE=false` is only for localhost HTTP development. A network-accessible HTTPS control-plane instance requires `AUTH_COOKIE_SECURE=true`, a valid TLS reverse proxy, an exact `FRONTEND_ORIGIN`, and `CONTROL_PLANE_ONLY=true`. Do not expose the FastAPI development server directly to the internet. This release has no PostgreSQL backend and the SQLite control-plane database is single-host/single-instance; these settings do not make a public deployment production-safe.

The control-plane database is `%LOCALAPPDATA%\RMatrixCommandCenter\control_plane.sqlite3` by default (or `<R_MATRIX_DATA_DIR>\control_plane.sqlite3` when that override is set). Its schema is applied through a versioned migration table. It is separate from the local chat/document SQLite database; the main personal-data store is single-owner, not multi-user.

## Create the owner account

From the repository root, run the bootstrap command in the backend virtual environment. It prompts securely for a password and does not accept a password as a command-line argument:

```powershell
Set-Location 'C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\backend'
.\.venv\Scripts\python.exe -m app.bootstrap_owner
```

Choose a password with at least 12 characters. The bootstrap command refuses to create any second owner account. Password hashes use PBKDF2-HMAC-SHA256 with 600,000 iterations, and older hashes upgrade after successful sign-in. There is currently no self-service password reset or account recovery flow; protect the owner credential and local database accordingly.

## Two-factor sign-in (Telegram)

When `TELEGRAM_BOT_TOKEN` is configured and a chat is paired in Settings → Telegram, sign-in becomes two steps: the owner password first, then a six-digit code delivered privately by `@RMatrixPersonalAIBot`. No session cookie is issued until both steps pass.

- Codes are generated with `secrets.randbelow`, expire after `AUTH_OTP_TTL_MINUTES` (default five), and are single-use.
- Only a SHA-256 hash of each code is stored, bound to that challenge and owner. Codes are never logged, returned by the API, or written to the frontend.
- Each challenge allows `AUTH_OTP_MAX_ATTEMPTS` (default five) incorrect entries, then it is locked. Resends rotate the code, honour a `AUTH_OTP_RESEND_SECONDS` (default 30s) cooldown, and cap at five sends.
- Challenge creation is limited to five per owner per fifteen minutes. A pending challenge is not a session and cannot read any private API.
- The flow fails closed: while `AUTH_TELEGRAM_OTP_ENABLED=true`, a missing bot token, a missing pairing or a delivery failure blocks sign-in. Password verification alone never issues a session, and there is no silent password-only fallback.

### If Telegram is unavailable

There is no unauthenticated bypass and no recovery code. The owner, who already has local filesystem access to the backend, may temporarily set `AUTH_TELEGRAM_OTP_ENABLED=false` in `backend/.env`, restart the backend, sign in with the password only, repair Telegram pairing in Settings → Telegram, and then restore `AUTH_TELEGRAM_OTP_ENABLED=true` and restart again. Treat that window as a deliberate local recovery procedure, not a routine workaround, and close it immediately afterwards.

Start the backend and dashboard using the existing project commands. Sign in at the shared login gate to access any protected page. Browser sessions use short-lived HS256 JWTs in HttpOnly, SameSite=Strict cookies; JWT hashes are stored server-side so logout and revocation take effect immediately. State-changing browser requests require a session-bound CSRF token. The control-plane database stores password hashes and hashes of session, enrollment, and device tokens, not the raw device credential.

## Enroll and run the Windows agent

In Remote Access, select **Create enrollment code**. The code is single-use, expires after ten minutes, and is displayed only once. On the Windows device to enroll, install the agent's separate requirements and start the prompt-based enrollment:

```powershell
Set-Location 'C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\backend'
.\.venv\Scripts\python.exe -m pip install -r agent\requirements.txt
.\.venv\Scripts\python.exe agent\r_matrix_agent.py --enroll --server http://127.0.0.1:8000 --name "My Windows PC"
```

Use the HTTPS origin instead of the localhost example for a remote control plane. Non-local HTTP URLs are rejected. The prompt does not echo the enrollment code. The returned long-lived device credential is stored through the Windows Credential Manager keyring backend; the agent refuses to start if a Windows keyring backend is unavailable. The local agent config file contains only the server origin and device ID.

Run the agent in a separate PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe agent\r_matrix_agent.py
```

Its only allowed work is responding to explicitly queued `health.check` and `agent.status` jobs. It polls outbound, uses normal TLS certificate validation, and never opens a listener. The Remote Access page shows its last contact and job results. Use **Revoke** to invalidate its device credential.

## Verify and troubleshoot

1. Confirm the owner can sign in to Remote Access.
2. Create and use a one-time enrollment code; the enrolled device appears in the device list.
3. Keep the agent running and queue **health check** or **agent status**.
4. Refresh the dashboard and verify the job reaches `succeeded` with a small status result.
5. Revoke the device and verify it can no longer poll.

If sign-in fails, ensure the bootstrap command ran against the same `R_MATRIX_DATA_DIR` used by FastAPI and that the password has not been mistyped. If a browser request reports CSRF verification failure, refresh the dashboard so it obtains a fresh token. If enrollment fails, issue a new code and use it before the ten-minute expiry. If the agent cannot reach the server, verify the HTTPS certificate and reverse-proxy routing for `/api/control/`; do not disable TLS checks. A revoked device must be enrolled again with a new code.

## Current limitations

- One owner account; no invites, password-reset workflow, or multi-user authorization model. Multi-factor sign-in exists through the paired Telegram chat only, and is skipped while Telegram is unconfigured or unpaired.
- SQLite persistence is single-host and has no PostgreSQL adapter or production migration rollout.
- Agent operations are intentionally read-only and do not provide file browsing, shell execution, document transfer, or consequential actions.
- Control-plane-only mode hides chat/document/model/provider APIs, but health remains available and static hosting/reverse-proxy hardening is still required.
- No production deployment has been provisioned or tested. Do not expose this prototype publicly or send private document data through it.
