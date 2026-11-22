import sqlite3

from app import database


def test_legacy_messages_table_migrates_provider_and_model(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            sources_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        """
    )
    connection.close()

    with database.session(path) as migrated:
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert {"provider", "model"} <= columns
        conversation_id = database.create_conversation(migrated, "Migrated history")
        database.add_message(
            migrated,
            conversation_id,
            "assistant",
            "Saved with provider metadata.",
            provider="openai",
            model="test-model",
        )
        message = database.get_messages(migrated, conversation_id)[0]
        assert message["provider"] == "openai"
        assert message["model"] == "test-model"


def test_legacy_documents_and_memories_are_preserved_during_schema_extension(tmp_path):
    path = tmp_path / "legacy-knowledge.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO conversations VALUES ('conversation', 'Old conversation', '2026-01-01', '2026-01-01');
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            original_name TEXT NOT NULL,
            storage_name TEXT NOT NULL UNIQUE,
            extension TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            page_count INTEGER,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO documents VALUES ('document', 'notes.txt', 'document.txt', '.txt', 14, NULL, 'indexed', '2026-01-01');
        CREATE TABLE document_pages (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER,
            content TEXT NOT NULL
        );
        INSERT INTO document_pages VALUES ('page', 'document', NULL, 'Legacy indexed text');
        CREATE TABLE approved_memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            provenance TEXT NOT NULL,
            source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO approved_memories VALUES ('memory', 'Old approved memory', 'hash', 'user', 'conversation', '2026-01-01');
        """
    )
    connection.close()

    with database.session(path) as migrated:
        document = migrated.execute(
            "SELECT original_name, content_hash, chunk_count FROM documents WHERE id = 'document'"
        ).fetchone()
        memory = database.list_approved_memories(migrated)[0]
        assert document["original_name"] == "notes.txt"
        assert document["content_hash"] is None
        assert document["chunk_count"] == 0
        assert memory["content"] == "Old approved memory"
        assert memory["updated_at"] == "2026-01-01"
        assert migrated.execute(
            "SELECT content FROM document_pages WHERE id = 'page'"
        ).fetchone()["content"] == "Legacy indexed text"
