"""Isolated SQLite-backed reminder state machine."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable, Mapping

from lumen.services.delivery import DeliveryService
from lumen.services.reminder_wake import WindowsWakeAdapter
from lumen.timezones import kyiv_timezone


KYIV = kyiv_timezone()
CONFIRMATION_TIMEOUT = timedelta(minutes=15)
USER_RETRY_DELAY = timedelta(minutes=15)
DELIVERY_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))
MAX_REMINDERS_PER_MESSAGE = 10
DELIVERY_ROUTE_FIELDS = frozenset({"target_channel", "target_chat_id"})
SUPPORTED_DELIVERY_CHANNELS = frozenset({"telegram"})
_CANONICAL_CHAT_ID = re.compile(r"-?[1-9][0-9]*$")


def normalize_delivery_route(value: Mapping[str, object] | object | None) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != DELIVERY_ROUTE_FIELDS:
        return None
    channel = value.get("target_channel")
    target = value.get("target_chat_id")
    if not isinstance(channel, str) or channel not in SUPPORTED_DELIVERY_CHANNELS:
        return None
    if isinstance(target, bool) or not isinstance(target, (int, str)):
        return None
    target_value = str(target).strip()
    if not _CANONICAL_CHAT_ID.fullmatch(target_value):
        return None
    try:
        numeric_target = int(target_value)
    except ValueError:
        return None
    if numeric_target == 0 or not -(2**63) <= numeric_target < 2**63:
        return None
    if str(numeric_target) != target_value:
        return None
    return {"target_channel": channel, "target_chat_id": target_value}


@dataclass(frozen=True)
class ReminderDraft:
    text: str
    due_at: datetime
    recurrence_kind: str | None = None
    recurrence_weekday: int | None = None
    repeat_until: datetime | None = None


@dataclass(frozen=True)
class ReminderInterception:
    answer: str
    handled: bool = True


class ReminderService:
    """Own reminder state; chat only asks it to intercept reminder-related input."""

    def __init__(
        self,
        session_factory: Callable[[], object],
        delivery_service: DeliveryService | None = None,
        wake_adapter: WindowsWakeAdapter | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._delivery = delivery_service
        self._wake = wake_adapter
        self._logger = logger or logging.getLogger("lumen")
        self._list_snapshots: dict[str, tuple[tuple[int, str, str], ...]] = {}

    def intercept_contract(
        self,
        *,
        user_id: str,
        contract: Mapping[str, object],
        text: str,
        metadata: dict[str, object] | None = None,
        delivery_route: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> ReminderInterception | None:
        current = self._now(now)
        metadata = metadata or {}
        reminder = contract.get("reminder")
        reminder_contract = reminder if isinstance(reminder, Mapping) else {}
        confirmation = self._confirmation_answer(metadata, reminder_contract)
        if confirmation is not None:
            structured = metadata.get("reminder_id") is not None or metadata.get("delivery_id") is not None
            if structured or self._awaiting(user_id, None):
                return self._handle_confirmation(user_id, confirmation, metadata, current)
            return None
        action = str(reminder_contract.get("action", "none"))
        if action == "list":
            return ReminderInterception(self._list_active(user_id))
        if action == "cancel_list_item":
            return self._cancel_list_item(user_id, reminder_contract.get("delete_ordinal"), current)
        if action != "create":
            return None
        parsed = self._drafts_from_contract(reminder_contract.get("items"), current)
        if isinstance(parsed, str):
            return ReminderInterception(parsed)
        try:
            self._insert_many(user_id, parsed, current, delivery_route)
        except sqlite3.Error:
            return ReminderInterception("Не вдалося записати нагадування; жодне з них не було створено.")
        self._recalculate_wake()
        lines = [
            f"{index}. {draft.due_at.astimezone(KYIV).strftime('%d.%m.%Y о %H:%M')} — {draft.text}"
            for index, draft in enumerate(parsed, start=1)
        ]
        return ReminderInterception("Нагадування записано:\n" + "\n".join(lines))

    @classmethod
    def _drafts_from_contract(cls, items: object, now: datetime) -> list[ReminderDraft] | str:
        if not isinstance(items, list) or not items:
            return "Не вдалося однозначно визначити нагадування. Уточни дату, час і дію."
        if len(items) > MAX_REMINDERS_PER_MESSAGE:
            return f"За один раз можна створити не більше {MAX_REMINDERS_PER_MESSAGE} нагадувань."
        drafts: list[ReminderDraft] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, Mapping):
                return f"Пункт {index}: потрібні структуровані дата, час і дія."
            try:
                text = str(item["text"]).strip()
                due_date = date.fromisoformat(str(item["date"]).strip())
                due_time = time.fromisoformat(str(item["time"]).strip())
            except (KeyError, TypeError, ValueError):
                return f"Пункт {index}: потрібні коректні дата ISO, час і дія."
            if not text or len(text) > 1000 or due_time.second or due_time.microsecond:
                return f"Пункт {index}: потрібні коректні дата, час до хвилини і дія."
            kind_value = item.get("recurrence_kind")
            kind = None if kind_value in (None, "", "none") else str(kind_value)
            weekday = item.get("recurrence_weekday")
            if kind not in (None, "daily", "weekly"):
                return f"Пункт {index}: підтримуються лише daily або weekly повтори."
            if kind == "weekly" and (not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6):
                return f"Пункт {index}: weekly повтор потребує дня тижня."
            if kind != "weekly" and weekday not in (None, ""):
                return f"Пункт {index}: день тижня можливий лише для weekly повтору."
            try:
                until_value = item.get("repeat_until")
                until = datetime.combine(date.fromisoformat(str(until_value)), time.max, tzinfo=KYIV) if until_value else None
            except ValueError:
                return f"Пункт {index}: некоректна дата завершення повторення."
            due_at = datetime.combine(due_date, due_time, tzinfo=KYIV)
            if kind:
                while due_at <= now:
                    due_at += timedelta(days=1 if kind == "daily" else 7)
            elif due_at <= now:
                return f"Пункт {index}: час має бути в майбутньому."
            if until and due_at > until:
                return f"Пункт {index}: дата завершення повторення раніше за перше нагадування."
            drafts.append(ReminderDraft(text, due_at, kind, weekday if kind == "weekly" else None, until))
        seen: set[tuple[object, ...]] = set()
        for draft in drafts:
            key = (draft.text.casefold(), draft.due_at.isoformat(), draft.recurrence_kind, draft.recurrence_weekday, draft.repeat_until.isoformat() if draft.repeat_until else None)
            if key in seen:
                return "У повідомленні є однакові нагадування. Уточни їх, щоб не створювати дублікати."
            seen.add(key)
        return drafts

    async def run_due(self, *, now: datetime | None = None) -> int:
        current = self._now(now)
        self._recover_interrupted(current)
        self._expire_confirmations(current)
        rows = self._claim_due(current)
        for row in rows:
            await self._deliver_claimed(row, current)
        self._recalculate_wake()
        return len(rows)

    def _handle_confirmation(self, user_id: str, answer: str, metadata: dict[str, object], now: datetime) -> ReminderInterception:
        reminder_id = metadata.get("reminder_id")
        delivery_id = metadata.get("delivery_id")
        structured = reminder_id is not None or delivery_id is not None
        if structured and (reminder_id is None or not str(delivery_id or "").strip()):
            return ReminderInterception("Підтвердження нагадування не містить актуального delivery_id.")
        rows = self._awaiting(user_id, reminder_id)
        if not rows:
            if structured and self._is_completed_delivery(user_id, reminder_id, str(delivery_id)):
                return ReminderInterception("Підтвердження цього нагадування вже застосовано.")
            return ReminderInterception("Немає активного нагадування, яке очікує підтвердження.")
        if len(rows) > 1 and reminder_id is None:
            return ReminderInterception("Є кілька нагадувань, що очікують підтвердження. Відповідай кнопкою конкретного нагадування.")
        row = rows[0]
        if structured and str(row["delivery_id"] or "") != str(delivery_id):
            return ReminderInterception("Це підтвердження належить застарілій доставці нагадування і не було застосоване.")
        if answer == "yes":
            next_due = self._complete_confirmed_occurrence(row, user_id, now)
            if next_due is False:
                return self._confirmation_conflict(user_id, row["id"], row["delivery_id"])
            self._recalculate_wake()
            return ReminderInterception("Дякую, нагадування завершено." if next_due is None else "Дякую, наступне повторюване нагадування заплановано.")
        if not self._snooze_confirmed_occurrence(row, user_id, now):
            return self._confirmation_conflict(user_id, row["id"], row["delivery_id"])
        self._recalculate_wake()
        return ReminderInterception("Добре, повторю нагадування через 15 хвилин.")

    async def _deliver_claimed(self, row: object, now: datetime) -> None:
        delivery_id = str(row["delivery_id"])
        if not self._delivery_claim_is_current(row["id"], delivery_id):
            return
        if self._delivery is None:
            self._schedule_delivery_retry(row, now, "delivery unavailable")
            return
        delivery_route = self._delivery_route_from_row(row)
        if delivery_route is None:
            self._logger.warning(
                "Reminder delivery deferred id=%s: persisted delivery route is unavailable",
                row["id"],
            )
            self._schedule_delivery_retry(
                row,
                now,
                "delivery route unavailable",
                retry_delay=USER_RETRY_DELAY,
            )
            return
        try:
            await self._delivery.deliver_reminder(
                reminder_id=int(row["id"]), user_id=str(row["user_id"]), answer=str(row["text"]), request_id=delivery_id,
                metadata={
                    "reminder_id": int(row["id"]),
                    "delivery_id": delivery_id,
                    "request_id": delivery_id,
                    "confirmation_required": True,
                    **delivery_route,
                },
            )
        except Exception as exc:
            self._logger.warning("Reminder delivery failed id=%s: %s", row["id"], exc)
            self._schedule_delivery_retry(row, now, str(exc))
            return
        with self._session_factory() as connection:
            connection.execute("UPDATE reminders SET status='awaiting_confirmation', delivery_lease_until=NULL, confirmation_deadline_at=?, updated_at=? WHERE id=? AND status='delivering' AND delivery_id=?", ((now + CONFIRMATION_TIMEOUT).isoformat(), now.isoformat(), row["id"], delivery_id))
            connection.commit()

    def _schedule_delivery_retry(
        self,
        row: object,
        now: datetime,
        error: str,
        *,
        retry_delay: timedelta | None = None,
    ) -> None:
        attempts = int(row["delivery_attempts"])
        with self._session_factory() as connection:
            if attempts >= len(DELIVERY_RETRY_DELAYS):
                cursor = connection.execute("UPDATE reminders SET status='failed', delivery_lease_until=NULL, updated_at=? WHERE id=? AND status='delivering' AND delivery_id=?", (now.isoformat(), row["id"], row["delivery_id"]))
                if cursor.rowcount and row["parent_reminder_id"] is not None:
                    self._resolve_failed_child(connection, row, now)
            else:
                delay = retry_delay or DELIVERY_RETRY_DELAYS[attempts - 1]
                cursor = connection.execute("UPDATE reminders SET status='pending', due_at=?, delivery_lease_until=NULL, updated_at=? WHERE id=? AND status='delivering' AND delivery_id=?", ((now + delay).isoformat(), now.isoformat(), row["id"], row["delivery_id"]))
            connection.commit()
        if cursor.rowcount:
            self._logger.warning("Reminder delivery retry id=%s attempts=%s error=%s", row["id"], attempts, error)

    def _resolve_failed_child(self, connection: object, row: object, now: datetime) -> None:
        root = connection.execute(
            "SELECT * FROM reminders WHERE id=? AND user_id=? AND status='snoozed'",
            (row["parent_reminder_id"], row["user_id"]),
        ).fetchone()
        if root is None:
            return
        next_due = self._next_occurrence(root, after=now)
        if next_due is None:
            connection.execute(
                "UPDATE reminders SET status='failed', delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                "WHERE id=? AND status='snoozed'",
                (now.isoformat(), root["id"]),
            )
            return
        connection.execute(
            "UPDATE reminders SET status='pending', due_at=?, delivery_id=NULL, delivery_attempts=0, "
            "delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
            "WHERE id=? AND status='snoozed'",
            (next_due.isoformat(), now.isoformat(), root["id"]),
        )

    def _insert_many(
        self,
        user_id: str,
        drafts: list[ReminderDraft],
        now: datetime,
        delivery_route: Mapping[str, object] | None,
    ) -> list[int]:
        normalized_route = self._normalize_delivery_route(delivery_route)
        route_json = json.dumps(normalized_route, separators=(",", ":"), sort_keys=True) if normalized_route else None
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN")
                reminder_ids: list[int] = []
                for draft in drafts:
                    cursor = connection.execute(
                        "INSERT INTO reminders(user_id,text,due_at,timezone,status,recurrence_kind,recurrence_weekday,repeat_until,delivery_route_json,created_at,updated_at) VALUES (?,?,?,?, 'pending',?,?,?,?,?,?)",
                        (user_id, draft.text, draft.due_at.isoformat(), "Europe/Kyiv", draft.recurrence_kind, draft.recurrence_weekday, draft.repeat_until.isoformat() if draft.repeat_until else None, route_json, now.isoformat(), now.isoformat()),
                    )
                    reminder_ids.append(int(cursor.lastrowid))
                connection.commit()
                return reminder_ids
            except sqlite3.Error:
                connection.rollback()
                raise

    def upsert_calendar_reminder(
        self,
        *,
        user_id: str,
        source_ref: str,
        text: str,
        notification_at: datetime,
        delivery_route: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> int:
        """Project one calendar occurrence into a pending one-time reminder."""
        normalized_source_ref = self._calendar_source_ref(source_ref)
        reminder_text = str(text).strip()
        if not reminder_text:
            raise ValueError("Calendar reminder text must not be empty.")
        if notification_at.tzinfo is None or notification_at.utcoffset() is None:
            raise ValueError("Calendar reminder notification_at must be timezone-aware.")
        due_at = notification_at.astimezone(KYIV)
        current = self._now(now)
        normalized_route = self._normalize_delivery_route(delivery_route)
        route_json = json.dumps(normalized_route, separators=(",", ":"), sort_keys=True) if normalized_route else None
        changed = False
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM reminders WHERE user_id=? AND source_ref=?",
                    (user_id, normalized_source_ref),
                ).fetchone()
                if row is None:
                    cursor = connection.execute(
                        "INSERT INTO reminders("
                        "user_id,text,due_at,timezone,status,recurrence_kind,recurrence_weekday,repeat_until,"
                        "delivery_route_json,parent_reminder_id,source_ref,created_at,updated_at"
                        ") VALUES (?,?,?,?, 'pending',NULL,NULL,NULL,?,NULL,?,?,?)",
                        (
                            user_id,
                            reminder_text,
                            due_at.isoformat(),
                            "Europe/Kyiv",
                            route_json,
                            normalized_source_ref,
                            current.isoformat(),
                            current.isoformat(),
                        ),
                    )
                    reminder_id = int(cursor.lastrowid)
                    changed = True
                else:
                    reminder_id = int(row["id"])
                    mutable = row["parent_reminder_id"] is None and row["status"] == "pending"
                    changed = mutable and (
                        row["text"] != reminder_text
                        or row["due_at"] != due_at.isoformat()
                        or row["timezone"] != "Europe/Kyiv"
                        or row["delivery_route_json"] != route_json
                    )
                    if changed:
                        connection.execute(
                            "UPDATE reminders SET text=?,due_at=?,timezone=?,delivery_route_json=?,"
                            "delivery_id=NULL,delivery_attempts=0,delivery_lease_until=NULL,"
                            "confirmation_deadline_at=NULL,updated_at=? "
                            "WHERE id=? AND parent_reminder_id IS NULL AND status='pending'",
                            (
                                reminder_text,
                                due_at.isoformat(),
                                "Europe/Kyiv",
                                route_json,
                                current.isoformat(),
                                reminder_id,
                            ),
                        )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        if changed:
            self._recalculate_wake()
        return reminder_id

    def cancel_calendar_reminder(
        self,
        *,
        user_id: str,
        source_ref: str,
        now: datetime | None = None,
    ) -> bool:
        """Cancel a calendar projection only while its root is still pending."""
        normalized_source_ref = self._calendar_source_ref(source_ref)
        current = self._now(now)
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE reminders SET status='cancelled',delivery_lease_until=NULL,"
                    "confirmation_deadline_at=NULL,updated_at=? "
                    "WHERE user_id=? AND source_ref=? AND parent_reminder_id IS NULL AND status='pending'",
                    (current.isoformat(), user_id, normalized_source_ref),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        changed = bool(cursor.rowcount)
        if changed:
            self._recalculate_wake()
        return changed

    def reconcile_calendar_reminders(
        self,
        *,
        user_id: str,
        source_prefix: str,
        active_source_refs: Iterable[str],
        now: datetime | None = None,
    ) -> int:
        """Cancel pending calendar roots absent from one authoritative snapshot."""
        normalized_prefix = self._calendar_source_ref(source_prefix)
        active_refs = {self._calendar_source_ref(value) for value in active_source_refs}
        if any(not value.startswith(normalized_prefix) for value in active_refs):
            raise ValueError("Calendar snapshot source_ref is outside source_prefix.")
        current = self._now(now)
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                candidates = connection.execute(
                    "SELECT id,source_ref FROM reminders "
                    "WHERE user_id=? AND parent_reminder_id IS NULL AND status='pending' "
                    "AND source_ref IS NOT NULL AND substr(source_ref,1,?)=?",
                    (user_id, len(normalized_prefix), normalized_prefix),
                ).fetchall()
                stale_ids = [int(row["id"]) for row in candidates if row["source_ref"] not in active_refs]
                changed = 0
                for reminder_id in stale_ids:
                    cursor = connection.execute(
                        "UPDATE reminders SET status='cancelled',delivery_lease_until=NULL,"
                        "confirmation_deadline_at=NULL,updated_at=? "
                        "WHERE id=? AND user_id=? AND parent_reminder_id IS NULL AND status='pending'",
                        (current.isoformat(), reminder_id, user_id),
                    )
                    changed += cursor.rowcount
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        if changed:
            self._recalculate_wake()
        return changed

    @staticmethod
    def _calendar_source_ref(value: str) -> str:
        normalized = str(value).strip()
        if not normalized or len(normalized) > 1000:
            raise ValueError("Calendar reminder source_ref must contain 1 to 1000 characters.")
        return normalized

    def _complete_confirmed_occurrence(
        self,
        row: object,
        user_id: str,
        now: datetime,
    ) -> datetime | None | bool:
        parent_id = row["parent_reminder_id"]
        root_id = int(parent_id) if parent_id is not None else int(row["id"])
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN")
                root = connection.execute(
                    "SELECT * FROM reminders WHERE id=? AND user_id=?",
                    (root_id, user_id),
                ).fetchone()
                if root is None:
                    connection.rollback()
                    return False
                if parent_id is None:
                    occurrence = connection.execute(
                        "UPDATE reminders SET delivery_lease_until=NULL, updated_at=? "
                        "WHERE id=? AND user_id=? AND status='awaiting_confirmation' AND delivery_id=?",
                        (now.isoformat(), row["id"], user_id, row["delivery_id"]),
                    )
                else:
                    occurrence = connection.execute(
                        "UPDATE reminders SET status='completed', delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                        "WHERE id=? AND user_id=? AND status='awaiting_confirmation' AND delivery_id=?",
                        (now.isoformat(), row["id"], user_id, row["delivery_id"]),
                    )
                if not occurrence.rowcount:
                    connection.rollback()
                    return self._snooze_is_idempotently_applied(root_id, user_id)
                next_due = self._next_occurrence(root, after=now)
                expected_status = "awaiting_confirmation" if parent_id is None else "snoozed"
                if next_due is None:
                    root_update = connection.execute(
                        "UPDATE reminders SET status='completed', delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                        "WHERE id=? AND user_id=? AND status=?",
                        (now.isoformat(), root_id, user_id, expected_status),
                    )
                else:
                    root_update = connection.execute(
                        "UPDATE reminders SET status='pending', due_at=?, delivery_id=NULL, delivery_attempts=0, "
                        "delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                        "WHERE id=? AND user_id=? AND status=?",
                        (next_due.isoformat(), now.isoformat(), root_id, user_id, expected_status),
                    )
                if not root_update.rowcount:
                    connection.rollback()
                    return False
                connection.commit()
                return next_due
            except sqlite3.Error:
                connection.rollback()
                return False

    def _snooze_confirmed_occurrence(self, row: object, user_id: str, now: datetime) -> bool:
        parent_id = row["parent_reminder_id"]
        root_id = int(parent_id) if parent_id is not None else int(row["id"])
        retry_at = now + USER_RETRY_DELAY
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN")
                root = connection.execute(
                    "SELECT * FROM reminders WHERE id=? AND user_id=?",
                    (root_id, user_id),
                ).fetchone()
                if root is None:
                    connection.rollback()
                    return False
                if parent_id is None:
                    occurrence = connection.execute(
                        "UPDATE reminders SET status='snoozed', delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                        "WHERE id=? AND user_id=? AND status='awaiting_confirmation' AND delivery_id=?",
                        (now.isoformat(), row["id"], user_id, row["delivery_id"]),
                    )
                else:
                    occurrence = connection.execute(
                        "UPDATE reminders SET status='completed', delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                        "WHERE id=? AND user_id=? AND status='awaiting_confirmation' AND delivery_id=?",
                        (now.isoformat(), row["id"], user_id, row["delivery_id"]),
                    )
                if not occurrence.rowcount:
                    connection.rollback()
                    return self._snooze_is_idempotently_applied(root_id, user_id)
                active_child = connection.execute(
                    "SELECT 1 FROM reminders WHERE parent_reminder_id=? "
                    "AND status IN ('pending','delivering','awaiting_confirmation')",
                    (root_id,),
                ).fetchone()
                if active_child is not None:
                    connection.rollback()
                    return self._snooze_is_idempotently_applied(root_id, user_id)
                route = self._delivery_route_from_row(root)
                route_json = json.dumps(route, separators=(",", ":"), sort_keys=True) if route else None
                connection.execute(
                    "INSERT INTO reminders(user_id,text,due_at,timezone,status,recurrence_kind,recurrence_weekday,repeat_until,"
                    "delivery_route_json,parent_reminder_id,created_at,updated_at) "
                    "VALUES (?,?,?,?, 'pending',NULL,NULL,NULL,?,?,?,?)",
                    (
                        user_id,
                        root["text"],
                        retry_at.isoformat(),
                        root["timezone"],
                        route_json,
                        root_id,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.commit()
                return True
            except sqlite3.Error as exc:
                connection.rollback()
                if self._is_snooze_contention(exc) and self._snooze_is_idempotently_applied(root_id, user_id):
                    return True
                raise

    def _snooze_is_idempotently_applied(self, root_id: int, user_id: str) -> bool:
        with self._session_factory() as connection:
            root = connection.execute(
                "SELECT status FROM reminders WHERE id=? AND user_id=?",
                (root_id, user_id),
            ).fetchone()
            active_children = connection.execute(
                "SELECT count(*) FROM reminders WHERE parent_reminder_id=? "
                "AND status IN ('pending','delivering','awaiting_confirmation')",
                (root_id,),
            ).fetchone()[0]
        return root is not None and root["status"] == "snoozed" and active_children == 1

    @staticmethod
    def _is_snooze_contention(exc: sqlite3.Error) -> bool:
        if isinstance(exc, sqlite3.IntegrityError):
            return True
        return isinstance(exc, sqlite3.OperationalError) and any(
            marker in str(exc).lower() for marker in ("locked", "busy")
        )

    def _cancel_list_item(self, user_id: str, ordinal: object, now: datetime) -> ReminderInterception:
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            return ReminderInterception("Вкажи номер зі щойно показаного списку, наприклад #2.")
        snapshot = self._list_snapshots.get(user_id)
        if snapshot is None or ordinal > len(snapshot):
            return ReminderInterception("Цей список уже неактуальний. Спершу покажи свої активні нагадування ще раз.")
        reminder_id, due_at, updated_at = snapshot[ordinal - 1]
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN")
                cursor = connection.execute(
                    "UPDATE reminders SET status='cancelled', delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                    "WHERE id=? AND user_id=? AND due_at=? AND updated_at=? "
                    "AND parent_reminder_id IS NULL "
                    "AND status IN ('pending','delivering','awaiting_confirmation','snoozed')",
                    (now.isoformat(), reminder_id, user_id, due_at, updated_at),
                )
                if cursor.rowcount:
                    connection.execute(
                        "UPDATE reminders SET status='cancelled', delivery_lease_until=NULL, confirmation_deadline_at=NULL, updated_at=? "
                        "WHERE parent_reminder_id=? AND status IN ('pending','delivering','awaiting_confirmation')",
                        (now.isoformat(), reminder_id),
                    )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                return ReminderInterception("Не вдалося скасувати нагадування; жодне не було змінено.")
        self._list_snapshots.pop(user_id, None)
        if not cursor.rowcount:
            return ReminderInterception("Це нагадування вже неактивне. Покажи список ще раз, щоб вибрати інше.")
        self._recalculate_wake()
        return ReminderInterception(f"Скасовано нагадування #{ordinal}.")

    def _delivery_claim_is_current(self, reminder_id: object, delivery_id: str) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET updated_at=updated_at WHERE id=? AND status='delivering' AND delivery_id=?",
                (reminder_id, delivery_id),
            )
            connection.commit()
        return bool(cursor.rowcount)

    @classmethod
    def _delivery_route_from_row(cls, row: object) -> dict[str, str] | None:
        raw_route = row["delivery_route_json"]
        if not raw_route:
            return None
        try:
            parsed = json.loads(str(raw_route))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return cls._normalize_delivery_route(parsed)

    @staticmethod
    def _normalize_delivery_route(value: Mapping[str, object] | object | None) -> dict[str, str] | None:
        return normalize_delivery_route(value)

    def _list_active(self, user_id: str) -> str:
        with self._session_factory() as connection:
            rows = connection.execute(
                "SELECT root.id,root.text,COALESCE((SELECT child.due_at FROM reminders AS child "
                "WHERE child.parent_reminder_id=root.id "
                "AND child.status IN ('pending','delivering','awaiting_confirmation') "
                "ORDER BY child.due_at,child.id LIMIT 1), root.due_at) AS due_at, "
                "root.due_at AS root_due_at,root.updated_at,root.status,root.recurrence_kind FROM reminders AS root "
                "WHERE root.user_id=? AND root.parent_reminder_id IS NULL "
                "AND root.status IN ('pending','delivering','awaiting_confirmation','snoozed') "
                "ORDER BY due_at,root.id LIMIT 11",
                (user_id,),
            ).fetchall()
        if not rows:
            self._list_snapshots.pop(user_id, None)
            return "Активних нагадувань немає."
        truncated = len(rows) > 10
        self._list_snapshots[user_id] = tuple(
            (int(row["id"]), str(row["root_due_at"]), str(row["updated_at"])) for row in rows[:10]
        )
        lines = []
        for index, row in enumerate(rows[:10], start=1):
            due_at = datetime.fromisoformat(row["due_at"]).astimezone(KYIV).strftime("%d.%m.%Y о %H:%M")
            suffix = " (щодня)" if row["recurrence_kind"] == "daily" else " (щотижня)" if row["recurrence_kind"] == "weekly" else ""
            lines.append(f"{index}. {row['text']} — {due_at}{suffix}")
        if truncated:
            lines.append("Є ще активні нагадування; показано перші 10.")
        return "Активні нагадування:\n" + "\n".join(lines)

    def _claim_due(self, now: datetime) -> list[object]:
        with self._session_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM reminders AS candidate "
                "WHERE (candidate.status='pending' OR (candidate.status='delivering' AND candidate.delivery_lease_until <= ?)) "
                "AND candidate.due_at <= ? "
                "AND (candidate.parent_reminder_id IS NOT NULL OR NOT EXISTS ("
                "SELECT 1 FROM reminders AS child WHERE child.parent_reminder_id=candidate.id "
                "AND child.status IN ('pending','delivering','awaiting_confirmation'))) "
                "ORDER BY candidate.due_at, candidate.id LIMIT 100",
                (now.isoformat(), now.isoformat()),
            ).fetchall()
            claimed: list[object] = []
            for row in rows:
                delivery_id = secrets.token_hex(16)
                cursor = connection.execute("UPDATE reminders SET status='delivering', delivery_id=?, delivery_attempts=delivery_attempts+1, delivery_lease_until=?, updated_at=? WHERE id=? AND (status='pending' OR (status='delivering' AND delivery_lease_until <= ?))", (delivery_id, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), row["id"], now.isoformat()))
                if cursor.rowcount:
                    claimed.append(connection.execute("SELECT * FROM reminders WHERE id=?", (row["id"],)).fetchone())
            connection.commit()
        return claimed

    def _recover_interrupted(self, now: datetime) -> None:
        # Expired delivery leases are reclaimed by _claim_due; a live worker is never reset.
        del now

    def _expire_confirmations(self, now: datetime) -> None:
        with self._session_factory() as connection:
            rows = connection.execute(
                "SELECT * FROM reminders WHERE status='awaiting_confirmation' "
                "AND confirmation_deadline_at <= ? ORDER BY id",
                (now.isoformat(),),
            ).fetchall()
        for row in rows:
            self._snooze_confirmed_occurrence(row, str(row["user_id"]), now)

    def _awaiting(self, user_id: str, reminder_id: object) -> list[object]:
        params: list[object] = [user_id]
        sql = "SELECT * FROM reminders WHERE user_id=? AND status='awaiting_confirmation'"
        if reminder_id is not None:
            try:
                params.append(int(reminder_id))
            except (TypeError, ValueError):
                return []
            sql += " AND id=?"
        with self._session_factory() as connection:
            return connection.execute(sql + " ORDER BY id", params).fetchall()

    def _is_completed_delivery(self, user_id: str, reminder_id: object, delivery_id: str) -> bool:
        try:
            identifier = int(reminder_id)
        except (TypeError, ValueError):
            return False
        with self._session_factory() as connection:
            row = connection.execute("SELECT 1 FROM reminders WHERE id=? AND user_id=? AND status='completed' AND delivery_id=?", (identifier, user_id, delivery_id)).fetchone()
        return row is not None

    def _confirmation_conflict(self, user_id: str, reminder_id: object, delivery_id: object) -> ReminderInterception:
        if self._is_completed_delivery(user_id, reminder_id, str(delivery_id)):
            return ReminderInterception("Підтвердження цього нагадування вже застосовано.")
        return ReminderInterception("Стан нагадування вже змінився; підтвердження не було застосоване.")

    def _recalculate_wake(self) -> None:
        if self._wake is None:
            return
        try:
            with self._session_factory() as connection:
                row = connection.execute(
                    "SELECT wake_at FROM ("
                    "SELECT due_at AS wake_at FROM reminders WHERE status='pending' "
                    "UNION ALL "
                    "SELECT confirmation_deadline_at AS wake_at FROM reminders WHERE status='awaiting_confirmation' AND confirmation_deadline_at IS NOT NULL"
                    ") ORDER BY wake_at LIMIT 1"
                ).fetchone()
            due_at = datetime.fromisoformat(row["wake_at"]) if row else None
            self._wake.schedule(due_at)
        except Exception as exc:
            self._logger.warning("Reminder wake scheduling failed; reminder rows are retained: %s", exc)

    @staticmethod
    def _confirmation_answer(metadata: Mapping[str, object], reminder_contract: Mapping[str, object]) -> str | None:
        if metadata.get("reminder_id") is not None and metadata.get("delivery_id") is not None:
            return "yes"
        answer = str(reminder_contract.get("confirmation", "")).strip().lower()
        return answer if answer in {"yes", "no"} else None

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        return (value or datetime.now(KYIV)).astimezone(KYIV)

    def _next_occurrence(self, row: object, *, after: datetime | None = None) -> datetime | None:
        kind = row["recurrence_kind"]
        if not kind:
            return None
        due = datetime.fromisoformat(row["due_at"]).astimezone(kyiv_timezone())
        interval = timedelta(days=1) if kind == "daily" else timedelta(days=7)
        next_due = due + interval
        if after is not None:
            while next_due <= after.astimezone(due.tzinfo):
                next_due += interval
        until = datetime.fromisoformat(row["repeat_until"]) if row["repeat_until"] else None
        if until is not None:
            until = until.replace(tzinfo=due.tzinfo) if until.tzinfo is None else until.astimezone(due.tzinfo)
        return None if until is not None and next_due > until else next_due
