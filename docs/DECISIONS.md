# Поточні архітектурні рішення

Це curated view. Повний хронологічний ledger збережено у root
`DECISIONS.md`. Причини, не підтверджені live-кодом, тут не домислюються.

## ADR-000: Ollama streaming watchdog for shared GPU

- Decision: final dialogue uses Ollama NDJSON streaming without a total or
  first-frame deadline. After the first valid frame,
  `OLLAMA_STREAM_IDLE_TIMEOUT_SECONDS=240` limits only silence between frames.
  Buffered contract/fact/embed calls retain `OLLAMA_TIMEOUT_SECONDS=240`.
- Reason: prompt evaluation can exceed four minutes while Ollama remains live;
  standard Ollama streaming exposes progress only after its first frame.
- Consequences: idle partial text is discarded without persistence or callback;
  inline endpoints return `504`. New messages do not cancel older requests,
  and `num_predict` remains unrestricted.
- Status: active.

## ADR-001: chat-memory-knowledge core

- Рішення: `ChatOnlyService` є єдиним chat orchestrator.
- Причина: root decision від 2026-07-17 фіксує відмову від coupled automation
  stack; live wiring це підтверджує.
- Альтернатива: planner/skills/tasks runtime.
- Наслідки: optional capabilities мають бути ізольованими integrations.
- Статус: active.

## ADR-002: один semantic core для ingress

- Рішення: `/chat`, `/connector`, `/api/chat`, `/api/generate` використовують
  `ChatOnlyService`.
- Причина: parity між callback та inline paths.
- Альтернатива: окремий dialogue engine на ingress.
- Наслідки: adapters відрізняються transport/delivery, не semantics.
- Статус: active.

## ADR-003: SQLite-backed memory і knowledge

- Рішення: facts, turns, knowledge embeddings і reminders зберігаються у
  local SQLite schema `v34`; vector similarity рахує `MemoryStore`.
- Причина: потребує підтвердження за межами наявного ledger; реалізація
  підтверджена.
- Альтернатива: зовнішня vector DB.
- Наслідки: простий local deployment, але scaling risk.
- Статус: active.

## ADR-004: bounded read-only external context

- Рішення: `ContextExecutor` allowlist-ить source/operation і додає лише
  temporary prompt context.
- Причина: не відновлювати coupled execution architecture.
- Альтернатива: direct adapter calls або imported external history.
- Наслідки: Telegram/web активні; HA/knowledge contract handlers pending.
- Статус: active/partial.

## ADR-005: reminders як isolated state machine

- Рішення: `ReminderService` окремо володіє validation, SQLite transitions,
  wake, delivery, recurrence, confirmation, management sessions і explicit snooze.
- Причина: reminders без повернення planner/skills/tasks.
- Альтернатива: generic task executor/scheduler.
- Наслідки: specialized state machine, schema `v34`, soft delete, pause/resume,
  30-minute missed grace та restart-safe ambiguous/bulk selection.
- Статус: active.

## ADR-006: fail-closed reminder routing

- Рішення: persist only normalized Telegram return route; не виводити target з
  `user_id`; confirmation потребує exact `delivery_id`.
- Причина: уникнути доставки не тому адресату.
- Альтернатива: user-to-chat inference/backfill.
- Наслідки: legacy route-less row не доставляється.
- Статус: active.

## ADR-007: calendar через Home Assistant

- Рішення: читати Google Calendar через HA current-day REST snapshot, WebSocket
  використовувати як invalidation, occurrences проєктувати у reminders.
- Причина: Google лишається source of truth, а reminder delivery reused.
- Альтернатива: direct Google client, poll loop, parallel calendar table.
- Наслідки: only current Kyiv day; stable `uid` потрібен для projection.
- Статус: active.

## ADR-008: calendar write без automatic retry

- Рішення: один typed HA create call у fixed configured entity; timeout →
  `unknown`.
- Причина: retry може створити duplicate.
- Альтернатива: automatic retry/conflict check/intermediate DB draft.
- Наслідки: manual inspection after uncertain result.
- Статус: active.

## ADR-009: speech re-enters text flow

- Рішення: STT-supported connector media транскрибується і стає text input.
  Telegram media STT застосовується до allowlisted user/group sources;
  channel sources використовують лише text/caption без media download/STT.
- Причина: спільна memory/orchestration/delivery поведінка.
- Альтернатива: multimodal/attachment execution engine.
- Наслідки: потрібні `ffmpeg` і `faster-whisper`.
- Статус: active.

## ADR-010: historical planner docs не є source of truth

- Рішення: `docs/TARGET_EXECUTION_*.md` і старі playbooks збережено тільки з
  historical status.
- Причина: current code не має відповідних modules/wiring.
- Альтернатива: видалення без audit.
- Наслідки: historical evidence доступне, але onboarding веде до live docs.
- Статус: active.

## ADR-011: news processing як isolated synchronous integration

- Рішення: `POST /news/process` використовує окремий `NewsProcessingService`,
  shared `WebPageExtractor`, exact user `news_context` dislikes і два bounded
  Ollama calls; одночасно виконується один job.
- Причина: не переносити article retrieval/editorial policy у Home Assistant і
  не повертати generic planner/action architecture.
- Альтернатива: Ollama prompt без article body у HA automation або окремі
  extractor integrations.
- Наслідки: `keep|skip` terminal, `retry`/`429` зберігають raw item; likes і
  нові config values не потрібні.
- Статус: active.
