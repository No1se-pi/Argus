from datetime import UTC, datetime

from app.analytics.dashboard import DashboardService
from app.storage.database import Database
from app.storage.models import Comment, Post, Source, VkComment, VkPost, VkSource


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SourceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_sources(self, include_inactive: bool = False) -> list[Source]:
        connection = self.database.require_connection()
        query = "SELECT * FROM sources"
        params: tuple[object, ...] = ()
        if not include_inactive:
            query += " WHERE is_active = 1"
        query += " ORDER BY id"
        async with connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [Source.from_row(row) for row in rows]

    async def count_active(self) -> int:
        connection = self.database.require_connection()
        async with connection.execute("SELECT COUNT(*) AS count FROM sources WHERE is_active = 1") as cursor:
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

    async def upsert_telegram_source(
        self,
        *,
        link: str,
        username: str | None,
        title: str,
        entity_id: int,
        access_hash: int | None,
        entity_type: str,
    ) -> Source:
        connection = self.database.require_connection()
        now = utc_now_iso()
        existing = await self.get_by_telegram_entity(entity_id)
        if existing:
            await connection.execute(
                """
                UPDATE sources
                SET link = ?, username = ?, title = ?, telegram_access_hash = ?,
                    telegram_entity_type = ?, is_active = 1, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (link, username, title, access_hash, entity_type, now, existing.id),
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
                telegram_entity_type, is_active, created_at, updated_at
            )
            VALUES ('telegram', ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (link, username, title, entity_id, access_hash, entity_type, now, now),
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


class RepositoryBundle:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.sources = SourceRepository(database)
        self.posts = PostRepository(database)
        self.comments = CommentRepository(database)
        self.snapshots = StatsSnapshotRepository(database)
        self.alerts = AlertRepository(database)
        self.scheduler_state = SchedulerStateRepository(database)
        self.runtime_settings = RuntimeSettingsRepository(database)
        self.vk = VkRepository(database)

    def dashboard_service(self) -> DashboardService:
        return DashboardService(
            sources=self.sources,
            posts=self.posts,
            comments=self.comments,
        )
