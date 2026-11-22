import json
import sqlite3
from pathlib import Path

import pytest

from app import backup, database, documents
from app.config import settings

SENSITIVE_KEYS = {
    "password_hash",
    "password_salt",
    "password_iterations",
    "token_hash",
    "csrf_hash",
    "session_token",
    "storage_name",
    "api_key",
}


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _upload(client, name: str = "notes.txt", content: bytes = b"Private export note."):
    return client.post(
        "/api/documents/upload",
        files={"file": (name, content, "text/plain")},
    )


def test_export_contains_owner_data_and_excludes_credentials(client):
    with database.session() as connection:
        conversation_id = database.create_conversation(connection, "Export check")
        database.add_message(connection, conversation_id, "user", "A private export message")
        database.create_task(connection, "Export task")

    uploaded = _upload(client)
    assert uploaded.status_code == 201

    with sqlite3.connect(settings.control_database_path) as connection:
        salt, password_hash = connection.execute(
            "SELECT password_salt, password_hash FROM owner_accounts"
        ).fetchone()

    response = client.get("/api/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()

    conversations = payload["conversations"]
    assert any(
        message["content"] == "A private export message"
        for bundle in conversations
        for message in bundle["messages"]
    )
    assert any(task["title"] == "Export task" for task in payload["tasks"])
    assert payload["documents"][0]["original_name"] == "notes.txt"
    assert "extracted_text" not in payload["documents"][0]

    assert not SENSITIVE_KEYS.intersection(set(_keys(payload)))
    assert salt and salt not in response.text
    assert password_hash and password_hash not in response.text


def test_export_requires_authentication(anonymous_client):
    assert anonymous_client.get("/api/export").status_code == 401


def test_malformed_payloads_are_rejected(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/documents/search", json={}).status_code == 422
    assert client.post("/api/tasks", json={"title": ""}).status_code == 422


def test_upload_filename_cannot_escape_document_storage(client, tmp_path):
    response = _upload(client, name="../../escape.txt", content=b"Traversal attempt file.")
    assert response.status_code == 201
    assert not (settings.data_dir.parent / "escape.txt").exists()
    assert not (Path(settings.data_dir).parent.parent / "escape.txt").exists()
    stored = list(Path(settings.documents_dir).iterdir())
    assert len(stored) == 1
    assert stored[0].parent == Path(settings.documents_dir)


def test_provider_privacy_metadata_is_reported(client):
    payload = client.get("/api/providers").json()
    providers = {entry["id"]: entry for entry in payload["providers"]}
    assert providers["ollama"]["privacy"]["processing"] == "local"
    assert providers["ollama"]["privacy"]["leaves_device"] is False
    assert providers["ollama"]["privacy"]["api_key_required"] is False
    for entry in providers.values():
        assert entry["privacy"]["data_sent"]
        if entry["cloud"]:
            assert entry["privacy"]["leaves_device"] is True


def test_backup_create_verify_and_restore_is_isolated(client, tmp_path):
    _upload(client)
    with database.session() as connection:
        document_count = len(documents.list_documents(connection))

    backup_dir = backup.create_backup(tmp_path / "backups")
    assert backup_dir.is_dir()
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["name"] for entry in manifest["databases"]} == {
        "r_matrix.sqlite3",
        "control_plane.sqlite3",
    }
    assert manifest["documents"]["included"] is True

    report = backup.verify_backup(backup_dir)
    assert report["ok"] is True
    assert all(entry["integrity_check"] == "ok" for entry in report["databases"])

    restore_target = tmp_path / "restored"
    backup.restore_backup(backup_dir, restore_target)
    with sqlite3.connect(restore_target / "r_matrix.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == document_count
    assert (restore_target / "documents").is_dir()

    with pytest.raises(ValueError, match="live data directory"):
        backup.restore_backup(backup_dir, settings.data_dir)
    with pytest.raises(ValueError, match="not empty"):
        backup.restore_backup(backup_dir, restore_target)


def test_backup_never_overwrites_an_existing_backup(client, tmp_path, monkeypatch):
    import app.backup as backup_module

    class FixedDatetime(backup_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(backup_module, "datetime", FixedDatetime)
    root = tmp_path / "backups"
    created = backup.create_backup(root)
    with pytest.raises(FileExistsError):
        backup.create_backup(root)
    assert created.is_dir()


def test_document_deletion_removes_file_pages_and_chunks(client):
    uploaded = _upload(client)
    assert uploaded.status_code == 201
    document = uploaded.json()
    stored = list(Path(settings.documents_dir).iterdir())
    assert stored

    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?", (document["id"],)
        ).fetchone()[0] > 0

    assert client.delete(f"/api/documents/{document['id']}").status_code == 204
    assert not any(Path(settings.documents_dir).iterdir())
    with sqlite3.connect(settings.database_path) as connection:
        for table in ("document_pages", "document_chunks"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE document_id = ?", (document["id"],)
            ).fetchone()[0] == 0
    assert client.get("/api/documents").json() == []
