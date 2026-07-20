"""Rebuild dialogue storage from a Telegram Desktop JSON export."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.storage.db import Database


async def embed_batch(ollama: OllamaConnector, texts: list[str], model: str) -> list[list[float]]:
    prepared = [text[:12000] for text in texts]
    try:
        payload = await ollama.embed(prepared, model=model)
        embeddings = payload.get("embeddings", [])
        if len(embeddings) != len(prepared):
            raise RuntimeError("Telegram import received an incomplete embedding batch")
        return embeddings
    except Exception:
        if len(prepared) == 1:
            raise
        middle = len(prepared) // 2
        return [
            *await embed_batch(ollama, prepared[:middle], model),
            *await embed_batch(ollama, prepared[middle:], model),
        ]


def text_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(text_value(item) for item in value).strip()
    if isinstance(value, dict):
        return text_value(value.get("text", ""))
    return ""


def dialogue_pairs(messages: list[dict[str, object]], *, user_name: str, bot_name: str) -> list[tuple[dict[str, object], str]]:
    pairs: list[tuple[dict[str, object], str]] = []
    pending: dict[str, object] | None = None
    answers: list[str] = []
    for message in messages:
        author = str(message.get("from", ""))
        text = text_value(message.get("text", ""))
        if author == user_name:
            if pending is not None and answers:
                pairs.append((pending, "\n\n".join(answers)))
            pending, answers = message, []
        elif author == bot_name and pending is not None and text:
            answers.append(text)
    if pending is not None and answers:
        pairs.append((pending, "\n\n".join(answers)))
    return pairs


def clear_runtime_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'memory_facts'"
        ).fetchall()
        for row in tables:
            connection.execute(f'DROP TABLE "{row[0]}"')
        connection.commit()


async def rebuild(args: argparse.Namespace) -> None:
    export = json.loads(args.export.read_text(encoding="utf-8"))
    messages = [item for item in export.get("messages", []) if isinstance(item, dict)]
    pairs = dialogue_pairs(messages, user_name=args.user_name, bot_name=args.bot_name)
    if not pairs:
        raise RuntimeError("No user-to-bot dialogue pairs found in the Telegram export")

    database = Database(str(args.database))
    if args.resume:
        store = MemoryStore(database.session)
        turns = store.unprocessed_turns(user_id=args.user_id, limit=5000)
        turn_ids = [turn.id for turn in turns]
        pairs = [({"text": turn.question}, turn.answer) for turn in turns]
    else:
        clear_runtime_tables(args.database)
        database.init()
        store = MemoryStore(database.session)
        turn_ids = []
        for message, answer in pairs:
            created_at = str(message.get("date", ""))
            turn_ids.append(
                store.add_turn(
                    user_id=args.user_id,
                    source="telegram_export",
                    question=text_value(message.get("text", "")),
                    answer=answer,
                    question_created_at=created_at,
                    answer_created_at=created_at,
                    created_at=created_at,
                    turn_meta={"telegram_message_id": message.get("id"), "imported_from": args.export.name},
                )
            )

    settings = Settings()
    ollama = OllamaConnector(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
    for start in range(0, len(pairs), args.batch_size):
        batch = pairs[start : start + args.batch_size]
        questions = await embed_batch(
            ollama,
            [text_value(message.get("text", "")) for message, _ in batch],
            settings.ollama_embedding_model,
        )
        answers = await embed_batch(ollama, [answer for _, answer in batch], settings.ollama_embedding_model)
        for index, turn_id in enumerate(turn_ids[start : start + len(batch)]):
            store.set_turn_embeddings(
                user_id=args.user_id,
                turn_id=turn_id,
                question_embedding=questions[index],
                answer_embedding=answers[index],
            )
    print(f"imported_turns={len(turn_ids)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--user-id", default="VST")
    parser.add_argument("--user-name", default="VST")
    parser.add_argument("--bot-name", default="Lumen")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--resume", action="store_true")
    asyncio.run(rebuild(parser.parse_args()))


if __name__ == "__main__":
    main()
