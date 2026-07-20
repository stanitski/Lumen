# Lumen Codex Guide

## Project overview

Lumen is a FastAPI-based local assistant. It accepts native and text-only connector chat, builds SQLite-backed memory and knowledge context, calls Ollama, persists dialogue/facts, and delivers final responses through a Home Assistant callback.

## Language rules

- Communicate with the user in Ukrainian unless the user explicitly asks otherwise.
- Keep code identifiers, file paths, class names, function names, environment variables, commands, logs, and error messages in their original language.
- Documentation files may be written in Ukrainian, but technical identifiers must remain unchanged.
- Commit messages and changelog entries may be Ukrainian unless the repository already uses English consistently.
- Keep responses short and practical.

Main runtime shape:

- API wiring: `src/lumen/main.py`
- Chat orchestration: `src/lumen/services/chat_only.py`
- Memory and knowledge: `src/lumen/services/memory.py`, `src/lumen/services/knowledge.py`
- Speech-to-text ingress: `src/lumen/services/speech_to_text.py`
- Delivery: `src/lumen/services/delivery.py`
- SQLite schema and persistence: `src/lumen/storage/`, `src/lumen/memory/`

## Before editing code

- Read the current task carefully and keep responses short.
- First identify the smallest relevant file set.
- If more than 3 files seem necessary, stop and explain why before editing.
- Prefer live code over older docs when they disagree.
- Inspect only the minimum paths needed for the task.
- For runtime behavior, start from the likely entry point and trace one flow end to end.
- For database questions, inspect `src/lumen/storage/db.py` and the directly related service/store code before assuming schema behavior.
- Do not show full diffs unless requested.

## After editing code

- Re-check the touched flow end to end at the narrowest reasonable scope.
- Run only the smallest relevant verification.
- Summarize what changed, what was checked, and any remaining TBDs.
- For documentation-only tasks, verify internal consistency and clearly state when live code was not re-checked.
- Update documentation when behavior, contracts, architecture, data flow, integrations, or operator workflows changed.
- Add an entry to `CHANGELOG_LUMEN.md` for meaningful repo changes.
- Add or update `DECISIONS.md` when a durable technical decision was made.
- Add or update `TODO_LUMEN.md` when new concrete follow-up work or risks were discovered.

## Cost-control rules

- Minimize token usage.
- Do not scan the whole repository unless explicitly asked.
- Ignore large/generated areas unless the task directly depends on them.
- Prefer targeted reads with exact file paths and narrow searches.
- Summarize structure instead of dumping long listings.
- Use `TBD` instead of guessing.
- Keep responses short.

## Forbidden actions

- Do not refactor unrelated code.
- Do not rename files unless explicitly requested.
- Do not change database schema unless explicitly requested.
- Do not change configuration files unless the task clearly requires it.
- Do not install dependencies unless explicitly requested.
- Do not run destructive commands.
- Do not revert user changes.
- Do not expose secrets from `.env`, local databases, or integration credentials.

## Testing and manual checking

- Use the smallest relevant test scope first.
- In this repo, prefer `python -m unittest ...` over `pytest` unless the user asks otherwise and the environment supports it.
- For syntax-only safety, use a narrow compile check when useful.
- For API flow changes, verify the directly affected endpoint or service path only.
- If verification was not run, say so clearly.

## Documentation rules

- Treat docs as part of the deliverable, not optional cleanup.
- Keep `PROJECT_MAP.md` human-readable and current when module boundaries or flows change.
- Record durable architecture or business-logic choices in `DECISIONS.md`.
- Keep `TODO_LUMEN.md` focused on real follow-ups, risks, and open questions.
- If docs and code disagree, document the live code path and note the mismatch.

Do not treat archive/ as current project truth unless explicitly asked.
