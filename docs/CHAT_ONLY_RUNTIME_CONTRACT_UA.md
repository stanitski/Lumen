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

Немає старого planner/task orchestration layer. Один `GetContract` передає `ReminderService` структуровану дію `create`, `list` або `cancel_list_item`; malformed, timeout або неоднозначний контракт не змінює SQLite. Створення до 10 one-time/daily/weekly root reminders атомарне після строгої перевірки дат, часу, повтору й дублікатів. У schema `v32` root має nullable `parent_reminder_id` лише у дочірніх snooze rows: recurring root зберігає свій початковий `due_at`, а silence або `no` атомарно створює рівно один one-shot child на `+15` хвилин з тим самим текстом, user і allowlisted route. `yes` на child завершує occurrence та просуває root до наступного майбутнього slot від початкового розкладу; child silence або `no` замінює тільки child. List і ordinal snapshot показують лише roots, але для `snoozed` root відображають effective due активного child; cancel root каскадно скасовує active children. Partial unique index і strict idempotent re-read не допускають двох active children. Під час connector creation зберігається allowlisted Telegram route (`target_channel` і canonical numeric `target_chat_id`) у `delivery_route_json`. Scheduled delivery повторно використовує лише цей route через `DeliveryService`, ніколи не виводить destination з `user_id`; legacy row без route не надсилається та лишається pending із bounded 15-minute retries до operator review. Якщо child вичерпує technical delivery retries, eligible recurring root переходить до наступного slot, а one-time або exhausted root стає `failed`, не лишаючись `snoozed`. Текстове підтвердження fired reminder розпізнає `GetContract`, а структурований callback з `reminder_id` і `delivery_id` лишається корельованим детермінованим шляхом.

## Calendar creation

Явна команда додати подію обробляється model-driven контрактом `calendar.action=create`; regex і phrase heuristics не використовуються. Контракт містить лише `summary`, `description`, `location`, `all_day`, `start`, `end` і не може вибирати calendar entity. `CalendarActionService` викликає `calendar.create_event` через Home Assistant для configured primary entity рівно один раз, без availability/conflict checks і без проміжного SQLite запису. Для timed event без `end` застосовується configured default 60 хвилин; для all-day event без `end` використовується наступна exclusive date. Після успішного write Lumen перечитує current-day HA snapshot, щоб наявна reminder projection одразу побачила подію. Timeout або transport error має результат `unknown` і не повторюється автоматично через ризик дубліката.

## Delivery

- async `/chat` і `/connector` доставляють відповідь через `DeliveryService`;
- inline `/api/chat` і `/api/generate` повертають той самий semantic result без callback;
- delivery failure не має переписувати semantic answer, лише фіксується окремо в result metadata/logs.
- reminder delivery є at-least-once, має `reminder_id` і cryptographically random 32-hex `delivery_id`, щоб `reminder_confirmed:<reminder_id>:<delivery_id>` помістився в Telegram callback limit; Home Assistant button передає обидва поля, а ingress передає їх у `/connector` для exact correlated confirmation.

Operator note: після оновлення потрібно rebuild і restart Lumen, щоб
застосувалась SQLite schema `v33`. Не backfill-ити `delivery_route_json` за
`user_id`; legacy rows потребують явного підтвердженого route або залишаються
fail-closed.

## Правило повторного розширення

Якщо optional capability повертається, її треба додавати як окрему, ізольовану integration поверх current core.

Не вважати старі planner/reminder/task docs поточним runtime contract.
