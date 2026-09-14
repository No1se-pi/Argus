import json
import logging
from datetime import datetime

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone
    UTC = timezone.utc  # noqa: UP017
from html import escape
from typing import Any

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

    async def _reviews_alert_enabled(self) -> bool:
        if self.runtime_settings is None:
            return getattr(self.settings, "alerts_reviews_enabled", True)
        return await self.runtime_settings.get_bool(
            "alerts_reviews_enabled",
            getattr(self.settings, "alerts_reviews_enabled", True),
        )

    def _split_raw_text(self, raw_text: str, max_escaped_len: int = 3300) -> list[str]:
        """
        Split unescaped raw text into chunks so that each chunk's HTML-escaped
        length does not exceed max_escaped_len. Slicing raw text ensures that
        HTML entities (&amp;, &lt;, etc.) are NEVER cut across boundaries.
        """
        if not raw_text:
            return ["(без текста)"]

        chunks: list[str] = []
        remaining = raw_text

        while remaining:
            if len(escape(remaining)) <= max_escaped_len:
                chunks.append(remaining)
                break

            low = 1
            high = min(len(remaining), max_escaped_len)
            best_idx = 1

            while low <= high:
                mid = (low + high) // 2
                if len(escape(remaining[:mid])) <= max_escaped_len:
                    best_idx = mid
                    low = mid + 1
                else:
                    high = mid - 1

            search_start = max(1, best_idx - 200)
            split_idx = remaining.rfind("\n", search_start, best_idx)
            if split_idx == -1:
                split_idx = remaining.rfind(" ", search_start, best_idx)
            if split_idx == -1:
                split_idx = best_idx

            chunk = remaining[:split_idx]
            remaining = remaining[split_idx:]
            if remaining.startswith("\n"):
                remaining = remaining[1:]
            chunks.append(chunk)

        return chunks if chunks else [raw_text]

    def _render_review_parts(self, review: Any, source: Any) -> list[str]:
        platform_name = "Яндекс.Карты" if getattr(review, "platform", "") == "yandex" else "2ГИС"
        rating_num = getattr(review, "rating", 0) or 0
        stars = (
            "⭐" * max(1, min(5, rating_num)) + f" ({rating_num}/5)"
            if rating_num > 0
            else "Без оценки"
        )
        branch_name = getattr(
            source, "branch_name", getattr(review, "branch_name", "Учебное отделение")
        )
        author = getattr(review, "author_name", "Аноним") or "Аноним"
        published_at = getattr(review, "published_at", "")
        raw_text = getattr(review, "text", "") or "(без текста)"
        if raw_text.strip() == "TEXT_EMPTY":
            raw_text = "(без текста)"

        review_url = getattr(review, "review_url", None)
        link_html = f'\n\n🔗 <a href="{escape(review_url)}">Открыть отзыв</a>' if review_url else ""

        header_full = (
            "🆕 <b>Новый отзыв</b>\n\n"
            f"<b>Площадка:</b> {escape(platform_name)}\n"
            f"<b>Учебное отделение:</b> {escape(branch_name)}\n"
            f"<b>Автор:</b> {escape(author)}\n"
            f"<b>Оценка:</b> {stars}\n"
            f"<b>Дата:</b> {escape(published_at)}\n\n"
            "<b>Текст:</b>\n"
        )

        single_msg = header_full + escape(raw_text) + link_html
        if len(single_msg) <= 4000:
            return [single_msg]

        raw_chunks = self._split_raw_text(raw_text, max_escaped_len=3300)
        total_parts = len(raw_chunks)
        parts: list[str] = []

        for idx, raw_chunk in enumerate(raw_chunks):
            escaped_chunk = escape(raw_chunk)
            suffix = f"\n\n<i>[Часть {idx + 1}/{total_parts}]</i>"
            if idx == total_parts - 1 and link_html:
                suffix += link_html

            if idx == 0:
                parts.append(header_full + escaped_chunk + suffix)
            else:
                cont_header = (
                    "🆕 <b>Новый отзыв (продолжение)</b>\n"
                    f"<b>Учебное отделение:</b> {escape(branch_name)}\n\n"
                )
                parts.append(cont_header + escaped_chunk + suffix)

        return parts

    async def send_review_alert(
        self,
        review: Any,
        source: Any,
        repo: Any,
    ) -> bool:
        if not await self._reviews_alert_enabled():
            logger.info("Reviews alert skipped: disabled in settings")
            return False

        chunks = self._render_review_parts(review, source)
        total_parts = len(chunks)
        targets = self._alert_targets()

        if not targets:
            logger.warning("No alert targets configured for review alerts delivery")
            if repo is not None and hasattr(review, "id") and review.id:
                await repo.update_delivery_progress(
                    review.id,
                    parts_sent=0,
                    parts_total=total_parts,
                    is_sent=False,
                    error="No alert targets configured",
                    raw_payload_json=review.raw_payload_json
                    if hasattr(review, "raw_payload_json")
                    else None,
                )
            return False

        target_progress: dict[str, int] = {}
        if hasattr(review, "raw_payload_json") and review.raw_payload_json:
            try:
                payload = json.loads(review.raw_payload_json)
                if (
                    isinstance(payload, dict)
                    and "targets" in payload
                    and isinstance(payload["targets"], dict)
                ):
                    target_progress = {str(k): int(v) for k, v in payload["targets"].items()}
            except Exception:
                pass

        default_sent = getattr(review, "telegram_parts_sent", 0)
        for chat_id in targets:
            if str(chat_id) not in target_progress:
                target_progress[str(chat_id)] = default_sent

        platform_str = (
            review.platform.value
            if hasattr(getattr(review, "platform", None), "value")
            else str(getattr(review, "platform", "review"))
        )
        rev_id_str = str(getattr(review, "external_review_id", getattr(review, "id", "")))

        first_exception: Exception | None = None

        for chat_id in targets:
            chat_str = str(chat_id)
            current_sent = target_progress.get(chat_str, 0)
            if current_sent >= total_parts:
                continue

            for part_idx in range(current_sent, total_parts):
                chunk_text = chunks[part_idx]
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=chunk_text,
                        disable_web_page_preview=True,
                    )
                    target_progress[chat_str] = part_idx + 1
                    min_parts_sent = min(
                        (target_progress.get(str(t), 0) for t in targets), default=0
                    )
                    payload_json = json.dumps({"targets": target_progress})
                    if hasattr(review, "raw_payload_json"):
                        review.raw_payload_json = payload_json
                    if hasattr(review, "telegram_parts_sent"):
                        review.telegram_parts_sent = min_parts_sent
                    if repo is not None and hasattr(review, "id") and review.id:
                        await repo.update_delivery_progress(
                            review.id,
                            parts_sent=min_parts_sent,
                            parts_total=total_parts,
                            is_sent=False,
                            error=None,
                            raw_payload_json=payload_json,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to send review alert part %d to chat %s: %s",
                        part_idx + 1,
                        chat_id,
                        exc,
                    )
                    if first_exception is None:
                        first_exception = exc
                    await self.alerts.create_platform_alert(
                        platform=platform_str,
                        source_id=None,
                        item_type="review",
                        item_id=rev_id_str,
                        alert_type="new_review",
                        chat_id=chat_id,
                        message=chunk_text,
                        status="failed",
                        sent_at=None,
                    )
                    break

        min_parts_sent = min((target_progress.get(str(t), 0) for t in targets), default=0)
        all_completed = bool(targets) and all(
            target_progress.get(str(t), 0) >= total_parts for t in targets
        )
        payload_json = json.dumps({"targets": target_progress})
        if hasattr(review, "raw_payload_json"):
            review.raw_payload_json = payload_json
        if hasattr(review, "telegram_parts_sent"):
            review.telegram_parts_sent = min_parts_sent

        if repo is not None and hasattr(review, "id") and review.id:
            await repo.update_delivery_progress(
                review.id,
                parts_sent=min_parts_sent,
                parts_total=total_parts,
                is_sent=all_completed,
                error=str(first_exception) if first_exception else None,
                raw_payload_json=payload_json,
            )

        if all_completed:
            for chat_id in targets:
                await self.alerts.create_platform_alert(
                    platform=platform_str,
                    source_id=None,
                    item_type="review",
                    item_id=rev_id_str,
                    alert_type="new_review",
                    chat_id=chat_id,
                    message=chunks[0] if chunks else "",
                    status="sent",
                    sent_at=datetime.now(UTC).isoformat(),
                )
            return True

        if first_exception:
            raise first_exception

        return False

    async def send_review_health_alert(self, source: Any, error_message: str) -> bool:
        if not await self._reviews_alert_enabled():
            return False
        platform_name = "Яндекс.Карты" if getattr(source, "platform", "") == "yandex" else "2ГИС"
        lines = [
            "🔴 <b>Reviews Monitor: проблема источника</b>\n",
            f"<b>Учебное отделение:</b> {escape(getattr(source, 'branch_name', ''))}",
            f"<b>Площадка:</b> {escape(platform_name)}",
            f"<b>Подробности:</b> {escape(error_message)}",
        ]
        msg = "\n".join(lines)
        targets = self._alert_targets()
        delivered_any = False
        for chat_id in targets:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    disable_web_page_preview=True,
                )
                delivered_any = True
            except Exception as exc:
                logger.warning("Failed to send review health alert to chat %s: %s", chat_id, exc)
        return delivered_any

    async def send_review_recovery_alert(self, source: Any) -> bool:
        if not await self._reviews_alert_enabled():
            return False
        platform_name = "Яндекс.Карты" if getattr(source, "platform", "") == "yandex" else "2ГИС"
        lines = [
            "🟢 <b>Reviews Monitor: источник восстановлен</b>\n",
            f"<b>Учебное отделение:</b> {escape(getattr(source, 'branch_name', ''))}",
            f"<b>Площадка:</b> {escape(platform_name)}",
            "<b>Статус:</b> Сбор отзывов возобновлен в штатном режиме.",
        ]
        msg = "\n".join(lines)
        targets = self._alert_targets()
        delivered_any = False
        for chat_id in targets:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    disable_web_page_preview=True,
                )
                delivered_any = True
            except Exception as exc:
                logger.warning("Failed to send review recovery alert to chat %s: %s", chat_id, exc)
        return delivered_any

    async def send_reviews_scheduler_health_alert(
        self, crash_count: int, error_message: str
    ) -> bool:
        if not await self._reviews_alert_enabled():
            return False
        lines = [
            "🔴 <b>Reviews Monitor: сбой планировщика</b>\n",
            f"<b>Статус:</b> Цикл опроса завершился аварийно {crash_count} раз(а) подряд.",
            f"<b>Подробности:</b> {escape(error_message)}",
        ]
        msg = "\n".join(lines)
        targets = self._alert_targets()
        delivered_any = False
        for chat_id in targets:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    disable_web_page_preview=True,
                )
                delivered_any = True
            except Exception as exc:
                logger.warning("Failed to send scheduler health alert to chat %s: %s", chat_id, exc)
        return delivered_any

    async def send_reviews_scheduler_recovery_alert(self) -> bool:
        if not await self._reviews_alert_enabled():
            return False
        lines = [
            "🟢 <b>Reviews Monitor: планировщик восстановлен</b>\n",
            "<b>Статус:</b> Цикл опроса отзывов успешно завершен в штатном режиме.",
        ]
        msg = "\n".join(lines)
        targets = self._alert_targets()
        delivered_any = False
        for chat_id in targets:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    disable_web_page_preview=True,
                )
                delivered_any = True
            except Exception as exc:
                logger.warning(
                    "Failed to send scheduler recovery alert to chat %s: %s", chat_id, exc
                )
        return delivered_any
