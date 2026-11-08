"""Audit/apply an explicit fact-inclusion manifest and backfill fact embeddings.

Defaults to a read-only preview. No schema initialization or dialogue delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.fact_policy import (
    FACT_EMBEDDING_SOURCE_PREFIX, FACT_EMBEDDING_SOURCE_TYPE, MANDATORY_TAG,
    fact_content_hash, is_prompt_fact,
)
from lumen.memory.store import MemoryStore
from lumen.services.fact_context import FactContextService
from lumen.storage.db import Database


def read_manifest(path: Path | None) -> list[dict]:
    if path is None:
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Manifest must be an array")
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "expected_hash", "inclusion"}:
            raise ValueError("Each manifest row requires id, expected_hash, inclusion")
        if isinstance(row["id"], bool) or not isinstance(row["id"], int) or row["id"] < 1 or row["id"] in seen:
            raise ValueError("Manifest IDs must be unique positive integers")
        if row["inclusion"] not in {"mandatory", "contextual"}:
            raise ValueError("Invalid inclusion policy")
        if not isinstance(row["expected_hash"], str) or len(row["expected_hash"]) != 64:
            raise ValueError("A reviewed content hash is required")
        seen.add(row["id"])
    return rows


def apply_manifest(connection: sqlite3.Connection, user_id: str, manifest: list[dict]) -> int:
    """Change tags only after validating every expected row inside one transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        updates = []
        for item in manifest:
            row = connection.execute(
                "SELECT * FROM memory_facts WHERE user_id=? AND id=?", (user_id, item["id"])
            ).fetchone()
            if row is None or fact_content_hash(dict(row)) != item["expected_hash"]:
                raise ValueError(f"Fact {item['id']} changed or is outside the selected user")
            fact = dict(row)
            fact["tags"] = json.loads(row["tags_json"])
            if not is_prompt_fact(fact):
                raise ValueError(f"Fact {item['id']} is not eligible for prompt inclusion")
            if item["inclusion"] == "mandatory" and user_id.casefold() == "global":
                raise ValueError("Mandatory policy must belong to a personal user")
            tags = [tag for tag in fact["tags"] if tag != MANDATORY_TAG]
            if item["inclusion"] == "mandatory":
                tags.append(MANDATORY_TAG)
            updates.append((json.dumps(tags, ensure_ascii=False), user_id, item["id"]))
        for tags, owner, fact_id in updates:
            connection.execute("UPDATE memory_facts SET tags_json=? WHERE user_id=? AND id=?", (tags, owner, fact_id))
            connection.execute(
                "DELETE FROM memory_embeddings WHERE user_id=? AND source_type=? AND source_ref=?",
                (owner, FACT_EMBEDDING_SOURCE_TYPE, f"{FACT_EMBEDDING_SOURCE_PREFIX}{fact_id}"),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(updates)


async def prepare(*, db_path: Path, user_id: str, manifest_path: Path | None = None,
                  apply: bool = False, index: bool = False, backup_dir: Path | None = None) -> dict:
    db_path = db_path.resolve(strict=True)
    manifest = read_manifest(manifest_path)
    uri = db_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source:
        source.row_factory = sqlite3.Row
        facts = [dict(row) for row in source.execute(
            "SELECT * FROM memory_facts WHERE user_id=? ORDER BY id", (user_id,)
        )]
        by_id = {row["id"]: row for row in facts}
        for item in manifest:
            fact = by_id.get(item["id"])
            if fact is None or fact_content_hash(fact) != item["expected_hash"]:
                raise ValueError(f"Fact {item['id']} changed or is outside the selected user")
        result = {
            "mode": "apply" if apply else "preview", "user_id": user_id,
            "facts": len(facts), "manifest_ids": [item["id"] for item in manifest],
            "index_requested": index,
        }
        if not apply:
            return result
        if not manifest and not index:
            return result
        destination = backup_dir or db_path.parent / "backups"
        destination.mkdir(parents=True, exist_ok=True)
        backup = destination / f"{db_path.stem}-before-fact-memory-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.db"
        with closing(sqlite3.connect(backup)) as copy:
            source.backup(copy)
        result["backup"] = str(backup.resolve())
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        result["marked"] = apply_manifest(connection, user_id, manifest)
        result["mandatory_ids"] = [row["id"] for row in connection.execute(
            "SELECT id,tags_json FROM memory_facts WHERE user_id=? ORDER BY id", (user_id,)
        ) if MANDATORY_TAG in json.loads(row["tags_json"])]
    if index:
        settings = Settings()
        store = MemoryStore(Database(str(db_path)).session)  # Existing schema only: never init().
        ollama = OllamaConnector(base_url=settings.ollama_base_url, model=settings.ollama_model,
                                 timeout_seconds=settings.ollama_timeout_seconds,
                                 keep_alive=settings.ollama_client_keep_alive)
        result["index"] = await FactContextService(store, ollama, settings).sync_index(user_id=user_id)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--index", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    result = asyncio.run(prepare(db_path=args.db or Path(Settings().lumen_db_path), user_id=args.user_id,
                                 manifest_path=args.manifest, apply=args.apply, index=args.index,
                                 backup_dir=args.backup_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("index", {}).get("failed", 0):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
