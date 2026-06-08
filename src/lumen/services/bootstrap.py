from __future__ import annotations

import json
from pathlib import Path

from lumen.connectors.home_assistant import HomeAssistantConnector
from lumen.knowledge.store import KnowledgeStore


class BootstrapService:
    def __init__(self, knowledge_store: KnowledgeStore, home_assistant: HomeAssistantConnector) -> None:
        self.knowledge_store = knowledge_store
        self.home_assistant = home_assistant

    async def sync_home_assistant_snapshot(self, output_dir: str) -> dict:
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        if not self.home_assistant.available:
            return {"status": "skipped", "reason": "home_assistant_unavailable"}

        snapshot = await self.home_assistant.snapshot_entities()
        snapshot_path = target_dir / "home-assistant-entities.json"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        indexed = self.knowledge_store.ingest_path(str(snapshot_path))
        return {
            "status": "ok",
            "snapshot_path": str(snapshot_path),
            "indexed_documents": indexed,
            "groups": {key: len(value) for key, value in snapshot.items()},
        }
