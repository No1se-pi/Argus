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
    telegram_monitor_mode TEXT NOT NULL DEFAULT 'posts',
    tracked_posts_limit INTEGER,
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

CREATE TABLE IF NOT EXISTS telegram_group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL,
    from_id INTEGER,
    date TEXT NOT NULL,
    text TEXT,
    message_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_tg_group_messages_source_date
ON telegram_group_messages(source_id, date);

CREATE TABLE IF NOT EXISTS telegram_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL,
    reply_to_message_id INTEGER,
    text TEXT,
    message_date TEXT NOT NULL,
    message_url TEXT,
    prefilter_priority INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT NOT NULL,
    analyzed_at TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    analysis_attempts INTEGER NOT NULL DEFAULT 0,
    last_analysis_error TEXT,
    next_analysis_at TEXT,
    alert_eligible INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_tg_messages_pending
ON telegram_messages(analysis_status, prefilter_priority DESC, collected_at);
CREATE INDEX IF NOT EXISTS idx_tg_messages_source_date
ON telegram_messages(source_id, message_date);

CREATE TABLE IF NOT EXISTS telegram_message_analysis (
    message_id INTEGER PRIMARY KEY REFERENCES telegram_messages(id) ON DELETE CASCADE,
    risk INTEGER NOT NULL,
    severity INTEGER NOT NULL,
    confidence REAL NOT NULL,
    categories_json TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    topic TEXT NOT NULL,
    intent_level TEXT NOT NULL,
    reason TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    analyzed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_risk_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE REFERENCES telegram_messages(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',
    reviewed_by INTEGER,
    reviewed_at TEXT,
    verdict TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_daily_stats (
    date TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    messages_count INTEGER NOT NULL DEFAULT 0,
    positive_count INTEGER NOT NULL DEFAULT 0,
    neutral_count INTEGER NOT NULL DEFAULT 0,
    negative_count INTEGER NOT NULL DEFAULT 0,
    severity_1_count INTEGER NOT NULL DEFAULT 0,
    severity_2_count INTEGER NOT NULL DEFAULT 0,
    severity_3_count INTEGER NOT NULL DEFAULT 0,
    topics_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(date, source_id)
);

CREATE TABLE IF NOT EXISTS telegram_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tg_keywords_active ON telegram_keywords(is_active);

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

CREATE TABLE IF NOT EXISTS review_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_initialized INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT 'PENDING',
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    health_alert_active INTEGER NOT NULL DEFAULT 0,
    health_alert_sent_at TEXT,
    backoff_until TEXT,
    last_rating REAL,
    total_reviews_count INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_review_sources_active ON review_sources(is_active);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES review_sources(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    external_review_id TEXT NOT NULL,
    author_name TEXT,
    rating INTEGER,
    text TEXT,
    published_at TEXT,
    edited_at TEXT,
    review_url TEXT,
    content_hash TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    telegram_parts_total INTEGER NOT NULL DEFAULT 1,
    telegram_parts_sent INTEGER NOT NULL DEFAULT 0,
    is_sent_to_telegram INTEGER NOT NULL DEFAULT 0,
    telegram_sent_at TEXT,
    last_delivery_error TEXT,
    raw_payload_json TEXT,
    UNIQUE(platform, external_review_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_source ON reviews(source_id);
CREATE INDEX IF NOT EXISTS idx_reviews_sent ON reviews(is_sent_to_telegram);
CREATE INDEX IF NOT EXISTS idx_reviews_published ON reviews(published_at);
"""


async def init_schema(database: Database) -> None:
    connection = database.require_connection()
    await connection.executescript(SCHEMA_SQL)
    await _ensure_source_columns(database)
    await _ensure_alert_columns(database)
    await _ensure_review_columns(database)
    await _ensure_telegram_monitoring_columns(database)
    await connection.commit()


async def _ensure_source_columns(database: Database) -> None:
    connection = database.require_connection()
    async with connection.execute("PRAGMA table_info(sources)") as cursor:
        rows = await cursor.fetchall()
    columns = {row["name"] for row in rows}
    migrations = []
    if "telegram_monitor_mode" not in columns:
        migrations.append(
            "ALTER TABLE sources ADD COLUMN telegram_monitor_mode TEXT NOT NULL DEFAULT 'posts'"
        )
    if "tracked_posts_limit" not in columns:
        migrations.append("ALTER TABLE sources ADD COLUMN tracked_posts_limit INTEGER")
    for statement in migrations:
        await connection.execute(statement)


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


async def _ensure_review_columns(database: Database) -> None:
    connection = database.require_connection()

    # Verify review_sources columns
    async with connection.execute("PRAGMA table_info(review_sources)") as cursor:
        sources_cols = {row["name"] for row in await cursor.fetchall()}

    if sources_cols:
        sources_migrations = []
        if "health_alert_active" not in sources_cols:
            sources_migrations.append(
                "ALTER TABLE review_sources "
                "ADD COLUMN health_alert_active INTEGER NOT NULL DEFAULT 0"
            )
        if "health_alert_sent_at" not in sources_cols:
            sources_migrations.append(
                "ALTER TABLE review_sources ADD COLUMN health_alert_sent_at TEXT"
            )
        for stmt in sources_migrations:
            await connection.execute(stmt)


async def _ensure_telegram_monitoring_columns(database: Database) -> None:
    connection = database.require_connection()
    async with connection.execute("PRAGMA table_info(telegram_messages)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if columns and "next_analysis_at" not in columns:
        await connection.execute("ALTER TABLE telegram_messages ADD COLUMN next_analysis_at TEXT")
    if columns and "alert_eligible" not in columns:
        await connection.execute(
            "ALTER TABLE telegram_messages ADD COLUMN alert_eligible INTEGER NOT NULL DEFAULT 1"
        )

    # Verify reviews columns
    async with connection.execute("PRAGMA table_info(reviews)") as cursor:
        reviews_cols = {row["name"] for row in await cursor.fetchall()}

    if reviews_cols:
        reviews_migrations = []
        if "telegram_parts_total" not in reviews_cols:
            reviews_migrations.append(
                "ALTER TABLE reviews ADD COLUMN telegram_parts_total INTEGER NOT NULL DEFAULT 1"
            )
        if "telegram_parts_sent" not in reviews_cols:
            reviews_migrations.append(
                "ALTER TABLE reviews ADD COLUMN telegram_parts_sent INTEGER NOT NULL DEFAULT 0"
            )
        if "last_delivery_error" not in reviews_cols:
            reviews_migrations.append("ALTER TABLE reviews ADD COLUMN last_delivery_error TEXT")
        for stmt in reviews_migrations:
            await connection.execute(stmt)
