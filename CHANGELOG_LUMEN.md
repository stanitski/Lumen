# Lumen Change Log

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
