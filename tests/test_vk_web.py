from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.vk.client import VKClient
from app.vk.likes import likes_access_token


def test_web_token_has_priority_over_other_keys():
    settings = SimpleNamespace(
        vk_web_access_token=SecretStr("web-key"),
        vk_service_access_token=SecretStr("service-key"),
    )
    assert likes_access_token(settings, SimpleNamespace(user_access_token="user")) == "web-key"


@pytest.mark.asyncio
async def test_web_transport_posts_credentials_in_body_without_cookies(monkeypatch):
    captured = {}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self, **kwargs):
            return {"response": {"count": 1, "items": [42]}}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr("app.vk.client.aiohttp.ClientSession", Session)
    client = VKClient(access_token="test-web-secret", api_version="5.199", web_mode=True)
    result = await client.get_likes_page(173208026, 7731, offset=50)
    assert result == {"count": 1, "items": [42]}
    assert captured["url"] == "https://web.api.vk.ru/method/likes.getList"
    assert "access_token" not in captured["params"]
    assert captured["data"]["access_token"] == "test-web-secret"
    assert captured["data"]["offset"] == 50
    assert captured["data"]["friends_only"] == 0
    assert captured["data"]["extended"] == 0
    assert captured["params"] == {"v": "5.289", "client_id": "6287487"}
    assert "Cookie" not in captured["headers"]
