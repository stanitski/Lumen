from __future__ import annotations

import json
import unittest
from datetime import datetime, time

import httpx

from lumen.services.home_assistant_calendar import KYIV, HomeAssistantCalendarClient, normalize_calendar_event


class FakeHttpClient:
    def __init__(self, payload: list[dict[str, object]]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> "FakeHttpClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        self.calls.append((url, headers))
        return httpx.Response(200, json=self.payload, request=httpx.Request("GET", url))


class FakeSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = [json.dumps(item) for item in messages]
        self.sent: list[dict[str, object]] = []

    async def __aenter__(self) -> "FakeSocket":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def recv(self) -> str:
        if not self.messages:
            raise RuntimeError("socket closed")
        return self.messages.pop(0)

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))


class HomeAssistantCalendarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 22, 8, 15, tzinfo=KYIV)
        self.payload = [
            {"uid": "timed-1", "summary": "Зустріч", "description": "Опис", "start": "2026-07-22T14:30:00+03:00", "end": "2026-07-22T15:30:00+03:00"},
            {"uid": "day-1", "recurrence_id": "20260722", "summary": "День народження", "start": "2026-07-22", "end": "2026-07-23"},
        ]
        self.snapshots: list[tuple[str, tuple[object, ...]]] = []
        self.updates: list[tuple[str, tuple[object, ...]]] = []

    def _client(self, **kwargs: object) -> HomeAssistantCalendarClient:
        return HomeAssistantCalendarClient(
            base_url="http://homeassistant.local:8123",
            token="secret",
            calendar_entity_ids=["calendar.main"],
            on_snapshot=lambda entity, events: self.snapshots.append((entity, events)),
            on_update=lambda entity, events: self.updates.append((entity, events)),
            now=lambda: self.now,
            reconnect_delay_seconds=0,
            **kwargs,
        )

    def test_normalizes_timed_and_all_day_event(self) -> None:
        timed = normalize_calendar_event("calendar.main", self.payload[0])
        all_day = normalize_calendar_event("calendar.main", self.payload[1])

        self.assertEqual("2026-07-22T14:30:00+03:00", timed.notification_at.isoformat())
        self.assertEqual("2026-07-22T09:00:00+03:00", all_day.notification_at.isoformat())
        self.assertEqual("20260722", all_day.recurrence_id)
        self.assertEqual("", all_day.description)

    def test_all_day_notification_time_is_configurable(self) -> None:
        event = normalize_calendar_event(
            "calendar.main",
            self.payload[1],
            all_day_notification_time=time(8, 30),
        )

        self.assertEqual("2026-07-22T08:30:00+03:00", event.notification_at.isoformat())

    async def test_snapshot_reads_only_current_kyiv_day_through_ha(self) -> None:
        fake_http = FakeHttpClient(self.payload)
        client = self._client(http_client_factory=lambda: fake_http)

        result = await client.snapshot_today()

        self.assertEqual(["day-1", "timed-1"], [event.uid for event in result["calendar.main"]])
        url, headers = fake_http.calls[0]
        self.assertIn("/api/calendars/calendar.main?", url)
        self.assertIn("start=2026-07-22T00%3A00%3A00%2B03%3A00", url)
        self.assertIn("end=2026-07-23T00%3A00%3A00%2B03%3A00", url)
        self.assertEqual("Bearer secret", headers["Authorization"])
        self.assertEqual(1, len(self.snapshots))

    async def test_websocket_authenticates_subscribes_and_publishes_update(self) -> None:
        socket = FakeSocket([
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": None},
            {"id": 1, "type": "event", "event": {"events": [self.payload[0]]}},
        ])
        fake_http = FakeHttpClient(self.payload)
        client = self._client(websocket_connect=lambda _url: socket, http_client_factory=lambda: fake_http)

        with self.assertRaisesRegex(RuntimeError, "socket closed"):
            await client._run_websocket_until_disconnect()

        self.assertEqual({"type": "auth", "access_token": "secret"}, socket.sent[0])
        self.assertEqual("calendar/event/subscribe", socket.sent[1]["type"])
        self.assertEqual("calendar.main", socket.sent[1]["entity_id"])
        self.assertEqual(["day-1", "timed-1"], [event.uid for event in self.updates[0][1]])
        self.assertEqual(1, len(fake_http.calls))

    def test_rejects_mixed_all_day_boundaries(self) -> None:
        with self.assertRaisesRegex(ValueError, "mixes all-day"):
            normalize_calendar_event("calendar.main", {"start": "2026-07-22", "end": "2026-07-22T10:00:00+03:00"})

    def test_accepts_documented_rest_boundary_objects(self) -> None:
        event = normalize_calendar_event("calendar.main", {
            "summary": "Подія",
            "start": {"dateTime": "2026-07-22T10:00:00+03:00"},
            "end": {"dateTime": "2026-07-22T11:00:00+03:00"},
        })

        self.assertEqual("2026-07-22T10:00:00+03:00", event.start.isoformat())
        self.assertIsNone(event.uid)


if __name__ == "__main__":
    unittest.main()
