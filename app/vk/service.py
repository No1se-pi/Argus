import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any

import aiohttp
from pydantic import SecretStr

from app.analytics.dashboard import _bucket_labels, _counts_by_bucket, _render_chart, _sum_by_bucket
from app.analytics.periods import parse_period
from app.config import Settings
from app.modules import ModuleInfo, ModuleStatus
from app.storage.models import VkComment, VkPost, VkSource
from app.storage.repositories import RuntimeSettingsRepository, VkRepository, mask_secret
from app.vk.client import VKAPIError, VKClient

logger = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class VKEffectiveConfig:
    enabled: bool
    group_token: str | None
    user_access_token: str | None
    group_id: int | None
    monitor_mode: str

    @property
    def any_token(self) -> str | None:
        return self.group_token or self.user_access_token

    @property
    def polling_token(self) -> str | None:
        return self.user_access_token


@dataclass(frozen=True)
class VKSyncResult:
    group_id: int
    posts_processed: int
    comments_processed: int
    new_posts: list[VkPost]
    new_comments: list[VkComment]


class VKService:
    def __init__(
        self,
        *,
        settings: Settings,
        runtime_settings: RuntimeSettingsRepository,
        repository: VkRepository,
    ) -> None:
        self.settings = settings
        self.runtime_settings = runtime_settings
        self.repository = repository
        self._last_error: str | None = None
        self._last_success_at: str | None = None
        self._longpoll_group_id: int | None = None
        self._longpoll_server: str | None = None
        self._longpoll_key: str | None = None
        self._longpoll_ts: str | None = None

    async def effective_config(self) -> VKEffectiveConfig:
        runtime_enabled = await self.runtime_settings.get("enable_vk_monitor")
        enabled = self.settings.enable_vk_monitor
        if runtime_enabled is not None:
            enabled = runtime_enabled.strip().lower() in {"1", "true", "yes", "on"}

        group_token = self._secret_value(self.settings.vk_group_token)
        if not group_token:
            group_token = self._secret_value(self.settings.vk_access_token)
        if not group_token:
            group_token = await self.runtime_settings.get("vk_group_token")
        if not group_token:
            group_token = await self.runtime_settings.get("vk_access_token")

        user_access_token = self._secret_value(self.settings.vk_user_access_token)
        if not user_access_token:
            user_access_token = await self.runtime_settings.get("vk_user_access_token")

        group_id = self.settings.vk_group_id
        if group_id is None:
            raw_group_id = await self.runtime_settings.get("vk_group_id")
            group_id = int(raw_group_id) if raw_group_id else None

        monitor_mode = await self.runtime_settings.get("vk_monitor_mode")
        return VKEffectiveConfig(
            enabled=enabled,
            group_token=group_token,
            user_access_token=user_access_token,
            group_id=abs(group_id) if group_id is not None else None,
            monitor_mode=monitor_mode or self.settings.vk_monitor_mode,
        )

    async def module_info(
        self,
        available_commands: list[str],
        *,
        check_network: bool = False,
    ) -> ModuleInfo:
        config = await self.effective_config()
        if not config.enabled:
            return ModuleInfo(
                name="VK Monitor",
                enabled=False,
                status=ModuleStatus.DISABLED,
                reason="VK Monitor is disabled",
                last_error=self._last_error,
            )

        missing = []
        if config.monitor_mode == "longpoll" and not config.group_token:
            missing.append("VK_GROUP_TOKEN")
        if config.monitor_mode == "polling" and not config.polling_token:
            missing.append("VK_USER_ACCESS_TOKEN")
        if not config.any_token:
            missing.append("VK_GROUP_TOKEN or VK_USER_ACCESS_TOKEN")
        if config.group_id is None:
            missing.append("VK_GROUP_ID")
        if missing:
            return ModuleInfo(
                name="VK Monitor",
                enabled=True,
                status=ModuleStatus.CONFIG_MISSING,
                reason=", ".join(missing) + " not set",
                last_error=self._last_error,
            )

        if check_network:
            try:
                await self.healthcheck()
            except Exception as exc:
                self._last_error = str(exc)
                return ModuleInfo(
                    name="VK Monitor",
                    enabled=True,
                    status=ModuleStatus.ERROR,
                    reason="VK API healthcheck failed",
                    last_error=self._last_error,
                )

        return ModuleInfo(
            name="VK Monitor",
            enabled=True,
            status=ModuleStatus.OK,
            reason="",
            last_error=self._last_error,
            available_commands=available_commands,
        )

    async def healthcheck(self) -> VkSource:
        config = await self._require_config()
        client = VKClient(access_token=config.any_token, api_version=self.settings.vk_api_version)
        group = await client.get_group(config.group_id)
        source = await self.repository.upsert_source(
            group_id=config.group_id,
            group_name=group.get("name"),
            screen_name=group.get("screen_name"),
            monitor_mode=config.monitor_mode,
        )
        self._last_error = None
        self._last_success_at = datetime.now(UTC).isoformat()
        return source

    async def sync_recent(self) -> VKSyncResult:
        config = await self._require_config()
        if not config.polling_token:
            raise VKAPIError(
                "VK polling sync needs VK_USER_ACCESS_TOKEN. "
                "A community key works for Long Poll events, but wall.get/wall.getComments "
                "are unavailable with group auth for this group.",
                code="polling_user_token_missing",
                method="wall.get",
            )
        client = VKClient(
            access_token=config.polling_token,
            api_version=self.settings.vk_api_version,
        )
        source = await self.healthcheck()
        try:
            posts = await client.get_wall_posts(
                config.group_id,
                count=self.settings.vk_recent_posts_limit,
            )
        except VKAPIError as exc:
            if exc.code == 27:
                raise VKAPIError(
                    "VK rejected wall.get with group auth. Put a user/admin token into "
                    "VK_USER_ACCESS_TOKEN, or use VK_MONITOR_MODE=longpoll for live events only.",
                    code=exc.code,
                    method=exc.method,
                ) from exc
            raise

        new_posts: list[VkPost] = []
        new_comments: list[VkComment] = []
        comments_processed = 0
        for raw_post in posts:
            post, created = await self._save_post(source, raw_post)
            await self.repository.create_snapshot(post)
            if created:
                new_posts.append(post)

            try:
                comments = await client.get_wall_comments(
                    config.group_id,
                    post.post_id,
                    count=100,
                )
            except VKAPIError as exc:
                if exc.code == 27:
                    raise VKAPIError(
                        "VK rejected wall.getComments with group auth. Put a user/admin token into "
                        "VK_USER_ACCESS_TOKEN, or use Long Poll for new comments.",
                        code=exc.code,
                        method=exc.method,
                    ) from exc
                raise
            comments_processed += len(comments)
            for raw_comment in comments:
                comment, created = await self._save_comment(
                    config.group_id,
                    post.post_id,
                    raw_comment,
                )
                if created:
                    new_comments.append(comment)

            await asyncio.sleep(0.35)

        self._last_error = None
        self._last_success_at = datetime.now(UTC).isoformat()
        return VKSyncResult(
            group_id=config.group_id,
            posts_processed=len(posts),
            comments_processed=comments_processed,
            new_posts=new_posts,
            new_comments=new_comments,
        )

    async def sync_now(self) -> VKSyncResult:
        config = await self._require_config()
        if config.polling_token:
            return await self.sync_recent()
        if config.group_token and config.monitor_mode == "longpoll":
            return await self.longpoll_once(wait_seconds=2)
        raise VKAPIError(
            "VK sync needs VK_USER_ACCESS_TOKEN for polling history "
            "or VK_GROUP_TOKEN for Long Poll.",
            code="sync_token_missing",
        )

    async def longpoll_once(self, *, wait_seconds: int = 25) -> VKSyncResult:
        config = await self._require_config()
        if not config.group_token:
            raise VKAPIError(
                "VK Long Poll needs VK_GROUP_TOKEN.",
                code="group_token_missing",
                method="groups.getLongPollServer",
            )
        client = VKClient(access_token=config.group_token, api_version=self.settings.vk_api_version)
        source = await self.repository.get_source(config.group_id)
        if source is None:
            source = await self.healthcheck()

        server, key, ts = await self._get_longpoll_state(client, config.group_id)
        params = {
            "act": "a_check",
            "key": key,
            "ts": ts,
            "wait": wait_seconds,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(server, params=params, timeout=wait_seconds + 10) as response:
                data = await response.json(content_type=None)

        if "failed" in data:
            failed_code = int(data["failed"])
            if failed_code == 1 and data.get("ts"):
                self._longpoll_ts = str(data["ts"])
                return VKSyncResult(config.group_id, 0, 0, [], [])
            await self._get_longpoll_state(client, config.group_id, force_refresh=True)
            raise VKAPIError(f"VK Long Poll failed with code {failed_code}")

        if data.get("ts"):
            self._longpoll_ts = str(data["ts"])

        updates = data.get("updates", [])
        new_posts: list[VkPost] = []
        new_comments: list[VkComment] = []
        for update in updates:
            event_type = update.get("type")
            event_object = update.get("object") or {}
            if not isinstance(event_object, dict):
                continue
            if isinstance(event_object.get("post"), dict):
                event_object = event_object["post"]
            if event_type == "wall_post_new":
                post, created = await self._save_post(source, event_object)
                await self.repository.create_snapshot(post)
                if created:
                    new_posts.append(post)
            elif event_type == "wall_reply_new":
                post_id = event_object.get("post_id")
                if post_id is None:
                    continue
                comment, created = await self._save_comment(
                    config.group_id,
                    int(post_id),
                    event_object,
                )
                if created:
                    new_comments.append(comment)
                    updated_post = await self.repository.adjust_post_counters(
                        config.group_id,
                        int(post_id),
                        comments_delta=1,
                    )
                    if updated_post is not None:
                        await self.repository.create_snapshot(updated_post)
            elif event_type in {"like_add", "like_remove"}:
                post_id = self._like_post_id(event_object, config.group_id)
                if post_id is None:
                    continue
                updated_post = await self.repository.adjust_post_counters(
                    config.group_id,
                    post_id,
                    likes_delta=1 if event_type == "like_add" else -1,
                )
                if updated_post is not None:
                    await self.repository.create_snapshot(updated_post)

        self._last_error = None
        self._last_success_at = datetime.now(UTC).isoformat()
        return VKSyncResult(
            group_id=config.group_id,
            posts_processed=len(new_posts),
            comments_processed=len(new_comments),
            new_posts=new_posts,
            new_comments=new_comments,
        )

    async def _get_longpoll_state(
        self,
        client: VKClient,
        group_id: int,
        *,
        force_refresh: bool = False,
    ) -> tuple[str, str, str]:
        if (
            force_refresh
            or self._longpoll_group_id != group_id
            or self._longpoll_server is None
            or self._longpoll_key is None
            or self._longpoll_ts is None
        ):
            server = await client.get_long_poll_server(group_id)
            self._longpoll_group_id = group_id
            self._longpoll_server = str(server["server"])
            self._longpoll_key = str(server["key"])
            self._longpoll_ts = str(server["ts"])

        return self._longpoll_server, self._longpoll_key, self._longpoll_ts

    async def render_status(self) -> str:
        config = await self.effective_config()
        info = await self.module_info(
            [
                "/vk_status",
                "/vk_setup",
                "/vk_recent_posts",
                "/vk_posts",
                "/vk_recent_comments",
                "/vk_sync",
                "/vk_dashboard",
            ],
            check_network=False,
        )
        group_token_status = "set" if config.group_token else "missing"
        user_token_status = "set" if config.user_access_token else "missing"
        group_status = str(config.group_id) if config.group_id is not None else "missing"
        posts_count = await self.repository.count_posts(config.group_id) if config.group_id else 0
        comments_count = (
            await self.repository.count_comments(config.group_id) if config.group_id else 0
        )
        return "\n".join(
            [
                "<b>VK status</b>",
                f"Enabled: {config.enabled}",
                f"Status: {info.status.value}",
                f"Reason: {escape(info.reason or 'ok')}",
                f"Group token: {group_token_status}",
                f"User token for polling: {user_token_status}",
                f"Group ID: {group_status}",
                f"Mode: {escape(config.monitor_mode)}",
                f"Last success: {self._last_success_at or 'never'}",
                f"Last error: {escape(self._last_error or 'none')}",
                f"Posts processed: {posts_count}",
                f"Comments processed: {comments_count}",
            ]
        )

    async def render_recent_posts(self, limit: int = 10) -> str:
        config = await self._require_config()
        limit = self._clamp_post_limit(limit)
        posts = await self.repository.list_recent_posts(config.group_id, limit)
        if not posts:
            return "VK posts: no data yet. Run /vk_sync first."
        lines = [f"<b>VK recent posts</b> (last {limit})"]
        for post in posts:
            lines.append(self._format_post_line(post))
        return self._trim_message("\n".join(lines))

    async def render_posts_by_period(self, period_value: str, limit: int = 10) -> str:
        config = await self._require_config()
        period = parse_period(period_value)
        limit = self._clamp_post_limit(limit)
        posts = await self.repository.list_posts_by_period(
            config.group_id,
            period.start_iso,
            period.end_iso,
        )
        total = len(posts)
        posts = sorted(posts, key=lambda item: item.date, reverse=True)[:limit]
        if not posts:
            return f"VK posts: no data for {escape(period.label)}. Run /vk_sync first."
        lines = [
            "<b>VK posts</b>",
            f"Период: {escape(period.label)}",
            f"Показано: {len(posts)} из {total}",
            "",
        ]
        for post in posts:
            lines.append(self._format_post_line(post))
        return self._trim_message("\n".join(lines))

    async def render_recent_comments(self, limit: int = 10) -> str:
        config = await self._require_config()
        limit = min(max(limit, 1), 20)
        comments = await self.repository.list_recent_comments(config.group_id, limit)
        if not comments:
            return "VK comments: no data yet. Run /vk_sync first."
        lines = [f"<b>VK recent comments</b> (last {limit})"]
        for comment in comments:
            text = self._short(comment.text or "(no text)", 80)
            lines.append(f"- post {comment.post_id}: {escape(text)}")
        return "\n".join(lines)

    async def render_dashboard(self, period_value: str) -> str:
        config = await self._require_config()
        period = parse_period(period_value)
        source = await self.repository.get_source(config.group_id)
        posts = await self.repository.list_posts_by_period(
            config.group_id,
            period.start_iso,
            period.end_iso,
        )
        comments_count = await self.repository.count_comments_by_period(
            config.group_id,
            period.start_iso,
            period.end_iso,
        )
        recent_comments = await self.repository.list_recent_comments_by_period(
            config.group_id,
            period.start_iso,
            period.end_iso,
            limit=5,
        )
        likes_total = sum(post.likes_count for post in posts)
        posts_count = len(posts)
        avg_likes = likes_total / posts_count if posts_count else 0
        avg_comments = comments_count / posts_count if posts_count else 0
        top_likes = sorted(posts, key=lambda item: item.likes_count, reverse=True)[:3]
        top_comments = sorted(posts, key=lambda item: item.comments_count, reverse=True)[:3]

        return "\n".join(
            [
                "<b>VK dashboard</b>",
                f"Группа: {escape(source.display_name if source else str(config.group_id))}",
                f"Период: {escape(period.label)}",
                "",
                f"Постов: {posts_count}",
                f"Комментариев: {comments_count}",
                f"Лайков всего: {likes_total}",
                f"Среднее лайков на пост: {avg_likes:.2f}",
                f"Среднее комментариев на пост: {avg_comments:.2f}",
                "",
                "Топ постов по лайкам:",
                *self._format_top_posts(top_likes, "likes"),
                "",
                "Топ постов по комментариям:",
                *self._format_top_posts(top_comments, "comments"),
                "",
                "Новые комментарии:",
                *self._format_comments(recent_comments),
            ]
        )

    async def render_dashboard_chart_png(self, period_value: str) -> bytes | None:
        config = await self._require_config()
        period = parse_period(period_value)
        source = await self.repository.get_source(config.group_id)
        posts = await self.repository.list_posts_by_period(
            config.group_id,
            period.start_iso,
            period.end_iso,
        )
        comments = await self.repository.list_comments_by_period(
            config.group_id,
            period.start_iso,
            period.end_iso,
        )
        buckets = _bucket_labels(period.start_iso, period.end_iso, period_value)
        series = {
            "Посты": _counts_by_bucket([post.date for post in posts], buckets),
            "Комментарии": _counts_by_bucket([comment.date for comment in comments], buckets),
            "Лайки": _sum_by_bucket([(post.date, post.likes_count) for post in posts], buckets),
        }
        title = f"VK {source.display_name if source else config.group_id} · {period.label}"
        return _render_chart(
            title=title,
            labels=[label for label, _start, _end in buckets],
            series=series,
        )

    async def save_setup(
        self,
        *,
        group_token: str | None,
        user_access_token: str | None,
        group_id: int | None,
    ) -> None:
        if group_token:
            await self.runtime_settings.set("vk_group_token", group_token, is_secret=True)
        if user_access_token:
            await self.runtime_settings.set(
                "vk_user_access_token",
                user_access_token,
                is_secret=True,
            )
        if group_id is not None:
            await self.runtime_settings.set("vk_group_id", str(abs(group_id)), is_secret=False)
        await self.runtime_settings.set("enable_vk_monitor", "true", is_secret=False)

    async def config_summary(self) -> str:
        config = await self.effective_config()
        group_token = mask_secret(config.group_token)
        user_token = mask_secret(config.user_access_token)
        group_id = str(config.group_id) if config.group_id else "not set"
        missing = []
        if not config.group_token:
            missing.append("VK_GROUP_TOKEN")
        if config.group_id is None:
            missing.append("VK_GROUP_ID")
        missing_text = ", ".join(missing) if missing else "none"
        return "\n".join(
            [
                "<b>VK setup</b>",
                f"Group token: {escape(group_token)}",
                f"User token: {escape(user_token)}",
                f"Group ID: {escape(group_id)}",
                f"Missing: {escape(missing_text)}",
                "",
                "VK_GROUP_TOKEN нужен для Long Poll событий сообщества.",
                "VK_USER_ACCESS_TOKEN нужен для /vk_sync истории через wall.get/wall.getComments.",
                "Из ссылки group-240114551 используйте VK_GROUP_ID=240114551.",
            ]
        )

    async def _save_post(self, source: VkSource, raw_post: dict[str, Any]) -> tuple[VkPost, bool]:
        post_id = int(raw_post["id"])
        owner_id = int(raw_post.get("owner_id") or -abs(source.group_id))
        post, created = await self.repository.upsert_post(
            group_id=source.group_id,
            post_id=post_id,
            owner_id=owner_id,
            text=raw_post.get("text"),
            date=self._timestamp_iso(raw_post.get("date")),
            likes_count=self._nested_count(raw_post, "likes"),
            comments_count=self._nested_count(raw_post, "comments"),
            reposts_count=self._nested_count(raw_post, "reposts"),
            views_count=self._nested_count(raw_post, "views"),
            url=f"https://vk.com/wall{owner_id}_{post_id}",
        )
        return post, created

    async def _save_comment(
        self,
        group_id: int,
        post_id: int,
        raw_comment: dict[str, Any],
    ) -> tuple[VkComment, bool]:
        await self._ensure_placeholder_post(group_id, post_id, raw_comment)
        return await self.repository.upsert_comment(
            group_id=group_id,
            post_id=post_id,
            comment_id=int(raw_comment["id"]),
            from_id=raw_comment.get("from_id"),
            text=raw_comment.get("text"),
            date=self._timestamp_iso(raw_comment.get("date")),
            parent_comment_id=raw_comment.get("reply_to_comment"),
            is_deleted=bool(raw_comment.get("deleted")),
        )

    async def _ensure_placeholder_post(
        self,
        group_id: int,
        post_id: int,
        raw_comment: dict[str, Any],
    ) -> None:
        if await self.repository.get_post(group_id, post_id) is not None:
            return
        owner_id = int(raw_comment.get("owner_id") or -abs(group_id))
        await self.repository.upsert_post(
            group_id=group_id,
            post_id=post_id,
            owner_id=owner_id,
            text=None,
            date=self._timestamp_iso(raw_comment.get("date")),
            likes_count=0,
            comments_count=1,
            reposts_count=0,
            views_count=0,
            url=f"https://vk.com/wall{owner_id}_{post_id}",
        )

    async def _require_config(self) -> VKEffectiveConfig:
        config = await self.effective_config()
        if not config.enabled:
            raise VKAPIError("VK Monitor is disabled.")
        missing = []
        if not config.any_token:
            missing.append("VK_GROUP_TOKEN or VK_USER_ACCESS_TOKEN")
        if config.group_id is None:
            missing.append("VK_GROUP_ID")
        if missing:
            raise VKAPIError(", ".join(missing) + " not set.")
        return config

    def _timestamp_iso(self, value: int | None) -> str:
        if value is None:
            return datetime.now(UTC).isoformat()
        return datetime.fromtimestamp(int(value), tz=UTC).isoformat()

    def _nested_count(self, payload: dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if isinstance(value, dict):
            return int(value.get("count") or 0)
        return 0

    def _like_post_id(self, payload: dict[str, Any], group_id: int) -> int | None:
        object_type = str(payload.get("object_type") or payload.get("type") or "").lower()
        if object_type and object_type not in {"post", "wall"}:
            return None

        owner_id = payload.get("object_owner_id") or payload.get("owner_id")
        if owner_id is not None:
            try:
                if int(owner_id) not in {-abs(group_id), abs(group_id)}:
                    return None
            except (TypeError, ValueError):
                return None

        for key in ("post_id", "object_id", "item_id"):
            raw_value = payload.get(key)
            if raw_value is None:
                continue
            try:
                post_id = int(raw_value)
            except (TypeError, ValueError):
                continue
            if post_id > 0:
                return post_id
        return None

    def _format_top_posts(self, posts: list[VkPost], metric: str) -> list[str]:
        if not posts:
            return ["Нет данных."]
        lines: list[str] = []
        for index, post in enumerate(posts, start=1):
            value = post.likes_count if metric == "likes" else post.comments_count
            lines.append(f"{index}. {self._post_snippet(post)} - {value}")
        return lines

    def _format_comments(self, comments: list[VkComment]) -> list[str]:
        if not comments:
            return ["Нет данных."]
        return [
            f"- post {comment.post_id}: {escape(self._short(comment.text or '', 80))}"
            for comment in comments
        ]

    def _format_post_line(self, post: VkPost) -> str:
        return (
            f"- {escape(post.date[:16])} · {self._post_snippet(post)} · "
            f"views: {post.views_count}, likes: {post.likes_count}, "
            f"comments: {post.comments_count}, reposts: {post.reposts_count}"
        )

    def _post_snippet(self, post: VkPost) -> str:
        text = self._short(post.text or f"post {post.post_id}", 80)
        if post.url:
            text = f"{text} ({post.url})"
        return escape(text)

    def _short(self, value: str, length: int) -> str:
        normalized = value.replace("\n", " ").strip()
        if len(normalized) <= length:
            return normalized
        return f"{normalized[: length - 3]}..."

    def _clamp_post_limit(self, limit: int) -> int:
        return min(max(limit, 1), 30)

    def _trim_message(self, text: str, limit: int = 3900) -> str:
        if len(text) <= limit:
            return text
        trimmed = text[: limit - 20].rsplit("\n", maxsplit=1)[0].strip()
        return f"{trimmed}\n...обрезано."

    def _secret_value(self, value: SecretStr | None) -> str | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        return secret or None
