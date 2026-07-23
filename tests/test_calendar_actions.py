from __future__ import annotations

import unittest
from datetime import datetime, timezone

import httpx

from lumen.services.calendar_actions import CalendarActionService


class FakeHttpClient:
    def __init__(self, *, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def __aenter__(self) -> "FakeHttpClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, request=httpx.Request("POST", url))


class CalendarActionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.refresh_calls = 0

        async def refresh() -> None:
            self.refresh_calls += 1

        self.service = CalendarActionService(
            base_url="http://homeassistant.local:8123",
            token="secret",
            write_entity_id="calendar.primary",
            default_duration_minutes=60,
            refresh_callback=refresh,
            http_client_factory=lambda: self.http,
        )

    async def test_timed_payload_uses_fixed_entity_and_default_duration(self) -> None:
        result = await self.service.execute({"calendar": {
            "action": "create",
            "item": {
                "summary": "Зустріч",
                "description": "Опис",
                "location": "Офіс",
                "all_day": False,
                "start": "2026-07-23T15:00:00+03:00",
                "entity_id": "calendar.model_selected",
            },
        }})

        self.assertEqual("created", result.status)
        self.assertEqual(1, len(self.http.calls))
        self.assertEqual("http://homeassistant.local:8123/api/services/calendar/create_event", self.http.calls[0]["url"])
        self.assertEqual({
            "entity_id": "calendar.primary",
            "summary": "Зустріч",
            "description": "Опис",
            "location": "Офіс",
            "start_date_time": "2026-07-23T15:00:00+03:00",
            "end_date_time": "2026-07-23T16:00:00+03:00",
        }, self.http.calls[0]["json"])
        self.assertEqual("Bearer secret", self.http.calls[0]["headers"]["Authorization"])
        self.assertEqual(1, self.refresh_calls)
        self.assertTrue(result.refresh_succeeded)

    async def test_explicit_aware_end_is_preserved(self) -> None:
        result = await self.service.create({
            "summary": "Подія",
            "all_day": False,
            "start": datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc),
        })

        self.assertEqual("created", result.status)
        self.assertEqual("2026-07-23T14:00:00+00:00", self.http.calls[0]["json"]["end_date_time"])

    async def test_all_day_defaults_to_exclusive_next_day(self) -> None:
        result = await self.service.create({"summary": "День народження", "all_day": True, "start": "2026-07-23"})

        self.assertEqual("created", result.status)
        self.assertEqual("2026-07-23", self.http.calls[0]["json"]["start_date"])
        self.assertEqual("2026-07-24", self.http.calls[0]["json"]["end_date"])
        self.assertNotIn("start_date_time", self.http.calls[0]["json"])

    async def test_invalid_contract_does_not_call_ha_or_refresh(self) -> None:
        cases = [
            {"summary": "", "all_day": False, "start": "2026-07-23T10:00:00+03:00"},
            {"summary": "Подія", "all_day": False, "start": "2026-07-23T10:00:00"},
            {"summary": "Подія", "all_day": False, "start": "2026-07-23T11:00:00+03:00", "end": "2026-07-23T10:00:00+03:00"},
            {"summary": "Подія", "all_day": True, "start": "2026-07-23", "end": "2026-07-23"},
        ]
        for contract in cases:
            with self.subTest(contract=contract):
                result = await self.service.create(contract)
                self.assertEqual("invalid", result.status)
        self.assertEqual([], self.http.calls)
        self.assertEqual(0, self.refresh_calls)

    async def test_timeout_is_unknown_and_never_retried_or_refreshed(self) -> None:
        request = httpx.Request("POST", "http://homeassistant.local:8123/api/services/calendar/create_event")
        self.http.error = httpx.ReadTimeout("timed out", request=request)

        result = await self.service.create({
            "summary": "Подія", "all_day": False, "start": "2026-07-23T10:00:00+03:00"
        })

        self.assertEqual("unknown", result.status)
        self.assertEqual(1, len(self.http.calls))
        self.assertEqual(0, self.refresh_calls)

    async def test_http_error_is_failed_and_not_refreshed(self) -> None:
        self.http.status = 400

        result = await self.service.create({
            "summary": "Подія", "all_day": False, "start": "2026-07-23T10:00:00+03:00"
        })

        self.assertEqual("failed", result.status)
        self.assertEqual(1, len(self.http.calls))
        self.assertEqual(0, self.refresh_calls)

    async def test_refresh_failure_does_not_change_success_into_failed_write(self) -> None:
        def refresh_failure() -> None:
            self.refresh_calls += 1
            raise RuntimeError("refresh failed")

        service = CalendarActionService(
            base_url="http://homeassistant.local:8123",
            token="secret",
            write_entity_id="calendar.primary",
            refresh_callback=refresh_failure,
            http_client_factory=lambda: self.http,
        )
        result = await service.create({
            "summary": "Подія", "all_day": False, "start": "2026-07-23T10:00:00+03:00"
        })

        self.assertEqual("created", result.status)
        self.assertFalse(result.refresh_succeeded)
        self.assertEqual(1, self.refresh_calls)


if __name__ == "__main__":
    unittest.main()
