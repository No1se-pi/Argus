import pytest
from types import SimpleNamespace

from app.services.llm.classifier import MessageClassifier
from app.services.llm.ollama_client import OllamaClient, OllamaUnavailable
from app.services.telegram_monitoring import prefilter_priority, telegram_message_link
from app.services.daily_digest import DailyDigestScheduler, DailyDigestService
from app.time import MOSCOW, local_date_iso, resolve_timezone
from app.collectors.telegram import TelegramCollector
from app.storage.database import Database
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema


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
    )
    assert created is True
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
