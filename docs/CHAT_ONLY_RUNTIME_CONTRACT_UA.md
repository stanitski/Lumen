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
- calendar create через `CalendarActionService` і current-day projection через `HomeAssistantCalendarClient`;
- synchronous `POST /news/process` через `NewsProcessingService` (окремо від chat/callback flow);
- memory manager і debug search.

## Що зараз не є runtime-фічами

- planner, skills, execution plans;
- planner-driven reminders, tasks і scheduler loops;
- smart-home control;
- policy engine для side effects;
- legacy broad Telegram ingestion;
- generic action executor; reminder recovery/wake і calendar projection працюють у власних вузьких paths.

## Контракт пам'яті

- `conversation_turns` зберігає user/assistant turn pair і retrieval metadata;
- `memory_facts` зберігає стабільні user facts і preferences;
- `memory_embeddings` використовується для knowledge retrieval;
- post-processing після turn persistence може добувати нові facts і оновлювати dialogue metadata;
- debug search використовує той самий live context-building path, але не створює новий turn і не викликає delivery.

Для звичайного chat порядок: `add_turn()` → callback delivery, якщо ввімкнено →
`postprocess_dialogue_turn()` для збереженого turn. Inline API пропускає callback
і повертає результат після processing. Delivery failure не блокує post-processing.
Користувач `global` не зберігає turn. У request або callback metadata прапорці
`debug`, `debug_request`, `lumen_debug`, `skip_memory`, `disable_memory` також
вимикають turn persistence і його post-processing, якщо значення — boolean
`true` або рядок `1`, `true`, `yes`, `on` (без урахування регістру та крайніх
пробілів). Читання memory context, reminder/calendar handling і delivery ці
прапорці не вимикають; це не загальний режим без side effects.

### Обов’язкові й контекстні факти

`lumen:mandatory` у `tags_json` позначає постійне персональне правило
спілкування. Воно передається окремим system-блоком у кожний normal dialogue
prompt, без ліміту трьох preferences та без бюджетів контекстних фактів.
Поточне явне прохання може перевизначити правило для однієї відповіді;
пам’ять не надає нових capabilities і не перевизначає системні обмеження.
Global facts не стають персональними правилами. Secrets, credentials та
прострочені факти не беруть участі в prompt/index.

Extraction повертає `inclusion=mandatory|contextual` і optional `fact_id`.
Мова, форма звертання, граматичні форми, постійний стиль і погоджена персона
класифікуються автоматично. Разові інструкції всередині задачі не повинні
ставати durable facts. Explicit update/revoke перевіряє owner та identity
існуючого запису; policy tag замінюється, а інші теги зберігаються.
Окремі правила мають окремі subjects (мова, звертання, стиль, граматичні форми,
персона). Конфліктні facts одного extraction batch з однаковою upsert identity
відкидаються з warning замість мовчазного перезапису останнім значенням.

`MemoryService` викликає `FactContextService`: усі активні facts читаються
keyset pages, mandatory відокремлюються, contextual добираються за cosine
similarity та лексичними збігами. `include_facts` залишається compatibility
полем retrieval policy і тепер завжди `true`; тип відповіді не забороняє facts.
Для визначеного follow-up пошуковий текст включає попереднє питання користувача.

Індекс зберігається в чинній `memory_embeddings`: `source_type=memory_fact`,
`source_ref=memory:fact:<id>`, `chunk_index=0`. Metadata містить hash змісту та
модель. Перед використанням перевіряються актуальний факт, owner, policy,
строк дії й hash/model. Оновлення/видалення інвалідує відповідний embedding;
post-processing індексує нові факти та повторює невдалі спроби. Початковий
backfill — окрема operator procedure. Збій embeddings дає lexical fallback,
не скасовуючи збереження facts і передачу mandatory.

Налаштування `FACT_SEMANTIC_MIN_SIMILARITY` (default `0.37`),
`FACT_CONTEXT_ITEM_LIMIT` (`10`) і `FACT_CONTEXT_CHAR_LIMIT` (`2400`) стосуються
лише contextual facts. `mandatory_facts`, `contextual_facts`, `mandatory_context`
та `fact_retrieval` у debug context пояснюють фактичний добір і fallback;
`selected_facts` містить саме відформатовані facts. `knowledge` у `/memory/items`
не включає службові embeddings фактів.

Детерміновані reminder/calendar replies, internal classifiers і news pipeline
не переводяться на цей dialogue prompt.

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
- для channel sources пошук використовує текст і caption допису, але не
  завантажує та не транскрибує вкладені audio/video; тип вкладення лишається в
  полі `media`;
- для allowlisted user/group sources media transcription лишається активною;
  помилка декодування одного вкладення (`SpeechToTextError`, `OSError`,
  `ValueError` або `IndexError`) не скасовує весь Telegram lookup: зберігається
  caption, після чого обробка наступних повідомлень продовжується;
- коли контекст недоступний або handler ще не реалізований, модель має бачити truthful unavailable/pending signal;
- final answer все одно генерує один основний Ollama dialogue call.

## Speech-to-text

`/connector` підтримує:

- `event_type="text"` для plain text;
- `event_type="attachment"` лише для STT-supported audio/video files.

Attachment path:

`connector -> SpeechToTextService -> ChatOnlyService`

Транскрипт проходить той самий reminder/calendar contract handling, що й текст.

## Фото та харчування

`OllamaConnector.chat_multimodal()` підтримує серіалізацію image bytes у base64,
але цей шлях не підключений до chat ingress. `/connector` повертає
`unsupported_attachment` для фото; `/api/chat` витягає лише текст. Knowledge
image upload не виконує OCR/vision extraction. Розпізнавання страв/калорій і
структурований журнал харчування не реалізовані. `nutrition_*` у historical
JSON та retention/topic classification не є активним nutrition executor.

## Reminder lifecycle

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
