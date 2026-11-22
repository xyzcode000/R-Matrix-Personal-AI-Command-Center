import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = database_path or settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Document extraction and OCR run in worker threads while the request
    # owns this connection; access is sequential, so cross-thread use is safe.
    connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    initialize(connection)
    return connection


@contextmanager
def session(database_path: Path | None = None):
    connection = connect(database_path)
    try:
        yield connection
    finally:
        connection.close()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            sources_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            execution_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
            ON messages(conversation_id, created_at);
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            original_name TEXT NOT NULL,
            storage_name TEXT NOT NULL UNIQUE,
            extension TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            page_count INTEGER,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            content_hash TEXT,
            extracted_text_available INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            last_processed_at TEXT,
            processing_error TEXT,
            extraction_method TEXT
        );
        CREATE TABLE IF NOT EXISTS document_pages (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER,
            content TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_document_pages_document
            ON document_pages(document_id);
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            page_number INTEGER,
            section_heading TEXT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(document_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_document_chunks_document
            ON document_chunks(document_id, chunk_index);
        CREATE TABLE IF NOT EXISTS application_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'blocked', 'done')),
            conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completion_evidence TEXT
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'planned', 'paused', 'completed', 'archived')),
            priority TEXT NOT NULL DEFAULT 'medium'
                CHECK(priority IN ('low', 'medium', 'high', 'urgent')),
            progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
            start_date TEXT,
            target_date TEXT,
            tech_stack_json TEXT NOT NULL DEFAULT '[]',
            github_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id);
        CREATE TABLE IF NOT EXISTS project_milestones (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            target_date TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'completed')),
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_project_milestones_project ON project_milestones(project_id, target_date);
        CREATE TABLE IF NOT EXISTS project_notes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN ('architecture', 'decision', 'development', 'requirement', 'bug', 'implementation', 'idea')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_project_notes_project ON project_notes(project_id, updated_at);
        CREATE TABLE IF NOT EXISTS owner_profile (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            full_name TEXT NOT NULL,
            preferred_name TEXT NOT NULL,
            user_role TEXT NOT NULL,
            personal_context TEXT NOT NULL DEFAULT '',
            project_context TEXT NOT NULL DEFAULT '',
            current_priorities TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_reminders (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES application_tasks(id) ON DELETE CASCADE,
            scheduled_at TEXT NOT NULL,
            reminder_preference TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            delivered_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_task_reminders_due
            ON task_reminders(status, next_attempt_at, scheduled_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_reminders_one_pending
            ON task_reminders(task_id) WHERE status = 'pending';
        CREATE TABLE IF NOT EXISTS agent_settings (
            agent_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approved_memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            provenance TEXT NOT NULL,
            source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            category TEXT,
            project_id TEXT,
            title TEXT NOT NULL DEFAULT '',
            importance INTEGER NOT NULL DEFAULT 3,
            confidence INTEGER NOT NULL DEFAULT 3,
            expires_at TEXT,
            archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS memory_proposals (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            provenance TEXT NOT NULL,
            source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            category TEXT,
            project_id TEXT,
            title TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            importance INTEGER NOT NULL DEFAULT 3,
            confidence INTEGER NOT NULL DEFAULT 3
        );
        CREATE INDEX IF NOT EXISTS idx_memory_proposals_status
            ON memory_proposals(status, created_at);
        CREATE TABLE IF NOT EXISTS action_approvals (
            id TEXT PRIMARY KEY,
            operation TEXT NOT NULL CHECK(operation IN ('file.write', 'external.communication', 'security.change')),
            target TEXT NOT NULL,
            summary TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'cancelled')),
            conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_executions (
            id TEXT PRIMARY KEY,
            conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            agent_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('succeeded', 'partial', 'failed')),
            tools_json TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE IF NOT EXISTS intelligence_checkpoints (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            last_message_rowid INTEGER NOT NULL DEFAULT 0,
            last_message_id TEXT,
            last_message_created_at TEXT,
            messages_processed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS intelligence_proposals (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('project', 'task', 'task_completion', 'project_progress', 'blocker', 'milestone')),
            status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected')),
            confidence REAL NOT NULL DEFAULT 0.5,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            candidate_project_name TEXT,
            workspace_hint TEXT,
            task_id TEXT,
            due_at TEXT,
            due_phrase TEXT,
            priority TEXT,
            progress_value INTEGER,
            milestone_target_date TEXT,
            conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            message_id TEXT,
            message_created_at TEXT,
            excerpt TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            review_note TEXT,
            result_id TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intelligence_proposals_fingerprint
            ON intelligence_proposals(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_intelligence_proposals_status
            ON intelligence_proposals(status, kind, created_at);
        CREATE TABLE IF NOT EXISTS project_aliases (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_project_aliases_alias ON project_aliases(alias);
        CREATE INDEX IF NOT EXISTS idx_project_aliases_project ON project_aliases(project_id);
        CREATE TABLE IF NOT EXISTS intelligence_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            conversations_scanned INTEGER NOT NULL DEFAULT 0,
            messages_scanned INTEGER NOT NULL DEFAULT 0,
            messages_skipped INTEGER NOT NULL DEFAULT 0,
            tasks_detected INTEGER NOT NULL DEFAULT 0,
            duplicates_skipped INTEGER NOT NULL DEFAULT 0,
            proposals_created INTEGER NOT NULL DEFAULT 0,
            model_used INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS telegram_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            paired_chat_id TEXT,
            paired_user_id TEXT,
            paired_username TEXT,
            paired_at TEXT,
            owner_username TEXT,
            conversation_id TEXT,
            last_update_id INTEGER NOT NULL DEFAULT 0,
            bot_username TEXT,
            last_poll_at TEXT,
            last_error TEXT,
            last_error_at TEXT,
            poll_owner TEXT,
            poll_heartbeat_at TEXT,
            messages_received INTEGER NOT NULL DEFAULT 0,
            messages_sent INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_pairing_codes (
            id TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            owner_username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_telegram_pairing_codes_expiry
            ON telegram_pairing_codes(used_at, expires_at);
        CREATE TABLE IF NOT EXISTS telegram_updates (
            update_id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            processed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id TEXT PRIMARY KEY,
            update_id INTEGER,
            chat_id TEXT NOT NULL,
            telegram_message_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
            kind TEXT NOT NULL,
            is_forwarded INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT,
            conversation_id TEXT,
            message_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, telegram_message_id, direction)
        );
        CREATE INDEX IF NOT EXISTS idx_telegram_messages_chat
            ON telegram_messages(chat_id, direction, created_at);
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )
    message_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "provider" not in message_columns:
        connection.execute("ALTER TABLE messages ADD COLUMN provider TEXT")
    if "model" not in message_columns:
        connection.execute("ALTER TABLE messages ADD COLUMN model TEXT")
    if "execution_json" not in message_columns:
        connection.execute("ALTER TABLE messages ADD COLUMN execution_json TEXT")
    execution_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(agent_executions)").fetchall()
    }
    if "project_id" not in execution_columns:
        connection.execute("ALTER TABLE agent_executions ADD COLUMN project_id TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_project ON agent_executions(project_id, created_at)"
    )
    document_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(documents)").fetchall()
    }
    for column, definition in (
        ("content_hash", "TEXT"),
        ("extracted_text_available", "INTEGER NOT NULL DEFAULT 0"),
        ("chunk_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_processed_at", "TEXT"),
        ("processing_error", "TEXT"),
        ("extraction_method", "TEXT"),
        ("display_name", "TEXT"),
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("category", "TEXT NOT NULL DEFAULT 'Other'"),
        ("mime_type", "TEXT"),
        ("processed_pages", "INTEGER NOT NULL DEFAULT 0"),
        ("failed_pages", "INTEGER NOT NULL DEFAULT 0"),
        ("ocr_status", "TEXT"),
        ("processing_stage", "TEXT"),
        ("processing_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_attempt_at", "TEXT"),
        ("document_type", "TEXT"),
    ):
        if column not in document_columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
    page_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(document_pages)").fetchall()
    }
    for column, definition in (
        ("status", "TEXT NOT NULL DEFAULT 'ok'"),
        ("section_heading", "TEXT"),
        ("extraction_method", "TEXT"),
        ("ocr_confidence", "REAL"),
        ("error_category", "TEXT"),
        ("error_detail", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("chunk_count", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in page_columns:
            connection.execute(f"ALTER TABLE document_pages ADD COLUMN {column} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_pages_number ON document_pages(document_id, page_number)"
    )
    # Backfill page progress for documents indexed before page-level status existed.
    connection.execute(
        """UPDATE documents SET
             processed_pages = (
                 SELECT COUNT(*) FROM document_pages p
                 WHERE p.document_id = documents.id AND p.status = 'ok' AND TRIM(p.content) <> ''
             ),
             failed_pages = (
                 SELECT COUNT(*) FROM document_pages p
                 WHERE p.document_id = documents.id AND p.status <> 'ok'
             )
           WHERE processed_pages = 0
             AND EXISTS (SELECT 1 FROM document_pages p WHERE p.document_id = documents.id)"""
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_processing_jobs (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('process', 'rebuild_index')),
            status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'partial', 'failed', 'retry_required', 'interrupted')),
            stage TEXT,
            attempts INTEGER NOT NULL DEFAULT 1,
            pages_total INTEGER NOT NULL DEFAULT 0,
            pages_done INTEGER NOT NULL DEFAULT 0,
            error_category TEXT,
            error_detail TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_jobs_document ON document_processing_jobs(document_id, started_at)"
    )
    # Owner-facing names were introduced after the first documents existed.
    connection.execute("UPDATE documents SET display_name = original_name WHERE display_name IS NULL OR TRIM(display_name) = ''")
    connection.execute("UPDATE documents SET category = 'Other' WHERE category IS NULL OR TRIM(category) = ''")
    memory_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(approved_memories)").fetchall()
    }
    if "updated_at" not in memory_columns:
        connection.execute("ALTER TABLE approved_memories ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE approved_memories SET updated_at = created_at WHERE updated_at = ''")
    if "category" not in memory_columns:
        connection.execute("ALTER TABLE approved_memories ADD COLUMN category TEXT")
    if "project_id" not in memory_columns:
        connection.execute("ALTER TABLE approved_memories ADD COLUMN project_id TEXT")
    for column, definition in (
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("importance", "INTEGER NOT NULL DEFAULT 3"),
        ("confidence", "INTEGER NOT NULL DEFAULT 3"),
        ("expires_at", "TEXT"),
        ("archived_at", "TEXT"),
    ):
        if column not in memory_columns:
            connection.execute(f"ALTER TABLE approved_memories ADD COLUMN {column} {definition}")
    proposal_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(memory_proposals)").fetchall()
    }
    for column, definition in (
        ("category", "TEXT"),
        ("project_id", "TEXT"),
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("importance", "INTEGER NOT NULL DEFAULT 3"),
        ("confidence", "INTEGER NOT NULL DEFAULT 3"),
    ):
        if column not in proposal_columns:
            connection.execute(f"ALTER TABLE memory_proposals ADD COLUMN {column} {definition}")
    task_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(application_tasks)").fetchall()
    }
    for column, definition in (
        ("due_at", "TEXT"),
        ("reminder_preference", "TEXT NOT NULL DEFAULT 'none'"),
        ("completed_at", "TEXT"),
        ("deleted_at", "TEXT"),
        ("priority", "TEXT NOT NULL DEFAULT 'medium'"),
        ("project_id", "TEXT"),
        ("milestone_id", "TEXT"),
        ("origin", "TEXT NOT NULL DEFAULT 'manual'"),
        ("confirmation_status", "TEXT NOT NULL DEFAULT 'confirmed'"),
        ("confirmation_note", "TEXT"),
        ("confirmed_at", "TEXT"),
        ("confidence", "REAL"),
        ("source_message_id", "TEXT"),
        ("source_excerpt", "TEXT"),
        ("blocked_by_task_id", "TEXT"),
        ("dedupe_hash", "TEXT"),
        ("due_precision", "TEXT"),
        ("detected_at", "TEXT"),
        ("archived_at", "TEXT"),
    ):
        if column not in task_columns:
            connection.execute(f"ALTER TABLE application_tasks ADD COLUMN {column} {definition}")
    connection.execute(
        """INSERT OR IGNORE INTO owner_profile
           (id, full_name, preferred_name, user_role, personal_context, project_context, current_priorities, updated_at)
           VALUES (1, 'Rahil Wankhede', 'Rahil', 'owner', '', '', '', ?)""",
        (utc_now(),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES ('reminder_recipient', '', ?)",
        (utc_now(),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO telegram_state (id, updated_at) VALUES (1, ?)",
        (utc_now(),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (2, ?)",
        (utc_now(),),
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_content_hash
           ON documents(content_hash) WHERE content_hash IS NOT NULL"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_application_tasks_project ON application_tasks(project_id, status, updated_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_application_tasks_dedupe ON application_tasks(dedupe_hash, project_id)"
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_application_tasks_lifecycle
           ON application_tasks(confirmation_status, archived_at, status, due_at)"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_approved_memories_project ON approved_memories(project_id, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_proposals_project ON memory_proposals(project_id, status, created_at)"
    )
    connection.commit()


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
    project = dict(row)
    project["tech_stack"] = json.loads(project.pop("tech_stack_json"))
    return project


def ensure_workspace_projects(
    connection: sqlite3.Connection,
    workspaces: dict[str, Path],
) -> None:
    now = utc_now()
    for workspace_id, path in workspaces.items():
        existing = connection.execute(
            "SELECT id FROM projects WHERE workspace_id = ? LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if existing:
            continue
        connection.execute(
            """INSERT OR IGNORE INTO projects
               (id, name, workspace_id, created_at, updated_at, last_activity_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (workspace_id, path.name, workspace_id, now, now, now),
        )
    connection.commit()


def list_projects(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, name, description, workspace_id, status, priority, progress, start_date,
                  target_date, tech_stack_json, github_url, created_at, updated_at, last_activity_at
           FROM projects ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'planned' THEN 1
             WHEN 'paused' THEN 2 WHEN 'completed' THEN 3 ELSE 4 END, updated_at DESC"""
    ).fetchall()
    return [_project_dict(row) for row in rows]


def get_project(connection: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT id, name, description, workspace_id, status, priority, progress, start_date,
                  target_date, tech_stack_json, github_url, created_at, updated_at, last_activity_at
           FROM projects WHERE id = ?""",
        (project_id,),
    ).fetchone()
    return _project_dict(row) if row else None


def create_project(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    name: str,
    description: str,
    workspace_id: str,
    status: str,
    priority: str,
    progress: int,
    start_date: str | None,
    target_date: str | None,
    tech_stack: list[str],
    github_url: str | None,
) -> dict[str, Any]:
    now = utc_now()
    connection.execute(
        """INSERT INTO projects
           (id, name, description, workspace_id, status, priority, progress, start_date,
            target_date, tech_stack_json, github_url, created_at, updated_at, last_activity_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id, name.strip(), description.strip(), workspace_id, status, priority,
            progress, start_date, target_date, json.dumps(tech_stack), github_url, now, now, now,
        ),
    )
    connection.commit()
    project = get_project(connection, project_id)
    if project is None:
        raise RuntimeError("The project was not available after creation.")
    return project


def update_project(connection: sqlite3.Connection, project_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "name", "description", "status", "priority", "progress", "start_date",
        "target_date", "tech_stack", "github_url",
    }
    if not fields or set(fields) - allowed:
        raise ValueError("The project update contains unsupported fields.")
    values = dict(fields)
    if "tech_stack" in values:
        values["tech_stack_json"] = json.dumps(values.pop("tech_stack"))
    assignments = ", ".join(f"{key} = ?" for key in values)
    now = utc_now()
    cursor = connection.execute(
        f"UPDATE projects SET {assignments}, updated_at = ?, last_activity_at = ? WHERE id = ?",
        (*values.values(), now, now, project_id),
    )
    if not cursor.rowcount:
        return None
    connection.commit()
    return get_project(connection, project_id)


def delete_project(connection: sqlite3.Connection, project_id: str) -> bool:
    references = connection.execute(
        """SELECT
             (SELECT COUNT(*) FROM application_tasks WHERE project_id = ? AND deleted_at IS NULL) +
             (SELECT COUNT(*) FROM approved_memories WHERE project_id = ?) +
             (SELECT COUNT(*) FROM memory_proposals WHERE project_id = ?) +
             (SELECT COUNT(*) FROM project_notes WHERE project_id = ?) +
             (SELECT COUNT(*) FROM project_milestones WHERE project_id = ?) AS count""",
        (project_id, project_id, project_id, project_id, project_id),
    ).fetchone()["count"]
    if references:
        return False
    cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    connection.commit()
    return bool(cursor.rowcount)


def touch_project(connection: sqlite3.Connection, project_id: str) -> None:
    now = utc_now()
    connection.execute("UPDATE projects SET updated_at = ?, last_activity_at = ? WHERE id = ?", (now, now, project_id))
    connection.commit()


def list_project_milestones(connection: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, project_id, title, description, target_date, status, completed_at, created_at, updated_at
           FROM project_milestones WHERE project_id = ?
           ORDER BY CASE WHEN target_date IS NULL THEN 1 ELSE 0 END, target_date, created_at""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_project_milestone(
    connection: sqlite3.Connection,
    project_id: str,
    title: str,
    description: str,
    target_date: str | None,
    status: str = "pending",
    milestone_id: str | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    completed_at = now if status == "completed" else None
    if milestone_id:
        cursor = connection.execute(
            """UPDATE project_milestones SET title = ?, description = ?, target_date = ?, status = ?,
                      completed_at = ?, updated_at = ? WHERE id = ? AND project_id = ?""",
            (title.strip(), description.strip(), target_date, status, completed_at, now, milestone_id, project_id),
        )
        if not cursor.rowcount:
            return None
        saved_id = milestone_id
    else:
        saved_id = str(uuid4())
        connection.execute(
            """INSERT INTO project_milestones
               (id, project_id, title, description, target_date, status, completed_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (saved_id, project_id, title.strip(), description.strip(), target_date, status, completed_at, now, now),
        )
    touch_project(connection, project_id)
    row = connection.execute(
        """SELECT id, project_id, title, description, target_date, status, completed_at, created_at, updated_at
           FROM project_milestones WHERE id = ?""",
        (saved_id,),
    ).fetchone()
    row_project_id = row["project_id"] if row else None
    if row_project_id:
        touch_project(connection, row_project_id)
    return dict(row) if row else None


def delete_project_milestone(connection: sqlite3.Connection, project_id: str, milestone_id: str) -> bool:
    cursor = connection.execute(
        "DELETE FROM project_milestones WHERE id = ? AND project_id = ?",
        (milestone_id, project_id),
    )
    if cursor.rowcount:
        touch_project(connection, project_id)
    connection.commit()
    return bool(cursor.rowcount)


def list_project_notes(connection: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, project_id, title, content, category, created_at, updated_at
           FROM project_notes WHERE project_id = ? ORDER BY updated_at DESC LIMIT 200""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_project_note(
    connection: sqlite3.Connection,
    project_id: str,
    title: str,
    content: str,
    category: str,
    note_id: str | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    if note_id:
        cursor = connection.execute(
            """UPDATE project_notes SET title = ?, content = ?, category = ?, updated_at = ?
               WHERE id = ? AND project_id = ?""",
            (title.strip(), content.strip(), category, now, note_id, project_id),
        )
        if not cursor.rowcount:
            return None
        saved_id = note_id
    else:
        saved_id = str(uuid4())
        connection.execute(
            """INSERT INTO project_notes
               (id, project_id, title, content, category, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (saved_id, project_id, title.strip(), content.strip(), category, now, now),
        )
    touch_project(connection, project_id)
    row = connection.execute(
        """SELECT id, project_id, title, content, category, created_at, updated_at
           FROM project_notes WHERE id = ?""",
        (saved_id,),
    ).fetchone()
    return dict(row) if row else None


def delete_project_note(connection: sqlite3.Connection, project_id: str, note_id: str) -> bool:
    cursor = connection.execute(
        "DELETE FROM project_notes WHERE id = ? AND project_id = ?",
        (note_id, project_id),
    )
    if cursor.rowcount:
        touch_project(connection, project_id)
    connection.commit()
    return bool(cursor.rowcount)


def create_conversation(connection: sqlite3.Connection, title: str) -> str:
    conversation_id = str(uuid4())
    now = utc_now()
    connection.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conversation_id, title[:80], now, now),
    )
    connection.commit()
    return conversation_id


def add_message(
    connection: sqlite3.Connection,
    conversation_id: str,
    role: str,
    content: str,
    sources: list[dict[str, Any]] | None = None,
    provider: str | None = None,
    model: str | None = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = {
        "id": str(uuid4()),
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "sources": sources or [],
        "created_at": utc_now(),
        "provider": provider,
        "model": model,
        "execution": execution,
    }
    connection.execute(
        """INSERT INTO messages
        (id, conversation_id, role, content, sources_json, created_at, provider, model, execution_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message["id"],
            conversation_id,
            role,
            content,
            json.dumps(message["sources"]),
            message["created_at"],
            provider,
            model,
            json.dumps(execution) if execution is not None else None,
        ),
    )
    connection.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (message["created_at"], conversation_id),
    )
    connection.commit()
    return message


def list_conversations(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT c.id, c.title, c.created_at, c.updated_at,
                  (SELECT content FROM messages m
                   WHERE m.conversation_id = c.id
                   ORDER BY m.created_at DESC LIMIT 1) AS last_message
           FROM conversations c ORDER BY c.updated_at DESC LIMIT 100"""
    ).fetchall()
    return [row_dict(row) for row in rows]


def delete_conversation(connection: sqlite3.Connection, conversation_id: str) -> bool:
    cursor = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    connection.commit()
    return cursor.rowcount > 0


def get_messages(connection: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, conversation_id, role, content, sources_json, created_at, provider, model, execution_json
           FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid""",
        (conversation_id,),
    ).fetchall()
    return [
        {
            **{key: row[key] for key in ("id", "conversation_id", "role", "content", "created_at")},
            "sources": json.loads(row["sources_json"]),
            "provider": row["provider"],
            "model": row["model"],
            "execution": json.loads(row["execution_json"]) if row["execution_json"] else None,
        }
        for row in rows
    ]


TASK_COLUMNS = (
    "id, title, description, status, conversation_id, created_at, updated_at, due_at, "
    "reminder_preference, completed_at, priority, project_id, milestone_id, origin, "
    "confirmation_status, confirmation_note, confirmed_at, confidence, source_message_id, "
    "source_excerpt, blocked_by_task_id, dedupe_hash, due_precision, detected_at, archived_at"
)
TASK_LIFECYCLES = (
    "detected",
    "pending_confirmation",
    "upcoming",
    "in_progress",
    "blocked",
    "completed",
    "overdue",
    "archived",
)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def task_lifecycle(task: dict[str, Any], now: str | None = None) -> str:
    """Derives the task lifecycle from stored, non-destructive task facts.

    `detected` is a freshly discovered task that is not yet attributed to a project;
    `pending_confirmation` is attributed work that still awaits the owner's confirmation.
    Nothing here completes or archives a task; those are explicit user decisions.
    """
    if task.get("archived_at"):
        return "archived"
    if task.get("status") == "done":
        return "completed"
    if (task.get("confirmation_status") or "confirmed") == "pending":
        return "pending_confirmation" if task.get("project_id") else "detected"
    if task.get("status") == "blocked":
        return "blocked"
    if task.get("status") == "in_progress":
        return "in_progress"
    due_at = parse_timestamp(task.get("due_at"))
    if due_at is not None:
        reference = parse_timestamp(now) or datetime.now(timezone.utc)
        return "overdue" if due_at < reference else "upcoming"
    return "detected"


def _task_dict(row: sqlite3.Row, now: str | None = None) -> dict[str, Any]:
    task = dict(row)
    task["lifecycle"] = task_lifecycle(task, now)
    return task


def get_task(connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT {TASK_COLUMNS} FROM application_tasks WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    ).fetchone()
    return _task_dict(row) if row else None


def create_task(
    connection: sqlite3.Connection,
    title: str,
    description: str = "",
    conversation_id: str | None = None,
    due_at: str | None = None,
    reminder_preference: str = "none",
    priority: str = "medium",
    project_id: str | None = None,
    milestone_id: str | None = None,
    *,
    origin: str = "manual",
    confirmation_status: str = "confirmed",
    confirmation_note: str | None = None,
    confidence: float | None = None,
    source_message_id: str | None = None,
    source_excerpt: str | None = None,
    blocked_by_task_id: str | None = None,
    dedupe_hash: str | None = None,
    due_precision: str | None = None,
    detected_at: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    task = {
        "id": str(uuid4()),
        "title": title.strip(),
        "description": description.strip(),
        "status": "pending",
        "conversation_id": conversation_id,
        "created_at": now,
        "due_at": due_at,
        "reminder_preference": reminder_preference,
        "completed_at": None,
        "priority": priority,
        "project_id": project_id,
        "milestone_id": milestone_id,
        "origin": origin,
        "confirmation_status": confirmation_status,
        "confirmation_note": confirmation_note,
        "confirmed_at": now if confirmation_status == "confirmed" else None,
        "confidence": confidence,
        "source_message_id": source_message_id,
        "source_excerpt": source_excerpt,
        "blocked_by_task_id": blocked_by_task_id,
        "dedupe_hash": dedupe_hash,
        "due_precision": due_precision,
        "detected_at": detected_at or (now if origin != "manual" else None),
        "archived_at": None,
    }
    connection.execute(
        """INSERT INTO application_tasks
           (id, title, description, status, conversation_id, created_at, updated_at, due_at,
            reminder_preference, priority, project_id, milestone_id, origin, confirmation_status,
            confirmation_note, confirmed_at, confidence, source_message_id, source_excerpt,
            blocked_by_task_id, dedupe_hash, due_precision, detected_at)
           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task["id"],
            task["title"],
            task["description"],
            conversation_id,
            task["created_at"],
            task["created_at"],
            due_at,
            reminder_preference,
            priority,
            project_id,
            milestone_id,
            origin,
            confirmation_status,
            confirmation_note,
            task["confirmed_at"],
            confidence,
            source_message_id,
            source_excerpt,
            blocked_by_task_id,
            dedupe_hash,
            due_precision,
            task["detected_at"],
        ),
    )
    if project_id:
        touch_project(connection, project_id)
    connection.commit()
    return task


def list_tasks(
    connection: sqlite3.Connection,
    status: str | None = None,
    project_id: str | None = None,
    lifecycle: str | None = None,
    *,
    include_archived: bool = False,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    conditions = ["deleted_at IS NULL"]
    values: list[str] = []
    if status:
        conditions.append("status = ?")
        values.append(status)
    if project_id is not None:
        conditions.append("project_id = ?")
        values.append(project_id)
    if origin:
        conditions.append("origin = ?")
        values.append(origin)
    if not include_archived:
        conditions.append("archived_at IS NULL")
    rows = connection.execute(
        f"""SELECT {TASK_COLUMNS}
            FROM application_tasks WHERE {' AND '.join(conditions)}
            ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, updated_at DESC LIMIT 200""",
        values,
    ).fetchall()
    tasks = [_task_dict(row) for row in rows]
    if lifecycle:
        tasks = [task for task in tasks if task["lifecycle"] == lifecycle]
    return tasks


def update_task_status(
    connection: sqlite3.Connection,
    task_id: str,
    status: str,
) -> dict[str, Any] | None:
    if status == "done":
        raise ValueError("A task cannot be completed without a verified underlying action result.")
    if status not in {"pending", "in_progress", "blocked"}:
        raise ValueError("Unsupported task status.")
    now = utc_now()
    cursor = connection.execute(
        """UPDATE application_tasks SET status = ?, updated_at = ?,
                  confirmation_status = 'confirmed', confirmed_at = COALESCE(confirmed_at, ?)
           WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL""",
        (status, now, now, task_id),
    )
    connection.commit()
    if not cursor.rowcount:
        return None
    task = get_task(connection, task_id)
    if task and task["project_id"]:
        touch_project(connection, task["project_id"])
    return task


def get_owner_profile(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        """SELECT full_name, preferred_name, user_role, personal_context, project_context,
                  current_priorities, updated_at FROM owner_profile WHERE id = 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError("The owner profile was not initialized.")
    return dict(row)


def get_agent_settings(connection: sqlite3.Connection, agent_ids: list[str]) -> dict[str, bool]:
    now = utc_now()
    connection.executemany(
        "INSERT OR IGNORE INTO agent_settings (agent_id, enabled, updated_at) VALUES (?, 1, ?)",
        [(agent_id, now) for agent_id in agent_ids],
    )
    connection.commit()
    rows = connection.execute(
        "SELECT agent_id, enabled FROM agent_settings"
    ).fetchall()
    enabled = {row["agent_id"]: bool(row["enabled"]) for row in rows}
    return {agent_id: enabled.get(agent_id, True) for agent_id in agent_ids}


def set_agent_enabled(connection: sqlite3.Connection, agent_id: str, enabled: bool) -> None:
    connection.execute(
        """INSERT INTO agent_settings (agent_id, enabled, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(agent_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at""",
        (agent_id, int(enabled), utc_now()),
    )
    connection.commit()


def agent_is_enabled(connection: sqlite3.Connection, agent_id: str) -> bool:
    row = connection.execute(
        "SELECT enabled FROM agent_settings WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    return True if row is None else bool(row["enabled"])


def update_owner_profile(connection: sqlite3.Connection, fields: dict[str, str]) -> dict[str, Any]:
    allowed = {
        "full_name", "preferred_name", "user_role", "personal_context", "project_context",
        "current_priorities",
    }
    if not fields or set(fields) - allowed:
        raise ValueError("The owner profile update contains unsupported fields.")
    assignments = ", ".join(f"{key} = ?" for key in fields)
    connection.execute(
        f"UPDATE owner_profile SET {assignments}, updated_at = ? WHERE id = 1",
        (*fields.values(), utc_now()),
    )
    connection.commit()
    return get_owner_profile(connection)


def set_reminder_recipient(connection: sqlite3.Connection, email: str) -> None:
    connection.execute(
        """INSERT INTO app_settings (key, value, updated_at) VALUES ('reminder_recipient', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (email, utc_now()),
    )
    connection.commit()


def get_reminder_recipient(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT value FROM app_settings WHERE key = 'reminder_recipient'"
    ).fetchone()
    return row["value"] if row else ""


def update_task(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    title: str,
    description: str,
    due_at: str | None,
    reminder_preference: str,
    priority: str | None = None,
    project_id: str | None = None,
    milestone_id: str | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    cursor = connection.execute(
        """UPDATE application_tasks
           SET title = ?, description = ?, due_at = ?, reminder_preference = ?,
               priority = COALESCE(?, priority), project_id = COALESCE(?, project_id),
               milestone_id = COALESCE(?, milestone_id), updated_at = ?,
               confirmation_status = 'confirmed', confirmed_at = COALESCE(confirmed_at, ?)
           WHERE id = ? AND status != 'done' AND deleted_at IS NULL AND archived_at IS NULL""",
        (
            title, description, due_at, reminder_preference, priority, project_id,
            milestone_id, now, now, task_id,
        ),
    )
    if not cursor.rowcount:
        return None
    connection.commit()
    task = get_task(connection, task_id)
    if task and task["project_id"]:
        touch_project(connection, task["project_id"])
    return task


def complete_task(connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    now = utc_now()
    cursor = connection.execute(
        """UPDATE application_tasks SET status = 'done', completed_at = ?, updated_at = ?,
                  confirmation_status = 'confirmed', confirmed_at = COALESCE(confirmed_at, ?)
           WHERE id = ? AND status != 'done' AND deleted_at IS NULL""",
        (now, now, now, task_id),
    )
    if not cursor.rowcount:
        return None
    connection.execute(
        "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status IN ('pending', 'sending')",
        (task_id,),
    )
    connection.commit()
    task = get_task(connection, task_id)
    if task and task["project_id"]:
        touch_project(connection, task["project_id"])
    return task


def delete_task(connection: sqlite3.Connection, task_id: str) -> bool:
    now = utc_now()
    cursor = connection.execute(
        """UPDATE application_tasks SET deleted_at = ?, updated_at = ?
           WHERE id = ? AND deleted_at IS NULL""",
        (now, now, task_id),
    )
    if cursor.rowcount:
        connection.execute(
            "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status IN ('pending', 'sending')",
            (task_id,),
        )
        task = connection.execute("SELECT project_id FROM application_tasks WHERE id = ?", (task_id,)).fetchone()
        if task and task["project_id"]:
            connection.execute(
                "UPDATE projects SET updated_at = ?, last_activity_at = ? WHERE id = ?",
                (now, now, task["project_id"]),
            )
    connection.commit()
    return bool(cursor.rowcount)


def schedule_task_reminder(
    connection: sqlite3.Connection,
    task_id: str,
    scheduled_at: str | None,
    preference: str,
) -> None:
    connection.execute(
        "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status IN ('pending', 'sending')",
        (task_id,),
    )
    if scheduled_at is not None and preference != "none":
        connection.execute(
            """INSERT INTO task_reminders
               (id, task_id, scheduled_at, reminder_preference, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (str(uuid4()), task_id, scheduled_at, preference, utc_now()),
        )
    connection.commit()


def create_memory_proposal(
    connection: sqlite3.Connection,
    content: str,
    content_hash: str,
    provenance: str,
    conversation_id: str | None = None,
    category: str | None = "context",
    project_id: str | None = None,
    title: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    approved = connection.execute(
        "SELECT id FROM approved_memories WHERE content_hash = ? AND archived_at IS NULL",
        (content_hash,),
    ).fetchone()
    if approved:
        return {"id": approved["id"], "status": "already_approved", "duplicate": True}
    existing = connection.execute(
        """SELECT id, status FROM memory_proposals WHERE content_hash = ?
           AND status = 'pending' AND COALESCE(project_id, '') = COALESCE(?, '')
           ORDER BY created_at DESC LIMIT 1""",
        (content_hash, project_id),
    ).fetchone()
    if existing:
        return {"id": existing["id"], "status": existing["status"], "duplicate": True}
    memory_id = str(uuid4())
    now = utc_now()
    connection.execute(
        """INSERT INTO memory_proposals
           (id, content, content_hash, provenance, source_conversation_id, status, created_at,
            category, project_id, title, reason)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
        (memory_id, content, content_hash, provenance, conversation_id, now, category, project_id,
         (title or content[:80]).strip(), reason.strip()),
    )
    connection.commit()
    return {"id": memory_id, "status": "pending", "duplicate": False, "created_at": now}


def list_memory_proposals(
    connection: sqlite3.Connection,
    query: str | None = None,
    category: str | None = None,
    project_id: str | None = None,
    status: str | None = "pending",
) -> list[dict[str, Any]]:
    conditions = []
    values: list[str] = []
    if status:
        conditions.append("status = ?")
        values.append(status)
    if category:
        conditions.append("category = ?")
        values.append(category)
    if project_id:
        conditions.append("project_id = ?")
        values.append(project_id)
    if query:
        conditions.append("(content LIKE ? OR title LIKE ?)")
        values.extend((f"%{query}%", f"%{query}%"))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = connection.execute(
        f"""SELECT id, content, title, reason, provenance, source_conversation_id, status,
                   created_at, category, project_id, importance, confidence
            FROM memory_proposals {where} ORDER BY created_at DESC LIMIT 200""",
        values,
    ).fetchall()
    return [dict(row) for row in rows]


def review_memory_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    approve: bool,
) -> dict[str, Any] | None:
    proposal = connection.execute(
        """SELECT id, content, content_hash, provenance, source_conversation_id, category,
                  project_id, title, importance, confidence
           FROM memory_proposals WHERE id = ? AND status = 'pending'""",
        (proposal_id,),
    ).fetchone()
    if proposal is None:
        return None
    reviewed_at = utc_now()
    state = "approved" if approve else "rejected"
    connection.execute(
        "UPDATE memory_proposals SET status = ?, reviewed_at = ? WHERE id = ?",
        (state, reviewed_at, proposal_id),
    )
    if approve:
        connection.execute(
            """INSERT OR IGNORE INTO approved_memories
               (id, content, content_hash, provenance, source_conversation_id, created_at, updated_at,
                category, project_id, title, importance, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid4()),
                proposal["content"],
                proposal["content_hash"],
                proposal["provenance"],
                proposal["source_conversation_id"],
                reviewed_at,
                reviewed_at,
                proposal["category"],
                proposal["project_id"],
                proposal["title"],
                proposal["importance"],
                proposal["confidence"],
            ),
        )
    connection.commit()
    return {"id": proposal_id, "status": state, "reviewed_at": reviewed_at}


def list_approved_memories(
    connection: sqlite3.Connection,
    query: str | None = None,
    category: str | None = None,
    project_id: str | None = None,
    include_archived: bool = False,
    global_only: bool = False,
) -> list[dict[str, Any]]:
    conditions = []
    values: list[str] = []
    if not include_archived:
        conditions.append("archived_at IS NULL")
    if category:
        conditions.append("category = ?")
        values.append(category)
    if project_id:
        conditions.append("project_id = ?")
        values.append(project_id)
    if global_only:
        conditions.append("project_id IS NULL")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = connection.execute(
        f"""SELECT id, content, title, provenance, source_conversation_id, created_at, updated_at,
                   category, project_id, importance, confidence, expires_at, archived_at
            FROM approved_memories {where} ORDER BY created_at DESC LIMIT 500""",
        values,
    ).fetchall()
    memories = [dict(row) for row in rows]
    if not query:
        return memories
    terms = {term for term in re.findall(r"[\w'-]+", query.casefold()) if len(term) > 1}
    if not terms:
        return []
    ranked = []
    for memory in memories:
        folded = memory["content"].casefold()
        score = sum(folded.count(term) for term in terms)
        if score:
            ranked.append((score, memory))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [memory for _, memory in ranked[:100]]


def update_approved_memory(
    connection: sqlite3.Connection,
    memory_id: str,
    content: str,
    content_hash: str,
    *,
    category: str | None = None,
    project_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    cursor = connection.execute(
        """UPDATE approved_memories SET content = ?, content_hash = ?, category = COALESCE(?, category),
                  project_id = ?, title = COALESCE(?, title), updated_at = ?
           WHERE id = ?""",
        (content, content_hash, category, project_id, title, now, memory_id),
    )
    if not cursor.rowcount:
        return None
    connection.commit()
    row = connection.execute(
        """SELECT id, content, title, provenance, source_conversation_id, created_at, updated_at,
                  category, project_id, importance, confidence, expires_at, archived_at
           FROM approved_memories WHERE id = ?""",
        (memory_id,),
    ).fetchone()
    return dict(row) if row else None


def delete_approved_memory(connection: sqlite3.Connection, memory_id: str) -> bool:
    cursor = connection.execute("DELETE FROM approved_memories WHERE id = ?", (memory_id,))
    connection.commit()
    return bool(cursor.rowcount)


def archive_approved_memory(connection: sqlite3.Connection, memory_id: str) -> bool:
    cursor = connection.execute(
        "UPDATE approved_memories SET archived_at = ?, updated_at = ? WHERE id = ? AND archived_at IS NULL",
        (utc_now(), utc_now(), memory_id),
    )
    connection.commit()
    return bool(cursor.rowcount)


def clear_approved_memories(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("DELETE FROM approved_memories")
    connection.commit()
    return cursor.rowcount


def create_action_approval(
    connection: sqlite3.Connection,
    operation: str,
    target: str,
    summary: str,
    inputs: dict[str, str],
    conversation_id: str | None = None,
) -> dict[str, Any]:
    approval = {
        "id": str(uuid4()),
        "operation": operation,
        "target": target,
        "summary": summary,
        "inputs": inputs,
        "status": "pending",
        "conversation_id": conversation_id,
        "created_at": utc_now(),
    }
    connection.execute(
        """INSERT INTO action_approvals
           (id, operation, target, summary, inputs_json, status, conversation_id, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (
            approval["id"],
            operation,
            target,
            summary,
            json.dumps(inputs),
            conversation_id,
            approval["created_at"],
        ),
    )
    connection.commit()
    return approval


def list_action_approvals(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, operation, target, summary, inputs_json, status, conversation_id, created_at
           FROM action_approvals WHERE status = 'pending' ORDER BY created_at DESC LIMIT 100"""
    ).fetchall()
    return [
        {**{key: row[key] for key in ("id", "operation", "target", "summary", "status", "conversation_id", "created_at")},
         "inputs": json.loads(row["inputs_json"])}
        for row in rows
    ]


def cancel_action_approval(connection: sqlite3.Connection, approval_id: str) -> bool:
    cursor = connection.execute(
        "UPDATE action_approvals SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
        (approval_id,),
    )
    connection.commit()
    return bool(cursor.rowcount)


def record_agent_execution(
    connection: sqlite3.Connection,
    conversation_id: str,
    execution: dict[str, Any],
) -> None:
    connection.execute(
        """INSERT INTO agent_executions
           (id, conversation_id, agent_id, reason, status, tools_json, sources_json, created_at, project_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            execution["id"],
            conversation_id,
            execution["agent_id"],
            execution["reason"],
            execution["status"],
            json.dumps(execution["tools"]),
            json.dumps(execution["sources"]),
            utc_now(),
            execution.get("project_id"),
        ),
    )
    connection.commit()


def list_agent_executions(
    connection: sqlite3.Connection,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, conversation_id, agent_id, reason, status, tools_json, sources_json, created_at, project_id
           FROM agent_executions ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "agent_id": row["agent_id"],
            "reason": row["reason"],
            "status": row["status"],
            "tools": json.loads(row["tools_json"]),
            "sources": json.loads(row["sources_json"]),
            "created_at": row["created_at"],
            "project_id": row["project_id"],
        }
        for row in rows
    ]


def list_messages_after(
    connection: sqlite3.Connection,
    conversation_id: str,
    after_rowid: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Returns messages newer than a checkpoint, using SQLite insertion order."""
    rows = connection.execute(
        """SELECT rowid AS rowid, id, conversation_id, role, content, created_at, provider, model
           FROM messages WHERE conversation_id = ? AND rowid > ? ORDER BY rowid LIMIT ?""",
        (conversation_id, after_rowid, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_intelligence_checkpoint(
    connection: sqlite3.Connection,
    conversation_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT conversation_id, last_message_rowid, last_message_id, last_message_created_at,
                  messages_processed, updated_at
           FROM intelligence_checkpoints WHERE conversation_id = ?""",
        (conversation_id,),
    ).fetchone()
    return dict(row) if row else None


def save_intelligence_checkpoint(
    connection: sqlite3.Connection,
    conversation_id: str,
    *,
    last_message_rowid: int,
    last_message_id: str | None,
    last_message_created_at: str | None,
    processed_increment: int = 0,
) -> None:
    connection.execute(
        """INSERT INTO intelligence_checkpoints
           (conversation_id, last_message_rowid, last_message_id, last_message_created_at,
            messages_processed, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(conversation_id) DO UPDATE SET
               last_message_rowid = MAX(intelligence_checkpoints.last_message_rowid, excluded.last_message_rowid),
               last_message_id = excluded.last_message_id,
               last_message_created_at = excluded.last_message_created_at,
               messages_processed = intelligence_checkpoints.messages_processed + excluded.messages_processed,
               updated_at = excluded.updated_at""",
        (
            conversation_id,
            last_message_rowid,
            last_message_id,
            last_message_created_at,
            processed_increment,
            utc_now(),
        ),
    )
    connection.commit()


def reset_intelligence_checkpoint(connection: sqlite3.Connection, conversation_id: str) -> None:
    """Explicitly rewinds one conversation so it can be reprocessed on request."""
    connection.execute(
        """INSERT INTO intelligence_checkpoints
           (conversation_id, last_message_rowid, messages_processed, updated_at)
           VALUES (?, 0, 0, ?)
           ON CONFLICT(conversation_id) DO UPDATE SET
               last_message_rowid = 0,
               last_message_id = NULL,
               last_message_created_at = NULL,
               updated_at = excluded.updated_at""",
        (conversation_id, utc_now()),
    )
    connection.commit()


def list_conversations_with_unprocessed_messages(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT m.conversation_id AS conversation_id, MIN(m.rowid) AS next_rowid
           FROM messages m
           LEFT JOIN intelligence_checkpoints c ON c.conversation_id = m.conversation_id
           WHERE m.rowid > COALESCE(c.last_message_rowid, 0)
           GROUP BY m.conversation_id ORDER BY next_rowid"""
    ).fetchall()
    return [dict(row) for row in rows]


def count_unprocessed_messages(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """SELECT COUNT(*) AS count FROM messages m
           LEFT JOIN intelligence_checkpoints c ON c.conversation_id = m.conversation_id
           WHERE m.rowid > COALESCE(c.last_message_rowid, 0)"""
    ).fetchone()
    return int(row["count"]) if row else 0


def record_intelligence_run(connection: sqlite3.Connection, run: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": run["id"],
        "started_at": run["started_at"],
        "finished_at": run.get("finished_at") or utc_now(),
        "mode": run.get("mode", "incremental"),
        "status": run.get("status", "succeeded"),
        "conversations_scanned": int(run.get("conversations_scanned", 0)),
        "messages_scanned": int(run.get("messages_scanned", 0)),
        "messages_skipped": int(run.get("messages_skipped", 0)),
        "tasks_detected": int(run.get("tasks_detected", 0)),
        "duplicates_skipped": int(run.get("duplicates_skipped", 0)),
        "proposals_created": int(run.get("proposals_created", 0)),
        "model_used": int(bool(run.get("model_used"))),
        "error": run.get("error"),
    }
    connection.execute(
        """INSERT INTO intelligence_runs
           (id, started_at, finished_at, mode, status, conversations_scanned, messages_scanned,
            messages_skipped, tasks_detected, duplicates_skipped, proposals_created, model_used, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        tuple(payload.values()),
    )
    connection.commit()
    return payload


def list_intelligence_runs(connection: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, started_at, finished_at, mode, status, conversations_scanned, messages_scanned,
                  messages_skipped, tasks_detected, duplicates_skipped, proposals_created, model_used, error
           FROM intelligence_runs ORDER BY started_at DESC, rowid DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{**dict(row), "model_used": bool(row["model_used"])} for row in rows]


PROPOSAL_COLUMNS = (
    "p.id, p.kind, p.status, p.confidence, p.title, p.detail, p.project_id, "
    "p.candidate_project_name, p.workspace_hint, p.task_id, p.due_at, p.due_phrase, p.priority, "
    "p.progress_value, p.milestone_target_date, p.conversation_id, p.message_id, "
    "p.message_created_at, p.excerpt, p.evidence_json, p.fingerprint, p.created_at, "
    "p.reviewed_at, p.review_note, p.result_id"
)


def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
    proposal = dict(row)
    proposal["evidence"] = json.loads(proposal.pop("evidence_json") or "[]")
    proposal["project_name"] = proposal.pop("project_name", None)
    return proposal


def create_intelligence_proposal(
    connection: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    fingerprint: str,
    detail: str = "",
    confidence: float = 0.5,
    project_id: str | None = None,
    candidate_project_name: str | None = None,
    workspace_hint: str | None = None,
    task_id: str | None = None,
    due_at: str | None = None,
    due_phrase: str | None = None,
    priority: str | None = None,
    progress_value: int | None = None,
    milestone_target_date: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    message_created_at: str | None = None,
    excerpt: str = "",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    proposal_id = str(uuid4())
    cursor = connection.execute(
        """INSERT OR IGNORE INTO intelligence_proposals
           (id, kind, status, confidence, title, detail, project_id, candidate_project_name,
            workspace_hint, task_id, due_at, due_phrase, priority, progress_value,
            milestone_target_date, conversation_id, message_id, message_created_at, excerpt,
            evidence_json, fingerprint, created_at)
           VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            proposal_id,
            kind,
            float(confidence),
            title.strip()[:200],
            detail.strip()[:2000],
            project_id,
            candidate_project_name,
            workspace_hint,
            task_id,
            due_at,
            due_phrase,
            priority,
            progress_value,
            milestone_target_date,
            conversation_id,
            message_id,
            message_created_at,
            excerpt[:1000],
            json.dumps(evidence or []),
            fingerprint,
            now,
        ),
    )
    connection.commit()
    if not cursor.rowcount:
        return None
    return get_intelligence_proposal(connection, proposal_id)


def get_intelligence_proposal(connection: sqlite3.Connection, proposal_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        f"""SELECT {PROPOSAL_COLUMNS}, pr.name AS project_name
            FROM intelligence_proposals p LEFT JOIN projects pr ON pr.id = p.project_id
            WHERE p.id = ?""",
        (proposal_id,),
    ).fetchone()
    return _proposal_dict(row) if row else None


def list_intelligence_proposals(
    connection: sqlite3.Connection,
    kind: str | None = None,
    status: str | None = "pending",
    project_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    conditions = []
    values: list[Any] = []
    if kind:
        conditions.append("p.kind = ?")
        values.append(kind)
    if status:
        conditions.append("p.status = ?")
        values.append(status)
    if project_id:
        conditions.append("p.project_id = ?")
        values.append(project_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    values.append(limit)
    rows = connection.execute(
        f"""SELECT {PROPOSAL_COLUMNS}, pr.name AS project_name
            FROM intelligence_proposals p LEFT JOIN projects pr ON pr.id = p.project_id
            {where} ORDER BY p.created_at DESC, p.rowid DESC LIMIT ?""",
        values,
    ).fetchall()
    return [_proposal_dict(row) for row in rows]


def mark_intelligence_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    status: str,
    result_id: str | None = None,
    review_note: str = "",
) -> dict[str, Any] | None:
    if status not in {"accepted", "rejected"}:
        raise ValueError("Unsupported proposal review state.")
    now = utc_now()
    cursor = connection.execute(
        """UPDATE intelligence_proposals SET status = ?, reviewed_at = ?, result_id = ?,
                  review_note = ? WHERE id = ? AND status = 'pending'""",
        (status, now, result_id, review_note.strip()[:500], proposal_id),
    )
    connection.commit()
    if not cursor.rowcount:
        return None
    return get_intelligence_proposal(connection, proposal_id)


def list_all_project_aliases(connection: sqlite3.Connection) -> dict[str, list[str]]:
    rows = connection.execute("SELECT project_id, alias FROM project_aliases ORDER BY created_at").fetchall()
    aliases: dict[str, list[str]] = {}
    for row in rows:
        aliases.setdefault(row["project_id"], []).append(row["alias"])
    return aliases


def list_project_aliases(connection: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, project_id, alias, source, created_at FROM project_aliases WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_project_alias_owner(connection: sqlite3.Connection, alias: str) -> str | None:
    row = connection.execute(
        "SELECT project_id FROM project_aliases WHERE alias = ?",
        (alias,),
    ).fetchone()
    return row["project_id"] if row else None


def add_project_alias(
    connection: sqlite3.Connection,
    project_id: str,
    alias: str,
    source: str = "manual",
) -> dict[str, Any] | None:
    normalized = " ".join(alias.casefold().split())
    if len(normalized) < 2 or len(normalized) > 80:
        return None
    owner = get_project_alias_owner(connection, normalized)
    if owner is not None and owner != project_id:
        return None
    alias_id = str(uuid4())
    cursor = connection.execute(
        """INSERT OR IGNORE INTO project_aliases (id, project_id, alias, source, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (alias_id, project_id, normalized, source, utc_now()),
    )
    connection.commit()
    if not cursor.rowcount:
        row = connection.execute(
            "SELECT id, project_id, alias, source, created_at FROM project_aliases WHERE alias = ?",
            (normalized,),
        ).fetchone()
        return dict(row) if row else None
    row = connection.execute(
        "SELECT id, project_id, alias, source, created_at FROM project_aliases WHERE id = ?",
        (alias_id,),
    ).fetchone()
    return dict(row) if row else None


def delete_project_alias(connection: sqlite3.Connection, project_id: str, alias_id: str) -> bool:
    cursor = connection.execute(
        "DELETE FROM project_aliases WHERE id = ? AND project_id = ?",
        (alias_id, project_id),
    )
    connection.commit()
    return bool(cursor.rowcount)


def confirm_task(
    connection: sqlite3.Connection,
    task_id: str,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    if project_id is not None:
        cursor = connection.execute(
            """UPDATE application_tasks SET confirmation_status = 'confirmed', confirmed_at = ?,
                      project_id = ?, updated_at = ?
               WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL""",
            (now, project_id, now, task_id),
        )
    else:
        cursor = connection.execute(
            """UPDATE application_tasks SET confirmation_status = 'confirmed', confirmed_at = ?,
                      updated_at = ?
               WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL""",
            (now, now, task_id),
        )
    if not cursor.rowcount:
        return None
    connection.commit()
    task = get_task(connection, task_id)
    if task and task["project_id"]:
        touch_project(connection, task["project_id"])
    return task


def archive_task(connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    now = utc_now()
    cursor = connection.execute(
        """UPDATE application_tasks SET archived_at = ?, updated_at = ?
           WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL""",
        (now, now, task_id),
    )
    if not cursor.rowcount:
        return None
    connection.execute(
        "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status IN ('pending', 'sending')",
        (task_id,),
    )
    connection.commit()
    return get_task(connection, task_id)


def set_task_completion_evidence(
    connection: sqlite3.Connection,
    task_id: str,
    evidence: str,
) -> None:
    connection.execute(
        "UPDATE application_tasks SET completion_evidence = ?, updated_at = ? WHERE id = ?",
        (evidence.strip()[:1000], utc_now(), task_id),
    )
    connection.commit()


TELEGRAM_STATE_COLUMNS = (
    "id, paired_chat_id, paired_user_id, paired_username, paired_at, owner_username, "
    "conversation_id, last_update_id, bot_username, last_poll_at, last_error, last_error_at, "
    "poll_owner, poll_heartbeat_at, messages_received, messages_sent, updated_at"
)


def get_telegram_state(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        f"SELECT {TELEGRAM_STATE_COLUMNS} FROM telegram_state WHERE id = 1"
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT OR IGNORE INTO telegram_state (id, updated_at) VALUES (1, ?)", (utc_now(),)
        )
        connection.commit()
        row = connection.execute(
            f"SELECT {TELEGRAM_STATE_COLUMNS} FROM telegram_state WHERE id = 1"
        ).fetchone()
    return dict(row)


def update_telegram_state(connection: sqlite3.Connection, fields: dict[str, Any]) -> None:
    allowed = set(TELEGRAM_STATE_COLUMNS.split(", ")) - {"id"}
    if not fields or set(fields) - allowed:
        raise ValueError("The Telegram state update contains unsupported fields.")
    assignments = ", ".join(f"{key} = ?" for key in fields)
    connection.execute(
        f"UPDATE telegram_state SET {assignments}, updated_at = ? WHERE id = 1",
        (*fields.values(), utc_now()),
    )
    connection.commit()


def clear_telegram_pairing(connection: sqlite3.Connection) -> None:
    connection.execute(
        """UPDATE telegram_state SET paired_chat_id = NULL, paired_user_id = NULL,
                  paired_username = NULL, paired_at = NULL, conversation_id = NULL,
                  updated_at = ? WHERE id = 1""",
        (utc_now(),),
    )
    connection.commit()


def invalidate_telegram_pairing_codes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DELETE FROM telegram_pairing_codes WHERE used_at IS NULL"
    )
    connection.commit()


def create_telegram_pairing_code(
    connection: sqlite3.Connection,
    *,
    code_hash: str,
    owner_username: str,
    expires_at: str,
) -> dict[str, Any]:
    connection.execute("DELETE FROM telegram_pairing_codes WHERE used_at IS NULL")
    code_id = str(uuid4())
    now = utc_now()
    connection.execute(
        """INSERT INTO telegram_pairing_codes
           (id, code_hash, owner_username, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        (code_id, code_hash, owner_username, now, expires_at),
    )
    connection.execute(
        "DELETE FROM telegram_pairing_codes WHERE used_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    connection.commit()
    return {"id": code_id, "owner_username": owner_username, "created_at": now, "expires_at": expires_at}


def find_telegram_pairing_code(connection: sqlite3.Connection, code_hash: str) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT id, code_hash, owner_username, created_at, expires_at, used_at
           FROM telegram_pairing_codes WHERE code_hash = ?""",
        (code_hash,),
    ).fetchone()
    return dict(row) if row else None


def mark_telegram_pairing_code_used(connection: sqlite3.Connection, code_id: str) -> bool:
    cursor = connection.execute(
        "UPDATE telegram_pairing_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
        (utc_now(), code_id),
    )
    connection.commit()
    return bool(cursor.rowcount)


def count_active_telegram_pairing_codes(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM telegram_pairing_codes WHERE used_at IS NULL AND expires_at > ?",
        (utc_now(),),
    ).fetchone()
    return int(row["count"]) if row else 0


def record_telegram_update(connection: sqlite3.Connection, update_id: int, kind: str) -> bool:
    cursor = connection.execute(
        "INSERT OR IGNORE INTO telegram_updates (update_id, kind, processed_at) VALUES (?, ?, ?)",
        (update_id, kind, utc_now()),
    )
    connection.commit()
    return bool(cursor.rowcount)


def record_telegram_message(
    connection: sqlite3.Connection,
    *,
    chat_id: str,
    telegram_message_id: str,
    direction: str,
    kind: str,
    status: str,
    update_id: int | None = None,
    is_forwarded: bool = False,
    text_length: int = 0,
    sent_at: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> bool:
    """Stores Telegram metadata only, never the message body. Returns False on a duplicate."""
    cursor = connection.execute(
        """INSERT OR IGNORE INTO telegram_messages
           (id, update_id, chat_id, telegram_message_id, direction, kind, is_forwarded,
            text_length, sent_at, conversation_id, message_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid4()),
            update_id,
            str(chat_id),
            str(telegram_message_id),
            direction,
            kind,
            int(bool(is_forwarded)),
            int(text_length),
            sent_at,
            conversation_id,
            message_id,
            status,
            utc_now(),
        ),
    )
    connection.commit()
    return bool(cursor.rowcount)


def telegram_message_seen(
    connection: sqlite3.Connection,
    chat_id: str,
    telegram_message_id: str,
    direction: str,
) -> bool:
    row = connection.execute(
        """SELECT 1 FROM telegram_messages
           WHERE chat_id = ? AND telegram_message_id = ? AND direction = ?""",
        (str(chat_id), str(telegram_message_id), direction),
    ).fetchone()
    return row is not None


def count_telegram_outbound_since(
    connection: sqlite3.Connection,
    chat_id: str,
    since: str,
) -> int:
    row = connection.execute(
        """SELECT COUNT(*) AS count FROM telegram_messages
           WHERE chat_id = ? AND direction = 'outbound' AND created_at >= ?""",
        (str(chat_id), since),
    ).fetchone()
    return int(row["count"]) if row else 0


def claim_telegram_polling_lease(
    connection: sqlite3.Connection,
    owner_id: str,
    *,
    stale_seconds: int = 60,
) -> bool:
    state = get_telegram_state(connection)
    now = utc_now()
    heartbeat = parse_timestamp(state["poll_heartbeat_at"])
    reference = datetime.now(timezone.utc)
    free = (
        state["poll_owner"] is None
        or state["poll_owner"] == owner_id
        or heartbeat is None
        or (reference - heartbeat).total_seconds() > stale_seconds
    )
    if not free:
        return False
    update_telegram_state(
        connection,
        {"poll_owner": owner_id, "poll_heartbeat_at": now},
    )
    return True


def release_telegram_polling_lease(connection: sqlite3.Connection, owner_id: str) -> None:
    connection.execute(
        """UPDATE telegram_state SET poll_owner = NULL, poll_heartbeat_at = NULL, updated_at = ?
           WHERE id = 1 AND poll_owner = ?""",
        (utc_now(), owner_id),
    )
    connection.commit()


def set_telegram_offset(connection: sqlite3.Connection, update_id: int) -> None:
    connection.execute(
        "UPDATE telegram_state SET last_update_id = MAX(last_update_id, ?), updated_at = ? WHERE id = 1",
        (int(update_id), utc_now()),
    )
    connection.commit()


def note_telegram_error(connection: sqlite3.Connection, message: str) -> None:
    connection.execute(
        "UPDATE telegram_state SET last_error = ?, last_error_at = ?, updated_at = ? WHERE id = 1",
        (message.strip()[:300], utc_now(), utc_now()),
    )
    connection.commit()
