"""SQLite schema for the chat, memory, and knowledge runtime."""

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
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    question_created_at TEXT NOT NULL,
    answer_created_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    memory_processed INTEGER NOT NULL DEFAULT 0,
    turn_meta_json TEXT NOT NULL DEFAULT '{}',
    primary_topic TEXT NOT NULL DEFAULT '',
    primary_topic_embedding_json TEXT NOT NULL DEFAULT '[]',
    question_embedding_json TEXT NOT NULL DEFAULT '[]',
    answer_embedding_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_user_created
ON conversation_turns(user_id, created_at DESC);

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
    tags_json TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'fact'
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_user_kind
ON memory_facts(user_id, memory_kind);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NULL,
    UNIQUE(user_id, source_type, source_ref, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user_source
ON memory_embeddings(user_id, source_type);

CREATE TABLE IF NOT EXISTS knowledge_upload_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_mime_type TEXT NULL,
    file_size INTEGER NULL,
    file_id TEXT NULL,
    downloaded_file_path TEXT NULL,
    downloaded_file_url TEXT NULL,
    status TEXT NOT NULL DEFAULT 'pending_scope',
    created_at TEXT NOT NULL,
    resolved_at TEXT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_knowledge_upload_sessions_status_created_at
ON knowledge_upload_sessions(status, created_at);
"""

CURRENT_SCHEMA_VERSION = 28


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self.session() as connection:
            connection.executescript(SCHEMA)
            connection.execute("DELETE FROM schema_version")
            connection.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
                (CURRENT_SCHEMA_VERSION,),
            )
            connection.commit()

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
