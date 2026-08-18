from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lumen.connectors.ollama import (
    OllamaConnector,
    OllamaInputPart,
    OllamaMessage,
    OllamaStreamIdleTimeout,
    OllamaStreamProtocolError,
)


class FakeStreamResponse:
    def __init__(self, lines: list[str] | None = None, *, delayed_lines: list[tuple[float, str]] | None = None) -> None:
        self._lines = lines or []
        self._delayed_lines = delayed_lines or []
        self.status_code = 200
        self.text = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        for delay, line in self._delayed_lines:
            await asyncio.sleep(delay)
            yield line


class FakeAsyncClient:
    def __init__(self, *, response: FakeStreamResponse, **kwargs) -> None:
        self.response = response
        self.kwargs = kwargs
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class OllamaConnectorRuntimeContextTest(unittest.TestCase):
    def test_chat_messages_are_prefixed_with_runtime_context(self) -> None:
        messages = [
            OllamaMessage(role="system", content="Existing system prompt."),
            OllamaMessage(role="user", content="What is today?"),
        ]

        enriched = OllamaConnector._with_runtime_context(messages)

        self.assertEqual(enriched[0].role, "system")
        self.assertIn("Runtime context:", enriched[0].content)
        self.assertIn("Current local date and time:", enriched[0].content)
        self.assertIn("Timezone:", enriched[0].content)
        self.assertEqual(enriched[1:], messages)

    def test_runtime_context_preserves_multimodal_messages(self) -> None:
        image_message = OllamaMessage(
            role="user",
            parts=[
                OllamaInputPart(type="text", text="Analyze this."),
                OllamaInputPart(type="image", data=b"image-bytes", mime_type="image/png"),
            ],
        )

        enriched = OllamaConnector._with_runtime_context([image_message])

        self.assertEqual(enriched[0].role, "system")
        self.assertEqual(enriched[1].parts[1].data, b"image-bytes")


class OllamaConnectorStreamChatTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, connector: OllamaConnector, **kwargs: object) -> list[str]:
        return [chunk async for chunk in connector.stream_chat([{"role": "user", "content": "Hello"}], **kwargs)]

    async def test_stream_chat_yields_content_deltas_and_keeps_runtime_context(self) -> None:
        response = FakeStreamResponse(
            lines=[
                json.dumps({"model": "test", "message": {"role": "assistant", "thinking": "reasoning", "content": ""}, "done": False}),
                json.dumps({"model": "test", "message": {"role": "assistant", "content": "Hello"}, "done": False}),
                json.dumps({"model": "test", "message": {"role": "assistant", "content": " world"}, "done": True}),
            ]
        )
        client = FakeAsyncClient(response=response)
        connector = OllamaConnector(base_url="http://ollama.test")

        with patch("lumen.connectors.ollama.httpx.AsyncClient", return_value=client):
            chunks = await self._collect(connector, idle_timeout_seconds=240)

        self.assertEqual(["Hello", " world"], chunks)
        self.assertEqual(1, len(client.calls))
        method, url, kwargs = client.calls[0]
        self.assertEqual(("POST", "/api/chat"), (method, url))
        self.assertTrue(kwargs["json"]["stream"])
        self.assertEqual("Runtime context:", kwargs["json"]["messages"][0]["content"][:16])

    async def test_stream_chat_allows_a_slow_first_frame(self) -> None:
        response = FakeStreamResponse(delayed_lines=[(0.02, json.dumps({"message": {"content": "late"}, "done": True}))])
        connector = OllamaConnector(base_url="http://ollama.test")

        with patch("lumen.connectors.ollama.httpx.AsyncClient", return_value=FakeAsyncClient(response=response)):
            chunks = await self._collect(connector, idle_timeout_seconds=0.001)

        self.assertEqual(["late"], chunks)

    async def test_stream_chat_times_out_only_between_valid_frames(self) -> None:
        response = FakeStreamResponse(
            delayed_lines=[
                (0, json.dumps({"message": {"content": "first"}, "done": False})),
                (0.02, json.dumps({"message": {"content": "late"}, "done": True})),
            ]
        )
        connector = OllamaConnector(base_url="http://ollama.test")

        with patch("lumen.connectors.ollama.httpx.AsyncClient", return_value=FakeAsyncClient(response=response)):
            with self.assertRaises(OllamaStreamIdleTimeout):
                await self._collect(connector, idle_timeout_seconds=0.001)

    async def test_stream_chat_has_no_total_deadline_while_frames_keep_arriving(self) -> None:
        response = FakeStreamResponse(
            delayed_lines=[
                (0.04, json.dumps({"message": {"content": "one"}, "done": False})),
                (0.04, json.dumps({"message": {"content": " two"}, "done": False})),
                (0.04, json.dumps({"message": {"content": " three"}, "done": True})),
            ]
        )
        connector = OllamaConnector(base_url="http://ollama.test")

        with patch("lumen.connectors.ollama.httpx.AsyncClient", return_value=FakeAsyncClient(response=response)):
            chunks = await self._collect(connector, idle_timeout_seconds=0.1)

        self.assertEqual(["one", " two", " three"], chunks)

    async def test_stream_chat_rejects_malformed_or_incomplete_ndjson(self) -> None:
        connector = OllamaConnector(base_url="http://ollama.test")
        malformed = FakeStreamResponse(lines=["not-json"])

        with patch("lumen.connectors.ollama.httpx.AsyncClient", return_value=FakeAsyncClient(response=malformed)):
            with self.assertRaises(OllamaStreamProtocolError):
                await self._collect(connector, idle_timeout_seconds=240)

        incomplete = FakeStreamResponse(lines=[json.dumps({"message": {"content": "partial"}, "done": False})])
        with patch("lumen.connectors.ollama.httpx.AsyncClient", return_value=FakeAsyncClient(response=incomplete)):
            with self.assertRaises(OllamaStreamProtocolError):
                await self._collect(connector, idle_timeout_seconds=240)

    async def test_stream_chat_propagates_cancellation(self) -> None:
        response = FakeStreamResponse(delayed_lines=[(1, json.dumps({"message": {"content": "late"}, "done": True}))])
        connector = OllamaConnector(base_url="http://ollama.test")
        with patch("lumen.connectors.ollama.httpx.AsyncClient", return_value=FakeAsyncClient(response=response)):
            task = asyncio.create_task(self._collect(connector, idle_timeout_seconds=240))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
