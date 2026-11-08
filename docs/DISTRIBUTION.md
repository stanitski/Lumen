# Lumen: перший запуск на Windows

Це starter-пакет поточного вихідного коду, а не автономний EXE чи offline installer.
Він створює окрему пам’ять нового користувача. Особисті бази, історія, документи,
Telegram-сесії, журнали, `.env` і встановлені моделі автора до ZIP не входять.

## Підготовка

1. Встановіть Python 3.11+ із Python Launcher (`py`). Ollama встановлюється на кроці 5.
2. Розпакуйте ZIP у власну папку з правом запису. Не запускайте із самого ZIP.
3. Запустіть `SETUP.bat` з командного рядка. Він створить `.venv`, встановить
   залежності з інтернету та скопіює `.env.example` у `.env`, якщо `.env` ще немає.
   Установлення залежностей не зафіксоване lock-файлом; чиста установка на іншому
   комп’ютері потребує окремої перевірки. `python-multipart` встановлюється додатково,
   бо він потрібен поточним FastAPI File/Form endpoints, але відсутній у pyproject.
4. Відредагуйте `.env`: задайте власний `LUMEN_USER_ID`, `OLLAMA_MODEL` і
   `OLLAMA_EMBEDDING_MODEL`. Залиште `LUMEN_HOST=127.0.0.1` для локального запуску.
   Налаштування беруться з `.env`; змінювати Python-код не потрібно.
5. Встановіть Ollama та завантажте моделі з `.env` через PowerShell:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\SETUP_OLLAMA.ps1
   ```

   Скрипт використовує наявну Ollama, а за її відсутності — `winget install --id
   Ollama.Ollama --exact --source winget`. Без winget встановіть Ollama через
   [офіційний інсталятор](https://ollama.com/download/windows) і повторіть запуск.
   Він перевіряє локальний API, за потреби запускає `ollama serve` приховано та
   виконує `ollama pull` для кожної унікальної моделі з конфігурації. Збій установки
   чи завантаження завершує скрипт помилкою; повторний запуск дозволений.
   Це завантаження з інтернету, яке може потребувати багато місця й часу.
   Скрипт не встановлює Ollama на віддалений сервер і не змінює системну execution policy.

   Ручна альтернатива після встановлення й запуску Ollama:

   ```powershell
   ollama pull <значення_OLLAMA_MODEL>
   ollama pull <значення_OLLAMA_EMBEDDING_MODEL>
   ```

   Якщо задано `OLLAMA_DIALOGUE_MODEL`, завантажте також цю модель.
   Не вводьте кутові дужки буквально. Шаблон містить назви моделей поточного
   проєкту; доступність у Ollama й придатність для вашого обладнання перевіряються
   на цільовому комп’ютері. Вимоги до RAM/VRAM залежать від вибраних моделей.
6. Запустіть `START.bat`. Зупинка — `Ctrl+C` у його вікні.

## Перевірка та спілкування

Відкрийте `http://127.0.0.1:8010/` для перевірки API, а
`http://127.0.0.1:8010/docs` — для інтерактивних запитів. Якщо змінили порт,
використовуйте його. Окремого графічного чат-клієнта цей пакет не містить.

Для першої відповіді в іншому PowerShell-вікні:

```powershell
$body = @{ prompt = 'Привіт! Розкажи, чим ти можеш допомогти.'; stream = $false } | ConvertTo-Json
$result = Invoke-RestMethod -Uri 'http://127.0.0.1:8010/api/generate' -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$result.response
```

Це inline endpoint: базове спілкування не потребує Home Assistant.
Перший запит може бути тривалим через завантаження моделей.
Пам’ять створюється в `data/lumen.db`; локальні журнали — у `data/debug`.
Для резервної копії зупиніть Lumen і збережіть `.env` та папку `data` приватно.

## Додаткові інтеграції

- Home Assistant: власні URL, token, callback automation/webhook і, за потреби,
  calendar entities. Шаблон не підключає інсталяцію автора. `/chat` та `/connector`
  використовують callback delivery; для них треба налаштувати доставку.
- Telegram ingress: потрібен власний бот і маршрут через Home Assistant connector.
  Цей пакет не створює бота чи HA automation. Telegram context lookup є окремим
  механізмом: власні API credentials, локальна авторизована MTProto session та
  дозволені джерела. Готового майстра авторизації пакет не містить.
- Голос: потрібна Whisper-модель; перше використання може завантажити її.
  `FFMPEG_PATH` задає наявний ffmpeg, який не входить до ZIP.
- Харчування початково вимкнене. Потрібні `NUTRITION_ENABLED=true`, vision-модель
  та налаштований Telegram/HA connector.
- Нагадування потребують доставки через HA. На Windows використовують Task Scheduler
  (`LumenReminderWake`); перевіряйте їх окремо після підключення HA. На одному Windows
  обліковому записі ця назва спільна для інсталяцій.

API не має окремої автентифікації. Для першого запуску він слухає лише localhost;
мережевий доступ потребує окремо налаштованого захисту. При підключенні HA з іншого
пристрою потрібно свідомо налаштувати адресу прослуховування та мережевий доступ.
Розклад нагадувань і календаря зараз використовує Kyiv timezone; це не універсальна
збірка для довільного часового поясу.

## Для розробника пакета

Цей розділ описує створення ZIP у вихідному репозиторії; сам пакувальник не входить
до starter ZIP. Для налаштування отриманої копії та EXE дивіться наступні розділи.

```powershell
python scripts/package_starter.py <шлях_до_нового_ZIP>
```

Пакувальник бере лише Python-файли `src/lumen`, bundled timezone, `pyproject.toml`,
нейтралізований `.env.example` та згенеровані launchers/цю інструкцію.
Він не запускає сервер, не встановлює залежності й не читає runtime `.env` або дані.
Архів відображає поточний робочий source, включно з незакоміченими змінами.
Він не включає старий `dist/Lumen.exe` чи прив’язаний до авторського диска `compile.bat`.

## Інструкція агенту: персональне налаштування

Спочатку прочитайте `src/lumen/config.py`, wiring у `src/lumen/main.py` та потрібний
service. Не переносіть значення з чужої інсталяції. Власник вводить секрети локально
у `.env` або в інтеграцію HA; не просіть вставляти їх у чат і не друкуйте `.env`.
Змінні середовища процесу мають пріоритет над `.env`: перевіряйте конфлікти без
виведення секретних значень. SETUP не перезаписує наявний `.env`.

### Обов’язкове для базового чату

| Що | Налаштування і контракт |
|---|---|
| Користувач | `LUMEN_USER_ID`: стабільний власний ID. Це ключ пам’яті, а не пароль і не Telegram chat ID. Не змінюйте після накопичення пам’яті без плану перенесення. Передавайте той самий ID у connector, knowledge uploads та персональних запитах. `OLLAMA_COMPAT_USER_ID` не використовується поточним `_user_id()`; не покладайтеся на нього. |
| Моделі | `OLLAMA_MODEL` для службових structured calls, `OLLAMA_DIALOGUE_MODEL` для відповідей (порожній = основна), `OLLAMA_EMBEDDING_MODEL` для embeddings. Потрібні всі налаштовані моделі, а не лише чатова. Шаблон: `gemma4:e4b` та `embeddinggemma`; агент перевіряє їх доступність і роботу на обладнанні власника. |
| Ollama | `OLLAMA_BASE_URL`, початково `http://127.0.0.1:11434`. Для локальних моделей не потрібен OpenAI API key або Telegram token. `SETUP_OLLAMA.ps1` завантажує моделі з цього `.env`. |
| Файли | `LUMEN_DB_PATH`, `LOG_FILE_PATH`, `OLLAMA_DEBUG_DUMP_PATH`, `KNOWLEDGE_PATHS`. За замовчуванням дані нові й локальні. Не підставляйте диски/папки автора. |
| API | `LUMEN_HOST`, `LUMEN_PORT`. Початково localhost. Окремих облікових записів/паролів API немає; `user_id` не перевіряє особу. |

Без `.env` код має авторські fallback `VST` та webhook ID. Тому власний `.env`
обов’язковий і для source, і для EXE. Це значення у коді, а не перенесена пам’ять.

### Як створюється база

`main.py` викликає `Database(...).init()` при завантаженні застосунку.
`storage/db.py` створює батьківську папку, SQLite-файл, таблиці та schema version;
окремий SQL-сервер, пароль БД або ручне створення таблиць не потрібні.
Для нового шляху база порожня: без фактів, діалогів, нагадувань і knowledge автора.
Для наявної бази виконується чинна initialization/migration логіка — не підміняйте
її копіюванням чужої бази. Перед роботою з власною наявною базою зробіть backup.
Харчування створює окремий JSON/CSV journal, коли функцію ввімкнено й отримано записи.

### Home Assistant і Telegram

1. Власний `HOME_ASSISTANT_URL`; власний довготривалий access token у
   `HOME_ASSISTANT_TOKEN` для calendar REST/WebSocket. Створіть його в профілі
   власного користувача HA. Не використовуйте token автора.
2. Створіть callback automation у HA з webhook ID із **вашого** `.env.example`:
   пакувальник генерує новий ID для кожного ZIP. Або задайте власний випадковий ID.
   Lumen складає `<HOME_ASSISTANT_URL>/api/webhook/<HOME_ASSISTANT_WEBHOOK_ID>`;
   `HOME_ASSISTANT_WEBHOOK_URL`, якщо непорожній, перекриває цей URL.
   Webhook delivery не передає Bearer token; ID webhook зберігайте приватно.
   HA callback automation має обробляти `HomeAssistantCallbackPayload` із
   `src/lumen/models.py`, відправляючи `answer` у власний канал/чат; сам webhook
   ще не забезпечує Telegram delivery. Перевірте також voice/chunked payload,
   reminders і їх підтвердження, якщо ці функції потрібні.
3. Для Telegram створіть власного бота через BotFather і підключіть його у HA.
   Bot token належить конфігурації HA, окремої змінної `TELEGRAM_BOT_TOKEN` у Lumen
   немає. У HA обмежте дозволені user/chat IDs. Зовнішній відправник не повинен
   довільно задавати чужий `user_id` або ціль доставки.
4. Налаштуйте власний HA REST/connector маршрут до `POST /connector`.
   Контракт — `ConnectorRequest` у `src/lumen/models.py`: `source=telegram`,
   `user_id` = власний Lumen ID, `text` та `reply.target` = фактичний Telegram chat ID.
   Для голосу/фото потрібні також file metadata, доступна Lumen URL і відповідні
   handlers у `main.py`. Не використовуйте авторські chat IDs, адреси WoL,
   сенсори живлення, entity IDs чи скрипти пробудження комп’ютера. Готова повна HA
   конфігурація не входить до пакета; агент створює її за контрактами цієї копії.
5. Calendar опціональний: `HOME_ASSISTANT_CALENDAR_ENTITY_IDS` (через `;`),
   `HOME_ASSISTANT_CALENDAR_WRITE_ENTITY_ID`, `HOME_ASSISTANT_CALENDAR_TARGET_CHAT_ID`.
   Вкажіть власні календарі й чат. Read і write вмикаються окремо; перевірте,
   що token має потрібний доступ. Поточний read projection охоплює поточний Kyiv day.

Telegram context lookup через Telethon — окремо від Telegram-бота:

- Отримайте власні `TELEGRAM_API_ID` і `TELEGRAM_API_HASH` через
  [Telegram API development tools](https://my.telegram.org).
- Задайте власний `TELEGRAM_SESSION_PATH`. Авторизуйте локально під своїм номером
  та 2FA, не через чат із агентом. Приклад у корені розпакованої папки після SETUP:

  ```powershell
  $env:PYTHONPATH = Join-Path (Get-Location) 'src'
  .\.venv\Scripts\python.exe -c "from pathlib import Path; from lumen.config import Settings; from telethon.sync import TelegramClient; s=Settings(); Path(s.telegram_session_path).parent.mkdir(parents=True, exist_ok=True); c=TelegramClient(s.telegram_session_path,s.telegram_api_id,s.telegram_api_hash); c.start(); c.disconnect()"
  ```

- `.session` дає доступ до акаунта: зберігайте приватно, не додавайте до ZIP/git.
- Новий allowlist порожній. Канали/групи додавайте лише з дозволу власника через
  чинний `MemoryStore.add_fact()` для його `user_id`: category=`messenger_context`,
  predicate=`uses`, tags містять `telegram` і `search_source`, value є JSON з
  `ref` і `kind` (`channel`/`group`/`user`). Перевірте точну signature в коді;
  subject має розрізняти джерела. Не імпортуйте авторський список чи весь акаунт.
- Явний `@username` у поточному запиті дозволяє лише разове bounded читання
  особистого `User`; він не дозволяє довільне читання каналів/груп.

### Пам’ять, обладнання та додаткові параметри

Ім’я, мова відповідей, звертання, правила спілкування, інтереси/небажані новини,
дозволені джерела, документи та історія належать власнику нової копії.
Попросіть його сформулювати власні правила через діалог і перевірте збереження
через `/memory`; не записуйте вигаданий профіль. Мова prompt/examples і Kyiv
timezone є властивостями поточного коду, а не автоматично визначеною локацією.

STT: `STT_MODEL`, `STT_DEVICE`, `STT_COMPUTE_TYPE`, `STT_LANGUAGE`, `FFMPEG_PATH`.
Початковий CPU/int8 не потребує налаштування CUDA; GPU Whisper перевіряйте окремо.
Nutrition: `NUTRITION_ENABLED`, `NUTRITION_MODEL`, `NUTRITION_DIRECTORY`,
`NUTRITION_TIMEZONE`; потрібна vision-модель. Для Feedreader задайте власний
зовнішній виклик `/news/process`; credentials Feedreader не є налаштуваннями Lumen.
Тривалість контексту, timeout/keep-alive та reminder grace/snooze мають початкові
значення в шаблоні; змінюйте лише за потребою власника.

## Ollama: сервер, обладнання та перевірка

Скрипт використовує локальний HTTP сервер за `OLLAMA_BASE_URL`; remote Ollama
налаштовується окремо. Ollama не читає `.env` Lumen: її серверні параметри задають
у середовищі Windows. Для іншого диска моделей задайте власний `OLLAMA_MODELS`,
закрийте Ollama з tray та запустіть знову. Не встановлюйте авторські шляхи.
За потреби `OLLAMA_HOST` сервера має відповідати `OLLAMA_BASE_URL` клієнта;
за замовчуванням зберігайте сервер локальним на `127.0.0.1:11434`.

Ollama сама обирає підтримувану GPU/CPU. Перевірте драйвери за офіційною документацією;
після першого запиту `ollama ps` показує `PROCESSOR`. Не обіцяйте GPU acceleration
лише на підставі наявності відеокарти. На слабшому ПК оберіть модель, яка підтримує
потрібні structured responses; перевірте окремо chat, embeddings і vision.
Проста заміна embedding-моделі не переіндексує вже збережені документи автоматично.

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/tags
ollama list
```

Потім виконайте перший `/api/generate` із розділу вище й перевірте повторний запит
на пам’ять. Ollama API і Lumen API — різні порти. Успішна перевірка health/schema не доводить
працездатність моделей; потрібна фактична відповідь. Для embeddings завантажте
невеликий власний тестовий документ через `/knowledge/upload` та перевірте retrieval.

Офіційні джерела: [Ollama Windows](https://docs.ollama.com/windows),
[Ollama FAQ: server settings, model location, GPU](https://docs.ollama.com/faq).

## Компіляція іншим агентом

На Windows після `SETUP.bat`:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\BUILD.bat
```

`BUILD.bat` використовує поточну папку, включає `lumen/data` та Trafilatura,
не включає `.env`, БД, сесії чи моделі і не задає авторського runtime temp path.
Це перенесена команда чинної збірки, її результат потрібно перевірити у своєму
середовищі. На цьому етапі пакет не містить уже перевіреного нового EXE.

Збережіть структуру:

```text
Lumen/
  .env
  data/             # створюється під час запуску
  dist/
    Lumen.exe
```

Поточний `_project_root()` у frozen mode використовує `exe.parent.parent`.
Не переміщуйте EXE просто в корінь або окрему папку без відповідного `.env` рівнем
вище. Для EXE більше не потрібен Python, але потрібні Ollama, моделі та інтеграції.

Перевірте спочатку `dist\Lumen.exe --timezone-smoke` у **окремій порожній staging
папці**, з її `.env` і такою ж структурою `dist/`: навіть timezone-smoke імпортує
`main.py` і створює БД/лог. Потім health/OpenAPI, фактичну відповідь Ollama, memory
та потрібні інтеграції. Не запускайте дві копії на одному порту/БД.
Повний startup також запускає reminder recovery; для ізольованої перевірки не
використовуйте робочу БД чи HA callback. Зафіксуйте версії Python, Ollama, моделей,
PyInstaller та установлених залежностей і повідомте, що перевірено локально,
що на цільовому ПК, а що лишається TBD.
