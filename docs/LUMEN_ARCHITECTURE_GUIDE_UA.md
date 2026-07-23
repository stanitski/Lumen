# Lumen Architecture Guide (UA)

## Статус документа

Це короткий актуальний гід по поточній версії Lumen. Джерело істини тут: `src/lumen/main.py`, `src/lumen/services/chat_only.py`, `src/lumen/services/reminders.py`, `src/lumen/services/memory.py`, `src/lumen/services/knowledge.py`, `src/lumen/services/delivery.py`, `src/lumen/services/speech_to_text.py`.

Старі planner/skills/tasks/reminder docs не описують поточний runtime. Поточний reminder contract описує лише ізольований `ReminderService`.

## 1. Що таке поточний Lumen

Lumen це локальний FastAPI-сервіс, який:

- приймає текстові запити через `/chat` і `/connector`;
- вміє транскрибувати connector audio/video attachments;
- добирає пам'ять, knowledge і bounded external context;
- робить один основний Ollama dialogue call;
- зберігає turn і post-process memory;
- доставляє фінальну відповідь через Home Assistant callback або повертає її inline.
- записує та доставляє ізольовані reminders без planner/skills/tasks.

Практично це chat-memory-knowledge core, а не automation runtime.

## 2. Головна ментальна модель

Найкоротший live path:

1. Запит заходить у `/chat`, `/connector`, `/api/chat` або `/api/generate`.
2. `main.py` створює `request_id`, нормалізує `user_id` і callback metadata.
3. Якщо це connector attachment з audio/video, `SpeechToTextService` робить transcript.
4. `ChatOnlyService` збирає retrieval context через `MemoryService`.
5. `ContextExecutor` за потреби додає temporary Telegram/web/other read-only context.
6. `OllamaConnector` генерує фінальну відповідь.
7. Reminder intent або structured reminder confirmation обробляється `ReminderService` до Ollama; звичайний chat продовжується без змін.
8. Turn зберігається в SQLite.
9. `MemoryService` робить post-processing.
10. `DeliveryService` доставляє відповідь через Home Assistant, якщо це async path.

## 3. Основні модулі

### `src/lumen/main.py`

Роль:

- HTTP entrypoint;
- wiring сервісів;
- `/chat`, `/connector`, `/knowledge/upload`, `/memory`, `/api/chat`, `/api/generate`, `/api/tags`, `/api/show`.

Коли дивитися сюди:

- endpoint приймає не той payload;
- незрозуміло, який сервіс зараз є головним owner chat flow;
- треба перевірити inline vs callback path.

### `src/lumen/services/chat_only.py`

Роль:

- центральна orchestration точка поточного runtime;
- збір prompt messages;
- виклик Ollama;
- turn persistence;
- callback delivery;
- debug memory preview.

Коли дивитися сюди:

- Lumen відповідає дивно або не тим тоном;
- turn не зберігається;
- delivery відбулася/не відбулася не так;
- треба зрозуміти, який саме prompt бачить Ollama.

### `src/lumen/services/memory.py`

Роль:

- resolve user identity;
- retrieval context для facts, dialogue, knowledge;
- query contract / retrieval policy;
- persistence helpers;
- post-processing turn metadata і fact extraction.

Коли дивитися сюди:

- Lumen пам'ятає не те;
- в prompt потрапляє noisy context;
- debug-search не збігається з живою відповіддю.

### `src/lumen/services/context_executor.py`

Роль:

- один extension point для external read-only context;
- валідація source/operation pair;
- dispatch на Telegram, web, Home Assistant, knowledge handlers;
- форматування external context у prompt-friendly shape.

Коли дивитися сюди:

- Lumen каже, що “подивився” щось зовнішнє, але evidence не сходиться;
- треба перевірити, який external context реально виконався;
- треба додати новий bounded read-only source.

### `src/lumen/services/telegram_context.py`

Роль:

- bounded read-only Telegram lookup;
- доступ лише через дозволені `messenger_context` facts;
- optional STT reuse для Telegram audio/video.

Коли дивитися сюди:

- explicit Telegram lookup не знаходить потрібний source;
- переплутано `kind=user|group|channel`;
- date range або history window працює не так.

### `src/lumen/services/web_search.py`

Роль:

- bounded DuckDuckGo retrieval;
- temporary cited web context для final answer prompt.

Коли дивитися сюди:

- web evidence відсутній або обрізаний;
- timeout підозрюється саме на web path;
- Lumen цитує URL, якого не було в context results.

### `src/lumen/services/knowledge.py`

Роль:

- knowledge upload;
- chunking;
- embedding/indexing;
- retrieval support для knowledge context.

Коли дивитися сюди:

- файл не індексується;
- knowledge upload завершився, але retrieval не бачить chunks;
- треба зрозуміти межу між uploaded knowledge і ordinary dialogue memory.

### `src/lumen/services/speech_to_text.py`

Роль:

- STT для connector audio/video attachments;
- `ffmpeg` normalization;
- `faster-whisper` inference.

Коли дивитися сюди:

- `/connector` прийняв attachment, але transcript порожній;
- підозра на `ffmpeg`, mime-type або Whisper config problem.

### `src/lumen/services/delivery.py`

Роль:

- outbound callback у Home Assistant;
- transport-only layer;
- не генерує answer, лише доставляє її.

Коли дивитися сюди:

- відповідь є в логіці, але не доходить до каналу;
- треба перевірити payload, webhook path, bearer/token behavior.

### `src/lumen/storage/db.py` і `src/lumen/memory/store.py`

Роль:

- схема SQLite;
- low-level persistence і retrieval queries.

Коли дивитися сюди:

- є підозра на schema drift;
- turn/fact/embedding не зберігається;
- memory manager показує не те, що очікувалось.

## 4. Що більше не є поточною архітектурою

У current runtime не треба виходити з припущення, що активні:

- `ChatService`
- `SkillPlanner`
- `SkillRegistry`
- `PlanExecutionService`
- `ActionExecutor`
- `TaskScheduler`
- `smart_home_control` runtime path

Ці назви можуть лишатися в історичних документах, changelog або старому коді, але вони не описують поточну головну execution path.

## 5. Як читати проблему правильно

### Якщо “Lumen не відповідає”

Починати з:

1. `src/lumen/main.py`
2. `src/lumen/services/chat_only.py`
3. `src/lumen/services/delivery.py`

### Якщо “Lumen пам'ятає не те”

Починати з:

1. `src/lumen/services/memory.py`
2. `src/lumen/memory/store.py`
3. `src/lumen/storage/db.py` тільки якщо вже є підозра на schema/data contract

### Якщо “Telegram/web context дивний”

Починати з:

1. `src/lumen/services/context_executor.py`
2. `src/lumen/services/telegram_context.py` або `src/lumen/services/web_search.py`
3. `src/lumen/services/chat_only.py`

### Якщо “voice/video attachment не працює”

Починати з:

1. `src/lumen/main.py`
2. `src/lumen/services/speech_to_text.py`
3. `src/lumen/services/chat_only.py`

## 6. Що важливо не припускати

- що planner обирає skill;
- що старі planner/tasks runtime paths активні;
- що smart-home control є live runtime feature;
- що Telegram messages зберігаються в пам'ять як chat history;
- що knowledge upload і ordinary dialogue memory це одна й та сама підсистема.

## 7. Reminders

`ReminderService` зберігає one-time, daily та weekly occurrence у власній SQLite схемі. Він створює одну Windows wake task на найближчий due або confirmation deadline, доставляє reminder через Home Assistant та приймає лише корельоване підтвердження `reminder_confirmed:<id>:<delivery_id>`. Старий callback без `delivery_id` відхиляється, бо не може безпечно завершити occurrence.
