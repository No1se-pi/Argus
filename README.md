# Argus

### Подключение ВК с телефона

В меню лайков есть «🔐 Подключить / обновить ВК». В личном чате администратора
бот выдаёт ссылку на 10 минут. Она открывает мобильную панель удалённого браузера
на вашем сервере. Пользователь сам входит на настоящем сайте VK внутри этого
браузера, при необходимости вводит код/пароль или проходит проверку ВК.
Argus наблюдает только веб-токены запросов к `web.api.vk.ru/method/`, проверяет
`wall.get`, `likes.getList` и `users.get`, затем сохраняет рабочий токен в SQLite.
Бот получает новый токен сразу, без перезапуска. Весь сбор остаётся HTTP-запросами;
браузер используется только во время подключения и закрывается после успеха,
отмены или истечения ссылки. Сохранённый профиль позволяет повторно подключаться
без ввода пароля, пока это разрешает сессия ВК. Бесшовный вход не гарантируется.

На сервере установите дополнительный компонент и Chromium:

```bash
.venv/bin/pip install '.[vk-auth]'
.venv/bin/python -m playwright install chromium
# При отсутствии системных библиотек (нужны права администратора):
sudo .venv/bin/python -m playwright install-deps chromium
```

В `.env`:

```dotenv
VK_AUTH_PUBLIC_URL=https://kaiton.ru
VK_AUTH_PORT=8788
VK_AUTH_BIND_HOST=127.0.0.1
```

Помощник отключён, пока `VK_AUTH_PUBLIC_URL` пуст. TLS завершается на reverse proxy.
Маршрут `/vk-connect*` должен целиком передаваться помощнику без удаления префикса.
Для vavilon бот работает в VPN namespace `botvpn`, поэтому используйте
`VK_AUTH_BIND_HOST=10.254.42.2`, а Caddy направьте на `10.254.42.2:8788`.
Пример, сохраняющий текущий сайт на порту 4000: [deploy/vavilon.Caddyfile](deploy/vavilon.Caddyfile).
Перед перезагрузкой конфигурации выполните `caddy validate --config ...`.
Не открывайте порт помощника в интернет напрямую.

Секрет ссылки находится во fragment URL и не попадает в HTTP access log. После
первого открытия окно привязано к Secure/HttpOnly cookie этого браузера. Новая
ссылка отменяет предыдущую. Пароль/коды не сохраняются приложением и не отправляются
боту; профиль браузера и токен содержат доступ к аккаунту и хранятся рядом с БД
в игнорируемой папке данных. Не включайте запись тел HTTP-запросов на прокси.

Для Telegram Bot API при необходимости можно отдельно задать `BOT_PROXY_URL`
(HTTP или SOCKS5) и установить `pip install '.[proxy]'`. Этот параметр не меняет
маршрут запросов VK и браузера. Если Telegram уже работает через `botvpn`, оставьте
его пустым и сохраните существующий запуск через VPN namespace.

### Сбор лайков через веб-запросы ВК

Опциональный `VK_WEB_ACCESS_TOKEN` в локальном `.env` включает для ручных отчётов
запросы к `https://web.api.vk.ru/method` по образцу сетевых запросов сайта ВК
(POST, версия 5.289, client_id веб-клиента 6287487). Это отдельный режим с токеном
сайта, а не OAuth-токен собственного приложения Argus. Cookies не сохраняются.
Приоритет ключей отчёта: веб-токен → сервисный ключ → пользовательский токен.
После изменения `.env` перезапустите бот. При отказе веб-токена сбор останавливается,
предыдущий отчёт сохраняется; скрытого переключения на другие ключи нет.
Проверка доступа и определение профиля используют тот же режим, что и сбор.
Автоматическое получение/обновление веб-токена не реализовано: после истечения
или отзыва нужен новый токен из собственного сеанса сайта. Этот внутренний
интерфейс может меняться; режим не является официальной интеграцией VK ID.

Если вход через VK ID прошёл, но `likes.getList` возвращает код 1051
(метод недоступен текущему типу профиля), можно проверить **сервисный ключ своего
приложения**: официальная страница `likes.getList` перечисляет этот тип ключа
как допустимый. Это не токен сообщества и не защищённый ключ приложения.
Добавьте его локально в `.env` как `VK_SERVICE_ACCESS_TOKEN=...` и перезапустите бот.
Для отчётов лайков и определения профилей этот ключ имеет приоритет над
пользовательским; фоновые мониторы сохраняют прежние настройки токенов.
Кнопка «Проверить доступ ВК» проверяет `wall.get`, затем на одном посте
`likes.getList`, затем `users.get`. Если постов нет, проверка лайков не считается
успешной. Доступ конкретному приложению подтверждается только результатом API;
при повторном отказе уточните доступ к методу у поддержки VK API.

Описание метода: https://dev.vk.ru/ru/method/likes.getList.

### Локальный вход через своё приложение VK ID

Для публичного приложения Web в кабинете VK ID укажите базовый домен `localhost`
и доверенный redirect URL `http://localhost` (без порта). Домен для этого не нужен.
Официальная документация разрешает локальное HTTP-тестирование на порту 80.
Адрес репозитория GitHub не является обработчиком авторизации.

На компьютере, где будет открыт браузер, выполните:

```powershell
.\.venv\Scripts\python -m app.vk_login --client-id YOUR_APP_ID
```

Команда на пять минут запускает обработчик только на loopback-интерфейсах порта 80,
открывает VK ID и обменивает код на токены с PKCE и проверкой `state`.
Пароль и подтверждение входа вводятся на стороне ВК. Для публичного приложения
защищённый и сервисный ключи этой команде не нужны.
Пользовательский токен записывается в `.env`, а комплект с refresh-токеном —
в `vk_id_tokens.json` рядом с базой данных. Оба исключены из Git.
Команда заменяет предыдущий `VK_USER_ACCESS_TOKEN`; перезапустите бот после входа.
Если порт 80 занят, сначала освободите его; другой HTTP-порт документация VK ID
для localhost не поддерживает.

Для обновления сохранённого токена вручную:

```powershell
.\.venv\Scripts\python -m app.vk_login --refresh
```

Автоматическое обновление во время работы бота пока не подключено.
По умолчанию запрашивается только `vkid.personal_info`. Дополнительные доступы
можно передать через `--scope "..."`, только если они разрешены в кабинете приложения.
Успешный вход не доказывает доступ к `wall.get` и `likes.getList`: после входа
используйте кнопку проверки доступа ВК в боте, затем пробный сбор одного поста.

Документация: [настройка приложения](https://id.vk.ru/about/business/go/docs/ru/vkid/latest/vk-id/connection/create-application),
[авторизация без SDK](https://id.vk.ru/about/business/go/docs/ru/vkid/latest/vk-id/connection/start-integration/auth-without-sdk/auth-without-sdk-web).

Argus is a Python 3.10+ Telegram control bot for social monitoring, alerts, and dashboards.
Python 3.11 is still the recommended runtime and the Docker image uses it.

The management UI is always the Telegram bot. Monitors are independent modules:

- VK Monitor - priority module for VK community posts and comments.
- Telegram Monitor - optional Telethon user-session monitor.
- Bot UI - core control panel through commands and inline buttons.

Argus is designed to start even when VK or Telethon is not configured. If `BOT_TOKEN`
and `ADMIN_IDS` are present, the control bot should run.

## Modes

- `FULL_MODE` - VK Monitor and Telegram Monitor are available.
- `VK_ONLY` - only VK Monitor is available.
- `TG_ONLY` - only Telegram Monitor is available.
- `CONTROL_ONLY` - only Bot UI, database, and setup/status screens are available.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m app.main
```

Minimum `.env` for Bot UI only:

```env
BOT_TOKEN=123456:replace_me
ADMIN_IDS=123456789
ENABLE_BACKGROUND_MONITORS=false
ENABLE_VK_MONITOR=true
ENABLE_TELEGRAM_MONITOR=false
```

Then open Telegram and send `/start` or `/menu`.

По умолчанию Argus работает в ручном режиме: при запуске не подключает Telethon,
не запускает фоновые опросы VK/отзывов, анализ очереди Ollama и автоматические
рассылки. Кнопки сбора лайков, CSV и PPTX продолжают работать с VK API по запросу.
Переключатель `ENABLE_BACKGROUND_MONITORS=false` имеет приоритет над ранее
включёнными модулями в SQLite. Для фоновой работы явно установите
`ENABLE_BACKGROUND_MONITORS=true` и перезапустите процесс.

При включённом фоне Telegram на новом источнике запоминает последнее сообщение
без загрузки всей истории. Пропущенные записи при старте сохраняются без
уведомлений; курсор продвигается к актуальному сообщению. Старые посты и комментарии
Telegram/VK не генерируют live-алерты, анализ накопленной очереди не отправляет
старые риск-алерты. Исторические отзывы и старая очередь доставки при старте
принимаются как тихая исходная база по тому же правилу, что и первый импорт.
Это не удаляет сохранённые записи и не мешает ручным историческим отчётам.

## VK Setup

VK is currently more important than Telegram Monitor in this project.

Set these values in `.env` or through `/setup` / inline menu `Настройка -> Настроить VK`:

```env
VK_GROUP_TOKEN=vk1...
VK_GROUP_ID=240114551
VK_MONITOR_MODE=longpoll
VK_ENABLE_POLLING_FALLBACK=true
```

`VK_GROUP_TOKEN` must have access to the community. For reliable community events,
posts, and comments, use a token connected to an admin-managed community. Do not use
HTML scraping.

For historical polling through `wall.get` and `wall.getComments`, VK may reject a
community key with `Group authorization failed: method is unavailable with group auth`.
In that case add a user access token from an admin account:

```env
VK_USER_ACCESS_TOKEN=vk1...
```

Use the tokens like this:

- `VK_GROUP_TOKEN` - Long Poll events from the community.
- `VK_USER_ACCESS_TOKEN` - manual/polling sync of recent wall posts and comments.
- `VK_ACCESS_TOKEN` - legacy alias for `VK_GROUP_TOKEN`.

If a browser URL contains `group-240114551`, use:

```env
VK_GROUP_ID=240114551
```

VK commands:

- `/vk_status`
- `/vk_setup`
- `/vk_sync`
- `/vk_recent_posts [limit]`
- `/vk_posts 24h 10`
- `/vk_recent_comments`
- `/vk_dashboard 7d`
- `/vk_watch_on`
- `/vk_watch_off`

The MVP implements official VK API polling fallback: it reads latest group wall posts
with `wall.get`, reads comments with `wall.getComments`, deduplicates by
`group_id + post_id + comment_id`, stores data in SQLite, and sends Telegram alerts
for new posts/comments. Long Poll uses the community token and does not need a public
Callback API URL.

## Лайки ВК по профилям

Бот проходит по постам за выбранный период, получает все страницы `likes.getList`
и составляет список профилей автоматически: один профиль добавляет **+1 за каждый
лайкнутый пост**. Предварительный список людей не нужен. Имена берутся из ВК;
однофамильцы различаются по VK ID. Если имя недоступно, остается ссылка и ID.
Лайки от имени сообществ учитываются отдельно со ссылкой на сообщество.

Для сбора нужен **пользовательский** `VK_USER_ACCESS_TOKEN`: токен сообщества для
`likes.getList` не подходит. Укажите токен локально в `.env`. Для вашего сообщества:

```env
ENABLE_VK_MONITOR=true
VK_USER_ACCESS_TOKEN=replace_locally
VK_GROUP_ID=173208026
```

Если используете только пользовательский токен, задайте `VK_MONITOR_MODE=polling`.
Перезапустите бот после изменения `.env`. Запуск: `python -m app.main`.
Токен должен иметь доступ к используемым методам и выбранным постам.

В Telegram откройте **VK → ❤️ Лайки по профилям** или отправьте:

```text
/vk_likes 7d
/vk_likes 2026-10-12 2026-10-19
/vk_likes last 17
/vk_likes https://vk.ru/kait_20_official 7d
/vk_likes https://vk.ru/wall-173208026_7730
/vk_likes_report
```

- Две даты включают оба дня в `TIMEZONE` (по умолчанию `Europe/Moscow`).
- `7d` означает последние 7 суток. Считаются посты по дате публикации, а лайки —
  на момент сбора. Дату, когда человек поставил лайк, API здесь не сообщает.
- `last 17` выбирает последние 17 постов по дате; старый закрепленный пост не
  вытесняет свежий. Если постов меньше, отчет показывает фактическое количество.
- Ссылка на пост без периода собирает один пост. Ссылка на сообщество с периодом
  выбирает это сообщество без изменения настроек. Поддерживаются `vk.ru`, `vk.com`
  и ссылки `?w=reactions-173208026_7730...`, как в окне списка лайков.
- Если ссылка не указана, используется `VK_GROUP_ID`.

Результат — краткая таблица в сообщении и полный CSV в UTF-8 с BOM для Excel:
имя, VK ID, ссылка на профиль, число лайков, число постов, период, время сбора,
сообщество и ссылки на посты, которые лайкнул профиль. Профили без лайков в список
не попадают: заранее заданного списка людей нет. Деление по отделам автоматически
не определяется.

Каждый успешный сбор сохраняется в SQLite (`vk_like_reports`); `/vk_likes_report`
выдает последний успешный отчет, включая сообщество и время сбора, без запросов к
ВК. Повторный сбор пересчитывает текущие лайки, включая снятые, без накопления
дублей между запусками. При неполной странице, ошибке доступа или сети частичный
отчет не публикуется, предыдущий остается доступен. Изменения лайков во время
сбора могут влиять на результат: API не предоставляет атомарный снимок.

Сбор запускается вручную, доступен только `ADMIN_IDS`, с паузами между запросами.
Одновременный второй запуск отклоняется. Ограничения одного запроса: до 1000
последних постов для `last`, до 100 страниц стены для периода и до 1000 страниц
лайков на пост; при достижении предела бот сообщает об ошибке, без усечения отчета.
Большие периоды могут выполняться несколько минут.

Проверка: `python -m pytest tests/test_vk_likes.py -q`.
Схемы методов: [VK API](https://github.com/VKCOM/vk-api-schema/blob/master/likes/methods.json).

### Заполнение презентации из Telegram

Основной путь — кнопка **❤️ Лайки и презентации ВК** в главном меню (также `/vk_likes`).
В панели доступны:

- **Собрать лайки** — выбрать 7/30 суток, последние 17 постов или ввести две даты.
- **Создать PPTX** — собрать свежие данные и заполнить сохранённый шаблон.
- **Последний отчёт** — показать таблицу и CSV без повторного сбора.
- **PPTX из отчёта** — применить текущие привязки к последней сохранённой выгрузке;
  полезно после уточнения одноимённых профилей, не требует нового запроса к ВК.
- **Шаблон презентации** — загрузить, заменить или скачать исходный PPTX.
- **Привязки профилей** — посмотреть, скачать, добавить или изменить соответствия
  «ФИО из шаблона → VK ID». Ввод: `Фамилия Имя Отчество | ссылка или ID`.

Ввод дат через кнопки: `12.10.2026 19.10.2026` (также поддерживается ISO).
Кнопка отмены возвращает в панель; главное меню сбрасывает незавершённый ввод.
Некорректный шаблон не заменяет ранее сохранённый.

Шаблон хранится рядом с SQLite: `data/vk_likes_template.pptx` при стандартном
`DATABASE_PATH`. Название шаблона и ручные привязки — в `runtime_settings`.
Они сохраняются после перезапуска и переносятся с каталогом `data/`.
На чистой базе однократно добавляются подтверждённые владельцем привязки:

- Цой Владислав Витальевич → `572058492` (`ghostsoy`, отображаемое имя `Vv C`).
- Первов Герман Алексеевич → `1076022785`.
- Петрова Ирина Викторовна → `374396012`.

Уже изменённые вручную значения при начальном заполнении не перезаписываются;
после этой миграции удалённые пользователем привязки не восстанавливаются.
Список стартовых значений находится в `app/vk/workspace.py`, сами привязки
при эксплуатации меняются через интерфейс и хранятся в SQLite, а не `.env`.

После обновления кода установите зависимости и перезапустите бот:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m app.main
```

На новом сервере шаблон можно загрузить через кнопки; при переносе существующей
установки сохраните `data/`, `.env` и `sessions/` по обычному сценарию развёртывания.
Кнопки доступны только администраторам из `ADMIN_IDS`.

Команды тоже сохраняются. Для одноразового шаблона:

Отправьте боту исходный **PPTX** с подписью:

```text
/vk_likes_pptx 2026-10-12 2026-10-19
```

Или ответьте этой командой на ранее отправленный файл. Поддерживаются также `7d`,
`last 17` и ссылка на сообщество перед периодом, как у `/vk_likes`.
Бот заново собирает данные через VK API и возвращает два файла:

1. **PPTX** в исходном оформлении с заполненной колонкой «Лайки».
2. **CSV-сверка** для Excel: слайд, исходное ФИО, было/стало, способ сопоставления,
   VK ID, имя профиля, кандидаты и предупреждения.

В PPTX изменяются таблицы с заголовками «ФИО» и «Лайки» и подписи о времени сбора,
периоде и количестве постов на этих слайдах. ФИО, стили ячеек, порядок строк,
изображения и остальные части презентации сохраняются. Слайды с отзывами
не обновляются. Ограничения: шаблон до 20 МБ, распакованный архив до 100 МБ;
файл должен содержать редактируемые таблицы, а не скриншоты таблиц.

Сопоставление учитывает перестановку имени/фамилии, регистр, пробелы и `е/ё`.
Допускается одна опечатка в достаточно длинном имени **или** фамилии при точном
совпадении второго слова. Такая строка обязательно отмечается в сверке.
Произвольные псевдонимы, смена фамилии и сокращенные имена не угадываются.
Если подходящих профилей несколько, значение — **«н/д»**, а в сверке перечислены
кандидаты. При отсутствии совпадения также ставится «н/д»: это не доказанный ноль.
Исходная пометка «нет профиля ВК» сохраняется.

Чтобы один раз уточнить профиль, отправьте:

```text
/vk_likes_bind 123456789 | Фамилия Имя Отчество
```

Привязка сохраняется в SQLite и действует после перезапуска. Ее можно заменить
той же командой с другим ID; ID `0` удаляет привязку. Только для явно привязанного
профиля отсутствие лайков в полной выгрузке означает `0`. Бот не выбирает
одноименный профиль по максимальному числу лайков.

Исторические числа в шаблоне могут отличаться от текущего состояния лайков.
Сверка показывает такие отличия отдельно; дату постановки лайка API не сообщает.
Одинаковые имя и фамилия сами по себе не подтверждают личность: явная привязка
к VK ID надежнее сопоставления по отображаемому имени.

Для локальной проверки на сохраненном JSON-снимке:

```powershell
python -m app.vk.presentation template.pptx snapshot.json result.pptx
# Необязательный файл: {"Фамилия Имя Отчество": 123456789}
python -m app.vk.presentation template.pptx snapshot.json result.pptx --bindings bindings.json
python -m pytest tests/test_vk_presentation.py -q
```

CLI также создает `result.matches.csv`, не перезаписывая исходный шаблон.

## Telegram Monitor

Telegram Monitor is optional and non-blocking. If `TG_API_ID`, `TG_API_HASH`, or the
Telethon session file are missing, Argus still starts and reports:

- `config_missing` when API credentials are absent.
- `auth_required` when the Telethon session file is absent or unauthorized.
- `error` when the Telethon client cannot start.

To enable it:

```env
ENABLE_TELEGRAM_MONITOR=true
TG_API_ID=123456
TG_API_HASH=replace_me
TG_SESSION_NAME=argus_user
TG_SESSION_DIR=sessions
```

Create the Telethon session locally. Session files are ignored by Git.

```bash
python -m app.telegram_login
```

Do not send Telegram login codes to the Argus bot or any Telegram chat. Telegram
can block the login attempt if it sees that the code was shared from your account.
`/tg_auth` in the bot only shows the safe local CLI instruction.

Restart Argus after successful auth so the Telegram Monitor scheduler can attach
to the new session.

Telegram commands:

- `/tg_status`
- `/tg_auth`
- `/tg_add_source`
- `/tg_sources`
- `/tg_keywords`
- `/tg_add_keyword <text>`
- `/tg_remove_keyword <id>`
- `/tg_recent_posts <source_id_or_telegram_id> [limit]`
- `/tg_posts <source_id_or_telegram_id> <period> [limit]`
- `/tg_sync_posts <source_id>`
- `/tg_dashboard <source_id_or_telegram_id> <period>`
- `/tg_monitor` — состояние realtime-сборщика и очереди анализа
- `/tg_alerts` — справка по риск-алертам
- `/tg_digest` — дневная сводка
- `/tg_charts` — четыре PNG-графика за 30 дней
- `/tg_llm_status` — модель и размер очереди Ollama

### Telegram Monitoring and Ollama analysis

The extended monitoring pipeline reuses the authorized Telethon user session. It does not
perform a second authorization and does not bypass access restrictions:

```text
Telegram -> Telethon NewMessage/catch-up -> SQLite -> persistent analysis queue
         -> Ollama -> strict classifier JSON -> alerts + daily analytics -> Argus Bot UI
```

Messages are unique by source and Telegram message id. On startup, Argus fetches only
messages after the saved cursor, up to `TELEGRAM_CATCHUP_LIMIT`, and then attaches the
realtime handler. Service messages are ignored. Entity ids/access hashes saved on the
source are reused instead of resolving a username for every event.

The prefilter only changes queue priority; it never makes the final safety decision.
Ollama evaluates intent and context and returns risk, severity, confidence, categories,
sentiment, topic, reason, and intent level. Alerts are deduplicated in SQLite, include the
model explanation, and link to the original message where Telegram permits it. Argus is
an aid for human review and does not make disciplinary decisions.

If Ollama is unavailable, collection continues and messages remain queued for retry.
Configure the integration with:

```env
TELEGRAM_CATCHUP_LIMIT=200
TELEGRAM_ANALYSIS_QUEUE_LIMIT=1000
OLLAMA_ENABLED=true
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_TIMEOUT=30
LLM_RISK_ANALYSIS_ENABLED=true
LLM_ANALYZE_ALL_MESSAGES=true
LLM_BATCH_SIZE=10
LLM_MAX_CONCURRENT_REQUESTS=2
LLM_ALERT_MIN_SEVERITY=2
DAILY_DIGEST_ENABLED=true
DAILY_DIGEST_TIME=09:00
TIMEZONE=Europe/Moscow
```

Only messages from explicitly configured sources accessible to the Telethon account are
stored. Argus does not build personal profiles or infer identity, beliefs, religion,
orientation, or other sensitive traits. Do not log or commit session files and secrets.

### Updating the existing Vavilon installation

The update keeps `.env`, `data/`, and `sessions/` in place and requires no Docker or
network-namespace changes:

```bash
cd /srv/services/argus
sudo -u no1se git pull --ff-only
sudo -u no1se /srv/services/argus/.venv/bin/pip install -r requirements.txt
sudo systemctl restart argus.service
sudo systemctl status argus.service --no-pager
sudo journalctl -u argus.service -n 100 --no-pager
```

SQLite tables are created idempotently during normal startup. Back up
`data/argus.sqlite3` before deployment according to the server's existing backup policy.

Legacy commands such as `/sources`, `/add_source`, `/sync_posts`, and `/dashboard`
still work when Telegram Monitor is available.

`/tg_add_source` expects a forwarded message from a channel, group, or discussion
group. After that Argus asks whether to monitor the source as posts or as a
discussion/comment stream. First sync stores a baseline and does not alert old
messages.

Use `/tg_sources` to see internal source IDs. Commands that read local Telegram
data also accept saved Telegram ids such as `-1001451549966`.

## Bot UI

`/start` and `/menu` open an inline control panel:

- Dashboards
- Alerts
- Modules
- Settings
- VK
- Telegram
- Status

Inline callbacks edit the existing message when possible. The UI is restricted to
`ADMIN_IDS`.

Users without access can send `/request_access`. Argus sends the current admins an
inline approval card; approving it appends the user id to `ADMIN_IDS` in `.env` and
updates the running process immediately.

The Alerts menu writes runtime settings:

- `alerts_vk_enabled`
- `alerts_vk_posts_enabled`
- `alerts_vk_comments_enabled`
- `alerts_telegram_enabled`
- `alerts_telegram_posts_enabled`
- `alerts_telegram_comments_enabled`
- `alerts_telegram_keywords_enabled`

Dashboard messages are capped: Argus counts comments/messages in SQLite but only
shows compact summaries. When `matplotlib` is installed, `/tg_dashboard` and
`/vk_dashboard` send a PNG chart with the dashboard text attached as the photo
caption. Charts are generated locally from SQLite data and still render when the
selected period has no rows.

VK live likes are counted from VK Long Poll `like_add` / `like_remove` events for
posts that already exist in SQLite. If these counters stay unchanged, check that
the community Long Poll settings include like events.

General commands:

- `/request_access`
- `/help`
- `/status`
- `/modules`
- `/setup`

## Docker

```bash
docker compose build
docker compose up
```

The compose service uses `build: .` and mounts:

- `./.env:/app/.env`
- `./data:/app/data`
- `./logs:/app/logs`
- `./sessions:/app/sessions`

## Database

SQLite is stored at `DATABASE_PATH`, default `data/argus.sqlite3`.

Main tables:

- Telegram: `sources`, `posts`, `comments`, `telegram_group_messages`,
  `telegram_keywords`, `stats_snapshots`
- VK: `vk_sources`, `vk_posts`, `vk_comments`, `vk_stats_snapshots`
- Reviews: `review_sources`, `reviews`
- Runtime: `runtime_settings`, `scheduler_state`, `alerts`

## Reviews Monitor

Reviews Monitor autonomously tracks public reviews across 12 educational branch endpoints (6 on Yandex Maps + 6 on 2GIS).

Key features:
- **Default State**: Disabled by default (`ENABLE_REVIEWS_MONITOR=false`).
- **Runtime Enable/Disable**: Toggle on/off dynamically via `/reviews_on` and `/reviews_off` or the Telegram UI without restarting Argus. While disabled, zero external HTTP requests are made.
- **Network Implementation**:
  - **2GIS**: Asynchronous HTTP client via `aiohttp` querying the 2GIS Reviews API (`public-api.reviews.2gis.com`). This endpoint is utilized by the public web client and represents an internal web implementation detail that may change over time; automated health monitoring detects breaking format or schema changes.
  - **Yandex Maps**: Lightweight standard library `urllib.request` dispatched via `asyncio.to_thread` with automated cookie/session initialization. Rather than requiring TLS/JA3 spoofing, visiting the public org page initializes valid `csrfToken` and `sessionId` cookies required by Yandex's internal `fetchReviews` endpoint.
- **Polite Rate Limiting**: Uniform `reviews_request_pause_seconds = 3.0s` default pause between requests to respect platform quotas and prevent 429/rate-limit blocks.
- **Zero Headless Browsers**: Operates entirely via verified, lightweight HTTP requests without Chromium or Playwright overhead.
- **Resource Footprint**: The Reviews Monitor module adds minimal memory overhead (~10–25 MB RSS based on standalone probe benchmarks).
- **Baseline Initialization**: The initial batch of historical reviews is saved without sending Telegram notifications (`is_initialized = 0`). Only new reviews detected in subsequent cycles trigger alerts.
- **Chronological Delivery**: New reviews are delivered in chronological order (oldest to newest).
- **Safe HTML & Multi-Target Multipart Delivery**: Long reviews (>4000 chars) are safely chunked before HTML escaping to prevent tag or entity slicing. Part-level delivery is tracked independently per alert chat target (`raw_payload_json`), preventing redundant re-delivery upon partial network failures.
- **Non-Waiting Sync Lock**: Manual or concurrent background sync requests immediately reject parallel runs with a clear warning rather than stacking duplicate HTTP traffic.
- **Health & Recovery Alerts**: Suppresses transient errors; sends a problem alert after 3 consecutive failures, and sends a single recovery alert upon successful reconnect.

Commands:
- `/reviews` — Open Reviews Monitor management screen.
- `/reviews_status` — View detailed status of all 12 monitored branches.
- `/reviews_sync` — Trigger immediate manual verification of all branches.
- `/reviews_on` — Activate reviews polling at runtime without restart.
- `/reviews_off` — Pause reviews polling at runtime without restart.

Runtime settings from the bot can supplement `.env`. `.env` remains the priority
configuration source.

## Safety

Never commit:

- `.env` or `.env.*`
- VK tokens
- Telegram bot token
- Telegram `api_hash`
- `sessions/`
- `*.session` and `*.session-journal`
- SQLite databases under `data/`
- logs

Argus masks secrets in UI summaries and does not log token values intentionally.
