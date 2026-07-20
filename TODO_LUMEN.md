# Lumen TODO

## Now

- Restart the deployed Lumen process from the cleaned source and verify one text message through Home Assistant.
- Send one audio and one video connector message to verify download, `ffmpeg`, faster-whisper, and chat delivery end to end.
- Verify existing explicit Telegram source facts and run one live approved-channel and one approved-chat lookup through `/memory` before relying on Telegram context.
- Add source-management controls to `/memory` for explicit Telegram `ref`, `title`, and `kind` facts.
- Verify a live Telegram request for a full past calendar day and assess whether the 500-message explicit-period scan limit is sufficient.
- Replace each `ContextExecutor` TODO handler only after its read-only adapter and policy checks have focused tests.

## Next

- Run live checks for dialogue retrieval, stable-fact extraction, knowledge upload, and Ollama-compatible endpoints.
- Simplify remaining legacy-only code inside `models.py`, `memory/store.py`, and `memory.py` in a separately reviewed pass.

## Later

- Reintroduce optional capabilities only as independent, tested integrations.

## Known risks

- Existing runtime process may use pre-cleanup code until restarted.
- SQLite is local and vector search is in-process; retrieval performance must be rechecked as history grows.

## Do not touch without explicit approval

- Production database and files under `data/`.
- Home Assistant callback contract and `.env` credentials.
- The local backup under `data/backups/`.

## Questions / TBD

- Which optional capability, if any, should be rebuilt first after the clean chat core is proven stable?
- If reminders return, should they be rebuilt as a narrow isolated integration instead of reviving the removed planner/skills/tasks stack?
