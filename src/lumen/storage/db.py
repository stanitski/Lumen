"""SQLite schema for the chat, memory, and knowledge runtime."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from lumen.timezones import kyiv_timezone


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

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    recurrence_kind TEXT NULL,
    recurrence_weekday INTEGER NULL,
    repeat_until TEXT NULL,
    delivery_id TEXT NULL,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_lease_until TEXT NULL,
    next_attempt_at TEXT NULL,
    confirmation_deadline_at TEXT NULL,
    delivery_route_json TEXT NULL,
    parent_reminder_id INTEGER NULL REFERENCES reminders(id),
    source_ref TEXT NULL,
    legacy_source_id INTEGER NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_due
ON reminders(status, due_at);

CREATE INDEX IF NOT EXISTS idx_reminders_user_confirmation
ON reminders(user_id, status, confirmation_deadline_at);

CREATE TABLE IF NOT EXISTS reminder_management_sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    delivery_route_json TEXT NULL,
    operation TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    patch_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminder_management_sessions_expires_at
ON reminder_management_sessions(expires_at);

"""

CURRENT_SCHEMA_VERSION = 34


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self.session() as connection:
            self._preserve_incompatible_legacy_reminders(connection)
            connection.executescript(SCHEMA)
            self._add_reminder_delivery_route_column(connection)
            self._add_reminder_parent_column(connection)
            self._add_reminder_source_ref_column(connection)
            self._add_reminder_next_attempt_column(connection)
            self._migrate_legacy_reminders(connection)
            connection.execute("DELETE FROM schema_version")
            connection.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
                (CURRENT_SCHEMA_VERSION,),
            )
            connection.commit()

    @staticmethod
    def _add_reminder_delivery_route_column(connection: sqlite3.Connection) -> None:
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(reminders)").fetchall()}
        if "delivery_route_json" not in columns:
            connection.execute("ALTER TABLE reminders ADD COLUMN delivery_route_json TEXT NULL")

    @staticmethod
    def _add_reminder_parent_column(connection: sqlite3.Connection) -> None:
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(reminders)").fetchall()}
        if "parent_reminder_id" not in columns:
            connection.execute("ALTER TABLE reminders ADD COLUMN parent_reminder_id INTEGER NULL REFERENCES reminders(id)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_parent_status "
            "ON reminders(parent_reminder_id, status)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_one_active_child "
            "ON reminders(parent_reminder_id) "
            "WHERE parent_reminder_id IS NOT NULL "
            "AND status IN ('pending', 'delivering', 'awaiting_confirmation')"
        )

    @staticmethod
    def _add_reminder_source_ref_column(connection: sqlite3.Connection) -> None:
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(reminders)").fetchall()}
        if "source_ref" not in columns:
            connection.execute("ALTER TABLE reminders ADD COLUMN source_ref TEXT NULL")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_user_source_ref "
            "ON reminders(user_id, source_ref) WHERE source_ref IS NOT NULL"
        )

    @staticmethod
    def _add_reminder_next_attempt_column(connection: sqlite3.Connection) -> None:
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(reminders)").fetchall()}
        if "next_attempt_at" not in columns:
            connection.execute("ALTER TABLE reminders ADD COLUMN next_attempt_at TEXT NULL")

    @staticmethod
    def _preserve_incompatible_legacy_reminders(connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminders'").fetchone()
        if row is None:
            return
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(reminders)").fetchall()}
        required = {
            "user_id", "text", "due_at", "timezone", "status", "recurrence_kind", "recurrence_weekday",
            "repeat_until", "delivery_id", "delivery_attempts", "delivery_lease_until", "confirmation_deadline_at",
            "legacy_source_id", "created_at", "updated_at",
        }
        if required.issubset(columns):
            return
        legacy_exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminders_legacy_pre_chat_only'").fetchone()
        if legacy_exists is not None:
            raise RuntimeError("Incompatible legacy reminders table is present; preserve it before initializing the new reminder service.")
        connection.execute("ALTER TABLE reminders RENAME TO reminders_legacy_pre_chat_only")

    @staticmethod
    def _migrate_legacy_reminders(connection: sqlite3.Connection) -> None:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminders_legacy_pre_chat_only'").fetchone()
        if exists is None:
            return
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(reminders_legacy_pre_chat_only)").fetchall()}
        previous_runtime = {
            "id", "user_id", "text", "due_at", "timezone", "status", "recurrence_kind", "recurrence_weekday",
            "repeat_until", "delivery_id", "delivery_attempts", "delivery_lease_until", "confirmation_deadline_at",
            "created_at", "updated_at",
        }
        if previous_runtime.issubset(columns):
            rows = connection.execute(
                "SELECT id, user_id, text, due_at, timezone, status, recurrence_kind, recurrence_weekday, repeat_until, delivery_id, delivery_attempts, delivery_lease_until, confirmation_deadline_at, created_at, updated_at FROM reminders_legacy_pre_chat_only"
            ).fetchall()
            for row in rows:
                due_at = Database._normalize_kyiv_iso(row["due_at"])
                repeat_until = Database._normalize_kyiv_iso(row["repeat_until"]) if row["repeat_until"] else None
                status = str(row["status"] or "failed") if due_at else "failed"
                connection.execute(
                    "INSERT OR IGNORE INTO reminders(user_id,text,due_at,timezone,status,recurrence_kind,recurrence_weekday,repeat_until,delivery_id,delivery_attempts,delivery_lease_until,confirmation_deadline_at,legacy_source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["user_id"], row["text"], due_at or "1970-01-01T00:00:00+02:00", "Europe/Kyiv", status, row["recurrence_kind"], row["recurrence_weekday"], repeat_until, row["delivery_id"], row["delivery_attempts"], row["delivery_lease_until"], row["confirmation_deadline_at"], int(row["id"]), row["created_at"], row["updated_at"]),
                )
            return
        required = {"id", "user_id", "text", "due_at", "status", "created_at", "repeat_interval_seconds", "repeat_until"}
        if not required.issubset(columns):
            return
        rows = connection.execute("SELECT id, user_id, text, due_at, status, created_at, repeat_interval_seconds, repeat_until FROM reminders_legacy_pre_chat_only").fetchall()
        for row in rows:
            due_at = Database._normalize_kyiv_iso(row["due_at"])
            created_at = Database._normalize_kyiv_iso(row["created_at"]) or due_at or "1970-01-01T00:00:00+02:00"
            repeat_until = Database._normalize_kyiv_iso(row["repeat_until"]) if row["repeat_until"] else None
            interval = row["repeat_interval_seconds"]
            recurrence_kind = "daily" if interval == 86400 else "weekly" if interval == 604800 else None
            legacy_status = str(row["status"] or "").casefold()
            status = "pending" if legacy_status == "pending" and due_at else "cancelled" if legacy_status == "cancelled" else "failed"
            parsed_due = datetime.fromisoformat(due_at) if due_at else None
            if interval is not None and recurrence_kind is None:
                status = "failed"
            weekday = None
            if recurrence_kind == "weekly":
                if parsed_due is None:
                    status = "failed"
                else:
                    weekday = parsed_due.astimezone(kyiv_timezone()).weekday()
            if recurrence_kind and row["repeat_until"] and repeat_until is None:
                status = "failed"
            connection.execute(
                "INSERT OR IGNORE INTO reminders(user_id,text,due_at,timezone,status,recurrence_kind,recurrence_weekday,repeat_until,legacy_source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(row["user_id"]), str(row["text"]), due_at or created_at, "Europe/Kyiv", status, recurrence_kind, weekday, repeat_until, int(row["id"]), created_at, created_at),
            )

    @staticmethod
    def _normalize_kyiv_iso(value: object) -> str | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        kyiv = kyiv_timezone()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=kyiv)
        else:
            parsed = parsed.astimezone(kyiv)
        return parsed.isoformat()

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
