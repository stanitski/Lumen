# Lumen TODO

## Now

- Lumen nutrition EXE and HA helper wiring deployed 2026-10-08 (`docs/NUTRITION.md`); health/OpenAPI, HA config check, narrow reload and forwarder verified. Still verify one real food photo, portion correction and replies to an older photo/confirmation.

- Packaged first greeting verified after the approved rebuild/start on 2026-08-31: all six mandatory rules and informal Ukrainian. Continue checking packaged semantic fact relevance and monitor `fact_retrieval` fallback and model-change reindexing.
- Monitor automatic standing-rule classification: extraction is model-driven, so a misclassified task-local instruction remains possible. The scoped explicit inclusion update and operator manifest provide correction without changing the fact schema.
- Expand fact-retrieval relevance checks on the real corpus: the multilingual coffee query finds the intended preference but also a temperature device alias at the calibrated threshold. Cosine candidates remain optional context, not guaranteed evidence; use debug match scores to guide further ranking/threshold calibration.

- Rebuild/restart Lumen with Trafilatura and verify `POST /news/process` against one controlled real article and one RSS fallback without changing Home Assistant queues.
- After the packaged endpoint is verified, replace the Home Assistant raw-news Ollama script with the `keep|skip|retry` contract; remove raw items only after `keep` or confirmed `skip`.

- Verify one intentional live calendar-create command through Telegram, including a description and the configured 60-minute default; do not create an automated test event in the user's calendar.
- Verify one live timed calendar event, one all-day event, one update, and one deletion through HA; the approved main and two Birthdays entities are already configured.
- Verify one deployed reminder lifecycle: create/list/update/pause/resume/delete, explicit snooze, 30-minute grace recovery, Windows Task Scheduler wake with the current-user interactive principal, and a structured Home Assistant `reminder_confirmed:<id>:<delivery_id>` callback. Do not alter the existing "пити таблетки" reminder during the migration check.
- Restart the deployed Lumen process from the cleaned source and verify one text message through Home Assistant.
- Send one audio and one video connector message to verify download, `ffmpeg`, faster-whisper, and chat delivery end to end.
- Verify existing explicit Telegram source facts and run one live approved-channel and one approved-chat lookup through `/memory` before relying on Telegram context.
- Add source-management controls to `/memory` for explicit Telegram `ref`, `title`, and `kind` facts.
- Verify a live Telegram request for a full past calendar day and assess whether the 500-message explicit-period scan limit is sufficient.
- Replace each `ContextExecutor` TODO handler only after its read-only adapter and policy checks have focused tests.

## Next

- Migrate FastAPI startup/shutdown decorators to lifespan in a focused change;
  the installed framework emits `on_event` deprecation warnings. Module-level
  initialization remains a separate service-factory concern.

- Run live checks for dialogue retrieval, stable-fact extraction, knowledge upload, and Ollama-compatible endpoints.
- Verify the next deployed artifact after the 2026-10-07 source cleanup, atomic knowledge replacement, configured user scoping and background-task lifecycle changes. Legacy execution models/store APIs/JSON seeds are removed; compatibility naming in `memory.py` remains a separate maintainability question.
- Resolve the `StarletteDeprecationWarning` emitted by `fastapi.testclient`
  against the installed `httpx`; do not install or migrate dependencies without
  a separately scoped compatibility check.

## Later

- Evaluate journal growth/retention before multi-year or multi-worker use: JSON/CSV are rewritten as complete files and the per-user lock covers one process only.

- Reintroduce optional capabilities only as independent, tested integrations.

## Known risks

- Existing runtime process may use pre-cleanup code until restarted.
- Windows wake can start on battery but cannot deliver while the PC is fully powered off; recovery intentionally marks occurrence beyond the configured grace as missed.
- SQLite is local and vector search is in-process; retrieval performance must be rechecked as history grows.

## Do not touch without explicit approval

- Production database and files under `data/`.
- Home Assistant callback contract and `.env` credentials.
- The local backup under `data/backups/`.

## Questions / TBD

- Which optional capability, if any, should be rebuilt first after the clean chat core is proven stable?
- Should module-level application initialization move into a service factory/lifespan in a separately scoped change? API tests now isolate initialization before importing the entrypoint.


## Connector audit follow-ups — 2026-10-18
- STT downloads still buffer the whole response and ffmpeg has no total execution deadline; add configurable byte/time bounds and failure cleanup in a separate focused change.
- Shared routing now classifies ordinary text with conversation context; detailed nutrition analysis runs only for selected nutrition actions. Monitor configured-model routing on short, ambiguous follow-ups across services.
- Verify real Telegram confirmation and reply correction after the 2026-10-18 restart; isolated live-photo/model validation does not exercise outgoing Telegram delivery.

- After shared-context deployment, verify the real Telegram sequence food → daily summary → portion correction → updated summary; isolated configured-model verification has passed.

- External HA callback acceptance and SQLite delivery marking cannot be one transaction: a crash in between can repeat a notification on retry. Stable event_id prevents repeating a committed journal action; receiver-side callback deduplication would be needed for a stronger transport guarantee.

- Fact index concurrency: make the existing `FactContextService.sync_index()` live-hash check and index write atomic. Concurrent admin edits/deletes can currently leave a stale/orphan index row; retrieval rejects it by live hash/record checks, but cleanup should be verified with a race regression test. No schema migration is needed.
