# Lumen Project Map

## Overview

Lumen is a local FastAPI assistant with shared conversation history/context, isolated reminders, calendar actions and nutrition logging, plus a request-driven Feedreader news API.

## Main directories

- `src/lumen/main.py`: HTTP endpoints and service wiring.
- `src/lumen/services/`: chat orchestration, memory, context execution, Telegram/web context, knowledge, STT, and delivery.
- `src/lumen/services/conversation.py`: shared user-facing persistence, callback delivery, event idempotency and post-processing.
- `src/lumen/memory/`: SQLite reads/writes and retrieval helpers.
- `src/lumen/services/memory_admin.py` and `src/lumen/memory_admin_ui.py`: local memory dashboard, revision-checked fact editing and exact answer-context previews; see `docs/MEMORY_ADMIN.md`.
- `src/lumen/storage/`: current SQLite schema.
- `src/lumen/connectors/`: Ollama client.
- `tests/`: focused chat/runtime/API regression tests.
- `data/`: local database, logs, uploaded knowledge files, and backups.

## Active modules and flows

1. `POST /chat` starts `ChatOnlyService`.
2. `POST /connector` accepts text, STT attachments and Telegram food photos. `NutritionService` handles photo estimates/corrections before chat, while `NutritionStore` atomically writes authoritative per-user JSON and exports its four-column CSV. The separate HA nutrition sent-correlation automation was removed on user request (2026-10-18); new bot-confirmation message bindings are no longer forwarded. See `docs/NUTRITION.md` and its helper package.
3. Audio/video attachments are normalized with `ffmpeg`, transcribed by `faster-whisper`, then routed into the same text chat path.
4. Before routing, `ChatOnlyService` loads bounded chronological turns for the user/chat. `MemoryService` uses the same recent context to build the query contract and retrieves dialogue/knowledge evidence. Its `FactContextService` reads mandatory personal rules separately and combines semantic/lexical retrieval for contextual facts. Shared `memory/fact_policy.py` defines the reserved marker and canonical content hash.
5. `ContextExecutor` validates optional read-only requests. Telegram and web
   handlers are live; generic Home Assistant state and contract-requested
   knowledge handlers still return `pending_implementation`.
6. `GetContract` класифікує `create`, `list`, `update`, `pause`, `resume`, `delete`, explicit `snooze`, `select` і `confirm_bulk`; `ReminderService` зберігає root schedule та optional one-shot snooze child у SQLite, сам робить user-/route-scoped selection і використовує `DeliveryService` та один nearest Windows wake task. Множинний selection і bulk delete мають persisted short-lived confirmation sessions.
7. Під час створення reminder з connector ingress зберігається лише allowlisted Telegram return route; scheduled delivery повторно використовує його, а не `user_id`.
8. `HomeAssistantCalendarClient` читає поточний день через HA Calendar REST, слухає `calendar/event/subscribe` через HA WebSocket і проєктує calendar occurrences в одноразові SQLite reminders; all-day notification time задає `HOME_ASSISTANT_CALENDAR_ALL_DAY_TIME` (default 09:00 `Europe/Kyiv`).
9. Для явної команди створення `GetContract` формує typed `calendar.create`; `CalendarActionService` один раз викликає HA `calendar.create_event` лише для configured write entity. Конфлікти не перевіряються, відсутній timed end отримує configured 60 хвилин, а успіх одразу запускає authoritative current-day refresh.
10. For ordinary chat, Ollama generates the final answer; handled reminder/calendar actions return deterministic user-facing responses through the same `ConversationService` persistence/delivery path.
11. `ConversationService` stores user-facing turns, delivers through Home Assistant when requested, and runs post-processing. Due reminders are assistant-only history events with stable `delivery_id`; failures to persist history retry without resending.
12. Knowledge upload records a short user-facing success/error result without file contents. Nutrition summaries read authoritative JSON through `ContextExecutor`; user-facing nutrition interactions share conversation history.
13. Stored-turn post-processing extracts stable facts/inclusion policy, updates dialogue metadata, and indexes missing or changed contextual facts. Ollama-compatible routes return inline after processing without callback. `scripts/prepare_fact_memory.py` provides a backed-up operator manifest application and initial index backfill.
14. `POST /news/process` opens one Feedreader article through the shared bounded
    `WebPageExtractor`, applies editorial junk filtering and user-only news
    topic `dislikes`, then returns `keep|skip|retry` with a narration-ready Todo
    item for `keep`. This request-driven Feedreader API stays outside ordinary
    conversation context; internal classification and network events are not
    stored as utterances.

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

- `conversation_turns`: dialogue pairs, retrieval metadata/embeddings, and
  idempotent user-facing events such as due-reminder notifications; no new
  table is introduced.
- `memory_facts`: stable user facts and preferences, including permanent
  user-only `news_context/topic:*` `dislikes`.
- `memory_embeddings`: indexed knowledge chunks and separately scoped `memory_fact` vectors; schema unchanged.
- `knowledge_upload_sessions`: upload state.
- `reminders`: isolated reminder state, including `delivery_route_json`, nullable `parent_reminder_id`, nullable calendar idempotency key `source_ref` і `next_attempt_at` для transport retry; schema `v34`.
- `reminder_management_sessions`: короткоживучі user-/route-scoped selection та bulk-confirmation tokens із expected row revision.
- `schema_version`: schema marker.

## Current truth

The current runtime does not use planner, skills, tasks, policy execution, scheduler loops, or smart-home control. Reminders, calendar actions/projection, and nutrition storage remain isolated integrations; user-facing outcomes use shared conversation history. News processing stays a request-driven Feedreader API rather than a dialogue/background pipeline.

Telegram food-photo estimates and a CSV journal are wired through `NutritionService`;
ordinary chat and Ollama-compatible ingress remain text/STT. Nutrition uses a
vision-capable configured Ollama model, separate JSON state/CSV, and no SQLite
schema changes. Knowledge image OCR/vision remains unsupported. Historical
`nutrition_*` labels are unrelated to the new isolated service.

Unused planner/task models, store APIs and JSON seeds were removed on
2026-10-07. Schema/migrations and bundled timezone data remain intact.
Knowledge replacement is atomic per user/type/ref after batch validation;
fact retrieval uses ID maps and cached lexical scores within each request.
API background tasks are tracked and cancelled/awaited during shutdown.

Historical documents may still mention those subsystems. They are not the source of truth for the current version unless a file explicitly says it is historical or target-state design only.

## Risks and TBD

- The active process on port `8010` may still be an older binary until a controlled restart is completed.
- Full live verification against Ollama and Home Assistant remains TBD.
- `ContextExecutor` still has pending handlers for some source/operation pairs.
- Calendar projection requires configured `HOME_ASSISTANT_CALENDAR_ENTITY_IDS` and canonical `HOME_ASSISTANT_CALENDAR_TARGET_CHAT_ID`; without both, it stays disabled. Projected rows є read-only у reminder lifecycle. A rebuilt and restarted runtime applies schema `v34`.
- Windows Task Scheduler може wake/start на батареї, але не може доставити reminder за повністю вимкненого ПК. Після запуску rows старші за configurable 30-minute grace не озвучуються пізно.
- Public URL checks do not pin the resolved TCP address; DNS rebinding remains a
  residual risk. News processing intentionally permits only one in-flight job.
