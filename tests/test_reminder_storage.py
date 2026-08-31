from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lumen.storage.db import CURRENT_SCHEMA_VERSION, Database


class ReminderStorageTests(unittest.TestCase):
    def test_v34_schema_has_retry_and_management_session_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "lumen.db"))
            database.init()

            with database.session() as connection:
                reminder_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(reminders)").fetchall()
                }
                session_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(reminder_management_sessions)").fetchall()
                }
                indexes = {
                    row["name"]
                    for row in connection.execute("PRAGMA index_list(reminder_management_sessions)").fetchall()
                }
                version = connection.execute("SELECT version FROM schema_version").fetchone()["version"]

            self.assertEqual(34, CURRENT_SCHEMA_VERSION)
            self.assertEqual(34, version)
            self.assertIn("next_attempt_at", reminder_columns)
            self.assertEqual(
                {
                    "token",
                    "user_id",
                    "delivery_route_json",
                    "operation",
                    "candidates_json",
                    "patch_json",
                    "expires_at",
                    "created_at",
                },
                session_columns,
            )
            self.assertIn("idx_reminder_management_sessions_expires_at", indexes)

    def test_v33_reminder_table_migrates_next_attempt_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lumen.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE reminders (
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
                        confirmation_deadline_at TEXT NULL,
                        delivery_route_json TEXT NULL,
                        parent_reminder_id INTEGER NULL REFERENCES reminders(id),
                        source_ref TEXT NULL,
                        legacy_source_id INTEGER NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(str(path))
            database.init()
            with database.session() as connection:
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(reminders)").fetchall()
                }
                version = connection.execute("SELECT version FROM schema_version").fetchone()["version"]

            self.assertIn("next_attempt_at", columns)
            self.assertEqual(34, version)
