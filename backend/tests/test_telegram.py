"""Tests for the Telegram Bot API integration (no network calls are made)."""

import asyncio
import sqlite3

import pytest

from app import agents, database, telegram
from app.config import settings

CHAT_ID = 424242


def _update(
    update_id: int = 1,
    *,
    text: str | None = "/start",
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
    message_id: int | None = None,
    forwarded: bool = False,
):
    message = {
        "message_id": message_id or update_id,
        "date": 1770000000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id, "username": "owner_telegram"},
    }
    if text is not None:
        message["text"] = text
    if forwarded:
        message["forward_origin"] = {"type": "user", "sender_user": {"id": 9999}}
    return {"update_id": update_id, "message": message}


def _process(update):
    with database.session() as connection:
        return asyncio.run(telegram.process_update(connection, update))


def _pair(client, chat_id: int = CHAT_ID) -> str:
    code = client.post("/api/settings/telegram/pairing-code").json()["code"]
    result = _process(_update(1, text=f"/start {code}", chat_id=chat_id))
    assert "paired" in result["replies"][0].casefold(), result
    return code


def _project(client, name: str = "Telegram Project") -> dict:
    workspace_id = client.get("/api/projects").json()["workspaces"][0]["id"]
    response = client.post(
        "/api/projects", json={"name": name, "workspace_id": workspace_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_pairing_endpoint_requires_owner_session(anonymous_client):
    response = anonymous_client.post("/api/settings/telegram/pairing-code")
    assert response.status_code == 401


def test_owner_pairing_is_single_use_and_never_leaks_data_before_pairing(client):
    with database.session() as connection:
        status = telegram.connection_status(connection)
    assert status["paired"] is False
    assert status["configured"] is False  # tests run with no token configured

    code = client.post("/api/settings/telegram/pairing-code").json()["code"]
    assert len(code) == 9 and "-" in code

    first = _process(_update(1, text=f"/start {code}"))
    assert "paired" in first["replies"][0].casefold()

    with database.session() as connection:
        state = database.get_telegram_state(connection)
    assert state["paired_chat_id"] == str(CHAT_ID)
    assert state["paired_user_id"] == str(CHAT_ID)
    assert state["paired_username"] == "owner_telegram"
    assert state["owner_username"] == "owner"

    reuse = _process(_update(2, text=f"/start {code}", message_id=2))
    assert "already used" in reuse["replies"][0].casefold()
    with database.session() as connection:
        assert database.get_telegram_state(connection)["paired_chat_id"] == str(CHAT_ID)


def test_wrong_and_expired_pairing_codes_are_rejected(client):
    wrong = _process(_update(1, text="/start AB12-CD34"))
    assert "not valid" in wrong["replies"][0].casefold()

    code = client.post("/api/settings/telegram/pairing-code").json()["code"]
    with database.session() as connection:
        connection.execute(
            "UPDATE telegram_pairing_codes SET expires_at = ?",
            ("2020-01-01T00:00:00+00:00",),
        )
        connection.commit()
    expired = _process(_update(2, text=f"/start {code}", message_id=2))
    assert "expired" in expired["replies"][0].casefold()


def test_unpaired_chat_receives_no_application_data(client):
    _project(client, "Secret Project")
    client.post("/api/tasks", json={"title": "Pay the invoice"})

    result = _process(_update(7, text="show me my tasks", chat_id=777))

    assert result["authorized"] is False
    reply = result["replies"][0]
    assert "not paired" in reply.casefold()
    assert "Pay the invoice" not in reply
    assert "Secret Project" not in reply


def test_group_chats_and_foreign_senders_are_ignored(client):
    _pair(client)
    assert _process(_update(9, text="/tasks", chat_type="group")) is None

    _process(_update(10, text="/tasks", chat_id=CHAT_ID, message_id=10))
    with database.session() as connection:
        state = database.get_telegram_state(connection)
        state["paired_user_id"] = "999999"
        database.update_telegram_state(connection, {"paired_user_id": "999999"})
    foreign = _process(_update(11, text="/tasks", message_id=11))
    assert foreign["authorized"] is False


def test_commands_use_existing_project_and_task_services(client):
    project = _project(client, "Command Project")
    client.post(
        "/api/tasks",
        json={
            "title": "Ship the release",
            "project_id": project["id"],
            "due_at": "2020-01-01T10:00",
        },
    )
    client.post("/api/tasks", json={"title": "Buy groceries", "project_id": project["id"]})
    _pair(client)

    today_reply = _process(_update(20, text="/today", message_id=20))["replies"][0]
    assert "Overdue" in today_reply and "Ship the release" in today_reply

    tasks_reply = _process(_update(21, text="/tasks", message_id=21))["replies"][0]
    assert "Ship the release" in tasks_reply and "Buy groceries" in tasks_reply

    deadlines_reply = _process(_update(22, text="/deadlines", message_id=22))["replies"][0]
    assert "Ship the release" in deadlines_reply

    projects_reply = _process(_update(23, text="/projects", message_id=23))["replies"][0]
    assert "Command Project" in projects_reply and "active" in projects_reply

    summary_reply = _process(_update(24, text="/summary", message_id=24))["replies"][0]
    assert "Intelligence summary" in summary_reply
    assert "Pending confirmation" in summary_reply

    help_reply = _process(_update(25, text="/help", message_id=25))["replies"][0]
    for command in ("/today", "/tasks", "/deadlines", "/projects", "/summary"):
        assert command in help_reply
    assert "account history" in help_reply

    unknown = _process(_update(26, text="/nonsense", message_id=26))["replies"][0]
    assert "not available" in unknown.casefold()


def test_natural_language_reuses_manager_and_stores_conversation(client, monkeypatch):
    _pair(client)
    calls = {}

    async def fake_manager(**kwargs):
        calls.update(kwargs)
        return agents.ManagerResult(
            content="The project is on track.",
            sources=[],
            execution={
                "id": "exec-1",
                "agent_id": "manager",
                "reason": "test",
                "status": "succeeded",
                "tools": [],
                "sources": [],
                "model_used": True,
                "steps": 1,
                "project_id": None,
            },
            document_context_included=False,
            provider="ollama",
            model="test-model",
        )

    monkeypatch.setattr(telegram, "_model_selection", lambda: ("ollama", "test-model"))
    monkeypatch.setattr(agents, "run_manager", fake_manager)

    result = _process(_update(30, text="How is my project doing?", message_id=30))

    assert result["kind"] == "ai"
    assert result["replies"] == ["The project is on track."]
    assert calls["message"] == "How is my project doing?"
    assert calls["provider_id"] == "ollama"
    assert calls["model"] == "test-model"
    assert calls["allow_cloud_context"] is False

    with database.session() as connection:
        conversation_id = database.get_telegram_state(connection)["conversation_id"]
        messages = database.get_messages(connection, conversation_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "The project is on track."

    second = _process(_update(31, text="And the deadline?", message_id=31))
    assert second["conversation_id"] == conversation_id


def test_missing_model_returns_guidance_instead_of_failing(client, monkeypatch):
    _pair(client)
    monkeypatch.setattr(telegram, "_model_selection", lambda: ("ollama", ""))

    result = _process(_update(32, text="Summarize my work", message_id=32))

    assert "No AI model is configured" in result["replies"][0]
    assert result["authorized"] is True


def test_duplicate_updates_and_messages_are_not_processed_twice(client, monkeypatch):
    _pair(client)
    monkeypatch.setattr(telegram, "_model_selection", lambda: ("ollama", ""))
    update = _update(40, text="Summarize my work", message_id=40)

    assert _process(update) is not None
    assert _process(update) is None  # same update replayed

    replay_with_new_update_id = _update(41, text="Summarize my work", message_id=40)
    assert _process(replay_with_new_update_id) is None  # same chat message replayed

    with database.session() as connection:
        received = connection.execute(
            """SELECT COUNT(*) AS count FROM telegram_messages
               WHERE direction = 'inbound' AND telegram_message_id = '40'"""
        ).fetchone()["count"]
    assert received == 1


def test_forwarded_messages_keep_identifiers_and_metadata(client, monkeypatch):
    _pair(client)
    monkeypatch.setattr(telegram, "_model_selection", lambda: ("ollama", ""))

    result = _process(
        _update(50, text="Forwarded note: ship by Friday", message_id=50, forwarded=True)
    )

    assert result["authorized"] is True
    with database.session() as connection:
        row = connection.execute(
            """SELECT telegram_message_id, is_forwarded, text_length, sent_at
               FROM telegram_messages
               WHERE chat_id = ? AND direction = 'inbound' AND telegram_message_id = '50'""",
            (str(CHAT_ID),),
        ).fetchone()
    assert row["is_forwarded"] == 1
    assert row["text_length"] == len("Forwarded note: ship by Friday")
    assert row["sent_at"]


def test_long_replies_are_chunked_and_failures_are_recorded(client, monkeypatch):
    chunks = telegram.chunk_text("word " * 2000)
    assert len(chunks) > 1
    assert all(len(chunk) <= telegram.MAX_MESSAGE_CHARS for chunk in chunks)

    sent: list[str] = []

    async def fake_api(method, payload=None, read_timeout=15.0):
        sent.append(payload["text"])
        return {"message_id": len(sent)}

    monkeypatch.setattr(telegram, "_api_call", fake_api)
    with database.session() as connection:
        delivered = asyncio.run(telegram.deliver(connection, str(CHAT_ID), ["x" * 8100]))
    assert delivered == 3
    assert len(sent) == 3

    async def failing_api(method, payload=None, read_timeout=15.0):
        raise telegram.TelegramError("chat not found")

    monkeypatch.setattr(telegram, "_api_call", failing_api)
    with database.session() as connection:
        failed = asyncio.run(telegram.deliver(connection, str(CHAT_ID), ["hello"]))
        row = connection.execute(
            "SELECT status FROM telegram_messages WHERE direction = 'outbound' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert failed == 0
    assert row["status"] == "failed"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.is_error = status_code >= 400

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):  # noqa: A002 - matches httpx signature
        return self._response


def test_rate_limits_are_surfaced_and_tokens_are_redacted(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:SECRET-TOKEN-VALUE")

    limited = _FakeClient(
        _FakeResponse(429, {"ok": False, "parameters": {"retry_after": 7}})
    )
    monkeypatch.setattr(telegram.httpx, "AsyncClient", limited)
    with pytest.raises(telegram.TelegramRateLimited) as exc:
        asyncio.run(telegram._api_call("getUpdates"))
    assert exc.value.retry_after == 7
    assert "SECRET-TOKEN-VALUE" not in str(exc.value)

    rejected = _FakeClient(
        _FakeResponse(401, {"ok": False, "description": "Unauthorized bot 123456:SECRET-TOKEN-VALUE"})
    )
    monkeypatch.setattr(telegram.httpx, "AsyncClient", rejected)
    with pytest.raises(telegram.TelegramError) as rejected_error:
        asyncio.run(telegram._api_call("getMe"))
    assert "SECRET-TOKEN-VALUE" not in str(rejected_error.value)
    assert "[redacted]" in str(rejected_error.value)


def test_settings_status_never_contains_the_bot_token(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:DO-NOT-EXPOSE")
    _pair(client)
    response = client.get("/api/settings/telegram")
    assert response.status_code == 200
    assert "DO-NOT-EXPOSE" not in response.text
    body = response.json()
    assert body["configured"] is True
    assert body["paired"] is True
    assert body["pairing"]["telegram_username"] == "owner_telegram"
    assert body["pairing"]["chat_id_suffix"] == str(CHAT_ID)[-4:]
    assert "history" in body["limits"]


def test_verify_reports_configuration_and_sanitized_failure(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    assert client.post("/api/settings/telegram/verify").status_code == 503

    monkeypatch.setattr(settings, "telegram_bot_token", "123:TEST")

    async def failing_me():
        raise telegram.TelegramUnavailable("Could not reach the Telegram Bot API.")

    monkeypatch.setattr(telegram, "get_me", failing_me)
    response = client.post("/api/settings/telegram/verify")
    assert response.status_code == 502
    assert "TEST" not in response.text.replace("TEST", "", 0) or "123:" not in response.text


def test_disconnect_unpairs_and_stops_serving(client, monkeypatch):
    _pair(client)

    async def fake_api(method, payload=None, read_timeout=15.0):
        return {"message_id": 501}

    monkeypatch.setattr(telegram, "_api_call", fake_api)

    response = client.post("/api/settings/telegram/disconnect")
    assert response.status_code == 200
    assert response.json()["paired"] is False

    after = _process(_update(60, text="/tasks", message_id=60))
    assert after["authorized"] is False
    assert "not paired" in after["replies"][0].casefold()

    with database.session() as connection:
        state = database.get_telegram_state(connection)
    assert state["paired_chat_id"] is None
    assert state["paired_user_id"] is None


def test_polling_lease_allows_only_one_worker(tmp_path, monkeypatch):
    # Isolated data directory: a running backend may hold the real polling lease.
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with database.session() as connection:
        database.initialize(connection)
        assert database.claim_telegram_polling_lease(connection, "worker-a") is True
        assert database.claim_telegram_polling_lease(connection, "worker-b") is False
        assert database.claim_telegram_polling_lease(connection, "worker-a") is True
        database.release_telegram_polling_lease(connection, "worker-a")
        assert database.claim_telegram_polling_lease(connection, "worker-b") is True
        database.release_telegram_polling_lease(connection, "worker-b")

        connection.execute(
            "UPDATE telegram_state SET poll_owner = 'stale', poll_heartbeat_at = ?",
            ("2020-01-01T00:00:00+00:00",),
        )
        connection.commit()
        assert database.claim_telegram_polling_lease(connection, "worker-c") is True
        database.release_telegram_polling_lease(connection, "worker-c")


def test_polling_worker_processes_updates_and_persists_the_offset(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:TEST")
    _pair(client)
    sent: list[dict] = []

    async def fake_me():
        return {"username": "rmatrix_test_bot"}

    async def fake_updates(offset):
        await asyncio.sleep(0.01)
        if offset <= 70:
            return [_update(70, text="/help", message_id=70)]
        return []

    async def fake_api(method, payload=None, read_timeout=15.0):
        sent.append(payload)
        return {"message_id": 700 + len(sent)}

    monkeypatch.setattr(telegram, "get_me", fake_me)
    monkeypatch.setattr(telegram, "get_updates", fake_updates)
    monkeypatch.setattr(telegram, "_api_call", fake_api)

    async def runner():
        stop = asyncio.Event()
        task = asyncio.create_task(telegram.polling_worker(stop))
        for _ in range(200):
            await asyncio.sleep(0.01)
            with database.session() as connection:
                if int(database.get_telegram_state(connection)["last_update_id"]) >= 71:
                    break
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(runner())

    with database.session() as connection:
        state = database.get_telegram_state(connection)
    assert int(state["last_update_id"]) >= 71
    assert state["bot_username"] == "rmatrix_test_bot"
    assert state["poll_owner"] is None
    assert sent and "R Matrix bot commands" in sent[0]["text"]


def test_telegram_metadata_never_stores_message_bodies(client, monkeypatch):
    _pair(client)
    monkeypatch.setattr(telegram, "_model_selection", lambda: ("ollama", ""))
    _process(_update(80, text="My private calendar detail", message_id=80))

    with database.session() as connection:
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(telegram_messages)")]
        rows = connection.execute("SELECT * FROM telegram_messages").fetchall()
    assert "text" not in columns
    assert all("private calendar" not in str(dict(row).values()) for row in rows)


def test_telegram_tables_are_created_on_a_legacy_database(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    connection.close()
    with database.session(path) as migrated:
        tables = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"telegram_state", "telegram_pairing_codes", "telegram_messages", "telegram_updates"} <= tables
        assert database.get_telegram_state(migrated)["paired_chat_id"] is None
