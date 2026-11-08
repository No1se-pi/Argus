from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.bot.vk_likes_ui import check_auth
from app.vk.client import VKAPIError, vk_error_text
from app.vk.likes import likes_access_token


def test_service_key_takes_priority_and_user_token_is_fallback():
    config = SimpleNamespace(user_access_token="user")
    assert likes_access_token(SimpleNamespace(), config) == "user"
    assert (
        likes_access_token(SimpleNamespace(vk_service_access_token=SecretStr("service")), config)
        == "service"
    )


def test_unsupported_profile_explains_action_without_echoing_remote_secrets():
    message = vk_error_text(VKAPIError("remote-token-must-not-leak", code=1051))
    assert "1051" in message
    assert "VK_SERVICE_ACCESS_TOKEN" in message
    assert "remote-token-must-not-leak" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "denied", "empty"])
async def test_access_check_probes_likes_and_does_not_claim_success_on_1051(monkeypatch, mode):
    config = SimpleNamespace(user_access_token="user", group_id=173208026)
    settings = SimpleNamespace(vk_api_version="5.199", vk_service_access_token=SecretStr("service"))
    service = SimpleNamespace(settings=settings, effective_config=AsyncMock(return_value=config))
    workspace = SimpleNamespace(service=SimpleNamespace(vk_service=service))
    wall = {"items": [] if mode == "empty" else [{"id": 7730}]}
    responses = [wall, [], []]
    if mode == "denied":
        responses = [wall, VKAPIError("unavailable profile type", code=1051)]
    client = SimpleNamespace(request=AsyncMock(side_effect=responses))
    def factory(**kwargs):
        return client
    monkeypatch.setattr("app.bot.vk_likes_ui.VKClient", factory)
    show = AsyncMock()
    monkeypatch.setattr("app.bot.vk_likes_ui.show", show)
    monkeypatch.setattr("app.bot.vk_likes_ui.asyncio.sleep", AsyncMock())
    await check_auth(
        SimpleNamespace(answer=AsyncMock()), SimpleNamespace(clear=AsyncMock()), workspace
    )
    message = show.call_args.args[1]
    assert "VK_SERVICE_ACCESS_TOKEN" in message
    if mode == "denied":
        assert "1051" in message
        assert "Можно запускать сбор" not in message
    elif mode == "empty":
        assert "не проверен" in message
        assert client.request.await_count == 1
    else:
        assert "Можно запускать сбор" in message
        assert [call.args[0] for call in client.request.await_args_list] == [
            "wall.get",
            "likes.getList",
            "users.get",
        ]
