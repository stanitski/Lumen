from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from lumen.services.reminders import KYIV, ReminderService
from lumen.storage.db import Database


class FakeWake:
    def schedule(self, due_at: datetime | None) -> None:
        pass


class FakeDelivery:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    async def deliver_reminder(self, **kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("delivery failed")
        self.calls.append(kwargs)


class FakeConversationService:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, object]] = {}
        self.calls: list[dict[str, object]] = []
        self.fail_next = False

    async def record_event(self, **kwargs: object) -> int:
        self.calls.append(kwargs)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("history unavailable")
        event_id = str(kwargs["event_id"])
        self.events.setdefault(event_id, kwargs)
        return 1


class ReminderHistoryTests(unittest.IsolatedAsyncioTestCase):
    ROUTE = {"target_channel": "telegram", "target_chat_id": "492198718"}

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp.name) / "lumen.db"))
        self.database.init()
        self.now = datetime(2026, 7, 20, 8, 0, tzinfo=KYIV)
        self.delivery = FakeDelivery()
        self.history = FakeConversationService()
        self.service = ReminderService(
            self.database.session,
            self.delivery,
            FakeWake(),
            conversation_service=self.history,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _insert_due(self) -> int:
        with self.database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO reminders(user_id,text,due_at,timezone,status,delivery_route_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "u1", "Випити воду", self.now.isoformat(), "Europe/Kyiv", "pending",
                    json.dumps(self.ROUTE), self.now.isoformat(), self.now.isoformat(),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    async def test_successful_delivery_records_one_assistant_notification(self) -> None:
        reminder_id = self._insert_due()

        await self.service.run_due(now=self.now)

        self.assertEqual(1, len(self.delivery.calls))
        self.assertEqual(1, len(self.history.events))
        event = next(iter(self.history.events.values()))
        self.assertEqual("assistant", event["role"])
        self.assertEqual("Випити воду", event["content"])
        self.assertEqual("", event["question"])
        self.assertEqual("reminder_notification", event["kind"])
        self.assertEqual(reminder_id, event["metadata"]["reminder_id"])
        self.assertEqual(self.ROUTE, {key: event["metadata"][key] for key in self.ROUTE})

    async def test_failed_delivery_does_not_record_history(self) -> None:
        self._insert_due()
        self.delivery.fail = True

        await self.service.run_due(now=self.now)

        self.assertEqual(0, len(self.history.events))
        self.assertEqual([], self.history.calls)

    async def test_history_failure_is_retried_without_redelivery_or_duplicate_event(self) -> None:
        self._insert_due()
        self.history.fail_next = True

        await self.service.run_due(now=self.now)
        self.assertEqual(1, len(self.delivery.calls))
        self.assertEqual(0, len(self.history.events))

        await self.service.run_due(now=self.now + timedelta(seconds=1))

        self.assertEqual(1, len(self.delivery.calls))
        self.assertEqual(1, len(self.history.events))
        self.assertEqual(2, len(self.history.calls))
        self.assertEqual(self.history.calls[0]["event_id"], self.history.calls[1]["event_id"])

    async def test_repeated_cycle_uses_idempotent_history_event_id(self) -> None:
        self._insert_due()

        await self.service.run_due(now=self.now)
        await self.service.run_due(now=self.now + timedelta(seconds=1))

        self.assertEqual(1, len(self.delivery.calls))
        self.assertEqual(1, len(self.history.events))
        self.assertEqual(2, len(self.history.calls))
        self.assertEqual(self.history.calls[0]["event_id"], self.history.calls[1]["event_id"])


if __name__ == "__main__":
    unittest.main()
