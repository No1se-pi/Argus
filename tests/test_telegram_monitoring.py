import pytest
from types import SimpleNamespace

from app.services.llm.classifier import MessageClassifier
from app.services.llm.ollama_client import OllamaClient, OllamaUnavailable
from app.services.telegram_monitoring import prefilter_priority, telegram_message_link
from app.services.daily_digest import DailyDigestScheduler, DailyDigestService
from app.time import MOSCOW, local_date_iso, period_utc_bounds, resolve_timezone
from app.bot.keyboards import main_menu_keyboard
from app.collectors.telegram import TelegramCollector
from app.storage.database import Database
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema


@pytest.mark.asyncio
async def test_historical_charts_do_not_require_daily_digest_cache(tmp_path):
    from datetime import datetime, timedelta, timezone
    from app.services.llm.classifier import Classification

    db = Database(tmp_path / "charts.sqlite3")
    await db.connect()
    try:
        await init_schema(db)
        repos = RepositoryBundle(db)
        source = await repos.sources.upsert_telegram_source(
            link="@history", username="history", title="History", entity_id=123,
            access_hash=456, entity_type="channel",
        )
        message_id, _ = await repos.telegram_monitoring.save_message(
            source_id=source.id, telegram_message_id=1, reply_to_message_id=None,
            text="Historical", message_date=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),
            message_url=None, prefilter_priority=0, alert_eligible=False,
        )
        await repos.telegram_monitoring.save_analysis(message_id,
            Classification(False,0,.9,[],"positive","other","Test","none"),"test","v1")
        rows = await repos.telegram_monitoring.chart_rows(30)
        assert sum(row["messages_count"] for row in rows) == 1
        assert sum(row["positive_count"] for row in rows) == 1
        assert not await repos.telegram_monitoring.list_risk_alerts(days=30)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_llm_status_button_displays_model_and_queue(monkeypatch):
    from unittest.mock import AsyncMock
    import app.bot.callbacks as callbacks

    edit = AsyncMock()
    monkeypatch.setattr(callbacks, "_edit", edit)
    await callbacks.monitoring_llm_callback(
        SimpleNamespace(),
        SimpleNamespace(ollama_enabled=True, ollama_model="test-model"),
        SimpleNamespace(pending_count=AsyncMock(return_value=7)),
        SimpleNamespace(average_latency_ms=100, last_error=None),
    )
    edit.assert_awaited_once()
    assert "test-model" in edit.await_args.args[1]
    assert "7" in edit.await_args.args[1]


@pytest.mark.asyncio
async def test_expired_callback_ack_does_not_prevent_screen_update():
    from unittest.mock import AsyncMock
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import AnswerCallbackQuery
    from app.bot.callbacks import _edit

    message = SimpleNamespace(edit_text=AsyncMock())
    query = SimpleNamespace(message=message, answer=AsyncMock(side_effect=TelegramBadRequest(
        method=AnswerCallbackQuery(callback_query_id="expired"),
        message="Bad Request: query is too old and response timeout expired or query ID is invalid",
    )))
    await _edit(query, "Updated screen")
    message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_button_finishes_without_llm_status_variables(monkeypatch):
    from unittest.mock import AsyncMock
    import app.bot.callbacks as callbacks

    edit = AsyncMock()
    monkeypatch.setattr(callbacks, "_edit", edit)
    collector = SimpleNamespace(backfill_analysis=AsyncMock(
        return_value=SimpleNamespace(fetched_count=2, saved_count=2)))
    await callbacks.monitoring_backfill_callback(
        SimpleNamespace(data="tgm:backfill:50", answer=AsyncMock()), collector,
        SimpleNamespace(list_sources=AsyncMock(return_value=[SimpleNamespace(id=1)])),
    )
    edit.assert_awaited_once()
    collector.backfill_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_risk_delivery_retries_only_failed_target_after_restart(tmp_path):
    from unittest.mock import AsyncMock
    from datetime import datetime, timezone
    from app.services.telegram_monitoring import AnalysisWorker

    db = Database(tmp_path / "deliveries.sqlite3")
    await db.connect()
    try:
        await init_schema(db)
        repos = RepositoryBundle(db)
        source = await repos.sources.upsert_telegram_source(
            link="@test", username="test", title="Test", entity_id=123,
            access_hash=456, entity_type="channel",
        )
        message_id, _ = await repos.telegram_monitoring.save_message(
            source_id=source.id, telegram_message_id=1, reply_to_message_id=None,
            text="Test", message_date=datetime.now(timezone.utc).isoformat(),
            message_url=None, prefilter_priority=5,
        )
        alert_id = await repos.telegram_monitoring.create_risk_alert(message_id)
        await repos.telegram_monitoring.queue_risk_deliveries(alert_id, [1, 2], "Test alert")
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError("network"), None]))
        worker = AnalysisWorker(settings=SimpleNamespace(llm_max_concurrent_requests=1),
                                repositories=repos, classifier=None, bot=bot)
        await worker._deliver_alerts()
        assert bot.send_message.await_count == 2
        await worker._deliver_alerts()
        assert bot.send_message.await_count == 2  # Retry deadline has not arrived.
        connection = db.require_connection()
        await connection.execute("UPDATE telegram_risk_deliveries SET next_retry_at=NULL")
        await connection.commit()
        await db.close()
        await db.connect()
        restarted_bot = SimpleNamespace(send_message=AsyncMock())
        restarted_worker = AnalysisWorker(settings=SimpleNamespace(llm_max_concurrent_requests=1),
                                          repositories=RepositoryBundle(db), classifier=None, bot=restarted_bot)
        await restarted_worker._deliver_alerts()
        restarted_bot.send_message.assert_awaited_once()
        assert restarted_bot.send_message.await_args.args[0] == 1
        assert not await repos.telegram_monitoring.pending_risk_deliveries()
    finally:
        await db.close()


def test_valid_llm_json_is_strictly_parsed():
    result = MessageClassifier.parse(
        '{"risk":true,"severity":2,"confidence":0.87,'
        '"categories":["alcohol","event_safety"],"sentiment":"neutral",'
        '"topic":"event","reason":"Есть намерение","intent_level":"planned"}'
    )
    assert result.risk is True
    assert result.severity == 2
    assert result.confidence == pytest.approx(0.87)


@pytest.mark.parametrize("raw", ["not json", "{}", '{"risk":"yes"}'])
def test_invalid_llm_output_is_rejected(raw):
    with pytest.raises(ValueError):
        MessageClassifier.parse(raw)


def test_prefilter_prioritizes_risk_but_does_not_classify():
    assert prefilter_priority("Го пронесём перцовку и водку на дискач") >= 5
    assert prefilter_priority("Мне нравится мальчик, дайте его юз") == 0


def test_semantic_severity_mapping_repairs_small_model_contradiction():
    result = MessageClassifier.parse(
        '{"risk":false,"severity":0,"confidence":0.9,'
        '"categories":["prohibited_item","alcohol","event_safety"],'
        '"sentiment":"neutral","topic":"event","reason":"Есть конкретный план",'
        '"intent_level":"planned"}'
    )
    assert result.risk is True
    assert result.severity == 2


def test_telegram_link_generation():
    assert telegram_message_link("school", 123, 7) == "https://t.me/school/7"
    assert telegram_message_link(None, 123, 7) == "https://t.me/c/123/7"


@pytest.mark.asyncio
async def test_message_deduplication(tmp_path):
    database = Database(tmp_path / "argus.sqlite3")
    await database.connect()
    await init_schema(database)
    repositories = RepositoryBundle(database)
    source = await repositories.sources.upsert_telegram_source(
        link="@test", username="test", title="Test", entity_id=123,
        access_hash=456, entity_type="channel",
    )
    args = dict(source_id=source.id, telegram_message_id=10, reply_to_message_id=None,
                text="hello", message_date="2026-10-06T10:00:00+00:00",
                message_url="https://t.me/test/10", prefilter_priority=0)
    first_id, first_created = await repositories.telegram_monitoring.save_message(**args)
    second_id, second_created = await repositories.telegram_monitoring.save_message(**args)
    assert first_id == second_id
    assert first_created is True
    assert second_created is False
    await repositories.telegram_monitoring.save_message(
        **{**args, "telegram_message_id": 11, "prefilter_priority": 10, "alert_eligible": False}
    )
    rows = await repositories.telegram_monitoring.pending(1)
    assert rows[0]["id"] == first_id  # A historical backfill must not delay live messages.
    await database.close()


@pytest.mark.asyncio
async def test_unavailable_ollama_is_nonfatal():
    client = OllamaClient("http://127.0.0.1:1", "test", timeout=0.05)
    with pytest.raises(OllamaUnavailable):
        await client.generate(system="test", prompt="test")


def test_daily_digest_time_validation():
    assert DailyDigestScheduler._parse_time("09:30") == (9, 30)
    with pytest.raises(ValueError):
        DailyDigestScheduler._parse_time("25:00")


def test_moscow_timezone_fallback_and_local_date(monkeypatch):
    import app.time as time_module

    def missing(_name):
        raise time_module.ZoneInfoNotFoundError

    monkeypatch.setattr(time_module, "ZoneInfo", missing)
    assert resolve_timezone("Europe/Moscow") is MOSCOW
    assert len(local_date_iso("Europe/Moscow")) == 10
    start, end = period_utc_bounds("Europe/Moscow", 1)
    assert start < end


def test_main_menu_exposes_telegram_analytics():
    labels = [button.text for row in main_menu_keyboard().inline_keyboard for button in row]
    assert "🔭 TG Аналитика" in labels


def test_private_invite_hash_parsing():
    collector = object.__new__(TelegramCollector)
    assert collector._invite_hash("https://t.me/+zOCU-V6SGucxOTVi") == "zOCU-V6SGucxOTVi"
    assert collector._invite_hash("https://t.me/joinchat/abc123") == "abc123"
    assert collector._invite_hash("https://t.me/public_name") is None


@pytest.mark.asyncio
async def test_daily_aggregation_and_alert_feedback(tmp_path):
    database = Database(tmp_path / "argus.sqlite3")
    await database.connect()
    await init_schema(database)
    repositories = RepositoryBundle(database)
    source = await repositories.sources.upsert_telegram_source(
        link="@digest", username="digest", title="Digest", entity_id=777,
        access_hash=888, entity_type="channel",
    )
    message_id, _ = await repositories.telegram_monitoring.save_message(
        source_id=source.id, telegram_message_id=1, reply_to_message_id=None,
        text="planned event", message_date="2026-10-06T10:00:00+00:00",
        message_url="https://t.me/digest/1", prefilter_priority=5,
    )
    result = MessageClassifier.parse(
        '{"risk":true,"severity":2,"confidence":0.9,'
        '"categories":["event_safety"],"sentiment":"negative",'
        '"topic":"event","reason":"plan","intent_level":"planned"}'
    )
    assert await repositories.telegram_monitoring.save_analysis(
        message_id, result, "test", "test-v1"
    )
    assert await repositories.telegram_monitoring.create_risk_alert(message_id)
    alerts = await repositories.telegram_monitoring.list_risk_alerts(days=3650)
    assert len(alerts) == 1
    assert await repositories.telegram_monitoring.set_alert_feedback(
        alerts[0]["alert_id"], "confirmed", 123
    )
    stats = await repositories.telegram_monitoring.aggregate_day("2026-10-06", source.id)
    assert stats["messages_count"] == 1
    assert stats["negative_count"] == 1
    assert stats["severity_2_count"] == 1
    summary = await repositories.telegram_monitoring.monitoring_summary(
        "2026-10-06T00:00:00+00:00", "2026-10-06T23:59:59+00:00"
    )
    assert summary["collected"] == 1
    assert summary["analyzed"] == 1
    assert summary["negative"] == 1
    assert summary["sources"][0]["label"] == "Digest"

    settings = SimpleNamespace(alert_chat_id=None, admin_ids={123})
    service = DailyDigestService(
        settings=settings, repositories=repositories, bot=SimpleNamespace(), llm_client=None
    )
    digest = await service.build("2026-10-06")
    assert "Сообщений: <b>1</b>" in digest
    assert "severity 2: 1" in digest
    await database.close()


@pytest.mark.asyncio
async def test_analysis_retry_backoff_and_reply_context(tmp_path):
    database = Database(tmp_path / "argus.sqlite3")
    await database.connect()
    await init_schema(database)
    repositories = RepositoryBundle(database)
    source = await repositories.sources.upsert_telegram_source(
        link="@reply", username="reply", title="Reply", entity_id=991,
        access_hash=992, entity_type="channel",
    )
    await repositories.telegram_monitoring.save_message(
        source_id=source.id, telegram_message_id=10, reply_to_message_id=None,
        text="original context", message_date="2026-10-06T10:00:00+00:00",
        message_url=None, prefilter_priority=0,
    )
    child_id, _ = await repositories.telegram_monitoring.save_message(
        source_id=source.id, telegram_message_id=11, reply_to_message_id=10,
        text="reply", message_date="2026-10-06T10:01:00+00:00",
        message_url=None, prefilter_priority=1,
    )
    rows = await repositories.telegram_monitoring.pending(10)
    child = next(row for row in rows if row["id"] == child_id)
    assert child["reply_text"] == "original context"

    await repositories.telegram_monitoring.mark_error(child_id, "Ollama unavailable")
    pending_ids = {row["id"] for row in await repositories.telegram_monitoring.pending(10)}
    assert child_id not in pending_ids
    connection = database.require_connection()
    async with connection.execute(
        "SELECT analysis_attempts, next_analysis_at FROM telegram_messages WHERE id=?",
        (child_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row["analysis_attempts"] == 1
    assert row["next_analysis_at"] is not None
    await database.close()


@pytest.mark.asyncio
async def test_deferred_queue_promotion_preserves_messages(tmp_path):
    database = Database(tmp_path / "argus.sqlite3")
    await database.connect()
    await init_schema(database)
    repositories = RepositoryBundle(database)
    source = await repositories.sources.upsert_telegram_source(
        link="@queue", username="queue", title="Queue", entity_id=881,
        access_hash=882, entity_type="channel",
    )
    deferred_id, created = await repositories.telegram_monitoring.save_message(
        source_id=source.id, telegram_message_id=1, reply_to_message_id=None,
        text="deferred", message_date="2026-10-06T10:00:00+00:00",
        message_url=None, prefilter_priority=5, analysis_status="deferred",
        alert_eligible=False,
    )
    assert created is True
    connection = database.require_connection()
    async with connection.execute(
        "SELECT alert_eligible FROM telegram_messages WHERE id=?", (deferred_id,)
    ) as cursor:
        stored = await cursor.fetchone()
    assert stored["alert_eligible"] == 0
    assert await repositories.telegram_monitoring.pending(10) == []
    assert await repositories.telegram_monitoring.promote_deferred(1) == 1
    rows = await repositories.telegram_monitoring.pending(10)
    assert [row["id"] for row in rows] == [deferred_id]
    await database.close()


@pytest.mark.asyncio
async def test_digest_requests_plain_text_from_ollama():
    class FakeLlm:
        def __init__(self):
            self.json_mode = None

        async def generate(self, *, system, prompt, json_mode=True):
            self.json_mode = json_mode
            return "Краткая сводка"

    class FakeMonitoring:
        async def digest_rows(self, date, source_id=None):
            return [{
                "source_id": 1, "sentiment": "neutral", "topic": "event",
                "severity": 0, "text": "message",
            }]

    llm = FakeLlm()
    repositories = SimpleNamespace(telegram_monitoring=FakeMonitoring())
    settings = SimpleNamespace(alert_chat_id=None, admin_ids={123})
    service = DailyDigestService(
        settings=settings, repositories=repositories, bot=SimpleNamespace(), llm_client=llm
    )
    text = await service.build("2026-10-06")
    assert llm.json_mode is False
    assert "Краткая сводка" in text
