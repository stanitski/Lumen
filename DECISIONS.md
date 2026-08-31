# Lumen Decisions

This file stores durable technical, architectural, database, reporting, integration, naming, and business-logic decisions that future work should reuse instead of rediscovering.

## 2026-08-27 - Не ретраїти недостатній контекст Feedreader

- Context:
  Недоступне посилання без достатнього title/content/description не дає змоги
  підготувати достовірну новину, але попередній `retry` залишав такий item у
  Home Assistant назавжди.
- Decision:
  Коли article extraction неуспішний і RSS fallback коротший за 160 символів,
  `NewsProcessingService` повертає terminal `skip` з
  `reason_code=insufficient_source_context`. Timeout, model/transport failure
  та HTTP `429` залишаються retriable.
- Consequences:
  Home Assistant видаляє лише неінформативний raw item, не змінюючи сценарій;
  достатній RSS fallback і звичайне `keep` не зачіпаються.
- Revisit when:
  Feedreader почне надавати інший надійний структурований контекст, який можна
  оцінювати окремо від поточного мінімального обсягу тексту.

## 2026-08-22 - Reminder lifecycle без auto-snooze та schedule drift

- Context:
  У попередньому lifecycle silence/`no` створював child через 15 хвилин, а
  delivery retry змінював `due_at`. Після outage або неотриманого повідомлення
  це могло віднести recurring reminder далеко від його canonical Kyiv slot.
- Decision:
  `GetContract` нормалізує тільки intent; `ReminderService` є єдиним owner
  user-/route-scoped selection, state transition та side effects. Lifecycle
  підтримує create/list/update/pause/resume/soft delete/explicit snooze.
  Нечіткі selections і bulk delete використовують persisted 10-minute by-default
  management sessions із exact candidate IDs та `updated_at` precondition.
  `source_ref` calendar projections є read-only. Schema `v34` зберігає
  transport retry в `next_attempt_at`, а `due_at` лишається canonical schedule.
  Після configurable 30-minute grace stale one-shot стає `missed`, recurring
  root переходить до next slot, а silence/`no` завершує occurrence без child.
  Explicit snooze створює один child (default 15 хв) і не перетинає наступний
  recurring slot.
- Consequences:
  Зміна/пауза/видалення скасовують active child і root delivery correlation у
  `BEGIN IMMEDIATE` transaction; stale selection відмовляється без partial
  mutation. List показує canonical `due_at`, `paused`, child snooze або
  `next_attempt_at` окремо. Task Scheduler може wake/start на батареї, але
  повністю вимкнений ПК обробляється recovery grace policy після запуску.
- Revisit when:
  Буде потрібне керування самими HA calendar events, кілька одночасних child
  occurrence або інший продуктово погоджений snooze policy.

## 2026-08-18 - Final dialogue uses a progress-based Ollama stream watchdog

- Context:
  Final generation could spend most of the 240-second request timeout evaluating a large prompt, begin producing a valid answer, and then be cancelled with all generated work lost. Ollama exposes no per-request prompt-evaluation progress before its first streaming frame.
- Decision:
  Only the final `ChatOnlyService` dialogue call uses `OllamaConnector.stream_chat()`. It has no total or first-frame deadline; after the first valid NDJSON frame, 240 seconds without another valid frame triggers `cancelled_idle`. Partial text is neither persisted nor delivered, while inline Ollama-compatible endpoints return HTTP `504`. Contract, fact, embedding, knowledge, and news calls remain buffered with `OLLAMA_TIMEOUT_SECONDS=240`. A new message does not cancel an older request.
- Consequences:
  A healthy long generation may run beyond four minutes. A request that never emits its first frame can wait until transport failure or process shutdown. Concurrent requests can complete out of order, and an idle stream leaves no dialogue turn or callback.

## 2026-08-04 - News processing is an isolated synchronous integration

- Context:
  Feedreader supplies metadata and a link, while Home Assistant needs one terminal decision and a narration-ready Todo item without embedding article retrieval or editorial prompts in automations.
- Decision:
  `POST /news/process` runs one `NewsProcessingService` job at a time. It uses the shared `WebPageExtractor`, falls back to sufficiently informative RSS text, rejects only technical/editorial junk, applies exact active user `news_context/topic:*` `dislikes`, and calls Ollama separately for classification and Ukrainian 5W editing. `keep` and confirmed `skip` are terminal; transient failures are `retry`, and contention is HTTP `429` with `Retry-After: 60`.
- Consequences:
  Home Assistant may remove a raw item only after `keep` or `skip`. The public Todo key is stable from the final canonical URL. The service introduces no scheduler, database schema, config setting, or user `likes` facts.

## 2026-08-04 - Public-page extraction is shared and bounded

- Context:
  Both web search and news processing open third-party pages, so duplicated HTML stripping and redirect behavior would create inconsistent results and security policy.
- Decision:
  `WebPageExtractor` in `services/web_search.py` owns public DNS checks, manual redirect validation, HTML-only streaming limits, canonical URLs, and Trafilatura extraction. `DuckDuckGoWebSearch` retains its existing 5-result, 2-page, 1,200-character limits; news may use 12,000 characters. DNS is checked before each request but the transport is not IP-pinned, so DNS rebinding remains a documented residual risk.
- Consequences:
  Search and news share one extraction policy and dependency. Technical limits remain code constants rather than new configuration.

## 2026-07-22 - Google Calendar is projected through Home Assistant into one-time reminders

- Context:
  Google Calendar is the source of truth for scheduled events, while Lumen already has durable Telegram delivery, confirmation, and 15-minute snooze behavior in `ReminderService`.
- Decision:
  Lumen never calls Google directly. At startup, reconnect, and Kyiv day rollover it reads the current day from Home Assistant Calendar REST. HA WebSocket `calendar/event/subscribe` is an invalidation signal followed by an authoritative REST reread. Each occurrence with a stable `uid` is idempotently projected into a one-time reminder using nullable `reminders.source_ref` (schema `v33`); timed events use their start, all-day events use configured 09:00 `Europe/Kyiv`. Deleted or moved occurrences cancel only still-pending projections. Existing delivering, awaiting-confirmation, snoozed, and completed rows remain immutable.
- Consequences:
  No cron poll, Google credentials, availability heuristics, or parallel calendar table are introduced. Event summary and description are delivered as stored by HA, then the existing Telegram confirmation/snooze state machine takes over.

## 2026-07-22 - Calendar creation is a single non-retrying Home Assistant action

- Context:
  Lumen must create Google Calendar events from an explicit natural-language command without conflict checks, phrase heuristics, or intermediate reminder rows.
- Decision:
  `GetContract` emits one strictly normalized `calendar.create` item and never chooses the target entity. `CalendarActionService` always writes to configured `HOME_ASSISTANT_CALENDAR_WRITE_ENTITY_ID` through one HA `calendar.create_event` REST call. Missing timed end uses configured 60 minutes; missing all-day end becomes the next exclusive date. A successful write immediately refreshes the authoritative current-day HA snapshot. HTTP timeout or transport loss is `unknown` and is never retried automatically.
- Consequences:
  Explicit creation happens immediately and does not check availability. A timeout may require manual calendar inspection, but cannot create an automatic duplicate. Events created for today enter the existing reminder projection immediately; future events are projected on their day.

## 2026-07-21 - Parent/child snooze preserves recurring schedules

- Context:
  Reusing a recurring reminder row for `+15` minute snooze overwrote its scheduled time and could shift the next daily or weekly occurrence.
- Decision:
  Schema `v32` adds nullable `reminders.parent_reminder_id`. A root owns the user schedule and keeps its original `due_at` while `snoozed`. Silence or `no` atomically creates exactly one one-shot child at `+15` minutes, with the root text, user and allowlisted route. Child `yes` completes the occurrence and advances or completes the root from its original schedule; child silence or `no` closes that child and creates its replacement. Lists and ordinals expose roots only, using an active child's due time as the effective display time; root cancellation cascades to active children. A partial unique index plus strict post-contention re-read makes a concurrent snooze idempotent only when the root is `snoozed` and exactly one active child exists.
- Consequences:
  Parent recurrence does not drift during snooze. Wake scheduling includes child due times. If a child exhausts technical delivery retries, its eligible recurring root advances to the next future slot; a one-time or exhausted root fails rather than remaining `snoozed` without a wake.
- Revisit when:
  Reminder editing, multiple concurrent occurrences per root, or richer snooze policies are approved.

## 2026-07-21 - Persist reminder return routes and fail closed

- Context:
  A scheduled reminder no longer has the connector request that supplied its reply destination. Reconstructing a Telegram target from `user_id` is unsafe and caused delivery ambiguity.
- Decision:
  On reminder creation, persist only the allowlisted connector return route `target_channel="telegram"` and canonical numeric `target_chat_id` in nullable `reminders.delivery_route_json` (schema `v31`). Scheduled delivery reuses that route through `DeliveryService`; it never maps a user ID to a destination. A missing or invalid route does not call Home Assistant and remains pending with bounded 15-minute retries. Each new delivery claim uses a cryptographically random 32-hex `delivery_id`; structured confirmation must exactly match both `reminder_id` and `delivery_id`. The Home Assistant Telegram button carries both values.
- Consequences:
  Recurring occurrences and snoozes keep their original route. Legacy rows without a route fail closed instead of being delivered to an inferred chat. Operators must rebuild and restart Lumen for schema `v31`, then inspect or explicitly repair route-less legacy rows; no automatic backfill is permitted.
- Revisit when:
  A second connector channel is explicitly designed, allowlisted, and verified end to end.

## 2026-07-20 - Isolated reminder state machine with correlated confirmation

- Context:
  The chat-only runtime needs persistent reminders without restoring planner, skills, or task execution.
- Decision:
  `GetContract` owns language understanding and returns structured reminder create/list/cancel-list-item data. `ReminderService` owns SQLite validation, lease-based delivery claims, and one `WindowsWakeAdapter` task for the nearest pending due time or confirmation deadline. Delivery is at-least-once and uses a correlated `delivery_id`. Structured confirmations require matching `reminder_id` and `delivery_id`; plain yes/no is classified by `GetContract`. Silence or explicit `no` follows the parent/child snooze decision above.
- Consequences:
  Timeout, malformed, or ambiguous contracts cannot mutate reminders. Wake scheduling failures do not remove database records. Daily and weekly recurrence is calendar-based in `Europe/Kyiv`.
- Revisit when:
  Editing, pagination beyond ten active rows, or richer recurrence rules are approved.

## 2026-07-20 - Atomic multi-reminder creation and database-backed listing

- Context:
  A single natural-language request can contain multiple reminders, while a prior single-draft parser could silently retain a second time in the reminder label. A dialogue-generated list could also disagree with live SQLite state.
- Decision:
  Validate up to 10 `GetContract` drafts before writing, reject the whole request on any invalid, ambiguous, unsupported, or duplicate item, and insert the complete list in one SQLite transaction. Recalculate the wake task once after commit. A list reads only the current user's active `reminders` rows and records a user-scoped in-memory snapshot in `due_at, id` order; `cancel_list_item` can cancel only its exact displayed ordinal. There is no delete-all operation.
- Consequences:
  Multi-reminder requests cannot partially succeed, list answers cannot hallucinate archived or legacy reminders, and an absent, stale, or state-changed snapshot refuses cancellation without recomputing an ordinal.
- Revisit when:
  Cancellation, editing, pagination beyond ten active rows, or broader Ukrainian temporal grammar is approved.

## 2026-07-20 - Current documentation must describe only the chat-only runtime

- Context:
  Root docs and several `docs/*.md` files drifted apart. Some still described planner, skills, tasks, reminders, and smart-home execution as if they were active runtime paths, while live code had already moved to `ChatOnlyService` plus memory, knowledge, STT, context execution, and callback delivery.
- Decision:
  Treat the chat-memory-knowledge runtime as the only current architecture in documentation. `README.md`, `PROJECT_MAP.md`, `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`, and `docs/LUMEN_ARCHITECTURE_GUIDE_UA.md` must describe the active `chat_only` path. Old target-execution docs remain only as historical references and must say so explicitly.
- Consequences:
  Future tasks should not assume reminders, tasks, planner, or smart-home control are still active just because older docs mention them. Any rebuilt optional capability must be introduced as a new isolated integration, not described as already present.
- Revisit when:
  A new optional action subsystem is explicitly approved and implemented in live code.

## 2026-07-17 - Honor explicit Telegram calendar ranges

- Context:
  The Telegram adapter used only rolling 4-hour/48-hour windows, so a request for a calendar date could omit messages earlier that day.
- Decision:
  Add inclusive `date_from` and `date_to` ISO fields to `ContextSourceRequest`. `GetContract` resolves them using the explicit current local date. Telegram uses the requested range instead of its rolling cutoff and scans up to 500 messages per source to reach older periods.
- Consequences:
  A request for `2026-07-15` receives only messages from that local calendar day. Normal requests keep their smaller default windows. Extremely busy sources can still exceed the bounded 500-message scan and should be reported as a bounded retrieval limitation.
- Revisit when:
  Telegram lookup needs server-side date offsets or pagination for high-volume sources.

## 2026-07-17 - Select Telegram context by source kind

- Context:
  One source catalog contains both private correspondence and news channels. Reading all facts when the user asks for one type would mix unrelated and private context.
- Decision:
  Require Telegram source facts to include `kind=user|group|channel`. `GetContract` emits `source_kind`; `channel` selects news/broadcast facts, while `user` or `group` selects correspondence facts. Without a matching kind and no explicit allowed ref, return `needs_source`.
- Consequences:
  A request for news cannot read private chats, and a request for correspondence cannot read news channels. When no matching fact exists, the final assistant asks the user to name or add a matching allowed source instead of searching broadly.
- Revisit when:
  A source-management UI or a richer multi-source query contract is introduced.

## 2026-07-17 - Restore Telegram as a bounded read-only context adapter

- Context:
  The former Telegram lookup mixed Telethon access, fact storage, planner steps, and a separate Ollama answer call.
- Decision:
  Reuse Telethon session access only behind `ContextExecutor`. Read only source refs from explicitly tagged `messenger_context` facts, bound per-source message count and history windows, normalize messages into temporary prompt context, and reuse the current STT service for Telegram audio/video.
- Consequences:
  Telegram messages and summaries are not persisted. The final chat Ollama call remains the sole answer generator. Missing source facts, credentials, an unauthorized session, or an unapproved explicit source produces a truthful unavailable result.
- Revisit when:
  Access scopes need per-user policy rather than one local allowlist, or semantic search over larger channel history is approved.

## 2026-07-17 - Dispatch external context through one executor

- Context:
  `GetContract` can request Telegram, web, Home Assistant, or knowledge context, but direct adapter calls from chat orchestration would recreate the old coupled architecture.
- Decision:
  Add `ContextExecutor` between contract construction and final prompt assembly. It independently validates source/operation pairs, dispatches source-specific handlers, and returns structured results. All four handlers currently return `pending_implementation` TODO results.
- Consequences:
  Chat orchestration has one stable extension point and cannot claim an external lookup succeeded without a result. The executor has no database, scheduler, or side-effect capability.
- Revisit when:
  The Telegram read-only adapter is implemented behind its access policy.

## 2026-07-17 - Describe external context needs in GetContract

- Context:
  Telegram, web, Home Assistant, and uploaded knowledge need a common entry point before separate read-only adapters are restored.
- Decision:
  Extend `GetContract` with a validated `context_sources` list. Each request has `source`, `operation`, `query`, and `scope`; permitted pairs are `telegram/search`, `web/search`, `home_assistant/read_state`, and `knowledge/search`.
- Consequences:
  Contract classification can request context without executing anything or storing external data. Invalid sources and operations are discarded. Adapter execution, access policy, and context injection remain separate work.
- Revisit when:
  The Telegram read-only adapter is introduced.

## 2026-07-17 - Route speech through the normal chat path

- Context:
  Users need to send audio and video messages without restoring the former attachment, planner, or task architecture.
- Decision:
  Accept connector audio/video attachments, transcribe them with `ffmpeg` plus `faster-whisper`, then pass the resulting text to `ChatOnlyService` as a normal message.
- Consequences:
  Speech uses the same memory retrieval, persistence, and callback delivery as text. It has no separate database table, scheduler, action capability, or voice-response mode.
- Revisit when:
  STT latency, device configuration, or failed-transcription delivery needs a user-visible retry flow.

## 2026-07-17 - Keep only the chat-memory-knowledge core

- Context:
  Planner, skills, execution, scheduling, reminders, smart-home, and speech paths were tightly coupled and repeatedly caused unrelated regressions.
- Decision:
  Remove those code paths and tables. Keep direct text chat, fact/dialogue memory, knowledge ingestion/retrieval, Home Assistant callback delivery, the memory manager, and Ollama-compatible API.
- Consequences:
  Lumen has no hidden full mode, scheduled work, device control, voice handling, or connector attachments. Each optional capability must be designed and tested as a separate integration before it returns.
- Revisit when:
  The clean core has passed live retrieval and delivery checks and there is an approved, isolated proposal for one optional capability.

## 2026-07-17 - Make debug memory evidence and policy inspectable

- Context:
  Operators could see selected dialogue hits but not the effective retrieval policy, fact selection weights, or the previous-turn anchor that also entered the dialogue prompt.
- Decision:
  Return the retrieval policy and all prompt evidence from `/memory/debug-search`. Display a deterministic fact-selection weight, the existing dialogue retrieval weight, and continuity-anchor priority; order the debug list by weight and mark duplicate prompt occurrences.
- Consequences:
  The memory manager now exposes the real prompt inputs without persisting or delivering anything. Weights remain source-specific diagnostics, not a future cross-source rank-fusion formula.
- Revisit when:
  Entity/lexical candidate union introduces a unified cross-source ranking model.

## 2026-07-17 - Read retrieval candidate sources in parallel

- Context:
  Topic, Q/A semantic, and knowledge candidate reads were sequential even though they are independent after query embeddings are available.
- Decision:
  Run independent topic, user/global Q/A semantic, exact lexical/entity, and enabled knowledge reads concurrently through separate read-only SQLite connections. Form the user dialogue candidate set as a deduplicated union, then rank once using Q/A similarity, a bounded topic bonus, exact-match bonus, and recency. Topic similarity must not gate Q/A or lexical candidates.
- Consequences:
  An arbitrary primary topic cannot discard a turn that is semantically relevant or names the requested entity/title. Exact quoted phrases and recognized multi-word entities become bounded candidates without a prompt-specific rule. The prompt budget still limits final context, so this is not a full-history dump or a reranker. The existing Q/A score still uses the stronger question or answer vector; it can over-rank an operational answer that merely repeats a title.
- Revisit when:
  Live debug results show that the fixed rank weights or `max(question, answer)` need calibration, or FTS/entity aliases are introduced for large histories.

## 2026-07-17 - Derive retrieval policy deterministically from query understanding

- Context:
  `GetContract` describes the requested answer, but retrieval still depended on separate service flags and could fetch irrelevant profile facts or long-term context for a low-signal request.
- Decision:
  Add `MemoryService.get_retrieval_policy(contract, payload_text)`. It is a pure mapping with no Ollama call and controls facts, topic/Q-A search, knowledge, previous-turn continuity, completeness, and future time filtering.
- Consequences:
  Query interpretation remains model-driven once, while memory access is predictable, testable, and cost-bounded. The first version does not yet implement entity/lexical retrieval or time-filtered event storage; those controls remain explicit policy outputs for subsequent work.
- Revisit when:
  Entity/lexical candidate union and event retrieval are implemented.

## 2026-07-17 - Backfill missing topic indexes across dialogue history

- Context:
  Only 6 of 935 persisted conversation turns had topic embeddings. Older turns about Ян Костирко were therefore excluded from topic-first retrieval despite having question and answer embeddings.
- Decision:
  Extend `scripts/backfill_turn_topics.py` to process all or a bounded date scope, skip turns that already have both topic fields, and support `--dry-run` and `--limit`. Create a local database backup before a live full run.
- Consequences:
  Topic-first retrieval can consider historical dialogue after the backfill without a schema change. The operation requires one Ollama classification and one embedding call per pending non-empty topic, may take substantial time, and blank-topic turns remain unindexed by design; without a separate schema marker they are retried on a later full run.
- Revisit when:
  The complete backfill has finished and retrieval is rechecked with person-specific and general queries.

## 2026-07-17 - Classify an answer contract with the retrieval topic

- Context:
  Topic retrieval returned the nutrition turns, but a free-form dialogue answer omitted details from records that were present in the prompt.
- Decision:
  Replace the deep-query topic-only call with `GetContract(text)`, returning a validated topic, answer mode, time scope, and completeness level in one clean Ollama call. Pass the contract to the final dialogue prompt. `list_records/complete` requires all confirmed relevant records and explicit conflicts.
- Consequences:
  Retrieval keeps using the topic vector and adds no extra classifier call. The contract is generic rather than nutrition-specific, but its small controlled vocabulary can still be classified incorrectly and must fall back safely to `direct_answer/none/normal`. The live `"Що я їв сьогодні?"` check returned `food/list_records/today/complete`, included all five selected turns, and surfaced the stored `931` question/answer conflict instead of silently replacing its details.
- Revisit when:
  More live memory-manager cases show whether the contract reliably prevents omissions without degrading ordinary dialogue or whether topic-near commentary and irrelevant facts need separate retrieval controls.

## 2026-07-17 - Use clean topic and embedding primitives

- Context:
  Topic classification behaved differently from a direct Ollama request because regular chat calls add Lumen runtime context. Embedding calls were also spread across several memory paths.
- Decision:
  Provide `MemoryService.get_topic(text)` and `MemoryService.get_embedding(text)`. `get_topic` sends only the generic classifier prompt and supplied text, with runtime context explicitly disabled. `get_embedding` is the common validated raw-embedding path for queries and stored turn fields.
- Consequences:
  Topic classification is reproducible from its visible two-message request and has no domain-specific prompt rules. Ollama may still vary free-form labels across independent calls; topic-vector retrieval remains tolerant of label mismatch. Existing normal dialogue calls retain their runtime context.
- Revisit when:
  Replacing free-form labels with a versioned controlled vocabulary or a non-LLM classifier is evaluated.

## 2026-07-17 - Use topic-first dialogue retrieval experiment

- Context:
  A daily nutrition query failed because relevant current turns had low embedding similarity and were discarded before the recency bonus, while unrelated generic dialogue scored highly.
- Decision:
  Add one `primary_topic` field and one embedding of that topic text to each conversation turn. Classify new turns and chat-only deep queries with Ollama. Use nearest topic vectors as candidates rather than exact topic-string equality, then rank candidates by the strongest question/answer embedding similarity plus recency bonus, without the global similarity threshold; otherwise retain global semantic fallback. Backfill only `2026-07-16` initially.
- Consequences:
  Topic metadata narrows retrieval without becoming a user-memory fact, requiring a controlled topic vocabulary, or requiring a date-specific branch. It adds one topic embedding call after each topic classification. The experiment cannot guarantee a complete calendar-day ledger or cleanly separate nearby themes such as nutrition logging and health discussion.
- Revisit when:
  Live memory-manager results show whether topics improve nutrition retrieval without harming general dialogue, before expanding the backfill range.

## 2026-07-16 - Debug memory through the production context builder

- Context:
  Retrieval errors must be inspectable without guessing which facts or dialogue turns were actually passed to Ollama.
- Decision:
  Make the memory manager `Search` action call `ChatOnlyService`'s shared message-context builder and show its selected facts, selected dialogue turns, semantic scores, and a non-persisting Ollama preview answer.
- Consequences:
  Debug output follows the normal chat retrieval contract rather than duplicating selection logic. The debug request consumes one embedding call and one dialogue-model call, but creates no turn, fact, embedding, or delivery callback.
- Revisit when:
  Chat-only context construction is split into separately versioned retrieval and prompt-assembly services.

## 2026-07-16 - Store dialogue question and answer embeddings separately

- Context:
  A combined `Question + Answer` vector failed to retrieve food-related turns for a summary query even though the raw dialogue existed.
- Decision:
  Keep each dialogue pair in one `conversation_turns` row, but store separate `question_embedding_json` and `answer_embedding_json` values there. Search both vectors, retain the stronger score per turn, and reserve `memory_embeddings` for knowledge documents.
- Consequences:
  Retrieval can match either what the user asked or what Lumen stated without duplicating the full turn in prompt context. Telegram Desktop JSON is the canonical source for the imported historical dialogue snapshot; `memory_facts` is preserved unchanged.
- Revisit when:
  A dedicated event model is approved or vector search moves out of SQLite.

## 2026-07-16 - Retrieve dialogue memory by relevance with time decay

- Context:
  Fixed recent-turn injection could crowd out older, highly relevant conversations, while pure semantic ranking did not prefer an active conversation over an equally relevant old one.
- Decision:
  In chat-only mode, evaluate all stored dialogue embeddings, reject weak semantic matches, and rank the remaining hits by semantic similarity plus an exponential time-decay bonus. The bonus halves every 72 hours. Also include exactly one immediately previous persisted user/assistant turn without retrieval evidence as a continuity anchor; the current user message has higher priority.
- Consequences:
  Prompt context is more relevant and bounded, while a fresh relevant conversation naturally outranks a slightly stronger but much older match. Short follow-ups retain their immediate conversational reference even when embedding similarity is broad. The anchor may duplicate a semantic hit, which is accepted for one turn. Existing knowledge embeddings remain read-only retrieval context; knowledge ingestion remains disabled.
- Revisit when:
  Retrieval metrics show that the threshold, 72-hour half-life, or context budget needs calibration against real conversations.

## 2026-07-16 - Stabilize Lumen in chat-only runtime mode

- Context:
  The coupled skills, planner, execution, scheduling, integration, and delivery stack has caused unrelated regressions during architectural work.
- Decision:
  Make `LUMEN_RUNTIME_MODE=chat_only` the default. Keep only direct dialogue, memory, embeddings, and final-answer delivery active. Preserve optional code, tables, and data behind an explicit `full` mode rather than deleting them.
- Consequences:
  Automation, reminders, knowledge, smart-home, attachment, and speech features are unavailable by default. Future experiments must re-enable one subsystem at a time with focused tests.
- Revisit when:
  The chat-only runtime is stable and an optional subsystem has a separately verified, low-coupling integration path.

## 2026-07-10 - Use documentation-first workflow for Lumen changes

- Context:
  The repository has multiple active subsystems, evolving architecture docs, and a large enough surface area that broad scans waste tokens and increase the chance of incorrect assumptions.
- Decision:
  Use a documentation-first workflow for Lumen changes. Start by identifying the smallest relevant file set, confirm the live code path before editing, and update root/project docs whenever architecture, contracts, or operator workflows change.
- Consequences:
  Future tasks should stay narrower, use fewer tokens, produce more stable repo knowledge, and leave a clearer record of architectural intent. Work may pause briefly up front to identify scope before edits.
- Revisit when:
  The repository gains a more formal architecture index, automated documentation generation, or a different development workflow that makes this guidance obsolete.

## 2026-07-04 - Enforce side-effect policy in shared runtime layers

- Context:
  Architecture review found that prompt-only controls were too weak for side-effect skills and goal wakeups.
- Decision:
  Keep capability checks, confirmation requirements, and related gating in shared runtime/storage layers instead of relying on one route, one adapter, or prompt wording alone.
- Consequences:
  Policy-sensitive behavior is more consistent across ingress paths, but schema/runtime changes in this area have broad impact and need regression coverage.
- Revisit when:
  Lumen gets a different execution architecture that centralizes policy in a narrower layer.

## 2026-07-01 - Use one ChatService core across native and Ollama-compatible ingress

- Context:
  Native `/chat` and Ollama-compatible endpoints had started to drift in behavior.
- Decision:
  Route `/chat`, `/api/chat`, and `/api/generate` through the same `ChatService` orchestration core, with delivery mode controlled by adapter context rather than separate logic stacks.
- Consequences:
  Behavior parity is easier to maintain, but regressions in shared chat orchestration can affect every ingress path at once.
- Revisit when:
  A future API contract requires materially different orchestration semantics for one ingress family.

## 2026-07-01 - Home Assistant owns reminder delivery defaults

- Context:
  Reminder routing work showed that Telegram/channel defaults were being inferred in the wrong layer.
- Decision:
  Lumen emits reminder callbacks to Home Assistant, and Home Assistant decides destination defaults and Telegram forwarding behavior.
- Consequences:
  Reminder logic stays less channel-specific inside Lumen, and live delivery debugging must include Home Assistant state rather than only repo-side Lumen code.
- Revisit when:
  Lumen becomes the intended source of truth for outbound reminder routing.

## 2026-06-24 - Personal automation scenarios belong in routines, not ad hoc new skills

- Context:
  Chat/routing work distinguished supported personal scenarios from system-skill creation.
- Decision:
  Use routines as the supported user-level path for personal scenarios, while keeping direct skill creation limited to system-controlled flows.
- Consequences:
  The planner/executor contract stays narrower, and operator tooling should favor routine management over casual skill authoring.
- Revisit when:
  Lumen gains an approved user-facing skill authoring workflow with validation and lifecycle controls.
