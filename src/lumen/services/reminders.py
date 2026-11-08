"""Isolated SQLite-backed reminder state machine."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable, Mapping

from lumen.services.delivery import DeliveryService
from lumen.services.reminder_wake import WindowsWakeAdapter
from lumen.timezones import kyiv_timezone


KYIV = kyiv_timezone()
CONFIRMATION_TIMEOUT = timedelta(minutes=15)
MISSING_ROUTE_RETRY_DELAY = timedelta(minutes=15)
DELIVERY_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))
MAX_REMINDERS_PER_MESSAGE = 10
MAX_MANAGEMENT_CANDIDATES = 100
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
        missed_grace_minutes: int = 30,
        default_snooze_minutes: int = 15,
        management_session_minutes: int = 10,
        conversation_service: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._delivery = delivery_service
        self._wake = wake_adapter
        self._logger = logger or logging.getLogger("lumen")
        self._missed_grace = timedelta(minutes=max(1, int(missed_grace_minutes)))
        self._default_snooze = timedelta(minutes=max(1, int(default_snooze_minutes)))
        self._management_session_ttl = timedelta(minutes=max(1, int(management_session_minutes)))
        self._conversation_service = conversation_service
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
        if action in {"update", "pause", "resume", "delete", "snooze"}:
            return self._manage(
                user_id=user_id,
                action=action,
                selector=reminder_contract.get("selector"),
                patch=reminder_contract.get("patch"),
                delivery_route=delivery_route,
                now=current,
            )
        if action in {"select", "confirm_bulk"}:
            return self._continue_management(
                user_id=user_id,
                action=action,
                selector=reminder_contract.get("selector"),
                delivery_route=delivery_route,
                now=current,
            )
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
            if kind == "weekly":
                due_at += timedelta(days=(int(weekday) - due_at.weekday()) % 7)
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
        await self._record_awaiting_histories()
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
        if not self._miss_confirmed_occurrence(row, user_id, now):
            return self._confirmation_conflict(user_id, row["id"], row["delivery_id"])
        self._recalculate_wake()
        return ReminderInterception("Добре, цей випадок позначено пропущеним; базовий розклад не змінено.")

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
                retry_delay=MISSING_ROUTE_RETRY_DELAY,
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
            connection.execute("UPDATE reminders SET status='awaiting_confirmation', delivery_lease_until=NULL, next_attempt_at=NULL, confirmation_deadline_at=?, updated_at=? WHERE id=? AND status='delivering' AND delivery_id=?", ((now + CONFIRMATION_TIMEOUT).isoformat(), now.isoformat(), row["id"], delivery_id))
            connection.commit()
        await self._record_reminder_history(row, delivery_id)

    async def _record_awaiting_histories(self) -> None:
        """Retry history writes for delivered reminders without redelivering them."""
        if self._conversation_service is None:
            return
        with self._session_factory() as connection:
            rows = connection.execute(
                "SELECT * FROM reminders WHERE status='awaiting_confirmation' AND delivery_id IS NOT NULL ORDER BY id"
            ).fetchall()
        for row in rows:
            await self._record_reminder_history(row, str(row["delivery_id"]))

    async def _record_reminder_history(self, row: object, delivery_id: str) -> None:
        if self._conversation_service is None:
            return
        delivery_route = self._delivery_route_from_row(row) or {}
        try:
            await self._conversation_service.record_event(
                user_id=str(row["user_id"]),
                role="assistant",
                content=str(row["text"]),
                question="",
                source="reminder",
                kind="reminder_notification",
                event_id=f"reminder:{delivery_id}",
                metadata={"reminder_id": int(row["id"]), "delivery_id": delivery_id, **delivery_route},
            )
        except Exception as exc:
            self._logger.warning(
                "Reminder history recording failed id=%s delivery_id=%s error_type=%s",
                row["id"], delivery_id, type(exc).__name__,
            )

    def _schedule_delivery_retry(
        self,
        row: object,
        now: datetime,
        error: str,
        *,
        retry_delay: timedelta | None = None,
    ) -> None:
        attempts = int(row["delivery_attempts"])
        retry_scheduled = False
        occurrence_missed = False
        with self._session_factory() as connection:
            current = connection.execute(
                "SELECT * FROM reminders WHERE id=? AND status='delivering' AND delivery_id=?",
                (row["id"], row["delivery_id"]),
            ).fetchone()
            if current is None:
                pass
            else:
                delay = retry_delay or DELIVERY_RETRY_DELAYS[min(attempts - 1, len(DELIVERY_RETRY_DELAYS) - 1)]
                retry_at = now + delay
                original_due = datetime.fromisoformat(current["due_at"]).astimezone(KYIV)
                if retry_at > original_due + self._missed_grace:
                    self._miss_row_in_connection(connection, current, now)
                    occurrence_missed = True
                else:
                    cursor = connection.execute(
                        "UPDATE reminders SET status='pending',next_attempt_at=?,delivery_lease_until=NULL,updated_at=? "
                        "WHERE id=? AND status='delivering' AND delivery_id=?",
                        (retry_at.isoformat(), now.isoformat(), row["id"], row["delivery_id"]),
                    )
                    retry_scheduled = bool(cursor.rowcount)
            connection.commit()
        if retry_scheduled:
            self._logger.warning("Reminder delivery retry id=%s attempts=%s error=%s", row["id"], attempts, error)
        elif occurrence_missed:
            self._logger.warning("Reminder delivery abandoned within grace id=%s attempts=%s error=%s", row["id"], attempts, error)

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

    @staticmethod
    def _normalized_text(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return " ".join(text.split())

    def _manage(
        self,
        *,
        user_id: str,
        action: str,
        selector: object,
        patch: object,
        delivery_route: Mapping[str, object] | None,
        now: datetime,
    ) -> ReminderInterception:
        selector_map = selector if isinstance(selector, Mapping) else {}
        patch_map = dict(patch) if isinstance(patch, Mapping) else {}
        candidates, calendar_matches, exact = self._management_candidates(user_id, selector_map)
        if not candidates:
            if calendar_matches:
                return ReminderInterception(
                    "Це нагадування керується календарем. Зміни або видали відповідну подію в календарі."
                )
            return ReminderInterception("Не знайдено активного локального нагадування за цим описом.")
        if len(candidates) > MAX_MANAGEMENT_CANDIDATES:
            return ReminderInterception(
                f"Знайдено понад {MAX_MANAGEMENT_CANDIDATES} нагадувань. Уточни текст або розклад, щоб звузити вибір."
            )

        bulk = str(selector_map.get("scope", "one")) == "all"
        if bulk:
            token = self._create_management_session(
                user_id=user_id,
                operation=f"bulk:{action}",
                rows=candidates,
                patch=patch_map,
                delivery_route=delivery_route,
                now=now,
            )
            stable_ids = ", ".join(f"#{int(row['id'])}" for row in candidates)
            return ReminderInterception(
                f"Знайдено {len(candidates)} нагадувань: {stable_ids}. "
                f"Для підтвердження напиши: підтвердити {token}."
            )
        if len(candidates) != 1 or not exact:
            token = self._create_management_session(
                user_id=user_id,
                operation=f"select:{action}",
                rows=candidates,
                patch=patch_map,
                delivery_route=delivery_route,
                now=now,
            )
            lines = [
                f"{index}. #{row['id']} {row['text']} — {self._format_due(row)}"
                for index, row in enumerate(candidates[:10], start=1)
            ]
            return ReminderInterception(
                "Знайдено кілька або неточний збіг:\n"
                + "\n".join(lines)
                + f"\nДля вибору напиши: обрати {token} #номер."
            )
        return self._apply_management(
            user_id=user_id,
            operation=action,
            targets=[(int(candidates[0]["id"]), str(candidates[0]["updated_at"]))],
            patch=patch_map,
            now=now,
        )

    def _continue_management(
        self,
        *,
        user_id: str,
        action: str,
        selector: object,
        delivery_route: Mapping[str, object] | None,
        now: datetime,
    ) -> ReminderInterception:
        selector_map = selector if isinstance(selector, Mapping) else {}
        token = str(selector_map.get("session_token", "")).strip()
        if not token:
            return ReminderInterception("Укажи код операції, який показав Люмен.")
        session = self._load_management_session(user_id, token, delivery_route, now)
        if session is None:
            return ReminderInterception("Ця операція не знайдена або вже прострочена. Повтори команду.")
        try:
            targets = [(int(item["id"]), str(item["updated_at"])) for item in json.loads(session["candidates_json"])]
            patch = json.loads(session["patch_json"] or "{}")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            self._delete_management_session(token)
            return ReminderInterception("Збережена операція пошкоджена. Повтори команду.")
        stored_operation = str(session["operation"])
        if action == "select":
            if not stored_operation.startswith("select:"):
                return ReminderInterception("Цей код призначений для іншого типу підтвердження.")
            ordinal = selector_map.get("ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= len(targets):
                return ReminderInterception("Укажи коректний номер зі списку цієї операції.")
            targets = [targets[ordinal - 1]]
        elif action != "confirm_bulk" or not stored_operation.startswith("bulk:") or len(targets) < 1:
            return ReminderInterception("Некоректне підтвердження операції.")
        result = self._apply_management(
            user_id=user_id,
            operation=stored_operation.split(":", 1)[1],
            targets=targets,
            patch=patch if isinstance(patch, dict) else {},
            now=now,
        )
        self._delete_management_session(token)
        return result

    def _management_candidates(
        self,
        user_id: str,
        selector: Mapping[str, object],
    ) -> tuple[list[object], list[object], bool]:
        with self._session_factory() as connection:
            rows = connection.execute(
                "SELECT * FROM reminders WHERE user_id=? AND parent_reminder_id IS NULL "
                "AND status IN ('pending','delivering','awaiting_confirmation','snoozed','paused') "
                "ORDER BY due_at,id",
                (user_id,),
            ).fetchall()
        identifier = selector.get("id")
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            matched = [row for row in rows if int(row["id"]) == identifier]
            return (
                [row for row in matched if row["source_ref"] is None],
                [row for row in matched if row["source_ref"] is not None],
                True,
            )
        query = self._normalized_text(selector.get("query"))
        if not query:
            if selector == {"scope": "all"}:
                return (
                    [row for row in rows if row["source_ref"] is None],
                    [row for row in rows if row["source_ref"] is not None],
                    False,
                )
            return [], [], False
        exact_rows = [row for row in rows if self._normalized_text(row["text"]) == query]
        selected = exact_rows or [row for row in rows if query in self._normalized_text(row["text"])]
        return (
            [row for row in selected if row["source_ref"] is None],
            [row for row in selected if row["source_ref"] is not None],
            bool(exact_rows),
        )

    def _create_management_session(
        self,
        *,
        user_id: str,
        operation: str,
        rows: list[object],
        patch: Mapping[str, object],
        delivery_route: Mapping[str, object] | None,
        now: datetime,
    ) -> str:
        token = secrets.token_hex(16)
        route = self._normalize_delivery_route(delivery_route)
        candidates = [{"id": int(row["id"]), "updated_at": str(row["updated_at"])} for row in rows]
        with self._session_factory() as connection:
            connection.execute("DELETE FROM reminder_management_sessions WHERE expires_at <= ?", (now.isoformat(),))
            connection.execute(
                "INSERT INTO reminder_management_sessions(token,user_id,delivery_route_json,operation,candidates_json,patch_json,expires_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    token,
                    user_id,
                    json.dumps(route, separators=(",", ":"), sort_keys=True) if route else None,
                    operation,
                    json.dumps(candidates, separators=(",", ":")),
                    json.dumps(dict(patch), separators=(",", ":"), sort_keys=True),
                    (now + self._management_session_ttl).isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
        return token

    def _load_management_session(
        self,
        user_id: str,
        token: str,
        delivery_route: Mapping[str, object] | None,
        now: datetime,
    ) -> object | None:
        route = self._normalize_delivery_route(delivery_route)
        with self._session_factory() as connection:
            row = connection.execute(
                "SELECT * FROM reminder_management_sessions WHERE token=? AND user_id=? AND expires_at>?",
                (token, user_id, now.isoformat()),
            ).fetchone()
        if row is None:
            return None
        try:
            stored_route = json.loads(row["delivery_route_json"]) if row["delivery_route_json"] else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return row if stored_route == route else None

    def _delete_management_session(self, token: str) -> None:
        with self._session_factory() as connection:
            connection.execute("DELETE FROM reminder_management_sessions WHERE token=?", (token,))
            connection.commit()

    def _apply_management(
        self,
        *,
        user_id: str,
        operation: str,
        targets: list[tuple[int, str]],
        patch: Mapping[str, object],
        now: datetime,
    ) -> ReminderInterception:
        changed = 0
        try:
            with self._session_factory() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for reminder_id, expected_updated_at in targets:
                    row = connection.execute(
                        "SELECT * FROM reminders WHERE id=? AND user_id=? AND parent_reminder_id IS NULL",
                        (reminder_id, user_id),
                    ).fetchone()
                    if row is None or str(row["updated_at"]) != expected_updated_at or row["source_ref"] is not None:
                        raise ValueError("stale")
                    if operation == "delete":
                        self._cancel_root_in_connection(connection, row, now)
                    elif operation == "pause":
                        self._pause_root_in_connection(connection, row, now)
                    elif operation == "resume":
                        self._resume_root_in_connection(connection, row, now)
                    elif operation == "update":
                        self._update_root_in_connection(connection, row, patch, now)
                    elif operation == "snooze":
                        self._explicit_snooze_in_connection(connection, row, patch, now)
                    else:
                        raise ValueError("unsupported")
                    changed += 1
                connection.commit()
        except ValueError as exc:
            message = str(exc)
            if message == "stale":
                return ReminderInterception("Стан нагадування змінився. Повтори команду зі свіжим списком.")
            return ReminderInterception(message or "Не вдалося змінити нагадування.")
        except sqlite3.Error:
            return ReminderInterception("Не вдалося змінити нагадування; жодна зміна не застосована.")
        self._recalculate_wake()
        labels = {"delete": "Скасовано", "pause": "Призупинено", "resume": "Відновлено", "update": "Оновлено", "snooze": "Відкладено"}
        return ReminderInterception(f"{labels[operation]} нагадувань: {changed}.")

    @staticmethod
    def _cancel_active_children(connection: object, root_id: int, now: datetime) -> None:
        connection.execute(
            "UPDATE reminders SET status='cancelled',delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? "
            "WHERE parent_reminder_id=? AND status IN ('pending','delivering','awaiting_confirmation')",
            (now.isoformat(), root_id),
        )

    def _cancel_root_in_connection(self, connection: object, row: object, now: datetime) -> None:
        if row["status"] not in {"pending", "delivering", "awaiting_confirmation", "snoozed", "paused"}:
            raise ValueError("Це нагадування вже неактивне.")
        self._cancel_active_children(connection, int(row["id"]), now)
        connection.execute(
            "UPDATE reminders SET status='cancelled',delivery_id=NULL,delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
            (now.isoformat(), row["id"]),
        )

    def _pause_root_in_connection(self, connection: object, row: object, now: datetime) -> None:
        if row["status"] == "paused":
            raise ValueError("Це нагадування вже призупинено.")
        if row["status"] not in {"pending", "delivering", "awaiting_confirmation", "snoozed"}:
            raise ValueError("Це нагадування не можна призупинити.")
        self._cancel_active_children(connection, int(row["id"]), now)
        connection.execute(
            "UPDATE reminders SET status='paused',delivery_id=NULL,delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
            (now.isoformat(), row["id"]),
        )

    def _resume_root_in_connection(self, connection: object, row: object, now: datetime) -> None:
        if row["status"] != "paused":
            raise ValueError("Відновити можна лише призупинене нагадування.")
        due = datetime.fromisoformat(row["due_at"]).astimezone(KYIV)
        if row["recurrence_kind"]:
            if due <= now:
                due = self._next_occurrence(row, after=now) or due
        elif due <= now:
            raise ValueError("Разове нагадування вже прострочене; спочатку вкажи новий час.")
        connection.execute(
            "UPDATE reminders SET status='pending',due_at=?,delivery_id=NULL,delivery_attempts=0,delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
            (due.isoformat(), now.isoformat(), row["id"]),
        )

    def _update_root_in_connection(
        self,
        connection: object,
        row: object,
        patch: Mapping[str, object],
        now: datetime,
    ) -> None:
        if not patch:
            raise ValueError("Не вказано, що саме змінити.")
        text = str(patch.get("text", row["text"])).strip()
        if not text or len(text) > 1000:
            raise ValueError("Текст нагадування має містити від 1 до 1000 символів.")
        current_due = datetime.fromisoformat(row["due_at"]).astimezone(KYIV)
        try:
            due_date = date.fromisoformat(str(patch["date"])) if "date" in patch else current_due.date()
            due_time = time.fromisoformat(str(patch["time"])) if "time" in patch else current_due.timetz().replace(tzinfo=None)
        except ValueError:
            raise ValueError("Некоректна дата або час нагадування.") from None
        if due_time.second or due_time.microsecond:
            raise ValueError("Час нагадування задається до хвилини.")
        kind_value = patch.get("recurrence_kind", row["recurrence_kind"])
        kind = None if kind_value in (None, "", "none") else str(kind_value)
        if kind not in (None, "daily", "weekly"):
            raise ValueError("Підтримуються лише daily або weekly повтори.")
        weekday = patch.get("recurrence_weekday", row["recurrence_weekday"])
        if kind == "weekly" and (not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6):
            raise ValueError("Weekly повтор потребує коректного дня тижня.")
        if kind != "weekly":
            weekday = None
        repeat_until = row["repeat_until"]
        if "repeat_until" in patch:
            try:
                repeat_until = None if patch["repeat_until"] in (None, "") else datetime.combine(date.fromisoformat(str(patch["repeat_until"])), time.max, tzinfo=KYIV).isoformat()
            except ValueError:
                raise ValueError("Некоректна дата завершення повторення.") from None
        due = datetime.combine(due_date, due_time, tzinfo=KYIV)
        if kind:
            if kind == "weekly":
                due += timedelta(days=(int(weekday) - due.weekday()) % 7)
            interval = timedelta(days=1 if kind == "daily" else 7)
            while due <= now:
                due += interval
        elif due <= now:
            raise ValueError("Час разового нагадування має бути в майбутньому.")
        if repeat_until and due > datetime.fromisoformat(repeat_until):
            raise ValueError("Дата завершення повторення раніше за наступне нагадування.")
        self._cancel_active_children(connection, int(row["id"]), now)
        status = "paused" if row["status"] == "paused" else "pending"
        connection.execute(
            "UPDATE reminders SET text=?,due_at=?,status=?,recurrence_kind=?,recurrence_weekday=?,repeat_until=?,"
            "delivery_id=NULL,delivery_attempts=0,delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
            (text, due.isoformat(), status, kind, weekday, repeat_until, now.isoformat(), row["id"]),
        )

    def _explicit_snooze_in_connection(
        self,
        connection: object,
        row: object,
        patch: Mapping[str, object],
        now: datetime,
    ) -> None:
        if row["status"] not in {"pending", "delivering", "awaiting_confirmation", "snoozed"}:
            raise ValueError("Це нагадування зараз не можна відкласти.")
        minutes_value = patch.get("snooze_minutes")
        minutes = int(minutes_value) if isinstance(minutes_value, int) and not isinstance(minutes_value, bool) else int(self._default_snooze.total_seconds() // 60)
        if minutes < 1:
            raise ValueError("Тривалість snooze має бути додатною.")
        retry_at = now + timedelta(minutes=minutes)
        if row["recurrence_kind"]:
            next_due = self._next_occurrence(row, after=datetime.fromisoformat(row["due_at"]))
            if next_due is not None and retry_at >= next_due:
                raise ValueError("Snooze перетинає наступний плановий слот; зміни розклад або призупини нагадування.")
        self._cancel_active_children(connection, int(row["id"]), now)
        route = self._delivery_route_from_row(row)
        connection.execute(
            "UPDATE reminders SET status='snoozed',delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
            (now.isoformat(), row["id"]),
        )
        connection.execute(
            "INSERT INTO reminders(user_id,text,due_at,timezone,status,recurrence_kind,recurrence_weekday,repeat_until,delivery_route_json,parent_reminder_id,created_at,updated_at) "
            "VALUES (?,?,?,?, 'pending',NULL,NULL,NULL,?,?,?,?)",
            (
                row["user_id"], row["text"], retry_at.isoformat(), row["timezone"],
                json.dumps(route, separators=(",", ":"), sort_keys=True) if route else None,
                row["id"], now.isoformat(), now.isoformat(),
            ),
        )

    @staticmethod
    def _format_due(row: object) -> str:
        return datetime.fromisoformat(row["due_at"]).astimezone(KYIV).strftime("%d.%m.%Y о %H:%M")

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
                            "delivery_id=NULL,delivery_attempts=0,delivery_lease_until=NULL,next_attempt_at=NULL,"
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
                        "UPDATE reminders SET status='completed', delivery_lease_until=NULL, confirmation_deadline_at=NULL, next_attempt_at=NULL, updated_at=? "
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
                        "UPDATE reminders SET status='completed', delivery_lease_until=NULL, confirmation_deadline_at=NULL, next_attempt_at=NULL, updated_at=? "
                        "WHERE id=? AND user_id=? AND status=?",
                        (now.isoformat(), root_id, user_id, expected_status),
                    )
                else:
                    root_update = connection.execute(
                        "UPDATE reminders SET status='pending', due_at=?, delivery_id=NULL, delivery_attempts=0, "
                        "delivery_lease_until=NULL, confirmation_deadline_at=NULL, next_attempt_at=NULL, updated_at=? "
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

    def _miss_confirmed_occurrence(self, row: object, user_id: str, now: datetime) -> bool:
        with self._session_factory() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM reminders WHERE id=? AND user_id=? AND status='awaiting_confirmation' AND delivery_id=?",
                    (row["id"], user_id, row["delivery_id"]),
                ).fetchone()
                if current is None:
                    connection.rollback()
                    return False
                self._miss_row_in_connection(connection, current, now)
                connection.commit()
                return True
            except sqlite3.Error:
                connection.rollback()
                return False

    def _miss_row_in_connection(self, connection: object, row: object, now: datetime) -> None:
        parent_id = row["parent_reminder_id"]
        if parent_id is not None:
            connection.execute(
                "UPDATE reminders SET status='missed',delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
                (now.isoformat(), row["id"]),
            )
            root = connection.execute(
                "SELECT * FROM reminders WHERE id=? AND user_id=?",
                (parent_id, row["user_id"]),
            ).fetchone()
            if root is not None and root["status"] == "snoozed":
                next_due = self._next_occurrence(root, after=now)
                if next_due is None:
                    connection.execute(
                        "UPDATE reminders SET status='missed',delivery_id=NULL,delivery_attempts=0,delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
                        (now.isoformat(), root["id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE reminders SET status='pending',due_at=?,delivery_id=NULL,delivery_attempts=0,delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
                        (next_due.isoformat(), now.isoformat(), root["id"]),
                    )
            return
        next_due = self._next_occurrence(row, after=now)
        if next_due is None:
            connection.execute(
                "UPDATE reminders SET status='missed',delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
                (now.isoformat(), row["id"]),
            )
        else:
            connection.execute(
                "UPDATE reminders SET status='pending',due_at=?,delivery_id=NULL,delivery_attempts=0,delivery_lease_until=NULL,confirmation_deadline_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
                (next_due.isoformat(), now.isoformat(), row["id"]),
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
                    "UPDATE reminders SET status='cancelled', delivery_id=NULL, delivery_lease_until=NULL, confirmation_deadline_at=NULL, next_attempt_at=NULL, updated_at=? "
                    "WHERE id=? AND user_id=? AND due_at=? AND updated_at=? "
                    "AND parent_reminder_id IS NULL "
                    "AND status IN ('pending','delivering','awaiting_confirmation','snoozed','paused')",
                    (now.isoformat(), reminder_id, user_id, due_at, updated_at),
                )
                if cursor.rowcount:
                    connection.execute(
                        "UPDATE reminders SET status='cancelled', delivery_id=NULL, delivery_lease_until=NULL, confirmation_deadline_at=NULL, next_attempt_at=NULL, updated_at=? "
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
                "SELECT root.id,root.text,root.due_at,root.updated_at,root.status,root.recurrence_kind,root.source_ref,root.next_attempt_at,"
                "(SELECT child.due_at FROM reminders AS child WHERE child.parent_reminder_id=root.id "
                "AND child.status IN ('pending','delivering','awaiting_confirmation') ORDER BY child.due_at,child.id LIMIT 1) AS snooze_due_at "
                "FROM reminders AS root "
                "WHERE root.user_id=? AND root.parent_reminder_id IS NULL "
                "AND root.status IN ('pending','delivering','awaiting_confirmation','snoozed','paused') "
                "ORDER BY due_at,root.id LIMIT 11",
                (user_id,),
            ).fetchall()
        if not rows:
            self._list_snapshots.pop(user_id, None)
            return "Активних нагадувань немає."
        truncated = len(rows) > 10
        self._list_snapshots[user_id] = tuple(
            (int(row["id"]), str(row["due_at"]), str(row["updated_at"])) for row in rows[:10]
        )
        lines = []
        for index, row in enumerate(rows[:10], start=1):
            due_at = datetime.fromisoformat(row["due_at"]).astimezone(KYIV).strftime("%d.%m.%Y о %H:%M")
            suffix = " (щодня)" if row["recurrence_kind"] == "daily" else " (щотижня)" if row["recurrence_kind"] == "weekly" else ""
            details = []
            if row["status"] == "paused":
                details.append("призупинено")
            if row["snooze_due_at"]:
                snooze_at = datetime.fromisoformat(row["snooze_due_at"]).astimezone(KYIV).strftime("%d.%m.%Y о %H:%M")
                details.append(f"snooze до {snooze_at}")
            if row["next_attempt_at"]:
                retry_at = datetime.fromisoformat(row["next_attempt_at"]).astimezone(KYIV).strftime("%d.%m.%Y о %H:%M")
                details.append(f"повтор доставки {retry_at}")
            if row["source_ref"]:
                details.append("керується календарем")
            detail_suffix = f" [{'; '.join(details)}]" if details else ""
            lines.append(f"{index}. #{row['id']} {row['text']} — {due_at}{suffix}{detail_suffix}")
        if truncated:
            lines.append("Є ще активні нагадування; показано перші 10.")
        return "Активні нагадування:\n" + "\n".join(lines)

    def _claim_due(self, now: datetime) -> list[object]:
        with self._session_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM reminders AS candidate "
                "WHERE (candidate.status='pending' OR (candidate.status='delivering' AND candidate.delivery_lease_until <= ?)) "
                "AND COALESCE(candidate.next_attempt_at,candidate.due_at) <= ? "
                "AND (candidate.parent_reminder_id IS NOT NULL OR NOT EXISTS ("
                "SELECT 1 FROM reminders AS child WHERE child.parent_reminder_id=candidate.id "
                "AND child.status IN ('pending','delivering','awaiting_confirmation'))) "
                "ORDER BY COALESCE(candidate.next_attempt_at,candidate.due_at), candidate.id LIMIT 100",
                (now.isoformat(), now.isoformat()),
            ).fetchall()
            claimed: list[object] = []
            for row in rows:
                due_at = datetime.fromisoformat(row["due_at"]).astimezone(KYIV)
                if now - due_at > self._missed_grace:
                    self._miss_row_in_connection(connection, row, now)
                    continue
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
            self._miss_confirmed_occurrence(row, str(row["user_id"]), now)

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
                    "SELECT COALESCE(next_attempt_at,due_at) AS wake_at FROM reminders WHERE status='pending' "
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
