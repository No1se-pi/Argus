import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.alerts.service import AlertService
from app.config import Settings
from app.reviews.analyzer import (
    GeminiAnalysisResult,
    GeminiRateLimiter,
    GeminiReviewClient,
    GeminiReviewWorker,
    compute_deterministic_verdict,
)
from app.reviews.models import ReviewItem, ReviewPlatform, ReviewSource
from app.reviews.scheduler import ReviewsPollingScheduler
from app.reviews.service import ReviewsService
from app.storage.database import Database
from app.storage.models import ReviewAiAnalysis
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema


class DummyBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> MagicMock:
        self.sent_messages.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        msg = MagicMock()
        msg.message_id = len(self.sent_messages)
        return msg


@pytest.fixture
async def gemini_env(tmp_path):
    db_path = tmp_path / "test_gemini.sqlite3"
    database = Database(db_path)
    await database.connect()
    await init_schema(database)
    bundle = RepositoryBundle(database)

    settings = Settings(
        bot_token=SecretStr("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        alert_chat_id=-100123456789,
        admin_ids_text="99999",
        enable_reviews_monitor=True,
        enable_gemini_review_analysis=True,
        gemini_api_key=SecretStr("test_secret_api_key_1234567890abcdef"),
        gemini_model="gemini-3.5-flash-lite",
        gemini_timeout_seconds=5.0,
        gemini_max_rpm=10,
        gemini_max_retries=3,
        alerts_reviews_enabled=True,
    )
    bot = DummyBot()
    alerts = AlertService(
        bot=bot,  # type: ignore
        settings=settings,
        alerts=bundle.alerts,
        runtime_settings=bundle.runtime_settings,
    )

    source = ReviewSource(
        id=1,
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        external_id="1011075765",
        url="https://yandex.ru/maps/org/1011075765",
        is_active=True,
        is_initialized=True,
    )
    await bundle.reviews.ensure_default_sources(
        [
            {
                "platform": ReviewPlatform.YANDEX,
                "branch_name": "Центральное отделение",
                "external_id": "1011075765",
                "url": "https://yandex.ru/maps/org/1011075765",
            }
        ]
    )

    yield {
        "db": database,
        "bundle": bundle,
        "settings": settings,
        "bot": bot,
        "alerts": alerts,
        "source": source,
    }

    await database.close()


# ==============================================================================
# 1. Позитивный 5★ отзыв -> GREEN
# ==============================================================================
def test_1_positive_5_stars_green():
    res = GeminiAnalysisResult(
        summary="Прекрасная школа, отличные учителя.",
        sentiment="positive",
        criticism_found=False,
        has_hidden_negative=False,
        stars_text_conflict=False,
        severity="none",
        requires_attention=False,
    )
    verdict = compute_deterministic_verdict(res)
    assert verdict == "GREEN"


# ==============================================================================
# 2. 5★ + серьёзный негатив -> RED
# ==============================================================================
def test_2_5_stars_serious_negative_red():
    res = GeminiAnalysisResult(
        summary="Начали хвалить, но потом пожаловались на хамство и обман с оплатой.",
        sentiment="negative",
        criticism_found=True,
        has_hidden_negative=True,
        stars_text_conflict=True,
        severity="high",
        requires_attention=True,
    )
    verdict = compute_deterministic_verdict(res)
    assert verdict == "RED"


# ==============================================================================
# 3. 5★ + небольшое замечание -> YELLOW
# ==============================================================================
def test_3_5_stars_minor_remark_yellow():
    res = GeminiAnalysisResult(
        summary="Всё замечательно, но в холле было душно.",
        sentiment="positive",
        criticism_found=True,
        has_hidden_negative=False,
        stars_text_conflict=False,
        severity="low",
        requires_attention=False,
    )
    verdict = compute_deterministic_verdict(res)
    assert verdict == "YELLOW"


# ==============================================================================
# 4. 1★, но положительный текст -> stars_text_conflict
# ==============================================================================
def test_4_1_star_positive_text_conflict():
    res = GeminiAnalysisResult(
        summary="Ребёнку безумно понравились уроки, педагоги супер.",
        sentiment="positive",
        criticism_found=False,
        has_hidden_negative=False,
        stars_text_conflict=True,
        severity="none",
        requires_attention=False,
    )
    assert res.stars_text_conflict is True
    verdict = compute_deterministic_verdict(res)
    assert verdict == "YELLOW"


# ==============================================================================
# 5. Mixed review -> YELLOW
# ==============================================================================
def test_5_mixed_review_yellow():
    res = GeminiAnalysisResult(
        summary="Преподаватели хорошие, но организация хромает.",
        sentiment="mixed",
        criticism_found=True,
        has_hidden_negative=False,
        stars_text_conflict=False,
        severity="medium",
        requires_attention=False,
    )
    verdict = compute_deterministic_verdict(res)
    assert verdict == "YELLOW"


# ==============================================================================
# 6. Gemini timeout -> Telegram всё равно работает
# ==============================================================================
@pytest.mark.asyncio
async def test_6_gemini_timeout_telegram_still_delivered(gemini_env):
    bundle = gemini_env["bundle"]
    settings = gemini_env["settings"]
    bot = gemini_env["bot"]
    alerts = gemini_env["alerts"]

    mock_client = MagicMock(spec=GeminiReviewClient)
    mock_client.analyze_review = AsyncMock(side_effect=TimeoutError("Timeout"))
    mock_client.model = settings.gemini_model

    worker = GeminiReviewWorker(
        settings=settings,
        repository=bundle.reviews,
        alerts=alerts,
        client=mock_client,
    )

    review = ReviewItem(
        external_review_id="rev_timeout_1",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Анна",
        rating=5,
        text="Хороший отзыв",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)
    assert review.id is not None

    # Process review in worker
    saved = await worker.process_review(review.id)
    assert saved is not None
    assert saved.status == "AI_ERROR"
    assert saved.verdict == "UNKNOWN"

    # Telegram message must be sent despite AI timeout!
    assert len(bot.sent_messages) == 1
    sent_text = bot.sent_messages[0]["text"]
    assert "ИИ-анализ временно недоступен" in sent_text
    assert "Хороший отзыв" in sent_text


# ==============================================================================
# 7. Gemini 429 -> retry без повторного scraping
# ==============================================================================
@pytest.mark.asyncio
async def test_7_gemini_429_retry_without_rescraping(gemini_env):
    client = GeminiReviewClient(
        api_key="fake-key",
        timeout_seconds=2.0,
        max_rpm=600,
        max_retries=3,
    )
    client.rate_limiter = MagicMock()
    client.rate_limiter.acquire = AsyncMock()

    call_count = 0

    async def fake_generate_content(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("429 Too Many Requests")
        resp = MagicMock()
        resp.text = json.dumps(
            {
                "summary": "Успешно после retry",
                "sentiment": "positive",
                "criticism_found": False,
                "has_hidden_negative": False,
                "stars_text_conflict": False,
                "severity": "none",
                "requires_attention": False,
            }
        )
        return resp

    mock_sdk_client = MagicMock()
    mock_sdk_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    client._client = mock_sdk_client

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await client.analyze_review(rating=5, text="Отличный отзыв")

    assert res.summary == "Успешно после retry"
    assert call_count == 2
    mock_sleep.assert_called_once()


# ==============================================================================
# 8. Gemini 5xx -> bounded retry
# ==============================================================================
@pytest.mark.asyncio
async def test_8_gemini_5xx_bounded_retry(gemini_env):
    client = GeminiReviewClient(
        api_key="fake-key",
        timeout_seconds=1.0,
        max_rpm=600,
        max_retries=3,
    )
    client.rate_limiter = MagicMock()
    client.rate_limiter.acquire = AsyncMock()

    call_count = 0

    async def fake_fail(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("503 Service Unavailable")

    mock_sdk_client = MagicMock()
    mock_sdk_client.aio.models.generate_content = AsyncMock(side_effect=fake_fail)
    client._client = mock_sdk_client

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError) as exc_info:
            await client.analyze_review(rating=5, text="Текст")
        assert "503 Service Unavailable" in str(exc_info.value)

    assert call_count == 3  # Strictly bounded to max_retries


# ==============================================================================
# 9. Invalid JSON -> UNKNOWN
# ==============================================================================
def test_9_invalid_json_returns_unknown():
    verdict = compute_deterministic_verdict({"summary": "не валидный словарь"})
    assert verdict == "UNKNOWN"


# ==============================================================================
# 10. Неизвестное значение enum -> UNKNOWN
# ==============================================================================
def test_10_unknown_enum_returns_unknown():
    invalid_data = {
        "summary": "Нормально",
        "sentiment": "super_mega_positive",  # invalid enum
        "criticism_found": False,
        "has_hidden_negative": False,
        "stars_text_conflict": False,
        "severity": "critical_extreme",  # invalid enum
        "requires_attention": False,
    }
    verdict = compute_deterministic_verdict(invalid_data)
    assert verdict == "UNKNOWN"


# ==============================================================================
# 11. Повторная доставка одного review не вызывает Gemini повторно после SUCCESS
# ==============================================================================
@pytest.mark.asyncio
async def test_11_duplicate_review_delivery_no_gemini_retrigger(gemini_env):
    bundle = gemini_env["bundle"]
    settings = gemini_env["settings"]
    alerts = gemini_env["alerts"]

    review = ReviewItem(
        external_review_id="rev_dup_11",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Иван",
        rating=5,
        text="Хорошо",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)

    existing_analysis = ReviewAiAnalysis(
        id=None,
        review_id=review.id,
        status="SUCCESS",
        verdict="GREEN",
        summary="Уже проанализировано",
        sentiment="positive",
        severity="none",
    )
    await bundle.reviews.save_ai_analysis(existing_analysis)

    mock_client = MagicMock(spec=GeminiReviewClient)
    mock_client.analyze_review = AsyncMock()

    worker = GeminiReviewWorker(
        settings=settings,
        repository=bundle.reviews,
        alerts=alerts,
        client=mock_client,
    )

    res = await worker.process_review(review.id)
    assert res.status == "SUCCESS"
    assert res.summary == "Уже проанализировано"
    mock_client.analyze_review.assert_not_called()


# ==============================================================================
# 12. SQLite review сохраняется раньше AI
# ==============================================================================
@pytest.mark.asyncio
async def test_12_sqlite_review_committed_before_ai(gemini_env):
    bundle = gemini_env["bundle"]

    review = ReviewItem(
        external_review_id="rev_seq_12",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Ольга",
        rating=5,
        text="Классные курсы",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)

    # Review MUST exist in database immediately
    db_rev = await bundle.reviews.get_review_by_external_id("yandex", "rev_seq_12")
    assert db_rev is not None
    assert db_rev.id is not None

    # AI record must initially be PENDING
    ai_record = await bundle.reviews.get_ai_analysis(db_rev.id)
    assert ai_record is not None
    assert ai_record.status == "PENDING"
    assert ai_record.verdict == "UNKNOWN"


# ==============================================================================
# 13. Restart worker не теряет PENDING analyses
# ==============================================================================
@pytest.mark.asyncio
async def test_13_restart_worker_recovers_pending_analyses(gemini_env):
    bundle = gemini_env["bundle"]
    settings = gemini_env["settings"]
    alerts = gemini_env["alerts"]

    review = ReviewItem(
        external_review_id="rev_restart_13",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Павел",
        rating=5,
        text="Отличный сервис",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)

    mock_client = MagicMock(spec=GeminiReviewClient)
    mock_client.analyze_review = AsyncMock(
        return_value=GeminiAnalysisResult(
            summary="Отличный сервис",
            sentiment="positive",
            criticism_found=False,
            has_hidden_negative=False,
            stars_text_conflict=False,
            severity="none",
            requires_attention=False,
        )
    )
    mock_client.model = settings.gemini_model

    new_worker = GeminiReviewWorker(
        settings=settings,
        repository=bundle.reviews,
        alerts=alerts,
        client=mock_client,
    )
    await new_worker.start()

    # The pending review should have been enqueued on startup
    assert new_worker.queue.qsize() >= 1

    # Wait for worker to finish processing
    await asyncio.sleep(0.1)
    await new_worker.stop()

    updated_ai = await bundle.reviews.get_ai_analysis(review.id)
    assert updated_ai is not None
    assert updated_ai.status == "SUCCESS"
    assert updated_ai.verdict == "GREEN"


# ==============================================================================
# 14. 40 reviews queue не превышает configured RPM
# ==============================================================================
@pytest.mark.asyncio
async def test_14_40_reviews_queue_rate_limiter_rpm():
    limiter = GeminiRateLimiter(max_rpm=60)  # 60 RPM -> 1.0s interval
    assert limiter.min_interval == 1.0

    call_timestamps = []

    # Mock asyncio.sleep to record wait times without real delay
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        for _ in range(5):
            await limiter.acquire()
            call_timestamps.append(limiter._last_call_time)

    # First call needs 0 sleep, subsequent calls wait min_interval
    assert mock_sleep.call_count >= 4


# ==============================================================================
# 15. Отсутствие GEMINI_API_KEY не ломает Argus
# ==============================================================================
def test_15_missing_gemini_api_key_does_not_break_startup():
    s = Settings(
        bot_token=SecretStr("fake:token"),
        ADMIN_IDS="123",
        enable_gemini_review_analysis=False,
        gemini_api_key=None,
    )
    assert s.gemini_api_key is None
    assert s.enable_gemini_review_analysis is False


# ==============================================================================
# 16. AI disabled -> вообще 0 Gemini calls
# ==============================================================================
@pytest.mark.asyncio
async def test_16_ai_disabled_zero_gemini_calls(gemini_env):
    bundle = gemini_env["bundle"]
    alerts = gemini_env["alerts"]

    disabled_settings = Settings(
        bot_token=SecretStr("fake:token"),
        ADMIN_IDS="123",
        enable_gemini_review_analysis=False,
    )

    mock_client = MagicMock(spec=GeminiReviewClient)
    mock_client.analyze_review = AsyncMock()

    service = ReviewsService(
        settings=disabled_settings,
        runtime_settings=bundle.runtime_settings,
        repository=bundle.reviews,
        alerts=alerts,
    )

    review = ReviewItem(
        external_review_id="rev_disabled_16",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Мария",
        rating=5,
        text="Хороший центр",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)

    delivered = await service.deliver_pending_reviews()
    assert delivered == 1
    mock_client.analyze_review.assert_not_called()
    await service.close()


# ==============================================================================
# 17. API key нигде не появляется в logs
# ==============================================================================
@pytest.mark.asyncio
async def test_17_api_key_never_appears_in_logs(caplog):
    secret_key = "dummy_secret_key_never_log_xyz987"
    client = GeminiReviewClient(api_key=secret_key, max_rpm=600, max_retries=1)

    with caplog.at_level(logging.DEBUG):
        # Trigger an error scenario
        try:
            await client.analyze_review(rating=5, text="тест")
        except Exception:
            pass

    for record in caplog.records:
        assert secret_key not in record.message
        assert secret_key not in str(record.args)


# ==============================================================================
# 18. Successful AI analysis immediately leads to Telegram delivery
# ==============================================================================
@pytest.mark.asyncio
async def test_18_successful_analysis_immediately_delivers_to_telegram(gemini_env):
    bundle = gemini_env["bundle"]
    settings = gemini_env["settings"]
    bot = gemini_env["bot"]
    alerts = gemini_env["alerts"]

    mock_client = AsyncMock()
    mock_client.model = "gemini-3.5-flash-lite"
    mock_client.analyze_review.return_value = GeminiAnalysisResult(
        summary="Отличный центр и классные преподаватели.",
        sentiment="positive",
        criticism_found=False,
        has_hidden_negative=False,
        stars_text_conflict=False,
        severity="none",
        requires_attention=False,
    )

    worker = GeminiReviewWorker(
        settings=settings,
        repository=bundle.reviews,
        alerts=alerts,
        client=mock_client,
    )

    review = ReviewItem(
        external_review_id="rev_immediate_18",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Алексей",
        rating=5,
        text="Отличный центр и классные преподаватели.",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)
    db_rev = await bundle.reviews.get_review_by_external_id("yandex", "rev_immediate_18")
    assert db_rev is not None
    assert db_rev.is_sent_to_telegram is False

    # Process review with worker
    res = await worker.process_review(db_rev.id)
    assert res is not None
    assert res.status == "SUCCESS"
    assert res.verdict == "GREEN"

    # Must be delivered IMMEDIATELY without calling deliver_pending_reviews()
    assert len(bot.sent_messages) == 1
    assert "🟢" in bot.sent_messages[0]["text"]
    assert "Отличный центр" in bot.sent_messages[0]["text"]

    # In database, review must be marked as sent
    updated_rev = await bundle.reviews.get_review_by_id(db_rev.id)
    assert updated_rev is not None
    assert updated_rev.is_sent_to_telegram is True


# ==============================================================================
# 19. No duplicate Telegram delivery on worker / deliver_pending_reviews race
# ==============================================================================
@pytest.mark.asyncio
async def test_19_no_duplicate_telegram_delivery_on_worker_and_service_race(gemini_env):
    bundle = gemini_env["bundle"]
    settings = gemini_env["settings"]
    bot = gemini_env["bot"]
    alerts = gemini_env["alerts"]

    mock_client = AsyncMock()
    mock_client.model = "gemini-3.5-flash-lite"
    mock_client.analyze_review.return_value = GeminiAnalysisResult(
        summary="Всё супер.",
        sentiment="positive",
        criticism_found=False,
        has_hidden_negative=False,
        stars_text_conflict=False,
        severity="none",
        requires_attention=False,
    )

    worker = GeminiReviewWorker(
        settings=settings,
        repository=bundle.reviews,
        alerts=alerts,
        client=mock_client,
    )
    service = ReviewsService(
        settings=settings,
        runtime_settings=bundle.runtime_settings,
        repository=bundle.reviews,
        alerts=alerts,
        ai_worker=worker,
    )

    review = ReviewItem(
        external_review_id="rev_race_19",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Иван",
        rating=5,
        text="Всё супер.",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)
    db_rev = await bundle.reviews.get_review_by_external_id("yandex", "rev_race_19")
    assert db_rev is not None

    # Run worker.process_review and service.deliver_pending_reviews concurrently
    results = await asyncio.gather(
        worker.process_review(db_rev.id),
        service.deliver_pending_reviews(),
        return_exceptions=True,
    )
    for r in results:
        assert not isinstance(r, Exception), f"Concurrent execution raised: {r}"

    # Exactly ONE Telegram message must be sent
    assert len(bot.sent_messages) == 1
    updated_rev = await bundle.reviews.get_review_by_id(db_rev.id)
    assert updated_rev.is_sent_to_telegram is True
    await service.close()


# ==============================================================================
# 20. PENDING review after simulated restart processed without new reviews
# ==============================================================================
@pytest.mark.asyncio
async def test_20_pending_review_processed_after_simulated_restart_without_new_reviews(gemini_env):
    bundle = gemini_env["bundle"]
    settings = gemini_env["settings"]
    bot = gemini_env["bot"]
    alerts = gemini_env["alerts"]

    # 1. Review is saved in SQLite before restart (AI=PENDING, is_sent_to_telegram=0)
    review = ReviewItem(
        external_review_id="rev_restart_20",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Дмитрий",
        rating=5,
        text="Хорошая подготовка к экзаменам",
        published_at="2026-10-01T12:00:00Z",
    )
    await bundle.reviews.save_reviews([review], source_id=1, mark_sent=False)
    db_rev = await bundle.reviews.get_review_by_external_id("yandex", "rev_restart_20")
    assert db_rev is not None
    assert db_rev.is_sent_to_telegram is False

    ai_rec = await bundle.reviews.get_ai_analysis(db_rev.id)
    assert ai_rec is not None
    assert ai_rec.status == "PENDING"

    # 2. Simulate process restart: create a new ReviewsService and scheduler
    mock_client = AsyncMock()
    mock_client.model = "gemini-3.5-flash-lite"
    mock_client.analyze_review.return_value = GeminiAnalysisResult(
        summary="Хорошая подготовка к экзаменам.",
        sentiment="positive",
        criticism_found=False,
        has_hidden_negative=False,
        stars_text_conflict=False,
        severity="none",
        requires_attention=False,
    )

    worker = GeminiReviewWorker(
        settings=settings,
        repository=bundle.reviews,
        alerts=alerts,
        client=mock_client,
    )

    mock_yandex = AsyncMock()
    mock_yandex.fetch_reviews.return_value = []
    mock_yandex.extract_rating_and_count.return_value = (None, None)

    new_service = ReviewsService(
        settings=settings,
        runtime_settings=bundle.runtime_settings,
        repository=bundle.reviews,
        alerts=alerts,
        yandex_client=mock_yandex,
        ai_worker=worker,
    )
    scheduler = ReviewsPollingScheduler(settings=settings, service=new_service)

    # 3. Scheduler runs poll cycle with 0 new reviews
    await scheduler._poll_cycle(wait_after=False)

    # Wait for background worker to process recovered review
    for _ in range(50):
        if len(bot.sent_messages) > 0:
            break
        await asyncio.sleep(0.05)

    assert len(bot.sent_messages) == 1
    assert "Хорошая подготовка" in bot.sent_messages[0]["text"]

    updated_ai = await bundle.reviews.get_ai_analysis(db_rev.id)
    assert updated_ai is not None
    assert updated_ai.status == "SUCCESS"
    assert updated_ai.verdict == "GREEN"

    await new_service.close()


# ==============================================================================
# 21. Quota pause recovers after cooldown
# ==============================================================================
@pytest.mark.asyncio
async def test_21_quota_pause_recovers_after_cooldown(gemini_env):
    bundle = gemini_env["bundle"]
    settings = gemini_env["settings"]
    alerts = gemini_env["alerts"]

    mock_client = AsyncMock()
    mock_client.model = "gemini-3.5-flash-lite"
    mock_client.analyze_review.return_value = GeminiAnalysisResult(
        summary="Отличный сервис после кулдауна.",
        sentiment="positive",
        criticism_found=False,
        has_hidden_negative=False,
        stars_text_conflict=False,
        severity="none",
        requires_attention=False,
    )

    worker = GeminiReviewWorker(
        settings=settings,
        repository=bundle.reviews,
        alerts=alerts,
        client=mock_client,
    )

    # Save 2 reviews
    rev1 = ReviewItem(
        external_review_id="rev_qp_1",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Ольга",
        rating=5,
        text="Первый отзыв",
        published_at="2026-10-01T12:00:00Z",
    )
    rev2 = ReviewItem(
        external_review_id="rev_qp_2",
        platform=ReviewPlatform.YANDEX,
        branch_name="Центральное отделение",
        author_name="Сергей",
        rating=5,
        text="Второй отзыв",
        published_at="2026-10-01T12:01:00Z",
    )
    await bundle.reviews.save_reviews([rev1, rev2], source_id=1, mark_sent=False)
    db_rev1 = await bundle.reviews.get_review_by_external_id("yandex", "rev_qp_1")
    db_rev2 = await bundle.reviews.get_review_by_external_id("yandex", "rev_qp_2")
    assert db_rev1 is not None
    assert db_rev2 is not None

    # 1. Trigger quota pause with 0.15s cooldown
    worker.pause_quota(cooldown_seconds=0.15)
    assert worker.is_quota_paused() is True

    # 2. Process review 1 while quota paused -> must NOT call analyze_review
    res1 = await worker.process_review(db_rev1.id)
    assert res1 is not None
    assert res1.status == "QUOTA_PAUSED"
    assert res1.verdict == "UNKNOWN"
    mock_client.analyze_review.assert_not_called()

    # 3. Wait for cooldown to expire
    await asyncio.sleep(0.2)
    assert worker.is_quota_paused() is False

    # 4. Process review 2 after cooldown -> must call analyze_review and succeed
    res2 = await worker.process_review(db_rev2.id)
    assert res2 is not None
    assert res2.status == "SUCCESS"
    assert res2.verdict == "GREEN"
    assert mock_client.analyze_review.call_count == 1
