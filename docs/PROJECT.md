# Проєкт Lumen

## Статус

Цей документ описує live-код гілки `codex/ollama-stream-watchdog` станом на
2026-10-07. Це звірка локального source, не deployed executable. Джерело істини: `src/lumen/main.py` та імпортовані з нього модулі.

## Мета

Lumen — локальний FastAPI-асистент із єдиним chat-memory-knowledge ядром. Він
приймає текст або транскрибоване мовлення, добирає контекст, звертається до
Ollama, зберігає діалог і доставляє відповідь через Home Assistant.

Поверх ядра зараз є дві вузькі side-effect інтеграції:

- `ReminderService` — повний lifecycle reminders у SQLite з Windows wake task,
  доставкою, confirmation, pause/resume, soft delete та explicit snooze;
- `CalendarActionService` та `HomeAssistantCalendarClient` — створення подій
  через Home Assistant і проєкція подій поточного дня у reminders.

Це підтверджено wiring у `src/lumen/main.py`, orchestration у
`ChatOnlyService.process_chat_request_result()` та schema `v34` у
`src/lumen/storage/db.py`.

## Основні можливості

- ingress: `POST /chat`, `POST /connector`, `POST /api/chat`,
  `POST /api/generate`;
- STT connector attachments через `SpeechToTextService`;
- facts, dialogue retrieval і post-processing через `MemoryService`;
- knowledge upload та semantic retrieval через `KnowledgeService` і
  `MemoryService.build_planner_context()`;
- read-only Telegram і web context через `ContextExecutor`;
- reminder create/list/update/pause/resume/delete/snooze/confirmation через typed `GetContract`;
- calendar create через один configured Home Assistant entity;
- поточноденна calendar projection через HA REST + WebSocket invalidation;
- callback delivery через `DeliveryService`;
- memory inspection через `/memory`, `/memory/items`,
  `/memory/debug-search`, `/memory/delete`.
- synchronous Feedreader article processing через `/news/process` зі shared
  `WebPageExtractor`, exact user news dislikes та narration-ready 5W Todo output.

## Межі відповідальності

Lumen не є загальним automation engine. У live wiring немає planner, skill
registry, task executor, policy engine або smart-home control. Наявні
`SkillRecord`, `ExecutionPlan`, `TaskRecord`, JSON-файли intents/skills і
частина store API є legacy residue, а не активним runtime.

`ContextExecutor` реально виконує `telegram/search` і `web/search`.
`home_assistant/read_state` та contract-requested `knowledge/search` у цьому
executor повертають `pending_implementation`; knowledge retrieval для
звичайного chat працює окремо всередині `MemoryService`.

## Основні сценарії

1. Текстовий Telegram/Home Assistant connector запит → chat або вузька
   reminder/calendar дія → callback.
2. Audio/video attachment → download → `ffmpeg`/`faster-whisper` → той самий
   text flow.
3. Ollama-compatible запит → той самий `ChatOnlyService` → inline response.
4. Knowledge upload → extraction/chunking/embedding → SQLite retrieval context.
5. Reminder due/wake → `ReminderService.run_due()` → Home Assistant callback →
   корельоване confirmation/snooze.
6. HA calendar snapshot/update → idempotent one-time reminder projection.
7. Feedreader item → public article або RSS fallback → editorial/user filter →
   `keep|skip|retry` і stable Todo item для озвучення.

## Поточний стан

Підтверджені unit-тести охоплюють chat, query contract, reminders, calendar,
Ollama-compatible API, Telegram policy, STT classification і timezone bundle.
Live перевірки Ollama, Home Assistant, Telegram/STT та deployed executable
залишаються у `docs/HANDOVER.md`.

Подальші технічні деталі:

- [Архітектура](ARCHITECTURE.md)
- [Модулі](MODULES.md)
- [Передача стану](HANDOVER.md)
- [Діагностика](DEBUGGING.md)
