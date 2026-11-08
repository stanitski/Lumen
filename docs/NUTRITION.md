# Журнал харчування

Реалізація 2026-10-08. Оновлений packaged Lumen перебудовано й запущено;
health/OpenAPI HTTP 200 на `8010`, nutrition modules і schema підтверджені.
HA wiring застосовано 2026-10-08: два helper scripts, дві змінені automations
та раніше додану `telegram_sent` correlation (видалено 2026-10-18). `ha core check` і narrow script/automation
reload пройшли; HA REST → Lumen та loaded forwarder перевірено без meal writes
чи Telegram sending. Повний photo → CSV → Telegram flow ще потребує реального фото.

Спільна conversation-history інтеграція зібрана й запущена 2026-10-18; HTTP health 200,
packaged modules перевірені. Configured-model перевірка record → summary → correction
→ summary пройшла в ізольованому сховищі. Реальний Telegram follow-up ще потребує перевірки;
деталі та backup — `HANDOVER.md`.

## Як користуватися

Надішли одну фотографію їжі з необов'язковим підписом. Люмен оцінює ккал
усієї видимої порції й одразу записує її. Коментар має пріоритет над фото.
Фото без їжі не записуються; невизначена страва потребує короткого уточнення.
«З'їв лише половину» змінює останній прийом їжі цього чату. Reply на фото,
уточнення змінює пов'язаний запис. Нові прив’язки до підтверджень бота
не створюються після видалення окремої HA correlation automation 2026-10-18. Невідомий reply
ніколи не підміняється останнім записом. Нульова з'їдена частка дає 0 ккал.

Дата й час беруться з вихідного Telegram-повідомлення, а не завершення аналізу.
Інший явно вказаний день без часу потребує уточнення. Звичайне виправлення
зберігає дату/час; уточнення «половина» рахується від початкової повної порції.
Явне текстове прохання «додай/запиши …» створює новий прийом їжі без фото.
Якщо опису чи порції недостатньо, Люмен уточнює. Запит про калорійність без
прохання записати повертається у звичайний чат. Кожне фото альбому є окремим прийомом їжі.

## Потік і збереження

`/connector -> NutritionService -> OllamaConnector.chat_multimodal -> NutritionStore -> DeliveryService -> HA`

Фото обробляються background; відповідь на `/connector` — `202`, не підтвердження
запису. Текст проходить окрему structured класифікацію: `record` створює
новий запис, `update` виправляє цільовий, `clarify` з `new_entry=true` починає
уточнення нового запису без прив’язки до попередньої їжі. `unrelated` повертається до звичайного chat. Nutrition не додає
facts у SQLite і не змінює schema; user-facing відповіді про запис/виправлення
входять у спільну conversation history через `ConversationService`. Значення
журналу залишаються authoritative у per-user JSON, а CSV є лише проєкцією.
Nutrition не використовує Telegram MTProto.
Audio/video STT та `/chat`/Ollama-compatible ingress зберігають попередній flow.

У `NUTRITION_DIRECTORY` створюються `<user_slug>-<user_hash>.json` і `.csv`.
JSON — authoritative state: повна оцінка порції, з'їдена частка, стабільні ID,
pending уточнення, прив'язки фото/відповідей, receipts повторної доставки.
CSV — відновлювана проєкція, UTF-8 із BOM, коми, стандартне CSV quoting:

```csv
дата,час,калорії,опис
2026-10-08,13:23,520,Гречка з куркою
```

Описи, що починаються як spreadsheet formula, екрануються апострофом.
Не редагуй CSV вручну: зміни будуть замінені проєкцією з JSON.
Кожен файл замінюється атомарно після flush/fsync; JSON зберігається перед CSV.
При збої між ними CSV відновлюється під час наступного звернення до журналу.
До завершення обох записів успішне підтвердження не відправляється. Пошкоджений
JSON не перезаписується. Закритий для запису CSV, наприклад через Excel,
потребує звільнення файлу. Один процес Lumen володіє каталогом; per-user lock
серіалізує аналіз і запис. Multi-worker запуск для журналу не підтримується.

Dedup key — canonical user + chat ID + message ID; повторна доставка того самого
повідомлення не викликає новий аналіз/рядок. Повторне надсилання фото з новим
Telegram ID вважається новою їжею. Якщо callback HTTP не прийнято, повторна
доставка використовує збережений receipt. HTTP acknowledgement HA не гарантує
фактичну Telegram-доставку; `telegram_sent` лише прив'язує ID відповіді до запису.

## Конфігурація

| Setting | Default | Призначення |
|---|---|---|
| `NUTRITION_ENABLED` | `true` | фото й виправлення в Telegram connector |
| `NUTRITION_DIRECTORY` | `./data/nutrition` | JSON/CSV, relative to project root |
| `NUTRITION_MODEL` | порожньо | override; інакше dialogue model, потім основна model |
| `NUTRITION_TIMEZONE` | `Europe/Kyiv` | timezone повідомлення/CSV; Kyiv має bundled fallback |
| `NUTRITION_IMAGE_MAX_BYTES` | `10485760` | межа завантажених bytes |
| `NUTRITION_IMAGE_TIMEOUT_SECONDS` | `30` | загальний deadline завантаження |

Вибрана Ollama model повинна підтримувати vision. JPEG/PNG/WebP визначаються
за bytes, не лише filename/MIME. URL має той самий scheme/host/port, що
`HOME_ASSISTANT_URL`; redirects заборонені, HA-token передається лише цьому
origin. Фото не зберігається сервісом у журналі; поточний Ollama debug dump
може містити image payload. Помилка/неповний model JSON не створює запис.

## Контракт Home Assistant

`ConnectorRequest.data` для фото й текстових уточнень:

```json
{"message_id": 101, "reply_to_message_id": null, "sent_at": "2026-10-08T10:23:00+00:00"}
```

`sent_at` — timestamp із timezone або Unix seconds. Для `source="telegram"`
обов'язкові message ID, reply.target (chat ID), sent_at; для photo ще file.url.
Відсутні ID/час не замінюються випадковими значеннями. Caption передається в `text`.
Outgoing `kind="chat"`, `voice=false`, metadata містить `nutrition=true`,
`reply_to_message_id` і, для запису/уточнення, `message_tag`. Tag має форму
`nutrition:{"user_id":"...","entry_id":"..."}`. Це локальний HA tag, не
Telegram callback_data; canonical owner не береться з Telegram bot user_id.

Окрему `Lumen nutrition sent correlation` automation видалено з HA 2026-10-18
за вказівкою користувача. Нові `nutrition_delivery` події не надсилаються.
Сумісний ingress у Lumen збережено для раніше налаштованих інтеграцій;
звичайний чат, журнал, виправлення останнього запису та reply на вихідне фото
продовжують працювати. Нові reply на підтвердження бота не мають цієї прив’язки.
Додавання будь-яких нових HA automations потребує попереднього узгодження.

### Локальна підготовка HA

[Helper package](examples/home_assistant_nutrition.yaml) містить тільки два scripts; нових automations не додає. Він використовує наявний `rest_command.lumen_connector`.
Не додавай ще один Telegram ingress/webhook — це дублюватиме відповіді.

Для поточного HA також підготовлено повні replacement entries двох existing
automations у локальному `data/runtime/nutrition_ha_automations.yaml` (ignored).
Вони перебазовані на свіжий live YAML; user routing, wake/download і callback
cleanup збережені. Застосовані зміни замінюють лише відповідні два entries,
а helper scripts додано до `scripts.yaml` і sent automation до `automations.yaml`.
Нормалізовані Telegram metadata перевірено через HA template API, включно з
datetime → string. `ha core check` пройшов; real-photo delivery ще не перевірено.

Backup: `/homeassistant/backups/lumen-nutrition-20261008-115807/` містить
попередні `automations.yaml` та `scripts.yaml`. Локальні originals, patched YAML
і manifest — `data/runtime/ha-nutrition-deploy-20261008-115807/` (ignored).

1. Підключи package в HA або перенеси його scripts/automation у відповідні
   existing sections. У поточному `Lumen Connector` перед wake delay захопи:

   ```yaml
   - variables:
       nutrition_telegram_event: >-
         {{ dict(id=trigger.event.data.get('id'),
           reply_to_message_id=trigger.event.data.get('reply_to_message_id'),
           text=trigger.event.data.get('text') or trigger.event.data.get('caption') or '',
           date=(trigger.event.data.get('date') if trigger.event.data.get('date') is number
                 else (trigger.event.data.get('date') or trigger.event.time_fired) | string)) }}
       nutrition_received_at: "{{ trigger.event.time_fired.isoformat() }}"
   ```

2. В обох існуючих викликах `rest_command.lumen_connector` (attachment/default)
   заміни action на `script.lumen_connector_nutrition`. Поточний service data
   перенеси цілком під `connector`, зберігши user/reply/file/event_data:

   ```yaml
   - action: script.lumen_connector_nutrition
     data:
       telegram_event: "{{ nutrition_telegram_event }}"
       received_at: "{{ nutrition_received_at }}"
       connector:
         # Тут ті самі наявні поля source/event_type/user_id/reply_to/text,
         # command/args/event_data та file_* без зміни їхніх значень.
   ```

   `user_id` залишається canonical Lumen user з чинного конектора. Download
   photo через `telegram_bot.download_file` та file_url залишаються наявними.

3. У webhook callback `choose` ПЕРЕД загальною chat-гілкою додай:

   ```yaml
   - conditions:
       - condition: template
         value_template: "{{ trigger.json.metadata.get('nutrition', false) }}"
     sequence:
       - action: script.lumen_nutrition_reply
         data:
           lumen: "{{ trigger.json }}"
   ```

   Це взаємовиключна гілка того самого choose. Не додавай другий незалежний
   send action; наступний cleanup downloaded file має залишитися активним.
   Helper передає tag і reply ID до `telegram_bot.send_message`; sent automation
   повертає його ID до Lumen. Якщо HA має кілька Telegram bots, додай до send
   потрібний existing `config_entry_id` відповідно до його routing policy.

Офіційні контракти: [Telegram events](https://www.home-assistant.io/integrations/telegram_bot/),
[send_message/message_tag](https://www.home-assistant.io/actions/telegram_bot.send_message/).
Для наступних змін: backup HA YAML, `ha core check`, narrow reload;
rebuild/restart Lumen лише за зміни його коду. Поточні Lumen і HA вже оновлені.
Наступна ручна перевірка: одне фото, половина, reply до старішого фото й
підтвердження; перевір CSV та відсутність дублікатів.

## Перевірка source

`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest tests.test_nutrition tests.test_nutrition_api`

42 isolated nutrition/API tests; full project suite — 238 tests passed у
проєктному `.venv`. Перевірено syntax та локальний YAML/native Jinja rendering.
Системний Python без `trafilatura` не підходить для full suite; dependencies
не встановлювалися. Це не live inference чи перевірка калорійності реальної їжі.


## Виправлення та аудит 2026-10-18

Реальне фото від 12:28 було відхилено до Ollama: HA передавав URL із
`homeassistant.local`, тоді як `HOME_ASSISTANT_URL` містив IP того самого
сервера. DNS-еквівалентність не означає однаковий origin. Локальну конфігурацію
узгоджено з перевіреною адресою HA; origin guard і заборона redirects збережені.
Під час налаштування звіряйте scheme, hostname та port фактичного `file_url`
із `HOME_ASSISTANT_URL`, а не тільки доступність HA API.

Помилки журналу тепер містять безпечний `stage` у логах:
`load`, `metadata`, `download`, `analysis`, `save`. Повідомлення користувачу
розрізняють ці етапи; порада про Excel стосується тільки збереження.
Raw exception/URL/model output у ці повідомлення не потрапляють.

Перевірено код ingress, завантаження, structured analysis, JSON/CSV persistence,
повторної доставки, виправлень і callback correlation; також суміжний STT download.
У STT замінено URL-prefix перевірку HA-token на точний parsed origin;
HTTP failures та redirects повертають санітизований `SpeechToTextError`.
Це цільовий аудит connector/media flow, не повний аудит усіх модулів Lumen.

Додаткова live-перевірка того самого фото виявила timeout 240 s із default
thinking; із `think=False` модель відповіла швидко, але пропустила optional
`estimate`. Поле зроблено required nullable у JSON schema; для record/update
validator додатково вимагає повну оцінку. Structured nutrition calls тепер
передають `think=False`, як інші structured extraction services.

Після виправлення реальні HA download + Ollama + isolated JSON/CSV пройшли:
яєчня з двома яйцями, беконом та квасолею — приблизно 450 ккал;
reply «З’їв лише половину» змінив той самий рядок на 225 ккал.
Це перевірка механізму, а не точності калорійності. Production journal і
Telegram delivery у тесті не змінювалися. 49 focused tests passed після
останньої зміни; повний suite перед нею — 243 passed.
