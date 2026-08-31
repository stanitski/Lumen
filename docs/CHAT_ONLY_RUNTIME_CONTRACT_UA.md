# Chat-Only Runtime Contract

## Призначення

Поточний Lumen працює як chat-memory-knowledge runtime без planner/skills/tasks. Reminders є окремою ізольованою інтеграцією.

Активний шлях:

`Ingress -> ChatOnlyService -> MemoryService context -> optional ContextExecutor reads -> Ollama dialogue -> SQLite persistence -> DeliveryService`

## Що зараз активно

- текстовий чат через `/chat`, `/connector`, `/api/chat`, `/api/generate`;
- STT для connector audio/video attachments;
- dialogue memory і profile facts у SQLite;
- knowledge retrieval як prompt context;
- bounded external context через `ContextExecutor`;
- callback delivery через Home Assistant;
- isolated `ReminderService` для one-time, daily та weekly reminders;
- memory manager і debug search.

## Що зараз не є runtime-фічами

- planner, skills, execution plans;
- planner-driven reminders, tasks і scheduler loops;
- smart-home control;
- policy engine для side effects;
- legacy broad Telegram ingestion;
- будь-який action execution поза chat response path.

## Контракт пам'яті

- `conversation_turns` зберігає user/assistant turn pair і retrieval metadata;
- `memory_facts` зберігає стабільні user facts і preferences;
- `memory_embeddings` використовується для knowledge retrieval;
- post-processing після turn persistence може добувати нові facts і оновлювати dialogue metadata;
- debug search використовує той самий live context-building path, але не створює новий turn і не викликає delivery.

## Контракт зовнішнього контексту

`ContextExecutor` приймає лише read-only context requests:

- `telegram/search`
- `web/search`
- `home_assistant/read_state`
- `knowledge/search`

`telegram/search` і `web/search` реалізовані. `home_assistant/read_state` та
contract-requested `knowledge/search` повертають `pending_implementation`.
Звичайний knowledge retrieval активний окремо через
`MemoryService.build_planner_context()`.

Правила:

- зовнішній контекст тимчасовий і не зберігається як imported dialogue;
- Telegram читається лише з явно дозволених `messenger_context` facts;
- коли контекст недоступний або handler ще не реалізований, модель має бачити truthful unavailable/pending signal;
- final answer все одно генерує один основний Ollama dialogue call.

## Speech-to-text

`/connector` підтримує:

- `event_type="text"` для plain text;
- `event_type="attachment"` лише для STT-supported audio/video files.

Attachment path:

`connector -> SpeechToTextService -> ChatOnlyService`

Немає старого planner/task orchestration layer. Один `GetContract` передає `ReminderService` typed дії `create`, `list`, `update`, `pause`, `resume`, `delete`, `snooze`, `select` і `confirm_bulk`; legacy `cancel_list_item` лишився compatibility path. Model лише витягає intent/selector/patch, а user-scoped matching, validation і side effects виконує state machine. Exact text або stable ID змінюються одразу; ambiguous і bulk selection використовують короткоживучу route-scoped SQLite session з token та revision preconditions. `delete` є soft cancel, calendar-projected roots read-only. Schema `v34` додає `next_attempt_at`, тому transport retry не змінює canonical `due_at`. Прострочення понад configured 30-minute grace не доставляється: one-shot стає `missed`, recurring переходить до наступного anchored slot. Silence або `no` також завершує occurrence без child; лише explicit `snooze` створює один one-shot child із persisted allowlisted route. List показує canonical slot окремо від explicit snooze/retry. Confirmation fired reminder лишається exact-correlated через `reminder_id` + `delivery_id`.

## Calendar creation

Явна команда додати подію обробляється model-driven контрактом `calendar.action=create`; regex і phrase heuristics не використовуються. Контракт містить лише `summary`, `description`, `location`, `all_day`, `start`, `end` і не може вибирати calendar entity. `CalendarActionService` викликає `calendar.create_event` через Home Assistant для configured primary entity рівно один раз, без availability/conflict checks і без проміжного SQLite запису. Для timed event без `end` застосовується configured default 60 хвилин; для all-day event без `end` використовується наступна exclusive date. Після успішного write Lumen перечитує current-day HA snapshot, щоб наявна reminder projection одразу побачила подію. Timeout або transport error має результат `unknown` і не повторюється автоматично через ризик дубліката.

## Delivery

- async `/chat` і `/connector` доставляють відповідь через `DeliveryService`;
- звичайний чат визначає `voice` через чинний `MemoryService.should_enable_voice_response`: «вголос»/«вслух» у запиті (без урахування регістру) або збережена голосова перевага користувача вмикає озвучення; без них `voice=false`;
- цей прапорець передається в `ChatResult` і стандартний callback HA; `skip_memory` не вимикає розпізнавання голосової команди. Окремі acknowledgement-відповіді reminders/calendar не змінюються;
- inline `/api/chat` і `/api/generate` повертають той самий semantic result без callback;
- delivery failure не має переписувати semantic answer, лише фіксується окремо в result metadata/logs.
- reminder delivery є at-least-once, має `reminder_id` і cryptographically random 32-hex `delivery_id`, щоб `reminder_confirmed:<reminder_id>:<delivery_id>` помістився в Telegram callback limit; Home Assistant button передає обидва поля, а ingress передає їх у `/connector` для exact correlated confirmation.

Operator note: після оновлення потрібно rebuild і restart Lumen, щоб
застосувалась SQLite schema `v34`. Не backfill-ити `delivery_route_json` за
`user_id`; legacy rows потребують явного підтвердженого route або залишаються
fail-closed.

## Правило повторного розширення

Якщо optional capability повертається, її треба додавати як окрему, ізольовану integration поверх current core.

Не вважати старі planner/reminder/task docs поточним runtime contract.
