from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class OllamaConnector:
    base_url: str
    model: str = "gemma4:e4b"
    timeout_seconds: float = 120.0
    keep_alive: str = "30m"

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    async def chat(self, messages: list[dict[str, str]], model: str | None = None) -> dict[str, Any]:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=self.timeout_seconds) as client:
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
            return response.json()

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
            response = await client.post("/api/embed", json=payload)
            response.raise_for_status()
            return response.json()

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
