"""Async HTTP client for Ollama chat, embeddings, health checks, and multimodal inputs."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx

from lumen.time_utils import local_now


OllamaModality = Literal["text", "image", "audio"]
_LOGGER = logging.getLogger("lumen")


class OllamaStreamProtocolError(RuntimeError):
    """Ollama returned an invalid or incomplete NDJSON chat stream."""


class OllamaStreamIdleTimeout(TimeoutError):
    """Ollama stopped producing valid NDJSON frames after stream progress."""


@dataclass
class OllamaInputPart:
    type: OllamaModality
    text: str | None = None
    data: bytes | None = None
    mime_type: str | None = None


@dataclass
class OllamaMessage:
    role: str
    content: str = ""
    parts: list[OllamaInputPart] = field(default_factory=list)


@dataclass
class OllamaConnector:
    base_url: str
    model: str = "gemma4:e4b"
    timeout_seconds: float = 120.0
    keep_alive: str = "30m"
    debug_dump_path: str = ""

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    def supports_modality(self, modality: OllamaModality) -> bool:
        return modality in {"text", "image"}

    def _dump_debug_payload(self, payload: dict[str, Any]) -> None:
        if not self.debug_dump_path.strip():
            return
        dump_path = Path(self.debug_dump_path)
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        temp_path = dump_path.with_suffix(dump_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(dump_path)

    @staticmethod
    def _runtime_context_message() -> OllamaMessage:
        now = local_now()
        timezone_name = now.tzname() or str(now.tzinfo or "")
        return OllamaMessage(
            role="system",
            content=(
                "Runtime context:\n"
                f"- Current local date and time: {now.isoformat()}\n"
                f"- Timezone: {timezone_name}\n"
                "Use this timestamp for all relative date/time reasoning."
            ),
        )

    @classmethod
    def _with_runtime_context(cls, messages: list[OllamaMessage]) -> list[OllamaMessage]:
        return [cls._runtime_context_message(), *messages]

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        *,
        response_format: str | dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        think: bool | str | None = None,
        include_runtime_context: bool = True,
    ) -> dict[str, Any]:
        multimodal_messages = [
            OllamaMessage(
                role=str(message.get("role", "user")).strip() or "user",
                content=str(message.get("content", "")),
            )
            for message in messages
        ]
        return await self.chat_multimodal(
            messages=multimodal_messages,
            model=model,
            response_format=response_format,
            options=options,
            think=think,
            include_runtime_context=include_runtime_context,
        )

    async def chat_multimodal(
        self,
        *,
        messages: list[OllamaMessage],
        model: str | None = None,
        response_format: str | dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        think: bool | str | None = None,
        include_runtime_context: bool = True,
    ) -> dict[str, Any]:
        payload = {
            "model": model or self.model,
            "messages": [
                self._serialize_message(message)
                for message in (self._with_runtime_context(messages) if include_runtime_context else messages)
            ],
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        if response_format is not None:
            payload["format"] = response_format
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = think
        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=self.timeout_seconds) as client:
            response = None
            try:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
                response_json = response.json()
            except Exception as exc:
                self._dump_debug_payload(
                    {
                        "kind": "chat_multimodal",
                        "request": payload,
                        "response": self._response_payload(response),
                        "error": repr(exc),
                    }
                )
                raise
        self._dump_debug_payload(
            {
                "kind": "chat_multimodal",
                "request": payload,
                "response": response_json,
            }
        )
        return response_json

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        idle_timeout_seconds: float,
        request_id: str = "",
    ) -> AsyncIterator[str]:
        """Yield final-dialogue content deltas from Ollama's NDJSON chat stream.

        The first valid frame intentionally has no deadline: Ollama does not
        expose prompt-evaluation progress.  Once any valid frame arrives,
        every subsequent frame must arrive within ``idle_timeout_seconds``.
        Empty and thinking-only frames still reset that watchdog.
        """
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be greater than zero")

        multimodal_messages = [
            OllamaMessage(
                role=str(message.get("role", "user")).strip() or "user",
                content=str(message.get("content", "")),
            )
            for message in messages
        ]
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                self._serialize_message(message)
                for message in self._with_runtime_context(multimodal_messages)
            ],
            "stream": True,
            "keep_alive": self.keep_alive,
        }
        response = None
        started_at = time.perf_counter()
        first_frame_ms: int | None = None
        received_frame = False
        done = False
        frame_count = 0
        content_parts: list[str] = []
        stream_timeout = httpx.Timeout(
            connect=self.timeout_seconds,
            read=None,
            write=self.timeout_seconds,
            pool=self.timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=stream_timeout) as client:
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    response.raise_for_status()
                    line_iterator = response.aiter_lines()
                    while True:
                        try:
                            if received_frame:
                                async with asyncio.timeout(idle_timeout_seconds):
                                    line = await anext(line_iterator)
                            else:
                                line = await anext(line_iterator)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            raise OllamaStreamIdleTimeout(
                                f"Ollama chat stream was idle for {idle_timeout_seconds:g} seconds after progress."
                            ) from exc

                        if not line.strip():
                            continue
                        try:
                            frame = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise OllamaStreamProtocolError("Ollama chat stream contains invalid JSON.") from exc
                        if not isinstance(frame, dict):
                            raise OllamaStreamProtocolError("Ollama chat stream frame must be a JSON object.")
                        if frame.get("error"):
                            raise OllamaStreamProtocolError(f"Ollama chat stream returned an error: {frame['error']}")

                        # A valid frame, even without content, proves that the
                        # upstream inference is still making observable progress.
                        if first_frame_ms is None:
                            first_frame_ms = round((time.perf_counter() - started_at) * 1000)
                        received_frame = True
                        frame_count += 1
                        message = frame.get("message")
                        if message is not None and not isinstance(message, dict):
                            raise OllamaStreamProtocolError("Ollama chat stream message must be a JSON object.")
                        content = "" if message is None else message.get("content", "")
                        if not isinstance(content, str):
                            raise OllamaStreamProtocolError("Ollama chat stream message content must be text.")
                        if content:
                            content_parts.append(content)
                            yield content
                        if frame.get("done") is True:
                            done = True
                            break
        except asyncio.CancelledError:
            _LOGGER.info(
                "Ollama stream request=%s model=%s elapsed_ms=%d first_frame_ms=%s frames=%d chars=%d outcome=cancelled",
                request_id,
                payload["model"],
                round((time.perf_counter() - started_at) * 1000),
                first_frame_ms,
                frame_count,
                sum(len(part) for part in content_parts),
            )
            raise
        except Exception as exc:
            outcome = (
                "idle_timeout"
                if isinstance(exc, OllamaStreamIdleTimeout)
                else "protocol_error"
                if isinstance(exc, OllamaStreamProtocolError)
                else "transport_error"
            )
            _LOGGER.warning(
                "Ollama stream request=%s model=%s elapsed_ms=%d first_frame_ms=%s frames=%d chars=%d outcome=%s",
                request_id,
                payload["model"],
                round((time.perf_counter() - started_at) * 1000),
                first_frame_ms,
                frame_count,
                sum(len(part) for part in content_parts),
                outcome,
            )
            self._dump_debug_payload(
                {
                    "kind": "stream_chat",
                    "request": payload,
                    "response": self._stream_response_payload(response),
                    "frames_received": frame_count,
                    "content_received": "".join(content_parts),
                    "done": done,
                    "error": repr(exc),
                }
            )
            raise

        if not done:
            error = OllamaStreamProtocolError("Ollama chat stream ended before its terminal done frame.")
            _LOGGER.warning(
                "Ollama stream request=%s model=%s elapsed_ms=%d first_frame_ms=%s frames=%d chars=%d outcome=protocol_error",
                request_id,
                payload["model"],
                round((time.perf_counter() - started_at) * 1000),
                first_frame_ms,
                frame_count,
                sum(len(part) for part in content_parts),
            )
            self._dump_debug_payload(
                {
                    "kind": "stream_chat",
                    "request": payload,
                    "response": self._stream_response_payload(response),
                    "frames_received": frame_count,
                    "content_received": "".join(content_parts),
                    "done": False,
                    "error": repr(error),
                }
            )
            raise error

        _LOGGER.info(
            "Ollama stream request=%s model=%s elapsed_ms=%d first_frame_ms=%s frames=%d chars=%d outcome=completed",
            request_id,
            payload["model"],
            round((time.perf_counter() - started_at) * 1000),
            first_frame_ms,
            frame_count,
            sum(len(part) for part in content_parts),
        )
        self._dump_debug_payload(
            {
                "kind": "stream_chat",
                "request": payload,
                "response": {"frames_received": frame_count, "content": "".join(content_parts), "done": True},
            }
        )

    async def embed(
        self,
        input: str | list[str],
        model: str | None = None,
        *,
        truncate: bool = True,
        dimensions: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "input": input,
            "truncate": truncate,
        }
        if dimensions is not None:
            payload["dimensions"] = dimensions
        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=self.timeout_seconds) as client:
            response = None
            try:
                response = await client.post("/api/embed", json=payload)
                response.raise_for_status()
                response_json = response.json()
            except Exception as exc:
                self._dump_debug_payload(
                    {
                        "kind": "embed",
                        "request": payload,
                        "response": self._response_payload(response),
                        "error": repr(exc),
                    }
                )
                raise
        self._dump_debug_payload(
            {
                "kind": "embed",
                "request": payload,
                "response": response_json,
            }
        )
        return response_json

    @staticmethod
    def _response_payload(response: httpx.Response | None) -> dict[str, Any] | None:
        if response is None:
            return None
        try:
            body: Any = response.json()
        except Exception:
            body = response.text
        return {
            "status_code": response.status_code,
            "body": body,
        }

    @staticmethod
    def _stream_response_payload(response: httpx.Response | None) -> dict[str, Any] | None:
        if response is None:
            return None
        return {"status_code": response.status_code}

    def _serialize_message(self, message: OllamaMessage) -> dict[str, Any]:
        content_parts: list[str] = []
        images: list[str] = []
        for part in message.parts:
            if part.type == "text":
                text = (part.text or "").strip()
                if text:
                    content_parts.append(text)
                continue
            if part.type == "image":
                if part.data is None:
                    raise ValueError("Image part is missing binary data.")
                images.append(base64.b64encode(part.data).decode("ascii"))
                continue
            if part.type == "audio":
                raise NotImplementedError("Audio input is reserved for future multimodal support; use STT for now.")
            raise ValueError(f"Unsupported Ollama modality: {part.type}")

        content = message.content.strip()
        if content:
            content_parts.insert(0, content)
        payload: dict[str, Any] = {
            "role": message.role,
            "content": "\n\n".join(part for part in content_parts if part).strip(),
        }
        if images:
            payload["images"] = images
        return payload

    async def healthcheck(self) -> bool:
        if not self.available:
            return False
        try:
            health_timeout = min(self.timeout_seconds, 5.0)
            async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=health_timeout) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
            return True
        except Exception:
            return False
