# Argus

Argus is a Python 3.11+ Telegram control bot for social monitoring, alerts, and dashboards.

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
ENABLE_VK_MONITOR=true
ENABLE_TELEGRAM_MONITOR=false
```

Then open Telegram and send `/start` or `/menu`.

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
- `/vk_recent_posts`
- `/vk_recent_comments`
- `/vk_dashboard 7d`
- `/vk_watch_on`
- `/vk_watch_off`

The MVP implements official VK API polling fallback: it reads latest group wall posts
with `wall.get`, reads comments with `wall.getComments`, deduplicates by
`group_id + post_id + comment_id`, stores data in SQLite, and sends Telegram alerts
for new posts/comments. Long Poll uses the community token and does not need a public
Callback API URL.

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
- `/tg_sources`
- `/tg_sync_posts <source_id>`
- `/tg_dashboard <source_id> <period>`

Legacy commands such as `/sources`, `/add_source`, `/sync_posts`, and `/dashboard`
still work when Telegram Monitor is available.

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

The Alerts menu writes runtime settings:

- `alerts_vk_enabled`
- `alerts_vk_posts_enabled`
- `alerts_vk_comments_enabled`

VK dashboard messages are capped: Argus counts comments in SQLite but only shows a
small recent sample, so a 30-day dashboard does not try to send every comment.

General commands:

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

- `./data:/app/data`
- `./logs:/app/logs`
- `./sessions:/app/sessions`

## Database

SQLite is stored at `DATABASE_PATH`, default `data/argus.sqlite3`.

Main tables:

- Telegram: `sources`, `posts`, `comments`, `stats_snapshots`
- VK: `vk_sources`, `vk_posts`, `vk_comments`, `vk_stats_snapshots`
- Runtime: `runtime_settings`, `scheduler_state`, `alerts`

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
