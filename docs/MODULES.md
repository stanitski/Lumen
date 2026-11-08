# Ключові модулі

Статус: звірено з локальним source 2026-10-18; без перевірки deployed executable.

| Модуль | Призначення і ключові символи | Хто викликає / що викликає | Стан |
|---|---|---|---|
| `src/lumen/main.py` | FastAPI wiring, endpoints, startup/shutdown, `_project_calendar_events()` | зовнішній HTTP; створює всі сервіси | активний; містить module-level initialization |
| `services/chat_only.py` | `ChatOnlyService`, prompt assembly, routing reminder/calendar, persistence/delivery | endpoints → service → memory/context/Ollama/reminder/calendar/delivery | центральний активний orchestrator |
| `services/conversation.py` | `ConversationService.finish()` спільно зберігає user-facing turn, доставляє callback і виконує post-processing; `record_event()` додає assistant-only події з idempotency key | chat, nutrition, reminder → MemoryService/DeliveryService | спільний owner persistence+delivery; transport metadata не є окремими utterances |
| `services/memory.py` | `MemoryService`, `GetContract`, retrieval policy, facts, recent chronological context, dialogue/knowledge retrieval, news topic dislikes | chat/knowledge/news → store/Ollama | активний, великий і частково має legacy naming (`build_planner_context`) |
| `memory/store.py` | SQLite queries, atomic document-index replacement та in-process cosine search | memory/knowledge/Telegram services | активний persistence layer; legacy execution API видалено |
| `services/fact_context.py` | mandatory/contextual fact retrieval, freshness і indexing | `MemoryService` → `MemoryStore`/Ollama; operator backfill | окремий активний fact-context service |
| `services/memory_admin.py`, `memory_admin_ui.py` | локальний dashboard, editable facts, optimistic revisions, frozen answer context | `/memory`, `/memory/admin/*` → store/fact context/chat | loopback-only; `MEMORY_ADMIN.md` |
| `memory/fact_policy.py` | reserved marker, canonical hash і prompt eligibility | store/fact context/extraction | спільний контракт без schema change |
| `storage/db.py` | `Database`, schema `v34`, reminder migrations і `reminder_management_sessions` | `main.py`, wake entrypoint, tests | активний; schema source of truth |
| `connectors/ollama.py` | `OllamaConnector.chat()`, `.embed()`, `.healthcheck()` | memory/chat/knowledge | єдиний live LLM provider |
| `services/context_executor.py` | `ContextExecutor.execute()` і prompt formatting | `ChatOnlyService` → Telegram/web adapters | Telegram/web активні; HA/knowledge handlers pending |
| `services/telegram_context.py` | allowlisted bounded MTProto reads | `ContextExecutor` → Telethon/STT | активний read-only adapter; не ingress |
| `services/web_search.py` | `WebPageExtractor`, `DuckDuckGoWebSearch.search()` | news/context executor → public web | активний, bounded і network-dependent |
| `services/news_processing.py` | `NewsProcessingService`, editorial filter, user dislikes, 5W narration | request-driven `/news/process` → extractor/memory/Ollama | active isolated Feedreader API; classifier/background work is outside conversation history |
| `services/knowledge.py` | upload, extraction, chunking, embeddings | `/knowledge/upload` → extractor/Ollama/store | активний ingestion path |
| `services/file_text.py` | text/PDF/file extraction | `KnowledgeService` | активний helper |
| `services/speech_to_text.py` | attachment classification, download, ffmpeg, Whisper | `/connector`, Telegram media context | активний; external binaries/models required |
| `services/nutrition.py` | bounded photo download, structured vision estimate, text correction, sent-message correlation | `/connector` → Ollama/store/delivery | source wired; deployment pending |
| `storage/nutrition.py` | authoritative per-user JSON, atomic CSV projection, receipts/message links | `NutritionService` | separate persistence, no SQLite migration |
| `services/delivery.py` | Home Assistant callback payload і chat/reminder delivery | chat/reminders/knowledge/nutrition | активний transport owner |
| `services/reminders.py` | `ReminderService`, typed lifecycle/selection state machine, recurrence, explicit snooze, confirmation | chat/startup/wake/calendar projection | активна ізольована side-effect інтеграція; canonical `due_at` не змінюється transport retry |
| `services/reminder_wake.py` | `WindowsWakeAdapter` і nearest scheduled task | `ReminderService` | Windows-only active adapter; дозволяє wake/start на батареї |
| `reminder_wakeup.py` | одноразовий wake entrypoint із shared `ConversationService` для delivered reminder history | Task Scheduler → reminder service → shared memory | активний; індексація event може бути скасована при виході та відновлена звичайним runtime recovery |
| `services/calendar_actions.py` | validate + one-shot HA calendar create | `ChatOnlyService` → HA REST | активний, non-retrying write |
| `services/home_assistant_calendar.py` | current-day REST snapshot + WebSocket invalidation | startup → HA → calendar projection | активний, conditional on config |
| `timezones.py` | bundled/system `Europe/Kyiv` loading | reminder/calendar/time helpers | активний, PyInstaller-safe |
| `models.py` | Pydantic API/storage models | main/services/store | активні chat/memory/news/knowledge/delivery contracts |

## Завершений legacy cleanup

2026-10-07 після перевірки imports/callers у source, tests та scripts видалено:

- `SkillRecord`, `IntentRecord`, `SkillPlanItem`, `ExecutionPlan`,
  `PolicyDecision`, `TaskRecord`, `TaskRunRecord` та залежні legacy models;
- `MemoryStore` methods для intents, skills, tasks, HA entity catalog і routines
  та вимкнені legacy reminder methods;
- невикористані `src/lumen/data/intents.json` і `system_skills.json`.

Schema `v34`, історичні DB migrations та активний `data/zoneinfo/Europe/Kyiv`
збережено. `memory.py` ще має compatibility names/retention labels; сама назва
не означає наявність execution subsystem.

## Дублювання та межі

- Knowledge retrieval має два шляхи: активний automatic retrieval у
  `MemoryService` і pending contract handler у `ContextExecutor`. Це різні
  entry contracts, але межу треба зробити явнішою.
- `build_planner_context` — застаріла назва активної chat retrieval функції;
  planner вона не викликає.
- Root `DECISIONS.md`/`CHANGELOG_LUMEN.md` є хронологічним ledger, а
  `docs/DECISIONS.md` — curated current decisions; це навмисне, не дублювання
  source of truth.
