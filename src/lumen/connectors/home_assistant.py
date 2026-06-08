from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class HomeAssistantConnector:
    base_url: str
    token: str
    timeout_seconds: float = 10.0

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.token)

    async def execute_service(self, domain: str, service: str, service_data: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/services/{domain}/{service}", json=service_data)

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            return await self._request("GET", f"/api/states/{entity_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def list_entities(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/states")

    async def list_scripts(self) -> list[str]:
        entities = await self.list_entities()
        return [entity["entity_id"] for entity in entities if entity.get("entity_id", "").startswith("script.")]

    async def list_scenes(self) -> list[str]:
        entities = await self.list_entities()
        return [entity["entity_id"] for entity in entities if entity.get("entity_id", "").startswith("scene.")]

    async def list_input_booleans(self) -> list[str]:
        entities = await self.list_entities()
        return [entity["entity_id"] for entity in entities if entity.get("entity_id", "").startswith("input_boolean.")]

    async def snapshot_entities(self) -> dict[str, list[dict[str, Any]]]:
        entities = await self.list_entities()
        return {
            "scripts": [entity for entity in entities if entity.get("entity_id", "").startswith("script.")],
            "scenes": [entity for entity in entities if entity.get("entity_id", "").startswith("scene.")],
            "input_booleans": [entity for entity in entities if entity.get("entity_id", "").startswith("input_boolean.")],
        }

    async def healthcheck(self) -> bool:
        if not self.available:
            return False
        try:
            await self._request("GET", "/api/")
            return True
        except Exception:
            return False

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.request(method, path, json=json)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
