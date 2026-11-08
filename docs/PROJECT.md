# Проєкт Lumen

## Статус

Цей документ описує локальний source станом на 2026-10-18; це не підтверджує
стан deployed executable. Джерело істини: `src/lumen/main.py` та імпортовані з нього модулі.

## Мета

Lumen — локальний FastAPI-асистент зі спільною chronological conversation
history/context. Він приймає текст або транскрибоване мовлення, завантажує
recent context перед routing і відповіддю, звертається до Ollama, зберігає
user-facing result та за потреби доставляє його через Home Assistant.

Поверх ядра зараз є три вузькі side-effect інтеграції:

- `ReminderService` — повний lifecycle reminders у SQLite з Windows wake task,
  доставкою, confirmation, pause/resume, soft delete та explicit snooze;
- `CalendarActionService` та `HomeAssistantCalendarClient` — створення подій
  через Home Assistant і проєкція подій поточного дня у reminders.
- `NutritionService` — Telegram food-photo estimates і виправлення per-user JSON/CSV журналу.

Це підтверджено wiring у `src/lumen/main.py`; reminders/calendar використовують
`ChatOnlyService.process_chat_request_result()` та schema `v34`, nutrition —
окремий file store без SQLite migration.

## Основні можливості

- ingress: `POST /chat`, `POST /connector`, `POST /api/chat`,
  `POST /api/generate`;
- STT connector attachments через `SpeechToTextService`;
- Telegram food photos та виправлення через `NutritionService`; per-user JSON/CSV журнал без зміни SQLite schema, див. `NUTRITION.md`;
- shared persistence/delivery lifecycle через `ConversationService`; facts,
  dialogue retrieval і post-processing через `MemoryService`;
- knowledge upload result записується в history без file content; upload та
  semantic retrieval реалізовані через `KnowledgeService` і
  `MemoryService.build_planner_context()`;
- read-only Telegram/web context і nutrition summary з authoritative JSON через
  `ContextExecutor`;
- reminder create/list/update/pause/resume/delete/snooze/confirmation через typed `GetContract`;
- calendar create через один configured Home Assistant entity;
- поточноденна calendar projection через HA REST + WebSocket invalidation;
- callback delivery через `DeliveryService`;
- memory inspection через `/memory`, `/memory/items`,
  `/memory/debug-search`, `/memory/delete`.
- request-driven Feedreader article processing через `/news/process` зі shared
  `WebPageExtractor`, exact user news dislikes та narration-ready 5W Todo output;
  internal news work лишається поза conversational history.

## Межі відповідальності

Lumen не є загальним automation engine. У live wiring немає planner, skill
registry, task executor, policy engine або smart-home control. Наявні
Legacy planner/task models, неактивний store API і JSON seeds intents/skills
видалено 2026-10-07 після перевірки callers. Schema та migrations збережено.

`ContextExecutor` реально виконує `telegram/search`, `web/search` і
`nutrition/summary` з authoritative JSON.
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
   assistant-only history event with stable `delivery_id` → correlated
   confirmation/snooze.
6. HA calendar snapshot/update → idempotent one-time reminder projection.
7. Feedreader request → public article або RSS fallback → editorial/user filter →
   `keep|skip|retry` і stable Todo item. Це request-driven API, не background
   processing усього dialogue/news потоку.

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
