# Reuse the existing integration fixture; test parameters deliberately share its name.
# ruff: noqa: F811
import json
import time
import urllib.error
from dataclasses import replace
from datetime import datetime

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.models import (
    DEFAULT_SOURCES,
    ReviewItem,
    ReviewPlatform,
    ReviewSource,
    ReviewSyncStatus,
)
from tests.test_reviews import test_env  # noqa: F401


def response(payload):
    result = MagicMock()
    result.read.return_value = json.dumps(payload).encode()
    result.__enter__.return_value = result
    return result


def client_and_source():
    client = YandexReviewsClient()
    client._csrf_token = "test"
    client._session_id = "test"
    client._opener = MagicMock()
    source = ReviewSource(id=1, **DEFAULT_SOURCES[0])
    client._ratings_updated_at[source.external_id] = time.time()
    return client, source


def test_stale_unsorted_html_must_not_report_success():
    client, source = client_and_source()
    # An old unordered HTML snapshot cannot substitute a fresh by_time API page.
    client._cached_html_reviews = {
        source.external_id: [
            {"reviewId": "old", "rating": 5, "updatedTime": "2026-01-01T00:00:00Z"}
        ]
    }
    client._opener.open.return_value = response({"error": {"code": 500}})
    with patch("app.reviews.client_yandex.time.sleep"):
        status, items, _, code, *_ = client._sync_fetch(source)
    assert status == ReviewSyncStatus.HTTP_ERROR
    assert items == []
    assert code == 500
    assert client._opener.open.call_count == 4


@pytest.mark.parametrize(
    "code,expected",
    [
        (429, ReviewSyncStatus.RATE_LIMITED),
        (403, ReviewSyncStatus.SOURCE_BLOCKED),
        (400, ReviewSyncStatus.HTTP_ERROR),
    ],
)
def test_json_nontransient_error_is_not_retried(code, expected):
    client, source = client_and_source()
    client._opener.open.return_value = response({"error": {"code": str(code)}})
    with (
        patch("app.reviews.client_yandex.time.sleep") as sleep,
        patch.object(
            client,
            "_initialize_session",
            return_value=(ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, None, 200),
        ),
    ):
        status, _, _, result_code, *_ = client._sync_fetch(source)
    assert status == expected
    assert result_code == code
    assert client._opener.open.call_count == (2 if code == 403 else 1)
    sleep.assert_not_called()


@pytest.mark.parametrize("transport", [False, True])
def test_auth_refresh_after_transient_error(transport):
    client, source = client_and_source()
    expired = (
        urllib.error.HTTPError(source.url, 403, "expired", {}, None)
        if transport
        else response({"csrfToken": "expired"})
    )
    client._opener.open.side_effect = [
        response({"error": {"code": 503}}),
        expired,
        response({"data": {"reviews": []}}),
    ]
    with (
        patch("app.reviews.client_yandex.time.sleep"),
        patch.object(
            client,
            "_initialize_session",
            return_value=(ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, None, 200),
        ) as refresh,
    ):
        assert client._sync_fetch(source)[0] == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    refresh.assert_called_once()


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_http_retries_are_bounded(code):
    client, source = client_and_source()
    client._opener.open.side_effect = urllib.error.HTTPError(source.url, code, "upstream", {}, None)
    with patch("app.reviews.client_yandex.time.sleep") as sleep:
        status, items, _, result_code, *_ = client._sync_fetch(source)
    assert (status, items, result_code) == (ReviewSyncStatus.HTTP_ERROR, [], code)
    assert client._opener.open.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,external_id,client_key",
    [
        ("yandex", "1093602317", "yandex_client"),
        ("2gis", "4504127908536611", "dgis_client"),
    ],
)
async def test_catchup_limit_does_not_make_older_unseen_reviews_unreachable(
    test_env, platform, external_id, client_key
):
    service, repo = test_env["service"], test_env["bundle"].reviews
    await repo.ensure_default_sources(DEFAULT_SOURCES)
    source = await repo.get_source_by_external_id(platform, external_id)
    await repo.mark_source_initialized(source.id)
    source = await repo.get_source_by_id(source.id)
    baseline = ReviewItem(
        "baseline", source.platform, source.branch_name, "A", 5, "Old", "2026-01-01T00:00:00Z"
    )
    await repo.save_reviews([baseline], source.id, mark_sent=True)
    stream = [
        replace(baseline, external_review_id=f"new-{i}", published_at="2026-10-11T00:00:00Z")
        for i in range(60)
    ] + [baseline]

    async def fetch(src, page=1, page_size=10):
        return (
            ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
            stream[(page - 1) * page_size : page * page_size],
            None,
            200,
            5.0,
            len(stream),
        )

    test_env[client_key].fetch_reviews = AsyncMock(side_effect=fetch)
    result = await service.sync_source(source)
    assert result.is_error
    assert "REVIEWS_MAX_CATCHUP_REVIEWS" in result.error_message
    assert not await repo.is_review_known(source.platform, "new-0")
    # Explicitly raising the budget and retrying recovers the entire gap.
    service.settings.reviews_max_catchup_reviews = 100
    source = await repo.get_source_by_id(source.id)
    source.backoff_until = None
    result = await service.sync_source(source)
    assert len(result.new_reviews) == 60


@pytest.mark.asyncio
async def test_no_recovery_alert_until_catchup_succeeds(test_env):
    service, repo = test_env["service"], test_env["bundle"].reviews
    await repo.ensure_default_sources(DEFAULT_SOURCES)
    source = await repo.get_source_by_external_id("yandex", "1093602317")
    source.is_initialized = True
    source.health_alert_active = 1
    service.alerts.send_review_recovery_alert = AsyncMock(return_value=True)
    items = [
        ReviewItem(
            str(i), source.platform, source.branch_name, "A", 5, "new", "2026-10-11T00:00:00Z"
        )
        for i in range(10)
    ]
    test_env["yandex_client"].fetch_reviews = AsyncMock(
        side_effect=[
            (ReviewSyncStatus.SUCCESS_NEW_REVIEWS, items, None, 200),
            (ReviewSyncStatus.HTTP_ERROR, [], "upstream", 500),
        ]
    )
    assert (await service.sync_source(source)).is_error
    service.alerts.send_review_recovery_alert.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rating,verdict,silent", [(5, "GREEN", True), (4, "GREEN", False), (5, "YELLOW", False)]
)
async def test_real_delivery_preserves_silent_policy(test_env, rating, verdict, silent):
    service, repo = test_env["service"], test_env["bundle"].reviews
    await repo.ensure_default_sources(DEFAULT_SOURCES)
    source = await repo.get_source_by_external_id("yandex", "1093602317")
    review = ReviewItem(
        "silent-test",
        source.platform,
        source.branch_name,
        "A",
        rating,
        "Review",
        "2026-10-11T00:00:00Z",
    )
    await repo.save_reviews([review], source.id, mark_sent=False)
    from app.storage.models import ReviewAiAnalysis

    await repo.save_ai_analysis(
        ReviewAiAnalysis(id=None, review_id=review.id, status="SUCCESS", verdict=verdict)
    )
    assert await service.deliver_pending_reviews() == 1
    assert test_env["bot"].sent_messages[-1]["disable_notification"] is silent


def test_2gis_page_metadata_parser():
    from app.reviews.client_2gis import DGisReviewsClient

    sample_html = (
        '{"reviews":{"general_rating":4.8,"general_review_count":53,'
        '"general_review_count_with_stars":69,"org_rating":4.9}}'
    )
    rating, count = DGisReviewsClient._parse_page_metadata(sample_html)
    assert rating == 4.8
    assert count == 69


def test_2gis_page_metadata_parser_fallback():
    from app.reviews.client_2gis import DGisReviewsClient

    html_fallback = '<meta property="og:description" content="Оценка 4.8, 16 фото, 53 отзыва." />'
    rating, count = DGisReviewsClient._parse_page_metadata(html_fallback)
    assert rating == 4.8
    assert count == 53


@pytest.mark.asyncio
async def test_2gis_prefers_page_rating_over_microservice_meta():
    from app.reviews.client_2gis import DGisReviewsClient

    client = DGisReviewsClient()
    source = ReviewSource(
        id=1,
        platform=ReviewPlatform.DGIS,
        branch_name="УО Датахаб",
        external_id="4504127908536611",
        url="https://2gis.ru/moscow/firm/4504127908536611/tab/reviews",
    )

    # Pre-populate page metadata with 4.8 / 69
    client._branch_ratings[source.external_id] = 4.8
    client._branch_counts[source.external_id] = 69
    client._ratings_updated_at[source.external_id] = time.time()

    # API returns 4.9 in meta
    api_data = {
        "reviews": [],
        "meta": {"branch_rating": 4.9, "total_count": 56},
    }

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=api_data)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.get.return_value.__aenter__.return_value = mock_resp
    client._session = mock_session

    status, items, err, code, rating, count = await client.fetch_reviews(source)
    assert status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    assert rating == 4.8
    assert count == 69


@pytest.mark.asyncio
async def test_2gis_cdn_cache_inconsistency_rejected():
    """
    When 2GIS CDN edge caches flap between stale and fresh (e.g. 4.9 on one node vs 5.0 on another),
    and API returns authoritative 5.0, the stale 4.9 must be discarded and 5.0 retained.
    """
    from app.reviews.client_2gis import DGisReviewsClient

    client = DGisReviewsClient()
    source = ReviewSource(
        id=12,
        platform=ReviewPlatform.DGIS,
        branch_name="УО Моссовет",
        external_id="4504127908875284",
        url="https://2gis.ru/moscow/firm/4504127908875284/tab/reviews",
        last_rating=5.0,
    )
    client._branch_ratings[source.external_id] = 5.0
    client._branch_counts[source.external_id] = 76

    # API returns 5.0
    api_resp = AsyncMock()
    api_resp.status = 200
    api_resp.json = AsyncMock(
        return_value={
            "reviews": [],
            "meta": {"branch_rating": 5.0, "total_count": 68},
        }
    )

    # Page fetch 1 returns stale 4.9 (from a stale CDN edge node)
    page_resp_1 = AsyncMock()
    page_resp_1.status = 200
    page_resp_1.text = AsyncMock(
        return_value='{"reviews":{"general_rating":4.9,"general_review_count_with_stars":76}}'
    )

    # Confirmation page fetch 2 returns fresh 5.0
    page_resp_2 = AsyncMock()
    page_resp_2.status = 200
    page_resp_2.text = AsyncMock(
        return_value='{"reviews":{"general_rating":5.0,"general_review_count_with_stars":76}}'
    )

    mock_session = MagicMock()
    mock_session.closed = False

    # Simulate get calls:
    # 1. API call
    # 2. Page fetch 1
    # 3. Confirmation page fetch 2
    mock_session.get.side_effect = [
        AsyncMock(__aenter__=AsyncMock(return_value=api_resp)),
        AsyncMock(__aenter__=AsyncMock(return_value=page_resp_1)),
        AsyncMock(__aenter__=AsyncMock(return_value=page_resp_2)),
    ]
    client._session = mock_session

    status, items, err, code, rating, count = await client.fetch_reviews(source)
    assert status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    # Stale 4.9 must NOT override 5.0!
    assert rating == 5.0
    assert count == 76


@pytest.mark.asyncio
async def test_service_anti_flapping_rating_alert(test_env):
    """
    Verify that if a rating oscillates rapidly (e.g. 5.0 -> 4.9 -> 5.0 within 1h),
    the reverse flapping alert is suppressed so users are not spammed.
    """
    service = test_env["service"]
    bundle = test_env["bundle"]
    bot = test_env["bot"]

    await bundle.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = await bundle.reviews.get_source_by_external_id("yandex", "1093602317")

    await bundle.reviews.mark_source_initialized(source.id)
    await bundle.reviews.update_source_status(
        source.id,
        status="SUCCESS_NO_NEW_REVIEWS",
        checked_at=datetime.now(UTC).isoformat(),
        success=True,
        last_rating=5.0,
        total_reviews_count=290,
    )
    s_curr = await bundle.reviews.get_source_by_id(source.id)

    # 1. First rating change: 5.0 -> 4.9 (Genuine alert should be sent)
    test_env["yandex_client"].fetch_reviews = AsyncMock(
        return_value=(ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, [], None, 200, 4.9, 290)
    )
    bot.sent_messages.clear()
    await service.sync_source(s_curr)
    assert len(bot.sent_messages) == 1
    assert "5.0 ➔ <b>4.9</b>" in bot.sent_messages[0]["text"]

    # 2. Flapping back within minutes: 4.9 -> 5.0 (Flapping alert must be suppressed!)
    s_curr = await bundle.reviews.get_source_by_id(source.id)
    assert s_curr.last_rating == 4.9

    test_env["yandex_client"].fetch_reviews = AsyncMock(
        return_value=(ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, [], None, 200, 5.0, 290)
    )
    bot.sent_messages.clear()
    await service.sync_source(s_curr)
    # The flapping alert is suppressed!
    assert len(bot.sent_messages) == 0
