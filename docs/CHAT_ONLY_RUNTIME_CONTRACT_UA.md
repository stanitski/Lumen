# Chat-Only Runtime Contract

## Призначення

Поточний Lumen працює як chat-memory-knowledge runtime без planner/skills/tasks/reminders.

Активний шлях:

`Ingress -> ChatOnlyService -> MemoryService context -> optional ContextExecutor reads -> Ollama dialogue -> SQLite persistence -> DeliveryService`

## Що зараз активно

- текстовий чат через `/chat`, `/connector`, `/api/chat`, `/api/generate`;
- STT для connector audio/video attachments;
- dialogue memory і profile facts у SQLite;
- knowledge retrieval як prompt context;
- bounded external context через `ContextExecutor`;
- callback delivery через Home Assistant;
- memory manager і debug search.

## Що зараз не є runtime-фічами

- planner, skills, execution plans;
- reminders, tasks, schedulers, wake timers;
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

`ContextExecutor` виконує лише read-only context requests:

- `telegram/search`
- `web/search`
- `home_assistant/read_state`
- `knowledge/search`

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

Немає окремого reminder/task/attachment orchestration layer.

## Delivery

- async `/chat` і `/connector` доставляють відповідь через `DeliveryService`;
- inline `/api/chat` і `/api/generate` повертають той самий semantic result без callback;
- delivery failure не має переписувати semantic answer, лише фіксується окремо в result metadata/logs.

## Правило повторного розширення

Якщо optional capability повертається, її треба додавати як окрему, ізольовану integration поверх current core.

Не вважати старі planner/reminder/task docs поточним runtime contract.
