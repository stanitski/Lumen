# Ключові модулі

Статус: звірено з локальним кодом гілки `codex/ollama-stream-watchdog`, 2026-10-07; без перевірки deployed executable.

| Модуль | Призначення і ключові символи | Хто викликає / що викликає | Стан |
|---|---|---|---|
| `src/lumen/main.py` | FastAPI wiring, endpoints, startup/shutdown, `_project_calendar_events()` | зовнішній HTTP; створює всі сервіси | активний; містить module-level initialization |
| `services/chat_only.py` | `ChatOnlyService`, prompt assembly, routing reminder/calendar, persistence/delivery | endpoints → service → memory/context/Ollama/reminder/calendar/delivery | центральний активний orchestrator |
| `services/memory.py` | `MemoryService`, `GetContract`, retrieval policy, facts, dialogue/knowledge retrieval, news topic dislikes | chat/knowledge/news → store/Ollama | активний, великий і частково має legacy naming (`build_planner_context`) |
| `memory/store.py` | SQLite queries та in-process cosine search | memory/knowledge/Telegram services | активна частина змішана з legacy intents/skills/tasks API |
| `services/fact_context.py` | mandatory/contextual fact retrieval, freshness і indexing | `MemoryService` → `MemoryStore`/Ollama; operator backfill | окремий активний fact-context service |
| `memory/fact_policy.py` | reserved marker, canonical hash і prompt eligibility | store/fact context/extraction | спільний контракт без schema change |
| `storage/db.py` | `Database`, schema `v34`, reminder migrations і `reminder_management_sessions` | `main.py`, wake entrypoint, tests | активний; schema source of truth |
| `connectors/ollama.py` | `OllamaConnector.chat()`, `.embed()`, `.healthcheck()` | memory/chat/knowledge | єдиний live LLM provider |
| `services/context_executor.py` | `ContextExecutor.execute()` і prompt formatting | `ChatOnlyService` → Telegram/web adapters | Telegram/web активні; HA/knowledge handlers pending |
| `services/telegram_context.py` | allowlisted bounded MTProto reads | `ContextExecutor` → Telethon/STT | активний read-only adapter; не ingress |
| `services/web_search.py` | `WebPageExtractor`, `DuckDuckGoWebSearch.search()` | news/context executor → public web | активний, bounded і network-dependent |
| `services/news_processing.py` | `NewsProcessingService`, editorial filter, user dislikes, 5W narration | `/news/process` → extractor/memory/Ollama | активна isolated synchronous integration |
| `services/knowledge.py` | upload, extraction, chunking, embeddings | `/knowledge/upload` → extractor/Ollama/store | активний ingestion path |
| `services/file_text.py` | text/PDF/file extraction | `KnowledgeService` | активний helper |
| `services/speech_to_text.py` | attachment classification, download, ffmpeg, Whisper | `/connector`, Telegram media context | активний; external binaries/models required |
| `services/delivery.py` | Home Assistant callback payload і chat/reminder delivery | chat/reminders/knowledge | активний transport owner |
| `services/reminders.py` | `ReminderService`, typed lifecycle/selection state machine, recurrence, explicit snooze, confirmation | chat/startup/wake/calendar projection | активна ізольована side-effect інтеграція; canonical `due_at` не змінюється transport retry |
| `services/reminder_wake.py` | `WindowsWakeAdapter` і nearest scheduled task | `ReminderService` | Windows-only active adapter; дозволяє wake/start на батареї |
| `reminder_wakeup.py` | одноразовий wake entrypoint | Task Scheduler → reminder service | активний operational entrypoint |
| `services/calendar_actions.py` | validate + one-shot HA calendar create | `ChatOnlyService` → HA REST | активний, non-retrying write |
| `services/home_assistant_calendar.py` | current-day REST snapshot + WebSocket invalidation | startup → HA → calendar projection | активний, conditional on config |
| `timezones.py` | bundled/system `Europe/Kyiv` loading | reminder/calendar/time helpers | активний, PyInstaller-safe |
| `models.py` | Pydantic API/storage models | main/services/store | активні chat/memory models змішані з legacy planner/task models |

## Підозріло невикористаний код

Підтверджено пошуком imports/callers:

- `SkillRecord`, `IntentRecord`, `SkillPlanItem`, `ExecutionPlan`,
  `PolicyDecision`, `TaskRecord`, `TaskRunRecord` у `models.py` не входять у
  live wiring;
- `MemoryStore` methods для intents, skills, tasks, HA entity catalog і routines
  не викликаються live modules;
- `src/lumen/data/intents.json` і `system_skills.json` не імпортуються;
- schema `v34` не створює таблиць intents/skills/tasks/HA entities, тому
  відповідні store methods не можуть бути поточним контрактом.

Це обґрунтований висновок про legacy residue, а не дозвіл на автоматичне
видалення. Окремий cleanup має перевірити сумісність scripts/data та міграційні
потреби.

## Дублювання та межі

- Knowledge retrieval має два шляхи: активний automatic retrieval у
  `MemoryService` і pending contract handler у `ContextExecutor`. Це різні
  entry contracts, але межу треба зробити явнішою.
- `build_planner_context` — застаріла назва активної chat retrieval функції;
  planner вона не викликає.
- Root `DECISIONS.md`/`CHANGELOG_LUMEN.md` є хронологічним ledger, а
  `docs/DECISIONS.md` — curated current decisions; це навмисне, не дублювання
  source of truth.
