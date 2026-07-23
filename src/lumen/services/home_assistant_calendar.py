"""Read-only Home Assistant calendar projection for the current Kyiv day."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
import websockets

from lumen.timezones import kyiv_timezone


KYIV = kyiv_timezone()


@dataclass(frozen=True)
class CalendarEvent:
    calendar_entity_id: str
    uid: str | None
    recurrence_id: str | None
    summary: str
    description: str
    start: datetime
    end: datetime
    notification_at: datetime


CalendarCallback = Callable[[str, tuple[CalendarEvent, ...]], Awaitable[None] | None]


def _parse_datetime(value: object, *, end: bool = False) -> tuple[datetime, bool]:
    if isinstance(value, Mapping):
        value = value.get("dateTime", value.get("date"))
    text = str(value or "").strip()
    if not text:
        raise ValueError("calendar event is missing start or end")
    if len(text) == 10:
        parsed_date = date.fromisoformat(text)
        return datetime.combine(parsed_date, time.min, tzinfo=KYIV), True
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV)
    return parsed.astimezone(KYIV), False


def normalize_calendar_event(
    calendar_entity_id: str,
    payload: Mapping[str, object],
    *,
    all_day_notification_time: time = time(9, 0),
) -> CalendarEvent:
    """Normalize one Home Assistant calendar event into Kyiv-aware values."""

    start, all_day = _parse_datetime(payload.get("start"))
    end, end_all_day = _parse_datetime(payload.get("end"), end=True)
    if all_day != end_all_day:
        raise ValueError("calendar event mixes all-day and timed boundaries")
    if end <= start:
        raise ValueError("calendar event end must be after start")
    notification_at = datetime.combine(start.date(), all_day_notification_time, tzinfo=KYIV) if all_day else start
    recurrence = payload.get("recurrence_id")
    return CalendarEvent(
        calendar_entity_id=calendar_entity_id,
        uid=str(payload["uid"]).strip() if payload.get("uid") not in (None, "") else None,
        recurrence_id=str(recurrence).strip() if recurrence not in (None, "") else None,
        summary=str(payload.get("summary") or "").strip(),
        description=str(payload.get("description") or "").strip(),
        start=start,
        end=end,
        notification_at=notification_at,
    )


class HomeAssistantCalendarClient:
    """Fetch today's calendar events and follow HA calendar WebSocket updates."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        calendar_entity_ids: Sequence[str],
        on_snapshot: CalendarCallback,
        on_update: CalendarCallback,
        logger: logging.Logger | None = None,
        http_client_factory: Callable[[], Any] | None = None,
        websocket_connect: Callable[..., Any] | None = None,
        now: Callable[[], datetime] | None = None,
        all_day_notification_time: time = time(9, 0),
        reconnect_delay_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._entity_ids = tuple(dict.fromkeys(item.strip() for item in calendar_entity_ids if item.strip()))
        self._on_snapshot = on_snapshot
        self._on_update = on_update
        self._logger = logger or logging.getLogger("lumen")
        self._http_client_factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=20.0))
        self._websocket_connect = websocket_connect or websockets.connect
        self._now = now or (lambda: datetime.now(KYIV))
        self._all_day_notification_time = all_day_notification_time
        self._reconnect_delay = reconnect_delay_seconds
        self._stopping = False

    async def snapshot_today(self) -> dict[str, tuple[CalendarEvent, ...]]:
        """Read and publish one authoritative current-day snapshot per calendar."""

        start, end = self._day_bounds()
        result: dict[str, tuple[CalendarEvent, ...]] = {}
        async with self._http_client_factory() as client:
            for entity_id in self._entity_ids:
                events = await self._fetch_entity(client, entity_id, start, end)
                result[entity_id] = events
                await self._invoke(self._on_snapshot, entity_id, events)
        return result

    async def run_forever(self) -> None:
        """Snapshot once, then reconnect and resnapshot after socket loss/day rollover."""

        self._stopping = False
        while not self._stopping:
            try:
                await self.snapshot_today()
                await self._run_websocket_until_disconnect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection failures are recovered by a fresh snapshot
                self._logger.warning("Home Assistant calendar WebSocket disconnected: %s", exc)
            if self._stopping:
                break
            await asyncio.sleep(self._reconnect_delay)

    def stop(self) -> None:
        self._stopping = True

    async def _run_websocket_until_disconnect(self) -> None:
        start, end = self._day_bounds()
        async with self._websocket_connect(self._websocket_url()) as socket:
            auth_required = json.loads(await socket.recv())
            if auth_required.get("type") != "auth_required":
                raise RuntimeError("unexpected Home Assistant WebSocket greeting")
            await socket.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth_result = json.loads(await socket.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant WebSocket authentication failed")

            subscriptions: dict[int, str] = {}
            for message_id, entity_id in enumerate(self._entity_ids, start=1):
                subscriptions[message_id] = entity_id
                await socket.send(json.dumps({
                    "id": message_id,
                    "type": "calendar/event/subscribe",
                    "entity_id": entity_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }))

            while not self._stopping:
                seconds_to_rollover = max(0.01, (end - self._current()).total_seconds())
                try:
                    message = json.loads(await asyncio.wait_for(socket.recv(), timeout=seconds_to_rollover))
                except asyncio.TimeoutError:
                    return
                message_id = message.get("id")
                entity_id = subscriptions.get(message_id) if isinstance(message_id, int) else None
                if entity_id is None:
                    continue
                if message.get("type") == "result" and not message.get("success", False):
                    raise RuntimeError(f"calendar subscription failed for {entity_id}")
                if self._events_from_message(message) is None:
                    continue
                # The subscription payload intentionally omits uid/recurrence_id.
                # Treat it as an invalidation signal and refresh through HA REST.
                async with self._http_client_factory() as client:
                    events = await self._fetch_entity(client, entity_id, start, end)
                await self._invoke(self._on_update, entity_id, events)

    async def _fetch_entity(
        self,
        client: Any,
        entity_id: str,
        start: datetime,
        end: datetime,
    ) -> tuple[CalendarEvent, ...]:
        response = await client.get(self._calendar_url(entity_id, start, end), headers=self._headers())
        response.raise_for_status()
        return self._normalize_many(entity_id, response.json())

    def _day_bounds(self) -> tuple[datetime, datetime]:
        current = self._current()
        start = datetime.combine(current.date(), time.min, tzinfo=KYIV)
        return start, start + timedelta(days=1)

    def _current(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=KYIV)
        return current.astimezone(KYIV)

    def _calendar_url(self, entity_id: str, start: datetime, end: datetime) -> str:
        query = urlencode({"start": start.isoformat(), "end": end.isoformat()})
        return f"{self._base_url}/api/calendars/{quote(entity_id, safe='')}?{query}"

    def _websocket_url(self) -> str:
        parsed = urlsplit(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/api/websocket", "", ""))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    @staticmethod
    def _events_from_message(message: Mapping[str, object]) -> Iterable[Mapping[str, object]] | None:
        payload = message.get("event") if message.get("type") == "event" else message.get("result")
        if isinstance(payload, Mapping):
            payload = payload.get("events", payload.get("event"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            return [payload]
        return None

    def _normalize_many(self, entity_id: str, payload: object) -> tuple[CalendarEvent, ...]:
        if not isinstance(payload, Iterable) or isinstance(payload, (str, bytes, Mapping)):
            raise ValueError("Home Assistant calendar response must be a list")
        events = [
            normalize_calendar_event(
                entity_id,
                item,
                all_day_notification_time=self._all_day_notification_time,
            )
            for item in payload
            if isinstance(item, Mapping)
        ]
        return tuple(sorted(events, key=lambda item: (item.notification_at, item.uid or "", item.summary)))

    @staticmethod
    async def _invoke(callback: CalendarCallback, entity_id: str, events: tuple[CalendarEvent, ...]) -> None:
        result = callback(entity_id, events)
        if inspect.isawaitable(result):
            await result
