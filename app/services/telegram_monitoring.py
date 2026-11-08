import asyncio
import logging
import re
from datetime import datetime, timezone
from html import escape
from time import monotonic

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import errors, events, types

from app.alerts.timing import is_live
from app.services.llm.ollama_client import OllamaUnavailable
from app.services.llm.prompts import PROMPT_VERSION

logger = logging.getLogger(__name__)
UTC = timezone.utc

RISK_TERMS = re.compile(
    r"\b(нож|оруж|угроз|убью|драк|перцов|алкогол|водк|наркот|бомб|взорв|суицид|"
    r"сломаем|украд|пронес[её]м|напад)\w*\b",
    re.I,
)
URL = re.compile(r"https?://|t\.me/", re.I)


def prefilter_priority(text: str | None, *, has_reply: bool = False) -> int:
    value = (text or "").strip()
    score = 0
    if RISK_TERMS.search(value):
        score += 5
    if URL.search(value):
        score += 2
    letters = [char for char in value if char.isalpha()]
    if len(letters) >= 8 and sum(char.isupper() for char in letters) / len(letters) >= 0.7:
        score += 2
    if has_reply:
        score += 1
    if len(value) >= 500:
        score += 1
    return score


def telegram_message_link(
    username: str | None, entity_id: int | None, message_id: int
) -> str | None:
    if username:
        return f"https://t.me/{username}/{message_id}"
    if entity_id is not None:
        return f"https://t.me/c/{abs(entity_id)}/{message_id}"
    return None


class TelegramRealtimeMonitor:
    def __init__(self, *, client, settings, repositories) -> None:
        self.client = client
        self.settings = settings
        self.repositories = repositories
        self._handler = self._on_new_message
        self._stop = asyncio.Event()
        self._blocked_until = 0.0

    async def start(self) -> None:
        await self.catch_up()
        self.client.add_event_handler(self._handler, events.NewMessage())
        logger.info("Telegram realtime collector started")

    async def stop(self) -> None:
        self._stop.set()
        self.client.remove_event_handler(self._handler, events.NewMessage())

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                await self.catch_up(alert_eligible=True)

    async def catch_up(self, *, alert_eligible: bool = False) -> None:
        if not await self._enabled():
            return
        if monotonic() < self._blocked_until:
            return
        sources = await self.repositories.sources.list_sources()
        for source in sources:
            if not source.is_active:
                continue
            try:
                cursor_key = f"telegram_risk_cursor:{source.id}"
                saved = await self.repositories.runtime_settings.get(cursor_key)
                cursor = int(saved) if saved is not None else source.last_message_id
                entity = self._entity(source)
                latest = await self.client.get_messages(entity, limit=1)
                if not latest:
                    continue
                head = max(message.id for message in latest if getattr(message, "id", None))
                if cursor is None:
                    await self.repositories.runtime_settings.set(cursor_key, str(head))
                    await self.repositories.sources.set_last_message_id(source.id, head)
                    logger.info(
                        "Telegram baseline source_id=%s cursor=%s (0 historical alerts)",
                        source.id,
                        head,
                    )
                    continue
                if head <= cursor:
                    await self.repositories.runtime_settings.set(cursor_key, str(head))
                    continue
                kwargs = {
                    "limit": self.settings.telegram_catchup_limit,
                    "min_id": cursor,
                    "max_id": head + 1,
                }
                if alert_eligible:
                    kwargs["reverse"] = True
                messages = await self.client.get_messages(entity, **kwargs)
                if len(messages) >= self.settings.telegram_catchup_limit:
                    logger.warning("Telegram catch-up limit reached source_id=%s", source.id)
                for message in sorted(messages, key=lambda item: item.id):
                    await self._store(source, message, alert_eligible=alert_eligible)
                next_cursor = head
                if alert_eligible and len(messages) >= self.settings.telegram_catchup_limit:
                    next_cursor = max((item.id for item in messages), default=cursor)
                await self.repositories.runtime_settings.set(cursor_key, str(next_cursor))
                if not alert_eligible:
                    await self.repositories.sources.set_last_message_id(source.id, head)
                logger.info(
                    "Telegram risk collection source_id=%s stored=%s cursor=%s live=%s",
                    source.id,
                    len(messages),
                    head,
                    alert_eligible,
                )
            except errors.FloodWaitError as exc:
                self._blocked_until = monotonic() + exc.seconds + 1
                logger.warning("Telegram risk polling paused for FloodWait seconds=%s", exc.seconds)
                break
            except Exception as exc:
                logger.warning("Telegram source unavailable source_id=%s error=%s", source.id, exc)

    async def _on_new_message(self, event) -> None:
        try:
            if not await self._enabled():
                return
            source = await self.repositories.sources.get_by_telegram_reference(event.chat_id)
            if source is None or not source.is_active:
                return
            await self._store(source, event.message)
        except Exception:
            logger.exception("Telethon unexpected exception while collecting message")

    async def _enabled(self) -> bool:
        return await self.repositories.runtime_settings.get_bool(
            "enable_telegram_monitor",
            getattr(self.settings, "enable_telegram_monitor", True),
        )

    async def _store(self, source, message, *, alert_eligible: bool = True) -> None:
        if isinstance(message, types.MessageService) or not getattr(message, "id", None):
            return
        text = getattr(message, "message", None)
        reply = getattr(message, "reply_to_msg_id", None)
        raw_date = message.date
        if raw_date.tzinfo is None:
            raw_date = raw_date.replace(tzinfo=UTC)
        queue_size = await self.repositories.telegram_monitoring.pending_count()
        analysis_status = (
            "deferred" if queue_size >= self.settings.telegram_analysis_queue_limit else "pending"
        )
        _, created = await self.repositories.telegram_monitoring.save_message(
            source_id=source.id,
            telegram_message_id=message.id,
            reply_to_message_id=reply,
            text=text,
            message_date=raw_date.astimezone(UTC).isoformat(),
            message_url=telegram_message_link(
                source.username, source.telegram_entity_id, message.id
            ),
            prefilter_priority=prefilter_priority(text, has_reply=reply is not None),
            analysis_status=analysis_status,
            alert_eligible=alert_eligible,
        )
        if created:
            logger.debug(
                "Telegram message collected source_id=%s message_id=%s", source.id, message.id
            )

    def _entity(self, source):
        if source.telegram_entity_type == "channel" and source.telegram_access_hash is not None:
            return types.InputPeerChannel(source.telegram_entity_id, source.telegram_access_hash)
        if source.telegram_entity_type == "chat":
            return types.InputPeerChat(source.telegram_entity_id)
        return source.username or source.link


class AnalysisWorker:
    def __init__(self, *, settings, repositories, classifier, bot) -> None:
        self.settings = settings
        self.repositories = repositories
        self.classifier = classifier
        self.bot = bot
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(settings.llm_max_concurrent_requests)
        self._delivery_lock = asyncio.Lock()
        self._started_at = datetime.now(UTC)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await self._deliver_alerts()
            queue_size = await self.repositories.telegram_monitoring.pending_count()
            available = max(0, self.settings.telegram_analysis_queue_limit - queue_size)
            if available:
                await self.repositories.telegram_monitoring.promote_deferred(
                    min(self.settings.llm_batch_size, available)
                )
            rows = await self.repositories.telegram_monitoring.pending(
                self.settings.llm_batch_size,
                priority_only=not self.settings.llm_analyze_all_messages,
            )
            eligible = [
                row
                for row in rows
                if self.settings.llm_analyze_all_messages or row["prefilter_priority"] > 0
            ]
            if not eligible:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                continue
            await asyncio.gather(*(self._process(row) for row in eligible))

    async def _process(self, row: dict) -> None:
        async with self._semaphore:
            try:
                result = await self.classifier.classify(
                    row["text"] or "", reply_text=row.get("reply_text")
                )
                inserted = await self.repositories.telegram_monitoring.save_analysis(
                    row["id"], result, self.classifier.client.model, PROMPT_VERSION
                )
                if (
                    inserted
                    and row["alert_eligible"]
                    and is_live(row.get("message_date"), self._started_at)
                    and result.risk
                    and result.severity >= self.settings.llm_alert_min_severity
                    and await self._alerts_enabled()
                ):
                    alert_id = await self.repositories.telegram_monitoring.create_risk_alert(
                        row["id"]
                    )
                    if alert_id:
                        await self._send_alert(row, result, alert_id)
            except OllamaUnavailable as exc:
                await self.repositories.telegram_monitoring.mark_error(row["id"], str(exc))
                logger.warning("Ollama unavailable; analysis retained in queue: %s", exc)
            except Exception as exc:
                await self.repositories.telegram_monitoring.mark_error(row["id"], str(exc))
                logger.warning("Telegram message analysis failed message_id=%s: %s", row["id"], exc)

    async def _send_alert(self, row: dict, result, alert_id: int) -> None:
        categories = ", ".join(result.categories) or "other"
        icons = {1: "🟡", 2: "🟠", 3: "🔴"}
        text = (
            "🚨 <b>Argus — требует внимания</b>\n\n"
            f"<b>Источник:</b> {escape(row['source_title'])}\n"
            f"<b>Категория:</b> {escape(categories)}\n"
            f"<b>Уровень:</b> {icons.get(result.severity, '⚪')} {result.severity} / 3\n\n"
            f"<b>Сообщение:</b> «{escape((row['text'] or '')[:1500])}»\n\n"
            f"<b>Почему:</b> {escape(result.reason)}\n"
            f"<b>Confidence:</b> {result.confidence:.0%}"
        )
        if row.get("message_url"):
            text += f'\n\n<a href="{escape(row["message_url"])}">🔗 Открыть сообщение</a>'
        targets = (
            [self.settings.alert_chat_id]
            if self.settings.alert_chat_id
            else list(self.settings.admin_ids)
        )
        await self.repositories.telegram_monitoring.queue_risk_deliveries(alert_id, targets, text)
        await self._deliver_alerts()

    async def _deliver_alerts(self) -> None:
        if not await self._alerts_enabled():
            return
        async with self._delivery_lock:
            for delivery in await self.repositories.telegram_monitoring.pending_risk_deliveries():
                await self._deliver_alert(delivery)

    async def _alerts_enabled(self) -> bool:
        return await self.repositories.runtime_settings.get_bool(
            "alerts_telegram_enabled",
            getattr(self.settings, "alerts_telegram_enabled", True),
        )

    async def _deliver_alert(self, delivery: dict) -> None:
        alert_id, chat_id = delivery["alert_id"], delivery["chat_id"]
        try:
            await self._send_delivery(chat_id, delivery["text"], alert_id)
        except Exception as exc:
            await self.repositories.telegram_monitoring.finish_risk_delivery(
                alert_id,
                chat_id,
                type(exc).__name__,
            )
            logger.warning(
                "Risk alert queued for retry alert_id=%s error=%s", alert_id, type(exc).__name__
            )
        else:
            await self.repositories.telegram_monitoring.finish_risk_delivery(alert_id, chat_id)

    async def _send_delivery(self, chat_id: int, text: str, alert_id: int) -> None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=f"tgm:feedback:{alert_id}:confirmed",
                    ),
                    InlineKeyboardButton(
                        text="❌ Ложное",
                        callback_data=f"tgm:feedback:{alert_id}:false_positive",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="👁 Просмотрено",
                        callback_data=f"tgm:feedback:{alert_id}:reviewed",
                    )
                ],
            ]
        )
        await self.bot.send_message(
            chat_id, text, disable_web_page_preview=True, reply_markup=keyboard
        )
