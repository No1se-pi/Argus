"""Bounded history backfill for an explicitly requested presentation period."""

import asyncio
import logging
from datetime import datetime, timezone

from app.reports.data import Period
from app.reviews.client_2gis import DGisReviewsClient
from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.models import ReviewSource, ReviewSyncStatus
from app.time import parse_iso_datetime

UTC = timezone.utc
logger = logging.getLogger(__name__)


class HistoricalReviewsLoader:
    def __init__(self, reviews_service):
        self.service = reviews_service
        self.observed: dict[int, set[str]] = {}
        self.verified_owners: dict[int, bool] = {}

    async def ensure(self, period: Period, sources: list[dict]) -> list[str]:
        # Do not interleave history pagination and the live monitor's database work.
        async with self.service._sync_lock:
            return await self._ensure(period, sources)

    async def _ensure(self, period: Period, sources: list[dict]) -> list[str]:
        self.observed = {}
        self.verified_owners = {}
        service = self.service
        proxy = service.settings.reviews_proxy_url
        proxy = proxy.get_secret_value() if proxy else None
        warnings = []
        for source in sources:
            # Always refresh: cached history cannot reveal newly published/edited reviews.
            current = await service.repository.get_source_by_id(source["id"])
            connection = service.repository.database.require_connection()
            await connection.execute(
                "DELETE FROM review_history_coverage WHERE source_id=?", (source["id"],)
            )
            await connection.commit()
            if not current.is_active:
                warnings.append(f"{current.branch_name}: источник отключён, история не проверена.")
                continue
            if current.backoff_until and parse_iso_datetime(current.backoff_until) > datetime.now(
                UTC
            ):
                warnings.append(
                    f"{current.branch_name}: сбор истории отложен после ошибки площадки."
                )
                continue
            client = (
                YandexReviewsClient(proxy=proxy)
                if current.platform == "yandex"
                else DGisReviewsClient(proxy=proxy)
            )
            try:
                complete = await self._load(
                    client, current, period, comparison_start=period.comparison_start
                )
                if not complete:
                    warnings.append(
                        f"{current.platform} / {current.branch_name}: "
                        "не удалось подтвердить полную историю."
                    )
            except Exception:
                logger.exception("Review history failed source_id=%s", current.id)
                warnings.append(
                    f"{current.platform} / {current.branch_name}: ошибка загрузки истории; "
                    "сохранённые отзывы показаны без подтверждения полноты."
                )
            finally:
                verified = getattr(client, "last_verified_owner", None)
                if current.platform == "yandex" and type(verified) is bool:
                    self.verified_owners[current.id] = verified
                await client.close()
            await asyncio.sleep(max(1, service.settings.reviews_request_pause_seconds))
        return warnings

    async def _load(
        self, client, source: ReviewSource, period: Period, *, comparison_start=None
    ) -> bool:
        oldest = comparison_start or period.start
        requested_complete = False
        seen = set()
        self.observed[source.id] = seen
        page_size = 50
        quiet_before = self.service.notify_since or datetime.now(UTC)
        complete = False
        ordered = True
        previous_date = None
        rating_saved = False
        for page in range(1, 41):
            result = await client.fetch_reviews(source, page=page, page_size=page_size)
            # Retry one transient transport/server failure, never a CAPTCHA or rate limit.
            if result[0] == ReviewSyncStatus.NETWORK_ERROR or (
                result[0] == ReviewSyncStatus.HTTP_ERROR
                and result[3] is not None and result[3] >= 500
            ):
                await asyncio.sleep(max(2, self.service.settings.reviews_request_pause_seconds))
                result = await client.fetch_reviews(source, page=page, page_size=page_size)
            if result[0] not in {
                ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
                ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
            }:
                logger.warning("Review history source_id=%s page=%s status=%s error=%s",
                               source.id, page, result[0], result[2])
                break
            if not rating_saved:
                rating, count = (
                    (result[4], result[5]) if source.platform == "2gis"
                    else (client.last_rating, getattr(client, "last_total_count", None))
                )
                if rating is not None:
                    await self.service.repository.save_rating_snapshot(
                        source.id, rating, count, datetime.now(UTC).isoformat()
                    )
                    rating_saved = True
            items = result[1]
            if not items:
                complete = True
                break
            ids = {item.external_review_id for item in items}
            if not ids - seen:
                logger.warning("Review history repeated page source_id=%s page=%s", source.id, page)
                break
            seen.update(ids)
            historical, live = [], []
            dates = []
            for item in items:
                date = parse_iso_datetime(item.published_at.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError("Review date is missing timezone")
                dates.append(date)
                if previous_date is not None and date > previous_date:
                    ordered = False
                previous_date = date
                (historical if date < quiet_before else live).append(item)
            await self.service.repository.save_reviews(historical, source.id, mark_sent=True)
            await self.service.repository.save_reviews(live, source.id, mark_sent=False)
            logger.info("Review history source_id=%s page=%s items=%s unique=%s ordered=%s",
                        source.id, page, len(items), len(seen), ordered)
            # A pinned old review or an edited review must not terminate a mixed page.
            if ordered and max(dates) < period.start:
                requested_complete = True
                if max(dates) < oldest:
                    complete = True
                    break
            elif not ordered:
                requested_complete = False
            has_more = getattr(client, "has_next_page", None)
            if has_more is not None:
                if not has_more(source, page_size, page):
                    complete = True
                    break
            elif len(items) < page_size:
                complete = True
                break
            await asyncio.sleep(max(1, self.service.settings.reviews_request_pause_seconds))
        if complete or requested_complete:
            connection = self.service.repository.database.require_connection()
            await connection.execute(
                """INSERT INTO review_history_coverage(source_id,period_start,period_end,checked_at)
                   VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                   period_start=excluded.period_start,period_end=excluded.period_end,checked_at=excluded.checked_at""",
                (
                    source.id,
                    (oldest if complete else period.start).isoformat(),
                    period.end.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await connection.commit()
        logger.info("Review history finished source_id=%s period=%s complete=%s unique=%s",
                    source.id, period.label, complete, len(seen))
        return complete or requested_complete
