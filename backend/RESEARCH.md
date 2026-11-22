# Real-Time Web Intelligence Engine

The research capability is an independently testable backend module in
`app.research`, exposed through `/api/research` and the dashboard's Research
page. Chat orchestration reuses this service when a request explicitly asks
for web search or automatic intent detection identifies time-sensitive public
information; ordinary questions do not trigger a search.

## Architecture

- `models.py` defines typed search results, extracted sources, issue/warning
  records, and the complete research response.
- `providers.py` defines the `SearchProvider` interface and the Tavily adapter.
- `service.py` coordinates a search, optional source extraction, timestamps,
  and partial failures.
- `extractor.py` retrieves HTML only, removes common non-content elements,
  extracts readable text and basic publication metadata, and caps bytes,
  characters, time, and redirects.
- `urls.py` rejects local/reserved targets. Its aiohttp DNS resolver validates
  addresses at connection time and returns those validated addresses to the
  connector; redirects are disabled and revalidated manually.
- `formatter.py` serializes results with an explicit untrusted-data notice for
  model context, with result and excerpt bounds.

Webpage and search-provider text are untrusted evidence. Chat treats retrieved
content as reference data, not instructions, and returns source titles, URLs,
publication dates when available, retrieval time, and provider errors. Only the
sanitized current user query is sent to Tavily; conversation history, owner
profile, memories, indexed documents, and project files are not included.
Current date, weekday, time, and the `Asia/Kolkata` timezone come from the
server clock and are provided to the selected model independently of web
research. Simple date/time questions are answered directly from that same
server clock, so correctness does not depend on model compliance. The service
logs error codes and events only, not queries, source text, or credentials.

## Search provider and credentials

Tavily is the initial provider because it supplies web search results in a
structured API response with URLs, titles, snippets, and publication dates
when available. A search query is sent to Tavily when the user submits it.
Set `TAVILY_API_KEY` in the backend environment. The key is
sent only in a server-side authorization header; it is not returned in the
research response or logged. There is no live API key required for the mocked
tests.

## Environment

These settings are read by `ResearchSettings.from_env()` and can also be
constructed directly for tests or other dependency-injection contexts.

| Variable | Default | Bounds / meaning |
|---|---:|---|
| `TAVILY_API_KEY` | empty | Required to make provider requests; sent only server-side |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `10` | `0.1`–`60` seconds |
| `WEB_SOURCE_TIMEOUT_SECONDS` | `8` | `0.1`–`60` seconds |
| `WEB_SEARCH_DEFAULT_LIMIT` | `5` | Default results; between 1 and max |
| `WEB_SEARCH_MAX_LIMIT` | `10` | Maximum requested results, at most 10 |
| `WEB_SOURCE_MAX_BYTES` | `1000000` | Download limit; 1,024–5,000,000 bytes |
| `WEB_SOURCE_MAX_CHARS` | `12000` | Extracted text limit; 100–100,000 chars |
| `WEB_SOURCE_MAX_REDIRECTS` | `3` | Redirect limit; 0–5 |

These are research-module settings.

## Tests

From `backend` with dependencies installed:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests for this module can be run alone with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_research.py
```

Network calls are mocked. No real search API calls are made.

## Dashboard API

- `GET /api/research/status` returns only whether the provider is configured.
- `POST /api/research` accepts `query`, `limit`, and `extract_sources`.
- Results include links, extracted passages, errors, and warnings. The
  Research page does not automatically attach its results to a separate chat.
- The dashboard states that the query is sent to Tavily before the user searches.
- Chat provides Automatic, Always search, and Never search modes. An explicit
  request not to browse takes precedence; profile, memory, task, and document
  requests are never sent to web search.

No persistent research-history table exists yet. The page shows the current
response; it does not pretend to save research records.
