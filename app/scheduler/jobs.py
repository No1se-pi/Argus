import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.alerts.service import AlertService
from app.collectors.telegram import LargeFloodWait, TelegramCollector
from app.config import Settings
from app.storage.models import Source
from app.storage.repositories import RuntimeSettingsRepository, SchedulerStateRepository, SourceRepository

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.scheduler_state = scheduler_state
        self.collector = collector
        self.alerts = alerts
        self.runtime_settings = runtime_settings
        self._stop_event = asyncio.Event()

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
            return

        active_sources = await self.sources.list_sources()
        for source in active_sources:
            if await self._is_deferred(source):
                continue

            try:
                result = await self.collector.sync_posts(source)
                fresh_source = await self.sources.get_source(source.id) or source
                for post in result.new_posts:
                    await self.alerts.send_new_post_alert(fresh_source, post)
                await self.sources.clear_error(source.id)
            except LargeFloodWait as exc:
                next_at = datetime.now(UTC) + timedelta(seconds=exc.seconds)
                await self.scheduler_state.set(self._defer_key(source.id), next_at.isoformat())
                await self.sources.set_error(
                    source.id,
                    f"FloodWait {exc.seconds}s during {exc.operation}",
                )
                logger.warning(
                    "Deferred source %s for %s seconds after FloodWait",
                    source.id,
                    exc.seconds,
                )
            except Exception as exc:
                await self.sources.set_error(source.id, str(exc))
                logger.exception("Failed to synchronize source %s", source.id)

            await asyncio.sleep(self.settings.source_sync_pause_seconds)

    async def _is_deferred(self, source: Source) -> bool:
        raw_value = await self.scheduler_state.get(self._defer_key(source.id))
        if not raw_value:
            return False
        try:
            next_at = datetime.fromisoformat(raw_value)
        except ValueError:
            return False
        return next_at > datetime.now(UTC)

    def _defer_key(self, source_id: int) -> str:
        return f"telegram:source:{source_id}:next_posts_sync_after"

    async def _telegram_monitor_enabled(self) -> bool:
        if self.runtime_settings is None:
            return self.settings.enable_telegram_monitor
        raw_value = await self.runtime_settings.get("enable_telegram_monitor")
        if raw_value is None:
            return self.settings.enable_telegram_monitor
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
