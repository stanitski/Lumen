"""Read bounded Telegram context through an authorized local Telethon session."""

from __future__ import annotations

import logging
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from lumen.config import Settings
from lumen.memory.store import MemoryStore
from lumen.services.speech_to_text import SpeechToTextError, SpeechToTextService
from lumen.time_utils import LOCAL_TIMEZONE, local_now


@dataclass(frozen=True)
class TelegramLookupResult:
    status: str
    detail: str
    context: str = ""


@dataclass(frozen=True)
class TelegramSource:
    ref: str
    kind: str


class TelegramContextService:
    _MAX_CONTEXT_CHARS = 24000
    _EXPLICIT_PERIOD_SCAN_LIMIT = 500

    def __init__(
        self,
        settings: Settings,
        store: MemoryStore,
        speech_to_text: SpeechToTextService,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._speech_to_text = speech_to_text
        self._logger = logger or logging.getLogger("lumen")

    async def search(self, request: dict[str, object], *, user_id: str, user_text: str) -> TelegramLookupResult:
        allowed_sources = self._allowed_sources(user_id)
        if not self._settings.telegram_api_id or not self._settings.telegram_api_hash.strip() or not self._settings.telegram_session_path.strip():
            return TelegramLookupResult("unavailable", "Telegram MTProto settings are not configured.")
        selected_sources = self._select_sources(request=request, user_text=user_text, allowed_sources=allowed_sources)
        if selected_sources is None:
            return TelegramLookupResult("unavailable", "Requested Telegram source is not in the allowed-source policy.")
        if not selected_sources:
            return TelegramLookupResult("needs_source", "No allowed Telegram source matches the requested chat or channel type.")
        date_from, date_to = self._requested_dates(request)
        try:
            from telethon import TelegramClient
            from telethon.errors import RPCError
            from telethon.tl.types import Channel, Chat, User
        except ImportError:
            return TelegramLookupResult("unavailable", "Telethon is not installed.")

        client = TelegramClient(self._settings.telegram_session_path, self._settings.telegram_api_id, self._settings.telegram_api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return TelegramLookupResult("unavailable", "Telegram session is not authorized.")
            messages: list[dict[str, str]] = []
            failures: list[str] = []
            for source in selected_sources:
                try:
                    entity = await client.get_entity(source.ref)
                    # An explicit personal-chat request grants access only to a User.
                    if source.kind == "user" and not isinstance(entity, User):
                        failures.append(f"{source.ref}: Requested personal chat resolves to a non-user entity.")
                        continue
                    kind = "channel" if isinstance(entity, Channel) and bool(getattr(entity, "broadcast", False)) else "group" if isinstance(entity, (Channel, Chat)) else "user" if isinstance(entity, User) else "unknown"
                    title = self._source_title(entity, source.ref)
                    cutoff = local_now() - timedelta(hours=self._history_hours(kind))
                    source_messages: list[dict[str, str]] = []
                    limit = self._EXPLICIT_PERIOD_SCAN_LIMIT if date_from else self._message_limit()
                    async for message in client.iter_messages(entity, limit=limit):
                        message_date = getattr(message, "date", None)
                        if message_date and date_from:
                            local_date = message_date.astimezone(LOCAL_TIMEZONE).date()
                            if date_to and local_date > date_to:
                                continue
                            if local_date < date_from:
                                break
                        elif message_date and message_date < cutoff:
                            break
                        text, media_kind = await self._message_text(
                            client,
                            message,
                            transcribe_media=kind != "channel",
                        )
                        if text:
                            source_messages.append({
                                "source": title,
                                "date": message_date.isoformat() if message_date else "",
                                "direction": "outgoing" if bool(getattr(message, "out", False)) else "incoming",
                                "text": text,
                                "media": media_kind,
                            })
                    messages.extend(reversed(source_messages))
                except (RPCError, ValueError) as exc:
                    failures.append(f"{source.ref}: {exc}")
            if not messages:
                detail = "No readable Telegram messages were found."
                if failures:
                    detail += " " + "; ".join(failures[:3])
                return TelegramLookupResult("unavailable", detail)
            self._logger.info("Read %s Telegram message(s) for user_id=%s", len(messages), user_id)
            return TelegramLookupResult("available", f"Read {len(messages)} Telegram message(s).", self._format_context(messages))
        finally:
            await client.disconnect()

    @staticmethod
    def _direct_source_refs(text: str) -> list[str]:
        refs = re.findall(r"(?i)(?:https?://)?t\.me/[^\s,;]+|(?<!\w)@[A-Za-z0-9_]{5,32}\b", text)
        return [ref.rstrip(".,;:!?()[]{}<>\"'") for ref in refs]

    @classmethod
    def _normalize_ref(cls, value: str) -> str:
        value = value.strip().rstrip(".,;:!?()[]{}<>\"'")
        if value.casefold().startswith("t.me/"):
            return "https://" + value
        return value

    def _allowed_sources(self, user_id: str) -> list[TelegramSource]:
        sources: list[TelegramSource] = []
        for fact in self._store.list_facts(user_id, limit=500, include_global=False):
            if str(fact.get("category", "")) != "messenger_context" or str(fact.get("predicate", "")) != "uses":
                continue
            tags = fact.get("tags", [])
            if not isinstance(tags, list) or not {"telegram", "search_source"}.issubset({str(tag) for tag in tags}):
                continue
            raw_value = str(fact.get("value", "")).strip()
            try:
                payload = json.loads(raw_value)
            except json.JSONDecodeError:
                payload = {"ref": raw_value}
            if not isinstance(payload, dict):
                continue
            source = self._normalize_ref(str(payload.get("ref", "")))
            kind = str(payload.get("kind", "")).strip().lower()
            if source and kind in {"user", "group", "channel"}:
                sources.append(TelegramSource(source, kind))
        return list({source.ref.casefold(): source for source in sources}.values())

    def _select_sources(
        self,
        *,
        request: dict[str, object],
        user_text: str,
        allowed_sources: list[TelegramSource],
    ) -> list[TelegramSource] | None:
        requested_kind = str(request.get("source_kind", "")).strip().lower()
        direct = self._direct_source_refs(f"{user_text}\n{request.get('query', '')}")
        by_ref = {source.ref.casefold(): source for source in allowed_sources}
        explicit_refs = {self._normalize_ref(ref).casefold() for ref in self._direct_source_refs(user_text)}
        if not direct:
            return [source for source in allowed_sources if source.kind == requested_kind] if requested_kind in {"user", "group", "channel"} else []
        selected: list[TelegramSource] = []
        for ref in dict.fromkeys(self._normalize_ref(source) for source in direct):
            source = by_ref.get(ref.casefold())
            if source is None:
                if ref.casefold() not in explicit_refs or requested_kind not in {"user", "unspecified", ""}:
                    return None
                source = TelegramSource(ref, "user")
            if requested_kind in {"user", "group", "channel"} and source.kind != requested_kind:
                return []
            selected.append(source)
        return selected

    def _message_limit(self) -> int:
        return max(1, min(int(self._settings.telegram_default_message_limit), 100))

    @staticmethod
    def _requested_dates(request: dict[str, object]) -> tuple[date | None, date | None]:
        try:
            date_from = date.fromisoformat(str(request.get("date_from", "")).strip())
            date_to = date.fromisoformat(str(request.get("date_to", "")).strip())
        except ValueError:
            return None, None
        return (date_from, date_to) if date_from <= date_to else (None, None)

    def _history_hours(self, kind: str) -> int:
        value = self._settings.telegram_channel_history_hours if kind == "channel" else self._settings.telegram_dialog_history_hours
        return max(1, int(value))

    async def _message_text(
        self,
        client: Any,
        message: Any,
        *,
        transcribe_media: bool = True,
    ) -> tuple[str, str]:
        text = str(getattr(message, "message", "") or "").strip()
        media_kind = self._media_kind(message)
        if not media_kind:
            return text, ""
        if not transcribe_media:
            return text, media_kind
        try:
            raw = await client.download_media(message, file=bytes)
            transcript = await self._speech_to_text.transcribe_bytes(
                raw=raw or b"", file_name=f"telegram-{getattr(message, 'id', 'media')}.{ 'mp4' if media_kind == 'video' else 'ogg'}", mime_type="video/mp4" if media_kind == "video" else "audio/ogg"
            )
        except (SpeechToTextError, OSError, ValueError, IndexError) as exc:
            self._logger.warning("Telegram media transcription failed: %s", exc)
            return text, media_kind
        return f"{text}\n{transcript}".strip(), media_kind

    @staticmethod
    def _media_kind(message: Any) -> str:
        if any(getattr(message, name, None) for name in ("video", "video_note")):
            return "video"
        if any(getattr(message, name, None) for name in ("voice", "audio")):
            return "audio"
        mime = str(getattr(getattr(message, "document", None), "mime_type", "") or "").lower()
        return "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else ""

    @staticmethod
    def _source_title(entity: Any, fallback: str) -> str:
        return str(getattr(entity, "title", "") or getattr(entity, "username", "") or getattr(entity, "first_name", "") or fallback)

    def _format_context(self, messages: list[dict[str, str]]) -> str:
        lines = ["Telegram context (temporary, read-only):"]
        chars = len(lines[0])
        for message in messages:
            line = "- source={source} | date={date} | direction={direction} | media={media} | text={text}".format(**message)
            if chars + len(line) > self._MAX_CONTEXT_CHARS:
                lines.append("- [context truncated]")
                break
            lines.append(line)
            chars += len(line)
        return "\n".join(lines)
