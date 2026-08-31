# Lumen TODO

## Now

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

- Run live checks for dialogue retrieval, stable-fact extraction, knowledge upload, and Ollama-compatible endpoints.
- Simplify remaining legacy-only code inside `models.py`, `memory/store.py`, and `memory.py` in a separately reviewed pass.
- Resolve the `StarletteDeprecationWarning` emitted by `fastapi.testclient`
  against the installed `httpx`; do not install or migrate dependencies without
  a separately scoped compatibility check.

## Later

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
- If reminders return, should they be rebuilt as a narrow isolated integration instead of reviving the removed planner/skills/tasks stack?
