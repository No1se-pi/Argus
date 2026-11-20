from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.reports.data import Period, read_snapshot
from app.reports.history import HistoricalReviewsLoader
from app.reviews.models import DEFAULT_SOURCES, ReviewItem, ReviewPlatform, ReviewSyncStatus
from app.reviews.validation import review_date, review_rating
from app.storage.database import Database
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema


@pytest.fixture
async def store(tmp_path):
    db = Database(tmp_path / "reviews.sqlite3")
    await db.connect()
    await init_schema(db)
    repos = RepositoryBundle(db)
    await repos.reviews.ensure_default_sources(DEFAULT_SOURCES)
    source = (await repos.reviews.list_sources())[0]
    yield db, repos, source
    await db.close()


def review(source, key, date):
    return ReviewItem(key, ReviewPlatform.YANDEX, source.branch_name, "Author", 5, "Text", date)


@pytest.mark.parametrize("value", [True, False, 4.9, 0, 6, None, "NaN"])
def test_invalid_rating_cannot_become_stars(value):
    with pytest.raises((ValueError, TypeError, OverflowError)):
        review_rating(value)


@pytest.mark.parametrize("value", ["2026-11-09", "2026-11-09T12:00:00", "yesterday", ""])
def test_report_dates_require_actual_timezone(value):
    with pytest.raises(ValueError):
        review_date(value)


@pytest.mark.asyncio
async def test_edited_review_refreshes_content_preserving_delivery(store):
    db, repos, source = store
    old = review(source, "edited", "2026-11-01T12:00:00Z")
    assert await repos.reviews.save_reviews([old], source.id, mark_sent=True) == 1
    new = replace(old, text="Edited", rating=1, published_at="2026-11-10T12:00:00Z")
    assert await repos.reviews.save_reviews([new], source.id, mark_sent=False) == 0
    async with db.require_connection().execute("SELECT * FROM reviews") as cursor:
        row = await cursor.fetchone()
    assert row["text"] == "Edited" and row["rating"] == 1
    assert row["published_at"] == new.published_at and row["is_sent_to_telegram"] == 1


@pytest.mark.asyncio
async def test_old_saved_review_is_not_proof_of_period_coverage(store):
    db, repos, source = store
    await repos.reviews.save_reviews([review(source, "old", "2026-10-01T00:00:00Z")],
                                    source.id, mark_sent=True)
    await repos.reviews.mark_source_initialized(source.id)
    snapshot = read_snapshot(db.path, Period.parse("09.11.2026 16.11.2026"))
    assert not next(s for s in snapshot["sources"] if s["id"] == source.id)["coverage_complete"]


@pytest.mark.asyncio
async def test_mixed_history_page_does_not_hide_following_reviews(store, monkeypatch):
    import app.reports.history as history

    monkeypatch.setattr(history.asyncio, "sleep", AsyncMock())
    db, repos, source = store
    first = [review(source, "pinned", "2026-10-01T00:00:00Z")]
    first += [review(source, str(i), "2026-11-12T00:00:00Z") for i in range(49)]
    second = [review(source, "last", "2026-11-10T00:00:00Z")]
    client = SimpleNamespace(fetch_reviews=AsyncMock(side_effect=[
        (ReviewSyncStatus.SUCCESS_NEW_REVIEWS, first, None, 200),
        (ReviewSyncStatus.SUCCESS_NEW_REVIEWS, second, None, 200),
    ]), last_rating=None)
    loader = HistoricalReviewsLoader(SimpleNamespace(repository=repos.reviews,
        notify_since=None, settings=SimpleNamespace(reviews_request_pause_seconds=0)))
    period = Period.parse("09.11.2026 16.11.2026")
    assert await loader._load(client, source, period)
    assert client.fetch_reviews.await_count == 2
    snapshot = read_snapshot(db.path, period)
    assert len(snapshot["reviews"]) == 50
    assert next(s for s in snapshot["sources"] if s["id"] == source.id)["coverage_complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,calls,complete", [
    (ReviewSyncStatus.NETWORK_ERROR, 2, True),
    (ReviewSyncStatus.CAPTCHA, 1, False),
    (ReviewSyncStatus.RATE_LIMITED, 1, False),
])
async def test_transient_failure_retries_but_challenges_stop(store, monkeypatch,
                                                           status, calls, complete):
    import app.reports.history as history

    monkeypatch.setattr(history.asyncio, "sleep", AsyncMock())
    _, repos, source = store
    client = SimpleNamespace(fetch_reviews=AsyncMock(side_effect=[
        (status, [], "Failed", 403),
        (ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
         [review(source, "old", "2026-10-01T00:00:00Z")], None, 200),
    ]), last_rating=None)
    loader = HistoricalReviewsLoader(SimpleNamespace(repository=repos.reviews,
        notify_since=None, settings=SimpleNamespace(reviews_request_pause_seconds=0)))
    assert await loader._load(client, source, Period.parse("09.11.2026 16.11.2026")) is complete
    assert client.fetch_reviews.await_count == calls


@pytest.mark.asyncio
async def test_report_refresh_preserves_partial_delivery_and_public_payload(store):
    import json
    from unittest.mock import MagicMock

    from aiogram.exceptions import TelegramAPIError

    from app.alerts.service import AlertService
    from app.config import Settings

    _, repos, source = store
    item = replace(review(source, "partial", "2026-11-13T12:00:00Z"),
                   raw_payload_json='{"public":"original"}')
    await repos.reviews.save_reviews([item], source.id)
    attempts = []

    async def send(chat_id, **kwargs):
        attempts.append(chat_id)
        if chat_id == 222 and attempts.count(222) == 1:
            raise TelegramAPIError(method=MagicMock(), message="Temporary failure")

    alerts = AlertService(bot=SimpleNamespace(send_message=send),
        settings=Settings(_env_file=None, bot_token="123456:TEST", ADMIN_IDS="111,222"),
        alerts=repos.alerts, runtime_settings=repos.runtime_settings)
    saved = (await repos.reviews.get_recent_reviews(1))[0]
    with pytest.raises(TelegramAPIError):
        await alerts.send_review_alert(saved, source, repos.reviews)
    # Reproduce the exact report-triggered duplicate: a public JSON refresh arrives
    # while one recipient already has the alert and another is still pending.
    refreshed = replace(item, raw_payload_json='{"public":"refreshed"}')
    await repos.reviews.save_reviews([refreshed], source.id, mark_sent=True)
    saved = (await repos.reviews.get_recent_reviews(1))[0]
    payload = json.loads(saved.raw_payload_json)
    assert payload["targets"] == {"111": 1, "222": 0}
    assert payload["public"] == "refreshed" and saved.first_seen_at
    assert await alerts.send_review_alert(saved, source, repos.reviews)
    assert sorted(attempts) == [111, 222, 222]
    saved = (await repos.reviews.get_recent_reviews(1))[0]
    payload = json.loads(saved.raw_payload_json)
    assert set(payload["target_sent_at"]) == {"111", "222"}
    assert payload["public"] == "refreshed"


@pytest.mark.asyncio
async def test_blocked_recipient_is_terminal_and_never_blocks_other_targets(store):
    import json
    from unittest.mock import MagicMock

    from aiogram.exceptions import TelegramForbiddenError

    from app.alerts.service import AlertService
    from app.config import Settings

    _, repos, source = store
    await repos.reviews.save_reviews([review(source, "blocked", "2026-11-13T12:00:00Z")],
                                    source.id)
    attempts = []

    async def send(chat_id, **kwargs):
        attempts.append(chat_id)
        if chat_id == 111:
            raise TelegramForbiddenError(method=MagicMock(), message="Bot was blocked")

    alerts = AlertService(bot=SimpleNamespace(send_message=send),
        settings=Settings(_env_file=None, bot_token="123456:TEST", ADMIN_IDS="111,222"),
        alerts=repos.alerts, runtime_settings=repos.runtime_settings)
    saved = (await repos.reviews.get_recent_reviews(1))[0]
    assert await alerts.send_review_alert(saved, source, repos.reviews)
    saved = (await repos.reviews.get_recent_reviews(1))[0]
    assert json.loads(saved.raw_payload_json)["blocked_targets"] == {"111": "forbidden"}
    assert saved.is_sent_to_telegram
    assert not await repos.reviews.get_unsent_reviews()
    assert await alerts.send_review_alert(saved, source, repos.reviews)
    assert sorted(attempts) == [111, 222]


@pytest.mark.asyncio
async def test_history_captures_card_rating_even_without_reviews(store):
    db, repos, source = store
    client = SimpleNamespace(
        fetch_reviews=AsyncMock(
            return_value=(ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, [], None, 200)
        ),
        last_rating=4.9,
        last_total_count=86,
    )
    loader = HistoricalReviewsLoader(SimpleNamespace(repository=repos.reviews, notify_since=None))
    period = Period.parse("09.11.2026 16.11.2026")
    assert await loader._load(client, source, period)
    snapshot = read_snapshot(db.path, period)
    branch = next(item for item in snapshot["sources"] if item["id"] == source.id)
    assert branch["latest_rating"] == 4.9 and branch["latest_rating_count"] == 86
    assert not snapshot["reviews"]
