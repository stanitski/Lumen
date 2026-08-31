# Lumen Project Map

## Overview

Lumen is a local FastAPI assistant with a chat-memory-knowledge core plus isolated reminders, calendar actions, and synchronous Feedreader news processing.

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
5. `ContextExecutor` validates optional read-only requests. Telegram and web
   handlers are live; generic Home Assistant state and contract-requested
   knowledge handlers still return `pending_implementation`.
6. `GetContract` класифікує `create`, `list`, `update`, `pause`, `resume`, `delete`, explicit `snooze`, `select` і `confirm_bulk`; `ReminderService` зберігає root schedule та optional one-shot snooze child у SQLite, сам робить user-/route-scoped selection і використовує `DeliveryService` та один nearest Windows wake task. Множинний selection і bulk delete мають persisted short-lived confirmation sessions.
7. Під час створення reminder з connector ingress зберігається лише allowlisted Telegram return route; scheduled delivery повторно використовує його, а не `user_id`.
8. `HomeAssistantCalendarClient` читає поточний день через HA Calendar REST, слухає `calendar/event/subscribe` через HA WebSocket і проєктує Google Calendar occurrences в одноразові SQLite reminders; all-day події отримують `due_at` о 09:00 `Europe/Kyiv`.
9. Для явної команди створення `GetContract` формує typed `calendar.create`; `CalendarActionService` один раз викликає HA `calendar.create_event` лише для configured write entity. Конфлікти не перевіряються, відсутній timed end отримує configured 60 хвилин, а успіх одразу запускає authoritative current-day refresh.
10. Ollama generates the final answer.
11. The turn is persisted, then post-processing extracts stable facts and updates dialogue metadata.
12. `DeliveryService` sends callback responses through Home Assistant; Ollama-compatible routes return inline.
13. `POST /news/process` opens one Feedreader article through the shared bounded
    `WebPageExtractor`, applies editorial junk filtering and user-only news
    topic `dislikes`, then returns `keep|skip|retry` with a narration-ready Todo
    item for `keep`.

Important endpoints:

- `/chat`
- `/connector`
- `/api/chat`
- `/api/generate`
- `/api/tags`
- `/api/show`
- `/knowledge/upload`
- `/memory`
- `/news/process`

## Database

- `conversation_turns`: dialogue pairs plus retrieval metadata and embeddings.
- `memory_facts`: stable user facts and preferences, including permanent
  user-only `news_context/topic:*` `dislikes`.
- `memory_embeddings`: indexed knowledge chunks.
- `knowledge_upload_sessions`: upload state.
- `reminders`: isolated reminder state, including `delivery_route_json`, nullable `parent_reminder_id`, nullable calendar idempotency key `source_ref` і `next_attempt_at` для transport retry; schema `v34`.
- `reminder_management_sessions`: короткоживучі user-/route-scoped selection та bulk-confirmation tokens із expected row revision.
- `schema_version`: schema marker.

## Current truth

The current runtime does not use planner, skills, tasks, policy execution, scheduler loops, or smart-home control. Reminders are the one isolated optional integration; they do not restore those subsystems.

Historical documents may still mention those subsystems. They are not the source of truth for the current version unless a file explicitly says it is historical or target-state design only.

## Risks and TBD

- The active process on port `8010` may still be an older binary until a controlled restart is completed.
- Full live verification against Ollama and Home Assistant remains TBD.
- `ContextExecutor` still has pending handlers for some source/operation pairs.
- Calendar projection requires configured `HOME_ASSISTANT_CALENDAR_ENTITY_IDS` and canonical `HOME_ASSISTANT_CALENDAR_TARGET_CHAT_ID`; without both, it stays disabled. Projected rows є read-only у reminder lifecycle. A rebuilt and restarted runtime applies schema `v34`.
- Windows Task Scheduler може wake/start на батареї, але не може доставити reminder за повністю вимкненого ПК. Після запуску rows старші за configurable 30-minute grace не озвучуються пізно.
- Public URL checks do not pin the resolved TCP address; DNS rebinding remains a
  residual risk. News processing intentionally permits only one in-flight job.
