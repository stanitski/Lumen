# Технічний roadmap

Це не продуктова стратегія. Джерела: `TODO_LUMEN.md`, live TODOs, audit
2026-07-23 та current tests.

## 1. Довести deployed baseline

- rebuild/restart current branch;
- перевірити port `8010`, `/`, `/openapi.json`;
- підтвердити primary DB/log paths і schema `v34`;
- порівняти source commit та deployed artifact.

Залежність: усі подальші live checks мають виконуватися на відомому artifact.

## 2. Live integration matrix

Після baseline:

1. text connector callback;
2. `/api/chat` і `/api/generate`, stream/non-stream;
3. reminder lifecycle: management, explicit snooze, grace/missed, wake/delivery/exact confirmation;
4. HA calendar timed/all-day/update/delete;
5. одна навмисна calendar create command;
6. audio/video STT;
7. allowed Telegram channel/chat lookup;
8. dialogue/fact/knowledge retrieval.

Не автоматизувати side effects у user calendar/Telegram без явної тестової
межі.

## 3. Завершити або закрити partial adapters

- вирішити долю `home_assistant/read_state`;
- вирішити, чи потрібен `knowledge/search` через `ContextExecutor`;
- додати Telegram source-management controls;
- кожний adapter: typed contract → access policy → bounded fetch → truthful
  failure → focused tests.

## 4. Legacy cleanup

Окремим reviewed change:

- inventory callers `SkillRecord`/`IntentRecord`/execution/task models;
- inventory legacy `MemoryStore` methods;
- вирішити долю `intents.json`/`system_skills.json`;
- прибрати або ізолювати лише підтверджено невикористане;
- оновити imports/tests/docs.

Залежність: owner approval щодо backward compatibility та scripts/data.

## 5. Maintainability

- вирішити naming `build_planner_context`;
- розглянути service factory/lifespan замість module-level initialization;
- виправити або замінити `run.bat`;
- додати explicit deployment/version diagnostic;
- визначити SQLite growth/retention benchmark.

## Питання власнику

- Merge/base branch для calendar-reminders?
- Canonical deployment command/artifact?
- Чи потрібні generic HA actions або лише вузькі integrations?
- Чи потрібен explicit knowledge context source?
- Коли legacy cleanup дозволений?
- Які volume/SLA/retention очікуються?

## Не підтверджено

- current external service health;
- current process/executable version;
- production Task Scheduler state;
- adequacy 500-message Telegram scan на busy source;
- retrieval performance на майбутньому обсязі.
