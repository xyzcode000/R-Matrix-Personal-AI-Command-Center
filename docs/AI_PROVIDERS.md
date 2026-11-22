# AI provider configuration

The backend routes chat requests to one of four providers: local Ollama, OpenAI, Anthropic Claude, or Google Gemini. The browser never receives provider API keys. Gemini uses Google's official asynchronous `google-genai` Python SDK; OpenAI and Anthropic use fixed HTTPS endpoints through the backend `httpx` client. Automated tests mock external calls.

## Configure a provider

1. In `backend/.env`, set `AI_PROVIDER` to the desired default (`ollama`, `openai`, `anthropic`, or `gemini`). The chat page can also select any provider that is fully configured.
2. For Ollama, set `OLLAMA_BASE_URL` to a loopback URL and `OLLAMA_MODEL` to an installed model. Remote Ollama URLs are rejected.
3. For a cloud provider, set both its API key and model ID:

   | Provider | API key | Model ID |
   |---|---|---|
   | OpenAI | `OPENAI_API_KEY` | `OPENAI_MODEL` |
   | Anthropic | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` |
   | Google Gemini | `GEMINI_API_KEY` | `GEMINI_MODEL` |

4. Restart FastAPI after editing environment settings. The backend reads `AI_PROVIDER` first and supports the legacy `MODEL_PROVIDER` setting only when `AI_PROVIDER` is unset.
5. Check `GET /api/providers` for configuration status and `GET /api/health/model` for the default provider. These endpoints do not make a cloud request or verify credentials. `GET /api/models?provider=<provider>` returns installed local models for Ollama or the configured model ID for a cloud provider; it does not fetch a cloud model catalog.

Keep `backend/.env` private and untracked. Do not copy keys into `frontend/.env.local`, any `NEXT_PUBLIC_*` variable, source code, logs, prompts, tests, or documentation. The local dashboard does not have a global login gate; owner sign-in is limited to the separate Remote Access control plane. The development service remains loopback-only and must not be exposed to a public or shared network.

### Gemini setup on Windows

The backend environment file is `backend\.env`, absolute path `C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\backend\.env`. It is not present by default in a fresh checkout and is excluded by `.gitignore`. From the repository root, create it once with:

```powershell
Copy-Item .\backend\.env.example .\backend\.env
```

Edit the created file locally. Keep the key value private:

```dotenv
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
GEMINI_MODEL=gemini-2.5-flash
```

Replace the placeholder locally with your API key; never paste it into chat. The `gemini-2.5-flash` identifier is currently listed in [Google's model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash). Change `GEMINI_MODEL` if another supported model is preferred.

After saving `.env`, stop FastAPI with Ctrl+C and restart from the backend directory:

```powershell
Set-Location 'C:\Users\RAHIL\OneDrive\Desktop\CODE\projects\R matrix ai\R-Matrix-Personal-AI-Command-Center\backend'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open AI Chat, select **Google Gemini**, and confirm the model shown beside the provider selector. Send a harmless prompt and verify the returned assistant message shows `gemini · gemini-2.5-flash`. `/api/providers` and the selector indicate configuration only, not connectivity; only a successful chat response verifies the real API call. To return to local Ollama, select **Ollama**, or set `AI_PROVIDER=ollama` and restart FastAPI.

If Gemini is disabled, verify `GEMINI_API_KEY` is non-empty and `GEMINI_MODEL` is set. A 404 or invalid-model response means the configured model ID is unsupported or unavailable to the account; compare it with Google's current model catalog, update `.env`, and restart FastAPI. Authentication errors require checking the locally stored API key. Error responses do not include provider response bodies or credentials.

## What is sent

- With Ollama, chat requests go to the configured local Ollama instance. The backend restricts its Ollama URL to localhost or a loopback IP.
- With OpenAI, Anthropic, or Gemini, the current user message and eligible prior conversation turns are sent to the selected vendor. Select a cloud provider only when you are comfortable with that transfer, and follow the provider's privacy and data-retention terms.
- Matching document excerpts are not included in cloud requests by default. In AI Chat, the consent checkbox authorizes matching excerpts and source snippets for the next request only; it resets after submission. The UI identifies the cloud provider before sending.
- Source-bearing prior exchanges are excluded from cloud history, even when consenting to current document excerpts. Messages and other eligible history can still contain private information; review the visible notice before sending.
- Chat history and provider/model metadata are persisted in the local SQLite database. Provider status responses do not include credentials. Existing SQLite databases are migrated to add nullable provider and model fields.

Retrieved documents are untrusted reference data. They are passed as quoted context when included and must not override system instructions. The local workspace API does not require the Remote Access sign-in; local storage does not protect data from other users or processes on the same device.

## API adapter endpoints

The cloud adapters use HTTPS:

- OpenAI Chat Completions: `https://api.openai.com/v1/chat/completions`
- Anthropic Messages: `https://api.anthropic.com/v1/messages`
- Gemini uses Google's official `google-genai` SDK (`client.aio.models.generate_content`) and `GEMINI_MODEL` selects the model. The installed async client is closed after each request.

HTTP errors are converted to safe messages; provider response bodies and keys are not returned to the frontend. A configured model is not necessarily supported by an account or verified as reachable. Provider errors are shown when a chat request is made.

## Testing

Run backend tests from `backend`:

```powershell
python -m pytest
```

Cloud provider tests use mocked responses and need no API keys. They must not be used to claim live vendor connectivity.
