import asyncio
import logging

from app.alerts.service import AlertService
from app.config import Settings
from app.vk.client import VKAPIError
from app.vk.service import VKService

logger = logging.getLogger(__name__)


class VKPollingScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        service: VKService,
        alerts: AlertService,
    ) -> None:
        self.settings = settings
        self.service = service
        self.alerts = alerts
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except VKAPIError as exc:
                logger.info("VK scheduler is not ready: %s", exc)
            except Exception:
                logger.exception("Unexpected VK scheduler error")

            delay = await self._next_delay_seconds()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=delay,
                )
            except TimeoutError:
                continue

    async def run_once(self) -> None:
        config = await self.service.effective_config()
        try:
            if config.monitor_mode == "longpoll":
                result = await self.service.longpoll_once()
            else:
                result = await self.service.sync_recent()
        except Exception:
            if not self.settings.vk_enable_polling_fallback or not config.polling_token:
                raise
            logger.info("VK Long Poll unavailable; falling back to polling sync")
            result = await self.service.sync_recent()

        for post in result.new_posts:
            await self.alerts.send_vk_post_alert(post)
        for comment in result.new_comments:
            await self.alerts.send_vk_comment_alert(comment)

    async def _next_delay_seconds(self) -> int:
        try:
            config = await self.service.effective_config()
        except Exception:
            return self.settings.vk_posts_poll_interval_seconds
        if config.enabled and config.monitor_mode == "longpoll" and config.group_token:
            return 1
        return self.settings.vk_posts_poll_interval_seconds
