import asyncio
import logging
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone
    UTC = timezone.utc  # noqa: UP017

from app.config import Settings
from app.reviews.models import ReviewsSyncAlreadyRunningError
from app.reviews.service import ReviewsService

logger = logging.getLogger(__name__)


class ReviewsPollingScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        service: ReviewsService,
    ) -> None:
        self.settings = settings
        self.service = service
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._last_completed_cycle_at: datetime | None = None
        self._started_at: datetime = datetime.now(UTC)
        self._consecutive_loop_crashes: int = 0
        self._scheduler_health_alert_active: bool = False
        self._state: str = "IDLE_DISABLED"
        self._current_task: asyncio.Task | None = None

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        self._state = "STOPPED"

    def wake(self) -> None:
        self._wake_event.set()

    @property
    def last_completed_cycle_at(self) -> datetime | None:
        return self._last_completed_cycle_at

    @property
    def consecutive_crashes(self) -> int:
        return self._consecutive_loop_crashes

    @property
    def scheduler_health_alert_active(self) -> bool:
        return self._scheduler_health_alert_active

    async def get_status(self) -> dict[str, Any]:
        config = await self.service.effective_config()
        health = "HEALTHY"
        now = datetime.now(UTC)

        if not config.enabled:
            health = "IDLE_DISABLED"
        elif self._scheduler_health_alert_active or self._consecutive_loop_crashes >= 3:
            health = "UNHEALTHY_CRASHING"
        elif self._last_completed_cycle_at is not None:
            elapsed = (now - self._last_completed_cycle_at).total_seconds()
            if elapsed > 2 * config.poll_interval_seconds:
                health = "DEGRADED_STALE"
        else:
            uptime = (now - self._started_at).total_seconds()
            if uptime > 2 * config.poll_interval_seconds:
                health = "DEGRADED_STALE"

        return {
            "state": self._state,
            "is_running": not self._stop_event.is_set(),
            "effective_enabled": config.enabled,
            "poll_interval_seconds": config.poll_interval_seconds,
            "last_completed_cycle_at": self._last_completed_cycle_at.isoformat()
            if self._last_completed_cycle_at
            else None,
            "consecutive_loop_crashes": self._consecutive_loop_crashes,
            "health_status": health,
        }

    async def _poll_cycle(self, wait_after: bool = True) -> None:
        try:
            config = await self.service.effective_config()

            if not config.enabled:
                self._state = "IDLE_DISABLED"
                self._wake_event.clear()
                if wait_after:
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=5.0)
                    except TimeoutError:
                        pass
                return

            # Module is enabled: run full cycle
            self._state = "RUNNING"
            logger.info("ReviewsPollingScheduler starting full sync cycle")
            try:
                await self.service.sync_all_sources()
            except ReviewsSyncAlreadyRunningError:
                logger.info("Reviews sync already in progress, skipping scheduled cycle")
                self._wake_event.clear()
                if wait_after:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=float(config.poll_interval_seconds),
                        )
                    except TimeoutError:
                        pass
                return

            self._last_completed_cycle_at = datetime.now(UTC)
            self._consecutive_loop_crashes = 0
            logger.info(
                "ReviewsPollingScheduler completed full cycle successfully at %s",
                self._last_completed_cycle_at.isoformat(),
            )

            # If scheduler health alert was active, dispatch recovery alert
            if self._scheduler_health_alert_active:
                try:
                    sent = await self.service.alerts.send_reviews_scheduler_recovery_alert()
                    if sent:
                        self._scheduler_health_alert_active = False
                except Exception as rec_exc:
                    logger.error("Failed to send scheduler recovery alert: %s", rec_exc)

            # Sleep until next interval or wake/stop event
            self._wake_event.clear()
            if wait_after:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=float(config.poll_interval_seconds),
                    )
                except TimeoutError:
                    pass

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._consecutive_loop_crashes += 1
            self._state = "CRASHING"
            backoff = min(30 * (2 ** (self._consecutive_loop_crashes - 1)), 300)
            logger.critical(
                "Unexpected exception in Reviews polling scheduler loop: %s (crash count: %d). "
                "Backing off for %ds...",
                exc,
                self._consecutive_loop_crashes,
                backoff,
                exc_info=True,
            )

            # Threshold: 3 consecutive loop crashes -> dispatch scheduler health alert
            if self._consecutive_loop_crashes >= 3 and not self._scheduler_health_alert_active:
                try:
                    sent = await self.service.alerts.send_reviews_scheduler_health_alert(
                        crash_count=self._consecutive_loop_crashes,
                        error_message=str(exc),
                    )
                    if sent:
                        self._scheduler_health_alert_active = True
                except Exception as alert_exc:
                    logger.error("Failed to send scheduler health alert: %s", alert_exc)

            if wait_after:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=float(backoff))
                except TimeoutError:
                    pass

    async def run(self) -> None:
        self._started_at = datetime.now(UTC)
        logger.info("ReviewsPollingScheduler started")

        while not self._stop_event.is_set():
            try:
                await self._poll_cycle(wait_after=True)
            except asyncio.CancelledError:
                logger.info("ReviewsPollingScheduler task cancelled")
                break

        self._state = "STOPPED"
        logger.info("ReviewsPollingScheduler stopped")
