from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from lumen.time_utils import LOCAL_TIMEZONE


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    question_created_at TEXT NOT NULL,
    answer_created_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    memory_processed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL,
    importance INTEGER NOT NULL,
    source_ref TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    expires_at TEXT NULL,
    tags_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    role TEXT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    embedding_json TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 5,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(source_type, source_ref, chunk_index)
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    conversation_id TEXT NULL,
    source_ref TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    sent_at TEXT NULL,
    repeat_interval_seconds INTEGER NULL,
    repeat_until TEXT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reminders_status_due_at
ON reminders(status, due_at);
"""

LEGACY_TABLES = (
    "action_traces",
    "knowledge_documents",
    "knowledge_chunks",
    "ingestion_runs",
    "conversation_logs",
)

CURRENT_SCHEMA_VERSION = 9


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        sqlite_path = Path(db_path)
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self.session() as connection:
            connection.executescript(SCHEMA)
            self._migrate_conversation_turns_without_session_id(connection)
            self._migrate_long_term_memory_user_id(connection)
            self._migrate_reminders_repeat_columns(connection)
            self._migrate_timestamps_to_local_time(connection)
            for table_name in LEGACY_TABLES:
                connection.execute(f"DROP TABLE IF EXISTS {table_name}")

            cursor = connection.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = cursor.fetchone()
            if row is None or int(row["version"]) < CURRENT_SCHEMA_VERSION:
                connection.execute("DELETE FROM schema_version")
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
                    (CURRENT_SCHEMA_VERSION,),
                )
            connection.commit()

    def _migrate_conversation_turns_without_session_id(self, connection) -> None:
        columns = connection.execute("PRAGMA table_info(conversation_turns)").fetchall()
        if not columns:
            return
        column_names = {str(row["name"]) for row in columns}
        if "session_id" not in column_names:
            return
        connection.execute("ALTER TABLE conversation_turns RENAME TO conversation_turns_legacy")
        connection.execute(
            """
            CREATE TABLE conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                source TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                question_created_at TEXT NOT NULL,
                answer_created_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                memory_processed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, conversation_id, user_id, source, question, answer,
                question_created_at, answer_created_at, created_at, memory_processed
            )
            SELECT
                id, conversation_id, user_id, source, question, answer,
                question_created_at, answer_created_at, created_at, memory_processed
            FROM conversation_turns_legacy
            """
        )
        connection.execute("DROP TABLE conversation_turns_legacy")

    def _migrate_long_term_memory_user_id(self, connection) -> None:
        for table_name in ("memory_facts", "memory_embeddings"):
            columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            if not columns:
                continue
            column_names = {str(row["name"]) for row in columns}
            if "user_id" in column_names:
                continue
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN user_id TEXT NOT NULL DEFAULT 'lumen-user'")

    def _migrate_reminders_repeat_columns(self, connection) -> None:
        columns = connection.execute("PRAGMA table_info(reminders)").fetchall()
        if not columns:
            return
        column_names = {str(row["name"]) for row in columns}
        if "repeat_interval_seconds" not in column_names:
            connection.execute("ALTER TABLE reminders ADD COLUMN repeat_interval_seconds INTEGER NULL")
        if "repeat_until" not in column_names:
            connection.execute("ALTER TABLE reminders ADD COLUMN repeat_until TEXT NULL")

    @staticmethod
    def _to_local_iso(value: str | None) -> str | None:
        if value is None:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return candidate
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TIMEZONE).isoformat()

    def _migrate_timestamps_to_local_time(self, connection) -> None:
        tables = {
            "conversation_turns": ["question_created_at", "answer_created_at", "created_at"],
            "memory_facts": ["last_seen", "expires_at"],
            "memory_embeddings": ["created_at", "updated_at", "last_seen"],
            "reminders": ["due_at", "created_at", "sent_at", "repeat_until"],
        }
        for table_name, columns in tables.items():
            existing_columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            if not existing_columns:
                continue
            column_names = {str(row["name"]) for row in existing_columns}
            if not any(column in column_names for column in columns):
                continue
            rows = connection.execute(
                f"SELECT id, {', '.join(column for column in columns if column in column_names)} FROM {table_name}"
            ).fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column in columns:
                    if column not in column_names:
                        continue
                    converted = self._to_local_iso(row[column])
                    if converted is not None and converted != row[column]:
                        updates[column] = converted
                if updates:
                    assignments = ", ".join(f"{column} = ?" for column in updates)
                    params = list(updates.values()) + [row["id"]]
                    connection.execute(
                        f"UPDATE {table_name} SET {assignments} WHERE id = ?",
                        params,
                    )

    @contextmanager
    def session(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def ping(self) -> bool:
        try:
            with self.session() as connection:
                connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            return True
        except Exception:
            return False
