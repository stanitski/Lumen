# Lumen

Lumen is a local FastAPI assistant built around one active runtime path:

`Ingress -> ChatOnlyService -> MemoryService context -> optional ContextExecutor reads -> Ollama dialogue -> SQLite persistence -> Home Assistant callback delivery`

Current Lumen is a chat-memory-knowledge core. It supports:

- text chat through `/chat`, `/connector`, `/api/chat`, and `/api/generate`;
- audio and video transcription for connector attachments through `ffmpeg` and `faster-whisper`;
- SQLite-backed dialogue memory and profile facts;
- knowledge retrieval as read-only prompt context;
- bounded external context through `ContextExecutor`;
- callback delivery through Home Assistant.

Current Lumen does not run planner, skills, tasks, or smart-home control. It has one isolated reminder integration with its own SQLite state, one Windows wake task, and Home Assistant delivery confirmation.

## Architecture

- `src/lumen/main.py` wires FastAPI endpoints and service instances.
- `src/lumen/services/chat_only.py` owns chat orchestration, prompt assembly, persistence, and delivery calls.
- `src/lumen/services/memory.py` builds retrieval context, stores turns and facts, and post-processes dialogue.
- `src/lumen/services/context_executor.py` executes validated read-only context requests.
- `src/lumen/services/telegram_context.py` reads bounded Telegram context from approved memory facts only.
- `src/lumen/services/web_search.py` performs bounded DuckDuckGo retrieval for temporary prompt context.
- `src/lumen/services/knowledge.py` handles knowledge upload and retrieval indexing.
- `src/lumen/services/speech_to_text.py` transcribes connector audio and video attachments.
- `src/lumen/services/delivery.py` posts final answers back through the Home Assistant callback contract.
- `GetContract` owns natural-language reminder interpretation; `src/lumen/services/reminders.py` validates typed contracts and manages SQLite state transitions, delivery retries, and confirmation handling.
- `src/lumen/services/reminder_wake.py` maintains the single nearest Windows wake task.
- `src/lumen/memory/store.py` is the SQLite persistence layer.
- `src/lumen/storage/db.py` defines the current schema and migrations.
- `src/lumen/connectors/ollama.py` is the async Ollama client.

## Active flow

1. `POST /chat` accepts text and starts background processing.
2. `POST /connector` accepts text or STT-supported attachments.
3. Audio/video attachments are transcribed, then re-enter the normal text chat path.
4. `ChatOnlyService` resolves the effective `user_id`.
5. `MemoryService.build_planner_context(...)` retrieves relevant facts, dialogue evidence, knowledge evidence, and a query contract.
6. `ContextExecutor` optionally fetches temporary external context for `telegram/search`, `web/search`, `home_assistant/read_state`, or `knowledge/search`.
7. Ollama generates the final answer from the assembled prompt.
8. The turn is persisted in SQLite.
9. Post-processing extracts stable facts and refreshes dialogue metadata and embeddings.
10. `DeliveryService` sends the callback response through Home Assistant, while Ollama-compatible routes return inline.

## Connector and speech behavior

`/connector` currently supports:

- `event_type="text"` with plain text;
- `event_type="attachment"` only when the attachment is recognized as speech/video for STT.

Unsupported connector events are ignored with an explicit reason. Speech attachments are not routed through planner, task, or reminder flows.

## External context contract

Lumen can request bounded read-only context before the final answer. The current contract is:

- `telegram/search`
- `web/search`
- `home_assistant/read_state`
- `knowledge/search`

Implementation status:

- `telegram/search` and `web/search` have live adapters;
- `home_assistant/read_state` and contract-requested `knowledge/search` return
  `pending_implementation`;
- ordinary knowledge retrieval is active through
  `MemoryService.build_planner_context(...)`, independently of the pending
  `ContextExecutor` handler.

Rules:

- external context is temporary prompt context, not persisted imported content;
- Telegram access is allowed only from explicitly tagged `messenger_context` facts;
- Telegram source facts must carry `ref`, `title`, and `kind=user|group|channel`;
- web results may be cited by URL in the answer;
- unavailable or pending context must be reported truthfully, not invented.

## Delivery contract

Lumen sends outbound callback payloads through Home Assistant in a channel-neutral shape:

```json
{
  "request_id": "chat:...",
  "kind": "chat",
  "user_id": "VST",
  "answer": "...",
  "voice": false,
  "metadata": {
    "target_channel": "telegram",
    "target_chat_id": "492198718"
  }
}
```

Ollama-compatible `/api/chat` and `/api/generate` return inline responses instead of callback delivery.

## Memory model

Memory is scoped by canonical Lumen `user_id`.

Main persistence tables:

- `conversation_turns`
- `memory_facts`
- `memory_embeddings`
- `knowledge_upload_sessions`
- `schema_version`

Current runtime behavior:

- turns are stored for non-`global` users;
- profile facts are extracted after persistence;
- dialogue retrieval uses bounded semantic and lexical evidence;
- knowledge is retrieval context, not a second chat engine;
- debug memory search reuses the same live prompt-building path without persisting a turn.

## Current non-goals

These are not current runtime features:

- planner-driven skill execution;
- planner-driven reminders or scheduled tasks;
- smart-home device control;
- policy-gated side-effect execution;
- legacy broad Telegram ingestion or message persistence.

If any of these return in the future, they should be rebuilt as isolated, tested integrations on top of the current chat-memory-knowledge core.
