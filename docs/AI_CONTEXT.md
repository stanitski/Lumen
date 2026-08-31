# Lumen AI Context

## 1. Паспорт проєкту

Lumen — локальний персональний AI-асистент із FastAPI API, Ollama та
SQLite-backed пам’яттю. Він приймає текстові запити, транскрибує підтримувані
audio/video attachments, добирає релевантні facts, dialogue та knowledge,
отримує відповідь від Ollama і повертає її inline або доставляє через Home
Assistant callback.

Основна мова коду — Python 3.11+. Основні framework і libraries визначені в
`pyproject.toml`: FastAPI, Uvicorn, Pydantic, `pydantic-settings`, `httpx`,
Telethon, `faster-whisper` і `pypdf`.

Точка входу — `src/lumen/main.py`; FastAPI application — `app`. Source runtime
запускається через Uvicorn. `compile.bat` збирає one-file `Lumen.exe`.

Поточний статус: active chat-memory-knowledge core із двома вузькими
side-effect integrations — reminders і calendar. Поточна schema:
`CURRENT_SCHEMA_VERSION = 34`. Зовнішні live integrations після rebuild/restart
ще потребують ручної перевірки.

## 2. Джерела істини

Довіряти у такому порядку:

1. current Git branch, imports і service wiring у `src/lumen/main.py`;
2. реалізація безпосередньо викликаних methods у `src/lumen/services/`;
3. `src/lumen/storage/db.py` для schema та migrations;
4. focused tests у `tests/`;
5. `AGENTS.md` і цей документ для правил та короткого контексту;
6. `docs/ARCHITECTURE.md` для потоків і меж компонентів;
7. `docs/MODULES.md` для ownership map;
8. `docs/HANDOVER.md`, `docs/ROADMAP.md`, `TODO_LUMEN.md` для поточного стану;
9. `docs/DEBUGGING.md` для operational перевірок;
10. `README.md`, `PROJECT_MAP.md`, `docs/PROJECT.md` для огляду.

`CHANGELOG_LUMEN.md` і root `DECISIONS.md` — хронологічні записи, а не доказ
активного wiring. При розбіжності документа з кодом перевірити callers та
оновити документ відповідно до live implementation.

## 3. Архітектура в одному екрані

```text
FastAPI ingress
  -> ChatOnlyService
     -> MemoryService -> MemoryStore -> SQLite
     -> ContextExecutor -> TelegramContextService | DuckDuckGoWebSearch
     -> OllamaConnector -> Ollama
     -> ReminderService -> DeliveryService | WindowsWakeAdapter
     -> CalendarActionService -> Home Assistant Calendar REST
     -> DeliveryService -> Home Assistant callback

  -> NewsProcessingService
     -> WebPageExtractor -> public article | RSS fallback
     -> MemoryService -> exact user news dislikes
     -> OllamaConnector -> editorial classification + Ukrainian 5W

HomeAssistantCalendarClient
  -> Home Assistant REST + WebSocket
  -> ReminderService calendar projection

SpeechToTextService
  -> normalized text
  -> ChatOnlyService
```

`ChatOnlyService` є центральним orchestrator. Реалізація неоднорідна:
звичайна відповідь генерується через final Ollama dialogue call, reminder має
спеціалізовану SQLite state machine, calendar write є typed direct action, а
calendar read проєктує events у reminders.

## 4. Основний runtime-потік

### Звичайний запит

1. `POST /chat`, `POST /connector`, `POST /api/chat` або
   `POST /api/generate` приймає payload у `src/lumen/main.py`.
2. `_user_id()` нормалізує identity. `_callback_metadata()` формує transport
   metadata для connector. `_message_text()` дістає текст із
   Ollama-compatible message.
3. Connector attachment перевіряє
   `SpeechToTextService.is_speech_attachment()`, завантажується через
   `transcribe_url()` і повертається в той самий text flow.
4. `ChatOnlyService.process_chat_request_result()` визначає canonical user і
   викликає `_build_dialogue_messages()`.
5. `MemoryService.build_planner_context()` отримує query contract, embeddings,
   facts, dialogue candidates та knowledge candidates. Незалежні SQLite reads
   виконуються паралельно через `asyncio.gather()` і `asyncio.to_thread()`.
6. `ContextExecutor.execute()` обробляє дозволені context requests.
   `telegram/search` і `web/search` мають live adapters;
   `home_assistant/read_state` і contract-requested `knowledge/search`
   повертають `pending_implementation`.
7. Structured reminder confirmation може short-circuit context build.
   Звичайний reminder contract передається
   `ReminderService.intercept_contract()`. Calendar create передається
   `CalendarActionService.execute()`. Обидва flows можуть повернути
   детерміновану відповідь без final dialogue call.
8. Для звичайної розмови `ChatOnlyService._generate_answer()` агрегує
   `OllamaConnector.stream_chat()`. До першого кадру timeout немає; далі
   watchdog обмежує лише 240 секунд тиші між валідними NDJSON frames. Idle
   partial не зберігається і не доставляється.
9. Non-global turn записується через `MemoryService.add_turn()`. Після цього
   callback delivery виконує `DeliveryService.deliver_chat_answer()`, а
   `MemoryService.postprocess_dialogue_turn()` добуває durable facts і
   оновлює metadata та embeddings.
10. `/chat` і `/connector` працюють background через
    `asyncio.create_task()` та повертають `202`. Ollama-compatible endpoints
    чекають результат і повертають його inline з `deliver=False`.

### Обробка новин

`POST /news/process` синхронно обробляє один Feedreader item. Один shared
`WebPageExtractor` завантажує article і використовується також
`DuckDuckGoWebSearch`; за невдалого article extraction допускається достатній
RSS text. `NewsProcessingService` відкидає редакційне сміття та exact active
`news_context/topic:*` `dislikes` користувача, а для `keep` окремо формує
українську 5W-озвучку. Одночасно виконується лише один job; contention повертає
`429`, а тимчасові збої — `status=retry`. Якщо article недоступний і Feedreader
не надав достатнього контексту, це terminal `skip` з
`reason_code=insufficient_source_context`, а не нескінченний retry.

### Telegram і Home Assistant

У репозиторії немає окремого Telegram bot ingress. Telegram message приходить
через generic `/connector`, де `source` і reply target стають callback
metadata. `TelegramContextService` — інший механізм: bounded read-only MTProto
lookup за allowlisted facts; отримані повідомлення не імпортуються у dialogue
memory.

Home Assistant виконує три окремі ролі:

- доставляє chat/reminder payload через `DeliveryService`;
- приймає calendar create через `CalendarActionService`;
- надає calendar snapshot і WebSocket invalidation через
  `HomeAssistantCalendarClient`.

Generic Home Assistant entity-state read і device control не реалізовані.

## 5. Карта ключових файлів

| Шлях | Призначення | Ключові символи |
|---|---|---|
| `src/lumen/main.py` | API, wiring, startup/shutdown | `app`, `chat()`, `connector()`, `start_reminder_recovery()` |
| `src/lumen/services/chat_only.py` | central orchestration | `ChatOnlyService`, `process_chat_request_result()`, `_build_dialogue_messages()` |
| `src/lumen/services/memory.py` | contract, retrieval, facts, post-processing | `MemoryService`, `build_planner_context()`, `postprocess_dialogue_turn()` |
| `src/lumen/memory/store.py` | SQLite queries і similarity search | `MemoryStore`, `add_turn()`, `search_turn_embeddings()`, `search_embeddings()` |
| `src/lumen/storage/db.py` | schema та migrations | `Database`, `SCHEMA`, `CURRENT_SCHEMA_VERSION` |
| `src/lumen/connectors/ollama.py` | LLM/embedding adapter | `OllamaConnector`, `chat()`, `embed()` |
| `src/lumen/services/context_executor.py` | bounded external context dispatch | `ContextExecutor`, `execute()`, `format_for_prompt()` |
| `src/lumen/services/web_search.py` | bounded page retrieval and search | `WebPageExtractor`, `DuckDuckGoWebSearch` |
| `src/lumen/services/news_processing.py` | Feedreader article filtering and 5W editing | `NewsProcessingService`, `NewsProcessingBusyError` |
| `src/lumen/services/delivery.py` | HA callback transport | `DeliveryService`, `deliver_chat_answer()`, `deliver_reminder()` |
| `src/lumen/services/reminders.py` | reminder state machine | `ReminderService`, `intercept_contract()`, `run_due()` |
| `src/lumen/services/calendar_actions.py` | typed calendar write | `CalendarActionService`, `execute()`, `create()` |
| `src/lumen/services/home_assistant_calendar.py` | calendar read/invalidation | `HomeAssistantCalendarClient`, `snapshot_today()`, `run_forever()` |
| `src/lumen/services/speech_to_text.py` | connector media transcription | `SpeechToTextService`, `transcribe_url()` |
| `src/lumen/services/knowledge.py` | upload, chunking, indexing | `KnowledgeService`, `upload()`, `upload_bytes()` |
| `src/lumen/config.py` | environment settings | `Settings`, `PROJECT_ROOT` |

## 6. Актуальні абстракції

- Orchestration: `ChatOnlyService`, без універсального action engine.
- External context: `ContextExecutor` із дозволеними source/operation pairs та
  truthful unavailable statuses.
- Side effects: окремі `ReminderService` і `CalendarActionService`; вони не
  мають спільної execution abstraction.
- LLM provider: лише `OllamaConnector`.
- Memory/RAG: `MemoryService` + `MemoryStore`; deterministic retrieval policy,
  facts, semantic/lexical dialogue candidates, knowledge embeddings.
- Integrations: `TelegramContextService`, `WebPageExtractor`,
  `DuckDuckGoWebSearch`, `NewsProcessingService`,
  `DeliveryService`, `HomeAssistantCalendarClient`,
  `CalendarActionService`, `SpeechToTextService`.
- Configuration: `Settings` із `.env`; public template — `.env.example`.
- Persistence: local SQLite tables `conversation_turns`, `memory_facts`,
  `memory_embeddings`, `knowledge_upload_sessions`, `reminders`,
  `reminder_management_sessions`, `schema_version`.
- Background processing: per-request `asyncio.create_task()`, startup reminder
  recovery, long-lived HA calendar listener і one nearest Windows wake task.

## 7. Зовнішні залежності

| Залежність | Конфігурація | Використання |
|---|---|---|
| Ollama HTTP | `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_DIALOGUE_MODEL`, `OLLAMA_EMBEDDING_MODEL`, `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_STREAM_IDLE_TIMEOUT_SECONDS`, `OLLAMA_CLIENT_KEEP_ALIVE` | `OllamaConnector` |
| SQLite | `LUMEN_DB_PATH` | `Database`, `MemoryStore`, `ReminderService` |
| Reminder lifecycle | `REMINDER_GRACE_MINUTES`, `REMINDER_SNOOZE_MINUTES`, `REMINDER_MANAGEMENT_SESSION_TTL_MINUTES` | grace для пропущених occurrence, явний snooze, selection/confirmation session |
| Home Assistant | `HOME_ASSISTANT_URL`, `HOME_ASSISTANT_TOKEN`, `HOME_ASSISTANT_WEBHOOK_ID`, `HOME_ASSISTANT_WEBHOOK_URL` | delivery і calendar services |
| HA Calendar | `HOME_ASSISTANT_CALENDAR_ENTITY_IDS`, `HOME_ASSISTANT_CALENDAR_WRITE_ENTITY_ID`, `HOME_ASSISTANT_CALENDAR_TARGET_CHAT_ID`, `HOME_ASSISTANT_CALENDAR_ALL_DAY_TIME`, `HOME_ASSISTANT_CALENDAR_RECONNECT_SECONDS`, `HOME_ASSISTANT_CALENDAR_DEFAULT_DURATION_MINUTES` | calendar read/write/projection |
| Telegram MTProto | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH`, history/message limits | `TelegramContextService` |
| DuckDuckGo/public web | окремих credentials немає | `WebPageExtractor`, `DuckDuckGoWebSearch`, `NewsProcessingService`; Trafilatura виділяє main text |
| `ffmpeg`/Whisper | `FFMPEG_PATH`, `STT_MODEL`, `STT_DEVICE`, `STT_COMPUTE_TYPE`, `STT_LANGUAGE` | `SpeechToTextService` |
| Windows Task Scheduler | DB path передається wake entrypoint | `WindowsWakeAdapter` |

Cloud LLM provider і Docker deployment у репозиторії не визначені.

## 8. Команди

Репозиторій не містить окремого installation script. Dependencies визначені в
`pyproject.toml`; не вигадувати або автоматично виконувати install command.

Source запуск:

```powershell
$env:PYTHONPATH='src'
python -m uvicorn lumen.main:app --app-dir src --host 0.0.0.0 --port 8010
```

Build:

```powershell
cmd /c compile.bat
```

Тести запускати з окремою DB:

```powershell
$env:PYTHONPATH='src'
$env:LUMEN_DB_PATH = Join-Path $env:TEMP 'lumen-unittest.db'
python -m unittest discover -s tests
```

Діагностика:

```powershell
Get-NetTCPConnection -LocalPort 8010 -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8010/
Invoke-RestMethod http://127.0.0.1:8010/openapi.json
```

Lint, type-check і Docker commands у репозиторії не визначені.

## 9. Інваріанти та обмеження

- `/chat` і `/connector` лишаються async callback paths; `/api/chat` і
  `/api/generate` — inline adapters того самого semantic core.
- Reminder destination береться тільки з normalized persisted route, не з
  `user_id`.
- Confirmation потребує current `reminder_id` і `delivery_id`.
- Reminder management лишається user- і route-scoped: `GetContract` тільки
  нормалізує intent, а `ReminderService` шукає, перевіряє та змінює SQLite rows.
- Корені з `source_ref` є calendar projections: їх показують у списку, але не
  редагують, не призупиняють, не відкладають і не скасовують локально.
- `due_at` — canonical schedule. Transport retry зберігається окремо в
  `next_attempt_at`; він не може зсунути daily/weekly anchor.
- Непідтверджене occurrence не отримує auto-snooze: silence/`no` позначає його
  пропущеним. Snooze створюється лише явною командою.
- Uncertain calendar write не retry-ити автоматично.
- Calendar target entity задається конфігурацією, не model output.
- External context bounded, read-only і не повинен прикидатися доступним при
  failure.
- Telegram context читає лише allowlisted source facts.
- Knowledge retrieval — prompt context, не окремий dialogue engine.
- Не змінювати schema, production DB, `.env`, callback contract або зовнішні
  side effects без явного scope.
- Network I/O та LLM flow залишаються async; blocking SQLite work має бути
  вузьким або винесеним через `asyncio.to_thread()`.
- Не виводити credentials, Telegram session, callback metadata чи DB contents.
- Tests не повинні використовувати production `LUMEN_DB_PATH`.

## 10. Неактуальні підходи

- Загальний planner-driven execution stack не є current runtime. Старі згадки
  можуть бути в root changelog/decision ledger та
  `docs/TARGET_EXECUTION_CONTRACT_UA.md`. Заміна — `ChatOnlyService` плюс вузькі
  reminder/calendar services.
- Старі debugging guides описують видалені modules. Їхні status headers
  спрямовують до `docs/DEBUGGING.md`.
- Broad Telegram ingestion та persistence відсутні. Поточний механізм —
  allowlisted bounded `TelegramContextService`.
- Generic smart-home control відсутній. HA використовується для callback і
  calendar integrations.
- `docs/env.local.example` не є config template; authoritative public template
  — `.env.example`.

## 11. Відомі проблеми

- `ContextExecutor`: HA state і contract-requested knowledge handlers мають
  status `pending_implementation`; дивитися `_execute_home_assistant()` та
  `_execute_knowledge()`.
- Deployed process може працювати зі старішим artifact; перевірити port,
  OpenAPI та rebuild/restart. Статус live verification — TBD.
- `run.bat` містить PowerShell assignment у batch-файлі; canonical launch
  потребує рішення власника.
- Import `src/lumen/main.py` виконує `Database.init()`; test discovery без
  окремого DB path може торкнутися operational DB.
- Unit suite emits `StarletteDeprecationWarning` для
  `fastapi.testclient`/installed `httpx`; compatibility cleanup у TODO.
- Local in-process similarity search потребує benchmark зі зростанням history.

## 12. Поточний фокус

Підтверджено `TODO_LUMEN.md` і `docs/HANDOVER.md`:

1. rebuild/restart current artifact і health/OpenAPI smoke;
2. live text callback через Home Assistant;
3. reminder wake, delivery, lifecycle та exact confirmation;
4. calendar timed/all-day/update/delete і одна навмисна create command;
5. connector audio/video STT;
6. approved Telegram source lookup;
7. dialogue, facts, knowledge та Ollama-compatible endpoints;
8. окремий reviewed cleanup legacy residue;
9. рішення щодо partial HA/knowledge context handlers;
10. усунення test-client compatibility warning.

## 13. Правила роботи AI-агента

1. Прочитати `AGENTS.md`.
2. Прочитати `docs/AI_CONTEXT.md`.
3. Для деталей звернутися до `docs/ARCHITECTURE.md` і `docs/MODULES.md`.
4. Перед зміною простежити реалізацію від entrypoint до effect/persistence.
5. Не створювати дублікати services, contracts або документації.
6. Для великих змін спочатку показати план і найменший file set.
7. Запустити найвужчі релевантні перевірки з isolated test DB.
8. Оновити docs і changelog, якщо змінилися behavior або architecture.
9. Не трактувати власну пам’ять, старий документ чи назву символу як source of
   truth.

## 14. Невідоме

- Чи стане `codex/calendar-reminders` новим `main` baseline?
- Який canonical deployment: source Uvicorn, виправлений launcher чи
  `Lumen.exe`?
- Який exact commit зараз працює на port `8010`?
- Який current health Ollama, Home Assistant, Telegram session, STT runtime і
  Windows wake task?
- Чи потрібен generic HA state read?
- Чи потрібен explicit contract-requested knowledge lookup на додачу до
  automatic retrieval?
- Коли дозволений cleanup legacy models/store API/data?
- Які очікувані history volume, retention та performance targets?
