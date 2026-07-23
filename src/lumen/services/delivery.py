"""Outbound delivery service for Home Assistant callbacks."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from lumen.config import Settings
from lumen.models import HomeAssistantCallbackPayload


class DeliveryService:
    VOICE_CHUNK_MAX_CHARS = 1800

    def __init__(self, settings: Settings, logger: logging.Logger | None = None) -> None:
        self._settings = settings
        self._logger = logger or logging.getLogger("lumen")

    @staticmethod
    def _normalize_plain_text(text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"^\s{0,3}#{1,6}\s*", "", normalized, flags=re.MULTILINE)
        normalized = re.sub(r"\*\*(.+?)\*\*", r"\1", normalized, flags=re.DOTALL)
        normalized = re.sub(r"__(.+?)__", r"\1", normalized, flags=re.DOTALL)
        normalized = re.sub(r"`{1,3}(.+?)`{1,3}", r"\1", normalized, flags=re.DOTALL)
        normalized = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", normalized, flags=re.DOTALL)
        normalized = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", normalized, flags=re.DOTALL)
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    @staticmethod
    def _split_voice_chunks(text: str, max_chars: int) -> list[str]:
        cleaned = DeliveryService._normalize_plain_text(text)
        if not cleaned:
            return []
        if len(cleaned) <= max_chars:
            return [cleaned]

        paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", cleaned) if part.strip()]
        if not paragraphs:
            paragraphs = [part.strip() for part in re.split(r"\n+", cleaned) if part.strip()]
        if not paragraphs:
            return [cleaned]

        chunks: list[str] = []
        current = ""

        def flush_current() -> None:
            nonlocal current
            if current.strip():
                chunks.append(current.strip())
            current = ""

        def append_piece(piece: str) -> None:
            nonlocal current
            piece = piece.strip()
            if not piece:
                return
            if not current:
                current = piece
                return
            if len(current) + 2 + len(piece) <= max_chars:
                current = f"{current}\n\n{piece}"
                return
            flush_current()
            current = piece

        def split_oversized_piece(piece: str) -> list[str]:
            piece = piece.strip()
            if not piece:
                return []
            if len(piece) <= max_chars:
                return [piece]

            sentence_candidates = [
                candidate.strip()
                for candidate in re.split(r"(?<=[.!?…])\s+", piece)
                if candidate.strip()
            ]
            if not sentence_candidates:
                sentence_candidates = [piece]

            result: list[str] = []
            sentence_bucket = ""

            def flush_sentence_bucket() -> None:
                nonlocal sentence_bucket
                if sentence_bucket.strip():
                    result.append(sentence_bucket.strip())
                sentence_bucket = ""

            for sentence in sentence_candidates:
                if len(sentence) <= max_chars:
                    candidate = sentence if not sentence_bucket else f"{sentence_bucket} {sentence}"
                    if len(candidate) <= max_chars:
                        sentence_bucket = candidate
                    else:
                        flush_sentence_bucket()
                        sentence_bucket = sentence
                    continue

                flush_sentence_bucket()
                word_bucket = ""
                for word in sentence.split():
                    if not word:
                        continue
                    if len(word) > max_chars:
                        if word_bucket.strip():
                            result.append(word_bucket.strip())
                            word_bucket = ""
                        start = 0
                        while start < len(word):
                            result.append(word[start : start + max_chars])
                            start += max_chars
                        continue

                    candidate = word if not word_bucket else f"{word_bucket} {word}"
                    if len(candidate) <= max_chars:
                        word_bucket = candidate
                    else:
                        if word_bucket.strip():
                            result.append(word_bucket.strip())
                        word_bucket = word

                if word_bucket.strip():
                    result.append(word_bucket.strip())

            flush_sentence_bucket()
            return result

        for paragraph in paragraphs:
            pieces = split_oversized_piece(paragraph)
            if not pieces:
                continue
            for piece in pieces:
                append_piece(piece)

        flush_current()
        return chunks or [cleaned]

    def home_assistant_callback_url(self) -> str:
        callback_url = self._settings.home_assistant_webhook_url.strip()
        if callback_url:
            return callback_url

        webhook_id = self._settings.home_assistant_webhook_id.strip()
        if webhook_id:
            base_url = self._settings.home_assistant_url.strip().rstrip("/")
            if not base_url:
                raise RuntimeError("Home Assistant URL is not configured.")
            return f"{base_url}/api/webhook/{webhook_id}"

        raise RuntimeError("Home Assistant webhook is not configured.")

    async def post_home_assistant_callback(self, payload: HomeAssistantCallbackPayload) -> None:
        callback_url = self.home_assistant_callback_url()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        callback_path = urlparse(callback_url).path
        if "/api/webhook/" not in callback_path and self._settings.home_assistant_token.strip():
            headers["Authorization"] = f"Bearer {self._settings.home_assistant_token.strip()}"

        self._logger.debug(
            "Posting Home Assistant callback kind=%s request_id=%s reminder_id=%s delivery_id_present=%s voice=%s",
            payload.kind,
            payload.request_id,
            payload.reminder_id,
            bool(payload.metadata.get("delivery_id")),
            payload.voice,
        )
        async with httpx.AsyncClient(timeout=self._settings.ollama_timeout_seconds) as client:
            response = await client.post(callback_url, headers=headers, json=payload.model_dump())

        if response.is_success:
            self._logger.debug(
                "Home Assistant callback accepted status=%s kind=%s request_id=%s reminder_id=%s delivery_id_present=%s voice=%s",
                response.status_code,
                payload.kind,
                payload.request_id,
                payload.reminder_id,
                bool(payload.metadata.get("delivery_id")),
                payload.voice,
            )
            return
        raise RuntimeError(
            f"Home Assistant callback failed with status {response.status_code}."
        )

    async def deliver_chat_answer(
        self,
        *,
        request_id: str,
        user_id: str,
        answer: str,
        voice: bool,
        metadata: dict[str, object] | None = None,
    ) -> None:
        normalized_answer = self._normalize_plain_text(answer)
        voice_chunks = self._split_voice_chunks(normalized_answer, self.VOICE_CHUNK_MAX_CHARS) if voice else []
        callback_answer = voice_chunks[0] if voice_chunks else normalized_answer
        await self.post_home_assistant_callback(
            HomeAssistantCallbackPayload(
                request_id=request_id,
                kind="chat",
                user_id=user_id,
                answer=callback_answer,
                voice=voice,
                voice_chunks=voice_chunks,
                metadata=metadata or {},
            )
        )

    async def deliver_reminder(
        self,
        *,
        reminder_id: int,
        user_id: str,
        answer: str,
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        normalized_answer = self._normalize_plain_text(answer)
        voice_chunks = self._split_voice_chunks(normalized_answer, self.VOICE_CHUNK_MAX_CHARS)
        callback_answer = voice_chunks[0] if voice_chunks else normalized_answer
        await self.post_home_assistant_callback(
            HomeAssistantCallbackPayload(
                request_id=request_id or f"reminder:{reminder_id}",
                kind="reminder",
                user_id=user_id,
                answer=callback_answer,
                voice=True,
                voice_chunks=voice_chunks,
                reminder_id=reminder_id,
                metadata=metadata or {},
            )
        )

    async def deliver_knowledge_message(
        self,
        *,
        request_id: str,
        user_id: str,
        answer: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        normalized_answer = self._normalize_plain_text(answer)
        await self.post_home_assistant_callback(
            HomeAssistantCallbackPayload(
                request_id=request_id,
                kind="knowledge",
                user_id=user_id,
                answer=normalized_answer,
                voice=False,
                voice_chunks=[],
                metadata=metadata or {},
            )
        )
