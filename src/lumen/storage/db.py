from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,
    role TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE TABLE IF NOT EXISTS action_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    ha_domain TEXT NOT NULL,
    ha_service TEXT NOT NULL,
    service_data_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL,
    result_message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT NULL,
    executed_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    extra_metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    search_text TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    status TEXT NOT NULL,
    documents_indexed INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        sqlite_path = Path(db_path)
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self.session() as connection:
            connection.executescript(SCHEMA)
            cursor = connection.execute("SELECT version FROM schema_version WHERE version = 1")
            if cursor.fetchone() is None:
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
                    (1,),
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
