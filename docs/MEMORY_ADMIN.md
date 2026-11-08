# Адміністрування пам’яті Lumen

Сторінка: `http://127.0.0.1:8010/memory` (порт береться з `LUMEN_PORT`).
Реалізація: `main.py` → `services/memory_admin.py` → наявні
`MemoryStore`, `FactContextService` і `ChatOnlyService`.
Інтерфейс міститься в імпортованому `memory_admin_ui.py`; зовнішніх UI-залежностей немає.

## Факти

Сторінка завантажує всі доступні факти configured `LUMEN_USER_ID` та global
через keyset paging сховища. Фільтрування за текстом, категорією і типом
відбувається у браузері. Global записи доступні лише для читання;
`secret`/`credential` не показуються і не редагуються.

Деталі містять джерело; `turn:N` та `dialogue:turn:N` відкривають відповідний
збережений діалог, якщо він існує. `chat:...` показується як source reference;
довільний ID діалогу за ним не вгадується.

Редагуються `category`, `subject`, `predicate`, `value`, `inclusion`.
Наявні `confidence`, `importance`, `tags`, `source_ref`, `expires_at` зберігаються.
`mandatory` означає standing rule спілкування; `messenger_context`,
`device_alias` та global записи не можуть отримати цей статус через адмінку.
Видалення потребує підтвердження конкретного запису в інтерфейсі.

Кожен запис має `revision`: SHA-256 з persisted полів. Сховище перевіряє її
в одній `BEGIN IMMEDIATE` transaction із редагуванням/видаленням. HTTP `409`
означає, що дані вже змінилися; треба перечитати факт і перевірити зміни.
Зміна факту атомарно видаляє його старий embedding. Далі виконується наявний
`FactContextService.sync_index()`; помилка індексації явно показується окремо
від успішного збереження. До відновлення embedding діє lexical fallback.
Видалення прибирає факт і його index в одній transaction.

## Пошук і перевірка відповіді

«Пошук» будує той самий контекст, що звичайний chat, без фінальної відповіді.
Це може викликати Ollama для routing/embeddings і дозволені read-only
зовнішні context adapters. «Перевірити відповідь» додатково викликає dialogue
model з побудованими messages. Reminder/calendar actions не виконуються;
preview не записується в conversation history, не виділяє нових фактів і
не доставляється через Home Assistant/Telegram.

«Пам’ять, передана для відповіді» — знімок selected facts, retrieved dialogues,
bounded recent context, knowledge hits і external context саме цього запиту.
Технічні details містять фактичні prompt messages. Це свідчить про наданий
контекст, а не доводить причинний зв’язок окремого факту з фразою моделі.

Редагування зі знімка використовує його `revision`, а не непомітно підмінений
актуальний факт. Після зміни знімок позначається застарілим. Повторний запит
створює нову відповідь; попередня залишається для порівняння в поточній вкладці.
Preview не є реплікою live діалогу: повторні питання слід формулювати самодостатньо.

## Доступ та API

Нова адмінка і `/memory/admin/*` доступні лише для loopback peer та Host
`127.0.0.1`, `localhost` або `::1`. Cross-origin / cross-site запити
відхиляються; відповіді не кешуються. Це локальний operator interface,
не багатокористувацька система авторизації. Legacy memory endpoints збережені
сумісними; їхній історичний access contract цим не змінюється.

- `GET /memory/admin/items` — власник, лічильники, факти.
- `GET /memory/admin/facts/{id}` — факт та доступний source turn.
- `PATCH /memory/admin/facts/{id}` — усі editable поля плюс `revision`.
- `DELETE /memory/admin/facts/{id}` — JSON `{ "revision": "..." }`.
- `POST /memory/admin/preview` — JSON `{ "text": "...", "generate_answer": true }`.

Scope визначається серверним `LUMEN_USER_ID`, не довільним user ID із браузера.
Лічильник knowledge рахує indexed chunks, а не унікальні файли.
Схема SQLite не змінюється. Зміна source потребує rebuild/restart packaged EXE;
саме редагування файлів не оновлює вже запущену версію.

## Перевірки

`python -m unittest tests.test_memory_admin tests.test_memory_admin_storage tests.test_fact_storage`
використовує isolated DB та вимкнені external integrations.
Browser QA також має працювати на окремій БД: не змінювати реальну пам’ять
для перевірки кнопок. Live deployment і перевірка реальної Ollama звітуються окремо.
