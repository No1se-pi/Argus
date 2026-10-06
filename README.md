# Argus

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
