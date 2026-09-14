import asyncio
import logging
from datetime import datetime

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone
    UTC = timezone.utc  # noqa: UP017

from app.alerts.service import AlertService
from app.config import Settings
from app.reviews.client_2gis import DGisReviewsClient
from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.models import (
    DEFAULT_SOURCES,
    ReviewItem,
    ReviewPlatform,
    ReviewsEffectiveConfig,
    ReviewSource,
    ReviewsSyncAlreadyRunningError,
    ReviewSyncResult,
    ReviewSyncStatus,
)
from app.storage.repositories import ReviewRepository, RuntimeSettingsRepository

logger = logging.getLogger(__name__)


class ReviewsService:
    def __init__(
        self,
        *,
        settings: Settings,
        runtime_settings: RuntimeSettingsRepository,
        repository: ReviewRepository,
        alerts: AlertService,
        yandex_client: YandexReviewsClient | None = None,
        dgis_client: DGisReviewsClient | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_settings = runtime_settings
        self.repository = repository
        self.alerts = alerts
        self._yandex_client = yandex_client
        self._dgis_client = dgis_client
        self._sync_lock = asyncio.Lock()
        self._default_sources_seeded = False

    def _get_yandex_client(self) -> YandexReviewsClient:
        if self._yandex_client is None:
            self._yandex_client = YandexReviewsClient()
        return self._yandex_client

    def _get_dgis_client(self) -> DGisReviewsClient:
        if self._dgis_client is None:
            self._dgis_client = DGisReviewsClient()
        return self._dgis_client

    async def close(self) -> None:
        if self._yandex_client is not None:
            await self._yandex_client.close()
        if self._dgis_client is not None:
            await self._dgis_client.close()

    async def effective_config(self) -> ReviewsEffectiveConfig:
        runtime_enabled = await self.runtime_settings.get("enable_reviews_monitor")
        enabled = self.settings.enable_reviews_monitor
        if runtime_enabled is not None:
            enabled = runtime_enabled.strip().lower() in {"1", "true", "yes", "on"}

        poll_interval = await self.runtime_settings.get_int(
            "reviews_poll_interval_seconds",
            self.settings.reviews_poll_interval_seconds,
        )
        request_pause = self.settings.reviews_request_pause_seconds
        page_size = self.settings.reviews_fetch_page_size
        max_catchup = self.settings.reviews_max_catchup_reviews
        error_threshold = self.settings.reviews_error_alert_threshold
        max_backoff = self.settings.reviews_max_backoff_seconds

        alerts_enabled = await self.runtime_settings.get_bool(
            "alerts_reviews_enabled",
            self.settings.alerts_reviews_enabled,
        )

        return ReviewsEffectiveConfig(
            enabled=enabled,
            poll_interval_seconds=poll_interval,
            request_pause_seconds=request_pause,
            fetch_page_size=page_size,
            max_catchup_reviews=max_catchup,
            error_alert_threshold=error_threshold,
            max_backoff_seconds=max_backoff,
            alerts_enabled=alerts_enabled,
        )

    async def ensure_default_sources(self) -> int:
        if not self._default_sources_seeded:
            count = await self.repository.ensure_default_sources(DEFAULT_SOURCES)
            self._default_sources_seeded = True
            logger.info("Ensured %d review sources registered in database", count)
            return count
        return 0

    async def _handle_sync_failure(
        self,
        source: ReviewSource,
        status: ReviewSyncStatus,
        err: str | None,
        http_code: int | None,
        now_dt: datetime,
        now_iso: str,
        t_start: float,
        config: ReviewsEffectiveConfig,
    ) -> ReviewSyncResult:
        latency_ms = int((asyncio.get_event_loop().time() - t_start) * 1000)
        consecutive = source.consecutive_errors + 1
        backoff_sec = min(300 * (2 ** (consecutive - 1)), config.max_backoff_seconds)
        backoff_iso = datetime.fromtimestamp(
            now_dt.timestamp() + backoff_sec, tz=UTC
        ).isoformat()

        health_alert_active = source.health_alert_active
        health_alert_sent_at = source.health_alert_sent_at

        # Problem alert logic: threshold reached for the first time
        if consecutive >= config.error_alert_threshold and source.health_alert_active == 0:
            logger.warning(
                "Source %s (%s) reached error threshold (%d consecutive errors). "
                "Dispatching health alert.",
                source.branch_name,
                source.platform,
                consecutive,
            )
            try:
                sent = await self.alerts.send_review_health_alert(
                    source=source,
                    error_message=err or f"Status: {status}",
                )
                if sent:
                    health_alert_active = 1
                    health_alert_sent_at = now_iso
                else:
                    logger.warning(
                        "Review health alert delivery failed, "
                        "preserving state to retry next cycle"
                    )
            except Exception as alert_exc:
                logger.error("Failed to send review health alert: %s", alert_exc)

        await self.repository.update_source_status(
            source.id,
            status=status.value if hasattr(status, "value") else str(status),
            checked_at=now_iso,
            success=False,
            error=err,
            consecutive_errors=consecutive,
            health_alert_active=health_alert_active,
            health_alert_sent_at=health_alert_sent_at,
            backoff_until=backoff_iso,
        )
        return ReviewSyncResult(
            source=source,
            status=status,
            fetched_reviews=[],
            new_reviews=[],
            error_message=err,
            http_status=http_code,
            latency_ms=latency_ms,
        )

    async def sync_source(self, source: ReviewSource) -> ReviewSyncResult:
        now_dt = datetime.now(UTC)
        now_iso = now_dt.isoformat()
        config = await self.effective_config()

        # Check backoff
        if source.backoff_until:
            try:
                backoff_dt = datetime.fromisoformat(source.backoff_until)
                if now_dt < backoff_dt:
                    logger.info(
                        "Skipping %s (%s): backoff until %s",
                        source.branch_name,
                        source.platform,
                        source.backoff_until,
                    )
                    return ReviewSyncResult(
                        source=source,
                        status=ReviewSyncStatus.RATE_LIMITED,
                        error_message=f"Backoff active until {source.backoff_until}",
                    )
            except Exception:
                pass

        t_start = asyncio.get_event_loop().time()

        # Call platform client
        if source.platform == ReviewPlatform.YANDEX:
            client = self._get_yandex_client()
            status, items, err, http_code = await client.fetch_reviews(
                source, page=1, page_size=config.fetch_page_size
            )
            branch_rating = None
            total_count = None
        elif source.platform == ReviewPlatform.DGIS:
            d_client = self._get_dgis_client()
            (
                status,
                items,
                err,
                http_code,
                branch_rating,
                total_count,
            ) = await d_client.fetch_reviews(source, page=1, page_size=config.fetch_page_size)
        else:
            return ReviewSyncResult(
                source=source,
                status=ReviewSyncStatus.UNKNOWN_ERROR,
                error_message=f"Unsupported platform: {source.platform}",
            )

        # Handle failure
        if status not in (
            ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
            ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
        ):
            return await self._handle_sync_failure(
                source, status, err, http_code, now_dt, now_iso, t_start, config
            )

        # Handle successful fetch: send recovery alert if coming from degraded state
        health_alert_active = source.health_alert_active
        if source.health_alert_active == 1:
            logger.info(
                "Source %s (%s) recovered after degraded state. Dispatching recovery alert.",
                source.branch_name,
                source.platform,
            )
            try:
                sent = await self.alerts.send_review_recovery_alert(source=source)
                if sent:
                    health_alert_active = 0
                else:
                    logger.warning(
                        "Review recovery alert delivery failed, "
                        "preserving active state to retry next cycle"
                    )
            except Exception as alert_exc:
                logger.error("Failed to send review recovery alert: %s", alert_exc)

        # Handle Cold Start Baseline
        if not source.is_initialized:
            baseline_items = list(items)
            current_page = 2
            if (
                len(items) == config.fetch_page_size
                and config.max_catchup_reviews > config.fetch_page_size
            ):
                while len(baseline_items) < config.max_catchup_reviews:
                    if config.request_pause_seconds > 0:
                        await asyncio.sleep(config.request_pause_seconds)

                    if source.platform == ReviewPlatform.YANDEX:
                        b_status, b_items, b_err, b_http_code = (
                            await self._get_yandex_client().fetch_reviews(
                                source, page=current_page, page_size=config.fetch_page_size
                            )
                        )
                    else:
                        (
                            b_status,
                            b_items,
                            b_err,
                            b_http_code,
                            b_rating,
                            b_count,
                        ) = await self._get_dgis_client().fetch_reviews(
                            source, page=current_page, page_size=config.fetch_page_size
                        )
                        if b_rating is not None:
                            branch_rating = b_rating
                        if b_count is not None:
                            total_count = b_count

                    if b_status not in (
                        ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
                        ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
                    ):
                        logger.warning(
                            "Baseline initialization failed for %s (%s) on page %d: %s (%s). "
                            "Aborting baseline initialization (source will remain uninitialized).",
                            source.branch_name,
                            source.platform,
                            current_page,
                            b_status,
                            b_err,
                        )
                        return await self._handle_sync_failure(
                            source, b_status, b_err, b_http_code, now_dt, now_iso, t_start, config
                        )

                    if not b_items:
                        break

                    baseline_items.extend(b_items)

                    if len(b_items) < config.fetch_page_size:
                        break

                    current_page += 1

            if len(baseline_items) > config.max_catchup_reviews:
                baseline_items = baseline_items[: config.max_catchup_reviews]

            logger.info(
                "Initializing baseline for %s (%s) with %d historical reviews (0 alerts)",
                source.branch_name,
                source.platform,
                len(baseline_items),
            )
            await self.repository.save_reviews(baseline_items, source.id, mark_sent=True)
            await self.repository.mark_source_initialized(source.id)
            await self.repository.update_source_status(
                source.id,
                status="SUCCESS",
                checked_at=now_iso,
                success=True,
                consecutive_errors=0,
                health_alert_active=health_alert_active,
                health_alert_sent_at=None,
                backoff_until=None,
                last_rating=branch_rating,
                total_reviews_count=total_count,
            )
            return ReviewSyncResult(
                source=source,
                status=ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
                fetched_reviews=baseline_items,
                new_reviews=[],
                http_status=http_code,
                latency_ms=int((asyncio.get_event_loop().time() - t_start) * 1000),
            )

        # Handle Deduplication & New Reviews
        new_items: list[ReviewItem] = []
        for it in items:
            is_known = await self.repository.is_review_known(it.platform, it.external_review_id)
            if not is_known:
                new_items.append(it)

        # Catch-up during downtime
        # If page 1 had >=1 new review AND page 1 was full,
        # check subsequent pages up to max_catchup_reviews
        if (
            len(new_items) > 0
            and len(items) == config.fetch_page_size
            and config.max_catchup_reviews > config.fetch_page_size
        ):
            current_page = 2
            total_fetched = len(items)
            while total_fetched < config.max_catchup_reviews:
                if config.request_pause_seconds > 0:
                    await asyncio.sleep(config.request_pause_seconds)

                if source.platform == ReviewPlatform.YANDEX:
                    c_status, c_items, c_err, c_http_code = (
                        await self._get_yandex_client().fetch_reviews(
                            source, page=current_page, page_size=config.fetch_page_size
                        )
                    )
                else:
                    (
                        c_status,
                        c_items,
                        c_err,
                        c_http_code,
                        c_rating,
                        c_count,
                    ) = await self._get_dgis_client().fetch_reviews(
                        source, page=current_page, page_size=config.fetch_page_size
                    )
                    if c_rating is not None:
                        branch_rating = c_rating
                    if c_count is not None:
                        total_count = c_count

                if c_status not in (
                    ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
                    ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
                ):
                    logger.warning(
                        "Catch-up failed on page %d for %s (%s): %s (%s). "
                        "Aborting catch-up cycle without persisting new reviews "
                        "to prevent losing unseen reviews.",
                        current_page,
                        source.branch_name,
                        source.platform,
                        c_status,
                        c_err,
                    )
                    return await self._handle_sync_failure(
                        source, c_status, c_err, c_http_code, now_dt, now_iso, t_start, config
                    )

                if not c_items:
                    break

                page_new_count = 0
                for it in c_items:
                    is_known = await self.repository.is_review_known(
                        it.platform, it.external_review_id
                    )
                    if not is_known:
                        new_items.append(it)
                        page_new_count += 1

                total_fetched += len(c_items)

                # Stop if this page had zero new reviews (all known)
                if page_new_count == 0:
                    break

                # Stop if end of stream reached (less than full page returned)
                if len(c_items) < config.fetch_page_size:
                    break

                current_page += 1

        # Sort new reviews chronologically: OLDEST to NEWEST
        new_items.sort(key=lambda r: r.published_at)

        if new_items:
            await self.repository.save_reviews(new_items, source.id, mark_sent=False)
            status = ReviewSyncStatus.SUCCESS_NEW_REVIEWS
            logger.info(
                "Discovered %d new reviews for %s (%s)",
                len(new_items),
                source.branch_name,
                source.platform,
            )
        else:
            status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS

        await self.repository.update_source_status(
            source.id,
            status=status.value if hasattr(status, "value") else str(status),
            checked_at=now_iso,
            success=True,
            consecutive_errors=0,
            health_alert_active=health_alert_active,
            health_alert_sent_at=None,
            backoff_until=None,
            last_rating=branch_rating,
            total_reviews_count=total_count,
        )

        return ReviewSyncResult(
            source=source,
            status=status,
            fetched_reviews=items,
            new_reviews=new_items,
            http_status=http_code,
            latency_ms=int((asyncio.get_event_loop().time() - t_start) * 1000),
        )

    async def deliver_pending_reviews(self) -> int:
        config = await self.effective_config()
        if not config.alerts_enabled:
            return 0

        unsent = await self.repository.get_unsent_reviews(limit=50)
        sent_count = 0
        for review in unsent:
            source = (
                await self.repository.get_source_by_id(review.source_id)
                if getattr(review, "source_id", None)
                else None
            )
            if source is None:
                # Fallback: get source by platform and branch
                sources = await self.repository.list_sources()
                for s in sources:
                    if s.branch_name == review.branch_name and s.platform == review.platform:
                        source = s
                        break

            if source is None:
                logger.warning(
                    "Could not find source for review %s, skipping delivery",
                    review.external_review_id,
                )
                continue

            try:
                ok = await self.alerts.send_review_alert(
                    review=review,
                    source=source,
                    repo=self.repository,
                )
                if ok:
                    sent_count += 1
            except Exception as exc:
                logger.error(
                    "Failed to deliver review %s to Telegram: %s", review.external_review_id, exc
                )

        return sent_count

    async def sync_all_sources(self, force: bool = False) -> list[ReviewSyncResult]:
        if self._sync_lock.locked():
            raise ReviewsSyncAlreadyRunningError("Проверка отзывов уже выполняется.")

        async with self._sync_lock:
            config = await self.effective_config()
            if not config.enabled and not force:
                return []

            # Deliver any previously accumulated unsent reviews at the start of enabled cycle
            if config.alerts_enabled:
                await self.deliver_pending_reviews()

            await self.ensure_default_sources()
            sources = await self.repository.list_sources(only_active=True)
            results: list[ReviewSyncResult] = []

            for i, source in enumerate(sources):
                res = await self.sync_source(source)
                results.append(res)

                # Deliver any newly discovered reviews immediately
                if res.new_reviews and config.alerts_enabled:
                    await self.deliver_pending_reviews()

                # Pause between external requests to avoid bursts
                if i < len(sources) - 1 and config.request_pause_seconds > 0:
                    await asyncio.sleep(config.request_pause_seconds)

            return results
