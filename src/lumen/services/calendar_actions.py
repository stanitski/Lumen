"""Typed, non-retrying calendar write actions through Home Assistant."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import httpx


CalendarActionStatus = Literal["created", "invalid", "failed", "unknown", "not_handled"]
RefreshCallback = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True)
class CalendarCreateRequest:
    summary: str
    all_day: bool
    start: datetime | date
    end: datetime | date
    description: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class CalendarActionResult:
    status: CalendarActionStatus
    message: str
    request: CalendarCreateRequest | None = None
    refresh_succeeded: bool | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "created"


class CalendarActionService:
    """Create events in one constructor-selected HA calendar entity."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        write_entity_id: str,
        default_duration_minutes: int = 60,
        refresh_callback: RefreshCallback | None = None,
        http_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not write_entity_id.strip():
            raise ValueError("calendar write entity id is required")
        if default_duration_minutes <= 0:
            raise ValueError("default calendar duration must be positive")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._write_entity_id = write_entity_id.strip()
        self._default_duration = timedelta(minutes=default_duration_minutes)
        self._refresh_callback = refresh_callback
        self._http_client_factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=20.0))

    async def execute(self, contract: Mapping[str, object]) -> CalendarActionResult:
        """Execute a supported calendar contract without exposing entity selection."""

        action_contract = contract.get("calendar") if isinstance(contract.get("calendar"), Mapping) else contract
        action = str(action_contract.get("action") or "none").strip()
        if action != "create":
            return CalendarActionResult("not_handled", "Calendar action was not requested.")
        item = action_contract.get("item")
        if not isinstance(item, Mapping):
            return CalendarActionResult("invalid", "Calendar create action requires one structured item.")
        return await self.create(item)

    async def create(self, contract: Mapping[str, object]) -> CalendarActionResult:
        """Validate and create exactly one event; never retry an uncertain write."""

        parsed = self._parse_create_contract(contract)
        if isinstance(parsed, str):
            return CalendarActionResult("invalid", parsed)
        payload = self._payload(parsed)
        try:
            async with self._http_client_factory() as client:
                response = await client.post(
                    f"{self._base_url}/api/services/calendar/create_event",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return CalendarActionResult("failed", f"Home Assistant rejected calendar event creation ({exc.response.status_code}).", parsed)
        except httpx.RequestError:
            return CalendarActionResult(
                "unknown",
                "Calendar event creation result is unknown; do not retry automatically.",
                parsed,
            )

        refresh_succeeded = await self._refresh_after_success()
        message = "Calendar event created."
        if refresh_succeeded is False:
            message += " Calendar refresh failed; the event must not be created again automatically."
        return CalendarActionResult("created", message, parsed, refresh_succeeded)

    def _parse_create_contract(self, contract: Mapping[str, object]) -> CalendarCreateRequest | str:
        summary_value = contract.get("summary")
        if not isinstance(summary_value, str) or not summary_value.strip():
            return "Calendar event summary is required."
        all_day = contract.get("all_day")
        if not isinstance(all_day, bool):
            return "Calendar event all_day must be true or false."
        description = self._optional_text(contract.get("description"))
        if isinstance(description, ValueError):
            return str(description)
        location = self._optional_text(contract.get("location"))
        if isinstance(location, ValueError):
            return str(location)

        if all_day:
            start = self._parse_date(contract.get("start"))
            if isinstance(start, ValueError):
                return str(start)
            end_value = contract.get("end")
            end = start + timedelta(days=1) if end_value in (None, "") else self._parse_date(end_value)
            if isinstance(end, ValueError):
                return str(end)
        else:
            start = self._parse_aware_datetime(contract.get("start"), "start")
            if isinstance(start, ValueError):
                return str(start)
            end_value = contract.get("end")
            end = start + self._default_duration if end_value in (None, "") else self._parse_aware_datetime(end_value, "end")
            if isinstance(end, ValueError):
                return str(end)
        if end <= start:
            return "Calendar event end must be after start."
        return CalendarCreateRequest(summary_value.strip(), all_day, start, end, description, location)

    def _payload(self, request: CalendarCreateRequest) -> dict[str, object]:
        payload: dict[str, object] = {"entity_id": self._write_entity_id, "summary": request.summary}
        if request.description is not None:
            payload["description"] = request.description
        if request.location is not None:
            payload["location"] = request.location
        if request.all_day:
            payload["start_date"] = request.start.isoformat()
            payload["end_date"] = request.end.isoformat()
        else:
            payload["start_date_time"] = request.start.isoformat()
            payload["end_date_time"] = request.end.isoformat()
        return payload

    async def _refresh_after_success(self) -> bool | None:
        if self._refresh_callback is None:
            return None
        try:
            result = self._refresh_callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            return False
        return True

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    @staticmethod
    def _optional_text(value: object) -> str | None | ValueError:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            return ValueError("Calendar description and location must be text.")
        return value.strip() or None

    @staticmethod
    def _parse_date(value: object) -> date | ValueError:
        if isinstance(value, datetime):
            return ValueError("All-day calendar start and end must be dates.")
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            return ValueError("All-day calendar start and end must be ISO dates.")
        try:
            if len(value.strip()) != 10:
                raise ValueError
            return date.fromisoformat(value.strip())
        except ValueError:
            return ValueError("All-day calendar start and end must be ISO dates.")

    @staticmethod
    def _parse_aware_datetime(value: object, field: str) -> datetime | ValueError:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return ValueError(f"Timed calendar {field} must be an ISO datetime with timezone.")
        else:
            return ValueError(f"Timed calendar {field} must be an ISO datetime with timezone.")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return ValueError(f"Timed calendar {field} must include a timezone.")
        return parsed
