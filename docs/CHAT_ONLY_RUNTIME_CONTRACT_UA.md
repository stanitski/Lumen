# Chat-Only Runtime Contract

## Призначення

Поточний Lumen працює як chat-memory-knowledge runtime без planner/skills/tasks. Reminders є окремою ізольованою інтеграцією.

Активний шлях:

`Ingress -> shared chronological context -> ChatOnlyService/MemoryService routing -> optional ContextExecutor reads -> Ollama/action -> ConversationService persistence and delivery`

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
- shared chronological history для user-facing turns, reminder/calendar results,
  nutrition interactions і knowledge upload result.

## Що зараз не є runtime-фічами

- planner, skills, execution plans;
- planner-driven reminders, tasks і scheduler loops;
- smart-home control;
- policy engine для side effects;
- legacy broad Telegram ingestion;
- generic action executor; reminder recovery/wake і calendar projection працюють у власних вузьких paths.

## Контракт спільної історії

Telegram: прямий запит прочитати переписку з конкретним `@username` дозволяє
read-only доступ до цього користувача без додавання до `messenger_context`.
Routing лишається model-driven: `telegram/search`, `source_kind=user`,
`scope=explicit_chat`. Дозвіл перевіряється за ref у поточному user text,
а не лише в model query; resolved entity має бути `User`. Канали, групи й
неявний вибір джерел лишаються обмежені збереженим списком. Для імені без
`@username`, якого немає в каталозі, автоматичний пошук контакту не реалізовано.

`ConversationService` завершує user-facing взаємодію: `finish()` зберігає turn
через `MemoryService`, за потреби доставляє callback і запускає post-processing;
`record_event()` додає assistant-only подію без доставки. До routing і відповіді
завантажується bounded chronological context того самого користувача та, коли
доступний, того самого chat. Turns з невідомим chat ID лишаються сумісними;
контекст обмежений налаштуваннями часу, кількості й символів та подається як
untrusted evidence.

У shared history належать звичайні chat turns, user-facing результати
reminder/calendar дій, nutrition записи/виправлення/уточнення та короткий
результат knowledge upload. Due reminder після успішної доставки додається як
assistant-only `reminder_notification` з порожнім питанням і стабільним
`event_id` на основі `delivery_id`. Якщо запис історії не вдався, reminder не
доставляється повторно; запис повторюється ідемпотентно з `awaiting_confirmation`
під час наступного циклу.

Transport IDs, receipts, service synchronization і внутрішні classifier events
не є utterances. `POST /news/process` лишається request-driven Feedreader API;
його article fetch, classification і Todo plumbing не додаються до conversation
context. Окреме питання користувача про новини обробляється звичайним chat і
зберігається як звичайний user-facing turn.

Одноразовий `python -m lumen.reminder_wakeup` створює той самий
`ConversationService`: event записується до завершення процесу, а індексація
запускається task-local і скасовується при виході. Незавершений turn лишається в
SQLite для звичайного runtime recovery.

## Контракт пам'яті

- `conversation_turns` зберігає user/assistant turn pair і retrieval metadata;
- `memory_facts` зберігає стабільні user facts і preferences;
- `memory_embeddings` використовується для knowledge retrieval;
- post-processing після turn persistence може добувати нові facts і оновлювати dialogue metadata;
- debug search використовує той самий live context-building path, але не створює новий turn і не викликає delivery.

Для user-facing interaction `ConversationService.finish()` виконує persistence →
callback delivery, якщо ввімкнено → `postprocess_dialogue_turn()`. Inline API
пропускає callback і повертає результат після processing. Delivery failure не
блокує post-processing.
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

Детерміновані reminder/calendar replies не йдуть через Ollama dialogue prompt,
але їхні user-facing результати записуються у shared history. Internal
classifiers і news processing лишаються поза dialogue history.

## Контракт зовнішнього контексту

`ContextExecutor` приймає лише read-only context requests:

- `telegram/search`
- `web/search`
- `home_assistant/read_state`
- `knowledge/search`
- `nutrition/summary`

`telegram/search`, `web/search` і `nutrition/summary` реалізовані.
`home_assistant/read_state` та
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
- `event_type="attachment"` для STT-supported audio/video files або Telegram food photos;
- `event_type="nutrition_delivery"` для прив'язки HA sent-message ID до nutrition entry.

Attachment path:

`connector -> SpeechToTextService -> ChatOnlyService`

Транскрипт проходить той самий reminder/calendar contract handling, що й текст.

## Фото та харчування

Telegram фото через `/connector` передається до окремого `NutritionService`,
який викликає `OllamaConnector.chat_multimodal()`. Текстові виправлення
змінюють останній або reply-selected запис. JSON state/CSV зберігаються
поза SQLite; user-facing nutrition result записується через
`ConversationService`. Summary читає authoritative JSON через
`ContextExecutor`, а не history. Schema не змінена. `/api/chat` витягає лише текст;
knowledge image upload не виконує OCR/vision extraction. Контракти та HA
підготовка описані в `NUTRITION.md`. Historical `nutrition_*` retention/topic
labels не є частиною цього isolated service.

Невикористані execution JSON seeds і legacy models/store APIs видалено
2026-10-07; historical документи та retention/topic labels не активують їх.

## Цілісність knowledge і memory API

Knowledge ingestion перевіряє кількість, числові значення, скінченність,
ненульовість і однакову розмірність усіх vectors до mutation. Заміна всіх chunks
одного `(user_id, source_type, source_ref)` проходить однією transaction;
помилка Ollama або SQLite не залишає видалений чи частково оновлений індекс.
Скасування під час Ollama call зберігає попередній індекс; уже розпочатий DB write
у worker thread завершується атомарно, навіть якщо async caller скасовано.

Memory UI і API без explicit user використовують normalized `LUMEN_USER_ID`.
`/memory/delete` обмежує видалення turn заданим user scope, невалідний або
непозитивний ID повертає HTTP 400. Це scoping contract, не authentication layer.
Low-level embedding search вимагає непорожній `user_id` без implicit personal fallback.

Chat/connector, reminder recovery і calendar tasks утримуються в app registry.
Unexpected exceptions логуються з task label; shutdown скасовує й очікує всі
tracked tasks. Незавершений chat не зберігається як durable job для повтору.

## Reminder lifecycle

Немає старого planner/task orchestration layer. Один `GetContract` передає `ReminderService` typed дії `create`, `list`, `update`, `pause`, `resume`, `delete`, `snooze`, `select` і `confirm_bulk`; legacy `cancel_list_item` лишився compatibility path. Model лише витягає intent/selector/patch, а user-scoped matching, validation і side effects виконує state machine. Exact text або stable ID змінюються одразу; ambiguous і bulk selection використовують короткоживучу route-scoped SQLite session з token та revision preconditions. `delete` є soft cancel, calendar-projected roots read-only. Schema `v34` додає `next_attempt_at`, тому transport retry не змінює canonical `due_at`. Прострочення понад configured 30-minute grace не доставляється: one-shot стає `missed`, recurring переходить до наступного anchored slot. Silence або `no` також завершує occurrence без child; лише explicit `snooze` створює один one-shot child із persisted allowlisted route. List показує canonical slot окремо від explicit snooze/retry. Confirmation fired reminder лишається exact-correlated через `reminder_id` + `delivery_id`.

## Calendar creation

Явна команда додати подію обробляється model-driven контрактом `calendar.action=create`; regex і phrase heuristics не використовуються. Контракт містить лише `summary`, `description`, `location`, `all_day`, `start`, `end` і не може вибирати calendar entity. `CalendarActionService` викликає `calendar.create_event` через Home Assistant для configured primary entity рівно один раз, без availability/conflict checks і без проміжного SQLite запису. Для timed event без `end` застосовується configured default 60 хвилин; для all-day event без `end` використовується наступна exclusive date. Після успішного write Lumen перечитує current-day HA snapshot, щоб наявна reminder projection одразу побачила подію. Timeout або transport error має результат `unknown` і не повторюється автоматично через ризик дубліката.

## Delivery

- async `/chat` і `/connector` доставляють відповідь через `DeliveryService`;
- текстові (`voice=false`) Telegram callbacks понад 4096 UTF-16 units діляться на частини на межах абзаців/речень/слів (довге слово — за символами). Кожна частина надходить окремим послідовним callback зі спільним `request_id` та `metadata.part_index` / `part_count`; короткі відповіді й voice flow зберігають формат. Помилка частини припиняє надсилання і повертається caller; автоматичного повтору вже прийнятих частин немає. HA forwarder має обробляти кожен callback (режим `queued`, якщо попередня обробка ще триває). HTTP acceptance не підтверджує доставку в Telegram. Потрібні rebuild/restart та live перевірка HA forwarder;
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
