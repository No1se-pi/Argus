import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.storage.database import Database
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema
from app.vk.scheduler import VKPollingScheduler
from app.vk.service import VKEffectiveConfig, VKService, VKSyncResult


@pytest.fixture
async def vk_env(tmp_path: Path):
    database = Database(tmp_path / "argus.sqlite3")
    await database.connect()
    await init_schema(database)
    repositories = RepositoryBundle(database)
    settings = Settings(
        bot_token="123456:test",
        admin_ids_text="1",
        vk_group_token="env-group-token",
        vk_user_access_token="env-user-token",
        vk_group_id=111,
    )
    service = VKService(
        settings=settings,
        runtime_settings=repositories.runtime_settings,
        repository=repositories.vk,
    )
    yield settings, repositories, service
    await database.close()


async def test_runtime_vk_config_overrides_env(vk_env):
    _, repositories, service = vk_env
    await repositories.runtime_settings.set("vk_group_token", "runtime-group", is_secret=True)
    await repositories.runtime_settings.set(
        "vk_user_access_token", "runtime-user", is_secret=True
    )
    await repositories.runtime_settings.set("vk_group_id", "222", is_secret=False)

    config = await service.effective_config()

    assert config.group_token == "runtime-group"
    assert config.user_access_token == "runtime-user"
    assert config.group_id == 222


class FakeVKClient:
    longpoll_state_calls = 0

    def __init__(self, **_kwargs):
        pass

    async def get_group(self, group_id: int):
        return {"id": group_id, "name": "Test group", "screen_name": "test"}

    async def get_long_poll_server(self, _group_id: int):
        type(self).longpoll_state_calls += 1
        return {
            "server": "https://longpoll.test",
            "key": f"key-{self.longpoll_state_calls}",
            "ts": str(self.longpoll_state_calls),
        }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self.payload


class FakeSession:
    responses = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, *_args, **_kwargs):
        return FakeResponse(type(self).responses.pop(0))


async def test_wall_reply_new_is_deduplicated_and_alerted_once(vk_env):
    settings, repositories, service = vk_env
    event = {
        "ts": "2",
        "updates": [
            {
                "type": "wall_reply_new",
                "object": {
                    "comment": {
                        "id": 55,
                        "post_id": 44,
                        "owner_id": -111,
                        "from_id": 7,
                        "date": int(time.time()) + 60,
                        "text": "new comment",
                    }
                },
            }
        ],
    }
    FakeSession.responses = [event, event]
    FakeVKClient.longpoll_state_calls = 0

    with (
        patch("app.vk.service.VKClient", FakeVKClient),
        patch("app.vk.service.aiohttp.ClientSession", FakeSession),
    ):
        first = await service.longpoll_once(wait_seconds=1)
        second = await service.longpoll_once(wait_seconds=1)

    assert [comment.comment_id for comment in first.new_comments] == [55]
    assert second.new_comments == []
    assert await repositories.vk.count_comments(111) == 1

    alerts = AsyncMock()
    scheduler = VKPollingScheduler(settings=settings, service=service, alerts=alerts)
    await scheduler._send_alerts(first)
    await scheduler._send_alerts(second)
    alerts.send_vk_comment_alert.assert_awaited_once_with(first.new_comments[0])


async def test_longpoll_refreshes_expired_key_in_same_attempt(vk_env):
    _, _, service = vk_env
    FakeSession.responses = [
        {"failed": 2},
        {"ts": "3", "updates": []},
    ]
    FakeVKClient.longpoll_state_calls = 0

    with (
        patch("app.vk.service.VKClient", FakeVKClient),
        patch("app.vk.service.aiohttp.ClientSession", FakeSession),
    ):
        result = await service.longpoll_once(wait_seconds=1)

    assert result.new_comments == []
    assert FakeVKClient.longpoll_state_calls == 2


async def test_scheduler_keeps_running_after_python310_asyncio_timeout(vk_env, monkeypatch):
    settings, _, _ = vk_env

    class Python310AsyncioTimeoutError(Exception):
        pass

    class Service:
        calls = 0

        async def effective_config(self):
            return VKEffectiveConfig(True, "group", None, 111, "longpoll")

        async def longpoll_once(self, **_kwargs):
            self.calls += 1
            return VKSyncResult(111, 0, 0, [], [])

        def reset_longpoll_state(self):
            pass

    service = Service()
    scheduler = VKPollingScheduler(settings=settings, service=service, alerts=AsyncMock())
    real_wait_for = __import__("asyncio").wait_for
    waits = 0

    async def python310_wait_for(awaitable, timeout):
        nonlocal waits
        waits += 1
        if waits % 2:
            return await real_wait_for(awaitable, timeout)
        awaitable.close()
        if waits == 4:
            scheduler.stop()
        raise Python310AsyncioTimeoutError

    monkeypatch.setattr("app.vk.scheduler.asyncio.TimeoutError", Python310AsyncioTimeoutError)
    monkeypatch.setattr("app.vk.scheduler.asyncio.wait_for", python310_wait_for)

    await scheduler.run()

    assert service.calls == 2
