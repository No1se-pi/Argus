import json
from datetime import datetime

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone
    UTC = timezone.utc  # noqa: UP017

from app.analytics.dashboard import DashboardService
from app.reviews.models import ReviewItem, ReviewPlatform, ReviewSource
from app.storage.database import Database
from app.storage.models import (
    Comment,
    Post,
    Source,
    TelegramGroupMessage,
    TelegramKeyword,
    VkComment,
    VkPost,
    VkSource,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _telegram_entity_id_candidates(value: int) -> list[int]:
    candidates = {value, abs(value), -value}
    absolute = abs(value)
    channel_offset = 1_000_000_000_000
    if absolute > channel_offset:
        candidates.add(absolute - channel_offset)
        candidates.add(-(absolute - channel_offset))
    return sorted(candidates)


class SourceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_sources(
        self,
        include_inactive: bool = False,
        monitor_mode: str | None = None,
    ) -> list[Source]:
        connection = self.database.require_connection()
        query = "SELECT * FROM sources"
        conditions = []
        params: list[object] = []
        if not include_inactive:
            conditions.append("is_active = 1")
        if monitor_mode is not None:
            conditions.append("telegram_monitor_mode = ?")
            params.append(monitor_mode)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id"
        async with connection.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return [Source.from_row(row) for row in rows]

    async def count_active(self) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT COUNT(*) AS count FROM sources WHERE is_active = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def get_source(self, source_id: int) -> Source | None:
        connection = self.database.require_connection()
        async with connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)) as cursor:
            row = await cursor.fetchone()
        return Source.from_row(row) if row else None

    async def get_by_telegram_entity(self, entity_id: int) -> Source | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT * FROM sources WHERE kind = 'telegram' AND telegram_entity_id = ?",
            (entity_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return Source.from_row(row) if row else None

    async def get_by_telegram_reference(self, value: int) -> Source | None:
        candidates = _telegram_entity_id_candidates(value)
        connection = self.database.require_connection()
        placeholders = ", ".join("?" for _item in candidates)
        async with connection.execute(
            f"""
            SELECT * FROM sources
            WHERE kind = 'telegram' AND telegram_entity_id IN ({placeholders})
            ORDER BY is_active DESC, id ASC
            LIMIT 1
            """,
            tuple(candidates),
        ) as cursor:
            row = await cursor.fetchone()
        return Source.from_row(row) if row else None

    async def upsert_telegram_source(
        self,
        *,
        link: str,
        username: str | None,
        title: str,
        entity_id: int,
        access_hash: int | None,
        entity_type: str,
        monitor_mode: str = "posts",
        tracked_posts_limit: int | None = None,
    ) -> Source:
        connection = self.database.require_connection()
        now = utc_now_iso()
        existing = await self.get_by_telegram_entity(entity_id)
        if existing:
            await connection.execute(
                """
                UPDATE sources
                SET link = ?, username = ?, title = ?, telegram_access_hash = ?,
                    telegram_entity_type = ?, telegram_monitor_mode = ?,
                    tracked_posts_limit = COALESCE(?, tracked_posts_limit),
                    last_message_id = CASE
                        WHEN telegram_monitor_mode != ? THEN NULL
                        ELSE last_message_id
                    END,
                    is_active = 1, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    link,
                    username,
                    title,
                    access_hash,
                    entity_type,
                    monitor_mode,
                    tracked_posts_limit,
                    monitor_mode,
                    now,
                    existing.id,
                ),
            )
            await connection.commit()
            source = await self.get_source(existing.id)
            if source is None:
                raise RuntimeError("Updated source disappeared.")
            return source

        cursor = await connection.execute(
            """
            INSERT INTO sources (
                kind, link, username, title, telegram_entity_id, telegram_access_hash,
                telegram_entity_type, telegram_monitor_mode, tracked_posts_limit,
                is_active, created_at, updated_at
            )
            VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                link,
                username,
                title,
                entity_id,
                access_hash,
                entity_type,
                monitor_mode,
                tracked_posts_limit,
                now,
                now,
            ),
        )
        await connection.commit()
        source = await self.get_source(cursor.lastrowid)
        if source is None:
            raise RuntimeError("Inserted source disappeared.")
        return source

    async def deactivate(self, source_id: int) -> bool:
        connection = self.database.require_connection()
        cursor = await connection.execute(
            "UPDATE sources SET is_active = 0, updated_at = ? WHERE id = ? AND is_active = 1",
            (utc_now_iso(), source_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def set_last_message_id(self, source_id: int, message_id: int) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            "UPDATE sources SET last_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, utc_now_iso(), source_id),
        )
        await connection.commit()

    async def set_error(self, source_id: int, error: str) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            "UPDATE sources SET last_error = ?, updated_at = ? WHERE id = ?",
            (error[:500], utc_now_iso(), source_id),
        )
        await connection.commit()

    async def clear_error(self, source_id: int) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            "UPDATE sources SET last_error = NULL, updated_at = ? WHERE id = ?",
            (utc_now_iso(), source_id),
        )
        await connection.commit()

    async def update_monitor_settings(
        self,
        source_id: int,
        *,
        monitor_mode: str | None = None,
        tracked_posts_limit: int | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        params: list[object] = [utc_now_iso()]
        if monitor_mode is not None:
            assignments.append("telegram_monitor_mode = ?")
            params.append(monitor_mode)
            assignments.append(
                "last_message_id = CASE "
                "WHEN telegram_monitor_mode != ? THEN NULL "
                "ELSE last_message_id END"
            )
            params.append(monitor_mode)
        if tracked_posts_limit is not None:
            assignments.append("tracked_posts_limit = ?")
            params.append(tracked_posts_limit)
        params.append(source_id)
        connection = self.database.require_connection()
        await connection.execute(
            f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )
        await connection.commit()


class PostRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_post(
        self,
        *,
        source_id: int,
        telegram_message_id: int,
        date: str,
        text: str | None,
        views: int | None,
        reactions_total: int,
        comments_count: int,
        post_url: str | None,
    ) -> Post:
        connection = self.database.require_connection()
        now = utc_now_iso()
        await connection.execute(
            """
            INSERT INTO posts (
                source_id, telegram_message_id, date, text, views, reactions_total,
                comments_count, post_url, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, telegram_message_id) DO UPDATE SET
                date = excluded.date,
                text = excluded.text,
                views = excluded.views,
                reactions_total = excluded.reactions_total,
                comments_count = excluded.comments_count,
                post_url = excluded.post_url,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                telegram_message_id,
                date,
                text,
                views,
                reactions_total,
                comments_count,
                post_url,
                now,
                now,
            ),
        )
        await connection.commit()
        post = await self.get_by_message_id(source_id, telegram_message_id)
        if post is None:
            raise RuntimeError("Upserted post disappeared.")
        return post

    async def get_by_message_id(self, source_id: int, telegram_message_id: int) -> Post | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT * FROM posts WHERE source_id = ? AND telegram_message_id = ?",
            (source_id, telegram_message_id),
        ) as cursor:
            row = await cursor.fetchone()
        return Post.from_row(row) if row else None

    async def list_by_period(self, source_id: int, start: str, end: str) -> list[Post]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM posts
            WHERE source_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (source_id, start, end),
        ) as cursor:
            rows = await cursor.fetchall()
        return [Post.from_row(row) for row in rows]

    async def list_recent(self, source_id: int, limit: int = 10) -> list[Post]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM posts
            WHERE source_id = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (source_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [Post.from_row(row) for row in rows]

    async def count_created_by_period(self, source_id: int, start: str, end: str) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT COUNT(*) AS count FROM posts
            WHERE source_id = ? AND created_at >= ? AND created_at <= ?
            """,
            (source_id, start, end),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def update_metrics(self, post_id: int, reactions_total: int, comments_count: int) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            UPDATE posts
            SET reactions_total = ?, comments_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (reactions_total, comments_count, utc_now_iso(), post_id),
        )
        await connection.commit()


class CommentRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_comment(
        self,
        *,
        source_id: int,
        post_id: int,
        telegram_message_id: int,
        from_id: int | None,
        date: str,
        text: str | None,
    ) -> Comment:
        connection = self.database.require_connection()
        now = utc_now_iso()
        await connection.execute(
            """
            INSERT INTO comments (
                source_id, post_id, telegram_message_id, from_id, date, text, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id, telegram_message_id) DO UPDATE SET
                from_id = excluded.from_id,
                date = excluded.date,
                text = excluded.text,
                updated_at = excluded.updated_at
            """,
            (source_id, post_id, telegram_message_id, from_id, date, text, now, now),
        )
        await connection.commit()
        async with connection.execute(
            "SELECT * FROM comments WHERE post_id = ? AND telegram_message_id = ?",
            (post_id, telegram_message_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Upserted comment disappeared.")
        return Comment.from_row(row)

    async def count_by_period(self, source_id: int, start: str, end: str) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT COUNT(*) AS count FROM comments
            WHERE source_id = ? AND date >= ? AND date <= ?
            """,
            (source_id, start, end),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def list_by_period(self, source_id: int, start: str, end: str) -> list[Comment]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM comments
            WHERE source_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (source_id, start, end),
        ) as cursor:
            rows = await cursor.fetchall()
        return [Comment.from_row(row) for row in rows]


class TelegramGroupMessageRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_message(
        self,
        *,
        source_id: int,
        telegram_message_id: int,
        from_id: int | None,
        date: str,
        text: str | None,
        message_url: str | None,
    ) -> tuple[TelegramGroupMessage, bool]:
        connection = self.database.require_connection()
        now = utc_now_iso()
        existing = await self.get_by_message_id(source_id, telegram_message_id)
        await connection.execute(
            """
            INSERT INTO telegram_group_messages (
                source_id, telegram_message_id, from_id, date, text, message_url,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, telegram_message_id) DO UPDATE SET
                from_id = excluded.from_id,
                date = excluded.date,
                text = excluded.text,
                message_url = excluded.message_url,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                telegram_message_id,
                from_id,
                date,
                text,
                message_url,
                now,
                now,
            ),
        )
        await connection.commit()
        message = await self.get_by_message_id(source_id, telegram_message_id)
        if message is None:
            raise RuntimeError("Upserted Telegram group message disappeared.")
        return message, existing is None

    async def get_by_message_id(
        self,
        source_id: int,
        telegram_message_id: int,
    ) -> TelegramGroupMessage | None:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM telegram_group_messages
            WHERE source_id = ? AND telegram_message_id = ?
            """,
            (source_id, telegram_message_id),
        ) as cursor:
            row = await cursor.fetchone()
        return TelegramGroupMessage.from_row(row) if row else None

    async def count_by_period(self, source_id: int, start: str, end: str) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT COUNT(*) AS count FROM telegram_group_messages
            WHERE source_id = ? AND date >= ? AND date <= ?
            """,
            (source_id, start, end),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def list_by_period(
        self,
        source_id: int,
        start: str,
        end: str,
    ) -> list[TelegramGroupMessage]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM telegram_group_messages
            WHERE source_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (source_id, start, end),
        ) as cursor:
            rows = await cursor.fetchall()
        return [TelegramGroupMessage.from_row(row) for row in rows]

    async def list_recent(
        self,
        source_id: int,
        limit: int = 10,
    ) -> list[TelegramGroupMessage]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM telegram_group_messages
            WHERE source_id = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (source_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [TelegramGroupMessage.from_row(row) for row in rows]


class TelegramKeywordRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_keywords(self, include_inactive: bool = False) -> list[TelegramKeyword]:
        connection = self.database.require_connection()
        query = "SELECT * FROM telegram_keywords"
        if not include_inactive:
            query += " WHERE is_active = 1"
        query += " ORDER BY keyword COLLATE NOCASE"
        async with connection.execute(query) as cursor:
            rows = await cursor.fetchall()
        return [TelegramKeyword.from_row(row) for row in rows]

    async def add_keyword(self, keyword: str) -> TelegramKeyword:
        normalized = keyword.strip()
        if not normalized:
            raise ValueError("Keyword is empty.")
        connection = self.database.require_connection()
        now = utc_now_iso()
        await connection.execute(
            """
            INSERT INTO telegram_keywords(keyword, is_active, created_at, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(keyword) DO UPDATE SET
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (normalized, now, now),
        )
        await connection.commit()
        async with connection.execute(
            "SELECT * FROM telegram_keywords WHERE keyword = ?",
            (normalized,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Upserted keyword disappeared.")
        return TelegramKeyword.from_row(row)

    async def deactivate_keyword(self, keyword_id: int) -> bool:
        connection = self.database.require_connection()
        cursor = await connection.execute(
            """
            UPDATE telegram_keywords
            SET is_active = 0, updated_at = ?
            WHERE id = ? AND is_active = 1
            """,
            (utc_now_iso(), keyword_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def matching_keywords(self, text: str | None) -> list[TelegramKeyword]:
        if not text:
            return []
        haystack = text.casefold()
        keywords = await self.list_keywords()
        return [keyword for keyword in keywords if keyword.keyword.casefold() in haystack]


class StatsSnapshotRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_snapshot(
        self,
        *,
        source_id: int,
        post_id: int | None,
        snapshot_type: str,
        period_start: str | None,
        period_end: str | None,
        reactions_total: int,
        comments_count: int,
        payload_json: str | None,
    ) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            INSERT INTO stats_snapshots (
                source_id, post_id, snapshot_type, period_start, period_end, captured_at,
                reactions_total, comments_count, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                post_id,
                snapshot_type,
                period_start,
                period_end,
                utc_now_iso(),
                reactions_total,
                comments_count,
                payload_json,
            ),
        )
        await connection.commit()


class AlertRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_alert(
        self,
        *,
        source_id: int,
        post_id: int | None,
        alert_type: str,
        chat_id: int | None,
        message: str,
        status: str,
        sent_at: str | None,
    ) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            INSERT INTO alerts (
                source_id, post_id, alert_type, chat_id, message, status, created_at, sent_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, post_id, alert_type, chat_id, message, status, utc_now_iso(), sent_at),
        )
        await connection.commit()

    async def create_platform_alert(
        self,
        *,
        platform: str,
        source_id: int | None,
        item_type: str,
        item_id: str,
        alert_type: str,
        chat_id: int | None,
        message: str,
        status: str,
        sent_at: str | None,
    ) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            INSERT INTO alerts (
                platform, source_id, post_id, item_type, item_id, alert_type,
                chat_id, message, status, created_at, sent_at
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                source_id,
                item_type,
                item_id,
                alert_type,
                chat_id,
                message,
                status,
                utc_now_iso(),
                sent_at,
            ),
        )
        await connection.commit()


class SchedulerStateRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, key: str) -> str | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT value FROM scheduler_state WHERE key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row["value"]) if row else None

    async def set(self, key: str, value: str) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            INSERT INTO scheduler_state(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, utc_now_iso()),
        )
        await connection.commit()


class RuntimeSettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, key: str) -> str | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT value FROM runtime_settings WHERE key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row["value"]) if row else None

    async def get_bool(self, key: str, default: bool) -> bool:
        value = await self.get(key)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    async def get_int(self, key: str, default: int) -> int:
        value = await self.get(key)
        if value is None:
            return default
        try:
            return int(value.strip())
        except ValueError:
            return default

    async def set(self, key: str, value: str, *, is_secret: bool = False) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            INSERT INTO runtime_settings(key, value, is_secret, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                is_secret = excluded.is_secret,
                updated_at = excluded.updated_at
            """,
            (key, value, int(is_secret), utc_now_iso()),
        )
        await connection.commit()

    async def list_public(self) -> dict[str, str]:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT key, value, is_secret FROM runtime_settings ORDER BY key"
        ) as cursor:
            rows = await cursor.fetchall()
        result: dict[str, str] = {}
        for row in rows:
            value = str(row["value"])
            result[row["key"]] = mask_secret(value) if row["is_secret"] else value
        return result


def mask_secret(value: str | None) -> str:
    if not value:
        return "not set"
    if len(value) <= 8:
        return f"{value[:1]}***{value[-1:]}"
    return f"{value[:5]}******{value[-3:]}"


class TelegramMonitoringRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_message(
        self, *, source_id: int, telegram_message_id: int, reply_to_message_id: int | None,
        text: str | None, message_date: str, message_url: str | None,
        prefilter_priority: int,
    ) -> tuple[int, bool]:
        connection = self.database.require_connection()
        now = utc_now_iso()
        cursor = await connection.execute(
            """
            INSERT OR IGNORE INTO telegram_messages (
                source_id, telegram_message_id, reply_to_message_id, text, message_date,
                message_url, prefilter_priority, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, telegram_message_id, reply_to_message_id, text, message_date,
             message_url, prefilter_priority, now),
        )
        created = cursor.rowcount == 1
        async with connection.execute(
            "SELECT id FROM telegram_messages WHERE source_id = ? AND telegram_message_id = ?",
            (source_id, telegram_message_id),
        ) as result:
            row = await result.fetchone()
        await connection.commit()
        return int(row["id"]), created

    async def pending(self, limit: int) -> list[dict]:
        connection = self.database.require_connection()
        async with connection.execute(
            """SELECT m.*, s.title AS source_title, s.username AS source_username
               FROM telegram_messages m JOIN sources s ON s.id = m.source_id
               WHERE m.analysis_status IN ('pending', 'error')
               ORDER BY m.prefilter_priority DESC, m.collected_at ASC LIMIT ?""",
            (limit,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def pending_count(self) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT COUNT(*) AS count FROM telegram_messages WHERE analysis_status IN ('pending','error')"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def health_stats(self) -> dict[str, int]:
        connection = self.database.require_connection()
        query = """SELECT
            (SELECT COUNT(*) FROM sources WHERE kind='telegram') AS sources,
            (SELECT COUNT(*) FROM sources WHERE kind='telegram' AND is_active=1) AS online,
            (SELECT COUNT(*) FROM telegram_messages
             WHERE substr(message_date,1,10)=date('now')) AS messages_today,
            (SELECT COUNT(*) FROM telegram_risk_alerts
             WHERE substr(created_at,1,10)=date('now')) AS alerts_today,
            (SELECT COUNT(*) FROM telegram_messages
             WHERE analysis_status IN ('pending','error')) AS queue"""
        async with connection.execute(query) as cursor:
            row = await cursor.fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    async def save_analysis(self, message_id: int, result, model: str, prompt_version: str) -> bool:
        connection = self.database.require_connection()
        now = utc_now_iso()
        cursor = await connection.execute(
            """INSERT OR IGNORE INTO telegram_message_analysis (
                message_id, risk, severity, confidence, categories_json, sentiment,
                topic, intent_level, reason, model, prompt_version, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, int(result.risk), result.severity, result.confidence,
             json.dumps(result.categories, ensure_ascii=False), result.sentiment, result.topic,
             result.intent_level, result.reason, model, prompt_version, now),
        )
        await connection.execute(
            "UPDATE telegram_messages SET analyzed_at=?, analysis_status='done', last_analysis_error=NULL WHERE id=?",
            (now, message_id),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def set_active(self, source_id: int, active: bool) -> bool:
        connection = self.database.require_connection()
        cursor = await connection.execute(
            "UPDATE sources SET is_active = ?, updated_at = ? WHERE id = ?",
            (int(active), utc_now_iso(), source_id),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def mark_error(self, message_id: int, error: str) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """UPDATE telegram_messages SET analysis_status='error',
               analysis_attempts=analysis_attempts+1, last_analysis_error=? WHERE id=?""",
            (error[:500], message_id),
        )
        await connection.commit()

    async def create_risk_alert(self, message_id: int) -> int | None:
        connection = self.database.require_connection()
        cursor = await connection.execute(
            "INSERT OR IGNORE INTO telegram_risk_alerts(message_id, created_at) VALUES (?, ?)",
            (message_id, utc_now_iso()),
        )
        await connection.commit()
        if cursor.rowcount != 1:
            return None
        return int(cursor.lastrowid)

    async def aggregate_day(self, date: str, source_id: int) -> dict:
        connection = self.database.require_connection()
        async with connection.execute(
            """SELECT COUNT(*) AS messages_count,
               SUM(CASE WHEN a.sentiment='positive' THEN 1 ELSE 0 END) AS positive_count,
               SUM(CASE WHEN a.sentiment='neutral' THEN 1 ELSE 0 END) AS neutral_count,
               SUM(CASE WHEN a.sentiment='negative' THEN 1 ELSE 0 END) AS negative_count,
               SUM(CASE WHEN a.severity=1 THEN 1 ELSE 0 END) AS severity_1_count,
               SUM(CASE WHEN a.severity=2 THEN 1 ELSE 0 END) AS severity_2_count,
               SUM(CASE WHEN a.severity=3 THEN 1 ELSE 0 END) AS severity_3_count
               FROM telegram_messages m
               LEFT JOIN telegram_message_analysis a ON a.message_id=m.id
               WHERE m.source_id=? AND substr(m.message_date,1,10)=?""",
            (source_id, date),
        ) as cursor:
            counts = dict(await cursor.fetchone())
        async with connection.execute(
            """SELECT a.topic, COUNT(*) AS count FROM telegram_message_analysis a
               JOIN telegram_messages m ON m.id=a.message_id
               WHERE m.source_id=? AND substr(m.message_date,1,10)=?
               GROUP BY a.topic ORDER BY count DESC""",
            (source_id, date),
        ) as cursor:
            topics = {row["topic"]: row["count"] for row in await cursor.fetchall()}
        result = {key: int(value or 0) for key, value in counts.items()}
        result["topics"] = topics
        await connection.execute(
            """INSERT INTO telegram_daily_stats (
               date, source_id, messages_count, positive_count, neutral_count, negative_count,
               severity_1_count, severity_2_count, severity_3_count, topics_json, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, source_id) DO UPDATE SET
               messages_count=excluded.messages_count, positive_count=excluded.positive_count,
               neutral_count=excluded.neutral_count, negative_count=excluded.negative_count,
               severity_1_count=excluded.severity_1_count,
               severity_2_count=excluded.severity_2_count,
               severity_3_count=excluded.severity_3_count,
               topics_json=excluded.topics_json, updated_at=excluded.updated_at""",
            (date, source_id, result["messages_count"], result["positive_count"],
             result["neutral_count"], result["negative_count"], result["severity_1_count"],
             result["severity_2_count"], result["severity_3_count"],
             json.dumps(topics, ensure_ascii=False), utc_now_iso()),
        )
        await connection.commit()
        return result

    async def chart_rows(self, days: int = 30) -> list[dict]:
        connection = self.database.require_connection()
        async with connection.execute(
            """SELECT d.*, s.title FROM telegram_daily_stats d
               JOIN sources s ON s.id=d.source_id
               WHERE d.date >= date('now', ?) ORDER BY d.date, s.title""",
            (f"-{max(1, days)} days",),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def list_risk_alerts(
        self, *, days: int = 7, severity: int | None = None, limit: int = 20
    ) -> list[dict]:
        connection = self.database.require_connection()
        conditions = ["r.created_at >= datetime('now', ?)"]
        params: list[object] = [f"-{max(1, days)} days"]
        if severity is not None:
            conditions.append("a.severity = ?")
            params.append(severity)
        params.append(limit)
        async with connection.execute(
            f"""SELECT r.id AS alert_id, r.status, r.verdict, r.created_at,
                m.text, m.message_url, m.telegram_message_id,
                s.title AS source_title, a.severity, a.confidence,
                a.categories_json, a.reason
                FROM telegram_risk_alerts r
                JOIN telegram_messages m ON m.id=r.message_id
                JOIN telegram_message_analysis a ON a.message_id=m.id
                JOIN sources s ON s.id=m.source_id
                WHERE {' AND '.join(conditions)}
                ORDER BY r.created_at DESC LIMIT ?""",
            tuple(params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def set_alert_feedback(self, alert_id: int, verdict: str, admin_id: int) -> bool:
        if verdict not in {"confirmed", "false_positive", "reviewed"}:
            raise ValueError("Unknown alert verdict")
        connection = self.database.require_connection()
        cursor = await connection.execute(
            """UPDATE telegram_risk_alerts SET status='reviewed', verdict=?,
               reviewed_by=?, reviewed_at=? WHERE id=?""",
            (verdict, admin_id, utc_now_iso(), alert_id),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def digest_rows(self, date: str, source_id: int | None = None) -> list[dict]:
        connection = self.database.require_connection()
        params: list[object] = [date]
        source_filter = ""
        if source_id is not None:
            source_filter = " AND m.source_id=?"
            params.append(source_id)
        async with connection.execute(
            f"""SELECT m.text, m.message_date, m.source_id, s.title AS source_title,
                a.risk, a.severity, a.confidence, a.sentiment, a.topic, a.reason
                FROM telegram_messages m
                JOIN sources s ON s.id=m.source_id
                LEFT JOIN telegram_message_analysis a ON a.message_id=m.id
                WHERE substr(m.message_date,1,10)=? {source_filter}
                ORDER BY a.severity DESC, a.confidence DESC, m.message_date DESC""",
            tuple(params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


class VkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_source(
        self,
        *,
        group_id: int,
        group_name: str | None,
        screen_name: str | None,
        monitor_mode: str,
    ) -> VkSource:
        connection = self.database.require_connection()
        now = utc_now_iso()
        await connection.execute(
            """
            INSERT INTO vk_sources (
                group_id, group_name, screen_name, is_active, monitor_mode, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                group_name = excluded.group_name,
                screen_name = excluded.screen_name,
                monitor_mode = excluded.monitor_mode,
                updated_at = excluded.updated_at
            """,
            (group_id, group_name, screen_name, monitor_mode, now, now),
        )
        await connection.commit()
        source = await self.get_source(group_id)
        if source is None:
            raise RuntimeError("Upserted VK source disappeared.")
        return source

    async def get_source(self, group_id: int) -> VkSource | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT * FROM vk_sources WHERE group_id = ?",
            (group_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return VkSource.from_row(row) if row else None

    async def set_source_active(self, group_id: int, is_active: bool) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            "UPDATE vk_sources SET is_active = ?, updated_at = ? WHERE group_id = ?",
            (int(is_active), utc_now_iso(), group_id),
        )
        await connection.commit()

    async def upsert_post(
        self,
        *,
        group_id: int,
        post_id: int,
        owner_id: int,
        text: str | None,
        date: str,
        likes_count: int,
        comments_count: int,
        reposts_count: int,
        views_count: int,
        url: str | None,
    ) -> tuple[VkPost, bool]:
        connection = self.database.require_connection()
        now = utc_now_iso()
        existing = await self.get_post(group_id, post_id)
        await connection.execute(
            """
            INSERT INTO vk_posts (
                group_id, post_id, owner_id, text, date, likes_count, comments_count,
                reposts_count, views_count, url, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, post_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                text = excluded.text,
                date = excluded.date,
                likes_count = excluded.likes_count,
                comments_count = excluded.comments_count,
                reposts_count = excluded.reposts_count,
                views_count = excluded.views_count,
                url = excluded.url,
                updated_at = excluded.updated_at
            """,
            (
                group_id,
                post_id,
                owner_id,
                text,
                date,
                likes_count,
                comments_count,
                reposts_count,
                views_count,
                url,
                now,
                now,
            ),
        )
        await connection.commit()
        post = await self.get_post(group_id, post_id)
        if post is None:
            raise RuntimeError("Upserted VK post disappeared.")
        return post, existing is None

    async def get_post(self, group_id: int, post_id: int) -> VkPost | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT * FROM vk_posts WHERE group_id = ? AND post_id = ?",
            (group_id, post_id),
        ) as cursor:
            row = await cursor.fetchone()
        return VkPost.from_row(row) if row else None

    async def adjust_post_counters(
        self,
        group_id: int,
        post_id: int,
        *,
        likes_delta: int = 0,
        comments_delta: int = 0,
    ) -> VkPost | None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            UPDATE vk_posts
            SET
                likes_count = MAX(0, likes_count + ?),
                comments_count = MAX(0, comments_count + ?),
                updated_at = ?
            WHERE group_id = ? AND post_id = ?
            """,
            (likes_delta, comments_delta, utc_now_iso(), group_id, post_id),
        )
        await connection.commit()
        return await self.get_post(group_id, post_id)

    async def upsert_comment(
        self,
        *,
        group_id: int,
        post_id: int,
        comment_id: int,
        from_id: int | None,
        text: str | None,
        date: str,
        parent_comment_id: int | None,
        is_deleted: bool,
    ) -> tuple[VkComment, bool]:
        connection = self.database.require_connection()
        now = utc_now_iso()
        existing = await self.get_comment(group_id, post_id, comment_id)
        await connection.execute(
            """
            INSERT INTO vk_comments (
                group_id, post_id, comment_id, from_id, text, date,
                parent_comment_id, is_deleted, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, post_id, comment_id) DO UPDATE SET
                from_id = excluded.from_id,
                text = excluded.text,
                date = excluded.date,
                parent_comment_id = excluded.parent_comment_id,
                is_deleted = excluded.is_deleted,
                updated_at = excluded.updated_at
            """,
            (
                group_id,
                post_id,
                comment_id,
                from_id,
                text,
                date,
                parent_comment_id,
                int(is_deleted),
                now,
                now,
            ),
        )
        await connection.commit()
        comment = await self.get_comment(group_id, post_id, comment_id)
        if comment is None:
            raise RuntimeError("Upserted VK comment disappeared.")
        return comment, existing is None

    async def get_comment(self, group_id: int, post_id: int, comment_id: int) -> VkComment | None:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM vk_comments
            WHERE group_id = ? AND post_id = ? AND comment_id = ?
            """,
            (group_id, post_id, comment_id),
        ) as cursor:
            row = await cursor.fetchone()
        return VkComment.from_row(row) if row else None

    async def create_snapshot(self, post: VkPost) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            """
            INSERT INTO vk_stats_snapshots (
                group_id, post_id, likes_count, comments_count,
                reposts_count, views_count, checked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post.group_id,
                post.post_id,
                post.likes_count,
                post.comments_count,
                post.reposts_count,
                post.views_count,
                utc_now_iso(),
            ),
        )
        await connection.commit()

    async def list_posts_by_period(self, group_id: int, start: str, end: str) -> list[VkPost]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM vk_posts
            WHERE group_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (group_id, start, end),
        ) as cursor:
            rows = await cursor.fetchall()
        return [VkPost.from_row(row) for row in rows]

    async def list_recent_posts(self, group_id: int, limit: int = 10) -> list[VkPost]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM vk_posts
            WHERE group_id = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (group_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [VkPost.from_row(row) for row in rows]

    async def list_comments_by_period(self, group_id: int, start: str, end: str) -> list[VkComment]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM vk_comments
            WHERE group_id = ? AND date >= ? AND date <= ? AND is_deleted = 0
            ORDER BY date ASC
            """,
            (group_id, start, end),
        ) as cursor:
            rows = await cursor.fetchall()
        return [VkComment.from_row(row) for row in rows]

    async def count_comments_by_period(self, group_id: int, start: str, end: str) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT COUNT(*) AS count FROM vk_comments
            WHERE group_id = ? AND date >= ? AND date <= ? AND is_deleted = 0
            """,
            (group_id, start, end),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def list_recent_comments_by_period(
        self,
        group_id: int,
        start: str,
        end: str,
        limit: int = 5,
    ) -> list[VkComment]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM vk_comments
            WHERE group_id = ? AND date >= ? AND date <= ? AND is_deleted = 0
            ORDER BY date DESC
            LIMIT ?
            """,
            (group_id, start, end, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [VkComment.from_row(row) for row in rows]

    async def list_recent_comments(self, group_id: int, limit: int = 10) -> list[VkComment]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT * FROM vk_comments
            WHERE group_id = ? AND is_deleted = 0
            ORDER BY date DESC
            LIMIT ?
            """,
            (group_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [VkComment.from_row(row) for row in rows]

    async def count_posts(self, group_id: int) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT COUNT(*) AS count FROM vk_posts WHERE group_id = ?",
            (group_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def count_comments(self, group_id: int) -> int:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT COUNT(*) AS count FROM vk_comments WHERE group_id = ? AND is_deleted = 0",
            (group_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])


class ReviewRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def _row_to_source(self, row) -> ReviewSource:
        return ReviewSource(
            id=int(row["id"]),
            platform=ReviewPlatform(row["platform"]),
            branch_name=str(row["branch_name"]),
            external_id=str(row["external_id"]),
            url=str(row["url"]),
            is_active=bool(row["is_active"]),
            is_initialized=bool(row["is_initialized"]),
            last_status=str(row["last_status"]),
            last_checked_at=row["last_checked_at"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
            consecutive_errors=int(row["consecutive_errors"]),
            health_alert_active=int(row["health_alert_active"])
            if "health_alert_active" in row.keys()
            else 0,
            health_alert_sent_at=row["health_alert_sent_at"]
            if "health_alert_sent_at" in row.keys()
            else None,
            backoff_until=row["backoff_until"],
            last_rating=float(row["last_rating"]) if row["last_rating"] is not None else None,
            total_reviews_count=int(row["total_reviews_count"])
            if row["total_reviews_count"] is not None
            else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_review(self, row) -> ReviewItem:
        branch_name = row["branch_name"] if "branch_name" in row.keys() else ""
        source_id = (
            int(row["source_id"])
            if "source_id" in row.keys() and row["source_id"] is not None
            else None
        )
        return ReviewItem(
            id=int(row["id"]),
            source_id=source_id,
            external_review_id=str(row["external_review_id"]),
            platform=ReviewPlatform(row["platform"]),
            branch_name=branch_name,
            author_name=str(row["author_name"] or "Аноним"),
            rating=int(row["rating"]) if row["rating"] is not None else 0,
            text=str(row["text"] or ""),
            published_at=str(row["published_at"] or ""),
            edited_at=row["edited_at"] if "edited_at" in row.keys() else None,
            review_url=row["review_url"] if "review_url" in row.keys() else None,
            raw_payload_json=row["raw_payload_json"] if "raw_payload_json" in row.keys() else None,
            telegram_parts_total=int(row["telegram_parts_total"])
            if "telegram_parts_total" in row.keys()
            else 1,
            telegram_parts_sent=int(row["telegram_parts_sent"])
            if "telegram_parts_sent" in row.keys()
            else 0,
            is_sent_to_telegram=bool(row["is_sent_to_telegram"])
            if "is_sent_to_telegram" in row.keys()
            else False,
            telegram_sent_at=row["telegram_sent_at"] if "telegram_sent_at" in row.keys() else None,
            last_delivery_error=row["last_delivery_error"]
            if "last_delivery_error" in row.keys()
            else None,
        )

    async def ensure_default_sources(self, sources: list[dict]) -> int:
        connection = self.database.require_connection()
        now = utc_now_iso()
        count = 0
        for s in sources:
            platform_val = (
                s["platform"].value if hasattr(s["platform"], "value") else str(s["platform"])
            )
            async with connection.execute(
                """
                INSERT INTO review_sources (
                    platform, branch_name, external_id, url, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, external_id) DO UPDATE SET
                    branch_name = excluded.branch_name,
                    url = excluded.url,
                    updated_at = excluded.updated_at
                """,
                (platform_val, s["branch_name"], str(s["external_id"]), s["url"], now, now),
            ) as cursor:
                count += cursor.rowcount
        await connection.commit()
        return count

    async def list_sources(self, only_active: bool = False) -> list[ReviewSource]:
        connection = self.database.require_connection()
        query = "SELECT * FROM review_sources"
        if only_active:
            query += " WHERE is_active = 1"
        query += " ORDER BY id"
        async with connection.execute(query) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_source(row) for row in rows]

    async def get_source_by_id(self, source_id: int) -> ReviewSource | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT * FROM review_sources WHERE id = ?",
            (source_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_source(row) if row is not None else None

    async def get_source_by_external_id(
        self, platform: str, external_id: str
    ) -> ReviewSource | None:
        connection = self.database.require_connection()
        async with connection.execute(
            "SELECT * FROM review_sources WHERE platform = ? AND external_id = ?",
            (str(platform), str(external_id)),
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_source(row) if row is not None else None

    async def set_source_active(self, source_id: int, is_active: bool) -> None:
        connection = self.database.require_connection()
        now = utc_now_iso()
        await connection.execute(
            "UPDATE review_sources SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if is_active else 0, now, source_id),
        )
        await connection.commit()

    async def mark_source_initialized(self, source_id: int) -> None:
        connection = self.database.require_connection()
        now = utc_now_iso()
        await connection.execute(
            "UPDATE review_sources SET is_initialized = 1, updated_at = ? WHERE id = ?",
            (now, source_id),
        )
        await connection.commit()

    async def update_source_status(
        self,
        source_id: int,
        *,
        status: str,
        checked_at: str,
        success: bool,
        error: str | None = None,
        consecutive_errors: int | None = None,
        health_alert_active: int | None = None,
        health_alert_sent_at: str | None = None,
        backoff_until: str | None = None,
        last_rating: float | None = None,
        total_reviews_count: int | None = None,
    ) -> None:
        connection = self.database.require_connection()
        now = utc_now_iso()
        fields = ["last_status = ?", "last_checked_at = ?", "updated_at = ?"]
        params: list[object] = [status, checked_at, now]

        if success:
            fields.append("last_success_at = ?")
            params.append(checked_at)
            fields.append("last_error = NULL")
        else:
            fields.append("last_error = ?")
            params.append(error or "Unknown error")

        if consecutive_errors is not None:
            fields.append("consecutive_errors = ?")
            params.append(consecutive_errors)

        if health_alert_active is not None:
            fields.append("health_alert_active = ?")
            params.append(health_alert_active)

        if health_alert_sent_at is not None:
            fields.append("health_alert_sent_at = ?")
            params.append(health_alert_sent_at)
        elif success and health_alert_active == 0:
            fields.append("health_alert_sent_at = NULL")

        if backoff_until is not None:
            fields.append("backoff_until = ?")
            params.append(backoff_until)
        elif success:
            fields.append("backoff_until = NULL")

        if last_rating is not None:
            fields.append("last_rating = ?")
            params.append(last_rating)

        if total_reviews_count is not None:
            fields.append("total_reviews_count = ?")
            params.append(total_reviews_count)

        params.append(source_id)
        query = f"UPDATE review_sources SET {', '.join(fields)} WHERE id = ?"
        await connection.execute(query, tuple(params))
        await connection.commit()

    async def is_review_known(self, platform: str, external_review_id: str) -> bool:
        connection = self.database.require_connection()
        platform_val = platform.value if hasattr(platform, "value") else str(platform)
        async with connection.execute(
            "SELECT 1 FROM reviews WHERE platform = ? AND external_review_id = ? LIMIT 1",
            (platform_val, str(external_review_id)),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def save_reviews(
        self,
        reviews: list[ReviewItem],
        source_id: int,
        *,
        mark_sent: bool = False,
    ) -> int:
        connection = self.database.require_connection()
        now = utc_now_iso()
        inserted_count = 0
        for r in reviews:
            platform_val = r.platform.value if hasattr(r.platform, "value") else str(r.platform)
            is_sent_val = 1 if mark_sent else 0
            sent_at_val = now if mark_sent else None
            async with connection.execute(
                """
                INSERT OR IGNORE INTO reviews (
                    source_id, platform, external_review_id, author_name,
                    rating, text, published_at, edited_at, review_url,
                    content_hash, first_seen_at, updated_at,
                    telegram_parts_total, telegram_parts_sent,
                    is_sent_to_telegram, telegram_sent_at, raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    platform_val,
                    str(r.external_review_id),
                    r.author_name,
                    r.rating,
                    r.text,
                    r.published_at,
                    r.edited_at,
                    r.review_url,
                    None,
                    now,
                    now,
                    r.telegram_parts_total,
                    r.telegram_parts_sent if not mark_sent else r.telegram_parts_total,
                    is_sent_val,
                    sent_at_val,
                    r.raw_payload_json,
                ),
            ) as cursor:
                if cursor.rowcount > 0:
                    inserted_count += 1
        await connection.commit()
        return inserted_count

    async def get_unsent_reviews(self, limit: int = 50) -> list[ReviewItem]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT r.*, s.branch_name
            FROM reviews r
            JOIN review_sources s ON r.source_id = s.id
            WHERE r.is_sent_to_telegram = 0
            ORDER BY r.published_at ASC, r.id ASC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_review(row) for row in rows]

    async def update_delivery_progress(
        self,
        review_id: int,
        *,
        parts_sent: int,
        parts_total: int,
        is_sent: bool,
        error: str | None = None,
        raw_payload_json: str | None = None,
    ) -> None:
        connection = self.database.require_connection()
        now = utc_now_iso()
        if is_sent:
            await connection.execute(
                """
                UPDATE reviews
                SET telegram_parts_sent = ?,
                    telegram_parts_total = ?,
                    is_sent_to_telegram = 1,
                    telegram_sent_at = ?,
                    last_delivery_error = NULL,
                    raw_payload_json = COALESCE(?, raw_payload_json),
                    updated_at = ?
                WHERE id = ?
                """,
                (parts_sent, parts_total, now, raw_payload_json, now, review_id),
            )
        else:
            await connection.execute(
                """
                UPDATE reviews
                SET telegram_parts_sent = ?,
                    telegram_parts_total = ?,
                    last_delivery_error = ?,
                    raw_payload_json = COALESCE(?, raw_payload_json),
                    updated_at = ?
                WHERE id = ?
                """,
                (parts_sent, parts_total, error, raw_payload_json, now, review_id),
            )
        await connection.commit()

    async def get_recent_reviews(self, limit: int = 10) -> list[ReviewItem]:
        connection = self.database.require_connection()
        async with connection.execute(
            """
            SELECT r.*, s.branch_name
            FROM reviews r
            JOIN review_sources s ON r.source_id = s.id
            ORDER BY r.published_at DESC, r.id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_review(row) for row in rows]

    async def get_stats(self) -> dict:
        connection = self.database.require_connection()
        async with connection.execute("SELECT COUNT(*) AS c FROM review_sources") as cur:
            total_sources = int((await cur.fetchone())["c"])
        async with connection.execute(
            "SELECT COUNT(*) AS c FROM review_sources WHERE is_active = 1"
        ) as cur:
            active_sources = int((await cur.fetchone())["c"])
        async with connection.execute(
            "SELECT COUNT(*) AS c FROM review_sources WHERE health_alert_active = 1"
        ) as cur:
            degraded_sources = int((await cur.fetchone())["c"])
        async with connection.execute("SELECT COUNT(*) AS c FROM reviews") as cur:
            total_reviews = int((await cur.fetchone())["c"])
        async with connection.execute(
            "SELECT COUNT(*) AS c FROM reviews WHERE is_sent_to_telegram = 0"
        ) as cur:
            unsent_reviews = int((await cur.fetchone())["c"])
        return {
            "total_sources": total_sources,
            "active_sources": active_sources,
            "degraded_sources": degraded_sources,
            "total_reviews": total_reviews,
            "unsent_reviews": unsent_reviews,
        }


class RepositoryBundle:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.sources = SourceRepository(database)
        self.posts = PostRepository(database)
        self.comments = CommentRepository(database)
        self.group_messages = TelegramGroupMessageRepository(database)
        self.telegram_monitoring = TelegramMonitoringRepository(database)
        self.keywords = TelegramKeywordRepository(database)
        self.snapshots = StatsSnapshotRepository(database)
        self.alerts = AlertRepository(database)
        self.scheduler_state = SchedulerStateRepository(database)
        self.runtime_settings = RuntimeSettingsRepository(database)
        self.vk = VkRepository(database)
        self.reviews = ReviewRepository(database)

    def dashboard_service(self) -> DashboardService:
        return DashboardService(
            sources=self.sources,
            posts=self.posts,
            comments=self.comments,
            group_messages=self.group_messages,
            vk=self.vk,
        )
