# Передача поточного стану

Дата базового аудиту: 2026-07-23. Гілка: `codex/calendar-reminders`, commit
`0060a2a` (`Add calendar-backed reminders and event creation`).

Після цього аудиту в робочому дереві підготовлено (але тут не live-verified)
schema `v34` і повний локальний reminder lifecycle: management actions,
persisted selection/bulk-confirmation sessions, `paused`/`missed`, explicit-only
snooze, anchored `next_attempt_at` retry та battery-safe Windows wake settings.
Calendar projections лишаються read-only. Передавати це як source change, що
потребує rebuild/restart і окремого live verification, а не як підтверджений
deployed стан.

## Підтверджені останні зміни

За `git show 0060a2a`, live-кодом і tests:

- базовий commit додав ізольований `ReminderService` зі schema `v33`, recurrence,
  correlated confirmation, snooze children і fail-closed delivery route;
- додано `WindowsWakeAdapter` та `lumen.reminder_wakeup`;
- додано current-day Google Calendar projection через Home Assistant REST +
  WebSocket invalidation;
- додано typed, non-retrying calendar create через
  `CalendarActionService`;
- bundled `Europe/Kyiv` timezone підтримує frozen runtime;
- розширено tests reminders/calendar/API/query contract.

## Підходи, від яких відмовилися

Підтверджено current docs, відсутністю live imports і рішенням
`2026-07-20 - Current documentation must describe only the chat-only runtime`:

- старий planner/skills/tasks/policy/action executor stack;
- smart-home device control;
- broad Telegram ingestion/persistence;
- окремий attachment dialogue engine;
- прямий Google Calendar client;
- реконструкція reminder destination з `user_id`;
- automatic retry uncertain calendar writes.

Historical reasons до fresh Git baseline не завжди можна перевірити Git
історією; для них root `DECISIONS.md` є записом, але не live proof.

## Недороблене

- `ContextExecutor._execute_home_assistant()` і `_execute_knowledge()`;
- source-management UI/API для Telegram allowlist facts;
- cleanup legacy models/store methods/JSON seed files;
- rename або ізоляція legacy-named `build_planner_context`;
- live end-to-end verification після rebuild/restart.

## Відомі проблеми та ризики

- branch має великий calendar/reminder delta від `main`;
- process на `8010` може бути старим executable до контрольованого restart;
- calendar projection працює тільки для current Kyiv day;
- calendar create timeout має status `unknown`; manual calendar check потрібен
  перед повтором;
- SQLite similarity search може деградувати зі зростанням history;
- module-level service initialization у `main.py` ускладнює isolated startup;
- legacy code у `models.py`/`memory/store.py` вводить нових агентів в оману;
- `run.bat` містить PowerShell syntax у `.bat` файлі — його фактичний спосіб
  запуску потребує підтвердження.
- test suite проходить, але `fastapi.testclient` emits
  `StarletteDeprecationWarning` про installed `httpx` compatibility.

## Найближчі задачі

1. Rebuild/restart і health/openapi smoke.
2. Live text callback через Home Assistant.
3. Live reminder lifecycle: management, grace/missed, explicit snooze, wake + exact confirmation.
4. Live calendar read/update/delete і одна навмисна create command.
5. STT audio/video.
6. Telegram allowed source lookup.
7. Memory/knowledge/Ollama-compatible smoke.
8. Окремий reviewed cleanup legacy residue.

Деталі та залежності: [ROADMAP.md](ROADMAP.md).

## Питання власнику

- Чи має `codex/calendar-reminders` стати новим baseline/main?
- Який canonical deployment/start command: Uvicorn source, `run.bat` чи
  `Lumen.exe`?
- Чи потрібен generic Home Assistant state read, якщо calendar і callback
  інтеграції вже вузько реалізовані?
- Чи потрібен explicit contract-based knowledge search на додачу до automatic
  retrieval?
- Коли дозволено видалити legacy planner/skills/tasks моделі, store API та JSON?
- Який SLA/retention потрібен для SQLite history і knowledge?

## Невідоме

Цей docs-only аудит не перевіряв external credentials, live HA/Telegram/Ollama,
Windows Task Scheduler або запущений executable. Їхній стан — `TBD`.
