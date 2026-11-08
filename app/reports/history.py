"""Bounded history backfill for an explicitly requested presentation period."""

import asyncio
from datetime import datetime, timezone

from app.reports.data import Period
from app.reviews.client_2gis import DGisReviewsClient
from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.models import ReviewSource, ReviewSyncStatus
from app.time import parse_iso_datetime

UTC = timezone.utc


class HistoricalReviewsLoader:
    def __init__(self, reviews_service):
        self.service = reviews_service

    async def ensure(self, period: Period, sources: list[dict]) -> list[str]:
        service = self.service
        proxy = service.settings.reviews_proxy_url
        proxy = proxy.get_secret_value() if proxy else None
        warnings = []
        for source in sources:
            if source["coverage_complete"]:
                continue
            current = await service.repository.get_source_by_id(source["id"])
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
                complete = await self._load(client, current, period)
                if not complete:
                    warnings.append(
                        f"{current.platform} / {current.branch_name}: "
                        "не удалось подтвердить полную историю."
                    )
            finally:
                await client.close()
            await asyncio.sleep(max(1, service.settings.reviews_request_pause_seconds))
        return warnings

    async def _load(self, client, source: ReviewSource, period: Period) -> bool:
        seen = set()
        page_size = 50
        quiet_before = self.service.notify_since or datetime.now(UTC)
        complete = False
        for page in range(1, 41):
            result = await client.fetch_reviews(source, page=page, page_size=page_size)
            if result[0] not in {
                ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
                ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
            }:
                break
            items = result[1]
            if not items:
                complete = True
                break
            ids = {item.external_review_id for item in items}
            if not ids - seen:
                break
            seen.update(ids)
            historical, live = [], []
            for item in items:
                date = parse_iso_datetime(item.published_at.replace("Z", "+00:00"))
                (historical if date < quiet_before else live).append(item)
            await self.service.repository.save_reviews(historical, source.id, mark_sent=True)
            await self.service.repository.save_reviews(live, source.id, mark_sent=False)
            if source.platform == "2gis" and result[4] is not None:
                await self.service.repository.save_rating_snapshot(
                    source.id, result[4], result[5], datetime.now(UTC).isoformat()
                )
            elif source.platform == "yandex" and client.last_rating is not None:
                await self.service.repository.save_rating_snapshot(
                    source.id,
                    client.last_rating,
                    client.last_total_count,
                    datetime.now(UTC).isoformat(),
                )
            if (
                min(parse_iso_datetime(item.published_at.replace("Z", "+00:00")) for item in items)
                <= period.start
            ):
                complete = True
                break
            has_more = getattr(client, "has_next_page", None)
            if has_more is not None:
                if not has_more(source, page_size, page):
                    complete = True
                    break
            elif len(items) < page_size:
                complete = True
                break
            await asyncio.sleep(max(1, self.service.settings.reviews_request_pause_seconds))
        if complete:
            connection = self.service.repository.database.require_connection()
            await connection.execute(
                """INSERT INTO review_history_coverage(source_id,period_start,period_end,checked_at)
                   VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                   period_start=excluded.period_start,period_end=excluded.period_end,checked_at=excluded.checked_at""",
                (
                    source.id,
                    period.start.isoformat(),
                    period.end.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await connection.commit()
        return complete
