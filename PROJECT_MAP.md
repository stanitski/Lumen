# Lumen Project Map

## Overview

Lumen is a local FastAPI assistant with one active runtime: chat, memory, knowledge retrieval, bounded external context, speech-to-text for connector attachments, and Home Assistant callback delivery.

## Main directories

- `src/lumen/main.py`: HTTP endpoints and service wiring.
- `src/lumen/services/`: chat orchestration, memory, context execution, Telegram/web context, knowledge, STT, and delivery.
- `src/lumen/memory/`: SQLite reads/writes and retrieval helpers.
- `src/lumen/storage/`: current SQLite schema.
- `src/lumen/connectors/`: Ollama client.
- `tests/`: focused chat/runtime/API regression tests.
- `data/`: local database, logs, uploaded knowledge files, and backups.

## Active modules and flows

1. `POST /chat` starts `ChatOnlyService`.
2. `POST /connector` accepts text or STT-supported attachments.
3. Audio/video attachments are normalized with `ffmpeg`, transcribed by `faster-whisper`, then routed into the same text chat path.
4. `MemoryService` retrieves relevant facts, dialogue evidence, and knowledge evidence and builds a query contract.
5. `ContextExecutor` validates and dispatches optional read-only requests for `telegram`, `web`, `home_assistant`, and `knowledge`.
6. Ollama generates the final answer.
7. The turn is persisted, then post-processing extracts stable facts and updates dialogue metadata.
8. `DeliveryService` sends callback responses through Home Assistant; Ollama-compatible routes return inline.

Important endpoints:

- `/chat`
- `/connector`
- `/api/chat`
- `/api/generate`
- `/api/tags`
- `/api/show`
- `/knowledge/upload`
- `/memory`

## Database

- `conversation_turns`: dialogue pairs plus retrieval metadata and embeddings.
- `memory_facts`: stable user facts and preferences.
- `memory_embeddings`: indexed knowledge chunks.
- `knowledge_upload_sessions`: upload state.
- `schema_version`: schema marker.

## Current truth

The current runtime does not use planner, skills, tasks, reminders, policy execution, scheduler loops, or smart-home control.

Historical documents may still mention those subsystems. They are not the source of truth for the current version unless a file explicitly says it is historical or target-state design only.

## Risks and TBD

- The active process on port `8010` may still be an older binary until a controlled restart is completed.
- Full live verification against Ollama and Home Assistant remains TBD.
- `ContextExecutor` still has pending handlers for some source/operation pairs.
- If reminders are rebuilt later, they should be introduced as an isolated optional integration, not by reviving the removed planner/skills/tasks stack.
