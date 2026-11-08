# Передача поточного стану

2026-11-01: у live HA `Lumen Connector` додано відповідь у Telegram і
завершення обробки перед wake/connector, якщо `binary_sensor.power_input` і
`binary_sensor.10_0_0_100` мають стан `off`. За доступного комп’ютера запит
передається Lumen. Конфігурацію збережено й automation перезавантажено; фактичну доставку
повідомлення ще не перевірено. Backup попередньої automation:
`data/runtime/ha-lumen-connector-before-power-notice-20261101-121708.json`.

HA deployment 2026-10-08 за погодженням користувача: fresh live YAML
перевірено, змінено тільки `Lumen Connector` і callback automation; додано
`lumen_connector_nutrition`, `lumen_nutrition_reply` та sent correlation.
Backup: `/homeassistant/backups/lumen-nutrition-20261008-115807/`.
HA templates/installed REST payload перевірено; `ha core check` і
`script.reload`/`automation.reload` пройшли. HA REST → Lumen повернув `202`
з `nutrition_delivery`; loaded helper smoke — HTTP 200. Validation user/tag
не створили журнал і не відправляли Telegram messages. Core не перезапускався.
Real food-photo/portion/reply delivery залишаються ручною перевіркою.

Deployment 2026-10-08 за запитом запуску оновленого Lumen: обидва старі EXE
процеси зупинено; попередній EXE та SQLite backup з integrity `ok` збережено
в `data/backups/restart-20261008-114246/`. `compile.bat` завершився успішно,
44 focused nutrition/API/timezone tests і isolated packaged timezone smoke
пройшли; nutrition service/store підтверджено в EXE archive. Новий
`dist/Lumen.exe` запущено приховано, PID 32900/22352; health/OpenAPI HTTP 200
на `8010`, `nutrition_delivery` присутній у deployed schema. SHA-256:
`12a6454cf7f18ab60b91110c206e4fb86d54b13b5234ef76e1dedd2b06f6a519`.
На момент Lumen restart HA ще не був змінений; наступний HA deployment
зафіксовано вище. Real-photo inference і Telegram delivery ще не перевірені.
Нижчі dated записи описують попередній стан.

Початкова source-підготовка 2026-10-08: локально додано Telegram food-photo journal (`NutritionService`,
`NutritionStore`, per-user JSON/CSV), уточнення/виправлення й sent-message
correlation. Інструкція та HA helper — `NUTRITION.md` і
`examples/home_assistant_nutrition.yaml`. Тести ізольовані від real DB/Ollama/HA.
EXE не перебудовано, HA не змінено; потрібні окремий deployment та real-photo check.

Deployment 2026-10-07 після явного запиту restart: старі процеси
`D:\WORK\LUMEN\dist\Lumen.exe` зупинено, source перебудовано через `compile.bat`.
Packaged `--timezone-smoke` пройшов на isolated DB; новий EXE запущено приховано
з working directory `D:\WORK\LUMEN`. `/` і `/openapi.json` повернули HTTP 200
на `8010`. SHA-256 нового EXE:
`59c5baf69b6c45669f6140c62565a4f19416988dc76dc2100dac601b338f79f7`.
Попередній EXE та консистентний SQLite backup збережені в
`data/backups/restart-20261007-181158/`. Callback, STT, calendar writes і live
Ollama answer у цій перевірці не викликалися.

Оновлення 2026-10-07: локально видалено невикористані execution models/store
APIs і JSON seeds; timezone data та schema `v34` збережено. Knowledge replacement
атомарний після повної перевірки embedding batch, fact lookup використовує ID
maps і cached lexical scores. Memory API використовує configured user і
user-scoped turn deletion; background tasks tracked і скасовуються на shutdown.
`run.bat` став переносним launcher із Settings host/port. API test bootstrap
ізолює DB та external integrations до import entrypoint. Це source change,
без rebuild/restart deployed executable; нижчі dated deployment records є історією.

Оновлення 2026-09-02: Telegram lookup для channel sources переведено на
text/caption-only path. Lumen більше не завантажує і не транскрибує кожне
audio/video вкладення каналу; для allowlisted user/group розмов media STT
залишився активним. Помилка декодування окремого вкладення ізольована, caption
зберігається, а пошук продовжується. Зміна усуває затримку, через яку запит про
причину тривоги від 10:11 завершився лише о 10:19. Пройшли 39 focused
Telegram/query/chat tests. `dist/Lumen.exe` перебудовано й перезапущено;
`/openapi.json` відповідає HTTP 200, packaged module збігається із source.
Попередній EXE: `data/backups/Lumen-before-channel-caption-fix-20260902-102140.exe`.
Home Assistant у цій зміні не змінювався.

Оновлення 2026-08-31: checkpoint перед fact-memory зміною — `5aed97e`.
Source тепер розділяє `lumen:mandatory` правила та semantic/lexical contextual
facts; подробиці в `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`.
Шість перевірених правил позначені, 37 contextual vectors побудовані після
SQLite backup; повторне індексування не створює нових vectors. Source-only
greeting preview та локальна model-driven класифікація перевірені без запису
діалогу й delivery. За окремим запитом `dist/Lumen.exe` перебудовано й запущено
2026-08-31 о 15:41; health та `/openapi.json` відповідають на порту 8010.
Packaged `/memory/debug-search` для привітання отримав усі шість mandatory
правил без contextual facts і відповів на «ти», без запису діалогу й delivery.
Попередній EXE: `data/backups/Lumen-before-rebuild-20260831-154034.exe`.
Поріг retrieval налаштовуваний; зайві semantic candidates та можливі помилки
автоматичної класифікації залишаються documented risks у `TODO_LUMEN.md`.

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
- rename або ізоляція legacy-named `build_planner_context`;
- live end-to-end verification після rebuild/restart.

## Відомі проблеми та ризики

- branch має великий calendar/reminder delta від `main`;
- після source change слід звіряти packaged module з source: процес на `8010`
  не підхоплює зміни без контрольованого rebuild/restart;
- calendar projection працює тільки для current Kyiv day;
- calendar create timeout має status `unknown`; manual calendar check потрібен
  перед повтором;
- SQLite similarity search може деградувати зі зростанням history;
- module-level service initialization у `main.py` ускладнює isolated startup;
- source cleanup і переносний `run.bat` ще потребують перевірки наступного
  deployed artifact; локальний source smoke не підтверджує live integrations.
- test suite проходить, але `fastapi.testclient` emits
  `StarletteDeprecationWarning` про installed `httpx` compatibility.

## Найближчі задачі

1. Live text callback через Home Assistant після наступного планового запиту.
2. Live reminder lifecycle: management, grace/missed, explicit snooze, wake + exact confirmation.
3. Live calendar read/update/delete і одна навмисна create command.
4. STT audio/video для allowlisted user/group source.
5. Memory/knowledge/Ollama-compatible smoke.
6. Перевірка наступного artifact після локального cleanup.

Деталі та залежності: [ROADMAP.md](ROADMAP.md).

## Питання власнику

- Чи має `codex/calendar-reminders` стати новим baseline/main?
- Який canonical deployment/start command: Uvicorn source, `run.bat` чи
  `Lumen.exe`?
- Чи потрібен generic Home Assistant state read, якщо calendar і callback
  інтеграції вже вузько реалізовані?
- Чи потрібен explicit contract-based knowledge search на додачу до automatic
  retrieval?
- Чи потрібне подальше спрощення compatibility naming у `memory.py`?
- Який SLA/retention потрібен для SQLite history і knowledge?

## Невідоме

Live air-alert запит підтвердив Telegram channel retrieval, Ollama answer і
callback у Home Assistant до rebuild 2026-09-02. Після rebuild підтверджені
health, OpenAPI та packaged source; наступний реальний callback перевірятиме вже
новий caption-only runtime. Windows Task Scheduler, reminder lifecycle та
calendar writes у межах цього оновлення не перевірялися.


## 2026-10-18 — nutrition incident repair
- Backups: `data/backups/nutrition-fix-20261018-123459/` (previous EXE, private .env, SQLite integrity ok).
- `HOME_ASSISTANT_URL` aligned with actual HA photo origin after authenticated API verification; same incident JPEG downloaded successfully (136342 bytes).
- Targeted media-flow audit fixed nutrition failure stages and STT bearer origin comparison/HTTP failure wrapping.
- Real photo exposed default-thinking timeout and omitted optional estimate; fixed using think=False and required nullable schema field.
- Real HA + configured Ollama + isolated CSV/photo/reply-half succeeded (450 → 225 kcal, same row). No production nutrition write or Telegram send during validation.
- Focused tests: 49 passed after final source change. Full suite: 243 passed before final schema/thinking adjustment.

Final rebuild/restart verified 2026-10-18: PID 4944/25608, HTTP health 200; packaged nutrition `_analyze` includes think argument. EXE SHA-256 `3f06ab08c238c8466614f02ef1a12103dbd5430b085432b9cb9fcb9c57b04b7b`.

## 2026-10-18 — text meals deployment
Explicit Telegram text meal creation enabled; 48 focused and 247 full tests passed. Live configured-model test in isolated storage: coffee/cookies 350 kcal, separate banana 90 kcal, half banana correction 45 kcal; two rows preserved. Previous EXE: `data/backups/nutrition-text-20261018-125210/`. Final EXE SHA-256 `4c7eeca7588072e6463805b87f039762bd067a39a08c868692e289548ea38225`. Production journal untouched by validation; real Telegram sending not exercised by test.


## 2026-10-18 — shared conversation deployment
- Implemented shared ConversationService; routing and final responses receive bounded chronological conversation context. Nutrition, reminder/calendar results, due reminders, STT failure responses and knowledge upload outcomes use shared history. News processing is excluded by explicit user instruction.
- JSON nutrition summaries are computed in code with configured timezone. Transient failure recovery replaces the same event result without repeating a committed meal; callback retry preserves nutrition tags and reply IDs.
- App runtime schedules tracked indexing after persistence/delivery and restores configured-user unprocessed turns at startup. Source-mode reminder_wakeup also persists history before exiting; live Windows wake task was not triggered during validation.
- Backup: `D:\WORK\LUMEN\data\backups\shared-context-20261018-132510` contains previous EXE, consistent SQLite (integrity ok), and nutrition files. SQLite schema remains unchanged.
- Rebuilt final EXE and started hidden, PIDs 10780/22420. HTTP health 200; packaged ConversationService and result-recovery code verified in archive. SHA-256 `05f1bebf69924aa1d8523b6dd8a10487a6979d2c4249356141c5672ccba536df`.
- Configured-model validation in isolated SQLite/JSON/delivery: 350 → daily 350 → half 175 → daily 175, and short first text request banana 90 → daily 90 → half 45 → daily 45. Four turns persisted in each case, repeated delivery suppressed. Embedding calls in these focused live checks were stubbed; full automated tests cover indexing separately.
- No test Telegram messages, calendar events or nutrition entries were written to the user's live integrations. Live Telegram cross-service follow-up remains a manual check. HA YAML was not changed for this deployment.

Final verification: full project suite 282 tests passed after the final notification-chat-scope fix; focused reminder/conversation scope 17 passed. Existing configured-user journal read-only check returned 2 meals / 800 kcal for 2026-10-18. Git diff whitespace check passed.


## 2026-10-18 — separate nutrition sent correlation removed
User explicitly requested removal of `lumen_nutrition_sent_correlation` from live HA; no replacement automation created. Original automations YAML backed up, remaining automation objects verified unchanged and `ha core check` passed. Existing Lumen ingress/callback retained; new reply-to-bot nutrition bindings no longer arrive. Local example/documentation updated.
Backup: `/homeassistant/backups/remove-nutrition-correlation-20261018-193345/automations.yaml`. `automation.reload` HTTP 200; main connector remains on. Removed the resulting unavailable orphan entity from HA entity registry. Example YAML parses and contains no automation block.


## 2026-11-09 — memory administration dashboard

Implemented and deployed local `/memory` dashboard with editable facts, source-linked exact preview context, revision conflicts and explicit index failures. Contract and operator workflow: `MEMORY_ADMIN.md`. Static Python UI module is bundled automatically.

77 focused tests passed; browser QA used synthetic isolated data. Packaged EXE page/items/edit/delete smoke passed on an isolated database before deployment. Prior executable and consistent DB backup: `data/backups/memory-admin-20261109-005740`. New `dist/Lumen.exe` was hash-verified and restarted; live admin inventory returned 45 facts / 1260 conversation records. The dashboard adds no SQLite schema migration. Production writes were not exercised for QA.

Live real-model preview succeeded through `/memory/admin/preview`; read-only database verification afterward confirmed 45 facts / 1260 conversation records and the removed incorrect alias still absent. This verifies preview generation, not every external integration.
