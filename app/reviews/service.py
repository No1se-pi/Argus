import asyncio
import logging
import time
from datetime import datetime

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from app.alerts.service import AlertService
from app.config import Settings
from app.reviews.analyzer import GeminiReviewWorker
from app.reviews.client_2gis import DGisReviewsClient
from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.dates import parse_utc_datetime
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
        ai_worker: GeminiReviewWorker | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_settings = runtime_settings
        self.repository = repository
        self.alerts = alerts
        self._yandex_client = yandex_client
        self._dgis_client = dgis_client
        self._sync_lock = asyncio.Lock()
        self._default_sources_seeded = False
        self._ai_worker = ai_worker
        self._ai_worker_started = False
        self._last_rating_alerts: dict[int, tuple[float, float, float]] = {}

    def get_ai_worker(self) -> GeminiReviewWorker:
        if self._ai_worker is None:
            self._ai_worker = GeminiReviewWorker(
                settings=self.settings,
                repository=self.repository,
                alerts=self.alerts,
            )
        return self._ai_worker

    async def ensure_ai_worker_started(self) -> None:
        worker = self.get_ai_worker()
        if not self._ai_worker_started or worker._task is None or worker._task.done():
            await worker.start()
            self._ai_worker_started = True

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
        if self._ai_worker is not None:
            await self._ai_worker.stop()
            self._ai_worker_started = False

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
        backoff_iso = datetime.fromtimestamp(now_dt.timestamp() + backoff_sec, tz=UTC).isoformat()

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
                        "Review health alert delivery failed, preserving state to retry next cycle"
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

    async def _notify_recovery(self, source: ReviewSource) -> int:
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

        return health_alert_active

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
            res = await client.fetch_reviews(source, page=1, page_size=config.fetch_page_size)
            status, items, err, http_code = res[:4]
            branch_rating = res[4] if len(res) >= 5 else None
            total_count = res[5] if len(res) >= 6 else None
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

        # Validate rating and review count from platform response
        if branch_rating is not None:
            try:
                b_rating_val = round(float(branch_rating), 1)
                branch_rating = b_rating_val if 1.0 <= b_rating_val <= 5.0 else None
            except (ValueError, TypeError):
                branch_rating = None

        # Guard against transient zeroes for count: never overwrite known positive count with 0
        if total_count is not None:
            try:
                t_count_val = int(total_count)
                if t_count_val < 0:
                    total_count = None
                elif (
                    t_count_val == 0
                    and source.total_reviews_count is not None
                    and source.total_reviews_count > 0
                ):
                    logger.warning(
                        "Source %s (%s) reported 0 total reviews while previously having %d; "
                        "ignoring transient drop.",
                        source.branch_name,
                        source.platform,
                        source.total_reviews_count,
                    )
                    total_count = source.total_reviews_count
                else:
                    total_count = t_count_val
            except (ValueError, TypeError):
                total_count = None

        # Handle failure
        if status not in (
            ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
            ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
        ):
            return await self._handle_sync_failure(
                source, status, err, http_code, now_dt, now_iso, t_start, config
            )

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
                        y_res = await self._get_yandex_client().fetch_reviews(
                            source, page=current_page, page_size=config.fetch_page_size
                        )
                        b_status, b_items, b_err, b_http_code = y_res[:4]
                        if len(y_res) >= 6:
                            if y_res[4] is not None:
                                branch_rating = y_res[4]
                            if y_res[5] is not None:
                                total_count = y_res[5]
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
            baseline_effective_rating = (
                branch_rating
                if (branch_rating is not None and 1.0 <= branch_rating <= 5.0)
                else None
            )
            health_alert_active = await self._notify_recovery(source)
            await self.repository.update_source_status(
                source.id,
                status="SUCCESS",
                checked_at=now_iso,
                success=True,
                consecutive_errors=0,
                health_alert_active=health_alert_active,
                health_alert_sent_at=None,
                backoff_until=None,
                last_rating=baseline_effective_rating,
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
        oldest_ts = (
            await self.repository.get_oldest_review_published_at(source.id)
            if source.is_initialized
            else None
        )
        oldest_known_dt = parse_utc_datetime(oldest_ts) if oldest_ts else None

        new_items: list[ReviewItem] = []
        historical_items: list[ReviewItem] = []
        seen_ids: set[str] = set()
        for it in items:
            rev_id = str(it.external_review_id)
            if rev_id in seen_ids:
                continue
            is_known = await self.repository.is_review_known(it.platform, rev_id)
            if not is_known:
                it_dt = parse_utc_datetime(it.published_at)
                if oldest_known_dt is not None and it_dt is not None and it_dt < oldest_known_dt:
                    logger.info(
                        "Review %s for %s (%s) is older than historical baseline (%s < %s). "
                        "Marking as known historical review without alerting.",
                        rev_id,
                        source.branch_name,
                        source.platform,
                        it.published_at,
                        oldest_ts,
                    )
                    historical_items.append(it)
                    seen_ids.add(rev_id)
                else:
                    new_items.append(it)
                    seen_ids.add(rev_id)

        catchup_incomplete = len(new_items) > 0 and len(items) == config.fetch_page_size

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
            hit_historical_horizon = False
            while total_fetched < config.max_catchup_reviews:
                if config.request_pause_seconds > 0:
                    await asyncio.sleep(config.request_pause_seconds)

                if source.platform == ReviewPlatform.YANDEX:
                    y_res = await self._get_yandex_client().fetch_reviews(
                        source, page=current_page, page_size=config.fetch_page_size
                    )
                    c_status, c_items, c_err, c_http_code = y_res[:4]
                    if len(y_res) >= 6:
                        if y_res[4] is not None:
                            branch_rating = y_res[4]
                        if y_res[5] is not None:
                            total_count = y_res[5]
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
                    catchup_incomplete = False
                    break

                page_new_count = 0
                for idx, it in enumerate(c_items):
                    rev_id = str(it.external_review_id)
                    if rev_id in seen_ids:
                        continue
                    is_known = await self.repository.is_review_known(it.platform, rev_id)
                    if is_known:
                        continue

                    it_dt = parse_utc_datetime(it.published_at)
                    if (
                        oldest_known_dt is not None
                        and it_dt is not None
                        and it_dt < oldest_known_dt
                    ):
                        logger.info(
                            "Catch-up reached historical review %s for %s (%s) (%s < %s). "
                            "Saving remaining page silently and terminating catch-up.",
                            rev_id,
                            source.branch_name,
                            source.platform,
                            it.published_at,
                            oldest_ts,
                        )
                        historical_items.append(it)
                        seen_ids.add(rev_id)
                        hit_historical_horizon = True
                        for rest_it in c_items[idx + 1 :]:
                            rest_id = str(rest_it.external_review_id)
                            if rest_id not in seen_ids:
                                historical_items.append(rest_it)
                                seen_ids.add(rest_id)
                        break

                    new_items.append(it)
                    seen_ids.add(rev_id)
                    page_new_count += 1

                total_fetched += len(c_items)

                if hit_historical_horizon:
                    catchup_incomplete = False
                    break

                # Stop if this page had zero new reviews (all known)
                if page_new_count == 0:
                    catchup_incomplete = False
                    break

                # Stop if end of stream reached (less than full page returned)
                if len(c_items) < config.fetch_page_size:
                    catchup_incomplete = False
                    break

                current_page += 1

        if catchup_incomplete:
            return await self._handle_sync_failure(
                source,
                ReviewSyncStatus.UNKNOWN_ERROR,
                "Catch-up limit reached before the known review boundary; "
                "increase REVIEWS_MAX_CATCHUP_REVIEWS and retry. "
                "No new reviews were marked as known.",
                http_code,
                now_dt,
                now_iso,
                t_start,
                config,
            )

        # Silently persist any unindexed historical reviews (mark_sent=True)
        if historical_items:
            await self.repository.save_reviews(historical_items, source.id, mark_sent=True)

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
            runtime_ai = await self.runtime_settings.get_bool(
                "enable_gemini_review_analysis",
                self.settings.enable_gemini_review_analysis,
            )
            if runtime_ai:
                await self.ensure_ai_worker_started()
                for r in new_items:
                    if r.id is not None:
                        self.get_ai_worker().enqueue(r.id)
        else:
            status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS

        # Check for rating change alert
        # Both old and new ratings must be valid values in [1.0, 5.0] to prevent ghost alerts
        # from transient zero/unrated responses
        old_rating_val: float | None = None
        if source.last_rating is not None:
            try:
                r_val = round(float(source.last_rating), 1)
                if 1.0 <= r_val <= 5.0:
                    old_rating_val = r_val
            except (ValueError, TypeError):
                old_rating_val = None

        if (
            source.is_initialized
            and old_rating_val is not None
            and branch_rating is not None
            and 1.0 <= branch_rating <= 5.0
            and old_rating_val != branch_rating
            and config.alerts_enabled
            and self.alerts
        ):
            now_ts = time.time()
            last_alert = getattr(self, "_last_rating_alerts", {}).get(source.id)
            is_flapping = False
            if last_alert is not None:
                alert_ts, prev_old, prev_new = last_alert
                if (
                    prev_new == old_rating_val
                    and prev_old == branch_rating
                    and (now_ts - alert_ts) < 3600.0
                ):
                    is_flapping = True
                    logger.warning(
                        "Suppressing flapping rating alert for %s (%s): %s -> %s "
                        "(reverted within %.0fs)",
                        source.branch_name,
                        source.platform,
                        old_rating_val,
                        branch_rating,
                        now_ts - alert_ts,
                    )

            if not is_flapping:
                try:
                    logger.info(
                        "Rating changed for %s (%s): %s -> %s",
                        source.branch_name,
                        source.platform,
                        old_rating_val,
                        branch_rating,
                    )
                    effective_cnt = (
                        total_count if total_count is not None else source.total_reviews_count
                    )
                    await self.alerts.send_rating_change_alert(
                        source=source,
                        old_rating=old_rating_val,
                        new_rating=branch_rating,
                        total_count=effective_cnt,
                    )
                    if not hasattr(self, "_last_rating_alerts"):
                        self._last_rating_alerts = {}
                    self._last_rating_alerts[source.id] = (now_ts, old_rating_val, branch_rating)
                except Exception as rating_exc:
                    logger.error("Failed to send rating change alert: %s", rating_exc)

        # Do not overwrite valid last_rating with None if branch_rating failed to fetch
        effective_rating = (
            branch_rating
            if (branch_rating is not None and 1.0 <= branch_rating <= 5.0)
            else source.last_rating
        )
        effective_count = (
            total_count
            if (total_count is not None and (total_count > 0 or not source.total_reviews_count))
            else source.total_reviews_count
        )

        health_alert_active = await self._notify_recovery(source)
        await self.repository.update_source_status(
            source.id,
            status=status.value if hasattr(status, "value") else str(status),
            checked_at=now_iso,
            success=True,
            consecutive_errors=0,
            health_alert_active=health_alert_active,
            health_alert_sent_at=None,
            backoff_until=None,
            last_rating=effective_rating,
            total_reviews_count=effective_count,
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

        runtime_ai = await self.runtime_settings.get_bool(
            "enable_gemini_review_analysis",
            self.settings.enable_gemini_review_analysis,
        )
        if runtime_ai:
            await self.ensure_ai_worker_started()

        unsent = await self.repository.get_unsent_reviews(limit=50)
        sent_count = 0
        for review in unsent:
            if runtime_ai and getattr(review, "id", None):
                ai = await self.repository.get_ai_analysis(review.id)
                if ai is not None and ai.status == "PENDING":
                    continue

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

            # Re-enqueue any unanalyzed reviews for AI processing
            runtime_ai = await self.runtime_settings.get_bool(
                "enable_gemini_review_analysis",
                self.settings.enable_gemini_review_analysis,
            )
            if runtime_ai:
                await self.ensure_ai_worker_started()
                worker = self.get_ai_worker()
                unprocessed = await self.repository.get_pending_ai_reviews(limit=20)
                for unproc in unprocessed:
                    if unproc.id is not None:
                        worker.enqueue(unproc.id)

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
