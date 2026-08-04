# Поточні архітектурні рішення

Це curated view. Повний хронологічний ledger збережено у root
`DECISIONS.md`. Причини, не підтверджені live-кодом, тут не домислюються.

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
  local SQLite schema `v33`; vector similarity рахує `MemoryStore`.
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
  wake, delivery, recurrence, confirmation та snooze.
- Причина: reminders без повернення planner/skills/tasks.
- Альтернатива: generic task executor/scheduler.
- Наслідки: narrow feature, specialized state machine, schema `v33`.
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

- Рішення: connector/Telegram media транскрибується і стає text input.
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
