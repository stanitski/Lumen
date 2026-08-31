# Конвенції Lumen

## Фактичні правила, підтверджені кодом

### Структура

- HTTP wiring зосереджене у `src/lumen/main.py`.
- Бізнес-flow оформлюється service classes у `src/lumen/services/`.
- Ollama adapter — `src/lumen/connectors/ollama.py`.
- SQLite schema і queries розділені між `storage/db.py` та `memory/store.py`.
- Pydantic API/storage payloads знаходяться у `models.py`; внутрішні typed
  contracts також використовують frozen dataclasses.

### Іменування і типізація

- Python symbols — `snake_case`, classes — `PascalCase`;
- публічні/ключові методи мають type annotations;
- external structured payloads нормалізуються перед mutation:
  `MemoryService._normalize_*`, `normalize_delivery_route()`,
  `CalendarActionService._parse_create_contract()`;
- status/outcome передаються явними рядками або `Literal`.

### Async

- network/LLM/STT orchestration — `async`;
- незалежні memory searches збираються через `asyncio.gather()` та
  `asyncio.to_thread()`;
- async callback ingress створює `asyncio.create_task()`;
- блокувальні SQLite queries лишаються sync і виконуються вузько.

### Помилки

- transport failure не має переписувати semantic answer;
- uncertain calendar write не retry-иться;
- malformed/ambiguous reminder contract не мутує DB;
- optional context failure повертає truthful unavailable/pending result;
- broad exceptions на orchestration boundary логуються і деградують до
  user-facing fallback.

### Конфігурація

- `Settings`/environment, не hardcoded deployment secrets;
- відносні operational paths резолвляться від `PROJECT_ROOT`;
- optional integrations вмикаються тільки повним required config set;
- `.env.example` є public template; `.env` не читати/не публікувати.

### Логування

- logger `lumen`, file + stream handlers;
- IDs/status/counts логуються, secrets і full route metadata — ні;
- debug Ollama payload має окремий configured path;
- для live діагностики починати з `data/debug/lumen.log`.

### Tests

- stdlib `unittest`, включно з `IsolatedAsyncioTestCase`;
- external HTTP/WebSocket/wake/delivery замінюються fake factories;
- schema migration перевіряється temporary SQLite;
- live side effects не створюються автоматично tests.

### API та інтеграції

- async callback і inline Ollama adapters використовують один
  `ChatOnlyService`;
- destination для reminder — allowlisted persisted route;
- HA calendar entity для write задається constructor config, не LLM;
- external context обмежений source/operation allowlist і budget.

## Рекомендовані правила

Це рекомендації аудиту, а не усталений contract:

- не додавати новий module-level singleton без test injection seam;
- не розширювати `models.py`/`memory/store.py`, доки legacy API не ізольовано;
- перейменувати `build_planner_context` лише окремим compatibility-aware change;
- для нового external source вимагати typed contract, policy, bounded fetch,
  truthful failure і focused tests;
- додаючи side effect, явно визначати idempotency/retry/unknown-result policy.

## Винятки і борг

- `run.bat` не відповідає типовому Windows batch syntax;
- `MemoryStore` містить legacy methods до таблиць, яких немає у schema `v34`;
- legacy Pydantic models і JSON seeds не відображають current architecture;
- коментарі/імена з `planner` можуть означати retrieval contract, а не active
  planner subsystem.
