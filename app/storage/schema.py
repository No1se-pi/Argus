from app.storage.database import Database


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'telegram',
    link TEXT NOT NULL,
    username TEXT,
    title TEXT NOT NULL,
    telegram_entity_id INTEGER,
    telegram_access_hash INTEGER,
    telegram_entity_type TEXT,
    last_message_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_telegram_entity
ON sources(kind, telegram_entity_id)
WHERE telegram_entity_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_username
ON sources(kind, username)
WHERE username IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sources_active ON sources(is_active);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    text TEXT,
    views INTEGER,
    reactions_total INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    post_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_source_date ON posts(source_id, date);
CREATE INDEX IF NOT EXISTS idx_posts_source_message ON posts(source_id, telegram_message_id);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL,
    from_id INTEGER,
    date TEXT NOT NULL,
    text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(post_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_comments_source_date ON comments(source_id, date);

CREATE TABLE IF NOT EXISTS stats_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    snapshot_type TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    captured_at TEXT NOT NULL,
    reactions_total INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_stats_source_captured ON stats_snapshots(source_id, captured_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'telegram',
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    post_id INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    item_type TEXT,
    item_id TEXT,
    alert_type TEXT NOT NULL,
    chat_id INTEGER,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_source_created ON alerts(source_id, created_at);

CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vk_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL UNIQUE,
    group_name TEXT,
    screen_name TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    monitor_mode TEXT NOT NULL DEFAULT 'longpoll',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vk_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    text TEXT,
    date TEXT NOT NULL,
    likes_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    reposts_count INTEGER NOT NULL DEFAULT 0,
    views_count INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(group_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_vk_posts_group_date ON vk_posts(group_id, date);

CREATE TABLE IF NOT EXISTS vk_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    comment_id INTEGER NOT NULL,
    from_id INTEGER,
    text TEXT,
    date TEXT NOT NULL,
    parent_comment_id INTEGER,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(group_id, post_id, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_vk_comments_group_date ON vk_comments(group_id, date);

CREATE TABLE IF NOT EXISTS vk_stats_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    likes_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    reposts_count INTEGER NOT NULL DEFAULT 0,
    views_count INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vk_snapshots_group_checked
ON vk_stats_snapshots(group_id, checked_at);
"""


async def init_schema(database: Database) -> None:
    connection = database.require_connection()
    await connection.executescript(SCHEMA_SQL)
    await _ensure_alert_columns(database)
    await connection.commit()


async def _ensure_alert_columns(database: Database) -> None:
    connection = database.require_connection()
    async with connection.execute("PRAGMA table_info(alerts)") as cursor:
        rows = await cursor.fetchall()
    source_id_column = next((row for row in rows if row["name"] == "source_id"), None)
    if source_id_column is not None and int(source_id_column["notnull"]) == 1:
        await _rebuild_alerts_table(database)
        async with connection.execute("PRAGMA table_info(alerts)") as cursor:
            rows = await cursor.fetchall()

    columns = {row["name"] for row in rows}
    migrations = []
    if "platform" not in columns:
        migrations.append("ALTER TABLE alerts ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'")
    if "item_type" not in columns:
        migrations.append("ALTER TABLE alerts ADD COLUMN item_type TEXT")
    if "item_id" not in columns:
        migrations.append("ALTER TABLE alerts ADD COLUMN item_id TEXT")
    for statement in migrations:
        await connection.execute(statement)


async def _rebuild_alerts_table(database: Database) -> None:
    connection = database.require_connection()
    await connection.execute("PRAGMA foreign_keys = OFF")
    await connection.execute("ALTER TABLE alerts RENAME TO alerts_old")
    await connection.execute(
        """
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT 'telegram',
            source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
            post_id INTEGER REFERENCES posts(id) ON DELETE SET NULL,
            item_type TEXT,
            item_id TEXT,
            alert_type TEXT NOT NULL,
            chat_id INTEGER,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )
    await connection.execute(
        """
        INSERT INTO alerts (
            id, platform, source_id, post_id, item_type, item_id, alert_type,
            chat_id, message, status, created_at, sent_at
        )
        SELECT
            id, 'telegram', source_id, post_id, NULL, NULL, alert_type,
            chat_id, message, status, created_at, sent_at
        FROM alerts_old
        """
    )
    await connection.execute("DROP TABLE alerts_old")
    await connection.execute("PRAGMA foreign_keys = ON")
