import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.storage.models import (
    Post,
    Source,
    TelegramGroupMessage,
    TelegramKeyword,
    VkComment,
    VkPost,
)
from app.storage.repositories import AlertRepository, RuntimeSettingsRepository

logger = logging.getLogger(__name__)
UTC = timezone.utc


class AlertService:
    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        alerts: AlertRepository,
        runtime_settings: RuntimeSettingsRepository | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.alerts = alerts
        self.runtime_settings = runtime_settings

    async def send_new_post_alert(self, source: Source, post: Post) -> None:
        if not await self._telegram_alert_enabled("post"):
            return
        targets = self._alert_targets()
        message = self._render_new_post(source, post)
        for chat_id in targets:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    disable_web_page_preview=True,
                )
            except TelegramAPIError as exc:
                logger.warning("Failed to send alert to chat %s: %s", chat_id, exc)
                await self.alerts.create_alert(
                    source_id=source.id,
                    post_id=post.id,
                    alert_type="new_post",
                    chat_id=chat_id,
                    message=message,
                    status="failed",
                    sent_at=None,
                )
                continue

            await self.alerts.create_alert(
                source_id=source.id,
                post_id=post.id,
                alert_type="new_post",
                chat_id=chat_id,
                message=message,
                status="sent",
                sent_at=datetime.now(UTC).isoformat(),
            )

    def _render_new_post(self, source: Source, post: Post) -> str:
        text = (post.text or "").replace("\n", " ").strip()
        if len(text) > 300:
            text = f"{text[:297]}..."
        if not text:
            text = "(без текста)"

        lines = [
            "<b>Argus alert: новый пост</b>",
            f"Источник: {escape(source.display_name)}",
            f"Дата: {escape(post.date)}",
            f"Текст: {escape(text)}",
        ]
        if post.post_url:
            lines.append(f"Ссылка: {escape(post.post_url)}")
        return "\n".join(lines)

    async def send_telegram_comment_alert(
        self,
        source: Source,
        message: TelegramGroupMessage,
    ) -> None:
        if not await self._telegram_alert_enabled("comment"):
            return
        await self._send_platform_alert(
            platform="telegram",
            source_id=source.id,
            item_type="discussion_message",
            item_id=f"{source.id}:{message.telegram_message_id}",
            alert_type="new_comment",
            message=self._render_telegram_comment(source, message),
        )

    async def send_telegram_comment_summary(
        self,
        source: Source,
        total_count: int,
        sent_count: int,
    ) -> None:
        if not await self._telegram_alert_enabled("comment"):
            return
        message = "\n".join(
            [
                "<b>Argus alert: новые комментарии</b>",
                f"Источник: {escape(source.display_name)}",
                f"Новых сообщений: {total_count}",
                f"Подробно отправлено: {sent_count}",
            ]
        )
        await self._send_platform_alert(
            platform="telegram",
            source_id=source.id,
            item_type="discussion_summary",
            item_id=f"{source.id}:summary:{datetime.now(UTC).isoformat()}",
            alert_type="new_comments_summary",
            message=message,
        )

    async def send_keyword_post_alert(
        self,
        source: Source,
        post: Post,
        keywords: list[TelegramKeyword],
    ) -> None:
        if not await self._telegram_alert_enabled("keyword"):
            return
        message = self._render_keyword_post(source, post, keywords)
        await self._send_platform_alert(
            platform="telegram",
            source_id=source.id,
            item_type="post",
            item_id=f"{source.id}:{post.telegram_message_id}:keywords",
            alert_type="keyword_post",
            message=message,
        )

    async def send_vk_post_alert(self, post: VkPost) -> None:
        if not await self._vk_alert_enabled("post"):
            logger.info(
                "VK post alert skipped: disabled for group_id=%s post_id=%s",
                post.group_id,
                post.post_id,
            )
            return
        message = self._render_vk_post(post)
        await self._send_platform_alert(
            platform="vk",
            source_id=None,
            item_type="post",
            item_id=f"{post.group_id}:{post.post_id}",
            alert_type="new_post",
            message=message,
        )

    async def send_vk_comment_alert(self, comment: VkComment) -> None:
        if not await self._vk_alert_enabled("comment"):
            logger.info(
                "VK comment alert skipped: disabled for group_id=%s post_id=%s comment_id=%s",
                comment.group_id,
                comment.post_id,
                comment.comment_id,
            )
            return
        message = self._render_vk_comment(comment)
        await self._send_platform_alert(
            platform="vk",
            source_id=None,
            item_type="comment",
            item_id=f"{comment.group_id}:{comment.post_id}:{comment.comment_id}",
            alert_type="new_comment",
            message=message,
        )

    async def _send_platform_alert(
        self,
        *,
        platform: str,
        source_id: int | None,
        item_type: str,
        item_id: str,
        alert_type: str,
        message: str,
    ) -> None:
        targets = self._alert_targets()
        for chat_id in targets:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    disable_web_page_preview=True,
                )
            except TelegramAPIError as exc:
                logger.warning("Failed to send %s alert to chat %s: %s", platform, chat_id, exc)
                await self.alerts.create_platform_alert(
                    platform=platform,
                    source_id=source_id,
                    item_type=item_type,
                    item_id=item_id,
                    alert_type=alert_type,
                    chat_id=chat_id,
                    message=message,
                    status="failed",
                    sent_at=None,
                )
                continue

            await self.alerts.create_platform_alert(
                platform=platform,
                source_id=source_id,
                item_type=item_type,
                item_id=item_id,
                alert_type=alert_type,
                chat_id=chat_id,
                message=message,
                status="sent",
                sent_at=datetime.now(UTC).isoformat(),
            )
            logger.info(
                "Sent %s %s alert for %s to chat %s",
                platform,
                alert_type,
                item_id,
                chat_id,
            )

    def _render_vk_post(self, post: VkPost) -> str:
        text = (post.text or "").replace("\n", " ").strip()
        if len(text) > 300:
            text = f"{text[:297]}..."
        if not text:
            text = "(без текста)"
        lines = [
            "<b>VK alert: новый пост</b>",
            f"Группа: {post.group_id}",
            f"Дата: {escape(post.date)}",
            f"Текст: {escape(text)}",
        ]
        if post.url:
            lines.append(f"Ссылка: {escape(post.url)}")
        return "\n".join(lines)

    async def _vk_alert_enabled(self, item_type: str) -> bool:
        if self.runtime_settings is None:
            return self.settings.alerts_vk_enabled
        if not await self.runtime_settings.get_bool(
            "alerts_vk_enabled",
            self.settings.alerts_vk_enabled,
        ):
            return False
        if item_type == "post":
            return await self.runtime_settings.get_bool(
                "alerts_vk_posts_enabled",
                self.settings.alerts_vk_posts_enabled,
            )
        if item_type == "comment":
            return await self.runtime_settings.get_bool(
                "alerts_vk_comments_enabled",
                self.settings.alerts_vk_comments_enabled,
            )
        return True

    async def _telegram_alert_enabled(self, item_type: str) -> bool:
        if self.runtime_settings is None:
            return self.settings.alerts_telegram_enabled
        if not await self.runtime_settings.get_bool(
            "alerts_telegram_enabled",
            self.settings.alerts_telegram_enabled,
        ):
            return False
        if item_type == "post":
            return await self.runtime_settings.get_bool(
                "alerts_telegram_posts_enabled",
                self.settings.alerts_telegram_posts_enabled,
            )
        if item_type == "comment":
            return await self.runtime_settings.get_bool(
                "alerts_telegram_comments_enabled",
                self.settings.alerts_telegram_comments_enabled,
            )
        if item_type == "keyword":
            return await self.runtime_settings.get_bool(
                "alerts_telegram_keywords_enabled",
                self.settings.alerts_telegram_keywords_enabled,
            )
        return True

    def _render_telegram_comment(
        self,
        source: Source,
        message: TelegramGroupMessage,
    ) -> str:
        text = (message.text or "").replace("\n", " ").strip()
        if len(text) > 300:
            text = f"{text[:297]}..."
        if not text:
            text = "(без текста)"
        lines = [
            "<b>Argus alert: новый комментарий</b>",
            f"Источник: {escape(source.display_name)}",
            f"Дата: {escape(message.date)}",
            f"Автор: {message.from_id or 'unknown'}",
            f"Комментарий: {escape(text)}",
        ]
        if message.message_url:
            lines.append(f"Ссылка: {escape(message.message_url)}")
        return "\n".join(lines)

    def _alert_targets(self) -> list[int]:
        if self.settings.alert_chat_id:
            return [self.settings.alert_chat_id]
        return list(self.settings.admin_ids)

    def _render_keyword_post(
        self,
        source: Source,
        post: Post,
        keywords: list[TelegramKeyword],
    ) -> str:
        text = (post.text or "").replace("\n", " ").strip()
        if len(text) > 300:
            text = f"{text[:297]}..."
        if not text:
            text = "(без текста)"
        keyword_text = ", ".join(escape(keyword.keyword) for keyword in keywords)
        lines = [
            "<b>Argus alert: пост по ключевым словам</b>",
            f"Источник: {escape(source.display_name)}",
            f"Ключи: {keyword_text}",
            f"Дата: {escape(post.date)}",
            f"Текст: {escape(text)}",
        ]
        if post.post_url:
            lines.append(f"Ссылка: {escape(post.post_url)}")
        return "\n".join(lines)

    def _render_vk_comment(self, comment: VkComment) -> str:
        text = (comment.text or "").replace("\n", " ").strip()
        if len(text) > 300:
            text = f"{text[:297]}..."
        if not text:
            text = "(без текста)"
        post_url = f"https://vk.com/wall-{abs(comment.group_id)}_{comment.post_id}"
        lines = [
            "<b>VK alert: новый комментарий</b>",
            f"Группа: {comment.group_id}",
            f"Пост: {comment.post_id}",
            f"Автор: {comment.from_id or 'unknown'}",
            f"Комментарий: {escape(text)}",
            f"Ссылка: {escape(post_url)}",
        ]
        return "\n".join(lines)
