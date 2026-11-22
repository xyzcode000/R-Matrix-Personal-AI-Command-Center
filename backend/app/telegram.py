"""Telegram Bot API integration for the R Matrix Personal AI Command Center.

Design notes:

* Only the official Telegram Bot API is used, with long polling for this local instance.
  A bot can only receive messages that were actually sent or forwarded to it; it cannot
  read the owner's personal Telegram account history.
* The bot token is read from ``TELEGRAM_BOT_TOKEN`` and is never returned by an API route,
  logged, or included in an error message.
* One polling worker at a time is enforced with a persisted lease in SQLite, so a second
  backend process (or a uvicorn reload) does not poll with the same token.
* Updates are processed incrementally and ``last_update_id`` is persisted only after an
  update was handled, while ``(chat_id, message_id, direction)`` uniqueness prevents a
  replayed update from being processed twice.
* Before pairing, the bot sends no application data at all. Pairing requires a short-lived,
  single-use code that the signed-in owner creates in the dashboard.
"""

import asyncio
import hashlib
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from . import agents, database, intelligence, providers, time_context
from .config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000
MAX_REPLY_ATTEMPTS = 3
UNAUTHORIZED_REPLY_WINDOW_MINUTES = 10
UNAUTHORIZED_REPLY_LIMIT = 3
PAIRING_ATTEMPT_WINDOW_MINUTES = 10
PAIRING_ATTEMPT_LIMIT = 8
POLL_LEASE_SECONDS = 90
COMMANDS = ("start", "today", "tasks", "deadlines", "projects", "summary", "help")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PAIRING_HASH_DOMAIN = "r-matrix-telegram-pairing:v1:"

HELP_TEXT = (
    "R Matrix bot commands\n"
    "/today — overdue, due today and blocked work\n"
    "/tasks — open work by lifecycle state\n"
    "/deadlines — overdue and the next 14 days\n"
    "/projects — project status and progress\n"
    "/summary — priorities, risks and pending confirmations\n"
    "/help — this message\n\n"
    "Any other text is answered by the configured AI model, reusing the existing Manager "
    "agent. Only messages delivered to this bot (including ones you forward to it) are "
    "processed; a bot cannot read your personal Telegram account history."
)

DISCONNECT_NOTICE = "R Matrix has been disconnected from this chat. Pair again from the dashboard to continue."


class TelegramError(Exception):
    """A Telegram API failure with a message safe to store or show."""


class TelegramNotConfigured(TelegramError):
    pass


class TelegramUnavailable(TelegramError):
    pass


class TelegramRateLimited(TelegramError):
    def __init__(self, retry_after: int):
        super().__init__(f"Telegram asked to retry after {retry_after} seconds.")
        self.retry_after = max(1, int(retry_after))


def _safe_error(value: Any) -> str:
    text = " ".join(str(value or "").split())[:300]
    token = settings.telegram_bot_token
    if token and token in text:
        text = text.replace(token, "[redacted]")
    return text or "Telegram returned an error."


def _api_url(method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/{method}"


async def _api_call(
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    read_timeout: float = 15.0,
) -> Any:
    """Calls one Bot API method. Never raises with the token or a full request body."""
    if not settings.telegram_bot_token:
        raise TelegramNotConfigured("Telegram bot token is not configured.")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=8.0)
        ) as client:
            response = await client.post(_api_url(method), json=payload or {})
    except httpx.TimeoutException as exc:
        raise TelegramUnavailable("Telegram did not answer before the timeout.") from exc
    except httpx.HTTPError as exc:
        raise TelegramUnavailable("Could not reach the Telegram Bot API.") from exc

    if response.status_code == 429:
        retry_after = 5
        try:
            retry_after = int(
                (response.json().get("parameters") or {}).get("retry_after", 5)
            )
        except (ValueError, TypeError, AttributeError):
            pass
        raise TelegramRateLimited(retry_after)
    if response.is_error:
        description = ""
        try:
            description = (response.json() or {}).get("description", "")
        except ValueError:
            description = ""
        raise TelegramError(
            _safe_error(description) if description else f"Telegram returned HTTP {response.status_code}."
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise TelegramError("Telegram returned an invalid response.") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        raise TelegramError(_safe_error(body.get("description") if isinstance(body, dict) else None))
    return body.get("result")


async def get_me() -> dict[str, Any]:
    result = await _api_call("getMe")
    return result if isinstance(result, dict) else {}


async def get_updates(offset: int) -> list[dict[str, Any]]:
    result = await _api_call(
        "getUpdates",
        {
            "offset": int(offset),
            "timeout": settings.telegram_poll_timeout,
            "limit": 20,
            "allowed_updates": ["message"],
        },
        read_timeout=settings.telegram_poll_timeout + 15,
    )
    return [item for item in result or [] if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def _canonical_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _code_hash(code: str) -> str:
    return hashlib.sha256(f"{_PAIRING_HASH_DOMAIN}{_canonical_code(code)}".encode("utf-8")).hexdigest()


def create_pairing_code(connection: sqlite3.Connection, owner_username: str) -> dict[str, Any]:
    """Creates a single-use code. The plaintext code is returned once and never stored."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.telegram_pairing_ttl_minutes)
    record = database.create_telegram_pairing_code(
        connection,
        code_hash=_code_hash(raw),
        owner_username=owner_username,
        expires_at=expires.isoformat(),
    )
    return {
        "code": f"{raw[:4]}-{raw[4:]}",
        "expires_at": record["expires_at"],
        "ttl_minutes": settings.telegram_pairing_ttl_minutes,
        "bot_username": database.get_telegram_state(connection)["bot_username"],
        "instructions": (
            "Open Telegram, start a private chat with your bot and send: /start "
            f"{raw[:4]}-{raw[4:]}"
        ),
    }


def _pair_chat(
    connection: sqlite3.Connection,
    code: str,
    *,
    chat_id: str,
    user_id: str,
    username: str | None,
) -> str:
    if len(_canonical_code(code)) < 6 or len(_canonical_code(code)) > 24:
        return "That pairing code is not valid. Create a new code in Settings → Telegram."
    record = database.find_telegram_pairing_code(connection, _code_hash(code))
    if record is None:
        return "That pairing code is not valid. Create a new code in Settings → Telegram."
    if record["used_at"]:
        return "That pairing code was already used. Create a new code in Settings → Telegram."
    if database.parse_timestamp(record["expires_at"]) <= datetime.now(timezone.utc):
        return "That pairing code expired. Create a new code in Settings → Telegram."
    if not database.mark_telegram_pairing_code_used(connection, record["id"]):
        return "That pairing code was already used. Create a new code in Settings → Telegram."

    database.clear_telegram_pairing(connection)
    database.invalidate_telegram_pairing_codes(connection)
    database.update_telegram_state(
        connection,
        {
            "paired_chat_id": str(chat_id),
            "paired_user_id": str(user_id),
            "paired_username": username or None,
            "paired_at": database.utc_now(),
            "owner_username": record["owner_username"],
            "conversation_id": None,
            "last_error": None,
            "last_error_at": None,
        },
    )
    return (
        "R Matrix is now paired with this chat. Send /help to see the commands, or ask a "
        "question in plain language."
    )


# ---------------------------------------------------------------------------
# Command responses (existing project and task services)
# ---------------------------------------------------------------------------


def _project_names(connection: sqlite3.Connection) -> dict[str, str]:
    return {project["id"]: project["name"] for project in database.list_projects(connection)}


def _due_text(task: dict[str, Any]) -> str:
    due_at = database.parse_timestamp(task.get("due_at"))
    if due_at is None:
        return ""
    local = due_at.astimezone(ZoneInfo(time_context.DEFAULT_TIMEZONE))
    if (task.get("due_precision") or "") == "date":
        return local.strftime("%d %b %Y")
    return local.strftime("%d %b %Y %H:%M")


def _task_line(task: dict[str, Any], projects: dict[str, str]) -> str:
    parts = [task["title"]]
    due = _due_text(task)
    if due:
        parts.append(f"due {due}")
    project = projects.get(task["project_id"] or "")
    if project:
        parts.append(project)
    if task.get("lifecycle") in {"detected", "pending_confirmation"}:
        parts.append("unconfirmed")
    if task["priority"] in {"high", "urgent"}:
        parts.append(task["priority"])
    return "- " + " · ".join(parts)


def _section(title: str, tasks: list[dict[str, Any]], projects: dict[str, str], limit: int = 10) -> str:
    if not tasks:
        return f"{title}: none"
    lines = [f"{title} ({len(tasks)}):"]
    lines.extend(_task_line(task, projects) for task in tasks[:limit])
    if len(tasks) > limit:
        lines.append(f"- …and {len(tasks) - limit} more")
    return "\n".join(lines)


def _open_tasks(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [task for task in database.list_tasks(connection) if task["status"] != "done"]


def command_replies(connection: sqlite3.Connection, command: str) -> list[str]:
    projects = _project_names(connection)
    tasks = _open_tasks(connection)
    command = command.casefold()

    if command in {"help", "start"}:
        return [HELP_TEXT]

    if command == "today":
        local_now = time_context.current_time()
        today = local_now.date()
        due_today = [
            task
            for task in tasks
            if (parsed := database.parse_timestamp(task["due_at"])) is not None
            and parsed.astimezone(ZoneInfo(time_context.DEFAULT_TIMEZONE)).date() == today
        ]
        overdue = [task for task in tasks if task["lifecycle"] == "overdue"]
        blocked = [task for task in tasks if task["status"] == "blocked"]
        text = (
            f"Today — {local_now.strftime('%A, %d %B %Y')} "
            f"({time_context.DEFAULT_TIMEZONE})\n\n"
            + _section("Overdue", overdue, projects)
            + "\n\n"
            + _section("Due today", due_today, projects)
            + "\n\n"
            + _section("Blocked", blocked, projects)
        )
        if not (overdue or due_today or blocked):
            text += f"\n\nNothing is due or blocked today. {len(tasks)} open task(s) in total."
        return [text]

    if command == "tasks":
        groups = (
            ("Blocked", [task for task in tasks if task["lifecycle"] == "blocked"]),
            ("In progress", [task for task in tasks if task["lifecycle"] == "in_progress"]),
            ("Overdue", [task for task in tasks if task["lifecycle"] == "overdue"]),
            ("Upcoming", [task for task in tasks if task["lifecycle"] == "upcoming"]),
            (
                "Needs confirmation",
                [
                    task
                    for task in tasks
                    if task["lifecycle"] in {"detected", "pending_confirmation"}
                ],
            ),
        )
        body = "\n\n".join(_section(name, items, projects) for name, items in groups if items)
        return [body or "There is no open work in Task History right now."]

    if command == "deadlines":
        overdue = [task for task in tasks if task["lifecycle"] == "overdue"]
        local_now = time_context.current_time()
        horizon = local_now + timedelta(days=14)
        upcoming = []
        for task in tasks:
            due = database.parse_timestamp(task["due_at"])
            if due is not None and local_now <= due <= horizon:
                upcoming.append(task)
        upcoming.sort(key=lambda item: item["due_at"] or "")
        return [
            _section("Overdue", overdue, projects)
            + "\n\n"
            + _section("Next 14 days", upcoming, projects, limit=15)
        ]

    if command == "projects":
        records = database.list_projects(connection)
        if not records:
            return ["No projects are recorded yet."]
        tasks_by_project: dict[str, list[dict[str, Any]]] = {}
        for task in database.list_tasks(connection):
            tasks_by_project.setdefault(task["project_id"] or "", []).append(task)
        lines = ["Projects:"]
        for project in records:
            scoped = tasks_by_project.get(project["id"], [])
            open_count = len([task for task in scoped if task["status"] != "done"])
            lines.append(
                f"- {project['name']} · {project['status']} · {project['progress']}% · "
                f"{open_count} open task(s)"
            )
        return ["\n".join(lines[:40])]

    if command == "summary":
        summary = intelligence.build_summary(connection)
        projects_text = "\n".join(
            f"- {item['name']} · {item['status']} · {item['progress']}%"
            for item in summary["projects"][:10]
        ) or "No active projects."
        priorities = "\n".join(
            f"- {item['title']} ({item['lifecycle']})"
            for item in summary["current_priorities"][:8]
        ) or "- none"
        deadlines = "\n".join(
            f"- {item['title']} · {_due_text(item)}"
            for item in summary["upcoming_deadlines"][:8]
        ) or "- none in the next 14 days"
        risks = "\n".join(
            f"- [{item['severity']}] {item['summary']}" for item in summary["risks"][:8]
        ) or "- none detected"
        pending = summary["pending_confirmations"]
        return [
            "Intelligence summary\n\n"
            f"Projects:\n{projects_text}\n\n"
            f"Priorities:\n{priorities}\n\n"
            f"Upcoming deadlines:\n{deadlines}\n\n"
            f"Risks:\n{risks}\n\n"
            f"Pending confirmation: {pending['tasks']} task(s), {pending['proposals']} proposal(s)."
        ]

    return [HELP_TEXT]


# ---------------------------------------------------------------------------
# Update processing
# ---------------------------------------------------------------------------


def _parse_command(text: str) -> tuple[str, str]:
    match = re.match(r"^/([A-Za-z0-9_]+)(?:@\w+)?(?:\s+(.*))?$", (text or "").strip(), re.DOTALL)
    if not match:
        return "", ""
    return match.group(1).casefold(), (match.group(2) or "").strip()


def _telegram_sent_at(message: dict[str, Any]) -> str | None:
    raw = message.get("date")
    if not isinstance(raw, (int, float)):
        return None
    return datetime.fromtimestamp(raw, timezone.utc).isoformat()


def _is_forwarded(message: dict[str, Any]) -> bool:
    return bool(
        message.get("forward_origin")
        or message.get("forward_from")
        or message.get("forward_from_chat")
        or message.get("forward_date")
        or message.get("forward_sender_name")
    )


def _unauthorized_rate_limited(connection: sqlite3.Connection, chat_id: str) -> bool:
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=UNAUTHORIZED_REPLY_WINDOW_MINUTES)
    ).isoformat()
    return database.count_telegram_outbound_since(connection, chat_id, since) >= UNAUTHORIZED_REPLY_LIMIT


def _pairing_attempts_exceeded(connection: sqlite3.Connection, chat_id: str) -> bool:
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=PAIRING_ATTEMPT_WINDOW_MINUTES)
    ).isoformat()
    return database.count_telegram_outbound_since(connection, chat_id, since) >= PAIRING_ATTEMPT_LIMIT


async def _telegram_conversation(connection: sqlite3.Connection, state: dict[str, Any]) -> str:
    conversation_id = state.get("conversation_id")
    if conversation_id:
        exists = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if exists is not None:
            return conversation_id
    conversation_id = database.create_conversation(connection, "Telegram")
    database.update_telegram_state(connection, {"conversation_id": conversation_id})
    return conversation_id


def _model_selection() -> tuple[str, str]:
    provider_id = settings.model_provider
    provider = providers.provider_registry().get(provider_id)
    if provider is None:
        return provider_id, ""
    configured, _ = provider.configured()
    if not configured:
        return provider_id, ""
    return provider.provider_id, provider.configured_model()


async def _ai_reply(connection: sqlite3.Connection, conversation_id: str, text: str) -> str:
    provider_id, model = _model_selection()
    if not model:
        return (
            "No AI model is configured for natural-language replies yet. Set a provider and "
            "model in backend/.env, then try again. /help lists the available commands."
        )
    history = database.get_messages(connection, conversation_id)[-10:]
    user_message = database.add_message(connection, conversation_id, "user", text)
    try:
        result = await agents.run_manager(
            message=text,
            history=history,
            provider_id=provider_id,
            model=model,
            allow_cloud_context=False,
            connection=connection,
            conversation_id=conversation_id,
            project_roots=settings.authorized_project_dirs,
            current_context=agents.current_server_context(text=text),
        )
    except (providers.ProviderError, TimeoutError, agents.ToolRegistryError):
        logger.info("Telegram AI reply failed (%s).", provider_id)
        return (
            "The AI model could not answer just now. Check that Ollama (or the selected "
            "provider) is running, or use /tasks, /today, /projects or /summary."
        )
    database.add_message(
        connection,
        conversation_id,
        "assistant",
        result.content,
        result.sources,
        provider=result.provider if result.execution.get("model_used") else None,
        model=result.model if result.execution.get("model_used") else None,
        execution=result.execution,
    )
    try:
        intelligence.analyze_message(
            connection,
            conversation_id=conversation_id,
            message=user_message,
            reference_time=time_context.current_time(),
        )
    except sqlite3.Error:
        logger.warning("Telegram message could not be analyzed by the intelligence engine.")
    return result.content


async def process_update(connection: sqlite3.Connection, update: dict[str, Any]) -> dict[str, Any] | None:
    """Handles one update and returns the replies to deliver, or None to ignore it."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private":
        # Group chats are never served, so no owner data can leak into a shared room.
        return None
    chat_id = str(chat.get("id"))
    user_id = str(sender.get("id"))
    if not chat_id or not user_id:
        return None
    if chat_id != user_id:
        return None

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        text = message.get("caption") if isinstance(message.get("caption"), str) else ""
    kind = "text" if message.get("text") else "caption" if text else "unsupported"
    telegram_message_id = str(message.get("message_id"))
    if database.telegram_message_seen(connection, chat_id, telegram_message_id, "inbound"):
        return None
    database.record_telegram_message(
        connection,
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        direction="inbound",
        kind=kind,
        status="received",
        update_id=update.get("update_id"),
        is_forwarded=_is_forwarded(message),
        text_length=len(text or ""),
        sent_at=_telegram_sent_at(message),
    )
    state = database.get_telegram_state(connection)
    database.update_telegram_state(
        connection, {"messages_received": int(state["messages_received"]) + 1}
    )

    authorized = bool(state["paired_chat_id"]) and state["paired_chat_id"] == chat_id and (
        state["paired_user_id"] in {None, user_id}
    )
    command, arguments = _parse_command(text or "")

    if authorized:
        if command == "start" and arguments and not _pairing_attempts_exceeded(connection, chat_id):
            # Re-pairing is allowed so the owner can move the bot to another chat; the code
            # still has to be a fresh, single-use code created in the dashboard.
            reply = _pair_chat(
                connection,
                arguments,
                chat_id=chat_id,
                user_id=user_id,
                username=sender.get("username"),
            )
            return {"chat_id": chat_id, "replies": [reply], "kind": "pairing", "authorized": False}
        if command in {"start", "help"}:
            return {"chat_id": chat_id, "replies": [HELP_TEXT], "kind": "command", "authorized": True}
        if command in COMMANDS:
            return {
                "chat_id": chat_id,
                "replies": command_replies(connection, command),
                "kind": "command",
                "authorized": True,
            }
        if command:
            return {
                "chat_id": chat_id,
                "replies": ["That command is not available.\n\n" + HELP_TEXT],
                "kind": "command",
                "authorized": True,
            }
        if not text.strip():
            return {
                "chat_id": chat_id,
                "replies": [HELP_TEXT],
                "kind": "empty",
                "authorized": True,
            }
        conversation_id = await _telegram_conversation(connection, state)
        reply = await _ai_reply(connection, conversation_id, text.strip())
        return {
            "chat_id": chat_id,
            "replies": [reply],
            "kind": "ai",
            "authorized": True,
            "conversation_id": conversation_id,
        }

    # Unpaired or foreign chats never receive application data.
    if _unauthorized_rate_limited(connection, chat_id):
        return {"chat_id": chat_id, "replies": [], "kind": "unauthorized", "authorized": False}
    if command == "start" and arguments and not _pairing_attempts_exceeded(connection, chat_id):
        reply = _pair_chat(
            connection,
            arguments,
            chat_id=chat_id,
            user_id=user_id,
            username=sender.get("username"),
        )
        return {"chat_id": chat_id, "replies": [reply], "kind": "pairing", "authorized": False}
    return {
        "chat_id": chat_id,
        "replies": [
            "This chat is not paired with R Matrix. Open the dashboard → Settings → Telegram, "
            "create a pairing code, then send: /start YOUR-CODE"
        ],
        "kind": "unauthorized",
        "authorized": False,
    }


# ---------------------------------------------------------------------------
# Outgoing messages
# ---------------------------------------------------------------------------


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    remaining = (text or "").strip()
    chunks: list[str] = []
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_chunk(chat_id: str, text: str) -> dict[str, Any] | None:
    delay = 1.0
    for attempt in range(MAX_REPLY_ATTEMPTS):
        try:
            return await _api_call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
        except TelegramRateLimited as exc:
            await asyncio.sleep(min(exc.retry_after, 30))
        except TelegramUnavailable as exc:
            if attempt == MAX_REPLY_ATTEMPTS - 1:
                logger.warning("Telegram delivery failed: %s", exc)
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)
        except TelegramError as exc:
            logger.warning("Telegram rejected a message: %s", exc)
            return None
    return None


async def deliver(
    connection: sqlite3.Connection,
    chat_id: str,
    replies: list[str],
    update_id: int | None = None,
) -> int:
    """Sends replies in safe chunks and stores delivery metadata. Returns the sent count."""
    sent = 0
    for reply in replies:
        for chunk in chunk_text(reply):
            result = await _send_chunk(chat_id, chunk)
            delivered = bool(result)
            telegram_message_id = str(result.get("message_id")) if delivered else f"failed-{uuid4().hex}"
            database.record_telegram_message(
                connection,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                direction="outbound",
                kind="reply",
                status="sent" if delivered else "failed",
                update_id=update_id,
                text_length=len(chunk),
            )
            if delivered:
                sent += 1
                state = database.get_telegram_state(connection)
                database.update_telegram_state(
                    connection, {"messages_sent": int(state["messages_sent"]) + 1}
                )
    return sent


# ---------------------------------------------------------------------------
# Long polling worker
# ---------------------------------------------------------------------------


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.05, seconds))
    except TimeoutError:
        return False
    return True


async def polling_worker(stop: asyncio.Event) -> None:
    """Polls Telegram with a persisted lease so exactly one local worker consumes updates."""
    if not settings.telegram_enabled or not settings.telegram_bot_token:
        return
    worker_id = uuid4().hex
    with database.session() as connection:
        if not database.claim_telegram_polling_lease(connection, worker_id):
            logger.info("Another Telegram polling worker holds the lease; this one exits.")
            return
    backoff = 1.0
    try:
        try:
            me = await get_me()
            with database.session() as connection:
                database.update_telegram_state(
                    connection,
                    {
                        "bot_username": me.get("username"),
                        "last_error": None,
                        "last_error_at": None,
                    },
                )
        except TelegramError as exc:
            with database.session() as connection:
                database.note_telegram_error(connection, str(exc))

        while not stop.is_set():
            with database.session() as connection:
                if not database.claim_telegram_polling_lease(connection, worker_id):
                    return
                offset = int(database.get_telegram_state(connection)["last_update_id"])
            try:
                updates = await get_updates(offset)
            except TelegramRateLimited as exc:
                if await _wait(stop, min(exc.retry_after, 60)):
                    return
                continue
            except TelegramError as exc:
                with database.session() as connection:
                    database.note_telegram_error(connection, str(exc))
                if await _wait(stop, backoff):
                    return
                backoff = min(backoff * 2, 60)
                continue
            backoff = 1.0
            with database.session() as connection:
                database.update_telegram_state(
                    connection, {"last_poll_at": database.utc_now()}
                )
            for update in updates:
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    continue
                with database.session() as connection:
                    try:
                        result = await process_update(connection, update)
                    except sqlite3.Error:
                        logger.warning("Telegram update %s could not be stored.", update_id)
                        result = None
                    except Exception as exc:  # noqa: BLE001 - one bad update must not stop polling
                        logger.warning("Telegram update %s failed (%s).", update_id, type(exc).__name__)
                        result = None
                    if result and result.get("replies"):
                        await deliver(
                            connection,
                            result["chat_id"],
                            result["replies"],
                            update_id=update_id,
                        )
                    database.record_telegram_update(connection, update_id, (result or {}).get("kind", "ignored"))
                    database.set_telegram_offset(connection, update_id + 1)
    finally:
        with database.session() as connection:
            database.release_telegram_polling_lease(connection, worker_id)


# ---------------------------------------------------------------------------
# Settings status and disconnect
# ---------------------------------------------------------------------------


def connection_status(connection: sqlite3.Connection) -> dict[str, Any]:
    state = database.get_telegram_state(connection)
    heartbeat = database.parse_timestamp(state["poll_heartbeat_at"])
    now = datetime.now(timezone.utc)
    chat_id = state["paired_chat_id"] or ""
    return {
        "enabled": settings.telegram_enabled,
        "configured": bool(settings.telegram_bot_token),
        "bot_username": state["bot_username"],
        "paired": bool(chat_id),
        "pairing": {
            "telegram_username": state["paired_username"],
            "chat_id_suffix": chat_id[-4:] if chat_id else None,
            "paired_at": state["paired_at"],
            "owner_account": state["owner_username"],
            "active_codes": database.count_active_telegram_pairing_codes(connection),
            "code_ttl_minutes": settings.telegram_pairing_ttl_minutes,
            "conversation_id": state["conversation_id"],
        },
        "polling": {
            "last_update_id": state["last_update_id"],
            "last_poll_at": state["last_poll_at"],
            "worker_active": bool(
                heartbeat is not None and (now - heartbeat).total_seconds() < POLL_LEASE_SECONDS
            ),
            "last_error": state["last_error"],
            "last_error_at": state["last_error_at"],
        },
        "activity": {
            "messages_received": state["messages_received"],
            "messages_sent": state["messages_sent"],
        },
        "limits": (
            "A Telegram bot only receives messages sent or forwarded to it directly; it "
            "cannot read the owner's Telegram account history."
        ),
    }


async def disconnect(connection: sqlite3.Connection, *, notify: bool = True) -> dict[str, Any]:
    """Unpairs the chat. The pairing and offset state are reset; no user data is deleted."""
    state = database.get_telegram_state(connection)
    chat_id = state["paired_chat_id"]
    database.clear_telegram_pairing(connection)
    database.invalidate_telegram_pairing_codes(connection)
    if notify and chat_id and settings.telegram_bot_token:
        try:
            await deliver(connection, chat_id, [DISCONNECT_NOTICE])
        except TelegramError:
            logger.info("Telegram disconnect notice could not be delivered.")
    return connection_status(connection)


# ---------------------------------------------------------------------------
# Two-factor sign-in codes
# ---------------------------------------------------------------------------

OTP_MESSAGE_TEMPLATE = (
    "R MATRIX AI — sign-in verification\n\n"
    "Your verification code is {code}\n\n"
    "It expires in {minutes} minutes and can be used once. "
    "If you did not try to sign in, do not share this code."
)


def paired_chat_id(connection: sqlite3.Connection) -> str | None:
    """The chat that received a valid pairing code, if any."""
    state = database.get_telegram_state(connection)
    chat_id = state["paired_chat_id"]
    return str(chat_id) if chat_id else None


def otp_delivery_available(connection: sqlite3.Connection) -> bool:
    """True only when a code can actually reach the paired owner chat."""
    if not settings.auth_telegram_otp_enabled or not settings.telegram_enabled:
        return False
    if not settings.telegram_bot_token:
        return False
    return paired_chat_id(connection) is not None


async def send_login_code(
    connection: sqlite3.Connection,
    chat_id: str,
    code: str,
    ttl_minutes: int,
) -> bool:
    """Delivers a sign-in code privately through the existing bot.

    The code is never logged, stored in plaintext or returned to the browser.
    Delivery reuses the existing retry/backoff send path.
    """
    text = OTP_MESSAGE_TEMPLATE.format(code=code, minutes=ttl_minutes)
    result = await _send_chunk(chat_id, text)
    delivered = bool(result)
    database.record_telegram_message(
        connection,
        chat_id=chat_id,
        telegram_message_id=str(result.get("message_id")) if delivered else f"failed-{uuid4().hex}",
        direction="outbound",
        kind="otp",
        status="sent" if delivered else "failed",
        text_length=len(text),
    )
    if delivered:
        state = database.get_telegram_state(connection)
        database.update_telegram_state(
            connection, {"messages_sent": int(state["messages_sent"]) + 1}
        )
    return delivered
