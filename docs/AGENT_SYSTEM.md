# Local agent system

The current multi-agent workflow is part of the existing FastAPI `/api/chat` route and the existing dashboard. It does not create another chatbot, an autonomous agent framework, or a remote dependency.

## Routing and execution limits

The Central AI Manager uses deterministic intent routing. It selects at most one path per request:

| Intent | Specialist | Permitted operation |
|---|---|---|
| Explicit indexed-document question | Document Intelligence | Search the existing local document index; answer from passages and return their filenames/page numbers |
| Explicit project/development analysis | Developer and Project | Inspect matching text inside configured project roots, read-only |
| Task create/list/status change | Automation and Tasks | Create/list tasks or move them among pending, in-progress, and blocked |
| Clear memory request or conservative chat candidate | Approved Memory | Record a proposal that remains pending until explicit user approval |
| Request for a potentially consequential action | Automation and Tasks | Record a pending, non-executable proposal |
| Other chat | Central AI Manager | Call the selected model through the existing provider router |

Routing does not ask a model to choose arbitrary tools. A request can invoke at most one registered tool and, for document answers only, one selected-model completion. This is enforced by a two-step budget. Model completions have a 95-second Manager timeout; retries and agent-to-agent loops are not implemented. Provider failures remain explicit and are never replaced with fake success.

## Typed tools and permissions

`backend/app/agents.py` defines Pydantic input and output contracts for each tool. The central registry validates both sides, rejects unknown tools and invalid parameters, checks a permission grant for every call, and exposes only public schemas through `GET /api/agents`.

| Tool | Permission | Risk | Behavior |
|---|---|---|---|
| `documents.search` | `documents.read` | Low | Uses the existing local search service |
| `projects.inspect` | `projects.read` | Low | Reads bounded matching text from one configured root |
| `tasks.create`, `tasks.list`, `tasks.update` | `tasks.write`, `tasks.read` | Low | Persists tasks; update never accepts `done` |
| `memory.propose` | `memory.propose` | Low | Saves a pending memory proposal only |
| `approvals.propose` | `approvals.propose` | Medium | Saves a review item only; it cannot approve or execute it |

No tool executes a shell command, installs packages, modifies source files, deletes files, sends communications, or changes security settings.

## Document and project privacy

Document Intelligence reuses the existing SQLite document pages and keyword search; it does not create another index. Retrieved passages are untrusted evidence. If a cloud provider is selected, passages are not sent unless the existing per-request document-context consent is enabled. Without consent the Manager returns local source evidence and marks that no model was called. Cloud history filtering excludes prior document-backed exchanges.

`AUTHORIZED_PROJECT_DIRS` is an optional Windows semicolon-separated list of project roots. When absent, the official repository root is the only allowed root. The Manager resolves the project ID against that server-side allowlist; it never accepts an arbitrary file path from a user or model. The inspector does not follow symlinks, skips `.env` and credential files and common build/vendor folders, reads only selected text extensions, caps scanned files and output, redacts common credential patterns, and never modifies a file or invokes a process. Project content is sent to cloud providers only after the existing per-request project-context consent; project-backed assistant turns are removed from later cloud conversation history.

## Tasks and approvals

Tasks are stored in the existing application SQLite database. The Manager can create and list tasks, and update a task to pending, in-progress, or blocked. It cannot mark a task done because this release has no verified underlying-action event. A model statement or user prompt alone is not treated as proof of completion.

Potentially consequential requests create pending records with a fixed, non-executable operation type and a generic summary. The exact action inputs/executor, approval grant, and execution path do not exist. The dashboard can dismiss a proposal; it cannot trigger the proposed action. Never interpret a pending proposal as an approval.

## Approved long-term memory

Conversation history, tasks, memory proposals, approved memories, document passages, and execution metadata use separate tables/fields. Clear memory requests and selected user-authored preferences, project details, or technical decisions can create a pending proposal; ordinary chat is not saved. A new proposal is presented as a brief question in the same conversation, and only a clear affirmative reply approves it. A negative reply rejects it. Proposals are never automatically approved, duplicates are deduplicated, and sensitive or credential-shaped candidates are not proposed automatically. Approved memories remain separate from conversation history.

Relevant approved memory is retrieved for chat and added as a separate, bounded local Ollama context block, explicitly labeled as user context rather than instructions or verified project facts. Ordinary cloud-provider chat does not receive approved-memory context. Project context, including relevant memories, is shared with a cloud provider only when the existing per-request project-context consent is enabled. Conflicting memories are not automatically reconciled.

## Persistence, API, and local security

The existing `backend/app/database.py` persistence layer adds idempotent application-task, memory, approval, and execution tables and an `execution_json` message column. Existing chat and document records are preserved. Execution metadata stores selected agent, routing reason, tool names, status, step count, and source references; it does not record prompts or credentials. Chat history continues to persist user messages and assistant replies as before.

`GET /api/agents`, `/api/agent-executions`, `/api/tasks`, `/api/memories`, `/api/memory-proposals`, and `/api/action-approvals` are local application APIs. The Remote Access owner login does not authenticate these APIs; keep FastAPI bound to loopback and do not expose it to a network. Local application APIs are unavailable when `CONTROL_PLANE_ONLY=true`.

## Run and test

From `backend` with the project virtual environment active:

```powershell
python -m pytest
```

Set `AUTHORIZED_PROJECT_DIRS` in `backend\.env` only to folders you intend this read-only specialist to inspect, separated by semicolons. Restart FastAPI after changing configuration. Memory proposals raised in AI Chat are approved or rejected in that same conversation.

## Limitations

- Deterministic keyword routing can miss ambiguous phrasings. It does not use a model to classify intent.
- Project inspection is bounded text matching, not a full repository parser or security audit. It may omit relevant files and does not prove a feature is complete.
- Task completion events, advanced planning, multi-user ownership, remote agent execution, n8n, file writes, shell access, emails, and deployment are not implemented.
- Task, memory, and agent API endpoints are for the existing localhost-only single-user application and do not have user authentication or multi-user ownership checks.
- SQLite schema changes follow the existing idempotent initialization pattern. No PostgreSQL migration or multi-instance deployment path exists.
