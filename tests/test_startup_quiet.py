from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.alerts.timing import is_live
from app.config import Settings
from app.services.telegram_monitoring import AnalysisWorker, TelegramRealtimeMonitor
from app.storage.database import Database
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema
from app.vk.scheduler import VKPollingScheduler

UTC = timezone.utc  # noqa: UP017 - Python 3.10


@pytest.mark.asyncio
async def test_manual_start_has_no_background_network_or_workers(tmp_path, monkeypatch):
    import app.main as application

    settings = Settings(
        _env_file=None,
        bot_token="123456:TEST_ONLY",
        database_path=tmp_path / "argus.db",
        enable_background_monitors=False,
        enable_telegram_monitor=True,
        enable_reviews_monitor=True,
        enable_vk_monitor=True,
    )
    monkeypatch.setattr(application, "get_settings", lambda: settings)
    telethon = AsyncMock(side_effect=AssertionError("Telethon must not start"))
    monkeypatch.setattr(application, "TelegramClient", telethon)
    vk = AsyncMock()
    reviews = AsyncMock()
    analysis = AsyncMock()
    monkeypatch.setattr(application.VKPollingScheduler, "run", vk)
    monkeypatch.setattr(application.ReviewsPollingScheduler, "run", reviews)
    monkeypatch.setattr(application.AnalysisWorker, "run", analysis)
    polling = AsyncMock()
    dispatcher = SimpleNamespace(
        start_polling=polling, resolve_used_update_types=lambda: ["message"]
    )
    monkeypatch.setattr(application, "create_dispatcher", lambda **kwargs: dispatcher)
    await application.main()
    polling.assert_awaited_once()
    telethon.assert_not_called()
    vk.assert_not_called()
    reviews.assert_not_called()
    analysis.assert_not_called()


@pytest.fixture
async def collection(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    await init_schema(db)
    repositories = RepositoryBundle(db)
    source = await repositories.sources.upsert_telegram_source(
        link="@example",
        username="example",
        title="Test",
        entity_id=100,
        access_hash=200,
        entity_type="channel",
    )
    settings = SimpleNamespace(telegram_catchup_limit=200, telegram_analysis_queue_limit=1000)
    yield repositories, source, settings
    await db.close()


def message(message_id):
    return SimpleNamespace(
        id=message_id,
        message="Historical",
        date=datetime.now(UTC) - timedelta(days=2),
        reply_to_msg_id=None,
    )


@pytest.mark.asyncio
async def test_new_telegram_source_starts_at_head_without_loading_oldest_messages(collection):
    repos, source, settings = collection
    client = SimpleNamespace(get_messages=AsyncMock(return_value=[message(500)]))
    monitor = TelegramRealtimeMonitor(client=client, settings=settings, repositories=repos)
    await monitor.catch_up()
    assert client.get_messages.await_count == 1
    assert client.get_messages.await_args.kwargs == {"limit": 1}
    assert (await repos.sources.get_source(source.id)).last_message_id == 500
    assert await repos.telegram_monitoring.pending_count() == 0


@pytest.mark.asyncio
async def test_restart_catchup_is_silent_and_moves_shared_cursor_to_head(collection):
    repos, source, settings = collection
    await repos.sources.set_last_message_id(source.id, 100)
    client = SimpleNamespace(
        get_messages=AsyncMock(
            side_effect=[
                [message(500)],
                [message(500), message(499)],
            ]
        )
    )
    monitor = TelegramRealtimeMonitor(client=client, settings=settings, repositories=repos)
    await monitor.catch_up()
    rows = await repos.telegram_monitoring.pending(20)
    assert {row["telegram_message_id"] for row in rows} == {499, 500}
    assert all(row["alert_eligible"] == 0 for row in rows)
    assert (await repos.sources.get_source(source.id)).last_message_id == 500
    assert client.get_messages.await_args.kwargs == {"limit": 200, "min_id": 100, "max_id": 501}
    await monitor._store(await repos.sources.get_source(source.id), message(501))
    rows = await repos.telegram_monitoring.pending(20)
    assert next(row for row in rows if row["telegram_message_id"] == 501)["alert_eligible"] == 1


@pytest.mark.asyncio
async def test_live_risk_poll_uses_own_cursor_and_keeps_overflow(collection):
    repos, source, settings = collection
    settings.telegram_catchup_limit = 2
    await repos.sources.set_last_message_id(source.id, 500)
    await repos.runtime_settings.set(f"telegram_risk_cursor:{source.id}", "100")
    client = SimpleNamespace(get_messages=AsyncMock(side_effect=[
        [message(600)], [message(101), message(102)],
    ]))
    monitor = TelegramRealtimeMonitor(client=client, settings=settings, repositories=repos)
    await monitor.catch_up(alert_eligible=True)
    assert await repos.runtime_settings.get(f"telegram_risk_cursor:{source.id}") == "102"
    assert (await repos.sources.get_source(source.id)).last_message_id == 500
    assert client.get_messages.await_args.kwargs["reverse"] is True
    rows = await repos.telegram_monitoring.pending(20)
    assert {row["telegram_message_id"] for row in rows} == {101, 102}
    assert all(row["alert_eligible"] == 1 for row in rows)


@pytest.mark.asyncio
async def test_old_pending_analysis_never_sends_risk_alerts():
    repository = SimpleNamespace(
        save_analysis=AsyncMock(return_value=True),
        create_risk_alert=AsyncMock(return_value=1),
        mark_error=AsyncMock(),
    )
    classifier = SimpleNamespace(
        classify=AsyncMock(return_value=SimpleNamespace(risk=True, severity=3)),
        client=SimpleNamespace(model="test"),
    )
    worker = AnalysisWorker(
        settings=SimpleNamespace(llm_max_concurrent_requests=1, llm_alert_min_severity=2),
        repositories=SimpleNamespace(
            telegram_monitoring=repository,
            runtime_settings=SimpleNamespace(get_bool=AsyncMock(return_value=True)),
        ),
        classifier=classifier,
        bot=None,
    )
    worker._send_alert = AsyncMock()
    row = {
        "id": 1,
        "text": "old",
        "alert_eligible": 1,
        "message_date": (worker._started_at - timedelta(days=1)).isoformat(),
    }
    await worker._process(row)
    worker._send_alert.assert_not_awaited()
    row.update(id=2, message_date=(worker._started_at + timedelta(seconds=1)).isoformat())
    await worker._process(row)
    worker._send_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_vk_historical_poll_results_are_not_announced():
    alerts = SimpleNamespace(send_vk_post_alert=AsyncMock(), send_vk_comment_alert=AsyncMock())
    scheduler = VKPollingScheduler(settings=None, service=None, alerts=alerts)
    old = SimpleNamespace(date=(scheduler._started_at - timedelta(days=1)).isoformat())
    new = SimpleNamespace(date=(scheduler._started_at + timedelta(seconds=1)).isoformat())
    await scheduler._send_alerts(SimpleNamespace(new_posts=[old, new], new_comments=[old, new]))
    alerts.send_vk_post_alert.assert_awaited_once_with(new)
    alerts.send_vk_comment_alert.assert_awaited_once_with(new)


def test_alert_cutoff_handles_unknown_and_naive_dates():
    since = datetime(2026, 11, 7, 4, tzinfo=UTC)
    assert not is_live("2026-10-19T22:57:00+03:00", since)
    assert not is_live(None, since)
    assert not is_live("invalid", since)
    assert is_live("2026-11-07T04:00:00Z", since)
    assert is_live("2026-11-07T04:00:00", since)
