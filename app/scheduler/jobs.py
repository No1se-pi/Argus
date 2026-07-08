import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.alerts.service import AlertService
from app.collectors.telegram import LargeFloodWait, TelegramCollector
from app.config import Settings
from app.storage.models import Source
from app.storage.repositories import (
    RuntimeSettingsRepository,
    SchedulerStateRepository,
    SourceRepository,
    TelegramKeywordRepository,
)

logger = logging.getLogger(__name__)
UTC = timezone.utc


class BackgroundScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        sources: SourceRepository,
        scheduler_state: SchedulerStateRepository,
        collector: TelegramCollector,
        alerts: AlertService,
        runtime_settings: RuntimeSettingsRepository | None = None,
        keywords: TelegramKeywordRepository | None = None,
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.scheduler_state = scheduler_state
        self.collector = collector
        self.alerts = alerts
        self.runtime_settings = runtime_settings
        self.keywords = keywords
        self._stop_event = asyncio.Event()
        self._logged_disabled = False
        self._logged_no_sources = False

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected scheduler error")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.poll_interval_seconds,
                )
            except TimeoutError:
                continue

    async def run_once(self) -> None:
        if not await self._telegram_monitor_enabled():
            if not self._logged_disabled:
                logger.info("Telegram scheduler skipped: monitor is disabled")
                self._logged_disabled = True
            return

        self._logged_disabled = False
        active_sources = await self.sources.list_sources()
        if not active_sources:
            if not self._logged_no_sources:
                logger.info("Telegram scheduler has no active sources")
                self._logged_no_sources = True
            return

        self._logged_no_sources = False
        for source in active_sources:
            try:
                if source.telegram_monitor_mode == "discussion":
                    await self._sync_discussion_source(source)
                else:
                    await self._sync_post_source(source)
                    await self._sync_reactions_if_due(source)
                await self.sources.clear_error(source.id)
            except LargeFloodWait as exc:
                await self._defer_after_flood(source, exc)
            except Exception as exc:
                await self.sources.set_error(source.id, str(exc))
                logger.exception("Failed to synchronize source %s", source.id)

            await asyncio.sleep(self.settings.source_sync_pause_seconds)

    async def _sync_post_source(self, source: Source) -> None:
        if await self._is_deferred(self._defer_key(source.id, "posts")):
            return
        result = await self.collector.sync_posts(source)
        logger.info(
            "Telegram source %s (%s, mode=%s) posts sync: initialized=%s fetched=%s saved=%s new=%s last_message_id=%s",
            source.id,
            source.display_name,
            source.telegram_monitor_mode,
            result.initialized,
            result.fetched_count,
            result.saved_count,
            len(result.new_posts),
            result.last_message_id,
        )
        fresh_source = await self.sources.get_source(source.id) or source
        for post in result.new_posts:
            await self.alerts.send_new_post_alert(fresh_source, post)
            if self.keywords is None:
                continue
            matched_keywords = await self.keywords.matching_keywords(post.text)
            if matched_keywords:
                await self.alerts.send_keyword_post_alert(fresh_source, post, matched_keywords)

    async def _sync_discussion_source(self, source: Source) -> None:
        if await self._is_deferred(self._defer_key(source.id, "discussion")):
            return
        result = await self.collector.sync_discussion(source)
        logger.info(
            "Telegram source %s (%s, mode=%s) discussion sync: initialized=%s fetched=%s saved=%s new=%s last_message_id=%s",
            source.id,
            source.display_name,
            source.telegram_monitor_mode,
            result.initialized,
            result.fetched_count,
            result.saved_count,
            len(result.new_messages),
            result.last_message_id,
        )
        if result.initialized or not result.new_messages:
            return
        fresh_source = await self.sources.get_source(source.id) or source
        alert_limit = self.settings.tg_comment_alerts_per_cycle
        for message in result.new_messages[:alert_limit]:
            await self.alerts.send_telegram_comment_alert(fresh_source, message)
        if len(result.new_messages) > alert_limit:
            await self.alerts.send_telegram_comment_summary(
                fresh_source,
                total_count=len(result.new_messages),
                sent_count=alert_limit,
            )

    async def _sync_reactions_if_due(self, source: Source) -> None:
        due_key = self._reactions_due_key(source.id)
        if await self._is_deferred(self._defer_key(source.id, "reactions")):
            return
        if await self._is_deferred(due_key):
            return
        now = datetime.now(UTC)
        start = (now - timedelta(days=30)).isoformat()
        await self.collector.sync_reactions(source, start, now.isoformat())
        next_at = now + timedelta(seconds=self.settings.tg_reactions_sync_interval_seconds)
        await self.scheduler_state.set(due_key, next_at.isoformat())

    async def _is_deferred(self, key: str) -> bool:
        raw_value = await self.scheduler_state.get(key)
        if not raw_value:
            return False
        try:
            next_at = datetime.fromisoformat(raw_value)
        except ValueError:
            return False
        return next_at > datetime.now(UTC)

    async def _defer_after_flood(self, source: Source, exc: LargeFloodWait) -> None:
        operation = self._operation_from_flood(exc)
        next_at = datetime.now(UTC) + timedelta(seconds=exc.seconds)
        await self.scheduler_state.set(self._defer_key(source.id, operation), next_at.isoformat())
        await self.sources.set_error(
            source.id,
            f"FloodWait {exc.seconds}s during {exc.operation}",
        )
        logger.warning(
            "Deferred source %s operation %s for %s seconds after FloodWait",
            source.id,
            operation,
            exc.seconds,
        )

    def _defer_key(self, source_id: int, operation: str) -> str:
        return f"telegram:source:{source_id}:next_{operation}_sync_after"

    def _reactions_due_key(self, source_id: int) -> str:
        return f"telegram:source:{source_id}:next_reactions_due_after"

    def _operation_from_flood(self, exc: LargeFloodWait) -> str:
        operation = exc.operation.lower()
        if "reaction" in operation:
            return "reactions"
        if "discussion" in operation or "comment" in operation:
            return "discussion"
        return "posts"

    async def _telegram_monitor_enabled(self) -> bool:
        if self.runtime_settings is None:
            return self.settings.enable_telegram_monitor
        raw_value = await self.runtime_settings.get("enable_telegram_monitor")
        if raw_value is None:
            return self.settings.enable_telegram_monitor
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
