"""Execute validated external-context requests before final answer generation."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from lumen.services.web_search import DuckDuckGoWebSearch, WebSearchResult
from lumen.timezones import kyiv_timezone

if TYPE_CHECKING:
    from lumen.services.telegram_context import TelegramContextService
    from lumen.storage.nutrition import NutritionStore


@dataclass(frozen=True)
class ContextExecutionResult:
    source: str
    operation: str
    status: str
    detail: str
    context: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class ContextExecutor:
    """Dispatch read-only context requests; integrations are added independently."""

    _WEB_CONTEXT_CHAR_LIMIT = 3000
    _NUTRITION_CONTEXT_CHAR_LIMIT = 4000

    def __init__(
        self,
        telegram_service: "TelegramContextService | None" = None,
        web_search: DuckDuckGoWebSearch | None = None,
        *,
        nutrition_store: "NutritionStore | None" = None,
        nutrition_timezone: str | tzinfo | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("lumen")
        self._telegram_service = telegram_service
        self._web_search = web_search
        self._nutrition_store = nutrition_store
        self._nutrition_timezone = nutrition_timezone or "Europe/Kyiv"
        self._allowed_operations = {
            "telegram": "search",
            "web": "search",
            "home_assistant": "read_state",
            "knowledge": "search",
        }

    async def execute(self, requests: object, *, user_id: str, user_text: str) -> list[ContextExecutionResult]:
        if not isinstance(requests, list):
            return []
        results: list[ContextExecutionResult] = []
        for request in requests:
            if not isinstance(request, dict):
                continue
            source = str(request.get("source", "")).strip()
            operation = str(request.get("operation", "")).strip()
            if self._allowed_operations.get(source) != operation and not (
                source == "nutrition" and operation in {"summary", "read"}
            ):
                continue
            handler = getattr(self, f"_execute_{source}", None)
            if not callable(handler):
                continue
            results.append(await handler(request, user_id=user_id, user_text=user_text, operation=operation))
        return results

    async def _execute_telegram(self, request: dict[str, object], *, user_id: str, user_text: str, operation: str) -> ContextExecutionResult:
        if self._telegram_service is None:
            return self._pending("telegram", operation, user_id, request)
        result = await self._telegram_service.search(request, user_id=user_id, user_text=user_text)
        return ContextExecutionResult("telegram", operation, result.status, result.detail, result.context)

    async def _execute_web(self, request: dict[str, object], *, user_id: str, user_text: str, operation: str) -> ContextExecutionResult:
        del user_id, user_text
        if self._web_search is None:
            return ContextExecutionResult("web", operation, "unavailable", "Web search is not configured.")
        query = str(request.get("query", "")).strip()
        results = await self._web_search.search(query)
        if not results:
            return ContextExecutionResult("web", operation, "unavailable", "No web results were retrieved.")
        context = self._format_web_context(results)
        self._logger.debug("Web context results=%d context_chars=%d", len(results), len(context))
        return ContextExecutionResult("web", operation, "available", f"Retrieved {len(results)} web results.", context)

    async def _execute_home_assistant(self, request: dict[str, object], *, user_id: str, user_text: str, operation: str) -> ContextExecutionResult:
        # TODO: apply entity access policy and read Home Assistant state.
        return self._pending("home_assistant", operation, user_id, request)

    async def _execute_knowledge(self, request: dict[str, object], *, user_id: str, user_text: str, operation: str) -> ContextExecutionResult:
        # TODO: route a contract-requested knowledge lookup through the shared retriever.
        return self._pending("knowledge", operation, user_id, request)

    async def _execute_nutrition(self, request: dict[str, object], *, user_id: str, user_text: str, operation: str) -> ContextExecutionResult:
        del user_text
        if self._nutrition_store is None:
            return ContextExecutionResult("nutrition", operation, "unavailable", "Nutrition journal is not configured.")
        try:
            if isinstance(self._nutrition_timezone, tzinfo):
                tz = self._nutrition_timezone
            elif self._nutrition_timezone in {"Europe/Kyiv", "Europe/Kiev"}:
                tz = kyiv_timezone()
            else:
                tz = ZoneInfo(self._nutrition_timezone)
            start_raw = request.get("date_from") or request.get("start_date")
            end_raw = request.get("date_to") or request.get("end_date")
            today = datetime.now(tz).date()
            start = date.fromisoformat(str(start_raw)) if start_raw else today
            end = date.fromisoformat(str(end_raw)) if end_raw else today
            if end < start:
                raise ValueError("end date precedes start date")
        except (KeyError, ValueError, TypeError):
            return ContextExecutionResult("nutrition", operation, "unavailable", "Nutrition date range is invalid.")
        result = self._nutrition_store.read_period(user_id, start, end, tz)
        context = result.context
        if result.status == "available" and operation == "summary":
            context = (
                f"Nutrition summary for {start.isoformat()} through {end.isoformat()}: "
                f"{result.meal_count} meals, {result.total_calories} kcal total."
            )
        if len(context) > self._NUTRITION_CONTEXT_CHAR_LIMIT:
            context = context[: self._NUTRITION_CONTEXT_CHAR_LIMIT - 1].rstrip() + "…"
        return ContextExecutionResult("nutrition", operation, result.status, result.detail, context)

    def _pending(
        self,
        source: str,
        operation: str,
        user_id: str,
        request: dict[str, object],
    ) -> ContextExecutionResult:
        self._logger.info("External context pending source=%s operation=%s user_id=%s", source, operation, user_id)
        return ContextExecutionResult(
            source=source,
            operation=operation,
            status="pending_implementation",
            detail="No external context was retrieved.",
        )

    @staticmethod
    def format_for_prompt(results: list[ContextExecutionResult]) -> str:
        if not results:
            return ""
        prompt_parts = [result.context for result in results if result.context]
        needs_source = [f"{result.source}/{result.operation}" for result in results if result.status == "needs_source"]
        if needs_source:
            prompt_parts.append(
                f"External context needs a source selection: {', '.join(needs_source)}. "
                "Ask the user to name a matching allowed chat, group, or channel; do not claim that a search occurred."
            )
        unavailable = [f"{result.source}/{result.operation}" for result in results if result.status != "available"]
        unavailable = [name for name in unavailable if name not in needs_source]
        if unavailable:
            prompt_parts.append(
                f"External context requested but unavailable: {', '.join(unavailable)}. "
                "Do not claim that you searched these sources or saw their data."
            )
        return "\n\n".join(prompt_parts)

    @classmethod
    def _format_web_context(cls, results: list[WebSearchResult]) -> str:
        parts = ["Web search context. Use only these supplied sources; cite the source URL when relying on a web fact."]
        for index, result in enumerate(results, start=1):
            item = f"[{index}] {result.title}\nURL: {result.url}"
            if result.snippet:
                item += f"\nSnippet: {result.snippet}"
            if result.page_text:
                item += f"\nPage excerpt: {result.page_text}"
            candidate = "\n\n".join([*parts, item])
            if len(candidate) > cls._WEB_CONTEXT_CHAR_LIMIT:
                break
            parts.append(item)
        return "\n\n".join(parts)
