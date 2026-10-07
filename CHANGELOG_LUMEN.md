# Lumen Change Log

## 2026-10-07 — Documentation aligned with local implementation

- Виправлено порядок persistence → callback delivery → post-processing та
  опис reminder/calendar handling для транскрибованих вкладень.
- Узгоджено огляди calendar/news, source branch/date і metadata flags, що
  вимикають turn persistence, але не context reads чи side effects.
- Зафіксовано межі image support: adapter serialization є, chat image ingress,
  knowledge OCR/vision, calorie recognition і nutrition journal не реалізовані.
- Verification: static documentation/source consistency and local Markdown
  links; runtime code, configuration, databases and deployed artifact unchanged
  by this documentation update. Live integrations and unit tests not run.

## 2026-09-17 — Temporary news-processing timeout increase

- Тимчасово збільшено загальний deadline одного `POST /news/process` job зі
  180 до 300 секунд, щоб два послідовні structured Ollama calls встигали
  завершитися під поточним навантаженням.
- Зовнішній контракт `keep|skip|retry`, single-job locking та retry semantics
  не змінені. Home Assistant REST timeout узгоджено на 310 секунд.
- Verification: 17 focused news service/API tests passed; `Lumen.exe` rebuilt
  and restarted; `/` and `/openapi.json` return HTTP 200. Home Assistant
  `ha core check` and `rest_command.reload` completed successfully. Previous
  executable: `data/backups/Lumen-before-news-timeout-20260917-084500.exe`;
  live HA configuration backup:
  `/homeassistant/configuration.yaml.backup-20260917-084700`.

## 2026-09-08 — Stable PyInstaller extraction directory

- Fix: the one-file executable no longer extracts its live `_MEI*` runtime into
  the Windows `%TEMP%` directory, where cleanup partially removed files from a
  still-running Lumen process and broke every new `httpx` client with
  `FileNotFoundError` in `ssl.create_default_context()`.
- Packaging: `compile.bat` derives `data\runtime` from its own project directory
  and passes it through `--runtime-tmpdir`; the generated `Lumen.spec` preserves
  the resolved build path. Home Assistant and application service code are
  unchanged.
- Verification: PyInstaller 6.20.0 rebuilt `dist\Lumen.exe`; the new process
  extracted 2,618 files (275.6 MB) under `data\runtime`, exposed OpenAPI over
  HTTP 200, read 38 Telegram messages and returned the air-alert reason through
  inline `/api/chat` in 40.84 seconds. The previous EXE is preserved at
  `E:\LUMEN_BACKUPS\2026-09-08-runtime-tmpdir\Lumen-before-runtime-tmpdir.exe`.

## 2026-09-02 — Telegram channel media isolation

- Fix: an `IndexError` from `faster-whisper`/PyAV while decoding one Telegram
  audio/video attachment no longer aborts the complete Telegram context lookup.
  Lumen keeps the message caption and continues reading the remaining posts.
- Performance: channel searches now use post text/captions without downloading
  and transcribing every audio/video attachment. Media transcription remains
  enabled for allowlisted user and group conversations.
- Scope: `src/lumen/services/telegram_context.py` and focused regression coverage
  in `tests/test_telegram_context.py`; HA routing and speech attachments received
  directly through `/connector` are unchanged.
- Verification: 39 focused Telegram/query/chat tests passed. Rebuilt EXE exposes
  `/connector` through `/openapi.json` HTTP 200, and its packaged
  `lumen.services.telegram_context` matches the fixed source. Previous executable:
  `data/backups/Lumen-before-channel-caption-fix-20260902-102140.exe`.

## 2026-08-31 — Mandatory and contextual fact memory

- Replaced blanket/top-three preference injection with explicit `lumen:mandatory` personal communication rules and separately budgeted semantic/lexical contextual facts for every answer mode.
- Added fact inclusion extraction/update/revocation, scoped keyset paging, existing-schema fact vectors with content/model freshness, transactional invalidation, and observable lexical fallback.
- Distinct communication-rule subjects and extraction identity-conflict checks prevent unrelated standing rules from overwriting each other under `current_user/prefers`.
- Added `scripts/prepare_fact_memory.py`: read-only preview by default, explicit hash-checked policy manifest, SQLite backup, and idempotent index backfill. No embedded user IDs or production fact IDs.
- Debug context exposes mandatory/contextual selections and index status; knowledge listings exclude internal fact vectors. New fact retrieval settings preserve contextual budgets.
- Checkpoint before implementation: `5aed97e`. No schema migration or dependency installation.
- Verified production preparation: six reviewed rules tagged, 37 contextual vectors indexed; repeat sync reports 37 current and zero new/failed vectors. Fact rows differ from the SQLite backup only in the six approved `tags_json` fields. Source-only greeting preview receives all six rules without unrelated contextual facts; no turn persistence or delivery.
- Live local-model checks separate standing/task-local/contextual extraction. Multilingual calibration led to a configurable default threshold of 0.37; vector retrieval can still return extra candidates in the real corpus (tracked in TODO).
- Verification: 92 focused unit tests passed (fact storage/context/runtime, existing chat/query/news/API/context/Telegram); syntax and diff checks passed. External integrations were disabled for API tests.
- Approved deployment: rebuilt with `cmd /c compile.bat` and started `dist/Lumen.exe` on 2026-08-31 at 15:41. Health and `/openapi.json` respond on port 8010. Packaged `/memory/debug-search` greeting includes all six mandatory rules, no contextual facts, and answers using informal Ukrainian; no dialogue persistence or delivery. Previous executable saved as `data/backups/Lumen-before-rebuild-20260831-154034.exe`.

## 2026-08-30

- Fix: відновлено виклик `MemoryService.should_enable_voice_response` у
  `ChatOnlyService` замість постійного `voice=false`. Команда «вголос»/«вслух»
  і чинна збережена голосова перевага доходять до стандартного callback HA.
- Changed files: `src/lumen/services/chat_only.py`,
  `tests/test_chat_only_runtime.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`.
- Verification: regression coverage перевіряє реальну voice policy та
  `DeliveryService` для async/inline, звичайного тексту і `skip_memory`.
  Пройшли 27 tests (`test_chat_only_runtime`, `test_ollama_api`, isolated DB
  та вимкнений calendar listener). Оновлений EXE: `/openapi.json` HTTP 200;
  live `/connector` test отримав callback `voice=true`, HA виконав Talk-2
  і `tts.speak` без помилок. Контрольні суми трьох YAML-файлів HA незмінні.
- Runtime risk: low. HA/Talk-2, Telegram retrieval, маршрутизація та окремі
  reminder/calendar acknowledgements не змінені. Rebuild/restart EXE виконано;
  попередній EXE збережено в `data/backups/voice-fix-20260830-124410/`.

## 2026-08-27

- Type: Terminal rejection of insufficient Feedreader context
- Changed files: `src/lumen/services/news_processing.py`,
  `tests/test_news_processing.py`, news-processing architecture and decision docs.
- Reason: an inaccessible article with too little Feedreader text cannot become a
  trustworthy news brief, so return terminal `skip` instead of retaining it for
  an endless retry cycle in Home Assistant.
- Runtime risk: low. Valid RSS fallback stays available; only
  `insufficient_source_context` becomes terminal. Timeouts, Lumen/model
  failures and HTTP `429` remain retriable.

## 2026-08-22

- Type: Повний reminder lifecycle і усунення зсуву schedule
- Changed files: `src/lumen/services/memory.py`, `src/lumen/services/reminders.py`,
  `src/lumen/storage/db.py`, `src/lumen/config.py`, `src/lumen/main.py`,
  `src/lumen/reminder_wakeup.py`, `src/lumen/services/reminder_wake.py`, focused
  tests, `.env.example` і current runtime docs.
- Reason: GetContract тепер описує typed `update`, `pause`, `resume`, soft
  `delete`, explicit `snooze`, persisted selection і bulk confirmation; actual
  lookup/validation/mutation лишається в `ReminderService`. Schema `v34` додає
  `next_attempt_at` для transport retry та `reminder_management_sessions` із
  user-/route-scoped token і expected revision. Missed occurrence не
  auto-snooze: після configurable 30-minute grace one-shot має `missed`, а
  recurring root повертається до next canonical Kyiv slot. Retry не змінює
  `due_at`; snooze є лише явним і за замовчуванням становить 15 хв.
- Runtime risk: medium. Потрібні rebuild/restart для schema `v34` і нових
  settings. Windows task тепер дозволяє wake/start на батареї, але не може
  доставити reminder за повністю вимкненого ПК; live deployment verification
  не виконувалась у межах цієї зміни.
- Fix: contract-driven `delete + selector.scope=all` більше не відкидається
  через відсутність `id/query`; bulk confirmation показує точні stable `#ID`
  і лише після token confirmation виконує soft delete. Phrase-specific parsing
  або hardcoded українські команди не додавались.

## 2026-08-18

- Type: progress-based Ollama streaming watchdog
- Changed files: Ollama connector, chat orchestration, Ollama-compatible API,
  configuration, focused tests, and runtime documentation.
- Reason: preserve slow but live final generations beyond the old total timeout
  while detecting a genuine 240-second stall after observable stream progress.
- Runtime risk: medium. There is intentionally no deadline before the first
  frame; same-user requests are not cancelled and may finish out of order.

## 2026-08-08

- Type: Ollama inference timeout tuning
- Changed files: `src/lumen/config.py`, `.env.example`, `CHANGELOG_LUMEN.md`.
- Reason: allow long final Lumen responses to finish when the shared GPU is
  temporarily busy on the workstation.
- Runtime risk: medium. A stalled Ollama request can now occupy a chat flow for
  up to four minutes before Lumen returns its fallback answer.

## 2026-08-05

- Type: Feedreader RSS fallback completeness
- Changed files: `src/lumen/services/news_processing.py`, `tests/test_news_processing.py`.
- Reason: include the Feedreader title in fallback source text, so a valid short
  description can still be editorially filtered and converted into a 5W brief
  when its linked article cannot be opened.
- Runtime risk: low. Article extraction remains the preferred source and the
  existing minimum fallback-text threshold remains in force.

## 2026-08-04

- Type: Ізольована обробка Feedreader-новин
- Changed files: `src/lumen/services/news_processing.py`, спільний web extractor,
  memory fact operations, `POST /news/process`, Pydantic contracts, dependency/
  packaging metadata, focused tests, and architecture docs.
- Reason: завантажувати повний текст новини, відкидати лише редакційне сміття
  та явні user-specific `dislikes`, після чого формувати коротку українську
  5W-озвучку для стабільного Home Assistant Todo item.
- Runtime risk: medium. Endpoint виконується синхронно, допускає лише одну
  обробку одночасно та повертає `retry`/`429` без дозволу видаляти raw item.
  Перед HA integration потрібна перевірка rebuilt executable і живого Ollama.

## 2026-07-23

- Type: Token-efficient local AI context
- Changed files: `docs/AI_CONTEXT.md`, `AGENTS.md`,
  `docs/ONBOARDING_AI.md`, `CHANGELOG_LUMEN.md`
- Reason: add a concise, live-code-grounded context document for local agents
  and place it immediately after `AGENTS.md` in the onboarding order.
- Runtime risk: none; documentation-only change.

- Type: Current architecture knowledge-transfer package
- Changed files: `AGENTS.md`, `docs/PROJECT.md`, `docs/ARCHITECTURE.md`,
  `docs/MODULES.md`, `docs/ONBOARDING_AI.md`, `docs/HANDOVER.md`,
  `docs/CONVENTIONS.md`, `docs/DECISIONS.md`, `docs/DEBUGGING.md`,
  `docs/ROADMAP.md`, `README.md`, `PROJECT_MAP.md`, `TODO_LUMEN.md`,
  current contract docs, historical status headers, and
  `docs/env.local.example`
- Reason: document the live `ChatOnlyService` runtime, isolated
  reminder/calendar integrations, persistence/external contracts, confirmed
  legacy residue, debugging path, and current handover without treating old
  planner/skills/tasks material as active.
- Runtime risk: none; documentation-only change.

## 2026-07-22

- Type: Model-driven Google Calendar event creation through Home Assistant
- Changed files: `src/lumen/services/calendar_actions.py`, `src/lumen/services/memory.py`, `src/lumen/services/chat_only.py`, `src/lumen/main.py`, `src/lumen/config.py`, `.env.example`, focused tests, and architecture docs.
- Reason: let an explicit user command create one timed or all-day event in the configured primary Google calendar without availability checks or intermediate persistence. Missing timed end uses the configured 60-minute duration; successful writes refresh the current-day reminder projection.
- Runtime risk: medium. Writes are intentionally non-retrying; timeout returns an unknown result to avoid duplicates. Live verification should use an intentional real event rather than an automated test mutation.

- Type: Home Assistant Google Calendar reminder projection
- Changed files: `src/lumen/services/home_assistant_calendar.py`, `src/lumen/services/reminders.py`, `src/lumen/storage/db.py`, `src/lumen/main.py`, `src/lumen/config.py`, `.env.example`, focused tests, and root architecture docs.
- Reason: read current-day Google Calendar events through Home Assistant REST/WebSocket and idempotently reuse the existing one-time reminder, Telegram confirmation, and 15-minute snooze flow. Timed events notify at their start; all-day events notify at configured 09:00 `Europe/Kyiv`.
- Runtime risk: medium. Deployment requires schema `v33`, explicit calendar entity IDs and Telegram target chat ID, then a rebuilt/restarted runtime.

## 2026-07-21

- Type: Parent/child reminder snooze state machine
- Changed files: `src/lumen/storage/db.py`, `src/lumen/services/reminders.py`, `CHANGELOG_LUMEN.md`, `DECISIONS.md`, `PROJECT_MAP.md`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`.
- Reason: schema `v32` adds `parent_reminder_id`; recurring roots retain their original schedule while silence or `no` creates one inherited-route one-shot child at `+15` minutes. Child confirmation advances the root from its original schedule; list preserves root ordinals while showing the effective child due time.
- Runtime risk: medium. Active child uniqueness and idempotent re-read prevent duplicate snoozes; terminal child delivery failure advances an eligible recurring root or fails a one-time/exhausted root, so no root remains silently `snoozed`.

- Type: Reminder return-route and callback hardening
- Changed files: `src/lumen/storage/db.py`, `src/lumen/services/reminders.py`, `src/lumen/services/chat_only.py`, `src/lumen/services/delivery.py`, `CHANGELOG_LUMEN.md`, `DECISIONS.md`, `PROJECT_MAP.md`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`.
- Reason: persist the allowlisted Telegram return route from connector ingress in `delivery_route_json`, reuse it through `DeliveryService`, generate compact 32-hex correlated delivery IDs, and fail closed when a legacy row has no route.
- Runtime risk: medium. Schema `v31` is applied on Lumen startup; rebuild and restart the packaged runtime before use. Existing route-less rows are not sent to a `user_id` fallback and retry at most with the bounded reminder delivery policy until an operator resolves their destination.

## 2026-07-20

- Type: Reminder noninterference in chat-only flow
- Changed files: `src/lumen/services/chat_only.py`, `tests/test_chat_only_runtime.py`, `CHANGELOG_LUMEN.md`.
- Reason: restore normal planner-context GetContract plus embedding flow for ordinary chat, keep structured reminder confirmations before context build, and remove reminder-specific final-prompt wording.
- Runtime risk: low. Reminder create, list, ordinal cancellation, and delivery state transitions are unchanged.

- Type: Reminder contract action/confirmation separation
- Changed files: `src/lumen/services/memory.py`, `tests/test_query_contract.py`, `CHANGELOG_LUMEN.md`.
- Reason: ignore a model-supplied confirmation when it also returns a reminder create, list, or ordinal-cancel action; clarify the multi-time daily reminder contract.
- Runtime risk: low. Confirmation handling remains unchanged for `action=none`.

- Type: GetContract-driven reminder create/list/cancel
- Changed files: `src/lumen/services/memory.py`, `src/lumen/services/chat_only.py`, `src/lumen/services/reminders.py`, focused tests, `PROJECT_MAP.md`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: one model contract now supplies structured reminder items, list action, displayed ordinal cancellation and plain confirmation; `ReminderService` retains only validation and state transitions. Delete-all support was removed.
- Runtime risk: medium. A missing, stale, or state-changed in-memory list snapshot intentionally refuses ordinal cancellation until the user lists active reminders again.

- Type: Scoped multi-reminder invalid-date guard
- Changed files: `src/lumen/services/reminders.py`, `tests/test_reminders.py`, `CHANGELOG_LUMEN.md`.
- Reason: keep invalid item dates inside indexed all-or-nothing validation when a request also has a scoped date.
- Runtime risk: low. The guard does not alter valid scoped inheritance or transaction behavior.

- Type: Multi-reminder parser and active-list corrective pass
- Changed files: `src/lumen/services/reminders.py`, `tests/test_reminders.py`, `CHANGELOG_LUMEN.md`.
- Reason: preserve explicit scoped date/recurrence overrides, validate invalid repeat-until dates without partial writes, distinguish list requests from action wording, and bound active-list output truthfully.
- Runtime risk: low. Existing one-reminder, transaction, and wake semantics are unchanged.

- Type: Atomic multi-reminder creation and active reminder list
- Changed files: `src/lumen/services/reminders.py`, `tests/test_reminders.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: create up to ten validated reminders from one request without partial writes, preserve shared actions with multiple times, and answer reminder-list requests from live SQLite rows rather than dialogue context.
- Runtime risk: low. Unsupported or ambiguous multi-item wording now truthfully creates no rows and asks for clarification.

- Type: Wake task PowerShell argument binding repair
- Changed files: `src/lumen/services/reminder_wake.py`, `tests/test_reminders.py`, `CHANGELOG_LUMEN.md`.
- Reason: encode a fully quoted PowerShell script instead of relying on `$args` after `-Command`, which does not receive subprocess-list values on this runtime.
- Runtime risk: low. The registered task, principal, `WakeToRun`, `StartWhenAvailable`, and source/frozen working-directory behavior are unchanged.

- Type: Packaged Kyiv timezone fallback
- Changed files: `src/lumen/timezones.py`, `src/lumen/data/zoneinfo/Europe/Kyiv`, `src/lumen/services/reminders.py`, `src/lumen/storage/db.py`, `src/lumen/main.py`, `tests/test_timezones.py`, `CHANGELOG_LUMEN.md`.
- Reason: bundle the IANA `Europe/Kyiv` TZif entry so source and PyInstaller one-file runtimes retain DST-aware reminder scheduling without an installed `tzdata` package.
- Runtime risk: low. The bundled file is a single local IANA timezone asset; source runtime still prefers the operating-system timezone database.

- Type: Reminder corrective pass
- Changed files: local ignored `_ha_live_automations.yaml`, `src/lumen/main.py`, `src/lumen/services/reminders.py`, `src/lumen/services/reminder_wake.py`, `src/lumen/storage/db.py`, `src/lumen/memory/store.py`, `tests/test_reminders.py`, `tests/test_ollama_api.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `docs/LUMEN_ARCHITECTURE_GUIDE_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: correlate Telegram confirmation callbacks with `delivery_id`, align silence retry timing, isolate legacy store APIs, migrate compatible legacy rows, and avoid startup callback blocking.
- Runtime risk: medium. Deployment must validate the current-user Windows task principal and apply the Home Assistant automation change before callbacks can confirm reminders.

- Type: Isolated reminder service
- Changed files: `src/lumen/storage/db.py`, `src/lumen/services/reminders.py`, `src/lumen/services/reminder_wake.py`, `src/lumen/reminder_wakeup.py`, `src/lumen/services/chat_only.py`, `src/lumen/main.py`, `tests/test_reminders.py`, `README.md`, `PROJECT_MAP.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: restore persistent one-time/daily/weekly reminders with one Windows wake task, Home Assistant delivery, correlated confirmation, bounded delivery retries, and no planner/skills/tasks runtime.
- Runtime risk: medium. The first deployment must verify Windows Task Scheduler permissions and the Home Assistant connector's structured `reminder_id`/`delivery_id` callback payload.

## 2026-07-20

- Type: Documentation sync for current chat-only runtime
- Changed files: `README.md`, `PROJECT_MAP.md`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `docs/LUMEN_ARCHITECTURE_GUIDE_UA.md`, `docs/TARGET_EXECUTION_CONTRACT_UA.md`, `docs/TARGET_EXECUTION_IMPLEMENTATION_PROMPTS_UA.md`, `DECISIONS.md`, `TODO_LUMEN.md`, `CHANGELOG_LUMEN.md`.
- Reason: removed stale planner/skills/tasks/reminder architecture wording from current docs, aligned the documented source of truth with the live `ChatOnlyService` runtime, and marked old execution-contract docs as historical.
- Runtime risk: none, because no application code was changed.

## 2026-07-19

- Type: Bounded DuckDuckGo web context
- Changed files: `src/lumen/services/web_search.py`, `src/lumen/services/context_executor.py`, `src/lumen/services/chat_only.py`, `src/lumen/main.py`, `tests/test_context_executor.py`, `PROJECT_MAP.md`, `CHANGELOG_LUMEN.md`.
- Reason: execute validated `web/search` contract requests through a read-only DuckDuckGo adapter and provide bounded, URL-cited context before final response generation.
- Runtime risk: medium. DuckDuckGo HTML results may be rate-limited, blocked, or change markup; failures remain truthful unavailable context. The fixed initial policy is five results, two fetched pages, and a 3,000-character prompt budget.

## 2026-07-17

- Type: Explicit Telegram date ranges
- Changed files: `src/lumen/services/memory.py`, `src/lumen/services/telegram_context.py`, `tests/test_query_contract.py`, `tests/test_telegram_context.py`, `PROJECT_MAP.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: make requests such as "from the 15th" retrieve the requested calendar day instead of being cut off by the default 48-hour dialog window.
- Runtime risk: low to medium. Explicit periods scan up to 500 messages per source; very high-volume histories may need pagination later.

- Type: Typed Telegram source selection
- Changed files: `src/lumen/services/memory.py`, `src/lumen/services/telegram_context.py`, `src/lumen/services/context_executor.py`, `tests/test_query_contract.py`, `tests/test_telegram_context.py`, `PROJECT_MAP.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: distinguish private/group correspondence from news channels using typed Telegram source facts and avoid broad reads when no source type matches.
- Runtime risk: low. Missing source type or matching fact now asks for clarification instead of reading every configured Telegram source.

- Type: Fact-backed Telegram source catalog
- Changed files: `.env.example`, `src/lumen/config.py`, `src/lumen/services/telegram_context.py`, `src/lumen/main.py`, `tests/test_telegram_context.py`, `PROJECT_MAP.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: derive permitted Telegram channel/chat refs from explicit Lumen memory facts instead of a parallel environment allowlist.
- Runtime risk: low to medium. Only facts with `category=messenger_context`, `predicate=uses`, and tags `telegram` plus `search_source` authorize reads.

- Type: Read-only Telegram context adapter
- Changed files: `.env.example`, `src/lumen/config.py`, `src/lumen/services/telegram_context.py`, `src/lumen/services/context_executor.py`, `src/lumen/services/chat_only.py`, `src/lumen/main.py`, `tests/test_telegram_context.py`, `tests/test_context_executor.py`, `tests/test_chat_only_runtime.py`, `PROJECT_MAP.md`, `DECISIONS.md`, `TODO_LUMEN.md`, `CHANGELOG_LUMEN.md`.
- Reason: restore bounded Telegram channel/chat reads through the new context contract without restoring planner steps, fact storage, or a second Ollama summary call.
- Runtime risk: medium. It needs a configured authorized local Telethon session and explicit `TELEGRAM_ALLOWED_SOURCES`; the first live lookup must verify API access and context quality.

- Type: External context executor foundation
- Changed files: `src/lumen/services/context_executor.py`, `src/lumen/services/chat_only.py`, `src/lumen/main.py`, `tests/test_context_executor.py`, `tests/test_chat_only_runtime.py`, `PROJECT_MAP.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: dispatch validated external-context requests before final answer generation while keeping Telegram, web, Home Assistant, and knowledge actions as explicit TODO stubs.
- Runtime risk: low. Requested external context currently produces a truthful unavailable marker and does not make network calls.

- Type: External context contract foundation
- Changed files: `src/lumen/services/memory.py`, `tests/test_query_contract.py`, `PROJECT_MAP.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: add validated contract requests for Telegram, web, Home Assistant, and uploaded knowledge before implementing any external adapter.
- Runtime risk: low. The new field is diagnostic and does not execute an integration or change existing retrieval.

- Type: Speech-to-text connector ingress
- Changed files: `src/lumen/config.py`, `src/lumen/main.py`, `src/lumen/services/speech_to_text.py`, `tests/test_speech_to_text.py`, `AGENTS.md`, `PROJECT_MAP.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`.
- Reason: restore audio/video transcription as a narrow pre-chat step without restoring the deleted planner, skills, tasks, or attachment stack.
- Runtime risk: medium, because connector attachment URLs, `ffmpeg`, and local faster-whisper model/device availability must be verified with a live message.

- Type: Clean chat runtime
- Changed files: `src/lumen/main.py`, `src/lumen/config.py`, `src/lumen/services/chat_only.py`, `src/lumen/storage/db.py`, deleted planner/skills/tasks/reminder/smart-home/STT services and obsolete tests, `PROJECT_MAP.md`, `TODO_LUMEN.md`, `DECISIONS.md`.
- Reason: replace the coupled multi-service architecture with chat, memory, knowledge, Ollama-compatible API, and callback delivery only.
- Runtime risk: medium until the running process is restarted and live Ollama/Home Assistant checks pass. A pre-clean SQLite backup exists under `data/backups/`.

- Type: Topic-first dialogue retrieval experiment
- Changed files: `src/lumen/storage/db.py`, `src/lumen/models.py`, `src/lumen/memory/store.py`, `src/lumen/services/memory.py`, `src/lumen/main.py`, `scripts/backfill_turn_topics.py`, `tests/test_turn_metadata.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`
- Reason: add Ollama-classified `primary_topic` metadata plus topic embeddings to turns, use nearest topic vectors instead of exact labels, rank candidates without a global similarity gate, and provide a bounded one-day topic backfill script.
- Runtime risk: medium, because deep chat requests add a topic-classification and topic-embedding call; semantically close but distinct topics can still share candidates, while untagged history uses the existing fallback.

- Follow-up: removed nutrition-specific classifier instructions, examples, and keyword fallback; topic classification remains model-driven without domain hardcoding.

- Follow-up: memory-manager debug results now display the query topic returned by Ollama next to the prompt metrics.

- Follow-up: introduced clean `GetTopic(text)` and `GetEmbedding(text)` memory primitives. Topic classification explicitly excludes runtime context; all new turn query/question/answer embeddings use the shared validated embedding path.

- Follow-up: deep requests now use `GetContract(text)` to classify topic, answer mode, time scope, and completeness in one clean call. The memory-manager debug view displays this contract, and complete record-list requests instruct Ollama not to omit confirmed dialogue records.

- Verified: the live query `"Що я їв сьогодні?"` returned `food/list_records/today/complete`, included turns `930`, `931`, `933`, `934`, and `936`, and produced a complete prior-day record list while explicitly noting the stored `931` conflict.

- Follow-up: expanded `scripts/backfill_turn_topics.py` from a one-day helper into a rerunnable full-history topic-index backfill with `--all`, `--limit`, and `--dry-run`; already indexed turns are skipped through `MemoryStore`, while valid blank topics are retried.

- Follow-up: `GetContract` now disables Ollama reasoning tokens, keeping topic/contract classification bounded for normal requests and full-history indexing.

- Follow-up: added deterministic `get_retrieval_policy`, which applies the query contract to memory retrieval without another Ollama call and prevents small talk or record lists from adding inappropriate long-term context.

- Follow-up: topic, Q/A semantic, and knowledge candidate reads now run concurrently; existing topic-first dialogue selection remains unchanged pending a separate candidate-union and rank-fusion decision.

- Follow-up: memory debug now shows the effective retrieval policy plus every fact and dialogue turn included in the prompt, with fact/dialogue/continuity weight types, descending weights, and duplicate prompt-occurrence markers.

- Follow-up: replaced topic-first dialogue gating with a deduplicated union of Q/A semantic, nearest-topic, and exact quoted/entity candidates. A single bounded rank now applies semantic relevance, topic bonus, exact-match bonus, and recency; memory debug displays lexical-match evidence on selected dialogue cards.

- Follow-up: documented the remaining `max(question_embedding, answer_embedding)` ranking risk discovered with title retrieval; no Q/A weighting change was made in this iteration.

## 2026-07-16

- Type: Memory retrieval debug preview
- Changed files: `src/lumen/main.py`, `src/lumen/services/chat_only.py`, `src/lumen/services/memory.py`, `tests/test_chat_only_runtime.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`
- Reason: make `/memory` Search run the normal chat-only context builder, display the exact selected facts and dialogue turns with semantic scores, and show a non-persisting Ollama preview answer.
- Runtime risk: low, because the new endpoint does not write memory or send a delivery callback; it does make one embedding and one dialogue-model request per debug search.

- Follow-up: debug dialogue cards now show the final retrieval weight (semantic score plus the time-decay bonus) and are ordered by that weight descending.

- Type: Dialogue history rebuild with split embeddings
- Changed files: `src/lumen/storage/db.py`, `src/lumen/models.py`, `src/lumen/memory/store.py`, `src/lumen/services/memory.py`, `scripts/rebuild_dialogues_from_telegram.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`
- Reason: preserve profile facts, replace legacy dialogue vectors with separate question and answer embeddings in `conversation_turns`, and rebuild 942 completed dialogue pairs from a local Telegram Desktop JSON export.
- Runtime risk: medium, because the local runtime tables were rebuilt; a verified pre-rebuild SQLite backup is retained under `data/backups/` and Lumen remains stopped pending a live smoke test.

- Type: Immediate dialogue continuity anchor
- Changed files: `src/lumen/services/chat_only.py`, `tests/test_chat_only_runtime.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`
- Reason: always add one preceding persisted user/assistant turn before the current chat request so short follow-ups retain their direct reference without heuristic intent detection; semantic retrieval remains active and may duplicate that turn.
- Runtime risk: low, because the prompt adds at most one bounded stored turn and retains current-message priority.

- Type: Time-decayed dialogue retrieval
- Changed files: `src/lumen/memory/store.py`, `src/lumen/services/memory.py`, `src/lumen/services/chat_only.py`, `tests/test_chat_only_runtime.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `DECISIONS.md`, `CHANGELOG_LUMEN.md`
- Reason: retrieve dialogue from all stored embeddings using semantic relevance plus a 72-hour time-decay bonus, remove unconditional recent-turn injection, and restore read-only knowledge retrieval to the chat-only prompt.
- Runtime risk: low to medium, because context selection changes can alter answer grounding; weak semantic matches remain excluded and prompt budgets remain bounded.

- Follow-up: chat-only lazy recovery now retries up to 10 pending dialogue embeddings per user request, covering the current unindexed turn backlog without a sleep scheduler.

- Type: Chat-only stabilization mode
- Changed files: `src/lumen/config.py`, `src/lumen/memory/store.py`, `src/lumen/services/chat_only.py`, `src/lumen/services/memory.py`, `src/lumen/main.py`, `tests/test_chat_only_runtime.py`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, `PROJECT_MAP.md`, `DECISIONS.md`, `TODO_LUMEN.md`, `CHANGELOG_LUMEN.md`
- Reason: temporarily disable the coupled automation stack without deleting code or data, leaving only direct dialogue, profile/dialogue memory, immediate post-delivery embeddings, and answer delivery active by default.
- Runtime risk: medium, because the default runtime intentionally rejects optional feature requests; `LUMEN_RUNTIME_MODE=full` preserves the prior wiring for a controlled rollback.

## 2026-07-12

- Type: Independent chat stage outcomes
- Changed files: `src/lumen/models.py`, `src/lumen/services/chat.py`, `tests/test_pending_entity_clarification.py`, `CHANGELOG_LUMEN.md`
- Reason: expose execution, persistence, and delivery outcomes independently, continue delivery after persistence failure without repeating execution, and retain backward-compatible answer/delivered fields
- Runtime risk: low to medium, because persistence failures now degrade explicitly instead of aborting delivery; no schema, endpoint adapter, or side-effect retry behavior changed

- Type: Final shared chat delivery ownership
- Changed files: `src/lumen/services/plan_execution.py`, `src/lumen/services/chat.py`, `src/lumen/main.py`, `tests/test_pending_entity_clarification.py`, `CHANGELOG_LUMEN.md`
- Reason: remove `DeliveryService` from plan execution, make `ChatService` the single owner of general chat callbacks after conversation persistence, and expose transport errors separately from execution outcome
- Runtime risk: medium, because shared chat callback timing now follows the persistence checkpoint; inline responses and domain-owned delivery paths remain unchanged

- Type: Chat delivery ownership extraction from executor
- Changed files: `src/lumen/tasks/executor.py`, `src/lumen/services/plan_execution.py`, `tests/test_pending_entity_clarification.py`, `CHANGELOG_LUMEN.md`
- Reason: let shared chat plans disable step-level callbacks and use `PlanExecutionService` as the temporary compatibility delivery owner, while preserving direct scheduled-task delivery, channel suppression, and separation of transport failure from execution outcome
- Runtime risk: medium, because shared chat responses now rely on plan-level fallback delivery; direct scheduled tasks, knowledge-scope, and legacy confirmation flows temporarily retain executor-owned delivery

- Type: Shared chat policy gate wiring
- Changed files: `src/lumen/tasks/executor.py`, `src/lumen/services/plan_execution.py`, `tests/test_policy_engine.py`, `CHANGELOG_LUMEN.md`
- Reason: enforce policy before shared chat plan steps, persist confirmation-required actions as durable pending interactions, and require action-bound challenge evidence before execution resumes
- Runtime risk: medium, because sensitive shared-chat actions now pause for explicit confirmation; scheduled task execution remains a documented follow-up

- Follow-up: nested `skill` execution now inherits shared-chat policy enforcement so wrapper skills cannot bypass the gate; `run_steps` remains covered by recursive capability inspection of its nested steps

- Type: Shared policy decision foundation
- Changed files: `src/lumen/services/policy.py`, `tests/test_policy_engine.py`, `CHANGELOG_LUMEN.md`
- Reason: add a transport-neutral `PolicyEngine` that returns explicit `allow`, `confirm`, or `deny`, infers sensitive capabilities from steps, and accepts only action-bound structured confirmation evidence
- Runtime risk: none expected in this slice, because the policy engine is not wired into runtime execution yet

- Type: Execution contract model foundation
- Changed files: `src/lumen/models.py`, `tests/test_execution_contract_models.py`, `CHANGELOG_LUMEN.md`
- Reason: add transport-neutral `ExecutionPlan`, `PolicyDecision`, `ExecutionResult`, and `DeliveryEnvelope` models as a compatibility-only foundation for the target execution architecture
- Runtime risk: none expected, because the new models are not wired into runtime, endpoint payloads, persistence, or delivery

- Type: Target execution implementation prompts
- Changed files: `docs/TARGET_EXECUTION_IMPLEMENTATION_PROMPTS_UA.md`, `PROJECT_MAP.md`, `CHANGELOG_LUMEN.md`
- Reason: provide sequential, scope-controlled Codex prompts for implementing and verifying the target execution contract without mixing policy, delivery, persistence, and reminder/task migrations in one change
- Runtime risk: none, because no application code, configuration, schema, or runtime data was changed

- Type: Target execution architecture documentation
- Changed files: `docs/TARGET_EXECUTION_CONTRACT_UA.md`, `PROJECT_MAP.md`, `CHANGELOG_LUMEN.md`
- Reason: define the target `Plan -> Policy -> Execute -> Persist -> Deliver` contract and exact ownership boundaries between `PlanExecutionService`, `ActionExecutor`, reminders, and tasks while recording current implementation gaps
- Runtime risk: none, because no application code, configuration, schema, or runtime data was changed

## 2026-07-11

- Type: Knowledge-context budget and dialogue timeout fallback
- Changed files: `src/lumen/services/memory.py`, `src/lumen/tasks/executor.py`, `tests/test_turn_metadata.py`, `CHANGELOG_LUMEN.md`
- Reason: filter and bound knowledge chunks before they reach `general_response`, log knowledge-context sizes, and retry one timed-out dialogue response with only the current message and two recent turns
- Runtime risk: low to medium, because weak or oversized knowledge context is intentionally omitted and retry answers have no long-term context

- Type: Memory-context relevance and prompt-size control
- Changed files: `src/lumen/services/memory.py`, `tests/test_turn_metadata.py`, `CHANGELOG_LUMEN.md`
- Reason: select only request-relevant facts with bounded essential preferences, cap formatted fact and topic context without partial structured values, reject weak dialogue embedding hits, and limit topic expansion to explicit topic/follow-up signals
- Runtime risk: low to medium, because unrelated memories are intentionally no longer injected and weak semantic matches no longer provide dialogue context

## 2026-07-10

- Type: Nutrition activity tracking fix
- Changed files: `src/lumen/data/system_skills.json`, `src/lumen/tasks/executor.py`, `tests/test_pending_entity_clarification.py`, `LUMEN_NUTRITION_ACTIVITY_HANDOFF.md`, `CHANGELOG_LUMEN.md`, `TODO_LUMEN.md`
- Reason: store workouts with explicit burned calories as separate dated activity facts, aggregate them in the daily nutrition summary, and resolve the steps sensor from its memory alias before reading its live Home Assistant state
- Runtime risk: low to medium, because nutrition logs now rely on the model to distinguish explicit activity calories from food calories and on a valid Home Assistant sensor alias

- Type: Planner timeout resilience fix
- Changed files: `src/lumen/skills/planner.py`, `tests/test_pending_entity_clarification.py`, `CHANGELOG_LUMEN.md`
- Reason: planner timeout paths now degrade to intent/tag-based or deterministic fallback planning instead of bubbling `httpx`/`httpcore` read timeouts up to `ChatService` as `Не вдалося спланувати відповідь.`
- Runtime risk: low, because the patch is limited to planner timeout handling and keeps existing fallback selection behavior

- Type: Owner-facing debug guide
- Changed files: `LUMEN_DEBUG_GUIDE.md`, `CHANGELOG_LUMEN.md`
- Reason: added a plain-Ukrainian guide for diagnosing wrong Lumen answers without deep backend reading, with symptom-based prompts for Codex and a separate appendix for technical file names
- Runtime risk: none, because no application code was changed

- Type: Debugging documentation
- Changed files: `LUMEN_DEBUGGING_PLAYBOOK.md`, `CHANGELOG_LUMEN.md`, `TODO_LUMEN.md`
- Reason: added a practical playbook for diagnosing wrong answers, wrong skill selection, noisy memory retrieval, prompt issues, tool failures, and fallback behavior without changing application code
- Runtime risk: none, because no application code was changed

- Type: Architecture guide for project owner
- Changed files: `docs/LUMEN_ARCHITECTURE_GUIDE_UA.md`, `CHANGELOG_LUMEN.md`
- Reason: added a practical Ukrainian architecture guide that maps core modules, flows, terminology, debugging entry points, and safer Codex task phrasing for Lumen ownership work
- Runtime risk: none, because no application code was changed

- Type: Chat fallback reliability fix
- Changed files: `src/lumen/tasks/executor.py`, `src/lumen/services/plan_execution.py`, `src/lumen/services/chat.py`, `tests/test_pending_entity_clarification.py`
- Reason: preserved conversational text when callback delivery fails, converted blank dialogue-generation failures into an explicit error instead of the generic ack fallback, and added focused regression coverage for timeout and delivery-failure chat paths
- Runtime risk: low to medium, because the change stays inside the shared conversational response path but affects all ingress routes that use `general_response`

- Type: Documentation archival cleanup
- Changed files: `archive/old-docs/Home.md`, `archive/old-docs/HOME_ASSISTANT_SSH_ACCESS.md`, `AGENTS.md`, `PROJECT_MAP.md`
- Reason: moved outdated root-level docs into archive and clarified that archived docs are historical only
- Runtime risk: none, because no application code was changed

## 2026-07-08

- Type: Planner and skills follow-up
- Changed files: `src/lumen/skills/planner.py`, nutrition-summary skill/runtime files, related tests
- Reason: fixed close-match planner clarification behavior and restored `nutrition_summary` to the live sensor/memory contract
- Runtime risk: low, but packaging still had a separate locked-`dist\Lumen.exe` issue

## 2026-07-05

- Type: Reminder confirmation and STT runtime update
- Changed files: reminder response path, related reminder tests, `.env`, `src/lumen/services/speech_to_text.py`
- Reason: reminder confirmations now include due time plus reminder text, and STT was verified on CUDA through env-driven settings
- Runtime risk: medium for operators, because live processes still require restart after `.env` or reminder-service changes

## 2026-07-04

- Type: Runtime policy and memory contract hardening
- Changed files: shared runtime/policy layers, schema-backed turn/memory paths, docs, regression tests
- Reason: moved `PolicyEngine` from trace-only to enforcement, added goal/capability gating, and advanced the schema-backed turn-history contract
- Runtime risk: medium, because policy/schema changes affect shared behavior across ingress paths

## 2026-07-01

- Type: Chat ingress unification
- Changed files: `src/lumen/main.py`, `src/lumen/services/chat.py`, connector intake/adapter paths, docs, tests
- Reason: aligned native `/chat` and Ollama-compatible `/api/chat` and `/api/generate` over one `ChatService` core and removed leftover dead adapter wiring
- Runtime risk: medium, because all chat ingress paths now depend on the same shared orchestration contract

## 2026-07-01

- Type: Turn metadata and reminder routing contract
- Changed files: turn persistence/migration path, reminder routing path, related tests/docs
- Reason: added durable `turn_meta` threading and moved reminder destination defaults to Home Assistant instead of Lumen-side Telegram inference
- Runtime risk: medium, because chat history persistence and reminder delivery contracts changed together

## 2026-06-26 to 2026-06-30

- Type: Reminder reliability and live recovery
- Changed files: `src/lumen/services/reminders.py`, reminder delivery path, local reminder rows, related tests
- Reason: added auto-snooze for unconfirmed reminders, repaired stalled live reminders, and restored callback metadata needed for Telegram delivery through Home Assistant
- Runtime risk: medium, because reminder behavior spans scheduler logic, SQLite state, and Home Assistant callback handling

## 2026-07-10

- Type: Documentation setup
- Changed files: `AGENTS.md`, `PROJECT_MAP.md`, `CHANGELOG_LUMEN.md`, `DECISIONS.md`, `TODO_LUMEN.md`
- Reason: improve project understanding, reduce Codex token waste, and make future changes trackable
- Runtime risk: none, because no application code was changed

- Type: Documentation maintenance
- Changed files: `AGENTS.md`, `PROJECT_MAP.md`, `CHANGELOG_LUMEN.md`, `TODO_LUMEN.md`
- Reason: tighten guidance for docs-only tasks, clarify that `PROJECT_MAP.md` is an orientation aid, and remove low-value placeholder TODO sections
- Runtime risk: none, because no application code was changed

## 2026-07-19

- Type: Packaged runtime startup and database-path repair
- Changed files: `src/lumen/main.py`, `src/lumen/config.py`, `compile.bat`
- Reason: restore the Uvicorn entrypoint for `Lumen.exe`; resolve `.env`, SQLite, debug-file, and Telegram-session paths from the project root; keep `data/` out of the PyInstaller bundle so the runtime uses the primary database instead of a bundled copy.
- Runtime risk: low, because only executable startup and relative-path resolution changed.

- Type: Telegram source-aware contract routing
- Changed files: `src/lumen/services/memory.py`, `tests/test_query_contract.py`, `PROJECT_MAP.md`
- Reason: provide `GetContract` with approved Telegram source metadata so the model can select read-only Telegram context without treating transport sources as ordinary response memory.
- Runtime risk: low, because the catalog contains only approved source metadata and the existing executor continues to enforce source policy.

- Type: Ollama-compatible streaming response repair
- Changed files: `src/lumen/main.py`, `tests/test_ollama_api.py`
- Reason: serialize `/api/chat` and `/api/generate` stream events as valid JSON Lines so Home Assistant's Ollama client can parse the response and complete Assist delivery.
- Runtime risk: low, because only the wire serialization of existing stream payloads changed.

- Type: Ollama GPU residency tuning
- Changed files: `src/lumen/config.py`, `.env.example`
- Reason: align the Lumen default and example keep-alive duration with the active five-minute policy, allowing inactive models to release VRAM sooner.
- Runtime risk: low, because only cold-start latency after five idle minutes can increase.
