# Lumen

Lumen is a local FastAPI assistant built around one active runtime path:

`Ingress -> bounded chronological context -> ChatOnlyService/MemoryService routing -> optional ContextExecutor reads -> Ollama or deterministic action -> ConversationService persistence/delivery`

Current Lumen is a chat-memory-knowledge core. It supports:

- text chat through `/chat`, `/connector`, `/api/chat`, and `/api/generate`;
- audio and video transcription for connector attachments through `ffmpeg` and `faster-whisper`;
- SQLite-backed dialogue memory and profile facts;
- knowledge retrieval as read-only prompt context;
- bounded external context through `ContextExecutor`;
- callback delivery through Home Assistant.
- isolated reminder lifecycle and calendar read/create through Home Assistant;
- synchronous Feedreader article processing through `/news/process`.
- Telegram food-photo estimates and a per-user CSV journal with corrections.

Current Lumen does not run planner, skills, tasks, or smart-home control. Reminders have their own SQLite state, one Windows wake task, and Home Assistant delivery confirmation. Calendar and news use separate services.

## Швидкий запуск на Windows

Для власної ізольованої копії потрібні Python 3.11+, Ollama та новий локальний
`.env`; не копіюйте `.env`, `data/`, Telegram session або Home Assistant token
з інсталяції автора.

1. Створіть virtual environment та встановіть залежності:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\python -m pip install --upgrade pip
   .\.venv\Scripts\python -m pip install -e . python-multipart
   ```

2. Скопіюйте `.env.example` у `.env`, задайте власний `LUMEN_USER_ID` і
   залиште `LUMEN_HOST=127.0.0.1`. API не має окремої автентифікації, тому не
   відкривайте його в мережу без окремого reverse proxy та access control.
3. Встановіть [Ollama](https://ollama.com/download/windows), запустіть
   `ollama serve` і завантажте моделі зі свого `.env`, наприклад:

   ```powershell
   ollama pull gemma4:e4b
   ollama pull embeddinggemma
   ```

4. Запустіть `run.bat`, а потім відкрийте `http://127.0.0.1:8010/docs`.
   Базовий inline chat через `/api/generate` не потребує Home Assistant або
   Telegram.

Повна інструкція: [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md). Вона пояснює
Home Assistant, Telegram, STT, харчування, локальні дані й резервні копії.

`telethon` є опційною бібліотекою Python для Telegram MTProto context lookup.
Вона не потрібна для базового чату з Ollama, але входить у залежності повного
проєкту й потрібна для Telegram-функцій та повного тестового набору.

## Architecture

- `src/lumen/main.py` wires FastAPI endpoints and service instances.
- `src/lumen/services/chat_only.py` owns chat orchestration, prompt assembly, persistence, and delivery calls.
- `src/lumen/services/conversation.py` owns shared user-facing persistence, callback delivery, idempotent event recording, and turn post-processing.
- `src/lumen/services/memory.py` builds retrieval context, stores turns and facts, and post-processes dialogue.
- `src/lumen/services/context_executor.py` executes validated read-only context requests.
- `src/lumen/services/telegram_context.py` reads bounded Telegram context from approved memory facts only.
- `src/lumen/services/web_search.py` performs bounded DuckDuckGo retrieval for temporary prompt context.
- `src/lumen/services/knowledge.py` handles knowledge upload and retrieval indexing.
- `src/lumen/services/speech_to_text.py` transcribes connector audio and video attachments.
- `src/lumen/services/nutrition.py` handles food photos/corrections; `src/lumen/storage/nutrition.py` owns JSON state and its CSV export.
- `src/lumen/services/delivery.py` posts final answers back through the Home Assistant callback contract.
- `GetContract` owns natural-language reminder interpretation; `src/lumen/services/reminders.py` validates typed contracts and manages SQLite state transitions, delivery retries, confirmation handling, and assistant-only delivered-notification history events.
- `src/lumen/services/reminder_wake.py` maintains the single nearest Windows wake task.
- `src/lumen/memory/store.py` is the SQLite persistence layer.
- `src/lumen/storage/db.py` defines the current schema and migrations.
- `src/lumen/connectors/ollama.py` is the async Ollama client.

## Active flow

1. `POST /chat` accepts text and starts background processing.
2. `POST /connector` accepts text, STT attachments, and Telegram food photos. Nutrition runs as an isolated flow before ordinary chat.
3. Audio/video attachments are transcribed, then re-enter the normal text chat path.
4. `ChatOnlyService` resolves the effective `user_id`.
5. Bounded chronological context is loaded before routing; `MemoryService.build_planner_context(...)` uses it with relevant facts, dialogue evidence, knowledge evidence, and the query contract.
6. `ContextExecutor` optionally fetches temporary Telegram/web/nutrition context; Home Assistant state and contract-requested knowledge handlers remain pending. Reminder/calendar contracts can return deterministic responses before the final dialogue call; structured reminder confirmation can bypass context building.
7. Ollama generates the final answer from the assembled prompt.
8. `ConversationService.finish()` persists user-facing results, sends callback responses through Home Assistant when enabled, and runs post-processing. Ollama-compatible routes return inline after processing without callback delivery.

## Connector and speech behavior

`/connector` currently supports:

- `event_type="text"` with plain text;
- `event_type="attachment"` for speech/video STT or Telegram food-photo estimation;
- `event_type="nutrition_delivery"` for HA-local sent-message correlation.

Unsupported connector events are ignored with an explicit reason. Transcribed speech enters the same text flow, including reminder/calendar contract handling.

Telegram food photos use `NutritionService` and `OllamaConnector.chat_multimodal()`.
The four-column CSV (`дата,час,калорії,опис`) is generated from authoritative
per-user JSON, outside SQLite. User-facing nutrition replies share conversation
history; summaries read the authoritative JSON through `ContextExecutor`.
Captions override the photo; explicit
corrections update the latest meal or the meal selected by reply. See
[nutrition configuration and HA wiring](docs/NUTRITION.md). Source is prepared;
deployment and a real-photo check remain separate. `/api/chat` extracts text
only; knowledge image uploads still have no OCR/vision extraction.

## External context contract

Lumen can request bounded read-only context before the final answer. The current contract is:

- `telegram/search`
- `web/search`
- `home_assistant/read_state`
- `knowledge/search`
- `nutrition/summary` (reads the authoritative per-user JSON journal)

Implementation status:

- `telegram/search` and `web/search` have live adapters;
- `home_assistant/read_state` and contract-requested `knowledge/search` return
  `pending_implementation`;
- nutrition summaries are served from the authoritative JSON through
  `ContextExecutor`;
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
- `reminders`
- `reminder_management_sessions`
- `schema_version`

Current runtime behavior:

- turns are stored for non-`global` users;
- metadata flags `debug`, `debug_request`, `lumen_debug`, `skip_memory`, or `disable_memory` suppress turn persistence and its post-processing, while context retrieval remains active; see the [runtime contract](docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md);
- profile facts are extracted after persistence;
- dialogue retrieval uses bounded semantic and lexical evidence;
- knowledge is retrieval context, not a second chat engine;
- debug memory search reuses the same live prompt-building path without persisting a turn.

Memory UI and API defaults use `LUMEN_USER_ID`; deleting a turn respects the
requested user scope. Knowledge document replacement validates the full model
response before replacing its embeddings in one SQLite transaction, preserving
the old index on failure; upload history contains only a short success/error
result, never file contents. Due reminders are recorded as assistant-only events
after successful delivery, using stable event IDs so history retries do not
resend notifications. `/news/process` remains a request-driven Feedreader API;
internal classification and transport events are not dialogue turns, and only
truly user-facing results belong in history. Background chat/recovery/calendar
tasks are tracked, their failures logged, and unfinished tasks cancelled and
awaited on shutdown.

On Windows, `run.bat` launches from its own directory using the local virtual
environment when available. Host and port come from `Settings`.

## Current non-goals

These are not current runtime features:

- planner-driven skill execution;
- planner-driven reminders or scheduled tasks;
- smart-home device control;
- policy-gated side-effect execution;
- legacy broad Telegram ingestion or message persistence.

If any of these return in the future, they should be rebuilt as isolated, tested integrations on top of the current chat-memory-knowledge core.
