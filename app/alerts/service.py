import logging
from datetime import UTC, datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.storage.models import Post, Source, VkComment, VkPost
from app.storage.repositories import AlertRepository

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, *, bot: Bot, settings: Settings, alerts: AlertRepository) -> None:
        self.bot = bot
        self.settings = settings
        self.alerts = alerts

    async def send_new_post_alert(self, source: Source, post: Post) -> None:
        targets = [self.settings.alert_chat_id] if self.settings.alert_chat_id else self.settings.admin_ids
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

    async def send_vk_post_alert(self, post: VkPost) -> None:
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
        targets = [self.settings.alert_chat_id] if self.settings.alert_chat_id else self.settings.admin_ids
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
