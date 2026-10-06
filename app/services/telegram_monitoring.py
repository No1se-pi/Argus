import asyncio
import logging
import re
from datetime import timezone
from html import escape

from telethon import events, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.llm.ollama_client import OllamaUnavailable
from app.services.llm.prompts import PROMPT_VERSION

logger = logging.getLogger(__name__)
UTC = timezone.utc

RISK_TERMS = re.compile(
    r"\b(нож|оруж|угроз|убью|драк|перцов|алкогол|водк|наркот|бомб|взорв|суицид|"
    r"сломаем|украд|пронес[её]м|напад)\w*\b", re.I
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


def telegram_message_link(username: str | None, entity_id: int | None, message_id: int) -> str | None:
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

    async def start(self) -> None:
        await self.catch_up()
        self.client.add_event_handler(self._handler, events.NewMessage())
        logger.info("Telegram realtime collector started")

    async def stop(self) -> None:
        self.client.remove_event_handler(self._handler, events.NewMessage())

    async def catch_up(self) -> None:
        sources = await self.repositories.sources.list_sources()
        for source in sources:
            try:
                entity = self._entity(source)
                kwargs = {"limit": self.settings.telegram_catchup_limit, "reverse": True}
                if source.last_message_id:
                    kwargs["min_id"] = source.last_message_id
                messages = await self.client.get_messages(entity, **kwargs)
                if len(messages) >= self.settings.telegram_catchup_limit:
                    logger.warning("Telegram catch-up limit reached source_id=%s", source.id)
                for message in messages:
                    await self._store(source, message)
            except Exception as exc:
                logger.warning("Telegram source unavailable source_id=%s error=%s", source.id, exc)

    async def _on_new_message(self, event) -> None:
        try:
            source = await self.repositories.sources.get_by_telegram_reference(event.chat_id)
            if source is None or not source.is_active:
                return
            await self._store(source, event.message)
        except Exception:
            logger.exception("Telethon unexpected exception while collecting message")

    async def _store(self, source, message) -> None:
        if isinstance(message, types.MessageService) or not getattr(message, "id", None):
            return
        text = getattr(message, "message", None)
        reply = getattr(message, "reply_to_msg_id", None)
        raw_date = message.date
        if raw_date.tzinfo is None:
            raw_date = raw_date.replace(tzinfo=UTC)
        _, created = await self.repositories.telegram_monitoring.save_message(
            source_id=source.id, telegram_message_id=message.id,
            reply_to_message_id=reply, text=text,
            message_date=raw_date.astimezone(UTC).isoformat(),
            message_url=telegram_message_link(source.username, source.telegram_entity_id, message.id),
            prefilter_priority=prefilter_priority(text, has_reply=reply is not None),
        )
        if source.last_message_id is None or message.id > source.last_message_id:
            await self.repositories.sources.set_last_message_id(source.id, message.id)
        if created:
            logger.info("Telegram message collected source_id=%s message_id=%s", source.id, message.id)

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

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            rows = await self.repositories.telegram_monitoring.pending(self.settings.llm_batch_size)
            eligible = [row for row in rows if self.settings.llm_analyze_all_messages or row["prefilter_priority"] > 0]
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
                result = await self.classifier.classify(row["text"] or "")
                inserted = await self.repositories.telegram_monitoring.save_analysis(
                    row["id"], result, self.classifier.client.model, PROMPT_VERSION
                )
                if inserted and result.risk and result.severity >= self.settings.llm_alert_min_severity:
                    alert_id = await self.repositories.telegram_monitoring.create_risk_alert(row["id"])
                    if alert_id:
                        await self._send_alert(row, result, alert_id)
            except OllamaUnavailable as exc:
                await self.repositories.telegram_monitoring.mark_error(row["id"], str(exc))
                logger.warning("Ollama unavailable; analysis retained in queue: %s", exc)
                await asyncio.sleep(2)
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
            text += f"\n\n<a href=\"{escape(row['message_url'])}\">🔗 Открыть сообщение</a>"
        targets = [self.settings.alert_chat_id] if self.settings.alert_chat_id else list(self.settings.admin_ids)
        for chat_id in targets:
            try:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
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
                    [InlineKeyboardButton(
                        text="👁 Просмотрено",
                        callback_data=f"tgm:feedback:{alert_id}:reviewed",
                    )],
                ])
                await self.bot.send_message(
                    chat_id, text, disable_web_page_preview=True, reply_markup=keyboard
                )
            except Exception as exc:
                logger.warning("Failed to send risk alert chat_id=%s: %s", chat_id, exc)
