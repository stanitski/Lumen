# Діагностика Lumen

## Порядок перевірки

1. Зафіксувати exact input, ingress, `user_id`, час і фактичну відповідь.
2. Перевірити, який artifact/process слухає `8010`.
3. Перевірити `data/debug/lumen.log`.
4. Простежити endpoint у `main.py`.
5. Простежити `ChatOnlyService.process_chat_request_result()`.
6. Розділити execution, persistence, delivery і post-processing outcome.
7. Запустити один focused test.
8. Лише потім робити live integration check.

## Запуск і health

Source:

```powershell
$env:PYTHONPATH='src'
python -m uvicorn lumen.main:app --app-dir src --host 0.0.0.0 --port 8010
```

Перевірка:

```powershell
Get-NetTCPConnection -LocalPort 8010 -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8010/
Invoke-RestMethod http://127.0.0.1:8010/openapi.json
```

Build:

```powershell
cmd /c compile.bat
```

`run.bat` є Windows batch launcher від власного каталогу: використовує локальний
`.venv\Scripts\python.exe`, якщо він є, інакше `python` із PATH. Запускає
`python -m lumen.main`; host/port читаються з `Settings`. Source smoke:
`cmd /c run.bat --timezone-smoke` (тільки з isolated DB/log environment для тесту).

## Логи й конфігурація

- log: configured `LOG_FILE_PATH`, default `data/debug/lumen.log`;
- last Ollama request: `OLLAMA_DEBUG_DUMP_PATH`;
- defaults/validation: `src/lumen/config.py`;
- public values: `.env.example`;
- secrets: `.env` не друкувати.

Для final dialogue перевіряти lifecycle log `Ollama stream`: `request`, `model`,
`elapsed_ms`, `first_frame_ms`, `frames`, `chars`, `outcome`. Відсутній
`first_frame_ms` означає, що Ollama ще не віддала перший observable кадр;
під час prompt evaluation Lumen не має окремого progress signal. Після першого
кадру `idle_timeout` означає 240 секунд без нового валідного NDJSON frame.

Перевірити, що source і executable резолвлять той самий `PROJECT_ROOT`,
DB/log paths та `.env`.

## Симптом → точка входу

| Симптом | Перевірити |
|---|---|
| `202`, але немає відповіді | background task logs; `ChatOnlyService`; `DeliveryService` |
| Inline API має іншу відповідь | `/api/chat`/`/api/generate` adapter і `deliver=False` |
| Inline API повернув `504 Ollama stream stalled` | lifecycle log; час першого кадру; пауза між NDJSON frames |
| Відповідь не пам’ятає факт | `build_planner_context()`, selected facts, retrieval policy |
| Старий діалог домінує | semantic/lexical candidates, previous-turn anchor, debug-search |
| Knowledge upload є, retrieval нема | `KnowledgeService`, `memory_embeddings`, knowledge hit thresholds |
| Telegram context unavailable | allowlisted `messenger_context` facts, kind/ref, credentials/session |
| Web claim без evidence | `ContextExecutor` result status і supplied URLs |
| HA state не читається | generic handler pending; не плутати з calendar/delivery paths |
| Reminder не доставлено або прийшов надто пізно | route JSON, status/lease/attempts/`next_attempt_at`, grace, wake task, HA callback |
| Confirmation не працює | exact `reminder_id` + current `delivery_id` |
| Update/delete не застосовано | stable ID/query, management token, route scope, `updated_at` conflict, calendar `source_ref` |
| Calendar event duplicated | після timeout не retry; перевірити calendar вручну |
| Calendar reminder не з’явився | token/entities/target config, current-day snapshot, event `uid` |
| Audio/video ignored | mime/name detection, URL download, ffmpeg, Whisper settings |

## Memory/RAG

Локальна адмінка `/memory`: факти, редагування/видалення, пошук і тестова
відповідь із точним знімком переданого контексту. Див. [MEMORY_ADMIN.md](MEMORY_ADMIN.md).

Без запису нового turn:

```powershell
$body = @{ text = 'тестовий запит'; user_id = 'VST' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/memory/debug-search -ContentType application/json -Body $body
```

Перевірити `query_contract`, `retrieval_policy`, selected facts/turns/knowledge,
`context_execution` і final message count. `/memory/delete` змінює дані —
використовувати тільки з явним погодженням.

## Підготовка й перевірка fact memory

`scripts/prepare_fact_memory.py` не імпортує `lumen.main`, не виконує schema
init, не створює діалогів і не викликає delivery. Без `--apply` це read-only
preview; `--index` сам по собі не запускає Ollama чи записи.

Для ручного позначення підготувати приватний JSON manifest — масив об’єктів
із `id`, `expected_hash` (через `fact_content_hash()` від перевіреного запису)
та `inclusion` (`mandatory` або `contextual`). Переглянути зміст і джерело
кожного факту; не переносити task-local instructions у standing rules.
Manifest і backups зберігати поза Git, наприклад у `data/debug/` і `data/backups/`.

```powershell
$env:PYTHONPATH='src'
python scripts/prepare_fact_memory.py --user-id VST --manifest data/debug/fact-memory-mandatory-manifest.json --index
python scripts/prepare_fact_memory.py --user-id VST --manifest data/debug/fact-memory-mandatory-manifest.json --index --apply
```

Apply спершу робить консистентний SQLite backup, потім перевіряє всі owner/hash
у транзакції та змінює лише policy tags. Backfill можна повторити без manifest:
`python scripts/prepare_fact_memory.py --user-id VST --index --apply`.
Він пропускає актуальні hash/model, індексує missing/stale contextual facts,
повертає counters і завершується з ненульовим кодом за failed embeddings.
Для global facts запускати окремо з `--user-id global`; персональні правила
на global scope не позначати.

Під час debug перевіряти `mandatory_facts`, `contextual_facts`, `selected_facts`
і `fact_retrieval`: `missing_indexes`, `stale_indexes`, `fallback`, `status`.
Для «Привіт» усі standing rules мають бути в `mandatory_context`, а не лише
перші три. Зміна embedding model потребує backfill; до нього працює lexical
fallback. Налаштування порога `FACT_SEMANTIC_MIN_SIMILARITY=0.37` перевірено на
невеликому різномовному наборі; це не гарантія релевантності для всіх тем.

Відкат коду та БД — різні операції. Checkpoint перед цією зміною: `5aed97e`.
Не замінювати всю production БД старим backup після появи нових діалогів чи
reminders: повертати лише перевірені policy tags та службовий fact index.
Rebuild/restart executable виконується окремо.

API-тести із startup повинні також вимикати external integrations через
environment: порожні `HOME_ASSISTANT_TOKEN`, `HOME_ASSISTANT_CALENDAR_ENTITY_IDS`
і `HOME_ASSISTANT_URL`, окремі `LUMEN_DB_PATH` та `LOG_FILE_PATH`. Однієї
тимчасової БД недостатньо, щоб запобігти calendar reads із локального `.env`.
`tests/runtime_test_support.py` задає тимчасові DB/log/session paths і вимикає
HA/Telegram перед імпортом `lumen.main`; API-тести імпортують `main` через нього.
Для нових API-тестів використовувати цей helper, а не прямий import entrypoint.

## Packaged `_MEI` runtime

Якщо процес і порт `8010` живі, але Ollama, Telegram, HA callback та calendar
одночасно падають із `FileNotFoundError: [Errno 2] No such file or directory`
через `ssl.create_default_context()`, перевірити extraction-каталог процесу.
Частково очищений `%TEMP%\_MEI*` може залишити EXE доступним, але непридатним
для нових HTTP-з’єднань.

Поточна збірка має створювати `_MEI*` під `data\runtime`, а не в `%TEMP%`.
Контрольована процедура відновлення: завершити обидва процеси `Lumen.exe`,
перебудувати через `cmd /c compile.bat`, запустити `dist\Lumen.exe`, перевірити
`/openapi.json`, розташування нового `_MEI*` і один inline `/api/chat` без
delivery. Не видаляти extraction-каталог живого процесу.

## Telegram

STT: порожнє `STT_LANGUAGE=` вмикає автовизначення мови кожного audio/video
attachment; `STT_LANGUAGE=uk` примусово задає українську. Після зміни потрібен
restart Lumen, rebuild не потрібен. Якість перемикань мов усередині одного
запису перевіряти окремо на оригінальному аудіо.

1. `MemoryStore.list_facts()` має повернути explicit source fact.
2. `TelegramContextService._select_sources()` має дотримати `kind`.
3. Credentials і session мають бути configured.
4. Explicit date range обмежується 500 scanned messages/source.
5. Telegram context не повинен записувати fetched messages у memory.

Для channel source поточний runtime читає текст/caption і не запускає STT для
вкладених audio/video. Повторювані `ffmpeg was not found` або
`Telegram media transcription ...` під час саме channel lookup означають, що
запущений застарілий executable: звірити packaged
`lumen.services.telegram_context` із source, перебудувати EXE і перезапустити
процес. Для user/group source STT залишається очікуваною поведінкою.

Пошкоджене media може дати warning, але не повинно завершувати весь запит через
`Chat-only dialogue failed`: Lumen має зберегти caption і продовжити пошук.
Нормальна послідовність для асинхронного `/connector`: HTTP acceptance, згодом
`Read N Telegram message(s)`, Ollama request і callback у Home Assistant. Сам
успішний acceptance ще не підтверджує завершення обробки або delivery.

Login helper:

```powershell
$env:PYTHONPATH='src'
python scripts/telegram_login.py
```

Це інтерактивний credential flow; не передавати коди/паролі через чат.

## Home Assistant і reminders/calendar

- delivery URL: `DeliveryService.home_assistant_callback_url()`;
- calendar read: `HomeAssistantCalendarClient.snapshot_today()`;
- calendar write: `CalendarActionService.create()`;
- reminder recovery:

```powershell
$env:PYTHONPATH='src'
python -m lumen.reminder_wakeup
```

Для reminder drift перевірити в SQLite лише через safe diagnostic tooling:
`due_at` — canonical Kyiv slot, `next_attempt_at` — тільки наступна transport
спроба. Значення `missed` означає, що occurrence було старше
`REMINDER_GRACE_MINUTES` (default `30`) або не підтверджене до deadline; воно
не повинно автоматично створювати snooze child. Child створює лише explicit
`snooze`; default керується `REMINDER_SNOOZE_MINUTES` (default `15`).

`LumenReminderWake` реєструється з battery-safe settings. Це не гарантує
delivery за повністю вимкненого ПК: після увімкнення recovery пропускає
occurrence поза grace window. Для редагування/паузи/видалення calendar-projected
row очікувана відповідь — read-only, оскільки `source_ref` належить HA calendar
projection.

Live calendar create має бути лише навмисним ручним тестом. Не створювати
автоматичну test event у календарі користувача.

## Focused tests

```powershell
$env:PYTHONPATH='src'
$env:LUMEN_DB_PATH = Join-Path $env:TEMP 'lumen-unittest.db'
python -m unittest tests.test_chat_only_runtime
python -m unittest tests.test_query_contract tests.test_context_executor
python -m unittest tests.test_reminders
python -m unittest tests.test_reminder_storage tests.test_reminder_wake tests.test_config_reminders
python -m unittest tests.test_calendar_actions tests.test_home_assistant_calendar tests.test_timezones
python -m unittest tests.test_ollama_api tests.test_ollama_connector
python -m unittest tests.test_speech_to_text tests.test_telegram_context
```

Для повного локального regression:

```powershell
$env:PYTHONPATH='src'
$env:LUMEN_DB_PATH = Join-Path $env:TEMP 'lumen-unittest.db'
python -m unittest discover -s tests
```

Unit success не доводить стан external services або deployed executable.
Не запускати discovery з production `LUMEN_DB_PATH`: import `lumen.main`
виконує `Database.init()` на module load.
