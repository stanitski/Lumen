from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from lumen.services.reminders import KYIV, ReminderService
from lumen.storage.db import Database


class FakeWake:
    def __init__(self) -> None:
        self.scheduled: list[datetime | None] = []

    def schedule(self, due_at: datetime | None) -> None:
        self.scheduled.append(due_at)


class FakeDelivery:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def deliver_reminder(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class FailingDelivery(FakeDelivery):
    async def deliver_reminder(self, **kwargs: object) -> None:
        raise RuntimeError("delivery failed")


class ReminderServiceTests(unittest.IsolatedAsyncioTestCase):
    DEFAULT_ROUTE = {"target_channel": "telegram", "target_chat_id": "492198718"}

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp.name) / "lumen.db"))
        self.database.init()
        self.wake = FakeWake()
        self.delivery = FakeDelivery()
        self.service = ReminderService(self.database.session, self.delivery, self.wake)
        self.now = datetime(2026, 7, 20, 8, 0, tzinfo=KYIV)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _contract(
        action: str,
        *,
        items: list[dict[str, object]] | None = None,
        ordinal: int | None = None,
        selector: dict[str, object] | None = None,
        patch: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {"reminder": {"action": action, "items": items or [], "delete_ordinal": ordinal, "selector": selector or {}, "patch": patch or {}, "confirmation": ""}}

    def _rows(self, user_id: str = "u1") -> list[object]:
        with self.database.session() as connection:
            return connection.execute("SELECT * FROM reminders WHERE user_id=? ORDER BY due_at,id", (user_id,)).fetchall()

    def _insert(
        self,
        *,
        text: str,
        due_at: datetime,
        recurrence_kind: str | None = None,
        user_id: str = "u1",
        routed: bool = True,
        parent_reminder_id: int | None = None,
        repeat_until: datetime | None = None,
    ) -> int:
        route_json = json.dumps(self.DEFAULT_ROUTE) if routed else None
        with self.database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO reminders(user_id,text,due_at,timezone,status,recurrence_kind,repeat_until,delivery_route_json,parent_reminder_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, text, due_at.isoformat(), "Europe/Kyiv", "pending", recurrence_kind, repeat_until.isoformat() if repeat_until else None, route_json, parent_reminder_id, self.now.isoformat(), self.now.isoformat()),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def test_calendar_upsert_is_idempotent_and_user_scoped(self) -> None:
        source_ref = "ha_calendar:calendar.primary:event-1:2026-07-20T09:00:00+03:00"
        notification_at = self.now + timedelta(hours=1)
        first_id = self.service.upsert_calendar_reminder(
            user_id="u1",
            source_ref=source_ref,
            text="Зустріч\nОпис",
            notification_at=notification_at,
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now,
        )
        second_id = self.service.upsert_calendar_reminder(
            user_id="u1",
            source_ref=source_ref,
            text="Зустріч\nОпис",
            notification_at=notification_at,
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now + timedelta(minutes=1),
        )
        other_user_id = self.service.upsert_calendar_reminder(
            user_id="u2",
            source_ref=source_ref,
            text="Зустріч\nОпис",
            notification_at=notification_at,
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now,
        )

        row = self._rows()[0]
        self.assertEqual(first_id, second_id)
        self.assertNotEqual(first_id, other_user_id)
        self.assertEqual(1, len(self._rows()))
        self.assertEqual(1, len(self._rows("u2")))
        self.assertEqual("Зустріч\nОпис", row["text"])
        self.assertEqual(notification_at.isoformat(), row["due_at"])
        self.assertEqual(source_ref, row["source_ref"])
        self.assertIsNone(row["recurrence_kind"])
        self.assertEqual(self.DEFAULT_ROUTE, json.loads(row["delivery_route_json"]))
        self.assertEqual(self.now.isoformat(), row["updated_at"])

    def test_calendar_upsert_updates_only_pending_root(self) -> None:
        source_ref = "ha_calendar:calendar.primary:event-2:instance"
        reminder_id = self.service.upsert_calendar_reminder(
            user_id="u1",
            source_ref=source_ref,
            text="Стара назва",
            notification_at=self.now + timedelta(hours=1),
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now,
        )
        updated_at = self.now + timedelta(minutes=5)
        returned_id = self.service.upsert_calendar_reminder(
            user_id="u1",
            source_ref=source_ref,
            text="Нова назва\nНовий опис",
            notification_at=self.now + timedelta(hours=2),
            delivery_route=None,
            now=updated_at,
        )

        row = self._rows()[0]
        self.assertEqual(reminder_id, returned_id)
        self.assertEqual("Нова назва\nНовий опис", row["text"])
        self.assertEqual((self.now + timedelta(hours=2)).isoformat(), row["due_at"])
        self.assertIsNone(row["delivery_route_json"])
        self.assertEqual(updated_at.isoformat(), row["updated_at"])

    def test_calendar_upsert_and_cancel_do_not_mutate_delivered_occurrence(self) -> None:
        immutable_statuses = ("delivering", "awaiting_confirmation", "snoozed", "completed")
        for index, status in enumerate(immutable_statuses):
            with self.subTest(status=status):
                user_id = f"calendar-{index}"
                source_ref = f"ha_calendar:calendar.primary:event-{index}:instance"
                reminder_id = self.service.upsert_calendar_reminder(
                    user_id=user_id,
                    source_ref=source_ref,
                    text="Оригінал",
                    notification_at=self.now + timedelta(hours=1),
                    delivery_route=self.DEFAULT_ROUTE,
                    now=self.now,
                )
                with self.database.session() as connection:
                    connection.execute("UPDATE reminders SET status=? WHERE id=?", (status, reminder_id))
                    connection.commit()
                returned_id = self.service.upsert_calendar_reminder(
                    user_id=user_id,
                    source_ref=source_ref,
                    text="Змінено",
                    notification_at=self.now + timedelta(hours=2),
                    delivery_route=None,
                    now=self.now + timedelta(minutes=5),
                )
                cancelled = self.service.cancel_calendar_reminder(
                    user_id=user_id,
                    source_ref=source_ref,
                    now=self.now + timedelta(minutes=6),
                )
                row = self._rows(user_id)[0]
                self.assertEqual(reminder_id, returned_id)
                self.assertFalse(cancelled)
                self.assertEqual(status, row["status"])
                self.assertEqual("Оригінал", row["text"])
                self.assertEqual((self.now + timedelta(hours=1)).isoformat(), row["due_at"])
                self.assertEqual(self.DEFAULT_ROUTE, json.loads(row["delivery_route_json"]))

    def test_calendar_cancel_is_idempotent_for_pending_root(self) -> None:
        source_ref = "ha_calendar:calendar.primary:event-cancel:instance"
        self.service.upsert_calendar_reminder(
            user_id="u1",
            source_ref=source_ref,
            text="Скасувати",
            notification_at=self.now + timedelta(hours=1),
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now,
        )

        first = self.service.cancel_calendar_reminder(user_id="u1", source_ref=source_ref, now=self.now)
        second = self.service.cancel_calendar_reminder(user_id="u1", source_ref=source_ref, now=self.now)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual("cancelled", self._rows()[0]["status"])

    def test_calendar_reconcile_cancels_only_stale_pending_roots_in_scope(self) -> None:
        prefix = "ha_calendar:calendar.primary:"
        refs = {
            "active": prefix + "active:instance",
            "stale": prefix + "stale:instance",
            "completed": prefix + "completed:instance",
            "other-prefix": "ha_calendar:calendar.shared:stale:instance",
        }
        reminder_ids: dict[str, int] = {}
        for index, (label, source_ref) in enumerate(refs.items(), start=1):
            reminder_ids[label] = self.service.upsert_calendar_reminder(
                user_id="u1",
                source_ref=source_ref,
                text=label,
                notification_at=self.now + timedelta(hours=index),
                delivery_route=self.DEFAULT_ROUTE,
                now=self.now,
            )
        with self.database.session() as connection:
            connection.execute(
                "UPDATE reminders SET status='completed' WHERE id=?",
                (reminder_ids["completed"],),
            )
            connection.commit()
        other_user_id = self.service.upsert_calendar_reminder(
            user_id="u2",
            source_ref=refs["stale"],
            text="other user",
            notification_at=self.now + timedelta(hours=1),
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now,
        )
        wake_count = len(self.wake.scheduled)

        changed = self.service.reconcile_calendar_reminders(
            user_id="u1",
            source_prefix=prefix,
            active_source_refs={refs["active"]},
            now=self.now + timedelta(minutes=1),
        )
        repeated = self.service.reconcile_calendar_reminders(
            user_id="u1",
            source_prefix=prefix,
            active_source_refs={refs["active"]},
            now=self.now + timedelta(minutes=2),
        )

        rows = {row["id"]: row for row in self._rows()}
        self.assertEqual(1, changed)
        self.assertEqual(0, repeated)
        self.assertEqual("pending", rows[reminder_ids["active"]]["status"])
        self.assertEqual("cancelled", rows[reminder_ids["stale"]]["status"])
        self.assertEqual("completed", rows[reminder_ids["completed"]]["status"])
        self.assertEqual("pending", rows[reminder_ids["other-prefix"]]["status"])
        self.assertEqual("pending", self._rows("u2")[0]["status"])
        self.assertEqual(other_user_id, self._rows("u2")[0]["id"])
        self.assertEqual(wake_count + 1, len(self.wake.scheduled))

    def test_calendar_reconcile_rejects_active_ref_outside_prefix(self) -> None:
        source_ref = "ha_calendar:calendar.primary:event:instance"
        self.service.upsert_calendar_reminder(
            user_id="u1",
            source_ref=source_ref,
            text="event",
            notification_at=self.now + timedelta(hours=1),
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now,
        )

        with self.assertRaises(ValueError):
            self.service.reconcile_calendar_reminders(
                user_id="u1",
                source_prefix="ha_calendar:calendar.primary:",
                active_source_refs={"ha_calendar:calendar.other:event:instance"},
                now=self.now,
            )

        self.assertEqual("pending", self._rows()[0]["status"])

    def test_structured_multi_create_nutrition_and_one_wake(self) -> None:
        contract = self._contract("create", items=[
            {"text": "випити протеїновий коктейль", "date": "2026-07-20", "time": "10:30", "recurrence_kind": "none", "recurrence_weekday": None, "repeat_until": ""},
            {"text": "з'їсти обід з куркою", "date": "2026-07-20", "time": "13:00", "recurrence_kind": "none", "recurrence_weekday": None, "repeat_until": ""},
        ])
        result = self.service.intercept_contract(user_id="u1", contract=contract, text="Нагадай сьогодні о 10:30 випити протеїновий коктейль і о 13:00 з'їсти обід з куркою", now=self.now)
        self.assertIn("1.", result.answer)
        self.assertIn("2.", result.answer)
        self.assertEqual(["випити протеїновий коктейль", "з'їсти обід з куркою"], [row["text"] for row in self._rows()])
        self.assertEqual(["2026-07-20T10:30:00+03:00", "2026-07-20T13:00:00+03:00"], [row["due_at"] for row in self._rows()])
        self.assertEqual(1, len(self.wake.scheduled))

    def test_invalid_contract_item_is_atomic(self) -> None:
        contract = self._contract("create", items=[
            {"text": "перша", "date": "2026-07-21", "time": "09:00", "recurrence_kind": "none", "recurrence_weekday": None, "repeat_until": ""},
            {"text": "друга", "date": "invalid", "time": "18:00", "recurrence_kind": "none", "recurrence_weekday": None, "repeat_until": ""},
        ])
        result = self.service.intercept_contract(user_id="u1", contract=contract, text="", now=self.now)
        self.assertIn("Пункт 2", result.answer)
        self.assertEqual([], self._rows())
        self.assertEqual([], self.wake.scheduled)

    def test_list_snapshot_binds_ordinal_and_never_recomputes(self) -> None:
        for label, time_value in (("перше", "09:00"), ("друге", "18:00")):
            self.service.intercept_contract(user_id="u1", contract=self._contract("create", items=[{"text": label, "date": "2026-07-21", "time": time_value, "recurrence_kind": "none", "recurrence_weekday": None, "repeat_until": ""}]), text="", now=self.now)
        listed = self.service.intercept_contract(user_id="u1", contract=self._contract("list"), text="покажи", now=self.now)
        self.assertIn("1. #", listed.answer)
        self.assertIn("перше", listed.answer)
        first_id = self._rows()[0]["id"]
        with self.database.session() as connection:
            connection.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (first_id,))
            connection.commit()
        result = self.service.intercept_contract(user_id="u1", contract=self._contract("cancel_list_item", ordinal=1), text="видали #1", now=self.now)
        self.assertIn("вже неактивне", result.answer)
        self.assertEqual("pending", self._rows()[1]["status"])

    def test_list_snapshot_refuses_when_displayed_row_changes(self) -> None:
        reminder_id = self._insert(text="змінюване", due_at=self.now + timedelta(days=1))
        self.service.intercept_contract(user_id="u1", contract=self._contract("list"), text="", now=self.now)
        with self.database.session() as connection:
            connection.execute(
                "UPDATE reminders SET due_at=?, updated_at=? WHERE id=?",
                ((self.now + timedelta(days=2)).isoformat(), (self.now + timedelta(minutes=1)).isoformat(), reminder_id),
            )
            connection.commit()
        result = self.service.intercept_contract(user_id="u1", contract=self._contract("cancel_list_item", ordinal=1), text="", now=self.now)
        self.assertIn("вже неактивне", result.answer)
        self.assertEqual("pending", self._rows()[0]["status"])

    def test_list_and_cancel_are_user_scoped(self) -> None:
        for user_id, label in (("u1", "моє"), ("u2", "чуже")):
            self.service.intercept_contract(user_id=user_id, contract=self._contract("create", items=[{"text": label, "date": "2026-07-21", "time": "09:00", "recurrence_kind": "none", "recurrence_weekday": None, "repeat_until": ""}]), text="", now=self.now)
        listed = self.service.intercept_contract(user_id="u1", contract=self._contract("list"), text="", now=self.now)
        self.assertIn("моє", listed.answer)
        self.assertNotIn("чуже", listed.answer)
        self.service.intercept_contract(user_id="u1", contract=self._contract("cancel_list_item", ordinal=1), text="", now=self.now)
        self.assertEqual("cancelled", self._rows("u1")[0]["status"])
        self.assertEqual("pending", self._rows("u2")[0]["status"])

    def test_missing_snapshot_refuses_without_mutation(self) -> None:
        self.service.intercept_contract(user_id="u1", contract=self._contract("create", items=[{"text": "вода", "date": "2026-07-21", "time": "09:00", "recurrence_kind": "none", "recurrence_weekday": None, "repeat_until": ""}]), text="", now=self.now)
        result = self.service.intercept_contract(user_id="u1", contract=self._contract("cancel_list_item", ordinal=1), text="", now=self.now)
        self.assertIn("список", result.answer)
        self.assertEqual("pending", self._rows()[0]["status"])

    def test_legacy_list_ordinal_can_cancel_paused_root(self) -> None:
        reminder_id = self._insert(text="paused legacy", due_at=self.now + timedelta(days=1))
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("pause", selector={"id": reminder_id}),
            text="pause", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        self.service.intercept_contract(user_id="u1", contract=self._contract("list"), text="list", now=self.now)
        result = self.service.intercept_contract(
            user_id="u1", contract=self._contract("cancel_list_item", ordinal=1), text="cancel #1", now=self.now + timedelta(minutes=1),
        )
        self.assertIn("Скасовано", result.answer)
        self.assertEqual("cancelled", self._rows()[0]["status"])

    def test_plain_confirmation_requires_contract_but_callback_stays_direct(self) -> None:
        self.assertIsNone(self.service.intercept_contract(user_id="u1", contract=self._contract("none"), text="Так", now=self.now))
        callback = {"reminder_id": 999, "delivery_id": "known", "answer": "Так"}
        result = self.service.intercept_contract(user_id="u1", contract=self._contract("none"), text="", metadata=callback, now=self.now)
        self.assertIn("Немає", result.answer)

    async def test_correlated_yes_completes_and_no_marks_occurrence_missed(self) -> None:
        self._insert(text="так", due_at=self.now)
        await self.service.run_due(now=self.now)
        row = self._rows()[0]
        yes = self._contract("none")
        yes["reminder"]["confirmation"] = "yes"
        self.service.intercept_contract(user_id="u1", contract=yes, text="Так", now=self.now + timedelta(minutes=1))
        self.assertEqual("completed", self._rows()[0]["status"])

        self._insert(text="ні", due_at=self.now)
        await self.service.run_due(now=self.now)
        no = self._contract("none")
        no["reminder"]["confirmation"] = "no"
        self.service.intercept_contract(user_id="u1", contract=no, text="Ні", now=self.now + timedelta(minutes=2))
        root = self._rows()[1]
        self.assertEqual("missed", root["status"])
        self.assertEqual(2, len(self._rows()))

    async def test_persisted_route_survives_reconstruction_and_run_due(self) -> None:
        route = {"target_channel": "telegram", "target_chat_id": "492198718"}
        contract = self._contract("create", items=[{
            "text": "route survives",
            "date": "2026-07-20",
            "time": "09:00",
            "recurrence_kind": "none",
            "recurrence_weekday": None,
            "repeat_until": "",
        }])
        self.service.intercept_contract(user_id="u1", contract=contract, text="", delivery_route=route, now=self.now)
        stored = self._rows()[0]
        self.assertEqual(route, json.loads(stored["delivery_route_json"]))

        delivery = FakeDelivery()
        reconstructed = ReminderService(self.database.session, delivery, self.wake)
        await reconstructed.run_due(now=self.now + timedelta(hours=1))

        metadata = delivery.calls[0]["metadata"]
        self.assertEqual(route, {key: metadata[key] for key in route})
        delivery_id = str(metadata["delivery_id"])
        self.assertEqual(32, len(delivery_id))
        self.assertTrue(all(character in "0123456789abcdef" for character in delivery_id))
        callback = f"reminder_confirmed:{stored['id']}:{delivery_id}"
        self.assertLessEqual(len(callback.encode("utf-8")), 64)

    def test_telegram_route_is_accepted(self) -> None:
        route = {"target_channel": "telegram", "target_chat_id": "-1001234567890"}
        self.assertEqual(route, ReminderService._normalize_delivery_route(route))

    async def test_invalid_routes_do_not_persist_or_deliver(self) -> None:
        invalid_routes = {
            "unsupported-channel": {"target_channel": "discord", "target_chat_id": "123"},
            "nonnumeric-target": {"target_channel": "telegram", "target_chat_id": "not-a-chat-id"},
            "oversized-target": {"target_channel": "telegram", "target_chat_id": "1" * 501},
            "extra-field": {"target_channel": "telegram", "target_chat_id": "123", "scope": "private"},
        }
        for index, (label, route) in enumerate(invalid_routes.items(), start=1):
            with self.subTest(route=label):
                user_id = f"invalid-{index}"
                contract = self._contract("create", items=[{
                    "text": label,
                    "date": "2026-07-20",
                    "time": "09:00",
                    "recurrence_kind": "none",
                    "recurrence_weekday": None,
                    "repeat_until": "",
                }])
                self.service.intercept_contract(
                    user_id=user_id,
                    contract=contract,
                    text="",
                    delivery_route=route,
                    now=self.now,
                )
                row = self._rows(user_id)[0]
                self.assertIsNone(row["delivery_route_json"])

        await self.service.run_due(now=self.now + timedelta(hours=1))
        self.assertEqual([], self.delivery.calls)
        for user_id in (f"invalid-{index}" for index in range(1, 5)):
            self.assertEqual("pending", self._rows(user_id)[0]["status"])

    async def test_recurring_confirmation_preserves_delivery_route(self) -> None:
        route = {"target_channel": "telegram", "target_chat_id": "492198718"}
        reminder_id = self._insert(text="daily route", due_at=self.now, recurrence_kind="daily")
        with self.database.session() as connection:
            connection.execute("UPDATE reminders SET delivery_route_json=? WHERE id=?", (json.dumps(route), reminder_id))
            connection.commit()

        await self.service.run_due(now=self.now)
        delivery_id = self.delivery.calls[0]["metadata"]["delivery_id"]
        confirmation = self._contract("none")
        confirmation["reminder"]["confirmation"] = "yes"
        self.service.intercept_contract(
            user_id="u1",
            contract=confirmation,
            text="Так",
            metadata={"reminder_id": reminder_id, "delivery_id": delivery_id},
            now=self.now + timedelta(minutes=1),
        )
        await self.service.run_due(now=self.now + timedelta(days=1))

        self.assertEqual(route, {key: self.delivery.calls[1]["metadata"][key] for key in route})

    async def test_explicit_snooze_preserves_delivery_route(self) -> None:
        route = {"target_channel": "telegram", "target_chat_id": "492198718"}
        reminder_id = self._insert(text="retry route", due_at=self.now)
        with self.database.session() as connection:
            connection.execute("UPDATE reminders SET delivery_route_json=? WHERE id=?", (json.dumps(route), reminder_id))
            connection.commit()

        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1",
            contract=self._contract("snooze", selector={"id": reminder_id}, patch={"snooze_minutes": 15}),
            text="Нагадай через 15 хвилин",
            delivery_route=route,
            now=self.now + timedelta(minutes=1),
        )
        await self.service.run_due(now=self.now + timedelta(minutes=16))

        self.assertEqual(route, {key: self.delivery.calls[1]["metadata"][key] for key in route})

    async def test_route_less_row_retries_without_delivery_then_fails_at_max(self) -> None:
        self._insert(text="legacy route", due_at=self.now, user_id="492198718", routed=False)
        await self.service.run_due(now=self.now)
        row = self._rows("492198718")[0]
        self.assertEqual([], self.delivery.calls)
        self.assertEqual("pending", row["status"])
        self.assertEqual(self.now.isoformat(), row["due_at"])
        self.assertEqual((self.now + timedelta(minutes=15)).isoformat(), row["next_attempt_at"])

        await self.service.run_due(now=self.now + timedelta(minutes=15))
        row = self._rows("492198718")[0]
        self.assertEqual([], self.delivery.calls)
        self.assertEqual("pending", row["status"])
        self.assertEqual(self.now.isoformat(), row["due_at"])
        self.assertEqual((self.now + timedelta(minutes=30)).isoformat(), row["next_attempt_at"])

        await self.service.run_due(now=self.now + timedelta(minutes=30))
        self.assertEqual([], self.delivery.calls)
        self.assertEqual("missed", self._rows("492198718")[0]["status"])

    async def test_confirmation_requires_matching_delivery_id(self) -> None:
        reminder_id = self._insert(text="confirmation id", due_at=self.now)
        await self.service.run_due(now=self.now)
        confirmation = self._contract("none")
        confirmation["reminder"]["confirmation"] = "yes"
        legacy_delivery_id = "a" * 64
        with self.database.session() as connection:
            connection.execute("UPDATE reminders SET delivery_id=? WHERE id=?", (legacy_delivery_id, reminder_id))
            connection.commit()

        stale = self.service.intercept_contract(
            user_id="u1",
            contract=confirmation,
            text="Так",
            metadata={"reminder_id": reminder_id, "delivery_id": "b" * 64},
            now=self.now + timedelta(minutes=1),
        )
        self.assertIn("застарілій доставці", stale.answer)
        self.assertEqual("awaiting_confirmation", self._rows()[0]["status"])

        accepted = self.service.intercept_contract(
            user_id="u1",
            contract=confirmation,
            text="Так",
            metadata={"reminder_id": reminder_id, "delivery_id": legacy_delivery_id},
            now=self.now + timedelta(minutes=2),
        )
        self.assertIn("завершено", accepted.answer)
        self.assertEqual("completed", self._rows()[0]["status"])

    async def test_silence_retries_and_daily_confirmation_moves_to_next_occurrence(self) -> None:
        self._insert(text="тиша", due_at=self.now)
        await self.service.run_due(now=self.now)
        self.assertEqual(1, len(self.delivery.calls))
        self.assertEqual("awaiting_confirmation", self._rows()[0]["status"])
        with self.database.session() as connection:
            connection.execute("UPDATE reminders SET status='completed' WHERE id=?", (self._rows()[0]["id"],))
            connection.commit()

        self._insert(text="щодня", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        daily = self._rows()[1]
        yes = self._contract("none")
        yes["reminder"]["confirmation"] = "yes"
        self.service.intercept_contract(
            user_id="u1",
            contract=yes,
            text="yes",
            metadata={"reminder_id": daily["id"], "delivery_id": self.delivery.calls[-1]["metadata"]["delivery_id"]},
            now=self.now + timedelta(minutes=1),
        )
        self.service.intercept_contract(user_id="u1", contract=yes, text="Так", now=self.now + timedelta(minutes=1))
        self.assertEqual("pending", self._rows()[1]["status"])
        self.assertEqual((self.now + timedelta(days=1)).isoformat(), self._rows()[1]["due_at"])

    async def test_recurring_silence_advances_to_original_next_slot_without_child(self) -> None:
        reminder_id = self._insert(text="daily silence", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        await self.service.run_due(now=self.now + timedelta(minutes=15))
        root = next(row for row in self._rows() if row["id"] == reminder_id)
        self.assertEqual("pending", root["status"])
        self.assertEqual((self.now + timedelta(days=1)).isoformat(), root["due_at"])
        self.assertEqual([], [row for row in self._rows() if row["parent_reminder_id"] == reminder_id])
        self.assertEqual(1, len(self.delivery.calls))

    async def test_cancelled_delivery_claim_never_delivers_or_resurrects(self) -> None:
        reminder_id = self._insert(text="race", due_at=self.now)
        claimed = self.service._claim_due(self.now)[0]
        with self.database.session() as connection:
            connection.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (reminder_id,))
            connection.commit()
        await self.service._deliver_claimed(claimed, self.now)
        self.assertEqual([], self.delivery.calls)
        self.assertEqual("cancelled", self._rows()[0]["status"])

    async def test_startup_grace_delivers_at_29_minutes_and_misses_at_31(self) -> None:
        within_id = self._insert(text="within", due_at=self.now, user_id="within")
        await self.service.run_due(now=self.now + timedelta(minutes=29))
        self.assertEqual("awaiting_confirmation", self._rows("within")[0]["status"])
        self.assertEqual(within_id, self.delivery.calls[0]["reminder_id"])
        self._insert(text="stale one", due_at=self.now, user_id="stale-one")
        self._insert(text="stale daily", due_at=self.now, recurrence_kind="daily", user_id="stale-daily")
        await self.service.run_due(now=self.now + timedelta(minutes=31))
        self.assertEqual(1, len(self.delivery.calls))
        self.assertEqual("missed", self._rows("stale-one")[0]["status"])
        daily = self._rows("stale-daily")[0]
        self.assertEqual("pending", daily["status"])
        self.assertEqual((self.now + timedelta(days=1)).isoformat(), daily["due_at"])

    def test_daily_and_weekly_next_occurrence_preserve_kyiv_wall_time_across_dst(self) -> None:
        daily_due = datetime(2026, 10, 24, 10, 30, tzinfo=KYIV)
        weekly_due = datetime(2026, 10, 18, 9, 15, tzinfo=KYIV)
        self._insert(text="daily dst", due_at=daily_due, recurrence_kind="daily", user_id="daily-dst")
        self._insert(text="weekly dst", due_at=weekly_due, recurrence_kind="weekly", user_id="weekly-dst")
        daily_next = self.service._next_occurrence(self._rows("daily-dst")[0])
        weekly_next = self.service._next_occurrence(self._rows("weekly-dst")[0])
        self.assertEqual((10, 30), (daily_next.hour, daily_next.minute))
        self.assertEqual((9, 15), (weekly_next.hour, weekly_next.minute))
        self.assertNotEqual(daily_due.utcoffset(), daily_next.utcoffset())
        self.assertNotEqual(weekly_due.utcoffset(), weekly_next.utcoffset())

    async def test_no_advances_recurring_root_without_child(self) -> None:
        root_id = self._insert(text="replace child", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        no = self._contract("none")
        no["reminder"]["confirmation"] = "no"
        self.service.intercept_contract(user_id="u1", contract=no, text="no", now=self.now + timedelta(minutes=1))
        root = self._rows()[0]
        self.assertEqual("pending", root["status"])
        self.assertEqual((self.now + timedelta(days=1)).isoformat(), root["due_at"])
        self.assertEqual([], [row for row in self._rows() if row["parent_reminder_id"] == root_id])

    async def test_yes_on_explicit_snooze_child_advances_root_from_original_slot(self) -> None:
        root_id = self._insert(text="original slot", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1",
            contract=self._contract("snooze", selector={"id": root_id}, patch={"snooze_minutes": 15}),
            text="через 15 хвилин",
            delivery_route=self.DEFAULT_ROUTE,
            now=self.now + timedelta(minutes=1),
        )
        child = self._rows()[1]
        await self.service.run_due(now=self.now + timedelta(minutes=16))
        child_delivery_id = self.delivery.calls[-1]["metadata"]["delivery_id"]
        yes = self._contract("none")
        yes["reminder"]["confirmation"] = "yes"
        self.service.intercept_contract(
            user_id="u1",
            contract=yes,
            text="yes",
            metadata={"reminder_id": child["id"], "delivery_id": child_delivery_id},
            now=self.now + timedelta(minutes=17),
        )
        root = next(row for row in self._rows() if row["id"] == root_id)
        self.assertEqual("pending", root["status"])
        self.assertEqual((self.now + timedelta(days=1)).isoformat(), root["due_at"])
        self.assertEqual("completed", next(row for row in self._rows() if row["id"] == child["id"])["status"])

    async def test_concurrent_confirmation_expiry_advances_once_without_child(self) -> None:
        root_id = self._insert(text="concurrent", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        other = ReminderService(self.database.session, self.delivery, self.wake)
        await asyncio.gather(
            self.service.run_due(now=self.now + timedelta(minutes=15)),
            other.run_due(now=self.now + timedelta(minutes=15)),
        )
        rows = self._rows()
        root = next(row for row in rows if row["id"] == root_id)
        self.assertEqual("pending", root["status"])
        self.assertEqual((self.now + timedelta(days=1)).isoformat(), root["due_at"])
        self.assertEqual([], [row for row in rows if row["parent_reminder_id"] == root_id])

    async def test_wake_selects_child_after_explicit_snooze(self) -> None:
        root_id = self._insert(text="wake child", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("snooze", selector={"id": root_id}),
            text="відклади", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        child = next(row for row in self._rows() if row["parent_reminder_id"] == root_id)
        self.assertEqual(child["due_at"], self.wake.scheduled[-1].isoformat())

    async def test_child_route_survives_reconstruction(self) -> None:
        route = self.DEFAULT_ROUTE
        root_id = self._insert(text="child route", due_at=self.now, recurrence_kind="daily")
        with self.database.session() as connection:
            connection.execute("UPDATE reminders SET delivery_route_json=? WHERE id=?", (json.dumps(route), root_id))
            connection.commit()
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("snooze", selector={"id": root_id}),
            text="відклади", delivery_route=route, now=self.now + timedelta(minutes=1),
        )
        child_id = next(row["id"] for row in self._rows() if row["parent_reminder_id"] == root_id)
        reconstructed = ReminderService(self.database.session, self.delivery, self.wake)
        await reconstructed.run_due(now=self.now + timedelta(minutes=16))
        child = next(row for row in self._rows() if row["id"] == child_id)
        self.assertEqual(json.loads(child["delivery_route_json"]), route)
        self.assertEqual(route, {key: self.delivery.calls[-1]["metadata"][key] for key in route})

    async def test_list_hides_child_and_cancel_root_cascades(self) -> None:
        root_id = self._insert(text="visible root", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        no = self._contract("none")
        no["reminder"]["confirmation"] = "no"
        self.service.intercept_contract(user_id="u1", contract=no, text="no", now=self.now + timedelta(minutes=1))
        listed = self.service.intercept_contract(user_id="u1", contract=self._contract("list"), text="list", now=self.now)
        self.assertEqual(1, listed.answer.count("visible root"))
        self.service.intercept_contract(user_id="u1", contract=self._contract("cancel_list_item", ordinal=1), text="cancel", now=self.now)
        self.assertTrue(all(row["status"] == "cancelled" for row in self._rows()))
        self.assertEqual(root_id, self._rows()[0]["id"])

    async def test_delivery_failure_retries_root_and_child_without_mutating_parent_contract(self) -> None:
        failing = ReminderService(self.database.session, FailingDelivery(), self.wake)
        root_id = self._insert(text="failed delivery", due_at=self.now, recurrence_kind="daily")
        await failing.run_due(now=self.now)
        root = self._rows()[0]
        self.assertEqual("pending", root["status"])
        self.assertEqual(self.now.isoformat(), root["due_at"])
        self.assertEqual((self.now + timedelta(minutes=1)).isoformat(), root["next_attempt_at"])
        self.assertEqual("daily", root["recurrence_kind"])
        self.assertEqual(root_id, root["id"])

    async def test_delivery_failure_on_child_keeps_root_snoozed(self) -> None:
        root_id = self._insert(text="failed child", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("snooze", selector={"id": root_id}),
            text="відклади", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        child_id = next(row["id"] for row in self._rows() if row["parent_reminder_id"] == root_id)
        failing = ReminderService(self.database.session, FailingDelivery(), self.wake)
        await failing.run_due(now=self.now + timedelta(minutes=16))
        root = next(row for row in self._rows() if row["id"] == root_id)
        child = next(row for row in self._rows() if row["id"] == child_id)
        self.assertEqual("snoozed", root["status"])
        self.assertEqual("daily", root["recurrence_kind"])
        self.assertEqual("pending", child["status"])
        self.assertEqual((self.now + timedelta(minutes=16)).isoformat(), child["due_at"])
        self.assertEqual((self.now + timedelta(minutes=17)).isoformat(), child["next_attempt_at"])

    async def test_child_max_retries_advances_recurring_root_to_original_slot(self) -> None:
        root_id = self._insert(text="max recurring", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("snooze", selector={"id": root_id}),
            text="відклади", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        child_id = next(row["id"] for row in self._rows() if row["parent_reminder_id"] == root_id)
        failing = ReminderService(self.database.session, FailingDelivery(), self.wake)
        for retry_at in (
            self.now + timedelta(minutes=16),
            self.now + timedelta(minutes=17),
            self.now + timedelta(minutes=22),
            self.now + timedelta(minutes=37),
        ):
            await failing.run_due(now=retry_at)
        root = next(row for row in self._rows() if row["id"] == root_id)
        child = next(row for row in self._rows() if row["id"] == child_id)
        self.assertEqual("pending", root["status"])
        self.assertEqual((self.now + timedelta(days=1)).isoformat(), root["due_at"])
        self.assertEqual("missed", child["status"])
        self.assertNotEqual("snoozed", root["status"])
        self.assertEqual([], [row for row in self._rows() if row["status"] == "snoozed"])

    async def test_child_max_retries_fails_one_time_root_without_snoozed_orphan(self) -> None:
        root_id = self._insert(text="max one-time", due_at=self.now)
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("snooze", selector={"id": root_id}),
            text="відклади", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        child_id = next(row["id"] for row in self._rows() if row["parent_reminder_id"] == root_id)
        failing = ReminderService(self.database.session, FailingDelivery(), self.wake)
        for retry_at in (
            self.now + timedelta(minutes=16),
            self.now + timedelta(minutes=17),
            self.now + timedelta(minutes=22),
            self.now + timedelta(minutes=37),
        ):
            await failing.run_due(now=retry_at)
        root = next(row for row in self._rows() if row["id"] == root_id)
        child = next(row for row in self._rows() if row["id"] == child_id)
        self.assertEqual("missed", root["status"])
        self.assertEqual("missed", child["status"])
        self.assertEqual([], [row for row in self._rows() if row["status"] == "snoozed"])

    async def test_snoozed_listing_keeps_canonical_due_and_shows_child_separately(self) -> None:
        root_id = self._insert(text="effective due", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("snooze", selector={"id": root_id}),
            text="відклади", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        child = next(row for row in self._rows() if row["parent_reminder_id"] == root_id)
        listed = self.service.intercept_contract(user_id="u1", contract=self._contract("list"), text="list", now=self.now + timedelta(minutes=1))
        self.assertIn("08:00", listed.answer)
        self.assertIn("snooze до 20.07.2026 о 08:16", listed.answer)
        self.service.intercept_contract(user_id="u1", contract=self._contract("cancel_list_item", ordinal=1), text="cancel", now=self.now + timedelta(minutes=2))
        root = next(row for row in self._rows() if row["id"] == root_id)
        child_after = next(row for row in self._rows() if row["id"] == child["id"])
        self.assertEqual("cancelled", root["status"])
        self.assertEqual("cancelled", child_after["status"])

    def test_calendar_move_clears_pending_delivery_retry(self) -> None:
        source_ref = "ha_calendar:calendar.primary:event-retry"
        reminder_id = self.service.upsert_calendar_reminder(
            user_id="u1", source_ref=source_ref, text="зустріч",
            notification_at=self.now + timedelta(hours=1), delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        with self.database.session() as connection:
            connection.execute(
                "UPDATE reminders SET next_attempt_at=? WHERE id=?",
                ((self.now + timedelta(minutes=1)).isoformat(), reminder_id),
            )
            connection.commit()
        self.service.upsert_calendar_reminder(
            user_id="u1", source_ref=source_ref, text="зустріч",
            notification_at=self.now + timedelta(hours=6), delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        row = self._rows()[0]
        self.assertEqual((self.now + timedelta(hours=6)).isoformat(), row["due_at"])
        self.assertIsNone(row["next_attempt_at"])

    def test_weekly_create_normalizes_date_to_requested_weekday(self) -> None:
        contract = self._contract("create", items=[{
            "text": "понеділкове", "date": "2026-07-22", "time": "09:00",
            "recurrence_kind": "weekly", "recurrence_weekday": 0, "repeat_until": "",
        }])
        result = self.service.intercept_contract(user_id="u1", contract=contract, text="", now=self.now)
        self.assertIn("Нагадування записано", result.answer)
        due = datetime.fromisoformat(self._rows()[0]["due_at"])
        self.assertEqual(0, due.weekday())
        self.assertEqual("2026-07-27", due.date().isoformat())

    def test_exact_update_pause_resume_and_soft_delete_lifecycle(self) -> None:
        reminder_id = self._insert(text="пити таблетки", due_at=self.now + timedelta(days=1), recurrence_kind="daily")
        updated = self.service.intercept_contract(
            user_id="u1",
            contract=self._contract("update", selector={"query": "пити таблетки", "scope": "one"}, patch={"text": "прийняти таблетки", "time": "10:30"}),
            text="зміни", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        self.assertIn("Оновлено", updated.answer)
        row = self._rows()[0]
        self.assertEqual("прийняти таблетки", row["text"])
        self.assertEqual("10:30", datetime.fromisoformat(row["due_at"]).strftime("%H:%M"))
        for action, expected in (("pause", "paused"), ("resume", "pending"), ("delete", "cancelled")):
            result = self.service.intercept_contract(
                user_id="u1", contract=self._contract(action, selector={"id": reminder_id}),
                text=action, delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
            )
            self.assertTrue(result.handled)
            self.assertEqual(expected, self._rows()[0]["status"])

    def test_ambiguous_selection_survives_service_reconstruction(self) -> None:
        first = self._insert(text="пити ранкові таблетки", due_at=self.now + timedelta(days=1))
        second = self._insert(text="пити вечірні таблетки", due_at=self.now + timedelta(days=1, hours=8))
        result = self.service.intercept_contract(
            user_id="u1", contract=self._contract("delete", selector={"query": "таблетки", "scope": "one"}),
            text="видали таблетки", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        token_match = re.search(r"[a-f0-9]{32}", result.answer)
        self.assertIsNotNone(token_match)
        token = token_match.group(0)
        reconstructed = ReminderService(self.database.session, self.delivery, self.wake)
        wrong_mode = reconstructed.intercept_contract(
            user_id="u1", contract=self._contract("confirm_bulk", selector={"session_token": token}),
            text=f"підтвердити {token}", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(seconds=30),
        )
        self.assertIn("Некоректне підтвердження", wrong_mode.answer)
        self.assertTrue(all(row["status"] == "pending" for row in self._rows()))
        selected = reconstructed.intercept_contract(
            user_id="u1", contract=self._contract("select", selector={"session_token": token, "ordinal": 2}),
            text=f"обрати {token} #2", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        self.assertIn("Скасовано", selected.answer)
        rows = {row["id"]: row["status"] for row in self._rows()}
        self.assertEqual("pending", rows[first])
        self.assertEqual("cancelled", rows[second])

    def test_bulk_delete_requires_confirmation(self) -> None:
        ids = [self._insert(text="вода", due_at=self.now + timedelta(days=1, hours=index)) for index in range(2)]
        pending = self.service.intercept_contract(
            user_id="u1", contract=self._contract("delete", selector={"query": "вода", "scope": "all"}),
            text="видали всі", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        self.assertTrue(all(row["status"] == "pending" for row in self._rows()))
        token = re.search(r"[a-f0-9]{32}", pending.answer).group(0)
        confirmed = self.service.intercept_contract(
            user_id="u1", contract=self._contract("confirm_bulk", selector={"session_token": token}),
            text=f"підтвердити {token}", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        self.assertIn("Скасовано нагадувань: 2", confirmed.answer)
        self.assertEqual(ids, [row["id"] for row in self._rows()])
        self.assertTrue(all(row["status"] == "cancelled" for row in self._rows()))

    def test_scope_only_bulk_delete_lists_stable_ids_before_confirmation(self) -> None:
        ids = [
            self._insert(text="вода", due_at=self.now + timedelta(days=1, hours=index))
            for index in range(2)
        ]

        pending = self.service.intercept_contract(
            user_id="u1", contract=self._contract("delete", selector={"scope": "all"}),
            text="видали всі", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )

        self.assertTrue(all(f"#{reminder_id}" in pending.answer for reminder_id in ids))
        self.assertRegex(pending.answer, r"[a-f0-9]{32}")
        self.assertTrue(all(row["status"] == "pending" for row in self._rows()))

    def test_management_refuses_more_than_one_hundred_candidates(self) -> None:
        with self.database.session() as connection:
            connection.executemany(
                "INSERT INTO reminders(user_id,text,due_at,timezone,status,delivery_route_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        "u1", f"масове нагадування {index}",
                        (self.now + timedelta(days=1, minutes=index)).isoformat(), "Europe/Kyiv", "pending",
                        json.dumps(self.DEFAULT_ROUTE), self.now.isoformat(), self.now.isoformat(),
                    )
                    for index in range(101)
                ],
            )
            connection.commit()
        result = self.service.intercept_contract(
            user_id="u1", contract=self._contract("delete", selector={"query": "масове нагадування", "scope": "all"}),
            text="видали всі", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        self.assertIn("понад 100", result.answer)
        with self.database.session() as connection:
            session_count = connection.execute("SELECT count(*) FROM reminder_management_sessions").fetchone()[0]
        self.assertEqual(0, session_count)

    def test_management_session_is_route_scoped_and_revision_safe(self) -> None:
        reminder_id = self._insert(text="ранкова вода", due_at=self.now + timedelta(days=1))
        pending = self.service.intercept_contract(
            user_id="u1", contract=self._contract("delete", selector={"query": "вода", "scope": "one"}),
            text="видали воду", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        token = re.search(r"[a-f0-9]{32}", pending.answer).group(0)
        wrong_route = {"target_channel": "telegram", "target_chat_id": "777"}
        refused = self.service.intercept_contract(
            user_id="u1", contract=self._contract("select", selector={"session_token": token, "ordinal": 1}),
            text="select", delivery_route=wrong_route, now=self.now + timedelta(minutes=1),
        )
        self.assertIn("не знайдена", refused.answer)
        with self.database.session() as connection:
            connection.execute(
                "UPDATE reminders SET updated_at=? WHERE id=?",
                ((self.now + timedelta(seconds=1)).isoformat(), reminder_id),
            )
            connection.commit()
        stale = self.service.intercept_contract(
            user_id="u1", contract=self._contract("select", selector={"session_token": token, "ordinal": 1}),
            text="select", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        self.assertIn("Стан нагадування змінився", stale.answer)
        self.assertEqual("pending", self._rows()[0]["status"])

    async def test_update_cancels_active_explicit_snooze_child(self) -> None:
        reminder_id = self._insert(text="snooze update", due_at=self.now, recurrence_kind="daily")
        await self.service.run_due(now=self.now)
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("snooze", selector={"id": reminder_id}),
            text="snooze", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=1),
        )
        self.service.intercept_contract(
            user_id="u1", contract=self._contract("update", selector={"id": reminder_id}, patch={"time": "09:00"}),
            text="update", delivery_route=self.DEFAULT_ROUTE, now=self.now + timedelta(minutes=2),
        )
        root = next(row for row in self._rows() if row["id"] == reminder_id)
        child = next(row for row in self._rows() if row["parent_reminder_id"] == reminder_id)
        self.assertEqual("pending", root["status"])
        self.assertEqual("09:00", datetime.fromisoformat(root["due_at"]).strftime("%H:%M"))
        self.assertEqual("cancelled", child["status"])

    def test_calendar_projected_reminder_is_read_only_for_management(self) -> None:
        reminder_id = self.service.upsert_calendar_reminder(
            user_id="u1", source_ref="ha_calendar:calendar.primary:readonly", text="зустріч",
            notification_at=self.now + timedelta(hours=1), delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        result = self.service.intercept_contract(
            user_id="u1", contract=self._contract("delete", selector={"id": reminder_id}),
            text="видали", delivery_route=self.DEFAULT_ROUTE, now=self.now,
        )
        self.assertIn("керується календарем", result.answer)
        self.assertEqual("pending", self._rows()[0]["status"])

    def test_legacy_v31_row_has_no_parent_and_remains_route_less(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "legacy-v31.db")
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, text TEXT NOT NULL,
                    due_at TEXT NOT NULL, timezone TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    recurrence_kind TEXT NULL, recurrence_weekday INTEGER NULL, repeat_until TEXT NULL,
                    delivery_id TEXT NULL, delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    delivery_lease_until TEXT NULL, confirmation_deadline_at TEXT NULL,
                    delivery_route_json TEXT NULL, legacy_source_id INTEGER NULL UNIQUE,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO reminders(user_id,text,due_at,timezone,status,created_at,updated_at)
                VALUES ('legacy','old','2026-07-20T09:00:00+03:00','Europe/Kyiv','pending','2026-07-20T08:00:00+03:00','2026-07-20T08:00:00+03:00');
                """
            )
            connection.commit()
            connection.close()
            Database(db_path).init()
            migrated = sqlite3.connect(db_path)
            migrated.row_factory = sqlite3.Row
            row = migrated.execute("SELECT parent_reminder_id,delivery_route_json,source_ref FROM reminders").fetchone()
            migrated.close()
        self.assertIsNone(row["parent_reminder_id"])
        self.assertIsNone(row["delivery_route_json"])
        self.assertIsNone(row["source_ref"])


class DatabaseMigrationTests(unittest.TestCase):
    def test_v31_reminders_migrate_to_v34_with_projection_columns_and_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "legacy.db")
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
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
                    legacy_source_id INTEGER NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO reminders(user_id,text,due_at,timezone,status,created_at,updated_at)
                VALUES ('legacy-user','legacy reminder','2026-07-20T09:00:00+03:00','Europe/Kyiv','pending','2026-07-20T08:00:00+03:00','2026-07-20T08:00:00+03:00');
                """
            )
            connection.commit()
            connection.close()

            Database(db_path).init()

            migrated = sqlite3.connect(db_path)
            migrated.row_factory = sqlite3.Row
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(reminders)")}
            row = migrated.execute("SELECT user_id,text,delivery_route_json,source_ref FROM reminders").fetchone()
            version = migrated.execute("SELECT version FROM schema_version").fetchone()[0]
            indexes = {row["name"] for row in migrated.execute("PRAGMA index_list(reminders)")}
            migrated.close()

        self.assertIn("delivery_route_json", columns)
        self.assertIn("parent_reminder_id", columns)
        self.assertIn("source_ref", columns)
        self.assertIn("next_attempt_at", columns)
        self.assertIn("idx_reminders_parent_status", indexes)
        self.assertIn("idx_reminders_one_active_child", indexes)
        self.assertIn("idx_reminders_user_source_ref", indexes)
        self.assertEqual(34, version)
        self.assertEqual("legacy-user", row["user_id"])
        self.assertEqual("legacy reminder", row["text"])
        self.assertIsNone(row["delivery_route_json"])
        self.assertIsNone(row["source_ref"])


if __name__ == "__main__":
    unittest.main()
