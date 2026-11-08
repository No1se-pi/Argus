import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.vk.auth_helper import VKAuthHelper, browser_proxy, website_token
from app.vk.likes import likes_access_token, likes_web_token


@pytest.fixture
async def login(tmp_path):
    workspace = SimpleNamespace(
        template_path=tmp_path / "template.pptx", settings=SimpleNamespace(set=AsyncMock())
    )
    helper = VKAuthHelper(public_url="https://kaiton.ru", port=8788, workspace=workspace)
    client = TestClient(TestServer(helper.app))
    await client.start_server()
    url = await helper.issue(123, 173208026)
    secret = urlparse(url).fragment
    headers = {"X-Argus-Auth": secret, "Origin": "https://kaiton.ru"}

    async def open_browser():
        helper.status = "waiting"
        helper.page = SimpleNamespace(
            url="https://vk.ru/club173208026?do-not-expose=private",
            screenshot=AsyncMock(return_value=b"jpeg"),
            mouse=SimpleNamespace(click=AsyncMock(), wheel=AsyncMock()),
            keyboard=SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock()),
        )
        helper.context = SimpleNamespace(close=AsyncMock())

    helper.open_browser = AsyncMock(side_effect=open_browser)
    yield helper, client, headers
    await helper.close()
    await client.close()


async def open_link(client, headers):
    response = await client.post("/vk-connect/action", json={"action": "open"}, headers=headers)
    assert response.status == 200
    cookie = response.cookies["argus_vk_login"].value
    return {**headers, "Cookie": f"argus_vk_login={cookie}"}


@pytest.mark.asyncio
async def test_link_is_fragment_only_and_page_contains_no_secret(login):
    helper, client, headers = login
    assert helper.ticket.secret_hash != headers["X-Argus-Auth"]
    response = await client.get("/vk-connect")
    assert headers["X-Argus-Auth"] not in await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    helper.open_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_and_single_browser_cookie_are_required(login):
    helper, client, headers = login
    for bad in [{}, {"X-Argus-Auth": "wrong"}, {**headers, "Origin": "https://evil.example"}]:
        response = await client.post("/vk-connect/action", json={"action": "open"}, headers=bad)
        assert response.status in {401, 403}
    helper.open_browser.assert_not_awaited()
    claimed = await open_link(client, headers)
    response = await client.post("/vk-connect/action", json={"action": "status"}, headers=headers)
    assert response.status == 403
    response = await client.post("/vk-connect/action", json={"action": "status"}, headers=claimed)
    data = await response.json()
    assert data == {"status": "waiting", "host": "vk.ru"}
    assert "private" not in str(data)
    response = await client.get("/vk-connect/frame", headers=claimed)
    assert await response.read() == b"jpeg"
    assert response.content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_expiry_and_reissue_invalidate_old_link(login):
    helper, client, headers = login
    helper.ticket.expires = time.monotonic() - 1
    response = await client.post("/vk-connect/action", json={"action": "open"}, headers=headers)
    assert response.status == 401
    await helper.issue(123, 173208026)
    response = await client.post("/vk-connect/action", json={"action": "open"}, headers=headers)
    assert response.status == 401


@pytest.mark.asyncio
async def test_another_admin_cannot_take_over_active_login(login):
    helper, _, _ = login
    with pytest.raises(ValueError):
        await helper.issue(456, 173208026)


@pytest.mark.asyncio
async def test_input_is_bounded_and_cancel_closes_browser(login):
    helper, client, headers = login
    claimed = await open_link(client, headers)
    for data in [
        {"action": "click", "x": -1, "y": 0},
        {"action": "type", "text": "x" * 1025},
        {"action": "key", "key": "Control+L"},
        {"action": "navigate", "url": "http://127.0.0.1"},
    ]:
        response = await client.post("/vk-connect/action", json=data, headers=claimed)
        assert response.status == 400
    await client.post(
        "/vk-connect/action", json={"action": "type", "text": "test"}, headers=claimed
    )
    helper.page.keyboard.insert_text.assert_awaited_once_with("test")
    context = helper.context
    response = await client.post("/vk-connect/action", json={"action": "cancel"}, headers=claimed)
    assert response.status == 200
    context.close.assert_awaited_once()
    assert helper.ticket is None


@pytest.mark.parametrize(
    "url,body,expected",
    [
        ("https://web.api.vk.ru/method/wall.get", "access_token=vk1.a.test", "vk1.a.test"),
        ("https://web.api.vk.ru.evil.example/method/wall.get", "access_token=vk1.a.test", None),
        ("http://web.api.vk.ru/method/wall.get", "access_token=vk1.a.test", None),
        ("https://web.api.vk.ru/login", "access_token=vk1.a.test", None),
        ("https://web.api.vk.ru/method/wall.get", "password=secret", None),
        ("https://web.api.vk.ru/method/wall.get", "access_token=vk2.a.test", None),
    ],
)
def test_observer_only_reads_vk_website_api_tokens(url, body, expected):
    assert website_token(SimpleNamespace(url=url, post_data=body)) == expected


@pytest.mark.asyncio
async def test_candidate_is_saved_only_after_successful_access_check(login):
    helper, _, _ = login
    helper.verify = AsyncMock(side_effect=[ValueError("no likes access"), None])
    task = asyncio.create_task(helper.capture())
    try:
        helper.candidates.put_nowait("rejected-token")
        for _ in range(20):
            await asyncio.sleep(0)
            if helper.status == "rejected":
                break
        helper.workspace.settings.set.assert_not_awaited()
        helper.candidates.put_nowait("accepted-token")
        await asyncio.wait_for(task, timeout=2)
        helper.workspace.settings.set.assert_awaited_once_with(
            "vk_web_access_token", "accepted-token", is_secret=True
        )
        assert helper.status == "connected"
    finally:
        task.cancel()


def test_new_runtime_web_token_overrides_stale_environment_without_restart():
    settings = SimpleNamespace(vk_web_access_token=None)
    config = SimpleNamespace(web_access_token="fresh-web", user_access_token="old-oauth")
    assert likes_access_token(settings, config) == "fresh-web"
    assert likes_web_token(settings, config) == "fresh-web"


@pytest.mark.parametrize(
    "url", ["http://kaiton.ru", "https://kaiton.ru/path", "https://u:p@kaiton.ru"]
)
def test_helper_requires_https_origin(url):
    with pytest.raises(ValueError):
        VKAuthHelper(public_url=url, port=8788, workspace=None)


def test_browser_and_api_use_explicit_vk_proxy_without_leaking_credentials():
    from pydantic import SecretStr

    from app.vk.client import configured_vk_proxy

    url = "http://user:pass%21@10.254.42.1:8790"
    settings = SimpleNamespace(vk_proxy_url=SecretStr(url), bot_proxy_url=SecretStr("http://other"))
    assert configured_vk_proxy(settings) == url
    assert browser_proxy(configured_vk_proxy(settings)) == {
        "server": "http://10.254.42.1:8790",
        "username": "user",
        "password": "pass!",
    }
    assert browser_proxy(None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,age,title,expected",
    [
        ("\u200c " * 10, 1, "ВКонтакте", "loading"),
        ("\u200c " * 10, 35, "ВКонтакте", "stalled"),
        ("Форма входа с номером телефона и подтверждением", 35, "Вход", "interactive"),
        (
            "Пройдите проверку безопасности для продолжения",
            35,
            "Проверяем, что вы не робот",
            "challenge",
        ),
    ],
)
async def test_loading_state_does_not_claim_login_is_ready(login, text, age, title, expected):
    helper, _, _ = login
    helper.page_opened_at = time.monotonic() - age
    helper.page = SimpleNamespace(
        locator=lambda _: SimpleNamespace(inner_text=AsyncMock(return_value=text)),
        title=AsyncMock(return_value=title),
    )
    assert await helper.page_view() == expected
