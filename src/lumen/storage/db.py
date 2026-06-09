from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


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
"""

LEGACY_TABLES = (
    "action_traces",
    "knowledge_documents",
    "knowledge_chunks",
    "ingestion_runs",
    "conversation_logs",
)

CURRENT_SCHEMA_VERSION = 6


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
