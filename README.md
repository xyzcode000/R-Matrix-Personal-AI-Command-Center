# R Matrix Personal AI Command Center

**One Intelligence. Your Entire Digital World.**

A local-first development application for private document search and chat with Ollama, OpenAI, Anthropic, or Google Gemini. The dashboard and API run on localhost; uploaded documents, conversation records, and the SQLite database remain in the local application data directory. The local workspace has no global login gate; owner sign-in is limited to the separate Remote Access control plane. Cloud chat sends your message and eligible conversation history to the selected provider. Matching local document excerpts are sent only after per-request opt-in. This is not a remote deployment and is not ready for public access.

## Current status

| Capability | Status |
|---|---|
| Local dashboard and navigation | Implemented |
| Backend and provider configuration indicators | Implemented |
| Ollama, OpenAI, Anthropic, and Gemini chat adapters | Implemented; automated tests use mocks |
| Provider/model metadata in saved messages | Implemented |
| Per-request consent for cloud document context | Implemented |
| PDF, scanned PDF, DOC/DOCX, TXT, Markdown, CSV, XLSX, PPTX, PNG, JPG/JPEG, and WEBP ingestion | Implemented with local OCR requirements noted below |
| Ranked local keyword search with page/slide and available section references | Implemented; not semantic search |
| Document-aware chat, source display, and pre-send cloud excerpt review | Implemented |
| Structured owner profile and local identity/memory context | Implemented; profile context is not sent to cloud by default |
| Task deadlines, editing, completion history, and persisted reminder queue | Implemented; SMTP configuration is required for email delivery |
| Web research page and authorized project-folder search | Implemented with their existing backend services |
| Central Manager and document, project, task, and memory specialists | Implemented with deterministic routing and an allowlisted tool registry; mocked/local automated coverage |
| Chat memory proposals and review, search, edit, and deletion | Implemented with explicit in-chat approval and existing local SQLite storage |
| Consequential action execution | Not implemented; requests only create non-executable pending proposals |
| Remote Access owner sign-in, device enrollment/revocation, and read-only Windows agent requests | Implemented for Remote Access only; local workspace remains login-free |

## Architecture

- `frontend/`: Next.js, React, TypeScript dashboard.
- `backend/`: FastAPI API, SQLite persistence, local document extraction/search, provider router, and four provider adapters.
- `backend/app/control_plane.py`: a separate, versioned SQLite database for owner sessions, device credentials, and bounded remote jobs.
- `backend/app/agents.py`: deterministic Manager routing, typed specialist/tool contracts, permissions, and bounded local orchestration.
- `backend/app/database.py`: existing chat/document persistence extended with task, approved-memory, proposal, approval, and execution records.
- `backend/agent/`: outbound-only Windows agent; only reports basic health/runtime status and does not access files, run commands, or make changes.
- Local application data defaults to `%LOCALAPPDATA%\\RMatrixCommandCenter` on Windows. The SQLite database and uploaded files are stored there, outside the repository. Set `R_MATRIX_DATA_DIR` to use another local directory.
- The control-plane database is `control_plane.sqlite3` in that data directory. It currently uses single-host SQLite and is not suitable for a multi-instance public service.
- Ollama defaults to `http://127.0.0.1:11434`; the configured model defaults to `qwen2.5:3b`.
- `AI_PROVIDER=ollama` selects the default provider. Legacy `MODEL_PROVIDER` is supported when `AI_PROVIDER` is unset. Provider selection can also be changed in the chat UI for providers configured in `backend/.env`.
- `AUTHORIZED_PROJECT_DIRS` is an optional semicolon-separated Windows list of read-only project roots. If omitted, only the current repository root is allowlisted. The Developer/Project specialist accepts configured project IDs, never arbitrary paths.

The backend binds to `127.0.0.1` by default and rejects non-loopback `APP_HOST` values. `FRONTEND_ORIGIN` must also be a localhost or loopback origin. The default configuration permits the common local aliases `http://localhost:3000` and `http://127.0.0.1:3000`; set `FRONTEND_ORIGIN` in `backend/.env` if using a different local origin. Ollama endpoints are restricted to `localhost`, `127.0.0.1`, or `::1`. Cloud adapters use fixed vendor HTTPS endpoints, and API keys remain on the backend. Local workspace APIs do not require a global login; Remote Access operations use the separate owner session and CSRF protection. This single-user SQLite application remains local-only and is not production-ready or suitable for public exposure.

## Requirements

- Windows PowerShell
- Python 3.11 or newer (the current development environment uses Python 3.14)
- Node.js 20.9 or newer and npm
- Ollama for AI chat; document upload and keyword search work without it
- Tesseract OCR for scanned PDFs and image OCR (run `scripts\setup-document-ocr-windows.ps1` once)

## Setup and launch

Open the repository folder in VS Code. Use **Terminal 1** for FastAPI and **Terminal 2** for Next.js. From the repository root, run the commands below.

### Terminal 1 — backend

```powershell
cd "C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\backend"
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Preserve an existing local config and signing key.
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Select-String -Path .env -Pattern '^AUTH_JWT_SECRET=.{32,}$' -Quiet)) {
  $jwtSecret = python -c "import secrets; print(secrets.token_urlsafe(48))"
  Add-Content -Path .env -Value "AUTH_JWT_SECRET=$jwtSecret"
  Remove-Variable jwtSecret
}
# Run once only, to initialize the single owner account.
python -m app.bootstrap_owner
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The generated signing key is private and must remain stable in `backend/.env`; never commit it. The bootstrap command creates the single owner account once and prompts for the password without echoing it. If PowerShell blocks activation, use `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`, generate the key with `.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"`, add it to `.env`, and use `.\.venv\Scripts\python.exe -m app.bootstrap_owner` and `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000` instead. The backend health endpoint is `http://127.0.0.1:8000/api/health`.

### Terminal 2 — frontend

```powershell
cd "C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\frontend"
Copy-Item .env.local.example .env.local
npm install
npm run dev
```

Open **http://localhost:3000** after Next.js reports that it is ready. The browser API client follows the same loopback hostname to keep strict SameSite cookies working.

### Ollama

Keep Ollama running locally. Check it from PowerShell:

```powershell
ollama --version
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

If the configured model is not listed, install it with:

```powershell
ollama pull qwen2.5:3b
```

Ensure `OLLAMA_MODEL` exactly matches a model shown by `ollama list`; in this workspace it is set to the installed `llama3:latest`. The application does not download models automatically. Keep `AI_PROVIDER=ollama` and restart FastAPI after changing settings. Check `http://127.0.0.1:8000/api/health/model` for the configured provider and model state. `http://127.0.0.1:8000/api/health/ollama` remains available for the Ollama-specific status.

### Document OCR

The Python extractors install with `python -m pip install -r requirements.txt`. Scanned PDFs and image uploads additionally require the real Tesseract executable; `pytesseract` on its own is only a wrapper. Run the repeatable setup script once from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-document-ocr-windows.ps1
```

It detects an existing installation, installs Tesseract through winget when it is missing, verifies the executable, checks language data, runs a synthetic OCR self-test, and explains any remaining manual step. It never reads an owner document. To record the detected path explicitly, add `-WriteEnv`; the backend otherwise resolves `TESSERACT_CMD`, `PATH`, and standard Windows installation folders automatically and only reports OCR as ready when its runtime probe passes.

Confirm the whole pipeline at any time:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\diagnose-document-pipeline.ps1
```

OCR is local; documents are not submitted to an external OCR service. English is required; Hindi is used automatically when `hin.traineddata` is installed. The engine is bounded to 100 PDF pages and 10 OCR-required pages per document. Scanned documents are never marked Ready with zero chunks: they become **Retry required** (missing dependency) or **Partial** (some pages unread), and **Retry processing** re-reads the stored original without re-uploading. **Rebuild index** only rebuilds chunks from already-extracted pages. Legacy `.doc` files require Antiword on `PATH`; convert to DOCX if Antiword is unavailable. See [Personal Knowledge Engine](docs/KNOWLEDGE-ENGINE.md) for formats, processing statuses, recovery, retrieval behavior, encryption status and source grounding.

### Task reminder email

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_USE_TLS` in `backend/.env`. Enter the recipient email under **Settings → Email task reminders**, then use **Send test email**. For Gmail, use `smtp.gmail.com:587`, STARTTLS, and a Google App Password (not your account password). SMTP credentials stay on the backend and are not stored in SQLite. The backend and machine must be running to send reminders on time; pending reminders are processed after restart, but local operation cannot guarantee exact-time delivery while offline.

### Provider and credential configuration

The backend reads provider settings from `backend/.env`. Start with the empty cloud-key placeholders in `.env.example`; do not put credentials in the frontend, `NEXT_PUBLIC_*`, source code, tests, or Git. Set both a key and model ID to enable each cloud option:

| Provider | Selection value | Backend settings |
|---|---|---|
| Ollama | `ollama` | `OLLAMA_BASE_URL` (loopback only), `OLLAMA_MODEL` |
| OpenAI | `openai` | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| Anthropic Claude | `anthropic` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |
| Google Gemini | `gemini` | `GEMINI_API_KEY`, `GEMINI_MODEL` |

`AI_PROVIDER` sets the default selected provider; it defaults to `ollama`. Set it to one of the values above or select a configured option in AI Chat. Cloud model IDs are supplied by you and must be supported by your provider account. `/api/providers` reports whether a provider has the required configuration; it does not make a cloud call or claim that credentials have been verified. `/api/models` lists the configured cloud model ID without testing it.

Gemini uses the official `google-genai` Python SDK with asynchronous `models.generate_content` calls. `GEMINI_MODEL` defaults to `gemini-2.5-flash`, which is listed in Google's current [Gemini API model catalog](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash); you can change this value to another model enabled for your account. The model identifier is a configuration value, not a connectivity check.

The backend configuration file is `backend\.env` (absolute path: `C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\backend\.env`). It is private and excluded by `.gitignore`. If it does not exist, create it from the sample once:

```powershell
if (-not (Test-Path .\backend\.env)) { Copy-Item .\backend\.env.example .\backend\.env }
```

Then edit `backend\.env` locally and set `GEMINI_API_KEY=YOUR_GEMINI_API_KEY` (replace the placeholder with your key) and `GEMINI_MODEL=gemini-2.5-flash`. Never send the key in chat or put it in frontend configuration. From the repository root, stop the running FastAPI process with Ctrl+C, then restart it with:

```powershell
Set-Location 'C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\backend'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In AI Chat, select **Google Gemini** and verify the displayed model ID. Send a test prompt and confirm a response is returned with `gemini · gemini-2.5-flash` metadata. This is the manual live verification procedure; automated tests use mocks and no live Gemini inference is claimed. To return to Ollama, select **Ollama** in AI Chat (or set `AI_PROVIDER=ollama` in `backend\.env` and restart FastAPI).

If Gemini is disabled in the selector or `GET /api/providers` says it is not configured, check that `GEMINI_API_KEY` is set and non-empty. `GEMINI_MODEL` must be a supported Gemini model identifier, not a display label or API-key name; the configured value here is `gemini-2.5-flash`. Gemini reports actionable, redacted errors for invalid credentials, access or billing problems, unsupported models, invalid requests, quota limits, timeouts, and empty responses. Restart FastAPI after changing `.env`.

### Tests and checks

Backend tests (run from `backend` with its virtual environment active):

```powershell
python -m pytest
```

Frontend checks (run from `frontend`):

```powershell
npm run lint
npm run typecheck
npm run build
```

### Stop services

Press **Ctrl+C** in each terminal. To remove an environment later, deactivate it and remove only the named `backend\.venv` directory after confirming it contains no user data. Do not delete the application data directory if you want to keep chats or indexed documents.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | Backend and SQLite health |
| `GET` | `/api/auth/csrf` | Issue the browser CSRF bootstrap token |
| `POST` | `/api/auth/login` | Sign in the initialized owner |
| `GET` | `/api/auth/me` | Return the authenticated owner identity |
| `POST` | `/api/auth/logout` | Revoke the current owner session |
| `GET` | `/api/health/model` | Selected provider configuration; cloud connectivity is not tested |
| `GET` | `/api/health/ollama` | Ollama-specific availability |
| `GET` | `/api/providers` | Provider configuration inventory; never returns API keys |
| `GET` | `/api/models` | Local installed models or the configured cloud model ID |
| `POST` | `/api/chat` | Selected-provider chat; optional `conversation_id`, `provider`, `model`, and `allow_cloud_document_context` |
| `GET` | `/api/conversations` | Recent saved conversations |
| `GET` | `/api/conversations/{id}/messages` | Persistent conversation messages and sources |
| `GET` | `/api/documents` | Indexed document metadata |
| `POST` | `/api/documents/upload` | Multipart upload field `file`; supported office formats and images; 10 MiB default limit |
| `GET` | `/api/documents/{id}/content` | Extracted text and page content preview |
| `POST` | `/api/documents/search` | Keyword search; JSON `{"query":"...","limit":10}` |
| `GET` / `PUT` | `/api/settings/profile` | Read or update the structured owner profile |
| `POST` | `/api/research` | Web search and bounded source extraction; sends the submitted query to Tavily |
| `GET` | `/api/projects` | List explicitly authorized project folders |
| `POST` | `/api/projects/{id}/search` | Read-only bounded search within one authorized folder |
| `GET` | `/api/agents` | Registered agent and tool capabilities |
| `GET` / `POST` | `/api/tasks` | List or create persistent personal task records |
| `PUT` | `/api/tasks/{id}` | Edit task title, description, deadline, and reminder preference |
| `POST` | `/api/tasks/{id}/status` | Update a task to pending, in progress, or blocked |
| `POST` | `/api/tasks/{id}/complete` | Mark complete and cancel pending reminders |
| `DELETE` | `/api/tasks/{id}` | Remove from active and completed task views |
| `GET` / `PUT` | `/api/settings/reminders` | Read delivery status or configure recipient address |
| `POST` | `/api/settings/reminders/test` | Send a test message using server-side SMTP settings |
| `GET` | `/api/memories` | List user-approved memories |
| `GET` / `POST` | `/api/memory-proposals` | Inspect or propose long-term memory |
| `POST` | `/api/memory-proposals/{id}/review` | Approve or reject a memory proposal |
| `DELETE` | `/api/memories/{id}` | Remove an approved memory |
| `GET` | `/api/action-approvals` | List non-executable pending action proposals |
| `GET` | `/api/agent-executions` | List execution metadata without prompt content |

Interactive API documentation is available at `http://127.0.0.1:8000/docs` while the backend is running.

## Remote control-plane foundation

Owner sign-in applies only to **Remote Access**, not the local dashboard. Its control plane uses an expiring HS256 JWT in an HttpOnly, SameSite=Strict cookie, a database-backed revocation record, and CSRF validation for state-changing requests. The password hash uses PBKDF2-HMAC-SHA256 with 600,000 iterations; older local hashes are upgraded on successful login. The owner bootstrap is one-time and there is no public registration. The **Remote Access** page additionally supports one-time ten-minute device enrollment codes, revocable hashed device credentials, and short-lived jobs limited to `health.check` and `agent.status`. The Windows agent initiates outbound HTTPS polling and stores its credential using Windows Credential Manager. It does not expose a local listener, read files, execute shell commands, or perform consequential actions.

For local validation and the required environment variables, owner bootstrap, pairing, job verification, and the precise cloud-deployment boundary, see [Remote control-plane setup and limitations](docs/REMOTE-CONTROL.md).

## Local multi-agent system

The existing `/api/chat` endpoint now enters a deterministic Manager. It invokes at most one specialist and uses a per-request two-step budget (one registered tool call and, for document answers, one selected-model call) with a 95-second model timeout and no autonomous retries. The specialists are Document Intelligence, Developer/Project, Automation/Tasks, and Approved Memory. See [Agent system architecture and permissions](docs/AGENT_SYSTEM.md) for routes, data boundaries, persistence, setup, and limitations.

## Privacy and limitations

- Only supported file types are accepted. Uploads receive generated storage names and are stored inside the application documents directory; the API does not browse arbitrary local paths.
- OCR depends on a locally installed and runtime-verified Tesseract executable; without it scanned PDFs and images are reported as retry-required and can be recovered after installation without re-uploading.
- Search is local lexical BM25-style retrieval with document metadata, field-label boosts and Indian document aliases; it is not embedding/vector search.
- The model and retrieved local passages are treated as untrusted input. Source references are returned from retrieved passages; the model can still make mistakes.
- Cloud requests send the current user message and eligible conversation history to the chosen provider. Retrieved document excerpts are excluded by default. If document context is opted in, the dashboard previews the exact matching excerpts and requires a separate confirmation before sending them with that request; prior source-bearing exchanges are filtered from cloud history.
- Approved memories are proposed from clear user-authored preferences, project details, technical decisions, or explicit requests, and saved only after explicit approval in the same chat. Users can continue to manage existing memories through the local persistence APIs. Relevant approved memories may be included in local Ollama context; ordinary cloud chat does not receive memory context. Private project excerpts are sent to a cloud model only after the existing per-request project-context consent and are excluded from later cloud chat history.
- Local chat, document, memory, task, research, and project APIs do not require the Remote Access owner login. Keep them bound to loopback. `CONTROL_PLANE_ONLY=true` hides these APIs.
- Profile, extraction metadata, task deadlines/completion, reminder jobs, and reminder recipient are added through idempotent SQLite schema migrations. SMTP secrets remain in backend environment configuration, not SQLite.
- Project inspection is read-only and constrained to configured roots, filtered file types, file count, and output size. Research queries are sent to the configured Tavily provider.
- Provider status is configuration-only for cloud providers. Credentials are not tested until a chat is sent, and provider terms, retention, and privacy policies apply to data sent to those providers.
- Health/status endpoints remain public; authentication endpoints expose only the CSRF bootstrap token and sign-in/out behavior. Private API access requires the signed cookie and mutating requests additionally require a session-bound CSRF token. `CONTROL_PLANE_ONLY=true` makes the backend return 404 for local chat/document APIs; it does not turn the whole application into a secure production service.
- The control plane currently uses single-host SQLite, has no PostgreSQL adapter or deployment-grade account recovery, and has only a minimal read-only agent protocol. Do not publicly deploy it or use it for sensitive operations. A production rollout still requires a reviewed deployment architecture, PostgreSQL migrations, operational controls, and end-to-end testing.

## Troubleshooting

- **Python or venv not found:** run `py -0p` to list installed interpreters; use a Python 3.11+ interpreter. Python 3.14 is supported by the selected dependency set, subject to pip package availability.
- **Dependency install fails:** upgrade pip in the active virtual environment, then rerun the `pip install -r requirements.txt` command. Check network access and the exact missing wheel reported.
- **Frontend command not found:** confirm Node.js/npm with `node --version` and `npm --version`, then run `npm install` in `frontend`.
- **Port 8000 or 3000 is occupied:** stop the process using that port or configure another backend port and update `NEXT_PUBLIC_API_BASE_URL` and `FRONTEND_ORIGIN` to match before starting both services.
- **Dashboard says backend is offline / CORS error:** start FastAPI on `127.0.0.1:8000`; ensure `frontend/.env.local` uses `http://127.0.0.1:8000` and `backend/.env` sets `FRONTEND_ORIGIN=http://localhost:3000`; restart both services after changing configuration.
- **Ollama unavailable:** start Ollama and confirm `Invoke-RestMethod http://127.0.0.1:11434/api/tags` works. Ollama must remain on localhost.
- **Model missing or download fails:** run `ollama pull qwen2.5:3b`; confirm it appears in `ollama list`. Large model downloads require network access, disk space, and time.
- **Scanned PDF/image extraction fails:** run `scripts\setup-document-ocr-windows.ps1`, then `scripts\diagnose-document-pipeline.ps1`, restart the backend (restarting the UI does not change its environment), and use **Retry processing** on the document. Do not delete and re-upload it.
- **Office document upload fails:** review the stored processing error; legacy DOC needs Antiword, while DOCX/XLSX/PPTX must be valid Open XML files.
- **Project specialist says a folder is unavailable:** verify `AUTHORIZED_PROJECT_DIRS` contains the root path (separate Windows paths with semicolons) or use the default repository root. The requested project ID must match an authorized workspace configured by the local owner.
- **A reminder is not delivered:** verify SMTP and recipient status in Settings and keep the backend running. Local reminders are not guaranteed while the computer is asleep/offline.

## Project documents

See [Personal Knowledge Engine](docs/KNOWLEDGE-ENGINE.md) for document ingestion, chunking, retrieval, consent, memory operations, and limitations; also see [AI provider configuration and privacy details](docs/AI_PROVIDERS.md). The `docs/` folder also contains the supplied handoff guide and plain-language guide. The user agreement is a project draft, not a legally reviewed contract.
