import asyncio
import contextlib
import json
import urllib.error
from datetime import datetime, timedelta

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone
    UTC = timezone.utc  # noqa: UP017
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramAPIError

from app.alerts.service import AlertService
from app.config import Settings
from app.reviews.client_2gis import DGisReviewsClient
from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.models import (
    DEFAULT_SOURCES,
    ReviewItem,
    ReviewPlatform,
    ReviewSource,
    ReviewsSyncAlreadyRunningError,
    ReviewSyncResult,
    ReviewSyncStatus,
)
from app.reviews.scheduler import ReviewsPollingScheduler
from app.reviews.service import ReviewsService
from app.storage.database import Database
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema


class DummyBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.should_fail_on_part: int | None = None
        self.fail_all: bool = False
        self.fail_count: int = 0

    async def send_message(self, chat_id: int, text: str, **kwargs) -> MagicMock:
        if self.fail_all:
            self.fail_count += 1
            raise TelegramAPIError(method=MagicMock(), message="Telegram network error simulation")
        if self.should_fail_on_part is not None:
            # Check if text corresponds to failure trigger
            if f"[Часть {self.should_fail_on_part}/" in text:
                self.fail_count += 1
                raise TelegramAPIError(
                    method=MagicMock(), message="Telegram network error simulation"
                )

        self.sent_messages.append({"chat_id": chat_id, "text": text, **kwargs})
        msg = MagicMock()
        msg.message_id = len(self.sent_messages)
        return msg


class MockYandexClient:
    def __init__(self) -> None:
        self.calls: int = 0
        self.response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
        self.reviews_to_return: list[ReviewItem] = []
        self.error_message: str | None = None
        self.http_code: int = 200

    async def fetch_reviews(self, source: ReviewSource, page: int = 1, page_size: int = 10):
        self.calls += 1
        return (
            self.response_status,
            list(self.reviews_to_return),
            self.error_message,
            self.http_code,
        )

    async def close(self) -> None:
        pass


class MockDGisClient:
    def __init__(self) -> None:
        self.calls: int = 0
        self.response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
        self.reviews_to_return: list[ReviewItem] = []
        self.error_message: str | None = None
        self.http_code: int = 200
        self.branch_rating: float | None = 4.8
        self.total_count: int | None = 150

    async def fetch_reviews(self, source: ReviewSource, page: int = 1, page_size: int = 10):
        self.calls += 1
        return (
            self.response_status,
            list(self.reviews_to_return),
            self.error_message,
            self.http_code,
            self.branch_rating,
            self.total_count,
        )

    async def close(self) -> None:
        pass


@pytest.fixture
async def test_env(tmp_path: Path):
    db_file = tmp_path / "test_argus.sqlite3"
    database = Database(db_file)
    await database.connect()
    await init_schema(database)

    bundle = RepositoryBundle(database)
    settings = Settings(
        bot_token="123456:dummy_test_token",
        admin_ids_text="99999",
        enable_reviews_monitor=True,
        reviews_poll_interval_seconds=900,
        reviews_request_pause_seconds=0.0,
        reviews_fetch_page_size=10,
        reviews_max_catchup_reviews=50,
        reviews_error_alert_threshold=3,
        alerts_reviews_enabled=True,
    )
    bot = DummyBot()
    alert_service = AlertService(
        bot=bot,  # type: ignore
        settings=settings,
        alerts=bundle.alerts,
        runtime_settings=bundle.runtime_settings,
    )
    yandex_client = MockYandexClient()
    dgis_client = MockDGisClient()

    service = ReviewsService(
        settings=settings,
        runtime_settings=bundle.runtime_settings,
        repository=bundle.reviews,
        alerts=alert_service,
        yandex_client=yandex_client,
        dgis_client=dgis_client,
    )

    yield {
        "db": database,
        "bundle": bundle,
        "settings": settings,
        "bot": bot,
        "alert_service": alert_service,
        "service": service,
        "yandex_client": yandex_client,
        "dgis_client": dgis_client,
    }

    await service.close()
    await database.close()


# ==============================================================================
# 1. Default sources registration (exact 12, idempotent)
# ==============================================================================
@pytest.mark.asyncio
async def test_default_sources_registration_exact_12_no_duplicates(test_env):
    bundle = test_env["bundle"]
    # First seed
    count1 = await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    assert count1 == 12
    sources = await bundle.reviews.list_sources()
    assert len(sources) == 12

    yandex_sources = [s for s in sources if s.platform == ReviewPlatform.YANDEX]
    dgis_sources = [s for s in sources if s.platform == ReviewPlatform.DGIS]
    assert len(yandex_sources) == 6
    assert len(dgis_sources) == 6

    # Idempotent re-run
    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    sources_after = await bundle.reviews.list_sources()
    assert len(sources_after) == 12


# ==============================================================================
# 2. Database migration preserves existing tables & data
# ==============================================================================
@pytest.mark.asyncio
async def test_database_migration_preserves_existing_tables(tmp_path: Path):
    db_file = tmp_path / "old_argus.sqlite3"
    db = Database(db_file)
    await db.connect()

    # Pre-populate with older schema data
    await init_schema(db)
    conn = db.require_connection()
    await conn.execute(
        "INSERT INTO sources (kind, link, title, created_at, updated_at) "
        "VALUES ('telegram', 'https://t.me/test', 'Test Source', '2026-01-01', '2026-01-01')"
    )
    await conn.commit()

    # Run init_schema again (simulating app restart / upgrade)
    await init_schema(db)

    # Verify original source still exists
    async with conn.execute("SELECT title FROM sources WHERE link = 'https://t.me/test'") as cur:
        row = await cur.fetchone()
        assert row is not None
        assert row["title"] == "Test Source"

    # Verify review tables and columns exist
    async with conn.execute("PRAGMA table_info(review_sources)") as cur:
        cols = {r["name"] for r in await cur.fetchall()}
        assert "health_alert_active" in cols
        assert "health_alert_sent_at" in cols

    async with conn.execute("PRAGMA table_info(reviews)") as cur:
        cols = {r["name"] for r in await cur.fetchall()}
        assert "telegram_parts_total" in cols
        assert "telegram_parts_sent" in cols
        assert "last_delivery_error" in cols

    await db.close()


# ==============================================================================
# 3. ENABLE_REVIEWS_MONITOR=false means ZERO HTTP requests
# ==============================================================================
@pytest.mark.asyncio
async def test_enable_reviews_monitor_false_means_zero_http_requests(test_env):
    service = test_env["service"]
    y_client = test_env["yandex_client"]
    d_client = test_env["dgis_client"]
    runtime_settings = test_env["bundle"].runtime_settings

    # Explicitly disable
    await runtime_settings.set("enable_reviews_monitor", "false")

    results = await service.sync_all_sources(force=False)
    assert len(results) == 0
    assert y_client.calls == 0
    assert d_client.calls == 0


# ==============================================================================
# 4. Runtime enable starts polling without restart
# ==============================================================================
@pytest.mark.asyncio
async def test_runtime_enable_starts_polling_without_restart(test_env):
    service = test_env["service"]
    settings = test_env["settings"]
    y_client = test_env["yandex_client"]
    runtime_settings = test_env["bundle"].runtime_settings

    # Start disabled
    await runtime_settings.set("enable_reviews_monitor", "false")
    scheduler = ReviewsPollingScheduler(settings=settings, service=service)

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    assert y_client.calls == 0

    # Runtime enable + wake
    await runtime_settings.set("enable_reviews_monitor", "true")
    scheduler.wake()
    await asyncio.sleep(0.1)

    assert y_client.calls > 0

    scheduler.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ==============================================================================
# 5. Runtime disable stops polling
# ==============================================================================
@pytest.mark.asyncio
async def test_runtime_disable_stops_polling(test_env):
    service = test_env["service"]
    settings = test_env["settings"]
    y_client = test_env["yandex_client"]
    runtime_settings = test_env["bundle"].runtime_settings

    await runtime_settings.set("enable_reviews_monitor", "true")
    scheduler = ReviewsPollingScheduler(settings=settings, service=service)

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.1)
    calls_before = y_client.calls
    assert calls_before > 0

    # Runtime disable + wake
    await runtime_settings.set("enable_reviews_monitor", "false")
    scheduler.wake()
    await asyncio.sleep(0.1)

    calls_after = y_client.calls
    assert calls_after == calls_before

    scheduler.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ==============================================================================
# 6. Scheduler survives unexpected exception (no silent death)
# ==============================================================================
@pytest.mark.asyncio
async def test_scheduler_survives_unexpected_exception(test_env):
    service = test_env["service"]
    settings = test_env["settings"]

    call_count = 0

    async def flaky_sync(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Unexpected boom in loop!")
        return []

    service.sync_all_sources = flaky_sync  # type: ignore

    scheduler = ReviewsPollingScheduler(settings=settings, service=service)
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)

    # Scheduler task must still be running despite the exception!
    assert not task.done()
    status = await scheduler.get_status()
    assert status["consecutive_loop_crashes"] >= 1

    scheduler.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ==============================================================================
# 7. Stale cycle detection
# ==============================================================================
@pytest.mark.asyncio
async def test_scheduler_stale_cycle_detection(test_env):
    service = test_env["service"]
    settings = test_env["settings"]
    scheduler = ReviewsPollingScheduler(settings=settings, service=service)

    # Set last completed cycle to 2 hours ago (interval is 900s = 15m)
    stale_time = datetime.now(UTC) - timedelta(hours=2)
    scheduler._last_completed_cycle_at = stale_time

    status = await scheduler.get_status()
    assert status["health_status"] == "DEGRADED_STALE"


# ==============================================================================
# 8. Edited old review does NOT mask new review
# ==============================================================================
@pytest.mark.asyncio
async def test_old_review_edited_does_not_mask_new_review(test_env):
    service = test_env["service"]
    d_client = test_env["dgis_client"]
    bundle = test_env["bundle"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("2gis", "4504127908536611")
    assert source is not None

    # Step 1: Baseline setup
    old_review = ReviewItem(
        external_review_id="rev-old-1",
        platform=ReviewPlatform.DGIS,
        branch_name=source.branch_name,
        author_name="Old Author",
        rating=4,
        text="Original review text",
        published_at="2026-01-01T10:00:00Z",
        edited_at=None,
        review_url="https://2gis.ru/reviews/rev-old-1",
    )
    d_client.reviews_to_return = [old_review]
    res1 = await service.sync_source(source)
    assert res1.status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    assert (
        source.is_initialized or (await bundle.reviews.get_source_by_id(source.id)).is_initialized
    )

    # Step 2: Next cycle returns edited old review at top, plus new review below it
    refreshed_source = await bundle.reviews.get_source_by_id(source.id)
    edited_old_review = ReviewItem(
        external_review_id="rev-old-1",
        platform=ReviewPlatform.DGIS,
        branch_name=source.branch_name,
        author_name="Old Author",
        rating=5,  # edited rating
        text="Updated review text",
        published_at="2026-01-01T10:00:00Z",
        edited_at="2026-09-10T12:00:00Z",  # bumped to top by date_edited
        review_url="https://2gis.ru/reviews/rev-old-1",
    )
    brand_new_review = ReviewItem(
        external_review_id="rev-brand-new-2",
        platform=ReviewPlatform.DGIS,
        branch_name=source.branch_name,
        author_name="New Student",
        rating=5,
        text="Brand new fresh review!",
        published_at="2026-09-10T11:00:00Z",
        edited_at=None,
        review_url="https://2gis.ru/reviews/rev-brand-new-2",
    )
    d_client.reviews_to_return = [edited_old_review, brand_new_review]

    res2 = await service.sync_source(refreshed_source)
    assert res2.status == ReviewSyncStatus.SUCCESS_NEW_REVIEWS
    # Only brand new review must be detected as new!
    assert len(res2.new_reviews) == 1
    assert res2.new_reviews[0].external_review_id == "rev-brand-new-2"


# ==============================================================================
# 9. Long review partial delivery retry (multipart progress)
# ==============================================================================
@pytest.mark.asyncio
async def test_long_review_partial_delivery_retry(test_env):
    bundle = test_env["bundle"]
    bot = test_env["bot"]
    alert_service = test_env["alert_service"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    # Create a long review (> 4000 chars) to produce 2 parts
    long_text = "Очень подробный и полезный отзыв о колледже. " * 120  # ~5400 chars
    review = ReviewItem(
        external_review_id="rev-long-1",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Алексей Длинный",
        rating=5,
        text=long_text,
        published_at="2026-09-10T12:00:00Z",
    )

    await bundle.reviews.save_reviews([review], source.id, mark_sent=False)
    unsent = await bundle.reviews.get_unsent_reviews()
    saved_review = unsent[0]

    # First attempt: bot fails on part 2
    bot.should_fail_on_part = 2
    with pytest.raises(TelegramAPIError):
        await alert_service.send_review_alert(saved_review, source, bundle.reviews)

    # Verify partial progress persisted in SQLite
    after_fail = (await bundle.reviews.get_unsent_reviews())[0]
    assert after_fail.telegram_parts_sent == 1
    assert after_fail.is_sent_to_telegram is False
    assert after_fail.telegram_parts_total >= 2

    # Second attempt: bot succeeds on part 2
    bot.should_fail_on_part = None
    messages_count_before = len(bot.sent_messages)

    ok = await alert_service.send_review_alert(after_fail, source, bundle.reviews)
    assert ok is True

    # Part 1 was NOT re-sent; only remaining part was sent!
    assert len(bot.sent_messages) == messages_count_before + 1

    # In DB: is_sent_to_telegram == 1 now
    unsent_final = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_final) == 0


# ==============================================================================
# 10. Baseline cold start (0 alerts, marked initialized)
# ==============================================================================
@pytest.mark.asyncio
async def test_baseline_initialization_no_alerts(test_env):
    service = test_env["service"]
    y_client = test_env["yandex_client"]
    bot = test_env["bot"]
    bundle = test_env["bundle"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert source.is_initialized is False

    y_client.reviews_to_return = [
        ReviewItem(
            external_review_id=f"y-hist-{i}",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name=f"Student {i}",
            rating=5,
            text=f"Historical review {i}",
            published_at=f"2026-01-0{i + 1}T10:00:00Z",
        )
        for i in range(5)
    ]

    res = await service.sync_source(source)
    assert res.status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    assert len(res.new_reviews) == 0

    # Ensure ZERO Telegram alerts were sent
    assert len(bot.sent_messages) == 0

    # Source must now be marked initialized in DB
    updated_source = await bundle.reviews.get_source_by_id(source.id)
    assert updated_source.is_initialized is True

    # 5 reviews saved with is_sent_to_telegram = 1
    unsent = await bundle.reviews.get_unsent_reviews()
    assert len(unsent) == 0


# ==============================================================================
# 11. New review triggers exactly 1 alert
# ==============================================================================
@pytest.mark.asyncio
async def test_new_review_triggers_alert(test_env):
    service = test_env["service"]
    y_client = test_env["yandex_client"]
    bot = test_env["bot"]
    bundle = test_env["bundle"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    # Baseline
    y_client.reviews_to_return = [
        ReviewItem(
            external_review_id="y-base-1",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name="Base Author",
            rating=5,
            text="Base text",
            published_at="2026-01-01T10:00:00Z",
        )
    ]
    await service.sync_source(source)
    assert len(bot.sent_messages) == 0

    # New review arrives
    refreshed_source = await bundle.reviews.get_source_by_id(source.id)
    new_rev = ReviewItem(
        external_review_id="y-new-2",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Fresh Student",
        rating=5,
        text="Brand new alert worthy review",
        published_at="2026-09-10T15:00:00Z",
    )
    y_client.reviews_to_return = [new_rev, y_client.reviews_to_return[0]]

    res = await service.sync_source(refreshed_source)
    assert res.status == ReviewSyncStatus.SUCCESS_NEW_REVIEWS
    assert len(res.new_reviews) == 1

    # Deliver
    sent_count = await service.deliver_pending_reviews()
    assert sent_count == 1
    assert len(bot.sent_messages) == 1
    assert "Fresh Student" in bot.sent_messages[0]["text"]


# ==============================================================================
# 12. Subsequent polling deduplication (no 2nd alert)
# ==============================================================================
@pytest.mark.asyncio
async def test_subsequent_polling_deduplication(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]

    source = (
        (await bundle.reviews.list_sources())[0] if (await bundle.reviews.list_sources()) else None
    )
    if not source:
        await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
        source = (await bundle.reviews.list_sources())[0]

    # Poll again with same reviews
    res = await service.sync_source(source)
    assert res.status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    assert len(res.new_reviews) == 0

    sent_count = await service.deliver_pending_reviews()
    assert sent_count == 0


# ==============================================================================
# 13. Multiple new reviews delivered in chronological order (oldest to newest)
# ==============================================================================
@pytest.mark.asyncio
async def test_multiple_new_reviews_chronological(test_env):
    service = test_env["service"]
    y_client = test_env["yandex_client"]
    bundle = test_env["bundle"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1011075765")
    await bundle.reviews.mark_source_initialized(source.id)
    source = await bundle.reviews.get_source_by_id(source.id)

    # 3 new reviews with unsorted published_at
    r_late = ReviewItem(
        "r-late",
        ReviewPlatform.YANDEX,
        source.branch_name,
        "Late",
        5,
        "late",
        "2026-09-10T18:00:00Z",
    )
    r_early = ReviewItem(
        "r-early",
        ReviewPlatform.YANDEX,
        source.branch_name,
        "Early",
        5,
        "early",
        "2026-09-10T10:00:00Z",
    )
    r_mid = ReviewItem(
        "r-mid", ReviewPlatform.YANDEX, source.branch_name, "Mid", 5, "mid", "2026-09-10T14:00:00Z"
    )

    y_client.reviews_to_return = [r_late, r_early, r_mid]

    res = await service.sync_source(source)
    assert res.status == ReviewSyncStatus.SUCCESS_NEW_REVIEWS
    # Verify order in new_reviews: oldest to newest
    assert [r.external_review_id for r in res.new_reviews] == ["r-early", "r-mid", "r-late"]


# ==============================================================================
# 14. Restart simulation preserves state
# ==============================================================================
@pytest.mark.asyncio
async def test_restart_simulation(test_env):
    db = test_env["db"]
    bundle = test_env["bundle"]
    settings = test_env["settings"]
    bot = test_env["bot"]
    y_client = test_env["yandex_client"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = (await bundle.reviews.list_sources())[0]

    # Baseline initialized
    y_client.reviews_to_return = [
        ReviewItem(
            "rev-1", ReviewPlatform.YANDEX, source.branch_name, "A", 5, "t", "2026-01-01T00:00:00Z"
        )
    ]
    await test_env["service"].sync_source(source)

    # Create new service instance simulating app restart
    new_bundle = RepositoryBundle(db)
    new_service = ReviewsService(
        settings=settings,
        runtime_settings=new_bundle.runtime_settings,
        repository=new_bundle.reviews,
        alerts=test_env["alert_service"],
        yandex_client=y_client,
    )

    reloaded_source = await new_bundle.reviews.get_source_by_id(source.id)
    assert reloaded_source.is_initialized is True

    res = await new_service.sync_source(reloaded_source)
    assert res.status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    assert len(res.new_reviews) == 0
    assert len(bot.sent_messages) == 0


# ==============================================================================
# 15. HTML escaping in alerts
# ==============================================================================
@pytest.mark.asyncio
async def test_html_escaping_in_alerts(test_env):
    alert_service = test_env["alert_service"]
    bot = test_env["bot"]
    bundle = test_env["bundle"]

    review = ReviewItem(
        external_review_id="rev-xss-1",
        platform=ReviewPlatform.YANDEX,
        branch_name="<script>alert(1)</script>",
        author_name="<b>Hacker</b>",
        rating=5,
        text="Hello <tag> & world",
        published_at="2026-09-10T12:00:00Z",
        review_url="https://yandex.ru/maps?foo=1&bar=2",
    )
    source = ReviewSource(
        1, ReviewPlatform.YANDEX, "<script>alert(1)</script>", "123", "http://yandex.ru"
    )

    await alert_service.send_review_alert(review, source, bundle.reviews)
    assert len(bot.sent_messages) == 1
    sent_text = bot.sent_messages[0]["text"]

    # Verify raw tags are escaped
    assert "<script>" not in sent_text
    assert "&lt;script&gt;" in sent_text
    assert "&lt;tag&gt;" in sent_text
    assert "&lt;b&gt;Hacker&lt;/b&gt;" in sent_text


# ==============================================================================
# 16. Source failure isolation
# ==============================================================================
@pytest.mark.asyncio
async def test_source_failure_isolation(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    y_client = test_env["yandex_client"]
    d_client = test_env["dgis_client"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    sources = await bundle.reviews.list_sources()

    # Make Yandex fail, but 2GIS succeed
    y_client.response_status = ReviewSyncStatus.NETWORK_ERROR
    y_client.error_message = "DNS failure"

    d_client.response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    d_client.reviews_to_return = []

    results = await service.sync_all_sources(force=True)
    assert len(results) == len(sources)

    y_results = [r for r in results if r.source.platform == ReviewPlatform.YANDEX]
    d_results = [r for r in results if r.source.platform == ReviewPlatform.DGIS]

    assert all(r.status == ReviewSyncStatus.NETWORK_ERROR for r in y_results)
    assert all(r.status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS for r in d_results)


# ==============================================================================
# 17. Rate limited backoff
# ==============================================================================
@pytest.mark.asyncio
async def test_rate_limited_backoff(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    y_client = test_env["yandex_client"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = (await bundle.reviews.list_sources())[0]

    y_client.response_status = ReviewSyncStatus.RATE_LIMITED
    y_client.http_code = 429

    res = await service.sync_source(source)
    assert res.status == ReviewSyncStatus.RATE_LIMITED

    # Source should now have backoff_until set
    updated_source = await bundle.reviews.get_source_by_id(source.id)
    assert updated_source.backoff_until is not None

    # Next call immediately skips due to backoff
    y_calls_before = y_client.calls
    res2 = await service.sync_source(updated_source)
    assert res2.status == ReviewSyncStatus.RATE_LIMITED
    assert y_client.calls == y_calls_before


# ==============================================================================
# 18. Health transient error does NOT send recovery alert
# ==============================================================================
@pytest.mark.asyncio
async def test_health_transient_error_no_recovery_alert(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]
    y_client = test_env["yandex_client"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = (await bundle.reviews.list_sources())[0]
    await bundle.reviews.mark_source_initialized(source.id)

    # 1 transient error
    y_client.response_status = ReviewSyncStatus.NETWORK_ERROR
    await service.sync_source(source)
    assert len(bot.sent_messages) == 0  # no alert (threshold is 3)

    # Next cycle: success
    refreshed = await bundle.reviews.get_source_by_id(source.id)
    y_client.response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    await service.sync_source(refreshed)

    # MUST NOT send "источник восстановлен" because no problem alert was ever sent!
    assert len(bot.sent_messages) == 0


# ==============================================================================
# 19. Health threshold alert + single recovery alert
# ==============================================================================
@pytest.mark.asyncio
async def test_health_alert_and_recovery(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]
    y_client = test_env["yandex_client"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = (await bundle.reviews.list_sources())[0]
    await bundle.reviews.mark_source_initialized(source.id)

    y_client.response_status = ReviewSyncStatus.SOURCE_BLOCKED
    y_client.error_message = "HTTP 403 Forbidden"

    # Error 1
    s = await bundle.reviews.get_source_by_id(source.id)
    await service.sync_source(s)
    assert len(bot.sent_messages) == 0

    # Error 2
    s = await bundle.reviews.get_source_by_id(source.id)
    s.backoff_until = None  # reset backoff for testing
    await service.sync_source(s)
    assert len(bot.sent_messages) == 0

    # Error 3 (threshold reached: triggers health alert!)
    s = await bundle.reviews.get_source_by_id(source.id)
    s.backoff_until = None
    await service.sync_source(s)
    assert len(bot.sent_messages) == 1
    assert "проблема источника" in bot.sent_messages[0]["text"]

    # Error 4 (continues in degraded state: MUST NOT spam repeat alert)
    s = await bundle.reviews.get_source_by_id(source.id)
    s.backoff_until = None
    await service.sync_source(s)
    assert len(bot.sent_messages) == 1

    # Recovery: source succeeds
    s = await bundle.reviews.get_source_by_id(source.id)
    s.backoff_until = None
    y_client.response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    await service.sync_source(s)

    # Exactly 1 recovery alert sent!
    assert len(bot.sent_messages) == 2
    assert "источник восстановлен" in bot.sent_messages[1]["text"]

    # Subsequent success does NOT repeat recovery alert
    s = await bundle.reviews.get_source_by_id(source.id)
    await service.sync_source(s)
    assert len(bot.sent_messages) == 2


# ==============================================================================
# 20. Manual sync locking
# ==============================================================================
@pytest.mark.asyncio
async def test_manual_sync_locking(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)

    active_executions = 0
    max_concurrent = 0

    async def tracking_sync(source):
        nonlocal active_executions, max_concurrent
        active_executions += 1
        max_concurrent = max(max_concurrent, active_executions)
        await asyncio.sleep(0.01)
        active_executions -= 1
        return ReviewSyncResult(source=source, status=ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS)

    service.sync_source = tracking_sync  # type: ignore

    # Start first sync
    task1 = asyncio.create_task(service.sync_all_sources(force=True))
    await asyncio.sleep(0.002)

    # Second concurrent call must raise ReviewsSyncAlreadyRunningError immediately
    with pytest.raises(ReviewsSyncAlreadyRunningError):
        await service.sync_all_sources(force=True)

    await task1
    assert max_concurrent == 1


# ==============================================================================
# 21. Parser changed when HTTP 200 has invalid/broken schema
# ==============================================================================
@pytest.mark.asyncio
async def test_parser_changed_http_200_invalid_schema(test_env):
    """
    HTTP status = 200, response is syntactically valid JSON,
    but required reviews structure is missing/broken.
    MUST result in PARSER_FORMAT_CHANGED and NEVER in SUCCESS_NO_NEW_REVIEWS.
    """
    from app.reviews.client_2gis import DGisReviewsClient
    from app.reviews.client_yandex import YandexReviewsClient

    service = test_env["service"]
    bundle = test_env["bundle"]
    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)

    # 1. Test 2GIS client directly with broken HTTP 200 schema
    dgis_source = await bundle.reviews.get_source_by_external_id("2gis", "4504127908536611")
    assert dgis_source is not None

    broken_2gis_responses = [
        {"meta": {"branch_rating": 4.5}},  # missing "reviews"
        {"reviews": "not a list", "meta": {}},  # "reviews" is not a list
        {"error": "deprecated format", "code": 200},  # completely unexpected schema
    ]

    for broken_json in broken_2gis_responses:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=broken_json)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value.__aenter__.return_value = mock_resp

        client_2gis = DGisReviewsClient()
        client_2gis._session = mock_session

        status, items, err, code, _, _ = await client_2gis.fetch_reviews(dgis_source)
        assert status == ReviewSyncStatus.PARSER_FORMAT_CHANGED, f"Failed for {broken_json}"
        assert status != ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
        assert code == 200
        assert items == []
        assert "Invalid response schema" in (err or "")

    # 2. Test Yandex client directly with broken HTTP 200 schema
    yandex_source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert yandex_source is not None

    broken_yandex_responses = [
        {"data": {"something_else": []}},  # data dict without "reviews"
        {"data": "not a dict"},  # data is not a dict
        {"status": "ok", "items": []},  # missing "data" wrapper entirely
    ]

    for broken_json in broken_yandex_responses:
        client_yandex = YandexReviewsClient()
        client_yandex._csrf_token = "dummy_csrf"
        client_yandex._session_id = "dummy_session"
        client_yandex._base_origin = "https://yandex.ru"

        mock_resp_yandex = MagicMock()
        mock_resp_yandex.read.return_value = json.dumps(broken_json).encode("utf-8")
        mock_resp_yandex.__enter__.return_value = mock_resp_yandex
        mock_resp_yandex.__exit__.return_value = False

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp_yandex
        client_yandex._opener = mock_opener

        status, items, err, code = await client_yandex.fetch_reviews(yandex_source)
        assert status == ReviewSyncStatus.PARSER_FORMAT_CHANGED, f"Failed for {broken_json}"
        assert status != ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
        assert code == 200
        assert items == []
        assert "Invalid response schema" in (err or "")

    # 3. Test through ReviewsService.sync_source: must increment errors and NOT mark initialized
    mock_d_client = test_env["dgis_client"]
    mock_d_client.response_status = ReviewSyncStatus.PARSER_FORMAT_CHANGED
    mock_d_client.reviews_to_return = []
    mock_d_client.error_message = "Invalid response schema: expected reviews list"
    mock_d_client.http_code = 200

    sync_res = await service.sync_source(dgis_source)
    assert sync_res.status == ReviewSyncStatus.PARSER_FORMAT_CHANGED
    assert sync_res.status != ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    assert sync_res.new_reviews == []

    updated_source = await bundle.reviews.get_source_by_id(dgis_source.id)
    assert updated_source.consecutive_errors == 1
    assert updated_source.last_status == "PARSER_FORMAT_CHANGED"
    assert updated_source.is_initialized is False


# ==============================================================================
# 22. Short review Telegram send failure and delivery retry
# ==============================================================================
@pytest.mark.asyncio
async def test_short_review_telegram_send_failure_retry(test_env):
    """
    Scenario:
    - new standard short review saved (is_sent_to_telegram=0);
    - first Telegram send fails (bot.fail_all = True);
    - is_sent_to_telegram remains False, delivery error logged;
    - next delivery retry sends it (bot.fail_all = False);
    - after success is_sent_to_telegram=True;
    - second successful duplicate is NOT sent.
    """
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert source is not None

    # Step 1: Save short review in SQLite with mark_sent=False
    short_review = ReviewItem(
        external_review_id="rev-short-fail-1",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Иван Иванов",
        rating=5,
        text="Отличный колледж, сильные преподаватели и хорошая практика!",
        published_at="2026-09-10T15:00:00Z",
    )
    inserted = await bundle.reviews.save_reviews([short_review], source.id, mark_sent=False)
    assert inserted == 1

    unsent_before = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_before) == 1
    assert unsent_before[0].is_sent_to_telegram is False
    assert unsent_before[0].telegram_parts_sent == 0

    # Step 2: First delivery attempt FAILS
    bot.fail_all = True
    sent_count_1 = await service.deliver_pending_reviews()
    assert sent_count_1 == 0
    assert len(bot.sent_messages) == 0

    # Verify is_sent_to_telegram remains False, error is captured
    unsent_after_fail = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_after_fail) == 1
    assert unsent_after_fail[0].is_sent_to_telegram is False
    assert unsent_after_fail[0].telegram_parts_sent == 0
    assert unsent_after_fail[0].last_delivery_error is not None
    assert "Telegram network error simulation" in unsent_after_fail[0].last_delivery_error

    # Step 3: Next delivery retry SUCCEEDS
    bot.fail_all = False
    sent_count_2 = await service.deliver_pending_reviews()
    assert sent_count_2 == 1
    assert len(bot.sent_messages) == 1
    assert "Иван Иванов" in bot.sent_messages[0]["text"]

    # Verify in DB: is_sent_to_telegram is now True, unsent is empty
    unsent_after_success = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_after_success) == 0

    recent = await bundle.reviews.get_recent_reviews(limit=5)
    matched = [r for r in recent if r.external_review_id == "rev-short-fail-1"]
    assert len(matched) == 1
    assert matched[0].is_sent_to_telegram is True
    assert matched[0].telegram_parts_sent == 1
    assert matched[0].telegram_sent_at is not None

    # Step 4: Subsequent call must NOT send a duplicate
    sent_count_3 = await service.deliver_pending_reviews()
    assert sent_count_3 == 0
    # Sent messages count must remain exactly 1!
    assert len(bot.sent_messages) == 1


# ==============================================================================
# 23. Alerts source_id=None schema and FK integrity proof
# ==============================================================================
@pytest.mark.asyncio
async def test_alerts_source_id_none_schema_and_fk_integrity(test_env):
    """
    Prove that source_id=None for review alerts:
    - is permitted by SQLite schema;
    - does not break Foreign Key constraints (PRAGMA foreign_key_check is empty);
    - properly sets platform ('yandex' / '2gis') instead of 'telegram';
    - does not collide when Yandex and 2GIS share identical external_review_id;
    - does not alter Telegram/VK alerts statistics or tables.
    """
    database = test_env["db"]
    bundle = test_env["bundle"]
    alert_service = test_env["alert_service"]

    conn = database.require_connection()
    await conn.execute("PRAGMA foreign_keys = ON")

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    y_source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    d_source = await bundle.reviews.get_source_by_external_id("2gis", "4504127908536611")

    # Identical external_review_id on both platforms
    y_review = ReviewItem(
        external_review_id="shared-id-999",
        platform=ReviewPlatform.YANDEX,
        branch_name=y_source.branch_name,
        author_name="User 1",
        rating=5,
        text="Yandex text",
        published_at="2026-09-10T12:00:00Z",
    )
    d_review = ReviewItem(
        external_review_id="shared-id-999",
        platform=ReviewPlatform.DGIS,
        branch_name=d_source.branch_name,
        author_name="User 2",
        rating=4,
        text="2GIS text",
        published_at="2026-09-10T12:05:00Z",
    )

    await bundle.reviews.save_reviews([y_review], y_source.id, mark_sent=False)
    await bundle.reviews.save_reviews([d_review], d_source.id, mark_sent=False)

    unsent = await bundle.reviews.get_unsent_reviews()
    assert len(unsent) == 2

    # Send alerts for both
    for r in unsent:
        src = y_source if r.platform == ReviewPlatform.YANDEX else d_source
        ok = await alert_service.send_review_alert(r, src, bundle.reviews)
        assert ok is True

    # 1. Check foreign key integrity: MUST be 0 errors
    async with conn.execute("PRAGMA foreign_key_check") as cursor:
        fk_errors = await cursor.fetchall()
    assert len(fk_errors) == 0, f"Foreign key violations found: {fk_errors}"

    # 2. Check alerts table rows: source_id IS NULL, post_id IS NULL, platform correct
    async with conn.execute(
        "SELECT platform, source_id, post_id, item_type, item_id, alert_type, status FROM alerts"
    ) as cursor:
        rows = await cursor.fetchall()

    assert len(rows) == 2
    platforms = {r["platform"] for r in rows}
    assert platforms == {"yandex", "2gis"}

    for r in rows:
        assert r["source_id"] is None
        assert r["post_id"] is None
        assert r["item_type"] == "review"
        assert r["item_id"] == "shared-id-999"
        assert r["alert_type"] == "new_review"
        assert r["status"] == "sent"

    # 3. Both reviews in reviews table are properly isolated (no collision)
    assert (await bundle.reviews.is_review_known("yandex", "shared-id-999")) is True
    assert (await bundle.reviews.is_review_known("2gis", "shared-id-999")) is True


# ==============================================================================
# 24. Yandex 403 Session Refresh before retry & max 1 retry
# ==============================================================================
@pytest.mark.asyncio
async def test_yandex_403_refreshes_session_before_retry():
    client = YandexReviewsClient()
    source = ReviewSource(
        id=1,
        platform=ReviewPlatform.YANDEX,
        branch_name="Тест",
        external_id="12345",
        url="https://yandex.ru/maps/org/test/12345/",
    )

    client._csrf_token = "stale-csrf"
    client._session_id = "stale-session"
    client._base_origin = "https://yandex.ru"

    mock_resp_page = MagicMock()
    mock_resp_page.geturl.return_value = "https://yandex.ru/maps/org/test/12345/"
    mock_resp_page.read.return_value = (
        b'<html><script type="application/json">{"config":{"csrfToken":"fresh-csrf",'
        b'"counters":{"analytics":{"sessionId":"fresh-session"}}}}</script></html>'
    )
    mock_resp_page.__enter__.return_value = mock_resp_page
    mock_resp_page.__exit__.return_value = None

    valid_reviews_json = json.dumps(
        {
            "data": {
                "reviews": [
                    {
                        "reviewId": "rev-fresh-1",
                        "rating": 5,
                        "author": {"name": "Fresh User"},
                        "text": "Great place",
                        "updatedTime": "2026-09-10T12:00:00Z",
                    }
                ]
            }
        }
    ).encode("utf-8")
    mock_resp_api = MagicMock()
    mock_resp_api.read.return_value = valid_reviews_json
    mock_resp_api.__enter__.return_value = mock_resp_api
    mock_resp_api.__exit__.return_value = None

    calls = []

    def fake_open(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        if "fetchReviews" in url and len([c for c in calls if "fetchReviews" in c]) == 1:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if "fetchReviews" in url:
            return mock_resp_api
        return mock_resp_page

    with patch.object(client, "_create_opener") as mock_create_opener:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_create_opener.return_value = mock_opener
        client._opener = mock_opener

        status, items, err, code = await client.fetch_reviews(source)

        assert status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
        assert len(items) == 1
        assert items[0].external_review_id == "rev-fresh-1"
        assert client._csrf_token == "fresh-csrf"
        assert client._session_id == "fresh-session"
        assert len(calls) == 3  # 1st fetchReviews (failed), page visit, 2nd fetchReviews (success)


@pytest.mark.asyncio
async def test_yandex_403_stops_after_max_one_retry():
    client = YandexReviewsClient()
    source = ReviewSource(
        id=1,
        platform=ReviewPlatform.YANDEX,
        branch_name="Тест",
        external_id="12345",
        url="https://yandex.ru/maps/org/test/12345/",
    )
    client._csrf_token = "stale-csrf"
    client._session_id = "stale-session"
    client._base_origin = "https://yandex.ru"

    mock_resp_page = MagicMock()
    mock_resp_page.geturl.return_value = "https://yandex.ru/maps/org/test/12345/"
    mock_resp_page.read.return_value = (
        b'<html><script type="application/json">{"config":{"csrfToken":"fresh-csrf",'
        b'"counters":{"analytics":{"sessionId":"fresh-session"}}}}</script></html>'
    )
    mock_resp_page.__enter__.return_value = mock_resp_page
    mock_resp_page.__exit__.return_value = None

    def fake_open(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "fetchReviews" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return mock_resp_page

    with patch.object(client, "_create_opener") as mock_create_opener:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_create_opener.return_value = mock_opener
        client._opener = mock_opener

        status, items, err, code = await client.fetch_reviews(source)

        assert status == ReviewSyncStatus.SOURCE_BLOCKED
        assert code == 403
        assert len(items) == 0


# ==============================================================================
# 25. Pending review delivery retried on cycle without new reviews
# ==============================================================================
@pytest.mark.asyncio
async def test_pending_review_retried_next_cycle_without_new_reviews(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    rev = ReviewItem(
        external_review_id="pending-rev-100",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Pending Author",
        rating=5,
        text="Pending review text",
        published_at="2026-09-10T15:00:00Z",
    )
    await bundle.reviews.save_reviews([rev], source.id, mark_sent=False)

    unsent = await bundle.reviews.get_unsent_reviews()
    assert len(unsent) == 1

    # Mock clients find 0 new reviews
    test_env["yandex_client"].response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    test_env["yandex_client"].reviews_to_return = []
    test_env["dgis_client"].response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    test_env["dgis_client"].reviews_to_return = []

    # Cycle runs and must deliver pending reviews at start
    await service.sync_all_sources()

    unsent_after = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_after) == 0
    assert len(bot.sent_messages) == 1
    assert "Pending Author" in bot.sent_messages[0]["text"]


# ==============================================================================
# 26. Concurrent sync rejection (non-waiting lock)
# ==============================================================================
@pytest.mark.asyncio
async def test_concurrent_manual_sync_is_rejected_not_queued(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)

    sync_started = asyncio.Event()
    finish_sync = asyncio.Event()

    async def slow_fetch(source, page=1, page_size=10):
        sync_started.set()
        await finish_sync.wait()
        return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, [], None, 200

    test_env["yandex_client"].fetch_reviews = slow_fetch

    task1 = asyncio.create_task(service.sync_all_sources(force=True))
    await sync_started.wait()

    # Second sync must raise ReviewsSyncAlreadyRunningError immediately
    with pytest.raises(ReviewsSyncAlreadyRunningError):
        await service.sync_all_sources(force=True)

    finish_sync.set()
    await task1


# ==============================================================================
# 27. Strict rating validation: Never invent rating=5 (Yandex & 2GIS & DB)
# ==============================================================================
@pytest.mark.asyncio
async def test_yandex_missing_and_invalid_rating_never_becomes_five_stars():
    client = YandexReviewsClient()
    source = ReviewSource(
        id=1,
        platform=ReviewPlatform.YANDEX,
        branch_name="Branch",
        external_id="111",
        url="https://yandex.ru/maps/org/test/111/",
    )

    bad_payloads = [
        {
            "reviewId": "rev-1",
            "author": {"name": "U"},
            "text": "H",
            "updatedTime": "2026-09-10T12:00:00Z",
        },
        {
            "reviewId": "rev-2",
            "author": {"name": "U"},
            "text": "H",
            "rating": None,
            "updatedTime": "2026-09-10T12:00:00Z",
        },
        {
            "reviewId": "rev-3",
            "author": {"name": "U"},
            "text": "H",
            "rating": 0,
            "updatedTime": "2026-09-10T12:00:00Z",
        },
        {
            "reviewId": "rev-4",
            "author": {"name": "U"},
            "text": "H",
            "rating": 6,
            "updatedTime": "2026-09-10T12:00:00Z",
        },
        {
            "reviewId": "rev-5",
            "author": {"name": "U"},
            "text": "H",
            "rating": "five",
            "updatedTime": "2026-09-10T12:00:00Z",
        },
    ]

    for bad_item in bad_payloads:
        raw_json = json.dumps({"data": {"reviews": [bad_item]}}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_json
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        client._csrf_token = "c"
        client._session_id = "s"
        client._base_origin = "https://yandex.ru"
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        client._opener = mock_opener

        status, items, err, code = await client.fetch_reviews(source)
        assert status == ReviewSyncStatus.PARSER_FORMAT_CHANGED
        assert len(items) == 0
        assert "rating" in (err or "").lower()


@pytest.mark.asyncio
async def test_2gis_missing_and_invalid_rating_never_becomes_five_stars():
    client = DGisReviewsClient()
    source = ReviewSource(
        id=1,
        platform=ReviewPlatform.DGIS,
        branch_name="Branch",
        external_id="222",
        url="https://2gis.ru/spb/firm/222",
    )

    bad_payloads = [
        {"id": "rev-1", "user": {"name": "U"}, "text": "H", "date_created": "2026-09-10T12:00:00Z"},
        {
            "id": "rev-2",
            "user": {"name": "U"},
            "text": "H",
            "rating": None,
            "date_created": "2026-09-10T12:00:00Z",
        },
        {
            "id": "rev-3",
            "user": {"name": "U"},
            "text": "H",
            "rating": 0,
            "date_created": "2026-09-10T12:00:00Z",
        },
        {
            "id": "rev-4",
            "user": {"name": "U"},
            "text": "H",
            "rating": 6,
            "date_created": "2026-09-10T12:00:00Z",
        },
        {
            "id": "rev-5",
            "user": {"name": "U"},
            "text": "H",
            "rating": "invalid",
            "date_created": "2026-09-10T12:00:00Z",
        },
    ]

    for bad_item in bad_payloads:
        data = {"reviews": [bad_item], "meta": {"branch_rating": 4.5, "total_count": 1}}
        mock_session = MagicMock()
        mock_session.closed = False
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=data)
        mock_session.get.return_value.__aenter__.return_value = mock_resp
        mock_session.get.return_value.__aexit__.return_value = None
        client._session = mock_session

        status, items, err, code, rating, total = await client.fetch_reviews(source)
        assert status == ReviewSyncStatus.PARSER_FORMAT_CHANGED
        assert len(items) == 0
        assert "rating" in (err or "").lower()


@pytest.mark.asyncio
async def test_db_row_to_review_never_invents_five_stars(test_env):
    bundle = test_env["bundle"]

    row_none = {
        "id": 1,
        "source_id": 1,
        "external_review_id": "r1",
        "platform": "yandex",
        "branch_name": "B",
        "author_name": "A",
        "rating": None,
        "text": "T",
        "published_at": "2026-09-10T12:00:00Z",
        "edited_at": None,
        "review_url": None,
        "is_sent_to_telegram": 0,
    }
    rev_none = bundle.reviews._row_to_review(row_none)
    assert rev_none.rating == 0
    assert rev_none.rating != 5

    row_zero = dict(row_none, rating=0)
    rev_zero = bundle.reviews._row_to_review(row_zero)
    assert rev_zero.rating == 0
    assert rev_zero.rating != 5


# ==============================================================================
# 28. Catch-up with mixed known and new reviews on pages
# ==============================================================================
@pytest.mark.asyncio
async def test_catchup_mixed_known_and_new_pages_does_not_miss_reviews(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    await bundle.reviews.mark_source_initialized(source.id)
    await bundle.reviews.update_source_status(
        source.id,
        status="SUCCESS",
        checked_at=datetime.now(UTC).isoformat(),
        success=True,
        total_reviews_count=9,
        last_rating=4.9,
    )
    known_reviews = [
        ReviewItem(
            external_review_id=f"known-{i}",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name=f"Known {i}",
            rating=5,
            text=f"Known text {i}",
            published_at=f"2026-09-0{i + 1}T10:00:00Z",
        )
        for i in range(1, 10)
    ]
    await bundle.reviews.save_reviews(known_reviews, source.id, mark_sent=True)

    # Page 1: 1 new review ("new-p1-1") and 9 known reviews ("known-1" to "known-9")
    p1_items = [
        ReviewItem(
            external_review_id="new-p1-1",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name="New Page 1",
            rating=5,
            text="New on page 1",
            published_at="2026-09-10T12:00:00Z",
        )
    ] + known_reviews

    # Page 2: 2 new reviews ("new-p2-1", "new-p2-2") and 8 known reviews
    p2_items = [
        ReviewItem(
            external_review_id="new-p2-1",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name="New Page 2 A",
            rating=4,
            text="New on page 2 A",
            published_at="2026-09-10T11:00:00Z",
        ),
        ReviewItem(
            external_review_id="new-p2-2",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name="New Page 2 B",
            rating=5,
            text="New on page 2 B",
            published_at="2026-09-10T10:00:00Z",
        ),
    ] + known_reviews[:8]

    # Page 3: 10 items, all known reviews -> catch-up stops
    p3_items = known_reviews + [known_reviews[0]]

    def mock_fetch(src, page=1, page_size=10):
        if page == 1:
            return ReviewSyncStatus.SUCCESS_NEW_REVIEWS, p1_items, None, 200
        elif page == 2:
            return ReviewSyncStatus.SUCCESS_NEW_REVIEWS, p2_items, None, 200
        else:
            return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, p3_items, None, 200

    test_env["yandex_client"].fetch_reviews = AsyncMock(side_effect=mock_fetch)

    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    result = await service.sync_source(source)

    assert len(result.new_reviews) == 3
    new_ids = {r.external_review_id for r in result.new_reviews}
    assert new_ids == {"new-p1-1", "new-p2-1", "new-p2-2"}

    assert (await bundle.reviews.is_review_known("yandex", "new-p1-1")) is True
    assert (await bundle.reviews.is_review_known("yandex", "new-p2-1")) is True
    assert (await bundle.reviews.is_review_known("yandex", "new-p2-2")) is True


# ==============================================================================
# 29. Reliable Health and Recovery Alert delivery confirmation
# ==============================================================================
@pytest.mark.asyncio
async def test_health_alert_send_failure_is_retried(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    test_env["yandex_client"].response_status = ReviewSyncStatus.NETWORK_ERROR
    test_env["yandex_client"].error_message = "Network timeout"
    test_env["yandex_client"].reviews_to_return = []

    # Consecutive error 1
    await service.sync_source(source)
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.consecutive_errors == 1
    assert s.health_alert_active == 0

    # Consecutive error 2
    s.backoff_until = None
    await service.sync_source(s)
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.consecutive_errors == 2
    assert s.health_alert_active == 0

    # Consecutive error 3 -> threshold reached, but bot fails
    bot.fail_all = True
    s.backoff_until = None
    await service.sync_source(s)
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.consecutive_errors == 3
    assert s.health_alert_active == 0  # Delivery failed, remains 0!

    # Consecutive error 4 -> bot recovers, health alert delivered
    bot.fail_all = False
    s.backoff_until = None
    await service.sync_source(s)
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.consecutive_errors == 4
    assert s.health_alert_active == 1
    assert any(
        "🔴 <b>Reviews Monitor: проблема источника</b>" in m["text"] for m in bot.sent_messages
    )

    # Consecutive error 5 -> alert already active, no duplicate
    sent_count_before = len(bot.sent_messages)
    s.backoff_until = None
    await service.sync_source(s)
    assert len(bot.sent_messages) == sent_count_before


@pytest.mark.asyncio
async def test_recovery_alert_send_failure_does_not_lose_state(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    await bundle.reviews.update_source_status(
        source.id,
        status=ReviewSyncStatus.NETWORK_ERROR.value,
        checked_at=datetime.now(UTC).isoformat(),
        success=False,
        error="Network timeout",
        consecutive_errors=3,
        health_alert_active=1,
    )
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.health_alert_active == 1

    # Recovery cycle 1: sync succeeds, but bot fails on recovery alert
    test_env["yandex_client"].response_status = ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    test_env["yandex_client"].reviews_to_return = []
    bot.fail_all = True

    s.backoff_until = None
    await service.sync_source(s)
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.health_alert_active == 1  # Still 1 because recovery alert delivery failed!

    # Recovery cycle 2: sync succeeds, bot succeeds
    bot.fail_all = False
    s.backoff_until = None
    await service.sync_source(s)
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.health_alert_active == 0  # Now reset to 0!
    assert any(
        "🟢 <b>Reviews Monitor: источник восстановлен</b>" in m["text"] for m in bot.sent_messages
    )


# ==============================================================================
# 30. Scheduler-level crash alert and recovery
# ==============================================================================
@pytest.mark.asyncio
async def test_reviews_scheduler_crash_alert_and_recovery(test_env):
    service = test_env["service"]
    bot = test_env["bot"]

    scheduler = ReviewsPollingScheduler(
        settings=test_env["settings"],
        service=service,
    )

    crash_count = 0

    async def crash_sync(force=False):
        nonlocal crash_count
        crash_count += 1
        raise RuntimeError(f"Simulated unhandled scheduler crash {crash_count}")

    service.sync_all_sources = crash_sync

    # Cycle 1: crash 1 -> no alert
    await scheduler._poll_cycle(wait_after=False)
    assert scheduler.consecutive_crashes == 1
    assert scheduler.scheduler_health_alert_active is False
    assert len(bot.sent_messages) == 0

    # Cycle 2: crash 2 -> no alert
    await scheduler._poll_cycle(wait_after=False)
    assert scheduler.consecutive_crashes == 2
    assert scheduler.scheduler_health_alert_active is False
    assert len(bot.sent_messages) == 0

    # Cycle 3: crash 3 -> sends scheduler health alert
    await scheduler._poll_cycle(wait_after=False)
    assert scheduler.consecutive_crashes == 3
    assert scheduler.scheduler_health_alert_active is True
    assert len(bot.sent_messages) == 1
    assert "🔴 <b>Reviews Monitor: сбой планировщика</b>" in bot.sent_messages[0]["text"]

    # Cycle 4: crash 4 -> no duplicate alert
    await scheduler._poll_cycle(wait_after=False)
    assert scheduler.consecutive_crashes == 4
    assert len(bot.sent_messages) == 1

    # Cycle 5: success -> sends recovery alert
    async def ok_sync(force=False):
        return []

    service.sync_all_sources = ok_sync

    await scheduler._poll_cycle(wait_after=False)
    assert scheduler.consecutive_crashes == 0
    assert scheduler.scheduler_health_alert_active is False
    assert len(bot.sent_messages) == 2
    assert "🟢 <b>Reviews Monitor: планировщик восстановлен</b>" in bot.sent_messages[1]["text"]


# ==============================================================================
# 31. Multi-target partial delivery tracking (no duplicate resends)
# ==============================================================================
@pytest.mark.asyncio
async def test_multi_target_partial_failure_idempotent_retry(tmp_path: Path):
    db_file = tmp_path / "test_multi_target.sqlite3"
    database = Database(db_file)
    await database.connect()
    await init_schema(database)

    bundle = RepositoryBundle(database)
    settings = Settings(
        bot_token="123456:dummy_test_token",
        admin_ids_text="111,222",
        enable_reviews_monitor=True,
        alerts_reviews_enabled=True,
    )

    sent_by_target: dict[int, list[str]] = {111: [], 222: []}
    fail_target_222_part_2 = True

    class MultiTargetBot:
        async def send_message(self, chat_id: int, text: str, **kwargs):
            if chat_id == 222 and "[Часть 2/" in text and fail_target_222_part_2:
                raise TelegramAPIError(method=MagicMock(), message="Failed part 2 for target 222")
            sent_by_target[chat_id].append(text)
            msg = MagicMock()
            msg.message_id = len(sent_by_target[chat_id])
            return msg

    bot = MultiTargetBot()
    alert_service = AlertService(
        bot=bot,  # type: ignore
        settings=settings,
        alerts=bundle.alerts,
        runtime_settings=bundle.runtime_settings,
    )

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    long_text = "А" * 5000
    rev = ReviewItem(
        external_review_id="rev-multi-1",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Multi Target Author",
        rating=5,
        text=long_text,
        published_at="2026-09-10T12:00:00Z",
    )
    await bundle.reviews.save_reviews([rev], source.id, mark_sent=False)

    unsent = await bundle.reviews.get_unsent_reviews()
    assert len(unsent) == 1

    # Attempt 1: target 111 gets parts 1 & 2; target 222 gets part 1, fails part 2
    with pytest.raises(TelegramAPIError):
        await alert_service.send_review_alert(unsent[0], source, bundle.reviews)

    assert len(sent_by_target[111]) == 2
    assert len(sent_by_target[222]) == 1

    unsent_after_attempt1 = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_after_attempt1) == 1
    assert unsent_after_attempt1[0].is_sent_to_telegram is False
    assert unsent_after_attempt1[0].raw_payload_json is not None
    payload_data = json.loads(unsent_after_attempt1[0].raw_payload_json)
    assert payload_data["targets"] == {"111": 2, "222": 1}

    # Attempt 2: Target 222 network recovers
    fail_target_222_part_2 = False
    ok2 = await alert_service.send_review_alert(unsent_after_attempt1[0], source, bundle.reviews)
    assert ok2 is True

    # Target 111 must receive NO new messages!
    assert len(sent_by_target[111]) == 2
    # Target 222 must receive ONLY part 2!
    assert len(sent_by_target[222]) == 2
    assert "[Часть 2/" in sent_by_target[222][1]

    unsent_final = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_final) == 0
    recent = await bundle.reviews.get_recent_reviews(1)
    final_payload = json.loads(recent[0].raw_payload_json)
    assert final_payload["targets"] == {"111": 2, "222": 2}

    await database.close()


# ==============================================================================
# 32. Safe Multipart HTML Escaping and Character Boundaries
# ==============================================================================
def test_safe_multipart_html_escaping_and_boundaries(test_env):
    alert_service = test_env["alert_service"]
    source = ReviewSource(
        id=1,
        platform=ReviewPlatform.YANDEX,
        branch_name="Branch",
        external_id="111",
        url="https://yandex.ru/maps/org/test/111/",
    )
    special_chunk = "Text with <special & symbols> and \"quotes\" and 'apostrophe'. "
    long_raw_text = special_chunk * 100
    rev = ReviewItem(
        external_review_id="rev-html-1",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="<Author & Co>",
        rating=5,
        text=long_raw_text,
        published_at="2026-09-10T12:00:00Z",
    )

    parts = alert_service._render_review_parts(rev, source)
    assert len(parts) >= 2

    for part in parts:
        assert len(part) <= 4000
        assert part.count("<b>") == part.count("</b>")
        assert part.count("<i>") == part.count("</i>")
        stripped = (
            part.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
        )
        assert "<" not in stripped
        assert ">" not in stripped
        assert "&am " not in part
        assert "&qu " not in part

    long_unbroken = "X" * 10000
    chunks = alert_service._split_raw_text(long_unbroken, max_escaped_len=3000)
    assert "".join(chunks) == long_unbroken
    for chunk in chunks:
        assert len(chunk) <= 3000


# ==============================================================================
# 33. Baseline Multi-Page Pagination: Old reviews never alerted after new review
# ==============================================================================
@pytest.mark.asyncio
async def test_baseline_multiple_pages_old_reviews_never_alerted_after_first_new_review(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert source.is_initialized == 0

    # Historical reviews: 25 total reviews across 3 pages (page size 10)
    historical_p1 = [
        ReviewItem(
            external_review_id=f"hist-p1-{i}",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name=f"Hist Author P1 {i}",
            rating=5,
            text=f"Historical text P1 {i}",
            published_at=f"2026-08-01T10:{i:02d}:00Z",
        )
        for i in range(10)
    ]
    historical_p2 = [
        ReviewItem(
            external_review_id=f"hist-p2-{i}",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name=f"Hist Author P2 {i}",
            rating=4,
            text=f"Historical text P2 {i}",
            published_at=f"2026-07-01T10:{i:02d}:00Z",
        )
        for i in range(10)
    ]
    historical_p3 = [
        ReviewItem(
            external_review_id=f"hist-p3-{i}",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name=f"Hist Author P3 {i}",
            rating=5,
            text=f"Historical text P3 {i}",
            published_at=f"2026-06-01T10:{i:02d}:00Z",
        )
        for i in range(5)
    ]

    def baseline_fetch(src, page=1, page_size=10):
        if page == 1:
            return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, historical_p1, None, 200
        elif page == 2:
            return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, historical_p2, None, 200
        elif page == 3:
            return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, historical_p3, None, 200
        return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, [], None, 200

    test_env["yandex_client"].fetch_reviews = AsyncMock(side_effect=baseline_fetch)

    # 1. Cold start baseline initialization
    res = await service.sync_source(source)
    assert res.status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    assert len(res.fetched_reviews) == 25
    assert len(res.new_reviews) == 0

    # Source must be initialized
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.is_initialized == 1

    # Zero unsent reviews in DB
    unsent = await bundle.reviews.get_unsent_reviews()
    assert len(unsent) == 0
    delivered = await service.deliver_pending_reviews()
    assert delivered == 0
    assert len(bot.sent_messages) == 0

    # 2. Next cycle: A brand new review arrives on page 1!
    new_rev = ReviewItem(
        external_review_id="brand-new-rev-1",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Brand New Author",
        rating=5,
        text="Brand new review just posted!",
        published_at="2026-09-10T15:00:00Z",
    )
    cycle2_p1 = [new_rev] + historical_p1[:9]
    cycle2_p2 = [historical_p1[9]] + historical_p2[:9]
    cycle2_p3 = [historical_p2[9]] + historical_p3

    def cycle2_fetch(src, page=1, page_size=10):
        if page == 1:
            return ReviewSyncStatus.SUCCESS_NEW_REVIEWS, cycle2_p1, None, 200
        elif page == 2:
            return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, cycle2_p2, None, 200
        elif page == 3:
            return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, cycle2_p3, None, 200
        return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, [], None, 200

    test_env["yandex_client"].fetch_reviews = AsyncMock(side_effect=cycle2_fetch)

    # Sync source on cycle 2 (catch-up will traverse page 2 and page 3)
    res2 = await service.sync_source(s)
    assert res2.status == ReviewSyncStatus.SUCCESS_NEW_REVIEWS
    # MUST contain ONLY the 1 real new review!
    assert len(res2.new_reviews) == 1
    assert res2.new_reviews[0].external_review_id == "brand-new-rev-1"

    # Deliver pending reviews -> ONLY 1 alert sent for the new review!
    delivered = await service.deliver_pending_reviews()
    assert delivered == 1
    msg_text = bot.sent_messages[0]["text"]
    assert "brand-new-rev-1" in msg_text or "Brand new review" in msg_text


# ==============================================================================
# 34. Catch-up page 2 failure does not permanently lose unseen reviews
# ==============================================================================
@pytest.mark.asyncio
async def test_catchup_page2_failure_does_not_permanently_lose_unseen_reviews(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    await bundle.reviews.mark_source_initialized(source.id)
    await bundle.reviews.update_source_status(
        source.id,
        status="SUCCESS",
        checked_at=datetime.now(UTC).isoformat(),
        success=True,
    )

    known_reviews = [
        ReviewItem(
            external_review_id=f"known-{i}",
            platform=ReviewPlatform.YANDEX,
            branch_name=source.branch_name,
            author_name=f"Author {i}",
            rating=5,
            text=f"Text {i}",
            published_at=f"2026-09-0{i}T10:00:00Z",
        )
        for i in range(1, 10)
    ]
    await bundle.reviews.save_reviews(known_reviews, source.id, mark_sent=True)

    # New reviews: 2 on page 1, 2 on page 2
    new_p1_a = ReviewItem(
        external_review_id="new-p1-a",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="P1 A",
        rating=5,
        text="New p1 a",
        published_at="2026-09-10T14:00:00Z",
    )
    new_p1_b = ReviewItem(
        external_review_id="new-p1-b",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="P1 B",
        rating=5,
        text="New p1 b",
        published_at="2026-09-10T13:00:00Z",
    )
    page1_items = [new_p1_a, new_p1_b] + known_reviews[:8]

    new_p2_a = ReviewItem(
        external_review_id="new-p2-a",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="P2 A",
        rating=5,
        text="New p2 a",
        published_at="2026-09-10T12:00:00Z",
    )
    new_p2_b = ReviewItem(
        external_review_id="new-p2-b",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="P2 B",
        rating=5,
        text="New p2 b",
        published_at="2026-09-10T11:00:00Z",
    )
    page2_items = [new_p2_a, new_p2_b, known_reviews[8]]

    # Cycle 1: Page 1 succeeds, but Page 2 fails with RATE_LIMITED
    def fetch_cycle1(src, page=1, page_size=10):
        if page == 1:
            return ReviewSyncStatus.SUCCESS_NEW_REVIEWS, page1_items, None, 200
        else:
            return ReviewSyncStatus.RATE_LIMITED, [], "HTTP 429 Too Many Requests", 429

    test_env["yandex_client"].fetch_reviews = AsyncMock(side_effect=fetch_cycle1)

    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    res1 = await service.sync_source(s)

    # Must return error status, NOT full success!
    assert res1.status == ReviewSyncStatus.RATE_LIMITED
    assert res1.error_message == "HTTP 429 Too Many Requests"
    assert len(res1.new_reviews) == 0

    # CRITICAL: Page 1 new reviews must NOT be persisted as known yet!
    assert (await bundle.reviews.is_review_known("yandex", "new-p1-a")) is False
    assert (await bundle.reviews.is_review_known("yandex", "new-p1-b")) is False

    # Cycle 2: After backoff, retry where both page 1 and page 2 succeed!
    def fetch_cycle2(src, page=1, page_size=10):
        if page == 1:
            return ReviewSyncStatus.SUCCESS_NEW_REVIEWS, page1_items, None, 200
        elif page == 2:
            return ReviewSyncStatus.SUCCESS_NEW_REVIEWS, page2_items, None, 200
        return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, [], None, 200

    test_env["yandex_client"].fetch_reviews = AsyncMock(side_effect=fetch_cycle2)

    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    s.backoff_until = None
    res2 = await service.sync_source(s)

    # Both page 1 (2 new) and page 2 (2 new) -> all 4 new reviews are detected!
    assert res2.status == ReviewSyncStatus.SUCCESS_NEW_REVIEWS
    assert len(res2.new_reviews) == 4
    found_ids = {r.external_review_id for r in res2.new_reviews}
    assert found_ids == {"new-p1-a", "new-p1-b", "new-p2-a", "new-p2-b"}

    # Deliver all 4 pending reviews without any lost
    delivered = await service.deliver_pending_reviews()
    assert delivered == 4
    assert len(bot.sent_messages) == 4


# ==============================================================================
# 35. Multipart Progress Survives Process Restart Between Parts
# ==============================================================================
@pytest.mark.asyncio
async def test_multipart_progress_survives_process_restart_between_parts(test_env):
    bundle = test_env["bundle"]
    settings = test_env["settings"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    long_text = (
        ("This is part one. " * 70)
        + "\n\n"
        + ("This is part two. " * 70)
        + "\n\n"
        + ("This is part three. " * 70)
    )
    rev = ReviewItem(
        external_review_id="rev-restart-1",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Author Restart",
        rating=5,
        text=long_text,
        published_at="2026-09-10T12:00:00Z",
    )

    # Save unsent review to DB
    await bundle.reviews.save_reviews([rev], source.id, mark_sent=False)
    unsent = await bundle.reviews.get_unsent_reviews()
    assert len(unsent) == 1
    db_rev = unsent[0]

    alert_service = test_env["alert_service"]
    parts = alert_service._render_review_parts(db_rev, source)
    assert len(parts) >= 2

    # Simulate failure on part 2
    bot.should_fail_on_part = 2

    # First attempt: sends part 1, fails on part 2
    with pytest.raises(TelegramAPIError):
        await alert_service.send_review_alert(db_rev, source, bundle.reviews)

    # Check SQLite state immediately after part 1
    recent = await bundle.reviews.get_recent_reviews(1)
    rev_after_p1 = recent[0]
    payload = json.loads(rev_after_p1.raw_payload_json)
    target_id = str(list(test_env["settings"].admin_ids)[0])
    # Progress for target must be 1 (part 1 completed)
    assert payload["targets"][target_id] == 1
    assert rev_after_p1.telegram_parts_sent == 1
    assert rev_after_p1.is_sent_to_telegram == 0

    # SIMULATE PROCESS RESTART:
    new_bot = DummyBot()
    new_alert_service = AlertService(
        bot=new_bot,  # type: ignore
        settings=settings,
        alerts=bundle.alerts,
        runtime_settings=bundle.runtime_settings,
    )

    # Re-read pending review from DB
    unsent_restarted = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_restarted) == 1
    restarted_rev = unsent_restarted[0]
    assert restarted_rev.telegram_parts_sent == 1

    # Second attempt: bot no longer fails
    ok = await new_alert_service.send_review_alert(restarted_rev, source, bundle.reviews)
    assert ok is True

    # Check messages sent by new_bot: part 1 must NOT be sent again!
    assert len(new_bot.sent_messages) == len(parts) - 1
    assert not any("[Часть 1/" in m["text"] for m in new_bot.sent_messages)
    assert any(f"[Часть {len(parts)}/" in m["text"] for m in new_bot.sent_messages)

    # Review in DB is now fully sent
    final_unsent = await bundle.reviews.get_unsent_reviews()
    assert len(final_unsent) == 0
    recent_final = await bundle.reviews.get_recent_reviews(1)
    assert recent_final[0].is_sent_to_telegram == 1


# ==============================================================================
# 36. Zero alert targets keeps review pending
# ==============================================================================
@pytest.mark.asyncio
async def test_zero_alert_targets_keeps_review_pending(test_env):
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    # Configure Settings with 0 alert targets
    settings_no_targets = Settings(
        bot_token="123456:dummy_test_token",
        admin_ids_text="",
        alert_chat_id=None,
        enable_reviews_monitor=True,
    )
    assert settings_no_targets.alert_chat_id is None
    assert len(settings_no_targets.admin_ids) == 0

    alert_service = AlertService(
        bot=bot,  # type: ignore
        settings=settings_no_targets,
        alerts=bundle.alerts,
        runtime_settings=bundle.runtime_settings,
    )
    assert alert_service._alert_targets() == []

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    rev = ReviewItem(
        external_review_id="rev-zero-targets-1",
        platform=ReviewPlatform.YANDEX,
        branch_name=source.branch_name,
        author_name="Author Zero",
        rating=5,
        text="Review with no recipients configured",
        published_at="2026-09-10T12:00:00Z",
    )
    await bundle.reviews.save_reviews([rev], source.id, mark_sent=False)
    unsent = await bundle.reviews.get_unsent_reviews()
    assert len(unsent) == 1
    db_rev = unsent[0]

    # Attempt to send review alert
    ok = await alert_service.send_review_alert(db_rev, source, bundle.reviews)
    assert ok is False

    # Review must remain pending (is_sent_to_telegram = 0)
    unsent_after = await bundle.reviews.get_unsent_reviews()
    assert len(unsent_after) == 1
    recent = await bundle.reviews.get_recent_reviews(1)
    assert recent[0].is_sent_to_telegram == 0
    assert recent[0].last_delivery_error == "No alert targets configured"

    # Must NOT write audit alert to alerts table
    conn = test_env["db"].require_connection()
    async with conn.execute(
        "SELECT COUNT(*) AS c FROM alerts WHERE item_id = 'rev-zero-targets-1'"
    ) as cur:
        count = int((await cur.fetchone())["c"])
    assert count == 0


# ==============================================================================
# 37. Scheduler never completed first cycle becomes stale
# ==============================================================================
@pytest.mark.asyncio
async def test_scheduler_never_completed_first_cycle_becomes_stale(test_env):
    service = test_env["service"]
    settings = test_env["settings"]

    scheduler = ReviewsPollingScheduler(
        settings=settings,
        service=service,
    )
    assert scheduler.last_completed_cycle_at is None

    # Case 1: Just started -> uptime is 0 < 2 * poll_interval_seconds -> HEALTHY
    status1 = await scheduler.get_status()
    assert status1["health_status"] == "HEALTHY"

    # Case 2: Scheduler uptime exceeds startup threshold (2 * poll_interval_seconds)
    # without ever completing first cycle
    config = await service.effective_config()
    stale_started_at = datetime.now(UTC) - timedelta(
        seconds=2 * config.poll_interval_seconds + 10
    )
    scheduler._started_at = stale_started_at

    status2 = await scheduler.get_status()
    assert status2["health_status"] == "DEGRADED_STALE"


# ==============================================================================
# 38. Source error message preserved in DB
# ==============================================================================
@pytest.mark.asyncio
async def test_source_error_message_preserved_in_db(test_env):
    service = test_env["service"]
    bundle = test_env["bundle"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    specific_error = "403 Forbidden: Yandex Bot Detection Challenge"
    test_env["yandex_client"].response_status = ReviewSyncStatus.SOURCE_BLOCKED
    test_env["yandex_client"].error_message = specific_error
    test_env["yandex_client"].http_code = 403
    test_env["yandex_client"].reviews_to_return = []

    res = await service.sync_source(source)
    assert res.status == ReviewSyncStatus.SOURCE_BLOCKED
    assert res.error_message == specific_error

    # Verify SQLite row in review_sources table
    s = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    assert s.last_error == specific_error
    assert s.last_status == ReviewSyncStatus.SOURCE_BLOCKED.value
    assert s.last_error != "Unknown error"


# ==============================================================================
# 39. Manual sync while disabled makes zero HTTP requests
# ==============================================================================
@pytest.mark.asyncio
async def test_manual_sync_while_disabled_makes_zero_http_requests(test_env):
    from app.bot.callbacks import reviews_sync_callback
    from app.bot.handlers import reviews_sync_command

    service = test_env["service"]
    bundle = test_env["bundle"]

    # Disable Reviews Monitor in runtime settings
    await bundle.runtime_settings.set("enable_reviews_monitor", "false")
    config = await service.effective_config()
    assert config.enabled is False

    # Reset mock call counters
    test_env["yandex_client"].calls = 0
    test_env["dgis_client"].calls = 0

    # 1. Test /reviews_sync command handler
    mock_message = AsyncMock()
    mock_message.answer = AsyncMock()

    await reviews_sync_command(mock_message, reviews_service=service)

    mock_message.answer.assert_called_once_with(
        "Reviews Monitor выключен. Сначала включите мониторинг."
    )
    assert test_env["yandex_client"].calls == 0
    assert test_env["dgis_client"].calls == 0

    # 2. Test reviews:sync callback handler
    mock_query = AsyncMock()
    mock_query.answer = AsyncMock()
    mock_query.message = AsyncMock()
    mock_query.message.answer = AsyncMock()

    await reviews_sync_callback(mock_query, reviews_service=service)

    mock_query.answer.assert_called_once_with(
        "Reviews Monitor выключен. Сначала включите мониторинг.", show_alert=True
    )
    mock_query.message.answer.assert_called_once_with(
        "Reviews Monitor выключен. Сначала включите мониторинг."
    )
    assert test_env["yandex_client"].calls == 0
    assert test_env["dgis_client"].calls == 0


# ==============================================================================
# 40. ModuleRegistry status reflects UNHEALTHY_CRASHING and degraded sources
# ==============================================================================
@pytest.mark.asyncio
async def test_module_registry_reviews_status_crashing_and_degraded(test_env):
    from app.modules import ModuleRegistry, ModuleStatus

    service = test_env["service"]
    bundle = test_env["bundle"]
    settings = test_env["settings"]

    scheduler = ReviewsPollingScheduler(
        settings=settings,
        service=service,
    )

    registry = ModuleRegistry(
        settings=settings,
        runtime_settings=bundle.runtime_settings,
        reviews_service=service,
        reviews_scheduler=scheduler,
    )

    # 1. Baseline: healthy state -> ModuleStatus.OK
    info = await registry.reviews_info()
    assert info.status == ModuleStatus.OK
    assert info.is_available is True

    # 2. Scheduler UNHEALTHY_CRASHING -> ModuleStatus.ERROR
    scheduler._consecutive_loop_crashes = 3
    scheduler._scheduler_health_alert_active = True
    info_crashing = await registry.reviews_info()
    assert info_crashing.status == ModuleStatus.ERROR
    assert "UNHEALTHY_CRASHING" in info_crashing.reason
    assert info_crashing.is_available is False

    # Reset scheduler to healthy
    scheduler._consecutive_loop_crashes = 0
    scheduler._scheduler_health_alert_active = False

    # 3. Degraded source (health_alert_active=1) -> ModuleStatus.ERROR, not healthy/OK
    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")
    await bundle.reviews.update_source_status(
        source.id,
        status="NETWORK_ERROR",
        checked_at=datetime.now(UTC).isoformat(),
        success=False,
        health_alert_active=1,
    )

    info_degraded = await registry.reviews_info()
    assert info_degraded.status == ModuleStatus.ERROR
    assert info_degraded.status != ModuleStatus.OK
    assert info_degraded.is_available is False
    assert "Sources degraded: 1" in info_degraded.reason
