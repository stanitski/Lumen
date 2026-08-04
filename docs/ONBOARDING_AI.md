# Onboarding для AI-агента

## Маршрут читання

1. `AGENTS.md` — правила роботи.
2. `docs/AI_CONTEXT.md` — стислий перевірений operational context.
3. `docs/PROJECT.md` — межі продукту.
4. `docs/ARCHITECTURE.md` — live flows.
5. `docs/MODULES.md` — карта власників логіки.
6. `docs/HANDOVER.md` і `docs/ROADMAP.md` — поточний стан та TBD.
7. Для багів — `docs/DEBUGGING.md`.
8. Потім прочитай entrypoint і один affected flow end to end.

## Джерела істини

Пріоритет:

1. поточна гілка та imports/wiring у `src/lumen/main.py`;
2. реалізація прямо викликаних services;
3. `src/lumen/storage/db.py` для schema;
4. focused tests;
5. актуальні `docs/*.md`;
6. root historical ledgers `DECISIONS.md` і `CHANGELOG_LUMEN.md`.

Назва класу, JSON-файл чи старий changelog entry не доводить, що subsystem
активний. Простеж caller від endpoint/startup.

## Де що шукати

- entrypoint/API: `src/lumen/main.py`;
- orchestration: `src/lumen/services/chat_only.py`;
- query contract/retrieval/facts: `src/lumen/services/memory.py`;
- low-level persistence: `src/lumen/memory/store.py`;
- schema/migrations: `src/lumen/storage/db.py`;
- Ollama: `src/lumen/connectors/ollama.py`;
- Telegram/web context: `src/lumen/services/context_executor.py`,
  `src/lumen/services/telegram_context.py`,
  `src/lumen/services/web_search.py`;
- reminders/calendar: `src/lumen/services/reminders.py`,
  `src/lumen/services/calendar_actions.py`,
  `src/lumen/services/home_assistant_calendar.py`,
  `src/lumen/services/reminder_wake.py`;
- Home Assistant delivery: `delivery.py`;
- STT: `speech_to_text.py`;
- knowledge: `knowledge.py`, `file_text.py`;
- tests: відповідний модуль у `tests/`;
- configuration contract: `config.py` та `.env.example`.

## Що не індексувати без потреби

`.git/`, `.venv/`, `build/`, `dist/`, `__pycache__/`, `archive/`,
`data/backups/`, databases, sessions і debug dumps. Не відкривай `.env` і не
виводь credentials.

Historical-only:

- `docs/TARGET_EXECUTION_*.md`;
- `LUMEN_DEBUG_GUIDE.md`;
- `LUMEN_DEBUGGING_PLAYBOOK.md`;
- `LUMEN_NUTRITION_ACTIVITY_HANDOFF.md`.

## Перевірка гіпотези

Для кожного технічного твердження вкажи file + symbol. Для runtime:

1. endpoint/startup;
2. constructor wiring;
3. service method;
4. persistence/external call;
5. focused test.

Якщо зв’язок не підтверджено, познач `TBD`. Не відновлюй planner/skills/tasks
за старою документацією.

## Після зміни

- запусти найвужчі `python -m unittest ...`;
- для packaging/runtime змін перевір actual `Lumen.exe`;
- онови документацію, `CHANGELOG_LUMEN.md`, а за потреби `DECISIONS.md` і
  `TODO_LUMEN.md`;
- перевір `git diff --stat` та відсутність unrelated changes.
