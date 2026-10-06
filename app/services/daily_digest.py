import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.services.llm.digest import aggregate_messages, digest_prompt
from app.services.llm.ollama_client import OllamaUnavailable

logger = logging.getLogger(__name__)


class DailyDigestService:
    def __init__(self, *, settings, repositories, bot, llm_client=None) -> None:
        self.settings = settings
        self.repositories = repositories
        self.bot = bot
        self.llm_client = llm_client

    async def build(self, date: str, source_id: int | None = None) -> str:
        rows = await self.repositories.telegram_monitoring.digest_rows(date, source_id)
        sources = {row["source_id"] for row in rows}
        sentiments = Counter((row.get("sentiment") or "unprocessed") for row in rows)
        topics = Counter((row.get("topic") or "unprocessed") for row in rows)
        severities = Counter(int(row.get("severity") or 0) for row in rows)
        lines = [
            "📊 <b>Argus Daily Digest</b>",
            escape(date),
            "",
            f"Источников: <b>{len(sources)}</b>",
            f"Сообщений: <b>{len(rows)}</b>",
            "",
            "<b>Настроение:</b>",
            f"🟢 positive: {sentiments['positive']}",
            f"⚪ neutral: {sentiments['neutral']}",
            f"🔴 negative: {sentiments['negative']}",
            "",
            "<b>Требуют внимания:</b>",
            f"🟡 severity 1: {severities[1]}",
            f"🟠 severity 2: {severities[2]}",
            f"🔴 severity 3: {severities[3]}",
        ]
        if topics:
            lines.extend(["", "<b>Основные темы:</b>"])
            lines.extend(
                f"{index}. {escape(topic)} — {count}"
                for index, (topic, count) in enumerate(topics.most_common(5), 1)
                if topic != "unprocessed"
            )
        summary = await self._llm_summary(rows)
        if summary:
            lines.extend(["", "<b>Основные наблюдения:</b>", escape(summary[:2000])])
        important = [row for row in rows if int(row.get("severity") or 0) >= 2][:3]
        if important:
            lines.extend(["", "<b>Важные сообщения:</b>"])
            for index, row in enumerate(important, 1):
                lines.append(f"{index}. {escape((row.get('text') or '')[:300])}")
        return "\n".join(lines)

    async def send(self, date: str) -> bool:
        text = await self.build(date)
        targets = [self.settings.alert_chat_id] if self.settings.alert_chat_id else list(
            self.settings.admin_ids
        )
        delivered = False
        for chat_id in targets:
            try:
                await self.bot.send_message(chat_id, text, disable_web_page_preview=True)
                delivered = True
            except Exception as exc:
                logger.warning("Failed to send daily digest chat_id=%s: %s", chat_id, exc)
        if delivered:
            logger.info("Telegram daily digest created date=%s", date)
        return delivered

    async def _llm_summary(self, rows: list[dict]) -> str | None:
        if self.llm_client is None or not rows:
            return None
        aggregates = aggregate_messages(rows)
        try:
            return (await self.llm_client.generate(
                system="Return a concise Russian monitoring summary. Do not invent facts.",
                prompt=digest_prompt(aggregates) + "\n/no_think",
            )).strip()
        except (OllamaUnavailable, ValueError) as exc:
            logger.warning("Daily digest LLM summary unavailable: %s", exc)
            return None


class DailyDigestScheduler:
    def __init__(self, *, settings, service, scheduler_state) -> None:
        self.settings = settings
        self.service = service
        self.scheduler_state = scheduler_state
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Daily digest scheduler failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> bool:
        if not self.settings.daily_digest_enabled:
            return False
        now = datetime.now(ZoneInfo(self.settings.timezone))
        hour, minute = self._parse_time(self.settings.daily_digest_time)
        if (now.hour, now.minute) < (hour, minute):
            return False
        digest_date = (now.date() - timedelta(days=1)).isoformat()
        state_key = "telegram:daily_digest:last_date"
        if await self.scheduler_state.get(state_key) == digest_date:
            return False
        if await self.service.send(digest_date):
            await self.scheduler_state.set(state_key, digest_date)
            return True
        return False

    @staticmethod
    def _parse_time(value: str) -> tuple[int, int]:
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("DAILY_DIGEST_TIME must use HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("DAILY_DIGEST_TIME must use HH:MM")
        return hour, minute
