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

`run.bat` містить PowerShell assignment syntax; не вважати його підтвердженим
batch launcher до окремої live перевірки.

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

Без запису нового turn:

```powershell
$body = @{ text = 'тестовий запит'; user_id = 'VST' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/memory/debug-search -ContentType application/json -Body $body
```

Перевірити `query_contract`, `retrieval_policy`, selected facts/turns/knowledge,
`context_execution` і final message count. `/memory/delete` змінює дані —
використовувати тільки з явним погодженням.

## Telegram

1. `MemoryStore.list_facts()` має повернути explicit source fact.
2. `TelegramContextService._select_sources()` має дотримати `kind`.
3. Credentials і session мають бути configured.
4. Explicit date range обмежується 500 scanned messages/source.
5. Telegram context не повинен записувати fetched messages у memory.

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
