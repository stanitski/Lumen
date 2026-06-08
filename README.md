# LUMEN

Lumen is a standalone local AI assistant core that sits between Home Assistant and Ollama.

## Goals

- Keep long-term memory under our control.
- Store user-provided knowledge separately from chat memory.
- Route requests directly to Ollama.
- Expose a clean API for Home Assistant automations.

## Planned Architecture

- `src/lumen/api`: FastAPI entrypoints.
- `src/lumen/connectors`: Home Assistant and Ollama adapters.
- `src/lumen/memory`: fact extraction and long-term memory retrieval.
- `src/lumen/knowledge`: document indexing and retrieval.
- `src/lumen/storage`: SQLite-backed persistence.
- `data/`: local runtime data, excluded from Git except placeholders.

## Current v1 Capabilities

- `POST /chat/ask`: orchestrates memory retrieval, knowledge retrieval, model answering, and optional action proposal generation.
- `POST /chat/confirm-action`: executes only previously proposed allowlisted Home Assistant actions.
- `POST /assist/process`: Home Assistant-friendly conversation entrypoint with speech + confirmation payloads.
- `POST /assist/confirm`: confirmation endpoint for Assist-driven pending actions.
- `GET /health/system`: reports database, Home Assistant, Ollama, and config readiness.
- `POST /memory/search`: searches structured long-term memory facts.
- `POST /knowledge/search`: searches curated local knowledge chunks.
- `POST /admin/reindex`: ingests configured knowledge paths into the local SQLite index.
- `POST /admin/bootstrap-home-assistant`: exports a lightweight Home Assistant entity snapshot into the knowledge layer.
- `GET /home-assistant/entities` and `GET /home-assistant/snapshot`: read-only Home Assistant introspection endpoints.

## Persistence Model

- `conversation_logs`: full user/assistant dialog history.
- `memory_facts`: structured durable facts with confidence and importance.
- `action_traces`: pending, cancelled, blocked, failed, and executed HA proposals.
- `knowledge_documents` and `knowledge_chunks`: curated indexed local knowledge.
- `ingestion_runs`: reindex history and status.

## Important Notes

- Practical integration notes now live in `docs/home-assistant-integration.md`.

## First Run

1. Create a virtual environment.
2. Install dependencies with `pip install -e .`.
3. Copy `.env.example` to `.env` and fill in tokens/URLs.
4. If you want a more realistic local template, start from `docs/env.local.example`.
5. Start the API with `uvicorn lumen.main:app --reload --app-dir src --host 127.0.0.1 --port 8010`.

## Knowledge Paths

- Configure one or more curated knowledge roots with `KNOWLEDGE_PATHS`.
- Separate multiple paths with `;`.
- Reindex with `POST /admin/reindex` after adding or updating local notes or Home Assistant config exports.
- A starter file lives at `data/knowledge/starter-house-rules.md` so the first reindex has at least one useful local source.

## Suggested First Live Flow

1. Fill `.env` with `HOME_ASSISTANT_TOKEN`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and `KNOWLEDGE_PATHS`.
2. Start the API.
3. Call `POST /admin/reindex` to index local notes and Home Assistant exports.
4. If Home Assistant API access is configured, call `POST /admin/bootstrap-home-assistant` once to capture scripts, scenes, and helper entities into the knowledge base.
5. Test `GET /home-assistant/snapshot`.
6. Test `POST /assist/process` or `POST /chat/ask` with a read-only question, then an action request that should require confirmation.
7. Optional: run `scripts/smoke-test.ps1` for a quick end-to-end API check.

## Cold Start Note

- On the first request after `Ollama` starts, model load time can exceed Home Assistant's default `rest_command` timeout of 10 seconds.
- Use `timeout: 120` on the Home Assistant `rest_command` that calls `LUMEN`.
- `LUMEN` also supports `OLLAMA_TIMEOUT_SECONDS` and `OLLAMA_KEEP_ALIVE` in `.env` to make cold starts less brittle.
