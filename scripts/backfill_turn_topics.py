"""Classify and store missing primary-topic indexes for conversation turns."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.services.memory import MemoryService
from lumen.storage.db import Database


async def backfill(
    *,
    database_path: Path,
    user_id: str,
    date: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    database = Database(str(database_path))
    database.init()
    store = MemoryStore(database.session)
    settings = Settings()
    service = MemoryService(
        store,
        OllamaConnector(
            base_url=settings.ollama_base_url,
            model=settings.ollama_dialogue_model or settings.ollama_model,
            timeout_seconds=settings.ollama_timeout_seconds,
        ),
        settings,
        default_user_id=user_id,
    )
    turns = store.list_turns(user_id=user_id, limit=10000)
    pending_turns = [
        turn
        for turn_id in store.turn_ids_missing_primary_topic_index(
            user_id=user_id,
            date=date,
            limit=limit,
        )
        if (turn := store.get_turn(user_id=user_id, turn_id=turn_id)) is not None
    ]
    if dry_run:
        print(f"pending={len(pending_turns)} total={len(turns)}", flush=True)
        return {"updated": 0, "empty_topic": 0, "pending": len(pending_turns)}

    updated = 0
    empty_topic = 0
    for turn in pending_turns:
        topic = await service.get_topic(f"User: {turn.question}\nAssistant: {turn.answer}")
        if topic:
            store.set_turn_primary_topic(
                user_id=user_id,
                turn_id=turn.id,
                primary_topic=topic,
                primary_topic_embedding=await service.embed_dialogue_topic(topic),
            )
            updated += 1
        else:
            empty_topic += 1
        print(f"turn={turn.id} topic={topic or '<none>'}", flush=True)
    return {"updated": updated, "empty_topic": empty_topic, "pending": len(pending_turns)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Process every turn missing a topic or topic embedding")
    scope.add_argument("--date", help="Process only one local calendar date in YYYY-MM-DD format")
    parser.add_argument("--user-id", default="VST")
    parser.add_argument("--database", type=Path, default=Path("data/lumen.db"))
    parser.add_argument("--limit", type=int, help="Maximum pending turns to process")
    parser.add_argument("--dry-run", action="store_true", help="Show pending count without Ollama calls or database writes")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    result = asyncio.run(
        backfill(
            database_path=arguments.database,
            user_id=arguments.user_id,
            date=arguments.date,
            limit=arguments.limit,
            dry_run=arguments.dry_run,
        )
    )
    print(" ".join(f"{key}={value}" for key, value in result.items()), flush=True)
