import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.alerts.service import AlertService
from app.config import Settings
from app.vk.client import VKAPIError
from app.vk.service import VKEffectiveConfig, VKService, VKSyncResult

logger = logging.getLogger(__name__)
UTC = timezone.utc


class VKPollingScheduler:
    LONGPOLL_WAIT_SECONDS = 25
    LONGPOLL_HARD_TIMEOUT_SECONDS = 45

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
        self._next_polling_sync_at: datetime | None = None
        self._logged_polling_unavailable = False
        self._last_config_summary: str | None = None

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
        self._log_config_if_changed(config)
        if config.monitor_mode == "longpoll":
            result = await self._run_longpoll_or_fallback(config)
            await self._send_alerts(result)
            await self._run_polling_reconciliation_if_due(config)
            return

        result = await self.service.sync_recent()
        self._log_result("VK polling sync", result)
        await self._send_alerts(result)

    def _log_config_if_changed(self, config: VKEffectiveConfig) -> None:
        summary = (
            f"enabled={config.enabled} mode={config.monitor_mode} "
            f"group_id={config.group_id or 'missing'} "
            f"group_token={'set' if config.group_token else 'missing'} "
            f"user_token={'set' if config.user_access_token else 'missing'}"
        )
        if summary == self._last_config_summary:
            return
        self._last_config_summary = summary
        logger.info("VK effective config: %s", summary)

    async def _run_longpoll_or_fallback(self, config: VKEffectiveConfig) -> VKSyncResult:
        try:
            logger.info(
                "VK Long Poll request started: group_id=%s wait=%ss",
                config.group_id,
                self.LONGPOLL_WAIT_SECONDS,
            )
            result = await asyncio.wait_for(
                self.service.longpoll_once(wait_seconds=self.LONGPOLL_WAIT_SECONDS),
                timeout=self.LONGPOLL_HARD_TIMEOUT_SECONDS,
            )
            self._log_result("VK Long Poll sync", result)
            return result
        except TimeoutError:
            self.service.reset_longpoll_state()
            logger.warning(
                "VK Long Poll timed out after %ss; state reset and next cycle will reconnect",
                self.LONGPOLL_HARD_TIMEOUT_SECONDS,
            )
            return self._empty_result(config)
        except Exception:
            if not self.settings.vk_enable_polling_fallback or not config.polling_token:
                raise
            logger.info("VK Long Poll unavailable; falling back to polling sync")
            result = await self.service.sync_recent()
            self._log_result("VK fallback polling sync", result)
            return result

    async def _run_polling_reconciliation_if_due(self, config: VKEffectiveConfig) -> None:
        if not await self._polling_reconciliation_due(config):
            return
        try:
            result = await self.service.sync_recent()
        except Exception:
            logger.exception("VK polling reconciliation failed")
            return
        self._log_result("VK polling reconciliation", result)
        await self._send_alerts(result)

    async def _polling_reconciliation_due(self, config: VKEffectiveConfig) -> bool:
        if not config.polling_token:
            if not self._logged_polling_unavailable:
                logger.info(
                    "VK polling reconciliation skipped: VK_USER_ACCESS_TOKEN is missing"
                )
                self._logged_polling_unavailable = True
            return False

        self._logged_polling_unavailable = False
        now = datetime.now(UTC)
        if self._next_polling_sync_at is not None and now < self._next_polling_sync_at:
            return False

        interval = max(60, self.settings.vk_comments_poll_interval_seconds)
        self._next_polling_sync_at = now + timedelta(seconds=interval)
        return True

    async def _send_alerts(self, result: VKSyncResult) -> None:
        for post in result.new_posts:
            await self.alerts.send_vk_post_alert(post)
        for comment in result.new_comments:
            await self.alerts.send_vk_comment_alert(comment)

    def _log_result(self, label: str, result: VKSyncResult) -> None:
        logger.info(
            "%s: group_id=%s posts_processed=%s comments_processed=%s new_posts=%s new_comments=%s",
            label,
            result.group_id,
            result.posts_processed,
            result.comments_processed,
            len(result.new_posts),
            len(result.new_comments),
        )

    def _empty_result(self, config: VKEffectiveConfig) -> VKSyncResult:
        return VKSyncResult(
            group_id=config.group_id or 0,
            posts_processed=0,
            comments_processed=0,
            new_posts=[],
            new_comments=[],
        )

    async def _next_delay_seconds(self) -> int:
        try:
            config = await self.service.effective_config()
        except Exception:
            return self.settings.vk_posts_poll_interval_seconds
        if config.enabled and config.monitor_mode == "longpoll" and config.group_token:
            return 1
        return self.settings.vk_posts_poll_interval_seconds
